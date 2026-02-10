"""Ansible playbook discovery, host scanning, and execution for InfraForge.

All functions are synchronous and designed to be called from Textual
``@work(thread=True)`` workers.  No Textual imports here — pure logic.
"""

from __future__ import annotations

import glob as glob_mod
import ipaddress
import os
import pty
import select
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


def resolve_targets(
    text: str,
    dns_client=None,
    dns_zones: list[str] | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Parse targets that may include hostnames, IPs, and CIDR ranges.

    For each comma-separated token:
    1. Try ``parse_ip_ranges()`` first (handles IPs, CIDRs, ranges).
    2. If that yields nothing, treat the token as a hostname and attempt
       DNS resolution — first as-is (FQDN), then with each configured
       zone appended (e.g. ``dns-test`` → ``dns-test.easypl.net``).
    3. Falls back to system DNS (``socket.gethostbyname``) if no
       *dns_client* is available or the client can't resolve it.

    Returns:
        (ips, resolved, unresolved) where:
        - *ips*: deduplicated list of resolved IP addresses
        - *resolved*: mapping of hostname → IP for names that resolved
        - *unresolved*: list of hostnames that could not be resolved
    """
    import socket

    zones = dns_zones or []
    all_ips: list[str] = []
    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    parts = [p.strip() for p in text.split(",") if p.strip()]

    for part in parts:
        # Try IP parsing first
        ip_result = parse_ip_ranges(part)
        if ip_result:
            all_ips.extend(ip_result)
            continue

        # Not an IP/range — treat as hostname
        hostname = part.strip()
        ip = _resolve_hostname(hostname, dns_client, zones)

        if ip:
            resolved[hostname] = ip
            all_ips.append(ip)
        else:
            unresolved.append(hostname)

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for ip in all_ips:
        if ip not in seen:
            seen.add(ip)
            deduped.append(ip)

    return deduped, resolved, unresolved


def _resolve_hostname(
    hostname: str,
    dns_client=None,
    zones: list[str] | None = None,
) -> str:
    """Try to resolve a hostname to an IP address.

    Resolution order:
    1. Query dns_client for hostname as-is (may be FQDN already)
    2. Query dns_client for hostname + each zone suffix
    3. Fall back to system DNS (socket.gethostbyname) as-is
    4. Fall back to system DNS with each zone suffix
    """
    import socket

    zones = zones or []

    # --- Try via InfraForge DNS client (configured BIND9 server) ---
    if dns_client is not None:
        # Try as-is first (user may have entered a FQDN)
        try:
            results = dns_client.lookup_record(hostname, rtype="A")
            if results:
                return results[0]
        except Exception:
            pass

        # Try with each configured zone appended
        for zone in zones:
            fqdn = f"{hostname}.{zone}" if not hostname.endswith(f".{zone}") else hostname
            try:
                results = dns_client.lookup_record(fqdn, rtype="A")
                if results:
                    return results[0]
            except Exception:
                pass

    # --- Fall back to system DNS ---
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        pass

    # Try with zone suffixes via system DNS
    for zone in zones:
        fqdn = f"{hostname}.{zone}" if not hostname.endswith(f".{zone}") else hostname
        try:
            return socket.gethostbyname(fqdn)
        except socket.gaierror:
            pass

    return ""


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

def build_credential_args(
    profile: "CredentialProfile",
) -> tuple[list[str], dict[str, str]]:
    """Convert a :class:`CredentialProfile` into CLI args and env vars.

    Returns ``(args_list, env_dict)`` suitable for passing to
    :func:`run_playbook` as *credential_args* and *credential_env*.

    For password auth, credentials are passed via a temporary extra-vars
    file (``@path``) to keep them out of the process table.
    """
    from infraforge.credential_manager import CredentialProfile  # noqa: F811

    args: list[str] = []
    env: dict[str, str] = {}

    if profile.username:
        args.extend(["-u", profile.username])

    if profile.auth_type == "ssh_key":
        if profile.private_key_path:
            args.extend(["--private-key", profile.private_key_path])
    elif profile.auth_type == "password":
        if profile.password:
            # Write credentials to a temp file for --extra-vars @file
            # This avoids exposing passwords in the process table
            cred_vars: dict[str, str] = {
                "ansible_ssh_pass": profile.password,
            }
            become_pass = profile.become_pass or profile.password
            if profile.become:
                cred_vars["ansible_become_pass"] = become_pass
            cred_file = _write_credential_vars(cred_vars)
            args.extend(["--extra-vars", f"@{cred_file}"])

    if profile.become:
        args.append("--become")
        if profile.become_method:
            args.extend(["--become-method", profile.become_method])
        # If become_pass was set separately (not via password auth above)
        if profile.become_pass and profile.auth_type != "password":
            cred_vars = {"ansible_become_pass": profile.become_pass}
            cred_file = _write_credential_vars(cred_vars)
            args.extend(["--extra-vars", f"@{cred_file}"])

    return args, env


def _write_credential_vars(cred_vars: dict[str, str]) -> Path:
    """Write credential variables to a secure temp file.

    Returns the path.  Caller (run_playbook) should clean up after use.
    The file has ``0o600`` permissions.
    """
    fd, tmp = tempfile.mkstemp(suffix=".yml", prefix="infraforge_creds_")
    path = Path(tmp)
    os.close(fd)
    path.write_text(yaml.dump(cred_vars, default_flow_style=False))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def run_playbook(
    playbook_path: str | Path,
    inventory_path: str | Path,
    log_path: str | Path,
    extra_args: list[str] | None = None,
    credential_args: list[str] | None = None,
    credential_env: dict[str, str] | None = None,
    host_key_checking: bool = True,
) -> Generator[tuple[str, str], None, None]:
    """Execute ``ansible-playbook`` and yield output lines.

    Each yielded item is ``(line_text, stream_type)`` where stream_type
    is ``"stdout"`` for command output or ``"status"`` for InfraForge
    status messages.

    The output is simultaneously written to *log_path*.

    Parameters
    ----------
    host_key_checking:
        Whether to verify SSH host keys.  Defaults to ``True`` (secure).
        Set to ``False`` only for newly provisioned VMs whose keys are
        not yet known.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)]
    if credential_args:
        cmd.extend(credential_args)
    if extra_args:
        cmd.extend(extra_args)

    yield (f"$ {' '.join(cmd)}\n", "status")

    run_env = {
        **os.environ,
        "ANSIBLE_FORCE_COLOR": "false",
        "ANSIBLE_HOST_KEY_CHECKING": str(host_key_checking),
    }
    if credential_env:
        run_env.update(credential_env)

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
                env=run_env,
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


