"""Ansible playbook discovery, host scanning, and execution for InfraForge.

All functions are synchronous and designed to be called from Textual
``@work(thread=True)`` workers.  No Textual imports here — pure logic.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Generator

import yaml


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PlaybookInfo:
    """Metadata extracted from an Ansible playbook YAML file."""
    path: Path
    filename: str
    name: str
    hosts: str
    task_count: int
    description: str
    has_roles: bool
    last_run: str | None
    last_status: str          # "success" | "failed" | "never"


# ---------------------------------------------------------------------------
# Playbook discovery
# ---------------------------------------------------------------------------

def discover_playbooks(playbook_dir: str) -> list[PlaybookInfo]:
    """Scan *playbook_dir* for Ansible playbook YAML files.

    Returns a sorted list of ``PlaybookInfo`` objects.  Files that are not
    valid Ansible playbooks (i.e. not a YAML list of play dicts) are silently
    skipped.
    """
    root = Path(playbook_dir).expanduser().resolve()
    if not root.is_dir():
        return []

    results: list[PlaybookInfo] = []
    log_dir = root / "logs"

    for ext in ("*.yml", "*.yaml"):
        for path in root.glob(ext):
            info = _parse_playbook(path, log_dir)
            if info is not None:
                results.append(info)

    results.sort(key=lambda p: p.filename.lower())
    return results


def _parse_playbook(path: Path, log_dir: Path) -> PlaybookInfo | None:
    """Try to parse a single YAML file as an Ansible playbook."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception:
        return None

    # Ansible playbooks are YAML lists of play dicts
    if not isinstance(data, list):
        return None
    if not data or not isinstance(data[0], dict):
        return None

    first_play = data[0]
    name = first_play.get("name", path.stem)
    hosts = str(first_play.get("hosts", "all"))
    description = name

    # Count tasks across all plays
    task_count = 0
    has_roles = False
    for play in data:
        if not isinstance(play, dict):
            continue
        for key in ("tasks", "pre_tasks", "post_tasks", "handlers"):
            tasks = play.get(key, [])
            if isinstance(tasks, list):
                task_count += len(tasks)
        if play.get("roles"):
            has_roles = True

    # Check for log files
    last_run, last_status = _check_last_run(path.stem, log_dir)

    return PlaybookInfo(
        path=path,
        filename=path.name,
        name=name,
        hosts=hosts,
        task_count=task_count,
        description=description,
        has_roles=has_roles,
        last_run=last_run,
        last_status=last_status,
    )


def _check_last_run(stem: str, log_dir: Path) -> tuple[str | None, str]:
    """Check for existing log files and determine last run status."""
    if not log_dir.is_dir():
        return None, "never"

    log_files = sorted(log_dir.glob(f"{stem}_*.log"), reverse=True)
    if not log_files:
        return None, "never"

    latest = log_files[0]
    # Extract timestamp from filename: stem_YYYYMMDD_HHMMSS.log
    ts_part = latest.stem[len(stem) + 1:]  # skip "stem_"
    try:
        ts = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
        last_run = ts.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        last_run = latest.stem

    # Determine status from last line of the log
    try:
        text = latest.read_text()
        lines = text.strip().splitlines()
        # Look for Ansible play recap or exit code in last few lines
        tail = "\n".join(lines[-5:]).lower()
        if "exit code: 0" in tail:
            return last_run, "success"
        elif "exit code:" in tail:
            return last_run, "failed"
        # Check for play recap line
        if "failed=0" in tail and "unreachable=0" in tail:
            return last_run, "success"
        if "failed=" in tail or "unreachable=" in tail:
            return last_run, "failed"
    except Exception:
        pass

    return last_run, "failed"


# ---------------------------------------------------------------------------
# IP range parsing
# ---------------------------------------------------------------------------

