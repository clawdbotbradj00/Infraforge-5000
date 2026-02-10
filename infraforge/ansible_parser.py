"""Real-time parser for ``ansible-playbook`` output.

Transforms raw stdout lines into structured per-host status updates
suitable for driving InfraForge's execution dashboard.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HostStatus:
    """Aggregate status of a single host during a playbook run."""

    ip: str
    ok: int = 0
    changed: int = 0
    failed: int = 0
    skipped: int = 0
    unreachable: int = 0
    rescued: int = 0
    ignored: int = 0
    current_state: str = "waiting"  # waiting | running | ok | changed | failed | unreachable | skipped | done
    error_msg: str = ""

    @property
    def tasks_completed(self) -> int:
        return self.ok + self.changed + self.failed + self.skipped

    @property
    def summary_state(self) -> str:
        """Overall state for display — worst status wins."""
        if self.unreachable > 0:
            return "unreachable"
        if self.failed > 0:
            return "failed"
        if self.changed > 0:
            return "changed"
        if self.ok > 0:
            return "ok"
        return "waiting"


@dataclass
class PlaybookProgress:
    """Live parsing state for a running ``ansible-playbook`` process."""

    hosts: dict[str, HostStatus] = field(default_factory=dict)
    current_play: str = ""
    current_task: str = ""
    task_index: int = 0
    in_recap: bool = False
    finished: bool = False
    warnings: list[str] = field(default_factory=list)

    # -- Compiled patterns (class-level) ------------------------------------

    _RE_PLAY = re.compile(r"^PLAY \[(.+?)\]")
    _RE_TASK = re.compile(r"^TASK \[(.+?)\]")
    _RE_OK = re.compile(r"^\s*ok: \[(.+?)\]")
    _RE_CHANGED = re.compile(r"^\s*changed: \[(.+?)\]")
    _RE_FATAL = re.compile(
        r"^\s*fatal: \[(.+?)\]:\s+(FAILED|UNREACHABLE)!\s*=>\s*(.*)"
    )
    _RE_SKIP = re.compile(r"^\s*skipping: \[(.+?)\]")
    _RE_RESCUED = re.compile(r"^\s*rescued: \[(.+?)\]")
    _RE_IGNORED = re.compile(r"^\s*(?:\.\.\.ignoring|ignoring:)\s*(?:\[(.+?)\])?")
    _RE_RECAP = re.compile(r"^PLAY RECAP\b")
    _RE_RECAP_LINE = re.compile(
        r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+"
        r"failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
    )
    _RE_WARNING = re.compile(r"^\[WARNING\]:\s*(.+)")

    # -- Public API ---------------------------------------------------------

    def feed_line(self, line: str) -> bool:
        """Parse one output line.  Returns ``True`` if the UI should refresh."""
        line = line.rstrip()
        if not line:
            return False

        # Play header
        m = self._RE_PLAY.match(line)
        if m:
            self.current_play = m.group(1)
            self.in_recap = False
            return True

        # Task header
        m = self._RE_TASK.match(line)
        if m:
            self.current_task = m.group(1)
            self.task_index += 1
            # Mark non-terminal hosts as "running"
            for h in self.hosts.values():
                if h.current_state not in ("unreachable",):
                    h.current_state = "running"
            return True

        # ok
        m = self._RE_OK.match(line)
        if m:
            host = self._get_host(m.group(1))
            if host:
                host.ok += 1
                host.current_state = "ok"
            return True

        # changed
        m = self._RE_CHANGED.match(line)
        if m:
            host = self._get_host(m.group(1))
            if host:
                host.changed += 1
                host.current_state = "changed"
            return True

        # fatal (FAILED or UNREACHABLE)
        m = self._RE_FATAL.match(line)
        if m:
            host = self._get_host(m.group(1))
            if host:
                fail_type = m.group(2)
                error_json = m.group(3).strip()
                if fail_type == "UNREACHABLE":
                    host.unreachable += 1
                    host.current_state = "unreachable"
                else:
                    host.failed += 1
                    host.current_state = "failed"
                host.error_msg = _extract_error_msg(error_json)
            return True

        # skipping
        m = self._RE_SKIP.match(line)
        if m:
            host = self._get_host(m.group(1))
            if host:
                host.skipped += 1
                host.current_state = "skipped"
            return True

        # rescued
        m = self._RE_RESCUED.match(line)
        if m:
            host = self._get_host(m.group(1))
            if host:
                host.rescued += 1
                host.current_state = "ok"
            return True

        # ...ignoring (after failed task with ignore_errors: true)
        m = self._RE_IGNORED.match(line)
        if m:
            # Some formats include the host, some don't
            hostname = m.group(1) if m.group(1) else None
            if hostname:
                host = self._get_host(hostname)
                if host:
                    host.ignored += 1
                    if host.failed > 0:
                        host.failed -= 1
                    host.current_state = "ok"
            return True

        # PLAY RECAP
        if self._RE_RECAP.match(line):
            self.in_recap = True
            self.current_task = ""
            return True

        # Recap line (final per-host stats)
        m = self._RE_RECAP_LINE.match(line)
        if m and self.in_recap:
            host = self._get_host(m.group(1))
            if host:
                host.ok = int(m.group(2))
                host.changed = int(m.group(3))
                host.unreachable = int(m.group(4))
                host.failed = int(m.group(5))
                host.skipped = int(m.group(6))
                host.rescued = int(m.group(7))
                host.ignored = int(m.group(8))
                host.current_state = "done"
            self.finished = True
            return True

        # Warning (accumulate but don't trigger full refresh)
        m = self._RE_WARNING.match(line)
        if m:
            self.warnings.append(m.group(1))
            return False

        return False

    # -- Internal helpers ---------------------------------------------------

    def _get_host(self, name: str) -> HostStatus | None:
        """Look up a host by IP or name (exact match only)."""
        return self.hosts.get(name.strip())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_error_msg(json_str: str) -> str:
    """Try to pull a human-readable ``msg`` from ansible error output."""
    # Try JSON parse first
    try:
        data = json.loads(json_str)
        msg = data.get("msg", "")
        if isinstance(msg, str) and msg:
            return msg[:80] + "..." if len(msg) > 80 else msg
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    # Fallback: regex for "msg": "..."
    m = re.search(r'"msg":\s*"([^"]+)"', json_str)
    if m:
        msg = m.group(1)
        return msg[:80] + "..." if len(msg) > 80 else msg

    return ""
