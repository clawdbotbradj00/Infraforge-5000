"""Configuration modals for the InfraForge setup wizard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import work
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch


# ── Arrow-key navigation mixin for config modals ──────────────────

_FOCUSABLE = (Input, Select, Switch)


class _ArrowNavModal(ModalScreen):
    """Base modal that adds up/down arrow navigation between fields
    and auto-focuses the first input on mount."""

    def on_mount(self) -> None:
        # Disable focus on scroll containers so they don't steal arrow keys
        for vs in self.query(VerticalScroll):
            vs.can_focus = False
        fields = self._get_focusable_fields()
        if fields:
            fields[0].focus()

    @staticmethod
    def _is_displayed(widget) -> bool:
        """Check widget and all ancestors have display=True."""
        node = widget
        while node is not None:
            if not node.display:
                return False
            node = node.parent
        return True

    def _get_focusable_fields(self) -> list:
        """Return visible, focusable fields in DOM order."""
        all_widgets = list(self.query("*"))
        return [
            w for w in all_widgets
            if self._is_displayed(w) and (
                isinstance(w, _FOCUSABLE)
                or (isinstance(w, Button) and (w.id or "").startswith("save-"))
            )
        ]

    def on_key(self, event) -> None:
        # Enter on Input/Switch advances to next field
        if event.key == "enter":
            focused = self.app.focused
            if isinstance(focused, (Input, Switch)):
                event.prevent_default()
                event.stop()
                self._move_field(1)
            return

        if event.key not in ("down", "up"):
            return
        # Don't intercept arrows when any Select dropdown is expanded
        for sel in self.query(Select):
            if sel.expanded:
                return
        event.prevent_default()
        event.stop()
        if event.key == "down":
            self._move_field(1)
        else:
            self._move_field(-1)

    def _move_field(self, direction: int) -> None:
        fields = self._get_focusable_fields()
        if not fields:
            return
        current = self.app.focused
        if current in fields:
            idx = fields.index(current)
            new_idx = (idx + direction) % len(fields)
            fields[new_idx].focus()
        else:
            fields[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("reveal-"):
            input_id = btn_id[len("reveal-"):]
            try:
                inp = self.query_one(f"#{input_id}", Input)
                inp.password = not inp.password
                event.button.label = "Hide" if not inp.password else "Reveal"
            except Exception:
                pass
        elif btn_id.startswith("copy-"):
            input_id = btn_id[len("copy-"):]
            try:
                inp = self.query_one(f"#{input_id}", Input)
                value = inp.value
                if value:
                    self.app.copy_to_clipboard(value)
                    self.notify("Copied to clipboard")
                else:
                    self.notify("Field is empty", severity="warning")
            except Exception:
                pass
        elif btn_id == "save-btn":
            self.action_save()
        elif btn_id == "cancel-btn":
            self.action_cancel()


# ── Shared CSS for all config modals ───────────────────────────────

_BOX_CSS = """
#config-outer {
    width: 95%;
    max-height: 90%;
    border: round $accent;
    background: $surface;
}
#config-form {
    width: 3fr;
    padding: 1 2;
}
#config-help {
    width: 2fr;
    border-left: tall $accent;
    padding: 1 2;
    background: $primary-background;
    content-align: left top;
}
#config-title {
    text-style: bold;
    color: $accent;
    margin: 0 0 1 0;
}
#help-content {
    width: 100%;
    text-align: left;
    color: $text;
    padding: 0 0 1 0;
}
.field-label {
    margin: 1 0 0 0;
    color: $text;
}
.field-hint {
    color: $text-muted;
    text-style: italic;
}
.modal-buttons {
    height: auto;
    margin: 1 0 0 0;
}
.modal-buttons Button {
    margin: 0 1 0 0;
}
.secret-row {
    height: auto;
}
.secret-row Input {
    width: 1fr;
}
.reveal-btn {
    width: 12;
    min-width: 12;
    margin: 0 0 0 1;
}
.copy-btn {
    width: 10;
    min-width: 10;
    margin: 0 0 0 0;
}
Input:focus {
    border: tall $accent;
}
Select:focus {
    border: tall $accent;
}
Switch:focus {
    border: tall $accent;
}
#save-btn:focus {
    background: $success;
    color: $text;
    text-style: bold;
}
"""

_MODAL_ALIGN = """
    align: left middle;
    padding: 0 0 0 2;