def parse_ip_ranges(text: str) -> list[str]:
    """Parse comma-separated IP ranges into individual IP addresses.

    Supported formats:
    - CIDR: ``10.0.1.0/24``
    - Range: ``10.0.5.1-10.0.5.100`` or ``10.0.5.1-100``
    - Single: ``10.0.5.50``
    """
    ips: list[str] = []
    parts = [p.strip() for p in text.split(",") if p.strip()]

    for part in parts:
        if "/" in part:
            try:
                network = ipaddress.ip_network(part, strict=False)
                ips.extend(str(host) for host in network.hosts())
            except ValueError:
                continue
        elif "-" in part:
            try:
                start_str, end_str = part.rsplit("-", 1)
                start_str = start_str.strip()
                end_str = end_str.strip()
                # Short form: 10.0.5.1-100 means 10.0.5.1-10.0.5.100
                if "." not in end_str:
                    prefix = start_str.rsplit(".", 1)[0]
                    end_str = f"{prefix}.{end_str}"
                start = int(ipaddress.ip_address(start_str))
                end = int(ipaddress.ip_address(end_str))
                for addr_int in range(start, end + 1):
                    ips.append(str(ipaddress.ip_address(addr_int)))
            except (ValueError, TypeError):
                continue
        else:
            try:
                ips.append(str(ipaddress.ip_address(part)))
            except ValueError:
                continue

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


# ---------------------------------------------------------------------------
# Ping sweep
# ---------------------------------------------------------------------------

def ping_sweep(
    ips: list[str],
    workers: int = 50,
    callback: Callable[[str, bool], None] | None = None,
) -> tuple[list[str], list[str]]:
    """Ping a list of IPs in parallel and return (alive, dead) lists.

    Parameters
    ----------
    ips:
        IP addresses to ping.
    workers:
        Max concurrent ping processes.
    callback:
        Called with ``(ip, alive)`` for each result — use for progress
        updates from a Textual worker thread.
    """
    alive: list[str] = []
    dead: list[str] = []

    def _ping_one(ip: str) -> tuple[str, bool]:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return ip, result.returncode == 0
        except Exception:
            return ip, False

    with ThreadPoolExecutor(max_workers=min(workers, len(ips) or 1)) as pool:
        futures = {pool.submit(_ping_one, ip): ip for ip in ips}
        for future in as_completed(futures):
            ip, is_alive = future.result()
            if is_alive:
                alive.append(ip)
            else:
                dead.append(ip)
            if callback:
                callback(ip, is_alive)

    alive.sort(key=lambda x: ipaddress.ip_address(x))
    dead.sort(key=lambda x: ipaddress.ip_address(x))
    return alive, dead


# ---------------------------------------------------------------------------
# Inventory generation
# ---------------------------------------------------------------------------

def generate_inventory(hosts: list[str]) -> Path:
    """Write a temporary Ansible INI inventory file and return its path."""
    fd, tmp = tempfile.mkstemp(suffix=".ini", prefix="infraforge_inv_")
    path = Path(tmp)
    os.close(fd)

    lines = ["[targets]"]
    lines.extend(hosts)
    lines.append("")
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Playbook execution
# ---------------------------------------------------------------------------

def run_playbook(
    playbook_path: str | Path,
    inventory_path: str | Path,
    log_path: str | Path,
    extra_args: list[str] | None = None,
) -> Generator[tuple[str, str], None, None]:
    """Execute ``ansible-playbook`` and yield output lines.

    Each yielded item is ``(line_text, stream_type)`` where stream_type
    is ``"stdout"`` for command output or ``"status"`` for InfraForge
    status messages.

    The output is simultaneously written to *log_path*.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)]
    if extra_args:
        cmd.extend(extra_args)

    yield (f"$ {' '.join(cmd)}\n", "status")

    with open(log_path, "w") as log_file:
        log_file.write(f"# InfraForge Ansible Run\n")
        log_file.write(f"# Command: {' '.join(cmd)}\n")
        log_file.write(f"# Started: {datetime.now().isoformat()}\n\n")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "ANSIBLE_FORCE_COLOR": "false"},
            )
        except FileNotFoundError:
            msg = "ansible-playbook not found. Install Ansible first.\n"
            log_file.write(msg)
            yield (msg, "status")
            return

        try:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                yield (line, "stdout")

            proc.wait()
        except Exception as e:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
            msg = f"Error: {e}\n"
            log_file.write(msg)
            yield (msg, "status")
            log_file.write(f"\n# Exit code: 1\n")
            return

        exit_code = proc.returncode
        log_file.write(f"\n# Exit code: {exit_code}\n")

    color = "green" if exit_code == 0 else "red"
    yield (f"\nCompleted with exit code {exit_code}\n", "status")
    yield (f"Log saved to {log_path}\n", "status")
