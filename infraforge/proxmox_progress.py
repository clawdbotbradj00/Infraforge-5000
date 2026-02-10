"""Proxmox task progress monitor for real-time deployment tracking.

Polls the Proxmox API during terraform apply to show detailed progress:
- Clone task detection and progress percentage
- Disk resize operations
- VM creation and configuration phases
- Green checkmarks as each phase completes

Usage:
    monitor = ProxmoxProgressMonitor(proxmox_client, node, log_fn)
    monitor.start()
    # ... run terraform apply ...
    monitor.stop()
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


# Proxmox task type labels for human-readable display
_TASK_TYPE_LABELS = {
    "qmclone": "Cloning VM",
    "qmcreate": "Creating VM",
    "qmconfig": "Configuring VM",
    "qmstart": "Starting VM",
    "qmstop": "Stopping VM",
    "qmresize": "Resizing disk",
    "qmmove": "Moving disk",
    "qmmigrate": "Migrating VM",
    "vzdump": "Backup",
    "vzrestore": "Restoring",
    "vzcreate": "Creating container",
    "vzstart": "Starting container",
    "vzstop": "Stopping container",
    "download": "Downloading",
    "imgcopy": "Copying image",
    "resize": "Resizing disk",
    "move_volume": "Moving volume",
    "pull": "Pulling",
}


@dataclass
class TaskProgress:
    """Snapshot of a tracked Proxmox task."""
    upid: str
    task_type: str
    vmid: str = ""
    status: str = "running"  # "running", "stopped"
    exit_status: str = ""    # "OK", error message
    progress: Optional[float] = None  # 0.0-100.0 if available
    log_lines: list[str] = field(default_factory=list)
    start_time: float = 0.0
    label: str = ""

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def is_ok(self) -> bool:
        return self.status == "stopped" and self.exit_status == "OK"

    @property
    def is_failed(self) -> bool:
        return self.status == "stopped" and self.exit_status != "OK"

    @property
    def type_label(self) -> str:
        return _TASK_TYPE_LABELS.get(self.task_type, self.task_type)

    def format_progress_bar(self, width: int = 20) -> str:
        """Render a text-based progress bar.

        Returns a string like: [============        ] 60%
        """
        if self.progress is None:
            return ""
        pct = max(0.0, min(100.0, self.progress))
        filled = int(width * pct / 100)
        empty = width - filled
        bar = "=" * filled + " " * empty
        return f"\\[{bar}] {pct:.0f}%"

    def format_status_line(self) -> str:
        """Format a Rich-markup status line for this task."""
        vmid_str = f" {self.vmid}" if self.vmid else ""

        if self.is_ok:
            return (
                f"[green]  \\[OK] {self.type_label}{vmid_str}[/green]"
            )
        elif self.is_failed:
            return (
                f"[red]  \\[FAIL] {self.type_label}{vmid_str}"
                f" -- {self.exit_status}[/red]"
            )
        else:
            # Running
            progress_str = ""
            if self.progress is not None:
                pct = max(0.0, min(100.0, self.progress))
                filled = int(20 * pct / 100)
                empty = 20 - filled
                bar = "=" * filled + " " * empty
                progress_str = f"  \\[{bar}] {pct:.0f}%"
            return (
                f"[cyan]  [bold]>>>[/bold] {self.type_label}"
                f"{vmid_str}{progress_str}[/cyan]"
            )


class ProxmoxProgressMonitor:
    """Polls Proxmox API for task progress during deployment.

    Designed to run in a background thread alongside terraform apply,
    reporting live progress through a callback function.

    Args:
        proxmox_client: A connected ProxmoxClient instance.
        node: The target Proxmox node name.
        log_fn: Callback that receives Rich-markup strings for display.
        poll_interval: Seconds between API polls (default 2.0).
    """

    def __init__(
        self,
        proxmox_client,
        node: str,
        log_fn: Callable[[str], None],
        poll_interval: float = 2.0,
    ):
        self._client = proxmox_client
        self._node = node
        self._log_fn = log_fn
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time: float = 0.0
        self._tracked_tasks: dict[str, TaskProgress] = {}
        self._reported_complete: set[str] = set()
        self._last_log_lines: dict[str, int] = {}  # upid -> last line number read

    def start(self):
        """Begin polling in a background thread.

        Records the current time as the baseline; only tasks started
        after this point will be tracked.
        """
        self._start_time = time.time()
        self._stop_event.clear()
        self._tracked_tasks.clear()
        self._reported_complete.clear()
        self._last_log_lines.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="proxmox-progress-monitor",
        )
        self._thread.start()

    def stop(self):
        """Signal the polling loop to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        # Do a final poll to catch any completed tasks
        self._poll_once()
        # Report any remaining completed tasks
        self._report_final_status()

    def _poll_loop(self):
        """Main polling loop that runs in the background thread."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                pass  # Don't crash the monitor on transient API errors
            self._stop_event.wait(timeout=self._poll_interval)

    def _poll_once(self):
        """Single poll iteration: discover new tasks, update tracked tasks."""
        # Discover new tasks on the node that started after our baseline
        try:
            recent_tasks = self._client.get_node_tasks(
                self._node,
                limit=20,
                since=self._start_time,
            )
        except Exception:
            return

        for task_data in recent_tasks:
            upid = task_data.get("upid", "")
            if not upid:
                continue

            task_type = task_data.get("type", "")
            vmid = str(task_data.get("id", ""))

            if upid not in self._tracked_tasks:
                # New task discovered
                tp = TaskProgress(
                    upid=upid,
                    task_type=task_type,
                    vmid=vmid,
                    start_time=float(task_data.get("starttime", 0)),
                )
                self._tracked_tasks[upid] = tp
                self._log_fn(
                    f"[cyan]  [bold]>>>[/bold] Proxmox: "
                    f"{tp.type_label}"
                    f"{' -> VMID ' + vmid if vmid else ''}"
                    f"...[/cyan]"
                )

        # Update status of all tracked tasks
        for upid, tp in self._tracked_tasks.items():
            if upid in self._reported_complete:
                continue

            try:
                status = self._client.get_task_status(self._node, upid)
            except Exception:
                continue

            tp.status = status.get("status", tp.status)
            tp.exit_status = status.get("exitstatus", "")

            # Try to extract progress from task log
            progress = self._extract_progress(upid, tp)
            if progress is not None:
                old_progress = tp.progress
                tp.progress = progress
                # Log progress updates at meaningful intervals
                if old_progress is None or (progress - (old_progress or 0)) >= 5:
                    self._log_fn(tp.format_status_line())

            # Report completion
            if tp.status == "stopped" and upid not in self._reported_complete:
                self._reported_complete.add(upid)
                if tp.is_ok:
                    self._log_fn(
                        f"[green]  \\[OK] {tp.type_label}"
                        f"{' VMID ' + tp.vmid if tp.vmid else ''}"
                        f"[/green]"
                    )
                else:
                    self._log_fn(
                        f"[red]  \\[FAIL] {tp.type_label}"
                        f"{' VMID ' + tp.vmid if tp.vmid else ''}"
                        f" -- {tp.exit_status}[/red]"
                    )

    def _extract_progress(self, upid: str, tp: TaskProgress) -> Optional[float]:
        """Try to extract a progress percentage from task log lines.

        Proxmox reports progress for clone operations and disk copies as
        percentage values in the task log. Example log lines:
            'drive-scsi0: transferred 1.0 GiB of 10.0 GiB (10.00%)'
            'transferred 5.0 GiB of 10.0 GiB (50.00%)'
        """
        try:
            start_line = self._last_log_lines.get(upid, 0)
            log_entries = self._client.get_task_log(
                self._node, upid, start=start_line, limit=50,
            )
        except Exception:
            return tp.progress

        if not log_entries:
            return tp.progress

        # Update our read position
        max_line = max(entry.get("n", 0) for entry in log_entries)
        self._last_log_lines[upid] = max_line

        # Scan log entries for progress percentages
        best_progress = tp.progress
        for entry in log_entries:
            text = entry.get("t", "")
            if not text:
                continue

            # Look for percentage patterns like "(45.00%)" or "45%"
            pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
            if pct_match:
                try:
                    pct = float(pct_match.group(1))
                    if 0 <= pct <= 100:
                        if best_progress is None or pct > best_progress:
                            best_progress = pct
                except ValueError:
                    pass

        return best_progress

    def _report_final_status(self):
        """Report final status for any tasks that completed during the apply."""
        for upid, tp in self._tracked_tasks.items():
            if upid in self._reported_complete:
                continue
            # Try one last status check
            try:
                status = self._client.get_task_status(self._node, upid)
                tp.status = status.get("status", tp.status)
                tp.exit_status = status.get("exitstatus", "")
            except Exception:
                pass

            if tp.status == "stopped":
                self._reported_complete.add(upid)
                if tp.is_ok:
                    self._log_fn(
                        f"[green]  \\[OK] {tp.type_label}"
                        f"{' VMID ' + tp.vmid if tp.vmid else ''}"
                        f"[/green]"
                    )
                elif tp.is_failed:
                    self._log_fn(
                        f"[red]  \\[FAIL] {tp.type_label}"
                        f"{' VMID ' + tp.vmid if tp.vmid else ''}"
                        f" -- {tp.exit_status}[/red]"
                    )

    @property
    def active_tasks(self) -> list[TaskProgress]:
        """Return list of currently running tasks."""
        return [
            tp for tp in self._tracked_tasks.values()
            if tp.is_running
        ]

    @property
    def completed_tasks(self) -> list[TaskProgress]:
        """Return list of completed tasks."""
        return [
            tp for tp in self._tracked_tasks.values()
            if not tp.is_running
        ]

    @property
    def all_tasks(self) -> list[TaskProgress]:
        """Return all tracked tasks."""
        return list(self._tracked_tasks.values())

    @property
    def has_failures(self) -> bool:
        """Return True if any tracked task failed."""
        return any(tp.is_failed for tp in self._tracked_tasks.values())

    def get_summary(self) -> str:
        """Return a Rich-markup summary of all tracked tasks."""
        if not self._tracked_tasks:
            return "[dim]  No Proxmox tasks detected[/dim]"

        lines = []
        for tp in self._tracked_tasks.values():
            lines.append(tp.format_status_line())
        return "\n".join(lines)
