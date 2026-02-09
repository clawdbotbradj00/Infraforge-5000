"""Ansible Run Modal — target selection, ping sweep, credential selection, and live execution.

A full-screen modal overlay with four phases:

Phase 0 — Target Selection
    Enter IP ranges manually or import from IPAM subnets.

Phase 1 — Ping Sweep
    Validate which hosts are alive before running.

Phase 2 — Credential Selection
    Pick or create a credential profile for authentication.

Phase 3 — Execution
    Stream ``ansible-playbook`` output live with elapsed timer.
"""

from __future__ import annotations

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

from infraforge.ansible_parser import PlaybookProgress, HostStatus as ExecHostStatus
from infraforge.ansible_runner import (
    PlaybookInfo,
    PlaybookRunner,
    build_credential_args,
    generate_inventory,
    parse_ip_ranges,
    ping_sweep,
    resolve_targets,
)
from infraforge.credential_manager import CredentialManager, CredentialProfile
from infraforge.host_enrichment import HostInfo, enrich_hosts, check_nmap_available

if TYPE_CHECKING:
    pass


def _state_display(state: str) -> tuple[str, str]:
    """Return ``(icon, color)`` for an execution host state."""
    return {
        "waiting":      ("[dim]\u00b7[/dim]", "dim"),
        "running":      ("[cyan]\u22ef[/cyan]", "cyan"),
        "ok":           ("[green]\u2713[/green]", "green"),
        "changed":      ("[yellow]\u2713[/yellow]", "yellow"),
        "failed":       ("[red bold]\u2717[/red bold]", "red bold"),
        "unreachable":  ("[red]\u2717[/red]", "red"),
        "skipped":      ("[dim]-[/dim]", "dim"),
        "done":         ("[green]\u2713[/green]", "green"),
    }.get(state, ("[dim]?[/dim]", "dim"))


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

    .exec-host-line {
        height: auto;
        min-height: 1;
        max-height: 2;
        padding: 0 1;
    }

    #exec-raw {
        margin-top: 1;
        padding: 0 1;
        max-height: 6;
        color: $text-muted;
    }

    #run-console-input {
        dock: bottom;
        margin: 0;
        border-top: solid $accent;
    }

    #run-console-input:focus {
        border-top: solid $success;
    }
    """

    def __init__(
        self,
        playbook: PlaybookInfo,
        target_ips: list[str] | None = None,
        credential: CredentialProfile | None = None,
        extra_vars: dict | None = None,
    ) -> None:
        super().__init__()
        self._playbook = playbook
        self._phase: int = 0
        # Phase 0/1 — target + scan
        self._resolved_ips: list[str] = []
        self._alive_ips: list[str] = []
        self._dead_ips: list[str] = []
        self._scan_total: int = 0
        self._scan_done: int = 0
        self._scan_alive: int = 0
        self._is_scanning: bool = False
        self._host_included: dict[str, bool] = {}  # IP -> included toggle
        self._host_cursor: int = 0                    # keyboard cursor index
        self._host_info: dict[str, HostInfo] = {}     # IP -> enrichment data
        self._enriching: bool = False
        self._subnet_cursor: int = -1                  # -1 = on the manual input
        # Phase 2 — credentials
        self._credential_mgr = CredentialManager()
        self._credential_profiles: list[CredentialProfile] = []
        self._selected_credential: CredentialProfile | None = None
        self._show_new_credential_form: bool = False
        self._new_cred_auth_type: str = "password"
        self._generated_pubkey: str = ""
        self._cred_cursor: int = 0  # cursor in the credential list
        self._detected_keys: list[tuple[str, str]] = []  # (path, label) pairs
        self._detected_key_cursor: int = -1  # cursor in detected key list
        self._form_cursor: int = 0           # cursor in new-credential form items
        self._form_items: list[str] = []     # ordered IDs of navigable form items
        # Phase 3 — execution
        self._is_running: bool = False
        self._run_start: float = 0.0
        self._run_timer: Timer | None = None
        self._exit_code: int | None = None
        self._log_path: Path | None = None
        self._runner: PlaybookRunner | None = None
        self._aborted: bool = False
        self._progress: PlaybookProgress | None = None
        self._raw_lines: list[str] = []       # last N raw output lines
        self._task_estimate: int = 0          # from PlaybookInfo.task_count
        # IPAM
        self._subnets: list[dict] = []
        self._ipam_loaded: bool = False
        self._skipped_scan: bool = False  # True if user used Direct Hosts
        # Extra vars for ansible-playbook --extra-vars
        self._extra_vars = extra_vars or {}

        # Pre-populate targets if provided (skip Phase 0/1)
        if target_ips is not None:
            self._resolved_ips = target_ips
            self._alive_ips = target_ips
            self._host_included = {ip: True for ip in target_ips}
            self._host_info = {ip: HostInfo(ip=ip) for ip in target_ips}
            self._skipped_scan = True
            self._phase = 2  # skip to credentials

        # Pre-populate credential if provided
        if credential is not None:
            self._selected_credential = credential
            self._credential_profiles = self._credential_mgr.load_profiles()

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

    def on_key(self, event) -> None:
        """Handle arrow keys for subnet selection (Phase 0), host toggles (Phase 1), and credential navigation (Phase 2)."""

        # ------------------------------------------------------------------
        # Phase 0: subnet list navigation
        # ------------------------------------------------------------------
        if self._phase == 0 and self._subnets:
            focused = self.focused
            # If ANY Input is focused, let it handle Enter — only intercept arrows
            if isinstance(focused, Input):
                if event.key in ("up", "down"):
                    pass  # fall through to arrow handling below
                else:
                    return
            if event.key == "up":
                event.prevent_default()
                event.stop()
                if self._subnet_cursor > 0:
                    self._subnet_cursor -= 1
                    self._refresh_subnet_lines()
                    self._scroll_to_subnet_cursor()
                    try:
                        self.query_one("#run-ip-input", Input).blur()
                    except Exception:
                        pass
            elif event.key == "down":
                event.prevent_default()
                event.stop()
                if self._subnet_cursor < len(self._subnets) - 1:
                    self._subnet_cursor += 1
                    self._refresh_subnet_lines()
                    self._scroll_to_subnet_cursor()
                    try:
                        self.query_one("#run-ip-input", Input).blur()
                    except Exception:
                        pass
                elif self._subnet_cursor == len(self._subnets) - 1:
                    self._subnet_cursor = -1
                    self._refresh_subnet_lines()
                    try:
                        self.query_one("#run-ip-input", Input).focus()
                    except Exception:
                        pass
            elif event.key == "enter" and self._subnet_cursor >= 0:
                event.prevent_default()
                event.stop()
                s = self._subnets[self._subnet_cursor]
                cidr = f"{s.get('subnet', '')}/{s.get('mask', '24')}"
                self._start_scan(cidr)
            return

        # ------------------------------------------------------------------
        # Phase 1: host toggle navigation
        # ------------------------------------------------------------------
        if self._phase == 1 and not self._is_scanning and self._alive_ips:
            focused = self.focused
            if isinstance(focused, (Button, Input)):
                return

            if event.key == "up":
                event.prevent_default()
                event.stop()
                if self._host_cursor > 0:
                    self._host_cursor -= 1
                    self._refresh_host_lines()
                    self._scroll_to_host_cursor()
            elif event.key == "down":
                event.prevent_default()
                event.stop()
                if self._host_cursor < len(self._alive_ips) - 1:
                    self._host_cursor += 1
                    self._refresh_host_lines()
                    self._scroll_to_host_cursor()
            elif event.key == "space":
                event.prevent_default()
                event.stop()
                self._toggle_host(self._host_cursor)
            elif event.key == "enter":
                event.prevent_default()
                event.stop()
                included = self._get_included_ips()
                if included:
                    self._transition_to_credentials()
            return

        # ------------------------------------------------------------------
        # Phase 2: credential FORM navigation (new credential form is open)
        # ------------------------------------------------------------------
        if self._phase == 2 and self._show_new_credential_form:
            focused = self.focused
            if isinstance(focused, Input):
                return  # Let Input handle all keys

            if not self._form_items:
                return

            if event.key == "up":
                event.prevent_default()
                event.stop()
                if self._form_cursor > 0:
                    self._form_cursor -= 1
                    self._refresh_form_items()
            elif event.key == "down":
                event.prevent_default()
                event.stop()
                if self._form_cursor < len(self._form_items) - 1:
                    self._form_cursor += 1
                    self._refresh_form_items()
            elif event.key == "enter":
                event.prevent_default()
                event.stop()
                self._handle_form_enter()
            elif event.key == "g":
                if self._new_cred_auth_type == "ssh_key":
                    event.prevent_default()
                    event.stop()
                    self._generate_ssh_key()
            elif event.key == "escape":
                event.prevent_default()
                event.stop()
                self._show_new_credential_form = False
                self._generated_pubkey = ""
                self._render_credential_selection()
            return

        # ------------------------------------------------------------------
        # Phase 2: credential LIST navigation (browsing saved profiles)
        # ------------------------------------------------------------------
        if self._phase == 2 and not self._show_new_credential_form:
            focused = self.focused
            if isinstance(focused, (Button, Input)):
                return

            total_items = len(self._credential_profiles) + 1  # profiles + "+ Create New Profile"
            if event.key == "up":
                event.prevent_default()
                event.stop()
                if self._cred_cursor > 0:
                    self._cred_cursor -= 1
                    self._refresh_cred_lines()
                    self._scroll_to_cred_cursor()
            elif event.key == "down":
                event.prevent_default()
                event.stop()
                if self._cred_cursor < total_items - 1:
                    self._cred_cursor += 1
                    self._refresh_cred_lines()
                    self._scroll_to_cred_cursor()
            elif event.key == "enter":
                event.prevent_default()
                event.stop()
                if self._cred_cursor < len(self._credential_profiles):
                    self._selected_credential = self._credential_profiles[self._cred_cursor]
                    self._refresh_cred_lines()
                    self._start_execution()
                else:
                    self._show_new_credential_form = True
                    self._generated_pubkey = ""
                    self._form_cursor = 0
                    self._form_items = self._build_form_items()
                    self._render_credential_selection()
            elif event.key == "space":
                event.prevent_default()
                event.stop()
                if self._cred_cursor < len(self._credential_profiles):
                    prof = self._credential_profiles[self._cred_cursor]
                    if self._selected_credential and self._selected_credential.name == prof.name:
                        self._selected_credential = None
                    else:
                        self._selected_credential = prof
                    self._refresh_cred_lines()
                    self._update_cred_status()
            elif event.key in ("d", "delete"):
                event.prevent_default()
                event.stop()
                if self._cred_cursor < len(self._credential_profiles):
                    self._delete_credential_at_cursor()
            return

    def _handle_form_enter(self) -> None:
        """Handle Enter press on the currently highlighted form item."""
        if not self._form_items or self._form_cursor >= len(self._form_items):
            return

        item_id = self._form_items[self._form_cursor]

        if item_id == "cred-form-auth-pw":
            if self._new_cred_auth_type != "password":
                self._new_cred_auth_type = "password"
                self._form_items = self._build_form_items()
                self._form_cursor = 0
                self._apply_cred_field_visibility()
                self._refresh_form_items()
                self._update_cred_status()

        elif item_id == "cred-form-auth-key":
            if self._new_cred_auth_type != "ssh_key":
                self._new_cred_auth_type = "ssh_key"
                if not self._detected_keys:
                    self._detected_keys = self._detect_ssh_keys()
                self._detected_key_cursor = 0 if self._detected_keys else -1
                self._form_items = self._build_form_items()
                self._form_cursor = 1
                self._apply_cred_field_visibility()
                self._refresh_form_items()
                self._update_cred_status()

        elif item_id.startswith("cred-form-key-"):
            try:
                idx = int(item_id.split("-")[-1])
                self._select_detected_key(idx)
                self._detected_key_cursor = idx
                self._refresh_form_items()
            except (ValueError, IndexError):
                pass

        elif item_id == "cred-form-genkey":
            self._generate_ssh_key()

        elif item_id == "cred-form-save":
            self._save_new_credential()

        elif item_id == "cred-form-cancel":
            self._show_new_credential_form = False
            self._generated_pubkey = ""
            self._render_credential_selection()

    def on_click(self, event) -> None:
        """Handle mouse clicks on host lines, subnet lines, credential lines, and form items."""
        widget = event.widget
        if not widget or not hasattr(widget, "id") or not widget.id:
            return

        # --- Phase 1: host line clicks ---
        if widget.id.startswith("host-line-"):
            try:
                idx = int(widget.id.split("-")[-1])
                if 0 <= idx < len(self._alive_ips):
                    self._host_cursor = idx
                    self._toggle_host(idx)
            except (ValueError, IndexError):
                pass

        # --- Phase 0: subnet line clicks ---
        elif widget.id.startswith("subnet-line-"):
            try:
                idx = int(widget.id.split("-")[-1])
                if 0 <= idx < len(self._subnets):
                    s = self._subnets[idx]
                    cidr = f"{s.get('subnet', '')}/{s.get('mask', '24')}"
                    self._start_scan(cidr)
            except (ValueError, IndexError):
                pass

        # --- Phase 2 list view: credential line clicks ---
        elif widget.id.startswith("cred-line-"):
            try:
                idx = int(widget.id.split("-")[-1])
                total_items = len(self._credential_profiles) + 1
                if 0 <= idx < total_items:
                    self._cred_cursor = idx
                    if idx < len(self._credential_profiles):
                        prof = self._credential_profiles[idx]
                        if self._selected_credential and self._selected_credential.name == prof.name:
                            self._selected_credential = None
                        else:
                            self._selected_credential = prof
                    else:
                        # Clicked "+ Create New Profile"
                        self._show_new_credential_form = True
                        self._generated_pubkey = ""
                        self._form_cursor = 0
                        self._form_items = self._build_form_items()
                        self._render_credential_selection()
                        return
                    self._refresh_cred_lines()
                    self._update_cred_status()
            except (ValueError, IndexError):
                pass

        # --- Phase 2 form view: form item clicks ---
        elif widget.id and widget.id.startswith("cred-form-"):
            if hasattr(self, "_form_items") and self._form_items:
                if widget.id in self._form_items:
                    self._form_cursor = self._form_items.index(widget.id)
                    self._refresh_form_items()
                    self._handle_form_enter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter pressed in an Input — handle per-phase."""
        if self._phase == 0 and event.input.id == "run-ip-input":
            self._start_scan()
        elif self._phase == 0 and event.input.id == "run-direct-input":
            self._use_direct_hosts()
        elif self._phase == 3 and event.input.id == "run-console-input":
            # Send user input to the running subprocess via PTY
            if self._runner and self._runner.is_running:
                text = event.input.value
                self._runner.send_input(text + "\n")
                # Echo what the user typed in the raw area
                self._raw_lines.append(f"> {text}")
                if len(self._raw_lines) > 5:
                    self._raw_lines = self._raw_lines[-5:]
                self._update_raw_output()
                event.input.value = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses — only bottom action bar buttons remain."""
        btn_id = event.button.id
        if not btn_id:
            return

        # --- Bottom action bar ---
        if btn_id == "run-cancel-btn":
            if self._is_running:
                self._abort_execution()
            elif self._phase == 2 and self._show_new_credential_form:
                self._show_new_credential_form = False
                self._generated_pubkey = ""
                self._render_credential_selection()
            elif self._phase == 2 and not self._show_new_credential_form:
                if self._skipped_scan:
                    self._phase = 0
                    self._skipped_scan = False
                else:
                    self._phase = 1
                    self._is_scanning = False
                self._render_phase()
            elif self._phase == 1 and not self._is_scanning:
                self._phase = 0
                self._render_phase()
            else:
                self.app.pop_screen()

        elif btn_id == "run-action-btn":
            if self._phase == 0:
                self._start_scan()
            elif self._phase == 1 and not self._is_scanning:
                self._transition_to_credentials()
            elif self._phase == 2:
                self._start_execution()
            elif self._phase == 3 and not self._is_running:
                self.app.pop_screen()


    # ------------------------------------------------------------------
    # Phase rendering
    # ------------------------------------------------------------------

    def _render_phase(self) -> None:
        if self._phase == 0:
            self._render_target_selection()
        elif self._phase == 1:
            self._render_ping_sweep()
        elif self._phase == 2:
            self._render_credential_selection()
        elif self._phase == 3:
            self._render_execution()

    def _render_target_selection(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 1/4: Target Selection"
        )

        # Clean up any widgets from other phases
        self._remove_cred_widgets()
        self._remove_cred_lines()
        self._remove_host_toggles()

        content = self.query_one("#run-phase-content", Static)
        scroll = self.query_one("#run-content", VerticalScroll)

        ipam_cfg = getattr(self.app.config, "ipam", None)
        has_ipam = ipam_cfg and getattr(ipam_cfg, "url", "")

        # -- Section 1: Scan subnet --
        lines = [
            "[bold]SCAN SUBNET[/bold]  [dim]— select a subnet or enter a range to ping sweep[/dim]",
            "",
        ]

        if has_ipam and not self._ipam_loaded and not self._subnets:
            lines.append("[dim]Loading IPAM subnets...[/dim]")
        elif self._subnets:
            lines.append(
                "[dim]arrow keys to select, Enter to scan[/dim]"
            )
            lines.append("")

        content.update("\n".join(lines))

        # -- Subnet lines: refresh existing or mount new --
        self._remove_subnet_lines()
        if self._subnets:
            if self._subnet_cursor == -1:
                self._subnet_cursor = 0
            # Mount before the scan input if it exists, else at end
            before_widget = None
            try:
                before_widget = self.query_one("#run-ip-input")
            except Exception:
                pass
            for idx, s in enumerate(self._subnets):
                label = self._format_subnet_line(idx, s)
                w = Static(
                    label,
                    markup=True,
                    id=f"subnet-line-{idx}",
                    classes="subnet-line",
                )
                if before_widget:
                    scroll.mount(w, before=before_widget)
                else:
                    scroll.mount(w)

        # -- Scan range input: reuse if already mounted --
        try:
            self.query_one("#run-ip-input", Input)
        except Exception:
            scroll.mount(
                Input(
                    placeholder="IP, range, or hostname: 10.0.1.0/24, dns-test, web01",
                    id="run-ip-input",
                )
            )

        # -- Separator + Direct hosts section: reuse if already mounted --
        try:
            self.query_one("#run-direct-separator", Static)
        except Exception:
            scroll.mount(
                Static(
                    "\n [dim]─────────────────────────────────────────────────────[/dim]\n\n"
                    " [bold]DIRECT HOSTS[/bold]  [dim]— enter hosts directly, skip the scan[/dim]\n"
                    " [dim]Comma-separated IPs, ranges, or hostnames[/dim]",
                    markup=True,
                    id="run-direct-separator",
                    classes="target-section-widget",
                )
            )

        try:
            self.query_one("#run-direct-input", Input)
        except Exception:
            scroll.mount(
                Input(
                    placeholder="e.g. 10.0.3.22, dns-test, web01.easypl.net",
                    id="run-direct-input",
                )
            )

        # Focus the appropriate input
        if self._subnet_cursor == -1 or not self._subnets:
            try:
                self.query_one("#run-ip-input", Input).focus()
            except Exception:
                pass

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Scan Hosts"
        action_btn.variant = "primary"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Cancel"

        status = self.query_one("#run-status", Static)
        if self._subnets:
            status.update(
                "[dim]Select subnet + Enter to scan  |  "
                "Or type hosts in Direct Hosts + Enter to skip scan[/dim]"
            )
        else:
            status.update(
                "[dim]Enter a range + Enter to scan  |  "
                "Or type hosts in Direct Hosts + Enter to skip scan[/dim]"
            )

        # Auto-load IPAM subnets in background if not loaded yet
        if has_ipam and not self._ipam_loaded:
            self._load_ipam_subnets()

    def _remove_target_widgets(self) -> None:
        """Remove all Phase 0 target-selection widgets from the DOM."""
        for sel in (
            "#run-ip-input", "#run-direct-input", "#run-direct-separator",
            "#run-ipam-btn",
        ):
            for w in self.query(sel):
                w.remove()
        for w in self.query(".target-section-widget"):
            w.remove()
        self._remove_subnet_lines()

    def _render_ping_sweep(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 2/4: Host Validation"
        )

        # Remove widgets from other phases
        self._remove_target_widgets()
        self._remove_cred_widgets()
        self._remove_cred_lines()
        self._remove_host_toggles()

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        if self._is_scanning:
            action_btn.label = "Scanning..."
            action_btn.disabled = True
            cancel_btn.label = "Cancel"
        else:
            # Re-show scan results with host toggles (e.g. when returning from Phase 2)
            if self._alive_ips:
                self._show_scan_results_with_toggles()
            else:
                action_btn.label = "Next"
                action_btn.variant = "primary"
                action_btn.disabled = True
                cancel_btn.label = "Back"

    def _render_credential_selection(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 3/4: Credentials"
        )

        # Remove widgets from other phases
        self._remove_target_widgets()
        self._remove_host_toggles()

        # Remove all credential widgets for clean re-render
        self._remove_cred_widgets()
        self._remove_cred_lines()

        scroll = self.query_one("#run-content", VerticalScroll)
        content = self.query_one("#run-phase-content", Static)

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        if not self._show_new_credential_form:
            # ── LIST VIEW ──────────────────────────────────────────────
            host_count = len(self._get_included_ips())
            lines = [
                "[bold]SAVED PROFILES[/bold]  "
                "[dim]up/down navigate, Space select, Enter run, d delete[/dim]",
                "",
                f"[dim]{host_count} hosts will be targeted.[/dim]",
                "",
            ]

            if self._credential_profiles:
                lines.append(
                    f"[dim]      {'Name':<20}  {'User':<12}  {'Auth':<6}Detail[/dim]"
                )
                lines.append("")
            else:
                lines.append("[yellow]No saved credential profiles.[/yellow]")
                lines.append("[dim]Navigate to '+ Create New Profile' and press Enter.[/dim]")
                lines.append("")

            content.update("\n".join(lines))

            # Mount credential profile lines as cursor-navigable Static widgets
            for idx, prof in enumerate(self._credential_profiles):
                label = self._format_cred_line(idx, prof)
                line = Static(
                    label,
                    markup=True,
                    id=f"cred-line-{idx}",
                    classes="cred-line",
                )
                scroll.mount(line)

            # Separator before the "create new" line
            scroll.mount(
                Static(
                    "\n [dim]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/dim]",
                    markup=True,
                    id="cred-line-separator",
                    classes="cred-line",
                )
            )

            # "+ Create New Profile" as a cursor-navigable Static line
            new_line_idx = len(self._credential_profiles)
            new_label = self._format_new_cred_line()
            new_line = Static(
                new_label,
                markup=True,
                id=f"cred-line-{new_line_idx}",
                classes="cred-line",
            )
            scroll.mount(new_line)

            # Bottom bar: Run Playbook enabled only if profile selected
            action_btn.label = "Run Playbook"
            action_btn.variant = "success"
            action_btn.disabled = self._selected_credential is None

            cancel_btn.label = "Back"

        else:
            # ── FORM VIEW ──────────────────────────────────────────────
            content.update("")
            self._mount_new_credential_form(scroll)

            action_btn.label = "Run Playbook"
            action_btn.variant = "success"
            action_btn.disabled = True

            cancel_btn.label = "Cancel"

        self._update_cred_status()

    def _format_cred_line(self, idx: int, profile: CredentialProfile) -> str:
        """Build the markup for a single credential profile line."""
        is_cursor = idx == self._cred_cursor
        is_selected = (
            self._selected_credential is not None
            and self._selected_credential.name == profile.name
        )
        cursor = ">" if is_cursor else " "
        check = "[green]\\[*][/green]" if is_selected else "[ ]"
        auth = "key" if profile.auth_type == "ssh_key" else "pw"
        name_padded = profile.name.ljust(20)
        user_padded = profile.username.ljust(12)
        auth_padded = auth.ljust(6)

        # For SSH key profiles, show the private_key_path (truncated if long)
        detail = ""
        if profile.auth_type == "ssh_key" and getattr(profile, "private_key_path", ""):
            kp = profile.private_key_path
            try:
                home = str(Path.home())
                if kp.startswith(home):
                    kp = "~" + kp[len(home):]
            except Exception:
                pass
            if len(kp) > 40:
                kp = "..." + kp[-37:]
            detail = kp

        if is_cursor:
            return (
                f" {cursor}  {check}  [bold]{name_padded}[/bold]  "
                f"[dim]{user_padded}  {auth_padded}[/dim]"
                f"[dim]{detail}[/dim]"
            )
        else:
            return (
                f" {cursor}  {check}  {name_padded}  "
                f"[dim]{user_padded}  {auth_padded}[/dim]"
                f"[dim]{detail}[/dim]"
            )

    def _format_new_cred_line(self) -> str:
        """Build the markup for the '+ New Profile' line."""
        is_cursor = self._cred_cursor == len(self._credential_profiles)
        cursor = ">" if is_cursor else " "
        if is_cursor:
            return f" {cursor}  [bold yellow]+ New Profile[/bold yellow]"
        else:
            return f" {cursor}  [dim]+ New Profile[/dim]"

    def _refresh_cred_lines(self) -> None:
        """Refresh all credential line labels (for cursor movement)."""
        for idx, prof in enumerate(self._credential_profiles):
            try:
                line = self.query_one(f"#cred-line-{idx}", Static)
                line.update(self._format_cred_line(idx, prof))
            except Exception:
                pass
        # Refresh the "+ New Profile" line
        new_idx = len(self._credential_profiles)
        try:
            line = self.query_one(f"#cred-line-{new_idx}", Static)
            line.update(self._format_new_cred_line())
        except Exception:
            pass

    def _scroll_to_cred_cursor(self) -> None:
        """Scroll the credential list so the cursor line is visible."""
        try:
            line = self.query_one(f"#cred-line-{self._cred_cursor}", Static)
            line.scroll_visible()
        except Exception:
            pass

    def _update_cred_status(self) -> None:
        """Update status bar and action button for credential phase."""
        action_btn = self.query_one("#run-action-btn", Button)

        status = self.query_one("#run-status", Static)
        if self._show_new_credential_form:
            auth_label = "SSH Key" if self._new_cred_auth_type == "ssh_key" else "Password"
            status.update(
                f"[dim]Auth: {auth_label} \u2014 "
                f"Tab to fields, up/down for items, Enter to activate[/dim]"
            )
            action_btn.disabled = True
        elif self._selected_credential:
            action_btn.disabled = False
            status.update(
                f"[dim]Selected: {self._selected_credential.name} "
                f"({self._selected_credential.username}) \u2014 "
                f"Enter to run, Space to deselect[/dim]"
            )
        else:
            action_btn.disabled = True
            status.update("[dim]Navigate with arrows, Space to select, Enter on profile to run[/dim]")

    def _apply_cred_field_visibility(self) -> None:
        """Show/hide password vs SSH-key widgets based on ``_new_cred_auth_type``."""
        is_pw = self._new_cred_auth_type == "password"
        for w in self.query(".cred-pw-field"):
            w.display = is_pw
        for w in self.query(".cred-key-field"):
            w.display = not is_pw

    def _toggle_cred_auth_type(self) -> None:
        """Toggle the credential form between password and SSH key modes.

        Rebuilds the form items list, updates navigable Static labels,
        and shows/hides the appropriate input fields.
        """
        self._form_items = self._build_form_items()

        if self._form_cursor >= len(self._form_items):
            self._form_cursor = max(0, len(self._form_items) - 1)

        self._apply_cred_field_visibility()
        self._refresh_form_items()
        self._update_cred_status()

    def _remove_cred_lines(self) -> None:
        """Remove all credential line widgets from the DOM."""
        for w in self.query(".cred-line"):
            w.remove()

    def _mount_new_credential_form(self, scroll: VerticalScroll) -> None:
        """Mount inline form widgets for creating a new credential.

        Uses ONLY Static and Input widgets — no Button widgets.
        All selectable/navigable items are Static with class ``cred-form-item``.
        """
        # Detect SSH keys eagerly so we know how many form items to build
        if not self._detected_keys:
            self._detected_keys = self._detect_ssh_keys()
            if self._detected_keys and self._detected_key_cursor == -1:
                self._detected_key_cursor = 0

        # Header
        scroll.mount(
            Static(
                "\n[bold]CREATE NEW PROFILE[/bold]",
                markup=True,
                classes="cred-widget",
            )
        )

        # Profile name
        scroll.mount(
            Static(
                "\n [dim]Profile name:[/dim]",
                markup=True,
                classes="cred-widget",
            )
        )
        scroll.mount(
            Input(
                placeholder="Profile name (e.g. deploy-root)",
                id="cred-name-input",
                classes="cred-widget",
            )
        )

        # Username
        scroll.mount(
            Static(
                " [dim]Username:[/dim]",
                markup=True,
                classes="cred-widget",
            )
        )
        scroll.mount(
            Input(
                placeholder="Username (default: root)",
                id="cred-user-input",
                classes="cred-widget",
            )
        )

        # Auth type section header
        scroll.mount(
            Static(
                "\n [bold]AUTH TYPE[/bold]  [dim]Enter to toggle[/dim]",
                markup=True,
                classes="cred-widget",
            )
        )

        # Auth type: Password — navigable Static
        scroll.mount(
            Static(
                "",
                markup=True,
                id="cred-form-auth-pw",
                classes="cred-widget cred-form-item",
            )
        )
        # Auth type: SSH Key — navigable Static
        scroll.mount(
            Static(
                "",
                markup=True,
                id="cred-form-auth-key",
                classes="cred-widget cred-form-item",
            )
        )

        # --- Password-mode field (visibility toggled) ---
        scroll.mount(
            Static(
                "\n [dim]Password:[/dim]",
                markup=True,
                classes="cred-widget cred-pw-field",
            )
        )
        scroll.mount(
            Input(
                placeholder="Password",
                password=True,
                id="cred-pass-input",
                classes="cred-widget cred-pw-field",
            )
        )

        # --- SSH-key-mode fields (visibility toggled) ---
        if self._detected_keys:
            scroll.mount(
                Static(
                    "\n [bold]DETECTED KEYS[/bold]  [dim]Enter to select[/dim]",
                    markup=True,
                    id="cred-detected-keys-header",
                    classes="cred-widget cred-key-field",
                )
            )
            for idx, (_kpath, _klabel) in enumerate(self._detected_keys):
                scroll.mount(
                    Static(
                        "",
                        markup=True,
                        id=f"cred-form-key-{idx}",
                        classes="cred-widget cred-key-field cred-form-item",
                    )
                )

        # Key path input
        default_keypath = ""
        if self._detected_keys and 0 <= self._detected_key_cursor < len(self._detected_keys):
            default_keypath = self._detected_keys[self._detected_key_cursor][0]

        scroll.mount(
            Static(
                "\n [dim]Key path:[/dim]",
                markup=True,
                classes="cred-widget cred-key-field",
            )
        )
        scroll.mount(
            Input(
                placeholder="Private key path (or select above / generate below)",
                value=default_keypath,
                id="cred-keypath-input",
                classes="cred-widget cred-key-field",
            )
        )

        # Passphrase input
        scroll.mount(
            Static(
                " [dim]Passphrase:[/dim]",
                markup=True,
                classes="cred-widget cred-key-field",
            )
        )
        scroll.mount(
            Input(
                placeholder="Key passphrase (optional)",
                password=True,
                id="cred-passphrase-input",
                classes="cred-widget cred-key-field",
            )
        )

        # Generate new key — navigable Static
        scroll.mount(
            Static(
                "",
                markup=True,
                id="cred-form-genkey",
                classes="cred-widget cred-key-field cred-form-item",
            )
        )

        # Generated public key display (if any)
        if self._generated_pubkey:
            scroll.mount(
                Static(
                    f"\n[bold green]Public key (copy to target hosts):[/bold green]\n"
                    f"[dim]{self._generated_pubkey}[/dim]",
                    markup=True,
                    id="run-cred-pubkey-display",
                    classes="cred-widget cred-key-field",
                )
            )

        # Separator
        scroll.mount(
            Static(
                "\n [dim]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/dim]",
                markup=True,
                classes="cred-widget",
            )
        )

        # Save Profile — navigable Static
        scroll.mount(
            Static(
                "",
                markup=True,
                id="cred-form-save",
                classes="cred-widget cred-form-item",
            )
        )
        # Cancel — navigable Static
        scroll.mount(
            Static(
                "",
                markup=True,
                id="cred-form-cancel",
                classes="cred-widget cred-form-item",
            )
        )

        # Build the navigable items list and set initial cursor
        self._form_items = self._build_form_items()
        self._form_cursor = 0

        # Apply initial visibility based on current auth type
        self._apply_cred_field_visibility()

        # Set all form item labels via refresh
        self._refresh_form_items()

    def _format_form_item(self, item_id: str, is_cursor: bool) -> str:
        """Format a single navigable form item as a Rich-markup line."""
        cursor = ">" if is_cursor else " "

        labels = {
            "cred-form-auth-pw": "Password",
            "cred-form-auth-key": "SSH Key",
            "cred-form-save": "Save Profile",
            "cred-form-cancel": "Cancel",
            "cred-form-genkey": "Generate New SSH Key",
        }

        # Detected key items
        if item_id.startswith("cred-form-key-"):
            idx = int(item_id.split("-")[-1])
            if 0 <= idx < len(self._detected_keys):
                _, klabel = self._detected_keys[idx]
                is_active = idx == self._detected_key_cursor
                if is_active:
                    prefix = "[green]*[/green]"
                else:
                    prefix = "[dim].[/dim]"
                if is_cursor:
                    return f" {cursor}  {prefix} [bold]{klabel}[/bold]"
                return f" {cursor}  {prefix} {klabel}"
            return f" {cursor}  ???"

        label = labels.get(item_id, item_id)

        # Highlight active auth type with radio-button indicators
        if item_id == "cred-form-auth-pw" and self._new_cred_auth_type == "password":
            label = f"[green]\u25cf[/green] {label}"
        elif item_id == "cred-form-auth-key" and self._new_cred_auth_type == "ssh_key":
            label = f"[green]\u25cf[/green] {label}"
        elif item_id in ("cred-form-auth-pw", "cred-form-auth-key"):
            label = f"[dim]\u25cb[/dim] {label}"

        if is_cursor:
            return f" {cursor}  [bold]{label}[/bold]"
        return f" {cursor}  {label}"

    def _refresh_form_items(self) -> None:
        """Update all navigable form item labels (cursor highlight, radio state)."""
        for i, item_id in enumerate(self._form_items):
            try:
                w = self.query_one(f"#{item_id}", Static)
                w.update(self._format_form_item(item_id, i == self._form_cursor))
            except Exception:
                pass

    def _build_form_items(self) -> list[str]:
        """Build the ordered list of navigable form-item IDs."""
        items: list[str] = ["cred-form-auth-pw", "cred-form-auth-key"]
        if self._new_cred_auth_type == "ssh_key":
            for idx in range(len(self._detected_keys)):
                items.append(f"cred-form-key-{idx}")
            items.append("cred-form-genkey")
        items.extend(["cred-form-save", "cred-form-cancel"])
        return items

    def _render_execution(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 4/4: Execution"
        )

        # Clear prior content and stale widgets
        content = self.query_one("#run-phase-content", Static)
        content.update("")
        self._remove_cred_widgets()
        self._remove_cred_lines()
        self._remove_host_toggles()
        self._remove_console_input()
        self._remove_exec_widgets()

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        scroll = self.query_one("#run-content", VerticalScroll)

        if self._is_running:
            action_btn.label = "Running..."
            action_btn.disabled = True
            cancel_btn.label = "Abort"

            # Header shows play/task — updated dynamically
            content.update("[dim]Starting...[/dim]")

            # Mount per-host status lines
            included = self._get_included_ips()
            col_header = (
                f"[dim]      {'IP':<16}  {'Hostname':<24}"
                f"{'Status':<14}{'Progress'}[/dim]"
            )
            scroll.mount(
                Static(col_header, markup=True, id="exec-col-header", classes="exec-widget")
            )
            for idx, ip in enumerate(included):
                label = self._format_exec_host_line(idx, ip)
                scroll.mount(
                    Static(label, markup=True, id=f"exec-host-{idx}", classes="exec-host-line exec-widget")
                )

            # Raw output area (last few lines for context)
            scroll.mount(
                Static("", markup=True, id="exec-raw", classes="exec-widget")
            )

            # Console input for interactive prompts
            console_input = Input(
                placeholder="Type here if the process needs input (Enter to send)",
                id="run-console-input",
            )
            scroll.mount(console_input)
            console_input.focus()
        else:
            action_btn.label = "Done"
            action_btn.variant = "primary"
            action_btn.disabled = False
            cancel_btn.display = False
            action_btn.focus()

    def _remove_console_input(self) -> None:
        """Remove the interactive console input widget."""
        for w in self.query("#run-console-input"):
            w.remove()

    def _remove_exec_widgets(self) -> None:
        """Remove all execution dashboard widgets."""
        for w in self.query(".exec-widget"):
            w.remove()

    def _remove_cred_widgets(self) -> None:
        """Remove all credential-phase widgets from the DOM."""
        for w in self.query(".cred-widget"):
            w.remove()

    # ------------------------------------------------------------------
    # Phase 0 -> 1: Start scan
    # ------------------------------------------------------------------

    def _get_dns_resolver(self) -> tuple:
        """Return (dns_client_or_None, zones_list) from app config."""
        dns_client = None
        dns_zones: list[str] = []
        try:
            dns_cfg = getattr(self.app.config, "dns", None)
            if dns_cfg and getattr(dns_cfg, "server", ""):
                dns_zones = list(getattr(dns_cfg, "zones", []) or [])
                if not dns_zones and getattr(dns_cfg, "domain", ""):
                    dns_zones = [dns_cfg.domain]
                from infraforge.dns_client import DNSClient
                dns_client = DNSClient.from_config(self.app.config)
        except Exception:
            pass
        return dns_client, dns_zones

    def _start_scan(self, target_override: str | None = None) -> None:
        if target_override:
            text = target_override.strip()
        else:
            try:
                ip_input = self.query_one("#run-ip-input", Input)
                text = ip_input.value.strip()
            except Exception:
                self.query_one("#run-status", Static).update(
                    "[bold red]No target input available — enter a range first[/bold red]"
                )
                return
        if not text:
            self.query_one("#run-status", Static).update(
                "[bold red]Enter an IP, range, or hostname[/bold red]"
            )
            return

        # Check if user typed a subnet ID (for IPAM import)
        if text.isdigit() and self._subnets:
            self._import_ipam_subnet(text)
            return

        try:
            dns_client, dns_zones = self._get_dns_resolver()
            ips, resolved, unresolved = resolve_targets(
                text, dns_client=dns_client, dns_zones=dns_zones,
            )
            self._resolved_ips = ips
        except Exception as e:
            self.query_one("#run-status", Static).update(
                f"[bold red]Invalid target: {e}[/bold red]"
            )
            return

        if not self._resolved_ips:
            msg = "[bold red]No valid IPs in the given range[/bold red]"
            if unresolved:
                msg = (
                    f"[bold red]Could not resolve: "
                    f"{', '.join(unresolved)}[/bold red]"
                )
            self.query_one("#run-status", Static).update(msg)
            return

        # Show what hostnames resolved to
        if resolved:
            parts = [f"{h} → {ip}" for h, ip in resolved.items()]
            self.query_one("#run-status", Static).update(
                f"[green]Resolved: {', '.join(parts)}[/green]"
            )

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
        self._skipped_scan = False
        self._render_phase()
        self._run_ping_sweep()

    def _use_direct_hosts(self) -> None:
        """Parse the direct hosts input and skip straight to credentials."""
        try:
            direct_input = self.query_one("#run-direct-input", Input)
            text = direct_input.value.strip()
        except Exception:
            self._set_status("[bold red]No input available[/bold red]")
            return

        if not text:
            self._set_status(
                "[bold red]Enter at least one host or IP[/bold red]"
            )
            return

        try:
            dns_client, dns_zones = self._get_dns_resolver()
            ips, resolved, unresolved = resolve_targets(
                text, dns_client=dns_client, dns_zones=dns_zones,
            )
        except Exception as e:
            self._set_status(f"[bold red]Invalid input: {e}[/bold red]")
            return

        if not ips:
            msg = "[bold red]No valid hosts in input[/bold red]"
            if unresolved:
                msg = (
                    f"[bold red]Could not resolve: "
                    f"{', '.join(unresolved)}[/bold red]"
                )
            self._set_status(msg)
            return

        if resolved:
            parts = [f"{h} → {ip}" for h, ip in resolved.items()]
            self._set_status(f"[green]Resolved: {', '.join(parts)}[/green]")

        # Treat all direct hosts as alive — skip the ping sweep
        self._resolved_ips = ips
        self._alive_ips = ips
        self._dead_ips = []
        self._host_included = {ip: True for ip in ips}
        self._host_info = {ip: HostInfo(ip=ip) for ip in ips}
        self._is_scanning = False
        self._skipped_scan = True

        # Skip phase 1 (scan), go straight to credentials
        self._transition_to_credentials()

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
        """Called after a fresh scan completes — initializes all hosts as included."""
        # Initialize all alive hosts as included
        self._host_included = {ip: True for ip in self._alive_ips}
        self._host_info = {ip: HostInfo(ip=ip) for ip in self._alive_ips}
        self._show_scan_results_with_toggles()
        # Start background enrichment (DNS, IPAM, nmap)
        if self._alive_ips:
            self._start_enrichment()

    def _show_scan_results_with_toggles(self) -> None:
        """Render scan results with a navigable host list."""
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
            lines.append(
                "[bold]Alive hosts:[/bold]  "
                "[dim]up/down to navigate, Space to toggle, Enter to proceed[/dim]"
            )
            lines.append(
                f"[dim]      {'IP':<16}  {'Hostname':<28}{'Description':<22}{'OS'}[/dim]"
            )
            lines.append("")
        else:
            lines.append(
                "[bold red]No alive hosts found. Cannot run playbook.[/bold red]"
            )

        self._update_phase_content("\n".join(lines))

        # Mount host lines as simple Static widgets
        if alive_count:
            self._remove_host_toggles()
            scroll = self.query_one("#run-content", VerticalScroll)
            for idx, ip in enumerate(self._alive_ips):
                label = self._format_host_line(idx, ip)
                line = Static(
                    label,
                    markup=True,
                    id=f"host-line-{idx}",
                    classes="host-line",
                )
                scroll.mount(line)

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Back"

        self._update_host_count()

    # Column widths for host line alignment
    _COL_IP = 16
    _COL_HOST = 28
    _COL_DESC = 22
    _COL_OS = 20

    def _format_host_line(self, idx: int, ip: str) -> str:
        """Build the markup for a single host line with enrichment data."""
        included = self._host_included.get(ip, True)
        is_cursor = idx == self._host_cursor
        cursor = ">" if is_cursor else " "

        if included:
            mark = "[green]\\[+][/green]"
        else:
            mark = "[red]\\[x][/red]"

        # Pad IP *before* adding markup so columns align
        ip_padded = ip.ljust(self._COL_IP)
        if included:
            ip_col = f"[bold]{ip_padded}[/bold]" if is_cursor else ip_padded
        else:
            ip_col = f"[bold dim]{ip_padded}[/bold dim]" if is_cursor else f"[dim]{ip_padded}[/dim]"

        # Enrichment columns
        info = self._host_info.get(ip)
        if info is None:
            return f" {cursor}  {mark}  {ip_col}"

        # Source indicator + hostname
        hostname = info.best_hostname
        is_autodiscovered = self._is_autodiscovered(info)
        if hostname:
            if len(hostname) > self._COL_HOST - 2:
                hostname = hostname[: self._COL_HOST - 5] + "..."
            if is_autodiscovered:
                # Globe icon for autodiscovered hosts
                host_display = f"[bright_blue]\U0001f310[/bright_blue] {hostname}"
                # Pad based on raw text length (globe counts as 1 char visually ~2 wide)
                pad = self._COL_HOST - len(hostname) - 2
            else:
                host_display = f"  {hostname}"
                pad = self._COL_HOST - len(hostname) - 2
            host_col = f"[cyan]{host_display}[/cyan]{' ' * max(pad, 1)}"
        elif info.dns_status == "running" or info.ipam_status == "running":
            placeholder = "resolving..."
            pad = self._COL_HOST - len(placeholder)
            host_col = f"[dim italic]  {placeholder}[/dim italic]{' ' * max(pad, 1)}"
        else:
            host_col = " " * self._COL_HOST

        # Description (skip "autodiscovered" text, use globe icon instead)
        desc_text = info.ipam_description or ""
        if "autodiscover" in desc_text.lower():
            desc_text = ""
        if desc_text:
            if len(desc_text) > self._COL_DESC - 2:
                desc_text = desc_text[: self._COL_DESC - 5] + "..."
            desc_padded = desc_text.ljust(self._COL_DESC)
            desc_col = f"[dim]{desc_padded}[/dim]"
        else:
            desc_col = " " * self._COL_DESC

        # OS guess
        os_text = info.os_guess or ""
        if os_text:
            if len(os_text) > self._COL_OS:
                os_text = os_text[: self._COL_OS - 3] + "..."
            os_col = f"[yellow]{os_text}[/yellow]"
        elif info.nmap_status == "running":
            os_col = "[dim italic]scanning...[/dim italic]"
        else:
            os_col = ""

        return f" {cursor}  {mark}  {ip_col}{host_col}{desc_col}{os_col}"

    @staticmethod
    def _is_autodiscovered(info: HostInfo) -> bool:
        """Check if the host was found via automatic discovery (DNS/IPAM)."""
        return bool(info.dns_hostname or info.ipam_hostname)

    def _toggle_host(self, idx: int) -> None:
        """Toggle inclusion of the host at the given index."""
        if 0 <= idx < len(self._alive_ips):
            ip = self._alive_ips[idx]
            self._host_included[ip] = not self._host_included.get(ip, True)
            # Update just this one line
            try:
                line = self.query_one(f"#host-line-{idx}", Static)
                line.update(self._format_host_line(idx, ip))
            except Exception:
                pass
            self._update_host_count()

    def _refresh_host_lines(self) -> None:
        """Refresh all host line labels (for cursor movement)."""
        for idx, ip in enumerate(self._alive_ips):
            try:
                line = self.query_one(f"#host-line-{idx}", Static)
                line.update(self._format_host_line(idx, ip))
            except Exception:
                pass

    def _scroll_to_host_cursor(self) -> None:
        """Scroll the host list so the cursor line is visible."""
        try:
            line = self.query_one(f"#host-line-{self._host_cursor}", Static)
            line.scroll_visible()
        except Exception:
            pass

    def _update_host_count(self) -> None:
        """Update the Next button and status bar with the current included count."""
        included_count = sum(1 for v in self._host_included.values() if v)
        total_alive = len(self._alive_ips)

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = f"Next ({included_count} hosts)"
        action_btn.disabled = included_count == 0

        status = self.query_one("#run-status", Static)
        if included_count:
            status.update(
                f"[dim]{included_count}/{total_alive} selected — "
                f"Space to toggle, Enter to proceed[/dim]"
            )
        else:
            status.update(
                "[dim]No hosts selected — Space to include hosts[/dim]"
            )

    def _remove_host_toggles(self) -> None:
        """Remove all host line widgets from the DOM."""
        for w in self.query(".host-line"):
            w.remove()

    def _remove_subnet_lines(self) -> None:
        """Remove all subnet suggestion lines from the DOM."""
        for w in self.query(".subnet-line"):
            w.remove()

    def _format_subnet_line(self, idx: int, s: dict) -> str:
        """Build markup for a single subnet suggestion line."""
        is_cursor = idx == self._subnet_cursor
        cursor = ">" if is_cursor else " "
        addr = s.get("subnet", "?")
        mask = s.get("mask", "?")
        cidr = f"{addr}/{mask}"
        desc = s.get("description", "")
        usage = s.get("usage", {})
        used = usage.get("used", "?")
        maxh = usage.get("maxhosts", "?")

        cidr_padded = cidr.ljust(20)
        usage_text = f"({used}/{maxh})"

        if is_cursor:
            line = f" {cursor}  [bold cyan]{cidr_padded}[/bold cyan]"
        else:
            line = f" {cursor}  {cidr_padded}"

        if desc:
            line += f"  [dim]{desc}[/dim]"
        line += f"  [dim]{usage_text}[/dim]"
        return line

    def _refresh_subnet_lines(self) -> None:
        """Refresh all subnet line labels (for cursor movement)."""
        for idx, s in enumerate(self._subnets):
            try:
                line = self.query_one(f"#subnet-line-{idx}", Static)
                line.update(self._format_subnet_line(idx, s))
            except Exception:
                pass

    def _scroll_to_subnet_cursor(self) -> None:
        """Scroll the subnet list so the cursor line is visible."""
        if self._subnet_cursor >= 0:
            try:
                line = self.query_one(f"#subnet-line-{self._subnet_cursor}", Static)
                line.scroll_visible()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Host enrichment (DNS, IPAM, nmap)
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="ansible-enrich")
    def _start_enrichment(self) -> None:
        """Run host enrichment in a background thread."""
        self._enriching = True
        self.app.call_from_thread(
            self._set_status, "[dim]Enriching host data (DNS, IPAM, nmap)...[/dim]"
        )

        # Instantiate clients based on config availability
        dns_client = None
        ipam_client = None

        dns_cfg = getattr(self.app.config, "dns", None)
        if dns_cfg and getattr(dns_cfg, "server", ""):
            try:
                from infraforge.dns_client import DNSClient
                dns_client = DNSClient.from_config(self.app.config)
            except Exception:
                pass

        ipam_cfg = getattr(self.app.config, "ipam", None)
        if ipam_cfg and getattr(ipam_cfg, "url", ""):
            try:
                from infraforge.ipam_client import IPAMClient
                ipam_client = IPAMClient(self.app.config)
            except Exception:
                pass

        # Check nmap availability
        nmap_found, sudo_works = check_nmap_available()

        def on_enrichment_update(ip: str, info: HostInfo) -> None:
            self._host_info[ip] = info
            self.app.call_from_thread(self._update_host_line_by_ip, ip)

        enrich_hosts(
            ips=self._alive_ips,
            dns_client=dns_client,
            ipam_client=ipam_client,
            enable_nmap=nmap_found,
            sudo_works=sudo_works,
            callback=on_enrichment_update,
        )

        self._enriching = False
        self.app.call_from_thread(self._on_enrichment_done)

    def _update_host_line_by_ip(self, ip: str) -> None:
        """Update a single host line widget after enrichment data arrives."""
        try:
            idx = self._alive_ips.index(ip)
            line = self.query_one(f"#host-line-{idx}", Static)
            line.update(self._format_host_line(idx, ip))
        except (ValueError, Exception):
            pass

    def _on_enrichment_done(self) -> None:
        """Called when all enrichment completes."""
        sources = []
        has_dns = any(i.dns_hostname for i in self._host_info.values())
        has_ipam = any(
            i.ipam_hostname or i.ipam_description for i in self._host_info.values()
        )
        has_os = any(i.os_guess for i in self._host_info.values())
        if has_dns:
            sources.append("DNS")
        if has_ipam:
            sources.append("IPAM")
        if has_os:
            sources.append("OS")
        if sources:
            label = ", ".join(sources)
            self._set_status(
                f"[dim]Enriched with {label} data  |  "
                f"Space to toggle, Enter to proceed[/dim]"
            )
        else:
            self._update_host_count()

    # ------------------------------------------------------------------
    # Phase 1 -> 2: Transition to credentials
    # ------------------------------------------------------------------

    def _get_included_ips(self) -> list[str]:
        """Return only the alive IPs that are toggled on."""
        return [ip for ip in self._alive_ips if self._host_included.get(ip, True)]

    def _transition_to_credentials(self) -> None:
        self._phase = 2
        self._credential_profiles = self._credential_mgr.load_profiles()
        if self._credential_profiles and self._selected_credential is None:
            self._selected_credential = self._credential_profiles[0]
        self._show_new_credential_form = False
        # Reset cursor: start at first profile, or at "+ New" if no profiles
        self._cred_cursor = 0
        self._render_phase()

    # ------------------------------------------------------------------
    # Phase 2: Credential CRUD
    # ------------------------------------------------------------------

    def _detect_ssh_keys(self) -> list[tuple[str, str]]:
        """Scan ~/.ssh/ and ~/.config/infraforge/ssh_keys/ for private keys.

        Returns list of (path, label) tuples.
        """
        keys: list[tuple[str, str]] = []
        seen: set[str] = set()

        search_dirs = [
            Path.home() / ".ssh",
            Path.home() / ".config" / "infraforge" / "ssh_keys",
        ]

        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                # Skip public keys, known_hosts, config, authorized_keys
                if f.suffix == ".pub" or f.name in (
                    "known_hosts", "known_hosts.old", "config",
                    "authorized_keys", "authorized_keys2",
                ):
                    continue
                # Heuristic: private keys start with "-----BEGIN" or are common names
                try:
                    head = f.read_bytes()[:40]
                    if b"BEGIN" in head or f.name.startswith("id_"):
                        path_str = str(f)
                        if path_str not in seen:
                            seen.add(path_str)
                            # Label: "id_ed25519 (~/.ssh/)"
                            rel = f"~/{f.relative_to(Path.home())}"
                            keys.append((path_str, f"{f.name}  [dim]{rel}[/dim]"))
                except (OSError, ValueError):
                    continue

        return keys

    def _select_detected_key(self, idx: int) -> None:
        """Fill the key path input with the selected detected key."""
        if 0 <= idx < len(self._detected_keys):
            path, _ = self._detected_keys[idx]
            self._detected_key_cursor = idx
            try:
                keypath_input = self.query_one("#cred-keypath-input", Input)
                keypath_input.value = path
            except Exception:
                pass
            self._set_status(f"[green]Selected key: {path}[/green]")

    def _save_new_credential(self) -> None:
        """Collect form inputs and save a new credential profile."""
        try:
            name_input = self.query_one("#cred-name-input", Input)
            user_input = self.query_one("#cred-user-input", Input)
        except Exception:
            self._set_status("[bold red]Form not ready[/bold red]")
            return

        name = name_input.value.strip()
        username = user_input.value.strip() or "root"

        if not name:
            self._set_status("[bold red]Profile name is required[/bold red]")
            return

        if self._credential_mgr.get_profile(name):
            self._set_status(
                f"[bold red]Profile '{name}' already exists[/bold red]"
            )
            return

        profile = CredentialProfile(
            name=name,
            auth_type=self._new_cred_auth_type,
            username=username,
        )

        if self._new_cred_auth_type == "password":
            try:
                pass_input = self.query_one("#cred-pass-input", Input)
                profile.password = pass_input.value
            except Exception:
                pass
        else:
            try:
                keypath_input = self.query_one("#cred-keypath-input", Input)
                profile.private_key_path = keypath_input.value.strip()
            except Exception:
                pass
            try:
                passphrase_input = self.query_one("#cred-passphrase-input", Input)
                profile.passphrase = passphrase_input.value
            except Exception:
                pass

        if self._new_cred_auth_type == "ssh_key" and not profile.private_key_path:
            self._set_status(
                "[bold red]SSH key path is required (or generate one)[/bold red]"
            )
            return

        self._credential_mgr.add_profile(profile)
        self._credential_profiles = self._credential_mgr.load_profiles()
        self._selected_credential = profile
        self._show_new_credential_form = False
        self._generated_pubkey = ""
        # Position cursor on the newly saved profile
        for idx, p in enumerate(self._credential_profiles):
            if p.name == profile.name:
                self._cred_cursor = idx
                break
        self._render_credential_selection()
        self._set_status(f"[green]Saved credential profile '{name}'[/green]")

    @work(thread=True, exclusive=True, group="ansible-keygen")
    def _generate_ssh_key(self) -> None:
        """Generate an SSH key pair in a background thread."""
        try:
            name_input = self.query_one("#cred-name-input", Input)
            name = name_input.value.strip() or "infraforge"
        except Exception:
            name = "infraforge"

        passphrase = ""
        try:
            passphrase_input = self.query_one("#cred-passphrase-input", Input)
            passphrase = passphrase_input.value
        except Exception:
            pass

        self.app.call_from_thread(
            self._set_status, "[dim]Generating SSH key (4096 bits)...[/dim]"
        )

        try:
            key_path, pubkey = self._credential_mgr.generate_ssh_key(
                name=name, passphrase=passphrase,
            )
            self._generated_pubkey = pubkey

            def _update_ui() -> None:
                try:
                    keypath_input = self.query_one("#cred-keypath-input", Input)
                    keypath_input.value = str(key_path)
                except Exception:
                    pass
                self._render_credential_selection()
                self._set_status(f"[green]Generated SSH key: {key_path}[/green]")

            self.app.call_from_thread(_update_ui)
        except Exception as e:
            self.app.call_from_thread(
                self._set_status,
                f"[bold red]Key generation failed: {e}[/bold red]",
            )

    def _delete_selected_credential(self) -> None:
        if self._selected_credential:
            name = self._selected_credential.name
            self._credential_mgr.delete_profile(name)
            self._credential_profiles = self._credential_mgr.load_profiles()
            self._selected_credential = (
                self._credential_profiles[0]
                if self._credential_profiles
                else None
            )
            # Clamp cursor
            total_items = len(self._credential_profiles) + 1
            if self._cred_cursor >= total_items:
                self._cred_cursor = max(0, total_items - 1)
            self._render_credential_selection()
            self._set_status(
                f"[yellow]Deleted credential profile '{name}'[/yellow]"
            )

    def _delete_credential_at_cursor(self) -> None:
        """Delete the credential profile at the current cursor position."""
        if self._cred_cursor < len(self._credential_profiles):
            prof = self._credential_profiles[self._cred_cursor]
            name = prof.name
            self._credential_mgr.delete_profile(name)
            self._credential_profiles = self._credential_mgr.load_profiles()
            # If the deleted profile was selected, clear or re-select
            if self._selected_credential and self._selected_credential.name == name:
                self._selected_credential = (
                    self._credential_profiles[0]
                    if self._credential_profiles
                    else None
                )
            # Clamp cursor
            total_items = len(self._credential_profiles) + 1
            if self._cred_cursor >= total_items:
                self._cred_cursor = max(0, total_items - 1)
            self._render_credential_selection()
            self._set_status(
                f"[yellow]Deleted credential profile '{name}'[/yellow]"
            )

    # ------------------------------------------------------------------
    # Execution dashboard display
    # ------------------------------------------------------------------

    # Column widths for exec host lines
    _EXEC_COL_IP = 16
    _EXEC_COL_HOST = 24
    _EXEC_COL_STATUS = 14

    def _format_exec_host_line(self, idx: int, ip: str) -> str:
        """Build markup for a single host status line in the execution dashboard."""
        progress = self._progress
        host_st = progress.hosts.get(ip) if progress else None

        # IP column
        ip_padded = ip.ljust(self._EXEC_COL_IP)

        # Hostname from enrichment data
        info = self._host_info.get(ip)
        hostname = ""
        if info:
            hostname = info.best_hostname
        if not hostname:
            hostname = "-"
        if len(hostname) > self._EXEC_COL_HOST - 2:
            hostname = hostname[: self._EXEC_COL_HOST - 5] + "..."
        host_padded = hostname.ljust(self._EXEC_COL_HOST)

        if host_st is None:
            # No status yet
            return f"      {ip_padded}  [dim]{host_padded}[/dim][dim]waiting[/dim]"

        # Status with color and icon — use summary_state for "done" hosts
        state = host_st.current_state
        display_state = host_st.summary_state if state == "done" else state
        icon, color = _state_display(display_state)
        status_text = display_state.upper() if display_state in ("failed", "unreachable") else display_state
        status_padded = status_text.ljust(self._EXEC_COL_STATUS)

        # Progress counters
        parts: list[str] = []
        if host_st.ok:
            parts.append(f"[green]ok:{host_st.ok}[/green]")
        if host_st.changed:
            parts.append(f"[yellow]changed:{host_st.changed}[/yellow]")
        if host_st.failed:
            parts.append(f"[red]failed:{host_st.failed}[/red]")
        if host_st.skipped:
            parts.append(f"[dim]skip:{host_st.skipped}[/dim]")
        if host_st.unreachable:
            parts.append(f"[red]unreach:{host_st.unreachable}[/red]")
        progress_text = " ".join(parts) if parts else ""

        # Error message (inline, truncated)
        error = ""
        if host_st.error_msg:
            err_text = host_st.error_msg
            if len(err_text) > 50:
                err_text = err_text[:47] + "..."
            error = f'  [red italic]"{err_text}"[/red italic]'

        return (
            f"  {icon}  {ip_padded}  "
            f"[dim]{host_padded}[/dim]"
            f"[{color}]{status_padded}[/{color}]"
            f"{progress_text}{error}"
        )

    def _process_output(self, text: str) -> None:
        """Parse PTY output and update the structured execution dashboard."""
        if not self._progress:
            return

        needs_refresh = False
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # Keep last N raw lines for the console area
            self._raw_lines.append(stripped)
            if len(self._raw_lines) > 5:
                self._raw_lines = self._raw_lines[-5:]
            # Feed to parser
            if self._progress.feed_line(stripped):
                needs_refresh = True

        if needs_refresh:
            self._refresh_execution_display()
        else:
            # At minimum update the raw output area
            self._update_raw_output()

    def _refresh_execution_display(self) -> None:
        """Refresh the entire execution dashboard from current state."""
        progress = self._progress
        if not progress:
            return

        # Update header (play + task)
        header_parts = []
        if progress.current_play:
            header_parts.append(f"[bold]Play:[/bold] {progress.current_play}")
        if progress.current_task:
            task_str = f"[bold]Task:[/bold] {progress.current_task}"
            if self._task_estimate > 0:
                task_str += f"  [dim]({progress.task_index} of ~{self._task_estimate})[/dim]"
            else:
                task_str += f"  [dim](#{progress.task_index})[/dim]"
            header_parts.append(task_str)
        elif progress.in_recap:
            header_parts.append("[bold]Play Recap[/bold]")

        if header_parts:
            self._update_phase_content("\n".join(header_parts))

        # Update per-host lines
        included = self._get_included_ips()
        for idx, ip in enumerate(included):
            try:
                line = self.query_one(f"#exec-host-{idx}", Static)
                line.update(self._format_exec_host_line(idx, ip))
            except Exception:
                pass

        # Update raw output area
        self._update_raw_output()

    def _update_raw_output(self) -> None:
        """Update the raw console output area with recent lines."""
        try:
            raw_widget = self.query_one("#exec-raw", Static)
            if self._raw_lines:
                escaped = [self._esc(ln) for ln in self._raw_lines[-5:]]
                raw_widget.update(
                    "[dim]" + "\n".join(escaped) + "[/dim]"
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Phase 2 -> 3: Start execution
    # ------------------------------------------------------------------

    def _start_execution(self) -> None:
        self._phase = 3
        self._is_running = True
        self._aborted = False
        self._run_start = time.monotonic()
        self._raw_lines = []
        self._task_estimate = self._playbook.task_count

        # Initialize progress tracker with all included hosts
        included = self._get_included_ips()
        self._progress = PlaybookProgress(
            hosts={ip: ExecHostStatus(ip=ip) for ip in included}
        )

        self._render_phase()
        self._start_run_timer()
        self._execute_playbook()

    @work(thread=True, exclusive=True, group="ansible-run")
    def _execute_playbook(self) -> None:
        # Generate temp inventory
        inv_path = generate_inventory(self._get_included_ips())

        # Compute log path
        playbook_dir = self._playbook.path.parent
        log_dir = playbook_dir / "logs"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_name = f"{self._playbook.path.stem}_{ts}.log"
        self._log_path = log_dir / log_name

        # Build credential arguments
        cred_args: list[str] = []
        cred_env: dict[str, str] = {}
        if self._selected_credential:
            cred_args, cred_env = build_credential_args(self._selected_credential)

        # Build extra-vars arguments
        extra_args: list[str] = []
        if self._extra_vars:
            import json
            extra_args.extend(["--extra-vars", json.dumps(self._extra_vars)])

        # Create interactive runner with PTY
        runner = PlaybookRunner(
            self._playbook.path,
            inv_path,
            self._log_path,
            extra_args=extra_args,
            credential_args=cred_args,
            credential_env=cred_env,
        )
        self._runner = runner

        try:
            cmd_str = runner.start()
            self._raw_lines.append(f"$ {cmd_str}")
            self.app.call_from_thread(self._update_raw_output)
        except FileNotFoundError as e:
            self._raw_lines.append(str(e))
            self.app.call_from_thread(self._update_raw_output)
            self._is_running = False
            self.app.call_from_thread(self._on_execution_done)
            return

        # Read loop — pull output from the PTY until the process exits
        while True:
            text = runner.read_output(timeout=0.2)
            if text:
                self.app.call_from_thread(self._process_output, text)
            if self._aborted:
                runner.kill()
                break
            if not runner.is_running:
                # Drain remaining output
                for _ in range(10):
                    leftover = runner.read_output(timeout=0.05)
                    if not leftover:
                        break
                    self.app.call_from_thread(self._process_output, leftover)
                break

        self._exit_code = runner.exit_code
        runner.cleanup()

        # Clean up temp inventory
        try:
            inv_path.unlink(missing_ok=True)
        except Exception:
            pass

        self._is_running = False
        self._runner = None
        self.app.call_from_thread(self._on_execution_done)



    def _on_execution_done(self) -> None:
        self._stop_run_timer()
        self._remove_console_input()
        elapsed = time.monotonic() - self._run_start
        ec = self._exit_code

        # Refresh host lines one final time with "done" state
        if self._progress:
            self._refresh_execution_display()

        # Update header to summary
        summary_parts = []
        if self._progress:
            ok_count = sum(
                1 for h in self._progress.hosts.values()
                if h.summary_state in ("ok", "changed")
            )
            fail_count = sum(
                1 for h in self._progress.hosts.values()
                if h.summary_state == "failed"
            )
            unreach_count = sum(
                1 for h in self._progress.hosts.values()
                if h.summary_state == "unreachable"
            )
            parts = []
            if ok_count:
                parts.append(f"[green]{ok_count} succeeded[/green]")
            if fail_count:
                parts.append(f"[red]{fail_count} failed[/red]")
            if unreach_count:
                parts.append(f"[red]{unreach_count} unreachable[/red]")
            summary_parts.append("[bold]Summary:[/bold]  " + ", ".join(parts))
        if ec is not None and not self._aborted:
            ec_color = "green" if ec == 0 else "red"
            summary_parts.append(f"[{ec_color}]Exit code: {ec}[/{ec_color}]")
        if self._log_path:
            summary_parts.append(f"[dim]Log: {self._log_path}[/dim]")
        if self._progress and self._progress.warnings:
            wc = len(self._progress.warnings)
            summary_parts.append(f"[yellow]{wc} warning(s)[/yellow]")
        if summary_parts:
            self._update_phase_content("\n".join(summary_parts))

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Done"
        action_btn.variant = "primary"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.display = False

        action_btn.focus()

        status = self.query_one("#run-status", Static)
        if self._aborted:
            status.update(
                f"[bold yellow]Aborted after {elapsed:.0f}s[/bold yellow]"
            )
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
        if self._runner:
            self._runner.kill()
        self._remove_console_input()
        self._raw_lines.append("--- Aborted by user ---")
        self._update_raw_output()

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
                f"Loaded {len(subnets)} subnets from IPAM",
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
                    f"[red]No addresses found in subnet {subnet_id}[/red]",
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
            self._resolved_ips,
            workers=50,
            callback=on_result,
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
