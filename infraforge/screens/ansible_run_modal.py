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

from infraforge.ansible_runner import (
    PlaybookInfo,
    PlaybookRunner,
    build_credential_args,
    generate_inventory,
    parse_ip_ranges,
    ping_sweep,
)
from infraforge.credential_manager import CredentialManager, CredentialProfile
from infraforge.host_enrichment import HostInfo, enrich_hosts, check_nmap_available

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

    #run-console-input {
        dock: bottom;
        margin: 0;
        border-top: solid $accent;
    }

    #run-console-input:focus {
        border-top: solid $success;
    }
    """

    def __init__(self, playbook: PlaybookInfo) -> None:
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
        # Phase 3 — execution
        self._is_running: bool = False
        self._run_start: float = 0.0
        self._run_timer: Timer | None = None
        self._exit_code: int | None = None
        self._log_path: Path | None = None
        self._runner: PlaybookRunner | None = None
        self._aborted: bool = False
        # IPAM
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

    def on_key(self, event) -> None:
        """Handle arrow keys for subnet selection (Phase 0), host toggles (Phase 1), and credential navigation (Phase 2)."""
        # Phase 0: subnet list navigation
        if self._phase == 0 and self._subnets:
            focused = self.focused
            # If the Input is focused and cursor is on manual entry, let it handle keys
            if isinstance(focused, Input) and self._subnet_cursor == -1:
                return
            # If the Input is focused but cursor is on a subnet line, intercept arrows
            if event.key == "up":
                event.prevent_default()
                event.stop()
                if self._subnet_cursor > 0:
                    self._subnet_cursor -= 1
                    self._refresh_subnet_lines()
                    self._scroll_to_subnet_cursor()
                    # Unfocus input when moving up into subnet list
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
                    # Move to manual input
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

        # Phase 1: host toggle navigation
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

        # Phase 2: credential list navigation
        if self._phase == 2 and not self._show_new_credential_form:
            focused = self.focused
            if isinstance(focused, (Button, Input)):
                return

            total_items = len(self._credential_profiles) + 1  # profiles + "New"
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
                    # Select profile and proceed to execution
                    self._selected_credential = self._credential_profiles[self._cred_cursor]
                    self._refresh_cred_lines()
                    self._start_execution()
                else:
                    # "+ New Profile" line
                    self._show_new_credential_form = True
                    self._generated_pubkey = ""
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

    def on_click(self, event) -> None:
        """Handle mouse clicks on host lines, subnet lines, and credential lines."""
        widget = event.widget
        if not widget or not hasattr(widget, "id") or not widget.id:
            return
        if widget.id.startswith("host-line-"):
            try:
                idx = int(widget.id.split("-")[-1])
                if 0 <= idx < len(self._alive_ips):
                    self._host_cursor = idx
                    self._toggle_host(idx)
            except (ValueError, IndexError):
                pass
        elif widget.id.startswith("subnet-line-"):
            try:
                idx = int(widget.id.split("-")[-1])
                if 0 <= idx < len(self._subnets):
                    s = self._subnets[idx]
                    cidr = f"{s.get('subnet', '')}/{s.get('mask', '24')}"
                    self._start_scan(cidr)
            except (ValueError, IndexError):
                pass
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
                        # Clicked "+ New Profile"
                        self._show_new_credential_form = True
                        self._generated_pubkey = ""
                        self._render_credential_selection()
                        return
                    self._refresh_cred_lines()
                    self._update_cred_status()
            except (ValueError, IndexError):
                pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter pressed in an Input — handle per-phase."""
        if self._phase == 0 and event.input.id == "run-ip-input":
            self._start_scan()
        elif self._phase == 3 and event.input.id == "run-console-input":
            # Send user input to the running subprocess via PTY
            if self._runner and self._runner.is_running:
                text = event.input.value
                self._runner.send_input(text + "\n")
                # Echo what the user typed into the output area
                self._append_output(f"> {text}\n", "status")
                event.input.value = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
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

        # --- Credential form buttons (new form only) ---
        elif btn_id == "run-cred-save-btn":
            self._save_new_credential()

        elif btn_id == "run-cred-cancel-btn":
            self._show_new_credential_form = False
            self._generated_pubkey = ""
            self._render_credential_selection()

        elif btn_id == "run-cred-genkey-btn":
            self._generate_ssh_key()

        elif btn_id == "run-cred-delete-btn":
            self._delete_selected_credential()


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
        self._remove_subnet_lines()

        content = self.query_one("#run-phase-content", Static)

        ipam_cfg = getattr(self.app.config, "ipam", None)
        has_ipam = ipam_cfg and getattr(ipam_cfg, "url", "")

        lines = [
            "[bold]Select a target range and press Enter to scan.[/bold]",
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

        # Remove old input for clean remount
        scroll = self.query_one("#run-content", VerticalScroll)
        prev_value = ""
        for w in self.query("#run-ip-input"):
            prev_value = w.value
            w.remove()
        for w in self.query("#run-ipam-btn"):
            w.remove()

        # Mount subnet suggestion lines (simple Static widgets)
        if self._subnets:
            if self._subnet_cursor == -1:
                self._subnet_cursor = 0  # auto-select first subnet
            for idx, s in enumerate(self._subnets):
                label = self._format_subnet_line(idx, s)
                line = Static(
                    label,
                    markup=True,
                    id=f"subnet-line-{idx}",
                    classes="subnet-line",
                )
                scroll.mount(line)

        # Manual input line at the bottom
        ip_input = Input(
            placeholder="Or type a range: 10.0.1.0/24, 10.0.5.1-100",
            id="run-ip-input",
            value=prev_value,
        )
        scroll.mount(ip_input)

        # Focus the input only if cursor is on manual entry
        if self._subnet_cursor == -1 or not self._subnets:
            ip_input.focus()

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Scan Hosts"
        action_btn.variant = "primary"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Cancel"

        status = self.query_one("#run-status", Static)
        if self._subnets:
            status.update("[dim]Select a subnet or type a range, then press Enter[/dim]")
        else:
            status.update("[dim]Enter target IPs and press Enter[/dim]")

        # Auto-load IPAM subnets in background if not loaded yet
        if has_ipam and not self._ipam_loaded:
            self._load_ipam_subnets()

    def _render_ping_sweep(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 2/4: Host Validation"
        )

        # Remove widgets from other phases
        for w in self.query("#run-ip-input"):
            w.remove()
        for w in self.query("#run-ipam-btn"):
            w.remove()
        self._remove_subnet_lines()
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

        # Remove widgets from other phases — Phase 0 widgets may linger
        for w in self.query("#run-ip-input"):
            w.remove()
        for w in self.query("#run-ipam-btn"):
            w.remove()
        self._remove_subnet_lines()
        self._remove_host_toggles()

        # Remove all credential widgets for clean re-render
        self._remove_cred_widgets()
        self._remove_cred_lines()

        scroll = self.query_one("#run-content", VerticalScroll)
        content = self.query_one("#run-phase-content", Static)

        lines = [
            "[bold]Select credentials for playbook execution.[/bold]",
            "",
            f"[dim]{len(self._get_included_ips())} hosts will be targeted.[/dim]",
            "",
        ]

        if self._credential_profiles:
            lines.append(
                "[bold]Saved Credential Profiles:[/bold]  "
                "[dim]up/down to navigate, Space to select, Enter to run, d to delete[/dim]"
            )
            lines.append(
                f"[dim]      {'Name':<20}  {'User':<12}  Auth[/dim]"
            )
            lines.append("")
        else:
            lines.append("[yellow]No saved credential profiles.[/yellow]")
            lines.append("[dim]Press Enter on '+ New Profile' to create one.[/dim]")

        content.update("\n".join(lines))

        # Mount credential profile lines as Static widgets (cursor-based)
        if not self._show_new_credential_form:
            for idx, prof in enumerate(self._credential_profiles):
                label = self._format_cred_line(idx, prof)
                line = Static(
                    label,
                    markup=True,
                    id=f"cred-line-{idx}",
                    classes="cred-line",
                )
                scroll.mount(line)

            # "+ New Profile" line
            new_line_idx = len(self._credential_profiles)
            new_label = self._format_new_cred_line()
            new_line = Static(
                new_label,
                markup=True,
                id=f"cred-line-{new_line_idx}",
                classes="cred-line",
            )
            scroll.mount(new_line)
        else:
            self._mount_new_credential_form(scroll)

        # Update action / cancel buttons
        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Run Playbook"
        action_btn.variant = "success"
        action_btn.disabled = self._selected_credential is None

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Back"

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
        if is_cursor:
            return f" {cursor}  {check}  [bold]{name_padded}[/bold]  [dim]{user_padded}  {auth}[/dim]"
        else:
            return f" {cursor}  {check}  {name_padded}  [dim]{user_padded}  {auth}[/dim]"

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
        action_btn.disabled = self._selected_credential is None

        status = self.query_one("#run-status", Static)
        if self._selected_credential:
            status.update(
                f"[dim]Selected: {self._selected_credential.name} "
                f"({self._selected_credential.username}) — "
                f"Enter to run, Space to deselect[/dim]"
            )
        else:
            status.update("[dim]Navigate with arrows, Space to select, Enter on profile to run[/dim]")

    def _remove_cred_lines(self) -> None:
        """Remove all credential line widgets from the DOM."""
        for w in self.query(".cred-line"):
            w.remove()

    def _mount_new_credential_form(self, scroll: VerticalScroll) -> None:
        """Mount inline form widgets for creating a new credential."""
        scroll.mount(
            Static(
                "\n[bold]New Credential Profile[/bold]",
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
        scroll.mount(
            Input(
                placeholder="Username (default: root)",
                id="cred-user-input",
                classes="cred-widget",
            )
        )

        # Auth type toggle
        scroll.mount(
            Static(
                "[bold]Auth Type:[/bold]",
                markup=True,
                classes="cred-widget",
            )
        )
        pw_variant = "primary" if self._new_cred_auth_type == "password" else "default"
        key_variant = "primary" if self._new_cred_auth_type == "ssh_key" else "default"
        scroll.mount(
            Button(
                "Password",
                variant=pw_variant,
                id="cred-auth-type-pw",
                classes="cred-widget",
            )
        )
        scroll.mount(
            Button(
                "SSH Key",
                variant=key_variant,
                id="cred-auth-type-key",
                classes="cred-widget",
            )
        )

        if self._new_cred_auth_type == "password":
            scroll.mount(
                Input(
                    placeholder="Password",
                    password=True,
                    id="cred-pass-input",
                    classes="cred-widget",
                )
            )
        else:
            scroll.mount(
                Input(
                    placeholder="Private key path (or generate below)",
                    id="cred-keypath-input",
                    classes="cred-widget",
                )
            )
            scroll.mount(
                Input(
                    placeholder="Key passphrase (optional)",
                    password=True,
                    id="cred-passphrase-input",
                    classes="cred-widget",
                )
            )
            scroll.mount(
                Button(
                    "Generate New SSH Key",
                    variant="warning",
                    id="run-cred-genkey-btn",
                    classes="cred-widget",
                )
            )

            if self._generated_pubkey:
                scroll.mount(
                    Static(
                        f"\n[bold green]Public key (copy to target hosts):[/bold green]\n"
                        f"[dim]{self._generated_pubkey}[/dim]",
                        markup=True,
                        id="run-cred-pubkey-display",
                        classes="cred-widget",
                    )
                )

        scroll.mount(
            Button(
                "Save Profile",
                variant="success",
                id="run-cred-save-btn",
                classes="cred-widget",
            )
        )
        scroll.mount(
            Button(
                "Cancel",
                variant="default",
                id="run-cred-cancel-btn",
                classes="cred-widget",
            )
        )

    def _render_execution(self) -> None:
        title = self.query_one("#run-title", Static)
        title.update(
            f"[bold]Run: {self._playbook.filename}[/bold]  Phase 4/4: Execution"
        )

        # Clear prior content
        content = self.query_one("#run-phase-content", Static)
        content.update("")
        self._remove_cred_widgets()
        self._remove_cred_lines()
        self._remove_host_toggles()
        self._remove_console_input()

        action_btn = self.query_one("#run-action-btn", Button)
        cancel_btn = self.query_one("#run-cancel-btn", Button)

        if self._is_running:
            action_btn.label = "Running..."
            action_btn.disabled = True
            cancel_btn.label = "Abort"

            # Mount interactive console input at the bottom of the scroll area
            scroll = self.query_one("#run-content", VerticalScroll)
            console_input = Input(
                placeholder="Type here to send input to the process (Enter to send)",
                id="run-console-input",
            )
            scroll.mount(console_input)
            console_input.focus()
        else:
            action_btn.label = "Close"
            action_btn.variant = "default"
            action_btn.disabled = False
            cancel_btn.label = "Close"

    def _remove_console_input(self) -> None:
        """Remove the interactive console input widget."""
        for w in self.query("#run-console-input"):
            w.remove()

    def _remove_cred_widgets(self) -> None:
        """Remove all credential-phase widgets from the DOM."""
        for w in self.query(".cred-widget"):
            w.remove()

    # ------------------------------------------------------------------
    # Phase 0 -> 1: Start scan
    # ------------------------------------------------------------------

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
    # Phase 2 -> 3: Start execution
    # ------------------------------------------------------------------

    def _start_execution(self) -> None:
        self._phase = 3
        self._is_running = True
        self._aborted = False
        self._run_start = time.monotonic()
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

        # Create interactive runner with PTY
        runner = PlaybookRunner(
            self._playbook.path,
            inv_path,
            self._log_path,
            credential_args=cred_args,
            credential_env=cred_env,
        )
        self._runner = runner

        try:
            cmd_str = runner.start()
            self.app.call_from_thread(
                self._append_output, f"$ {cmd_str}\n", "status"
            )
        except FileNotFoundError as e:
            self.app.call_from_thread(
                self._append_output, f"{e}\n", "status"
            )
            self._is_running = False
            self.app.call_from_thread(self._on_execution_done)
            return

        # Read loop — pull output from the PTY until the process exits
        while True:
            text = runner.read_output(timeout=0.2)
            if text:
                self.app.call_from_thread(
                    self._append_output, text, "stdout"
                )
            if self._aborted:
                runner.kill()
                break
            if not runner.is_running:
                # Drain any remaining output
                for _ in range(10):
                    leftover = runner.read_output(timeout=0.05)
                    if not leftover:
                        break
                    self.app.call_from_thread(
                        self._append_output, leftover, "stdout"
                    )
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

    def _append_output(self, text: str, stream_type: str) -> None:
        scroll = self.query_one("#run-content", VerticalScroll)
        css_class = "run-output-line"

        # PTY output arrives in chunks — split into lines for display.
        # Partial lines (no trailing newline) are displayed as-is so
        # interactive prompts show immediately.
        lines = text.split("\n")
        for i, line in enumerate(lines):
            # Skip empty trailing element from split
            if i == len(lines) - 1 and not line:
                continue
            if stream_type == "status":
                markup = f"[bold cyan]{self._esc(line)}[/bold cyan]"
            else:
                markup = self._esc(line)
            widget = Static(markup, classes=css_class, markup=True)
            # Insert before the console input (if present) so input stays at bottom
            try:
                console_input = self.query_one("#run-console-input", Input)
                scroll.mount(widget, before=console_input)
            except Exception:
                scroll.mount(widget)

        scroll.scroll_end(animate=False)

    def _on_execution_done(self) -> None:
        self._stop_run_timer()
        self._remove_console_input()
        elapsed = time.monotonic() - self._run_start

        # Show exit code in output area
        ec = self._exit_code
        if ec is not None and not self._aborted:
            color = "green" if ec == 0 else "red"
            self._append_output(
                f"\nCompleted with exit code {ec}\n", "status"
            )
            if self._log_path:
                self._append_output(
                    f"Log saved to {self._log_path}\n", "status"
                )

        action_btn = self.query_one("#run-action-btn", Button)
        action_btn.label = "Close"
        action_btn.variant = "default"
        action_btn.disabled = False

        cancel_btn = self.query_one("#run-cancel-btn", Button)
        cancel_btn.label = "Close"

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