"""


# ── Help content for each module ──────────────────────────────────

_PROXMOX_HELP = (
    "[bold cyan]PROXMOX SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]CREATE API TOKEN[/bold cyan]\n\n"
    "  [bold yellow]1[/bold yellow]  Open the Proxmox web UI\n\n"
    "  [bold yellow]2[/bold yellow]  Navigate to:\n"
    "     [reverse] Datacenter → Permissions → API Tokens [/reverse]\n\n"
    "  [bold yellow]3[/bold yellow]  Select your user, click [bold]Add[/bold]\n\n"
    "  [bold yellow]4[/bold yellow]  Token ID:  [bold]infraforge[/bold]\n\n"
    "  [bold yellow]5[/bold yellow]  [bold]Uncheck[/bold] \"Privilege Separation\"\n"
    "     [dim]Grants the token full user permissions[/dim]\n\n"
    "  [bold yellow]6[/bold yellow]  Copy the token value immediately\n"
    "     [dim]Shown only once — cannot be retrieved later[/dim]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]USER FORMAT[/bold cyan]\n\n"
    "  [reverse] root@pam  [/reverse]  Local root account\n"
    "  [reverse] admin@pve [/reverse]  PVE-managed user\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]TOKEN VS PASSWORD[/bold cyan]\n\n"
    "  [yellow]Tokens are recommended because they:[/yellow]\n\n"
    "  [dim]•[/dim] Don't expire with password changes\n"
    "  [dim]•[/dim] Can be revoked independently\n"
    "  [dim]•[/dim] Skip 2FA/TOTP prompts\n"
    "  [dim]•[/dim] Provide better security through scoping\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]SSL VERIFICATION[/bold cyan]\n\n"
    "  [dim]•[/dim] [bold]Disabled[/bold] — for self-signed certs [dim](most homelabs)[/dim]\n"
    "  [dim]•[/dim] [bold]Enabled[/bold]  — only with CA-signed certificates"
)

_DNS_HELP = (
    "[bold cyan]BIND9 DNS SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n"
    "[dim]Run these commands on your DNS server.[/dim]\n\n"
    "[bold yellow]1[/bold yellow]  [bold]Generate TSIG key[/bold]\n\n"
    "   [reverse] $ tsig-keygen infraforge-key \\           [/reverse]\n"
    "   [reverse]     > /etc/bind/infraforge-key.conf       [/reverse]\n"
    "   [reverse] $ chown root:bind \\                       [/reverse]\n"
    "   [reverse]     /etc/bind/infraforge-key.conf          [/reverse]\n"
    "   [reverse] $ chmod 640 /etc/bind/infraforge-key.conf  [/reverse]\n\n"
    "[bold yellow]2[/bold yellow]  [bold]Copy the secret from the key file[/bold]\n\n"
    "   [reverse] $ cat /etc/bind/infraforge-key.conf        [/reverse]\n\n"
    "   [dim]Output looks like:[/dim]\n"
    '   [dim]key "infraforge-key" \\{[/dim]\n'
    "   [dim]    algorithm hmac-sha256;[/dim]\n"
    '   [dim]    secret "R3HI8P6BKw9ZwXw...==";[/dim]\n'
    "   [dim]\\};[/dim]\n\n"
    "   [yellow]![/yellow] Copy the [bold]secret[/bold] value into the field\n"
    "     on the left.\n\n"
    "[bold yellow]3[/bold yellow]  [bold]Enable dynamic updates[/bold]\n\n"
    "   Add to [bold]named.conf.local[/bold]:\n\n"
    '   [reverse] include "/etc/bind/infraforge-key.conf";   [/reverse]\n\n'
    "   In your zone block, add:\n\n"
    '   [reverse] allow-update \\{ key "infraforge-key"; \\};  [/reverse]\n\n'
    "[bold yellow]4[/bold yellow]  [bold]Validate and reload[/bold]\n\n"
    "   [reverse] $ named-checkconf && rndc reload           [/reverse]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]USEFUL COMMANDS[/bold cyan]\n\n"
    "  [dim]•[/dim] List TSIG keys:\n"
    "    [reverse] $ rndc tsig-list [/reverse]\n\n"
    "  [dim]•[/dim] List configured zones:\n"
    "    [reverse] $ grep -oP 'zone \"\\K\\[^\"]+'              [/reverse]\n"
    "    [reverse]   /etc/bind/named.conf.local               [/reverse]\n\n"
    "  [dim]InfraForge auto-discovers zones on first connect.[/dim]"
)

_IPAM_HELP = (
    "[bold cyan]IPAM SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]DOCKER DEPLOYMENT[/bold cyan]\n\n"
    "  Deploys a complete phpIPAM stack locally:\n\n"
    "  [dim]•[/dim] [bold]MariaDB[/bold]        Database backend\n"
    "  [dim]•[/dim] [bold]phpIPAM Web[/bold]    HTTPS web interface\n"
    "  [dim]•[/dim] [bold]Cron Scanner[/bold]   Automatic subnet discovery\n"
    "  [dim]•[/dim] [bold]API App[/bold]        Pre-configured \"infraforge\"\n"
    "  [dim]•[/dim] [bold]SSL Cert[/bold]       Self-signed certificate\n\n"
    "  [yellow]![/yellow] Docker + compose auto-installed if missing.\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]EXISTING SERVER[/bold cyan]\n\n"
    "  To create an API app in your phpIPAM:\n\n"
    "  [bold yellow]1[/bold yellow]  Log in to the phpIPAM web UI\n\n"
    "  [bold yellow]2[/bold yellow]  Navigate to:\n"
    "     [reverse] Administration → API [/reverse]\n\n"
    "  [bold yellow]3[/bold yellow]  Click [bold]Create API key[/bold]\n\n"
    "  [bold yellow]4[/bold yellow]  Configure:\n"
    "     [dim]•[/dim] App ID:       [bold]infraforge[/bold]\n"
    "     [dim]•[/dim] Permissions:  [bold]Read / Write / Admin[/bold]\n"
    "     [dim]•[/dim] Security:     [bold]SSL with User token[/bold]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]AUTHENTICATION[/bold cyan]\n\n"
    "  [dim]•[/dim] [bold]Token auth[/bold] [dim](recommended)[/dim]\n"
    "    App ID + Token — no password needed\n\n"
    "  [dim]•[/dim] [bold]User auth[/bold]\n"
    "    App ID + Username + Password\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]SSL VERIFICATION[/bold cyan]\n\n"
    "  Leave [bold]disabled[/bold] for self-signed certs,\n"
    "  Docker deployments, and internal servers."
)

_TERRAFORM_HELP = (
    "[bold cyan]TERRAFORM SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]HOW IT WORKS[/bold cyan]\n\n"
    "  InfraForge generates Terraform HCL and runs\n"
    "  [bold]init → plan → apply[/bold] to provision VMs\n"
    "  and containers on Proxmox.\n\n"
    "  Provider: [bold]Telmate/proxmox v3.x[/bold]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]WORKSPACE LAYOUT[/bold cyan]\n\n"
    "  [dim]•[/dim] Deployments:\n"
    "    [reverse] terraform/deployments/{host}/ [/reverse]\n\n"
    "  [dim]•[/dim] Saved templates:\n"
    "    [reverse] terraform/templates/{name}.json [/reverse]\n\n"
    "  [dim]Default path is ./terraform relative to[/dim]\n"
    "  [dim]the InfraForge install directory.[/dim]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]STATE BACKEND[/bold cyan]\n\n"
    "  [dim]•[/dim] [bold]Local[/bold] [dim](default)[/dim]\n"
    "    State files in workspace directory\n"
    "    [dim]Simplest — ideal for single-user[/dim]\n\n"
    "  [dim]•[/dim] [bold]S3[/bold]\n"
    "    Remote state in AWS S3 bucket\n"
    "    [dim]For team collaboration[/dim]\n\n"
    "  [dim]•[/dim] [bold]Consul[/bold]\n"
    "    Remote state in HashiCorp Consul\n"
    "    [dim]For full HashiCorp stack[/dim]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]PREREQUISITES[/bold cyan]\n\n"
    "  [dim]•[/dim] Terraform CLI [dim](v1.0+)[/dim]\n"
    "  [dim]•[/dim] Proxmox API token with\n"
    "    VM.Allocate + VM.Config permissions\n"
    "  [dim]•[/dim] SSH public key for VM access\n"
    "    [dim]Scans ~/.ssh/*.pub automatically[/dim]\n\n"
    "  [yellow]![/yellow] [dim]InfraForge auto-installs Terraform if missing.[/dim]"
)

_ANSIBLE_HELP = (
    "[bold cyan]ANSIBLE SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]PLAYBOOK DIRECTORY[/bold cyan]\n\n"
    "  Path where InfraForge discovers playbooks.\n"
    "  Each [bold].yml[/bold] / [bold].yaml[/bold] file appears in the\n"
    "  Ansible screen automatically.\n\n"
    "  Default:\n"
    "  [reverse] ./ansible/playbooks [/reverse]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]INCLUDED PLAYBOOKS[/bold cyan]\n\n"
    "  [dim]•[/dim] [bold]deploy-ssh-key.yml[/bold]\n"
    "    [dim]Roll out SSH keys to target hosts[/dim]\n\n"
    "  [dim]•[/dim] [bold]install-claude-code.yml[/bold]\n"
    "    [dim]Install NVM + Node.js + Claude Code[/dim]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]HOW RUNS WORK[/bold cyan]\n\n"
    "  [dim]•[/dim] Target hosts from IPAM subnet scanning\n"
    "    or manual entry\n"
    "  [dim]•[/dim] SSH credentials configured per run\n"
    "  [dim]•[/dim] Live output streamed to the TUI\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]PREREQUISITES[/bold cyan]\n\n"
    "  [yellow]![/yellow] Ansible must be installed and in PATH.\n\n"
    "  [dim]InfraForge can auto-install it from the\n"
    "  setup screen if missing.[/dim]"
)

_AI_HELP = (
    "[bold cyan]AI COPILOT SETUP[/bold cyan]\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]GET AN API KEY[/bold cyan]\n\n"
    "  [bold yellow]1[/bold yellow]  Go to [bold]console.anthropic.com[/bold]\n\n"
    "  [bold yellow]2[/bold yellow]  Navigate to:\n"
    "     [reverse] Settings → API Keys [/reverse]\n\n"
    "  [bold yellow]3[/bold yellow]  Click [bold]Create Key[/bold]\n\n"
    "  [bold yellow]4[/bold yellow]  Copy the key\n"
    "     [dim]Starts with[/dim] [bold]sk-ant-...[/bold]\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]MODELS[/bold cyan]\n\n"
    "  [dim]•[/dim] [bold]Opus 4.6[/bold]\n"
    "    Most capable — complex analysis\n"
    "    and infrastructure planning\n\n"
    "  [dim]•[/dim] [bold]Sonnet 4.5[/bold] [dim](recommended)[/dim]\n"
    "    Fast and capable — great default\n"
    "    for everyday tasks\n\n"
    "  [dim]•[/dim] [bold]Haiku 4.5[/bold]\n"
    "    Fastest, lowest cost — quick\n"
    "    queries and simple answers\n\n"
    "[dim]───────────────────────────────────────[/dim]\n\n"
    "[bold cyan]WHAT AI CAN DO[/bold cyan]\n\n"
    "  [dim]•[/dim] Analyze VM configurations\n"
    "  [dim]•[/dim] Suggest infrastructure improvements\n"
    "  [dim]•[/dim] Help troubleshoot issues\n"
    "  [dim]•[/dim] Generate Terraform / Ansible configs\n"
    "  [dim]•[/dim] Answer questions about your cluster\n\n"
    "  [yellow]![/yellow] Press [bold]/[/bold] on the dashboard to open the\n"
    "    AI chat panel."
)


# ── Helper ────────────────────────────────────────────────────────

def get_config_modal(comp_id: str, full_cfg: dict) -> ModalScreen | None:
    """Return the appropriate config modal for a component."""
    section = dict(full_cfg.get(comp_id, {}))  # shallow copy
    modals = {
        "proxmox": ProxmoxConfigModal,
        "dns": DNSConfigModal,
        "ipam": IPAMConfigModal,
        "terraform": TerraformConfigModal,
        "ansible": AnsibleConfigModal,
        "ai": AIConfigModal,
    }
    cls = modals.get(comp_id)
    if cls is None:
        return None
    return cls(section)


# ── Proxmox Config Modal ──────────────────────────────────────────

class ProxmoxConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
ProxmoxConfigModal {{
{_MODAL_ALIGN}
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]Proxmox Configuration[/bold]", id="config-title", markup=True)

                yield Label("Host [dim](IP or hostname)[/dim]", classes="field-label", markup=True)
                yield Input(value=s.get("host", ""), placeholder="e.g. 10.0.200.1", id="f-host")

                yield Label("Port", classes="field-label")
                yield Input(value=str(s.get("port", 8006)), placeholder="8006", id="f-port")

                yield Label("User", classes="field-label")
                yield Input(value=s.get("user", "root@pam"), placeholder="root@pam", id="f-user")

                yield Label("Auth Method", classes="field-label")
                yield Select(
                    [("API Token (recommended)", "token"), ("Password", "password")],
                    value=s.get("auth_method", "token"),
                    id="f-auth-method",
                )

                yield Label("Token Name", classes="field-label", id="lbl-token-name")
                yield Input(value=s.get("token_name", ""), placeholder="e.g. infraforge", id="f-token-name")

                yield Label("Token Value", classes="field-label", id="lbl-token-value")
                with Horizontal(classes="secret-row", id="row-token-value"):
                    yield Input(value=s.get("token_value", ""), placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", id="f-token-value", password=True)
                    yield Button("Reveal", id="reveal-f-token-value", classes="reveal-btn")
                    yield Button("Copy", id="copy-f-token-value", classes="copy-btn")

                yield Label("Password", classes="field-label", id="lbl-password")
                with Horizontal(classes="secret-row", id="row-password"):
                    yield Input(value=s.get("password", ""), placeholder="", id="f-password", password=True)
                    yield Button("Reveal", id="reveal-f-password", classes="reveal-btn")
                    yield Button("Copy", id="copy-f-password", classes="copy-btn")

                yield Label("Verify SSL", classes="field-label")
                yield Switch(value=s.get("verify_ssl", False), id="f-verify-ssl")

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_PROXMOX_HELP, id="help-content", markup=True)

    def on_mount(self) -> None:
        super().on_mount()
        self._toggle_auth_fields()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-auth-method":
            self._toggle_auth_fields()

    def _toggle_auth_fields(self) -> None:
        auth = self.query_one("#f-auth-method", Select).value
        is_token = auth == "token"
        # Token fields
        self.query_one("#f-token-name", Input).display = is_token
        self.query_one("#lbl-token-name").display = is_token
        self.query_one("#row-token-value").display = is_token
        self.query_one("#lbl-token-value").display = is_token
        # Password field
        self.query_one("#row-password").display = not is_token
        self.query_one("#lbl-password").display = not is_token

    def action_save(self) -> None:
        host = self.query_one("#f-host", Input).value.strip()
        if not host:
            self.notify("Host is required!", severity="error")
            return
        result = {
            "host": host,
            "port": int(self.query_one("#f-port", Input).value.strip() or 8006),
            "user": self.query_one("#f-user", Input).value.strip() or "root@pam",
            "auth_method": self.query_one("#f-auth-method", Select).value,
            "token_name": self.query_one("#f-token-name", Input).value.strip(),
            "token_value": self.query_one("#f-token-value", Input).value.strip(),
            "password": self.query_one("#f-password", Input).value.strip(),
            "verify_ssl": self.query_one("#f-verify-ssl", Switch).value,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── DNS Config Modal ───────────────────────────────────────────────

class DNSConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
DNSConfigModal {{
{_MODAL_ALIGN}
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        zones = s.get("zones", [])
        if not zones and s.get("zone"):
            zones = [s["zone"]]
        zones_str = ", ".join(zones) if zones else ""

        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]DNS Configuration[/bold]", id="config-title", markup=True)

                yield Label("Provider", classes="field-label")
                yield Select(
                    [("BIND9", "bind9"), ("Cloudflare", "cloudflare"), ("Route53", "route53"), ("Custom", "custom")],
                    value=s.get("provider", "bind9"),
                    id="f-provider",
                )

                yield Label("Server [dim](BIND9 IP/hostname)[/dim]", classes="field-label", markup=True)
                yield Input(value=s.get("server", ""), placeholder="e.g. 10.0.200.2", id="f-server")

                yield Label("Port", classes="field-label")
                yield Input(value=str(s.get("port", 53)), placeholder="53", id="f-port")

                yield Label("Domain [dim](default FQDN domain)[/dim]", classes="field-label", markup=True)
                yield Input(value=s.get("domain", ""), placeholder="e.g. lab.local", id="f-domain")

                yield Label("Zones [dim](comma-separated)[/dim]", classes="field-label", markup=True)
                yield Input(value=zones_str, placeholder="e.g. lab.local, dev.local", id="f-zones")

                yield Label("TSIG Key Name", classes="field-label")
                yield Input(value=s.get("tsig_key_name", ""), placeholder="e.g. infraforge-key", id="f-tsig-name")

                yield Label("TSIG Key Secret [dim](base64 from key file)[/dim]", classes="field-label", markup=True)
                with Horizontal(classes="secret-row"):
                    yield Input(value=s.get("tsig_key_secret", ""), placeholder="base64 secret", id="f-tsig-secret", password=True)
                    yield Button("Reveal", id="reveal-f-tsig-secret", classes="reveal-btn")
                    yield Button("Copy", id="copy-f-tsig-secret", classes="copy-btn")

                yield Label("TSIG Algorithm", classes="field-label")
                yield Select(
                    [("hmac-sha256", "hmac-sha256"), ("hmac-sha512", "hmac-sha512"), ("hmac-md5", "hmac-md5")],
                    value=s.get("tsig_algorithm", "hmac-sha256"),
                    id="f-tsig-algo",
                )

                yield Label("API Key [dim](Cloudflare/Route53)[/dim]", classes="field-label", markup=True)
                with Horizontal(classes="secret-row"):
                    yield Input(value=s.get("api_key", ""), placeholder="API key", id="f-api-key", password=True)
                    yield Button("Reveal", id="reveal-f-api-key", classes="reveal-btn")
                    yield Button("Copy", id="copy-f-api-key", classes="copy-btn")

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_DNS_HELP, id="help-content", markup=True)

    def action_save(self) -> None:
        zones_raw = self.query_one("#f-zones", Input).value.strip()
        zones = [z.strip() for z in zones_raw.split(",") if z.strip()] if zones_raw else []
        result = {
            "provider": self.query_one("#f-provider", Select).value,
            "server": self.query_one("#f-server", Input).value.strip(),
            "port": int(self.query_one("#f-port", Input).value.strip() or 53),
            "domain": self.query_one("#f-domain", Input).value.strip(),
            "zones": zones,
            "tsig_key_name": self.query_one("#f-tsig-name", Input).value.strip(),
            "tsig_key_secret": self.query_one("#f-tsig-secret", Input).value.strip(),
            "tsig_algorithm": self.query_one("#f-tsig-algo", Select).value,
            "api_key": self.query_one("#f-api-key", Input).value.strip(),
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── IPAM Config Modal ──────────────────────────────────────────────

class IPAMConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
IPAMConfigModal {{
{_MODAL_ALIGN}
}}
#ipam-docker-fields, #ipam-existing-fields {{
    height: auto;
}}
#docker-status {{
    height: auto;
    margin: 1 0 0 0;
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section
        self._deploying = False

    def compose(self) -> ComposeResult:
        import secrets
        import string

        s = self._sec
        default_method = "existing" if s.get("url") else "docker"
        alphabet = string.ascii_lowercase + string.digits
        default_pass = "".join(secrets.choice(alphabet) for _ in range(20))

        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]IPAM Configuration[/bold]  [dim](phpIPAM)[/dim]", id="config-title", markup=True)

                yield Label("Setup Method", classes="field-label")
                yield Select(
                    [
                        ("Deploy phpIPAM with Docker (recommended)", "docker"),
                        ("Connect to existing phpIPAM server", "existing"),
                    ],
                    value=default_method,
                    id="f-ipam-method",
                )

                # ── Docker deployment fields ──
                with Vertical(id="ipam-docker-fields"):
                    yield Static(
                        "[dim]Deploys a local phpIPAM instance with MariaDB, "
                        "auto-configured API, and self-signed SSL.[/dim]",
                        markup=True,
                        classes="field-hint",
                    )
                    yield Label("HTTPS Port", classes="field-label")
                    yield Input(value="8443", placeholder="8443", id="f-docker-port")
                    yield Label("Admin Password", classes="field-label")
                    with Horizontal(classes="secret-row"):
                        yield Input(value=default_pass, placeholder="auto-generated", id="f-docker-pass", password=True)
                        yield Button("Reveal", id="reveal-f-docker-pass", classes="reveal-btn")
                        yield Button("Copy", id="copy-f-docker-pass", classes="copy-btn")
                    yield Static("", id="docker-status", markup=True)

                # ── Existing server fields ──
                with Vertical(id="ipam-existing-fields"):
                    yield Label("URL", classes="field-label")
                    yield Input(value=s.get("url", ""), placeholder="e.g. https://ipam.example.com", id="f-url")

                    yield Label("App ID", classes="field-label")
                    yield Input(value=s.get("app_id", "infraforge"), placeholder="infraforge", id="f-app-id")

                    yield Label("Token [dim](if token auth)[/dim]", classes="field-label", markup=True)
                    with Horizontal(classes="secret-row"):
                        yield Input(value=s.get("token", ""), placeholder="API token", id="f-token", password=True)
                        yield Button("Reveal", id="reveal-f-token", classes="reveal-btn")
                        yield Button("Copy", id="copy-f-token", classes="copy-btn")

                    yield Label("Username [dim](if user auth)[/dim]", classes="field-label", markup=True)
                    yield Input(value=s.get("username", ""), placeholder="admin", id="f-username")

                    yield Label("Password [dim](if user auth)[/dim]", classes="field-label", markup=True)
                    with Horizontal(classes="secret-row"):
                        yield Input(value=s.get("password", ""), placeholder="", id="f-password", password=True)
                        yield Button("Reveal", id="reveal-f-password", classes="reveal-btn")
                        yield Button("Copy", id="copy-f-password", classes="copy-btn")

                    yield Label("Verify SSL", classes="field-label")
                    yield Switch(value=s.get("verify_ssl", False), id="f-verify-ssl")

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_IPAM_HELP, id="help-content", markup=True)

    def on_mount(self) -> None:
        super().on_mount()
        self._toggle_method_fields()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-ipam-method":
            self._toggle_method_fields()

    def _toggle_method_fields(self) -> None:
        method = self.query_one("#f-ipam-method", Select).value
        is_docker = method == "docker"
        self.query_one("#ipam-docker-fields").display = is_docker
        self.query_one("#ipam-existing-fields").display = not is_docker

    def _set_status(self, msg: str) -> None:
        self.query_one("#docker-status", Static).update(msg)

    def action_save(self) -> None:
        method = self.query_one("#f-ipam-method", Select).value
        if method == "docker":
            if self._deploying:
                return
            port = self.query_one("#f-docker-port", Input).value.strip() or "8443"
            admin_pass = self.query_one("#f-docker-pass", Input).value.strip()
            if not admin_pass:
                import secrets, string
                alphabet = string.ascii_lowercase + string.digits
                admin_pass = "".join(secrets.choice(alphabet) for _ in range(20))
            self._deploy_docker(port, admin_pass)
        else:
            url = self.query_one("#f-url", Input).value.strip()
            if not url:
                self.notify("URL is required!", severity="error")
                return
            result = {
                "provider": "phpipam",
                "url": url,
                "app_id": self.query_one("#f-app-id", Input).value.strip() or "infraforge",
                "token": self.query_one("#f-token", Input).value.strip(),
                "username": self.query_one("#f-username", Input).value.strip(),
                "password": self.query_one("#f-password", Input).value.strip(),
                "verify_ssl": self.query_one("#f-verify-ssl", Switch).value,
            }
            self.dismiss(result)

    @work(thread=True)
    def _deploy_docker(self, port: str, admin_pass: str) -> None:
        """Deploy phpIPAM Docker stack in a background thread."""
        import secrets
        import subprocess
        import time
        from pathlib import Path

        self._deploying = True
        docker_dir = Path(__file__).resolve().parent.parent.parent / "docker"

        def status(msg: str) -> None:
            self.app.call_from_thread(self._set_status, f"[bold cyan]{msg}[/bold cyan]")

        def fail(msg: str) -> None:
            self.app.call_from_thread(self._set_status, f"[bold red]{msg}[/bold red]")
            self._deploying = False

        import os
        import shutil
        import tempfile
        import urllib.request as urlreq

        sudo = ["sudo"] if os.geteuid() != 0 else []
        has_apt = shutil.which("apt-get") is not None

        # ── Step 1: Ensure Docker is installed ──
        status("Checking Docker...")
        docker_ok = False
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            docker_ok = r.returncode == 0
        except FileNotFoundError:
            pass

        if not docker_ok:
            if not has_apt:
                fail("Docker is not installed and apt is not available.\nInstall Docker manually and retry.")
                return
            status("Installing Docker...")
            try:
                subprocess.run(sudo + ["apt-get", "update", "-qq"], capture_output=True, timeout=120)
                r = subprocess.run(
                    sudo + ["apt-get", "install", "-y", "docker.io"],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0:
                    fail(f"Failed to install Docker:\n[dim]{r.stderr.strip()[:200]}[/dim]")
                    return
                subprocess.run(sudo + ["systemctl", "start", "docker"], capture_output=True, timeout=30)
                subprocess.run(sudo + ["systemctl", "enable", "docker"], capture_output=True, timeout=30)
            except Exception as e:
                fail(f"Failed to install Docker: {e}")
                return

            # Verify it works now
            try:
                r = subprocess.run(sudo + ["docker", "info"], capture_output=True, timeout=10)
                if r.returncode != 0:
                    fail("Docker installed but daemon not responding.\n[dim]Try: sudo systemctl start docker[/dim]")
                    return
            except Exception:
                fail("Docker installed but not accessible.")
                return

        # If docker requires sudo, prefix all docker commands
        docker_prefix: list[str] = []
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            if r.returncode != 0:
                docker_prefix = sudo
        except Exception:
            docker_prefix = sudo

        # ── Step 2: Ensure docker compose is available ──
        compose_cmd: list[str] | None = None
        for candidate in [docker_prefix + ["docker", "compose"], ["docker-compose"]]:
            try:
                if subprocess.run(candidate + ["version"], capture_output=True, timeout=5).returncode == 0:
                    compose_cmd = candidate
                    break
            except Exception:
                continue

        if not compose_cmd:
            status("Installing docker compose v2 plugin...")
            try:
                arch = subprocess.run(
                    ["uname", "-m"], capture_output=True, text=True, timeout=5,
                ).stdout.strip() or "x86_64"
                compose_url = (
                    f"https://github.com/docker/compose/releases/latest/download"
                    f"/docker-compose-linux-{arch}"
                )
                plugin_dir = "/usr/local/lib/docker/cli-plugins"
                plugin_path = f"{plugin_dir}/docker-compose"
                subprocess.run(sudo + ["mkdir", "-p", plugin_dir], capture_output=True, timeout=10)

                # Download to temp then move (avoids permission issues)
                tmp = tempfile.mktemp(prefix="docker-compose-")
                urlreq.urlretrieve(compose_url, tmp)
                subprocess.run(sudo + ["mv", tmp, plugin_path], capture_output=True, timeout=10)
                subprocess.run(sudo + ["chmod", "+x", plugin_path], capture_output=True, timeout=10)
            except Exception as e:
                fail(f"Failed to install docker compose:\n[dim]{e}[/dim]")
                return

            # Verify
            for candidate in [docker_prefix + ["docker", "compose"], ["docker-compose"]]:
                try:
                    if subprocess.run(candidate + ["version"], capture_output=True, timeout=5).returncode == 0:
                        compose_cmd = candidate
                        break
                except Exception:
                    continue
            if not compose_cmd:
                fail("docker compose installed but not working.")
                return

        # ── Step 3: Check if already running ──
        try:
            r = subprocess.run(
                docker_prefix + ["docker", "inspect", "--format", "{{.State.Running}}", "infraforge-ipam-web"],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip() == "true":
                status("phpIPAM containers already running — using existing deployment.")
                existing_port = port
                env_file = docker_dir / ".env"
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if line.startswith("IPAM_PORT="):
                            existing_port = line.split("=", 1)[1].strip()
                time.sleep(1)
                self._deploying = False
                self.app.call_from_thread(self.dismiss, {
                    "provider": "phpipam",
                    "url": f"https://localhost:{existing_port}",
                    "app_id": "infraforge",
                    "token": "",
                    "username": "Admin",
                    "password": admin_pass,
                    "verify_ssl": False,
                })
                return
        except Exception:
            pass

        # ── Step 4: Generate SSL certs ──
        status("Generating SSL certificate...")
        ssl_script = docker_dir / "phpipam" / "generate-ssl.sh"
        if ssl_script.exists():
            subprocess.run(
                ["bash", str(ssl_script)],
                cwd=str(docker_dir / "phpipam"),
                capture_output=True, timeout=15,
            )

        # ── Step 5: Generate passwords + admin hash + write .env ──
        status("Generating credentials...")
        db_pass = secrets.token_urlsafe(16)
        db_root_pass = secrets.token_urlsafe(16)

        admin_hash = ""
        escaped_pass = admin_pass.replace("'", "\\'")
        php_code = f"echo password_hash('{escaped_pass}', PASSWORD_DEFAULT);"
        for php_cmd in [
            docker_prefix + ["docker", "run", "--rm", "php:cli", "php", "-r", php_code],
            docker_prefix + ["docker", "run", "--rm", "phpipam/phpipam-www:latest", "php", "-r", php_code],
        ]:
            try:
                r = subprocess.run(php_cmd, capture_output=True, text=True, timeout=60)
                if r.returncode == 0 and r.stdout.strip().startswith("$2"):
                    admin_hash = r.stdout.strip()
                    break
            except Exception:
                continue

        env_lines = [
            f"IPAM_DB_ROOT_PASS={db_root_pass}",
            f"IPAM_DB_PASS={db_pass}",
            f"IPAM_PORT={port}",
            "SCAN_INTERVAL=15m",
        ]
        if admin_hash:
            env_lines.append(f"IPAM_ADMIN_HASH={admin_hash.replace('$', '$$')}")
        (docker_dir / ".env").write_text("\n".join(env_lines) + "\n")

        # ── Step 6: Launch containers ──
        status("Starting containers...")
        r = subprocess.run(
            compose_cmd + ["-f", str(docker_dir / "docker-compose.yml"), "up", "-d"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            err = r.stderr.strip()[:200]
            fail(f"Failed to start containers:\n[dim]{err}[/dim]")
            return

        # ── Step 7: Wait for readiness ──
        status("Waiting for phpIPAM to start (may take 30-60s)...")
        import ssl as ssl_mod
        import urllib.request

        url = f"https://localhost:{port}"
        ready = False
        for _ in range(60):
            try:
                ctx = ssl_mod.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl_mod.CERT_NONE
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                    if resp.status in (200, 301, 302):
                        time.sleep(5)
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(3)

        if not ready:
            fail("phpIPAM did not become ready in time.\n[dim]Check: docker logs infraforge-ipam-web[/dim]")
            return

        # ── Step 8: Verify API ──
        status("Verifying API connectivity...")
        api_ok = False
        for _ in range(5):
            try:
                from infraforge.config import Config, IPAMConfig
                from infraforge.ipam_client import IPAMClient

                cfg = Config()
                cfg.ipam = IPAMConfig(
                    provider="phpipam", url=url, app_id="infraforge",
                    token="", username="Admin", password=admin_pass,
                    verify_ssl=False,
                )
                client = IPAMClient(cfg)
                if client.check_health():
                    api_ok = True
                    break
            except Exception:
                pass
            time.sleep(3)

        actual_pass = admin_pass
        if not api_ok and not admin_hash:
            self.app.call_from_thread(
                self.notify,
                "Could not verify admin password — check phpIPAM web UI to set it",
                severity="warning",
            )

        self.app.call_from_thread(
            self._set_status,
            f"[bold green]phpIPAM deployed at {url}[/bold green]\n"
            f"[dim]Web UI: {url}  (Admin / {actual_pass})[/dim]",
        )

        result = {
            "provider": "phpipam",
            "url": url,
            "app_id": "infraforge",
            "token": "",
            "username": "Admin",
            "password": actual_pass,
            "verify_ssl": False,
        }
        self._deploying = False
        time.sleep(2)
        self.app.call_from_thread(self.dismiss, result)

    def action_cancel(self) -> None:
        if self._deploying:
            self.notify("Deployment in progress — please wait", severity="warning")
            return
        self.dismiss(None)


# ── Terraform Config Modal ─────────────────────────────────────────

class TerraformConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
TerraformConfigModal {{
{_MODAL_ALIGN}
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]Terraform Configuration[/bold]", id="config-title", markup=True)

                yield Label("Workspace Directory", classes="field-label")
                yield Input(value=s.get("workspace", "./terraform"), placeholder="./terraform", id="f-workspace")

                yield Label("State Backend", classes="field-label")
                yield Select(
                    [("Local", "local"), ("S3", "s3"), ("Consul", "consul")],
                    value=s.get("state_backend", "local"),
                    id="f-backend",
                )

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_TERRAFORM_HELP, id="help-content", markup=True)

    def action_save(self) -> None:
        result = {
            "workspace": self.query_one("#f-workspace", Input).value.strip() or "./terraform",
            "state_backend": self.query_one("#f-backend", Select).value,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Ansible Config Modal ──────────────────────────────────────────

class AnsibleConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
AnsibleConfigModal {{
{_MODAL_ALIGN}
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]Ansible Configuration[/bold]", id="config-title", markup=True)

                yield Label("Playbook Directory", classes="field-label")
                yield Input(value=s.get("playbook_dir", "./ansible/playbooks"), placeholder="./ansible/playbooks", id="f-playbook-dir")

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_ANSIBLE_HELP, id="help-content", markup=True)

    def action_save(self) -> None:
        result = {
            "playbook_dir": self.query_one("#f-playbook-dir", Input).value.strip() or "./ansible/playbooks",
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── AI Config Modal ────────────────────────────────────────────────

class AIConfigModal(_ArrowNavModal):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = f"""
AIConfigModal {{
{_MODAL_ALIGN}
}}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with Horizontal(id="config-outer"):
            with VerticalScroll(id="config-form"):
                yield Static("[bold]AI Configuration[/bold]  [dim](Anthropic)[/dim]", id="config-title", markup=True)

                yield Label("API Key", classes="field-label")
                with Horizontal(classes="secret-row"):
                    yield Input(value=s.get("api_key", ""), placeholder="sk-ant-api03-...", id="f-api-key", password=True)
                    yield Button("Reveal", id="reveal-f-api-key", classes="reveal-btn")
                    yield Button("Copy", id="copy-f-api-key", classes="copy-btn")

                yield Label("Model", classes="field-label")
                yield Select(
                    [
                        ("Claude Opus 4.6", "claude-opus-4-6"),
                        ("Claude Sonnet 4.5", "claude-sonnet-4-5-20250929"),
                        ("Claude Haiku 4.5", "claude-haiku-4-5-20251001"),
                    ],
                    value=s.get("model", "claude-sonnet-4-5-20250929"),
                    id="f-model",
                )

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Cancel", id="cancel-btn")

            with VerticalScroll(id="config-help"):
                yield Static(_AI_HELP, id="help-content", markup=True)

    def action_save(self) -> None:
        key = self.query_one("#f-api-key", Input).value.strip()
        result = {
            "provider": "anthropic",
            "api_key": key,
            "model": self.query_one("#f-model", Select).value,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)
