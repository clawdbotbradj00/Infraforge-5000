"""Configuration modals for the InfraForge setup wizard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Select, Static, Switch
from textual import on


# ── Shared CSS for all config modals ───────────────────────────────

_BOX_CSS = """
#config-box {
    width: 75;
    max-height: 85%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
#config-title {
    text-style: bold;
    color: $accent;
    margin: 0 0 1 0;
}
.field-label {
    margin: 1 0 0 0;
    color: $text;
}
.field-hint {
    color: $text-muted;
    text-style: italic;
}
.modal-hint {
    margin: 1 0 0 0;
    color: $text-muted;
}
"""


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

class ProxmoxConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
ProxmoxConfigModal {
    align: center middle;
}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with VerticalScroll(id="config-box"):
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

            yield Label("Token Name [dim](if token auth)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("token_name", ""), placeholder="e.g. infraforge", id="f-token-name")

            yield Label("Token Value [dim](secret)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("token_value", ""), placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", id="f-token-value", password=True)

            yield Label("Password [dim](if password auth)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("password", ""), placeholder="", id="f-password", password=True)

            yield Label("Verify SSL", classes="field-label")
            yield Switch(value=s.get("verify_ssl", False), id="f-verify-ssl")

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

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

class DNSConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
DNSConfigModal {
    align: center middle;
}
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

        with VerticalScroll(id="config-box"):
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
            yield Input(value=s.get("tsig_key_name", ""), placeholder="e.g. api-control", id="f-tsig-name")

            yield Label("TSIG Key Secret", classes="field-label")
            yield Input(value=s.get("tsig_key_secret", ""), placeholder="base64 secret", id="f-tsig-secret", password=True)

            yield Label("TSIG Algorithm", classes="field-label")
            yield Select(
                [("hmac-sha256", "hmac-sha256"), ("hmac-sha512", "hmac-sha512"), ("hmac-md5", "hmac-md5")],
                value=s.get("tsig_algorithm", "hmac-sha256"),
                id="f-tsig-algo",
            )

            yield Label("API Key [dim](Cloudflare/Route53)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("api_key", ""), placeholder="API key", id="f-api-key", password=True)

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

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

class IPAMConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
IPAMConfigModal {
    align: center middle;
}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with VerticalScroll(id="config-box"):
            yield Static("[bold]IPAM Configuration[/bold]  [dim](phpIPAM)[/dim]", id="config-title", markup=True)

            yield Label("URL", classes="field-label")
            yield Input(value=s.get("url", ""), placeholder="e.g. https://ipam.example.com", id="f-url")

            yield Label("App ID", classes="field-label")
            yield Input(value=s.get("app_id", "infraforge"), placeholder="infraforge", id="f-app-id")

            yield Label("Token [dim](if token auth)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("token", ""), placeholder="API token", id="f-token", password=True)

            yield Label("Username [dim](if user auth)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("username", ""), placeholder="admin", id="f-username")

            yield Label("Password [dim](if user auth)[/dim]", classes="field-label", markup=True)
            yield Input(value=s.get("password", ""), placeholder="", id="f-password", password=True)

            yield Label("Verify SSL", classes="field-label")
            yield Switch(value=s.get("verify_ssl", False), id="f-verify-ssl")

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

    def action_save(self) -> None:
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

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Terraform Config Modal ─────────────────────────────────────────

class TerraformConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
TerraformConfigModal {
    align: center middle;
}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with VerticalScroll(id="config-box"):
            yield Static("[bold]Terraform Configuration[/bold]", id="config-title", markup=True)

            yield Label("Workspace Directory", classes="field-label")
            yield Input(value=s.get("workspace", "./terraform"), placeholder="./terraform", id="f-workspace")

            yield Label("State Backend", classes="field-label")
            yield Select(
                [("Local", "local"), ("S3", "s3"), ("Consul", "consul")],
                value=s.get("state_backend", "local"),
                id="f-backend",
            )

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

    def action_save(self) -> None:
        result = {
            "workspace": self.query_one("#f-workspace", Input).value.strip() or "./terraform",
            "state_backend": self.query_one("#f-backend", Select).value,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Ansible Config Modal ──────────────────────────────────────────

class AnsibleConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
AnsibleConfigModal {
    align: center middle;
}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with VerticalScroll(id="config-box"):
            yield Static("[bold]Ansible Configuration[/bold]", id="config-title", markup=True)

            yield Label("Playbook Directory", classes="field-label")
            yield Input(value=s.get("playbook_dir", "./ansible/playbooks"), placeholder="./ansible/playbooks", id="f-playbook-dir")

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

    def action_save(self) -> None:
        result = {
            "playbook_dir": self.query_one("#f-playbook-dir", Input).value.strip() or "./ansible/playbooks",
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── AI Config Modal ────────────────────────────────────────────────

class AIConfigModal(ModalScreen):

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
AIConfigModal {
    align: center middle;
}
""" + _BOX_CSS

    def __init__(self, section: dict) -> None:
        super().__init__()
        self._sec = section

    def compose(self) -> ComposeResult:
        s = self._sec
        with VerticalScroll(id="config-box"):
            yield Static("[bold]AI Configuration[/bold]  [dim](Anthropic)[/dim]", id="config-title", markup=True)

            yield Label("API Key", classes="field-label")
            yield Input(value=s.get("api_key", ""), placeholder="sk-ant-api03-...", id="f-api-key", password=True)

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

            yield Static(
                "[bold white on dark_green] Ctrl+S [/bold white on dark_green] Save    "
                "[bold white on dark_red] Esc [/bold white on dark_red] Cancel",
                classes="modal-hint",
                markup=True,
            )

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