# ---------------------------------------------------------------------------
# Interactive playbook runner (PTY-based)
# ---------------------------------------------------------------------------

class PlaybookRunner:
    """Run ``ansible-playbook`` with a pseudo-terminal for interactive I/O.

    Unlike :func:`run_playbook` (a generator), this class gives the caller
    control over reading output and sending input — ideal for embedding an
    interactive console inside a TUI.
    """

    def __init__(
        self,
        playbook_path: str | Path,
        inventory_path: str | Path,
        log_path: str | Path,
        extra_args: list[str] | None = None,
        credential_args: list[str] | None = None,
        credential_env: dict[str, str] | None = None,
        host_key_checking: bool = True,
    ):
        self._playbook_path = Path(playbook_path)
        self._inventory_path = Path(inventory_path)
        self._log_path = Path(log_path)
        self._extra_args = extra_args or []
        self._credential_args = credential_args or []
        self._credential_env = credential_env or {}
        self._host_key_checking = host_key_checking
        self._process: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._log_file = None
        self._exit_code: int | None = None

    # -- state queries -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        rc = self._process.poll()
        if rc is not None:
            self._exit_code = rc
        return rc is None

    @property
    def exit_code(self) -> int | None:
        if self._process is not None and self._exit_code is None:
            rc = self._process.poll()
            if rc is not None:
                self._exit_code = rc
        return self._exit_code

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> str:
        """Start the subprocess with a PTY.  Returns the command string."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ansible-playbook",
            "-i", str(self._inventory_path),
            str(self._playbook_path),
        ]
        if self._credential_args:
            cmd.extend(self._credential_args)
        if self._extra_args:
            cmd.extend(self._extra_args)

        cmd_str = " ".join(cmd)

        # Create PTY so ansible (and SSH) see a real terminal
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        run_env = {
            **os.environ,
            "ANSIBLE_FORCE_COLOR": "false",
            "ANSIBLE_HOST_KEY_CHECKING": str(self._host_key_checking),
        }
        run_env.update(self._credential_env)

        self._log_file = open(self._log_path, "w")
        self._log_file.write(f"# InfraForge Ansible Run\n")
        self._log_file.write(f"# Command: {cmd_str}\n")
        self._log_file.write(f"# Started: {datetime.now().isoformat()}\n\n")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=run_env,
                close_fds=True,
            )
        except FileNotFoundError:
            os.close(slave_fd)
            os.close(master_fd)
            self._master_fd = None
            if self._log_file:
                self._log_file.write("ansible-playbook not found.\n")
                self._log_file.close()
                self._log_file = None
            raise FileNotFoundError(
                "ansible-playbook not found. Install Ansible first."
            )

        # Close slave in parent — child has its own copy
        os.close(slave_fd)
        return cmd_str

    def read_output(self, timeout: float = 0.1) -> str:
        """Read available output from the PTY master fd.

        Returns decoded text (may be empty if nothing ready).  Strips
        carriage-returns that the PTY inserts.
        """
        if self._master_fd is None:
            return ""

        try:
            ready, _, _ = select.select([self._master_fd], [], [], timeout)
            if not ready:
                return ""
            data = os.read(self._master_fd, 8192)
            if not data:
                return ""
            text = data.decode("utf-8", errors="replace").replace("\r", "")
            if self._log_file:
                self._log_file.write(text)
                self._log_file.flush()
            return text
        except (OSError, ValueError):
            return ""

    def send_input(self, text: str) -> None:
        """Write *text* to the subprocess via the PTY (e.g. ``"yes\\n"``)."""
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, text.encode("utf-8"))
            except OSError:
                pass

    def kill(self) -> None:
        """Kill the subprocess."""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait()
            except Exception:
                pass
            self._exit_code = self._process.returncode

    def cleanup(self) -> None:
        """Close PTY, log file, and remove temp credential files."""
        # Close master fd
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        # Finalize log
        if self._log_file is not None:
            ec = self._exit_code if self._exit_code is not None else "?"
            self._log_file.write(f"\n# Exit code: {ec}\n")
            self._log_file.close()
            self._log_file = None

        # Clean up temp credential files written by build_credential_args
        _cleanup_credential_files()


def _cleanup_credential_files() -> None:
    """Remove any ``infraforge_creds_*.yml`` temp files."""
    import tempfile as _tmp

    pattern = os.path.join(_tmp.gettempdir(), "infraforge_creds_*.yml")
    for f in glob_mod.glob(pattern):
        try:
            os.unlink(f)
        except OSError:
            pass
