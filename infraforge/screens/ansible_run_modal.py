"""Ansible Run Modal — target selection, ping sweep, and live execution.

A full-screen modal overlay with three phases:

Phase 0 — Target Selection
    Enter IP ranges manually or import from IPAM subnets.

Phase 1 — Ping Sweep
    Validate which hosts are alive before running.

Phase 2 — Execution
    Stream ``ansible-playbook`` output live with elapsed timer.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, Static
from textual import work

from infraforge.ansible_runner import (
    PlaybookInfo,
    generate_inventory,
    parse_ip_ranges,
    ping_sweep,
    run_playbook,
)

if TYPE_CHECKING:
    pass


class AnsibleRunModal(ModalScreen):
    """Multi-phase modal for running an Ansible playbook."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Cancel/Close", show=True),
    ]

    DEFAULT_CSS = """
    AnsibleRunModal {
        align: center middle;
    }

    #run-outer {
        width: 90%;
        height: 90%;
        border: round $accent;
        background: $surface;
    }

    #run-title {
        dock: top;
        width: 100%;
        height: 3;
        padding: 1 2;
        background: $primary-background;
        color: $accent;
        text-style: bold;
        content-align: left middle;
    }

    #run-content {
        height: 1fr;
        padding: 1 2;
    }

    #run-phase-content {
        width: 100%;
    }

    #run-actions {
        dock: bottom;
        height: 3;
        layout: horizontal;
        content-align: right middle;
        padding: 0 2;
        background: $primary-background;
    }

    #run-actions Button {
        margin: 0 1;
    }

    #run-status {
        dock: bottom;
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    #run-ip-input {
        margin: 1 0;
    }

    .run-output-line {
        width: 100%;
    }
    """

    def __init__(self, playbook: PlaybookInfo) -> None:
        super().__init__()
        self._playbook = playbook
        self._phase: int = 0
        self._resolved_ips: list[str] = []
        self._alive_ips: list[str] = []
        self._dead_ips: list[str] = []
        self._scan_total: int = 0
        self._scan_done: int = 0
        self._scan_alive: int = 0
        self._is_scanning: bool = False
        self._is_running: bool = False
        self._run_start: float = 0.0
        self._run_timer: Timer | None = None
        self._exit_code: int | None = None
        self._log_path: Path | None = None
        self._process: subprocess.Popen | None = None
        self._aborted: bool = False
        self._subnets: list[dict] = []
        self._ipam_loaded: bool = False

    # ------------------------------------------------------------------
    # Compose / Mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        from textual.containers import Container

        with Container(id="run-outer"):
            yield Static("", id="run-title", markup=True)
            with VerticalScroll(id="run-content"):
                yield Static("", id="run-phase-content", markup=True)
            yield Static("", id="run-status", markup=True)
            with Horizontal(id="run-actions"):
                yield Button("Cancel", variant="default", id="run-cancel-btn")
                yield Button("Scan Hosts", variant="primary", id="run-action-btn")

    def on_mount(self) -> None:
        self._render_phase()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_handle_escape(self) -> None:
        if self._is_running:
            self._abort_execution()
        elif self._is_scanning:
            pass  # Let scan finish
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "run-cancel-btn":
            if self._is_running:
                self._abort_execution()
            elif self._phase == 1 and not self._is_scanning:
                # Back to target selection
                self._phase = 0
                self._render_phase()
            else:
                self.app.pop_screen()

        elif btn_id == "run-action-btn":
            if self._phase == 0:
                self._start_scan()
            elif self._phase == 1 and not self._is_scanning:
                self._start_execution()
            elif self._phase == 2 and not self._is_running:
                self.app.pop_screen()

        elif btn_id == "run-ipam-btn":
            self._load_ipam_subnets()

    # ------------------------------------------------------------------
    # Phase rendering
    # ------------------------------------------------------------------

    def _render_phase(self) -> None:
        if self._phase == 0:
            self._render_target_selection()
        elif self._phase == 1:
            self._render_ping_sweep()
        elif self._phase == 2:
            self._render_execution()

    def _render_target_selection(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(f"[bold]Run: {self._playbook.filename}[/bold]  Phase 1/3: Target Selection")

        content = self.query_one("#run-phase-content", Static)

        # Check if IPAM is configured
        ipam_cfg = getattr(self.app.config, "ipam", None)
        has_ipam = ipam_cfg and getattr(ipam_cfg, "url", "")

        lines = [
            "[bold]Define target hosts for this playbook.[/bold]",
            "",
            "Enter IP addresses, CIDR ranges, or dash ranges separated by commas.",
            "[dim]Examples: 10.0.1.0/24, 10.0.5.1-10.0.5.100, 192.168.1.50[/dim]",
            "",
        ]
        if has_ipam:
            lines.append("[dim]Or press the IPAM button below to import from phpIPAM subnets.[/dim]")
            lines.append("")

        if self._subnets:
            lines.append("[bold]IPAM Subnets:[/bold]")
            for s in self._subnets:
                sid = s.get("id", "?")
                addr = s.get("subnet", "?")
                mask = s.get("mask", "?")
                desc = s.get("description", "")
                usage = s.get("usage", {})
                used = usage.get("used", "?")
                maxh = usage.get("maxhosts", "?")
                lines.append(
                    f"  [{sid}] {addr}/{mask}  {desc}  ({used}/{maxh} used)"
                )
            lines.append("")
            lines.append("[dim]Type a subnet ID in the input to import its addresses.[/dim]")
            lines.append("[dim]Or type IP ranges directly.[/dim]")

        content.update("\n".join(lines))

        # Mount input if not already there
        scroll = self.query_one("#run-content", VerticalScroll)
        existing_inputs = self.query("#run-ip-input")
        if not existing_inputs:
            ip_input = Input(
                placeholder="e.g. 10.0.1.0/24, 10.0.5.1-100",
                id="run-ip-input",
            )
            scroll.mount(ip_input)
            if has_ipam and not self._ipam_loaded:
                ipam_btn = Button("Import from IPAM", variant="warning", id="run-ipam-btn")
                scroll.mount(ipam_btn)
            ip_input.focus()

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Scan Hosts"
        action_btn.variant = "primary"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Cancel"

        status = self.query_one("#run-status", Static)
        status.update("[dim]Enter target IPs and press Scan Hosts[/dim]")

    def _render_ping_sweep(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 2/3: Host Validation"
        )

        # Remove input widgets from phase 0
        for w in self.query("#run-ip-input"):
            w.remove()
        for w in self.query("#run-ipam-btn"):
            w.remove()

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        if self._is_scanning:
            action_btn.label = "Scanning..."
            action_btn.disabled = True
            cancel_btn.label = "Cancel"
        else:
            action_btn.label = "Run Playbook"
            action_btn.variant = "success"
            action_btn.disabled = len(self._alive_ips) == 0
            cancel_btn.label = "Back"

    def _render_execution(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 3/3: Execution"
        )

        # Clear prior content
        content = self.query_one("#run-phase-content", Static)
        content.update("")

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        if self._is_running:
            action_btn.label = "Running..."
            action_btn.disabled = True
            cancel_btn.label = "Abort"
        else:
            action_btn.label = "Close"
            action_btn.variant = "default"
            action_btn.disabled = False
            cancel_btn.label = "Close"

    # ------------------------------------------------------------------
    # Phase 0 → 1: Start scan
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        ip_input = self.query_one("#run-ip-input", Input)
        text = ip_input.value.strip()
        if not text:
            self.query_one("#run-status", Static).update(
                "[bold red]Enter at least one IP address or range[/bold red]"
            )
            return

        # Check if user typed a subnet ID (for IPAM import)
        if text.isdigit() and self._subnets:
            self._import_ipam_subnet(text)
            return

        try:
            self._resolved_ips = parse_ip_ranges(text)
        except Exception as e:
            self.query_one("#run-status", Static).update(
                f"[bold red]Invalid IP range: {e}[/bold red]"
            )
            return

        if not self._resolved_ips:
            self.query_one("#run-status", Static).update(
                "[bold red]No valid IPs in the given range[/bold red]"
            )
            return

        if len(self._resolved_ips) > 5000:
            self.query_one("#run-status", Static).update(
                f"[bold yellow]Warning: {len(self._resolved_ips)} IPs — "
                f"scan may take a while[/bold yellow]"
            )

        self._phase = 1
        self._scan_total = len(self._resolved_ips)
        self._scan_done = 0
        self._scan_alive = 0
        self._is_scanning = True
        self._render_phase()
        self._run_ping_sweep()

    @work(thread=True, exclusive=True, group="ansible-scan")
    def _run_ping_sweep(self) -> None:
        content_lines = [
            f"Scanning {self._scan_total} hosts...",
            "",
        ]
        self.app.call_from_thread(
            self._update_phase_content, "\n".join(content_lines)
        )

        def on_result(ip: str, alive: bool) -> None:
            self._scan_done += 1
            if alive:
                self._scan_alive += 1
            self.app.call_from_thread(self._update_scan_progress)

        alive, dead = ping_sweep(
            self._resolved_ips,
            workers=50,
            callback=on_result,
        )

        self._alive_ips = alive
        self._dead_ips = dead
        self._is_scanning = False
        self.app.call_from_thread(self._show_scan_results)

    def _update_scan_progress(self) -> None:
        content = self.query_one("#run-phase-content", Static)
        pct = (self._scan_done / self._scan_total * 100) if self._scan_total else 0
        content.update(
            f"Scanning {self._scan_total} hosts...\n\n"
            f"[bold]{self._scan_done}[/bold]/{self._scan_total} scanned "
            f"({pct:.0f}%)    "
            f"[green]{self._scan_alive} alive[/green]"
        )
        status = self.query_one("#run-status", Static)
        status.update(
            f"[dim]Pinging... {self._scan_done}/{self._scan_total}[/dim]"
        )

    def _show_scan_results(self) -> None:
        dead_count = len(self._dead_ips)
        alive_count = len(self._alive_ips)

        lines = [
            f"[bold]Scan complete:[/bold]  "
            f"[green]{alive_count} alive[/green], "
            f"[red]{dead_count} unreachable[/red] "
            f"out of {self._scan_total}",
            "",
        ]

        if alive_count:
            lines.append("[bold]Alive hosts:[/bold]")
            for ip in self._alive_ips:
                lines.append(f"  [green]{ip}[/green]")
            lines.append("")
        else:
            lines.append("[bold red]No alive hosts found. Cannot run playbook.[/bold red]")

        self._update_phase_content("\n".join(lines))

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Run Playbook"
        action_btn.variant = "success"
        action_btn.disabled = alive_count == 0

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Back"

        status = self.query_one("#run-status", Static)
        if alive_count:
            status.update(
                f"[dim]{alive_count} hosts ready — press Run Playbook[/dim]"
            )
        else:
            status.update("[dim]No alive hosts — press Back to try again[/dim]")

    # ------------------------------------------------------------------
    # Phase 1 → 2: Start execution
    # ------------------------------------------------------------------

    def _start_execution(self) -> None:
        self._phase = 2
        self._is_running = True
        self._aborted = False
        self._run_start = time.monotonic()
        self._render_phase()
        self._start_run_timer()
        self._execute_playbook()

    @work(thread=True, exclusive=True, group="ansible-run")
    def _execute_playbook(self) -> None:
        # Generate temp inventory
        inv_path = generate_inventory(self._alive_ips)

        # Compute log path
        playbook_dir = self._playbook.path.parent
        log_dir = playbook_dir / "logs"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_name = f"{self._playbook.path.stem}_{ts}.log"
        self._log_path = log_dir / log_name

        exit_code = -1
        try:
            for line, stream_type in run_playbook(
                self._playbook.path, inv_path, self._log_path
            ):
                if self._aborted:
                    break
                self.app.call_from_thread(
                    self._append_output, line, stream_type
                )
        except Exception as e:
            self.app.call_from_thread(
                self._append_output, f"Error: {e}\n", "status"
            )

        # Clean up temp inventory
        try:
            inv_path.unlink(missing_ok=True)
        except Exception:
            pass

        self._is_running = False
        self.app.call_from_thread(self._on_execution_done)

    def _append_output(self, line: str, stream_type: str) -> None:
        scroll = self.query_one("#run-content", VerticalScroll)
        css_class = "run-output-line"
        if stream_type == "status":
            markup = f"[bold cyan]{self._esc(line)}[/bold cyan]"
        else:
            markup = self._esc(line.rstrip())
        widget = Static(markup, classes=css_class, markup=True)
        scroll.mount(widget)
        scroll.scroll_end(animate=False)

    def _on_execution_done(self) -> None:
        self._stop_run_timer()
        elapsed = time.monotonic() - self._run_start

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Close"
        action_btn.variant = "default"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Close"

        status = self.query_one("#run-status", Static)
        if self._aborted:
            status.update(f"[bold yellow]Aborted after {elapsed:.0f}s[/bold yellow]")
        else:
            status.update(
                f"[dim]Finished in {elapsed:.0f}s  |  "
                f"Log: {self._log_path}[/dim]"
            )

        title = self.query_one("#run-title", Static)
        if self._aborted:
            title.update(
                f"[bold]Run: {self._playbook.filename}[/bold]  "
                f"[bold yellow]Aborted[/bold yellow]"
            )
        else:
            title.update(
                f"[bold]Run: {self._playbook.filename}[/bold]  "
                f"[bold green]Done ({elapsed:.0f}s)[/bold green]"
            )

    def _abort_execution(self) -> None:
        self._aborted = True
        self._stop_run_timer()
        # The run_playbook generator uses Popen internally;
        # we need to signal it to stop. Since we set _aborted,
        # the loop in _execute_playbook will break.
        # For faster abort, try to find and kill the ansible-playbook process
        try:
            import signal
            import os as _os
            # Kill any child ansible-playbook processes
            pid = _os.getpid()
            result = subprocess.run(
                ["pkill", "-P", str(pid), "ansible-playbook"],
                capture_output=True,
            )
        except Exception:
            pass

        self._append_output("\n--- Aborted by user ---\n", "status")

    # ------------------------------------------------------------------
    # IPAM integration
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="ansible-ipam")
    def _load_ipam_subnets(self) -> None:
        self.app.call_from_thread(
            self._set_status, "Loading IPAM subnets..."
        )
        try:
            from infraforge.ipam_client import IPAMClient
            client = IPAMClient(self.app.config)
            subnets = client.get_subnets()
            self._subnets = subnets
            self._ipam_loaded = True
            self.app.call_from_thread(self._render_target_selection)
            self.app.call_from_thread(
                self._set_status,
                f"Loaded {len(subnets)} subnets from IPAM"
            )
        except Exception as e:
            self.app.call_from_thread(
                self._set_status, f"[red]IPAM error: {e}[/red]"
            )

    def _import_ipam_subnet(self, subnet_id_str: str) -> None:
        self._set_status(f"Loading addresses for subnet {subnet_id_str}...")
        self._load_ipam_addresses(subnet_id_str)

    @work(thread=True, exclusive=True, group="ansible-ipam")
    def _load_ipam_addresses(self, subnet_id: str) -> None:
        try:
            from infraforge.ipam_client import IPAMClient
            client = IPAMClient(self.app.config)
            addresses = client.get_subnet_addresses(subnet_id)
            ips = [a.get("ip", "") for a in addresses if a.get("ip")]
            if ips:
                self._resolved_ips = ips
                self._phase = 1
                self._scan_total = len(ips)
                self._scan_done = 0
                self._scan_alive = 0
                self._is_scanning = True
                self.app.call_from_thread(self._render_phase)
                self._run_ping_sweep_direct()
            else:
                self.app.call_from_thread(
                    self._set_status,
                    f"[red]No addresses found in subnet {subnet_id}[/red]"
                )
        except Exception as e:
            self.app.call_from_thread(
                self._set_status, f"[red]IPAM error: {e}[/red]"
            )

    def _run_ping_sweep_direct(self) -> None:
        """Run ping sweep directly (already in a worker thread)."""
        def on_result(ip: str, alive: bool) -> None:
            self._scan_done += 1
            if alive:
                self._scan_alive += 1
            self.app.call_from_thread(self._update_scan_progress)

        alive, dead = ping_sweep(
            self._resolved_ips, workers=50, callback=on_result,
        )
        self._alive_ips = alive
        self._dead_ips = dead
        self._is_scanning = False
        self.app.call_from_thread(self._show_scan_results)

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _start_run_timer(self) -> None:
        self._stop_run_timer()
        self._run_timer = self.set_interval(1.0, self._tick_run_timer)

    def _stop_run_timer(self) -> None:
        if self._run_timer:
            self._run_timer.stop()
            self._run_timer = None

    def _tick_run_timer(self) -> None:
        if not self._is_running:
            self._stop_run_timer()
            return
        elapsed = int(time.monotonic() - self._run_start)
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  "
            f"[bold yellow]Running... {elapsed}s[/bold yellow]  "
            f"[dim]Esc[/dim] abort"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_phase_content(self, text: str) -> None:
        content = self.query_one("#run-phase-content", Static)
        content.update(text)

    def _set_status(self, text: str) -> None:
        status = self.query_one("#run-status", Static)
        status.update(text)

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("[", "\\[")
