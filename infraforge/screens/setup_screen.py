"""Textual-based setup wizard for InfraForge."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen, ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListView, ListItem, Select, Static, Switch
from textual import work

# Config path
CONFIG_DIR = Path.home() / ".config" / "infraforge"
CONFIG_PATH = CONFIG_DIR / "config.yaml"


def _load_existing_config() -> dict:
    """Load existing config.yaml as raw dict, or return empty dict."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
    return {}


def _save_config(data: dict) -> None:
    """Write config dict to config.yaml with restricted permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp, 0o600)
    tmp.rename(CONFIG_PATH)


def _check_component(cfg: dict, name: str) -> tuple[bool, str]:
    """Check if a component is configured. Returns (ok, info_text)."""
    if name == "proxmox":
        sec = cfg.get("proxmox", {})
        host = sec.get("host", "")
        if host:
            port = sec.get("port", 8006)
            user = sec.get("user", "root@pam")
            auth = "token" if sec.get("token_name") else ("password" if sec.get("password") else "no auth")
            return True, f"{host}:{port}  ({auth})"
        return False, "Not configured"

    elif name == "dns":
        sec = cfg.get("dns", {})
        provider = sec.get("provider", "")
        if not provider:
            return False, "Not configured"
        server = sec.get("server", "")
        zones = sec.get("zones", [])
        if not zones and sec.get("zone"):
            zones = [sec["zone"]]
        zone_str = f"{len(zones)} zone{'s' if len(zones) != 1 else ''}" if zones else "no zones"
        if provider == "bind9" and server:
            return True, f"BIND9 @ {server}  ({zone_str})"
        return True, f"{provider}  ({zone_str})"

    elif name == "ipam":
        sec = cfg.get("ipam", {})
        url = sec.get("url", "")
        if url:
            app_id = sec.get("app_id", "infraforge")
            return True, f"{url}  (app: {app_id})"
        return False, "Not configured"

    elif name == "terraform":
        sec = cfg.get("terraform", {})
        workspace = sec.get("workspace", "")
        has_binary = shutil.which("terraform") is not None
        configured = bool(workspace or sec.get("state_backend"))
        if has_binary and configured:
            return True, f"Installed  (workspace: {workspace or './terraform'})"
        elif configured:
            return True, f"Configured  (workspace: {workspace or './terraform'})  [dim]binary not in PATH[/dim]"
        elif has_binary:
            return True, f"Installed  (workspace: {workspace or './terraform'})"
        return False, "Not configured"

    elif name == "ansible":
        sec = cfg.get("ansible", {})
        pdir = sec.get("playbook_dir", "")
        has_binary = shutil.which("ansible") is not None
        configured = bool(pdir)
        if has_binary and configured:
            return True, f"Installed  (playbooks: {pdir})"
        elif configured:
            return True, f"Configured  (playbooks: {pdir})  [dim]binary not in PATH[/dim]"
        elif has_binary:
            return True, f"Installed  (playbooks: {pdir or './ansible/playbooks'})"
        return False, "Not configured"

    elif name == "ai":
        sec = cfg.get("ai", {})
        key = sec.get("api_key", "")
        if key:
            model = sec.get("model", "unknown")
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            return True, f"{model}  (key: {masked})"
        return False, "Not configured"

    return False, "Unknown"


# ── Components ─────────────────────────────────────────────────────

COMPONENTS = [
    ("proxmox", "Proxmox", "Hypervisor connection (required)"),
    ("dns", "DNS", "DNS record management"),
    ("ipam", "IPAM", "IP address management"),
    ("terraform", "Terraform", "Infrastructure provisioning"),
    ("ansible", "Ansible", "Configuration management"),
    ("ai", "AI", "AI assistant integration"),
]


def _format_row(index: int, comp_name: str, comp_desc: str, ok: bool, info: str) -> str:
    """Build the Rich markup for a single component row."""
    icon = "[bold green]\u2713[/bold green]" if ok else "[bold red]\u2717[/bold red]"
    return (
        f" {icon}  [bold]{index}.[/bold]  "
        f"[bold]{comp_name:<12}[/bold] [dim]{comp_desc:<32}[/dim] {info}"
    )


# ── Confirmation Modal ─────────────────────────────────────────────

class ConfirmConfigModal(ModalScreen):
    """Quick yes/no before entering a config modal."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmConfigModal {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, comp_name: str) -> None:
        super().__init__()
        self._comp_name = comp_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(
                f"[bold]Configure {self._comp_name}?[/bold]\n\n"
                f"This will open the setup form for [cyan]{self._comp_name}[/cyan].\n\n"
                f"  [bold white on dark_green] y [/bold white on dark_green] Configure    "
                f"[bold white on dark_red] n [/bold white on dark_red] Cancel",
                markup=True,
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── Test Result Modal ──────────────────────────────────────────────

class TestResultModal(ModalScreen):
    """Shows live-test results."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("enter", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TestResultModal {
        align: center middle;
    }
    #test-result-box {
        width: 70;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="test-result-box"):
            yield Static(
                f"[bold]{self._title}[/bold]\n\n{self._body}\n\n"
                f"[dim]Press Enter or Escape to close[/dim]",
                markup=True,
            )

    def action_close(self) -> None:
        self.dismiss(None)


# ── Dependency Install Modal ──────────────────────────────────────

class InstallDependencyModal(ModalScreen):
    """Offers to auto-install a missing dependency with live progress."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    InstallDependencyModal {
        align: center middle;
    }
    #install-box {
        width: 80;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
        overflow-x: hidden;
        overflow-y: hidden;
    }
    """

    def __init__(self, dep_name: str) -> None:
        super().__init__()
        self._dep_name = dep_name
        self._phase = "offer"  # offer | installing | done

    def compose(self) -> ComposeResult:
        with Vertical(id="install-box"):
            yield Static(id="install-status", markup=True)

    def on_mount(self) -> None:
        self.query_one("#install-status", Static).update(
            f"[bold yellow]{self._dep_name} is not installed[/bold yellow]\n\n"
            f"Would you like to install it now?\n\n"
            f"  [bold white on dark_green] y [/bold white on dark_green] Install now    "
            f"  [bold white on dark_red] n [/bold white on dark_red] Skip"
        )

    def on_key(self, event) -> None:
        if self._phase == "offer":
            if event.key == "y":
                event.stop()
                self._phase = "installing"
                if self._dep_name == "Terraform":
                    self._install_terraform()
                elif self._dep_name == "Ansible":
                    self._install_ansible()
            elif event.key == "n":
                event.stop()
                self.dismiss(False)
        elif self._phase == "installing":
            event.stop()
        elif self._phase == "done":
            if event.key in ("enter", "escape"):
                event.stop()
                self.dismiss(self._success)

    def action_cancel(self) -> None:
        if self._phase != "installing":
            self.dismiss(False)

    def _update(self, msg: str) -> None:
        self.app.call_from_thread(
            self.query_one("#install-status", Static).update, msg
        )

    @work(thread=True, exclusive=True)
    def _install_terraform(self) -> None:
        import subprocess
        from rich.markup import escape

        steps = [
            ("Adding HashiCorp GPG key...",
             "wget -qO- https://apt.releases.hashicorp.com/gpg "
             "| sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/hashicorp.gpg"),
            ("Adding APT repository...",
             'echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] '
             'https://apt.releases.hashicorp.com $(lsb_release -cs) main" '
             '| sudo tee /etc/apt/sources.list.d/hashicorp.list >/dev/null'),
            ("Updating package index...",
             "sudo apt-get update -qq"),
            ("Installing terraform...",
             "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq terraform"),
        ]

        for i, (msg, cmd) in enumerate(steps, 1):
            self._update(
                f"[bold cyan]Installing Terraform...[/bold cyan]\n\n"
                f"  Step {i}/{len(steps)}: {msg}"
            )
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    stderr = escape(result.stderr.strip()[:500])
                    self._update(
                        f"[bold red]Installation failed[/bold red]\n\n"
                        f"  Step {i}: {msg}\n\n"
                        f"  {stderr}\n\n"
                        f"[dim]Press Enter or Escape to close[/dim]"
                    )
                    self._phase = "done"
                    self._success = False
                    return
            except subprocess.TimeoutExpired:
                self._update(
                    f"[bold red]Installation timed out[/bold red]\n\n"
                    f"  Step {i}: {msg}\n\n"
                    f"[dim]Press Enter or Escape to close[/dim]"
                )
                self._phase = "done"
                self._success = False
                return

        # Verify
        try:
            result = subprocess.run(
                ["terraform", "version"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                ver = escape(result.stdout.strip().split("\n")[0])
                self._update(
                    f"[bold green]Terraform installed successfully![/bold green]\n\n"
                    f"  {ver}\n\n"
                    f"[dim]Press Enter or Escape to close[/dim]"
                )
                self._phase = "done"
                self._success = True
                return
        except Exception:
            pass
        self._update(
            f"[bold red]Installation completed but terraform not responding[/bold red]\n\n"
            f"[dim]Press Enter or Escape to close[/dim]"
        )
        self._phase = "done"
        self._success = False

    @work(thread=True, exclusive=True)
    def _install_ansible(self) -> None:
        import subprocess
        from rich.markup import escape

        steps = [
            ("Updating package index...",
             "sudo apt-get update -qq"),
            ("Installing ansible...",
             "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible"),
        ]

        for i, (msg, cmd) in enumerate(steps, 1):
            self._update(
                f"[bold cyan]Installing Ansible...[/bold cyan]\n\n"
                f"  Step {i}/{len(steps)}: {msg}"
            )
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    stderr = escape(result.stderr.strip()[:500])
                    self._update(
                        f"[bold red]Installation failed[/bold red]\n\n"
                        f"  Step {i}: {msg}\n\n"
                        f"  {stderr}\n\n"
                        f"[dim]Press Enter or Escape to close[/dim]"
                    )
                    self._phase = "done"
                    self._success = False
                    return
            except subprocess.TimeoutExpired:
                self._update(
                    f"[bold red]Installation timed out[/bold red]\n\n"
                    f"  Step {i}: {msg}\n\n"
                    f"[dim]Press Enter or Escape to close[/dim]"
                )
                self._phase = "done"
                self._success = False
                return

        # Verify
        try:
            result = subprocess.run(
                ["ansible", "--version"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                ver = escape(result.stdout.strip().split("\n")[0])
                self._update(
                    f"[bold green]Ansible installed successfully![/bold green]\n\n"
                    f"  {ver}\n\n"
                    f"[dim]Press Enter or Escape to close[/dim]"
                )
                self._phase = "done"
                self._success = True
                return
        except Exception:
            pass
        self._update(
            f"[bold red]Installation completed but ansible not responding[/bold red]\n\n"
            f"[dim]Press Enter or Escape to close[/dim]"
        )
        self._phase = "done"
        self._success = False


# ── Main Setup Screen ──────────────────────────────────────────────

class SetupScreen(Screen):
    """Main setup wizard screen with component list."""

    BINDINGS = [
        Binding("enter", "configure", "Configure", show=True),
        Binding("t", "test", "Test Connection", show=True),
        Binding("s", "save_exit", "Save & Exit", show=True),
        Binding("m", "launch_main", "Main Menu", show=False),
        Binding("escape", "quit_setup", "Exit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict = {}
        self._labels: dict[str, Label] = {}
        self._testing: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="setup-container"):
            yield Static(
                "[bold]InfraForge Setup[/bold]  [dim]\u2502[/dim]  "
                "Select a component to configure  [dim]\u2502[/dim]  "
                "[dim]Enter[/dim]=Configure  [dim]t[/dim]=Test  [dim]s[/dim]=Save & Exit",
                id="setup-title",
                markup=True,
            )
            items = []
            for idx, (comp_id, comp_name, comp_desc) in enumerate(COMPONENTS, 1):
                lbl = Label(_format_row(idx, comp_name, comp_desc, False, "Checking..."), markup=True)
                self._labels[comp_id] = lbl
                items.append(ListItem(lbl, id=f"setup-{comp_id}"))
            yield ListView(*items, id="setup-list")
            yield Static("", id="setup-status", markup=True)
            yield Static(
                "\n[bold green]Setup is complete![/bold green]  "
                "All modules are configured and ready.\n",
                id="setup-complete-msg",
                markup=True,
                classes="hidden",
            )
            with Horizontal(id="setup-launch-row", classes="hidden"):
                yield Button(
                    "\u2713  Launch InfraForge",
                    id="setup-launch-btn",
                    variant="success",
                )
        yield Footer()

    def on_mount(self) -> None:
        self._cfg = _load_existing_config()
        self._refresh_all()
        # Focus the list so arrow keys work immediately
        self.query_one("#setup-list", ListView).focus()

    def _refresh_all(self) -> None:
        """Update all row labels and the status bar from current config."""
        ok_count = 0
        total = len(COMPONENTS)
        for idx, (comp_id, comp_name, comp_desc) in enumerate(COMPONENTS, 1):
            ok, info = _check_component(self._cfg, comp_id)
            self._labels[comp_id].update(
                _format_row(idx, comp_name, comp_desc, ok, info)
            )
            if ok:
                ok_count += 1
        status = self.query_one("#setup-status", Static)
        complete_msg = self.query_one("#setup-complete-msg", Static)
        launch_row = self.query_one("#setup-launch-row", Horizontal)
        if ok_count == total:
            status.update(
                f"  [bold green]All {total} components configured![/bold green]"
            )
            complete_msg.remove_class("hidden")
            launch_row.remove_class("hidden")
        else:
            status.update(
                f"  [bold]{ok_count}[/bold] [dim]of[/dim] [bold]{total}[/bold] [dim]components configured[/dim]  "
                f"[dim]\u2502[/dim]  [bold yellow]{total - ok_count} need attention[/bold yellow]"
            )
            complete_msg.add_class("hidden")
            launch_row.add_class("hidden")

    def _get_selected_comp_id(self) -> str | None:
        """Return the comp_id of the currently highlighted ListView row."""
        lv = self.query_one("#setup-list", ListView)
        if lv.highlighted_child is None:
            return None
        item_id = lv.highlighted_child.id or ""
        # item IDs are "setup-proxmox", "setup-dns", etc.
        return item_id.removeprefix("setup-") if item_id.startswith("setup-") else None

    # ── Actions ────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter pressed on a list item — open configure flow."""
        self.action_configure()

    def action_configure(self) -> None:
        comp_id = self._get_selected_comp_id()
        if not comp_id:
            self.notify("Select a component first.", severity="warning")
            return
        comp_name = next((n for i, n, _ in COMPONENTS if i == comp_id), comp_id)
        self.app.push_screen(
            ConfirmConfigModal(comp_name),
            callback=lambda confirmed: self._on_confirm_config(confirmed, comp_id),
        )

    def _on_confirm_config(self, confirmed: bool, comp_id: str) -> None:
        if not confirmed:
            return
        # For terraform/ansible, detect missing binary and offer install first
        if comp_id == "terraform" and not shutil.which("terraform"):
            self.app.push_screen(
                InstallDependencyModal("Terraform"),
                callback=lambda _installed: self._after_install_check(comp_id),
            )
            return
        if comp_id == "ansible" and not shutil.which("ansible"):
            self.app.push_screen(
                InstallDependencyModal("Ansible"),
                callback=lambda _installed: self._after_install_check(comp_id),
            )
            return
        self._push_config_modal(comp_id)

    def _after_install_check(self, comp_id: str) -> None:
        """Called after install-dependency modal; proceed to config form."""
        self._refresh_all()
        self._push_config_modal(comp_id)

    def _push_config_modal(self, comp_id: str) -> None:
        """Push the configuration modal for a component."""
        from infraforge.screens.setup_modals import get_config_modal
        modal = get_config_modal(comp_id, self._cfg)
        if modal is None:
            self.notify(f"No config form for {comp_id}.", severity="warning")
            return
        self.app.push_screen(
            modal,
            callback=lambda result: self._on_config_saved(result, comp_id),
        )

    def _on_config_saved(self, result: dict | None, comp_id: str) -> None:
        """Called when a config modal is dismissed with updated config section."""
        if result is None:
            return
        self._cfg[comp_id] = result
        _save_config(self._cfg)
        self._refresh_all()
        comp_name = next((n for i, n, _ in COMPONENTS if i == comp_id), comp_id)
        self.notify(f"{comp_name} configuration saved!", title="Saved")

    def action_test(self) -> None:
        comp_id = self._get_selected_comp_id()
        if not comp_id:
            self.notify("Select a component first.", severity="warning")
            return
        if self._testing:
            self.notify("A test is already running.", severity="warning")
            return
        comp_name = next((n for i, n, _ in COMPONENTS if i == comp_id), comp_id)
        self.notify(f"Testing {comp_name}...", title="Testing")
        self._testing = True
        self._run_test(comp_id, comp_name)

    @work(thread=True, exclusive=True, group="setup-test")
    def _run_test(self, comp_id: str, comp_name: str) -> None:
        """Run a connection test for the given component in a background thread."""
        # Offer auto-install for missing CLI tools
        if comp_id == "terraform" and not shutil.which("terraform"):
            self.app.call_from_thread(self._clear_testing)
            self.app.call_from_thread(self._offer_install, "Terraform")
            return
        if comp_id == "ansible" and not shutil.which("ansible"):
            self.app.call_from_thread(self._clear_testing)
            self.app.call_from_thread(self._offer_install, "Ansible")
            return

        title = f"{comp_name} Test"
        try:
            if comp_id == "proxmox":
                body = self._test_proxmox()
            elif comp_id == "dns":
                body = self._test_dns()
            elif comp_id == "ipam":
                body = self._test_ipam()
            elif comp_id == "terraform":
                body = self._test_terraform()
            elif comp_id == "ansible":
                body = self._test_ansible()
            elif comp_id == "ai":
                body = self._test_ai()
            else:
                body = "[yellow]No test available for this component.[/yellow]"
        except Exception as e:
            from rich.markup import escape
            body = f"[bold red]Test failed:[/bold red] {escape(str(e))}"
        finally:
            self.app.call_from_thread(self._clear_testing)

        self.app.call_from_thread(
            self.app.push_screen,
            TestResultModal(title, body),
        )

    def _offer_install(self, dep_name: str) -> None:
        """Push the install-dependency modal and refresh status on completion."""
        self.app.push_screen(
            InstallDependencyModal(dep_name),
            callback=lambda _result: self._refresh_all(),
        )

    def _clear_testing(self) -> None:
        self._testing = False

    # ── Test helpers ───────────────────────────────────────────

    def _test_proxmox(self) -> str:
        sec = self._cfg.get("proxmox", {})
        if not sec.get("host"):
            return "[red]Not configured \u2014 nothing to test.[/red]"
        from infraforge.config import Config
        cfg = Config.load()
        from infraforge.proxmox_client import ProxmoxClient
        client = ProxmoxClient(cfg)
        client.connect()
        nodes = client.get_node_info()
        lines = [f"[bold green]Connected successfully![/bold green]\n"]
        for n in nodes:
            status_color = "green" if n.status == "online" else "red"
            lines.append(
                f"  [{status_color}]\u25cf[/{status_color}] [bold]{n.node}[/bold]  "
                f"{n.status}  CPU: {n.cpu_percent:.1f}%  Mem: {n.mem_percent:.1f}%  Up: {n.uptime_str}"
            )
        return "\n".join(lines)

    def _test_dns(self) -> str:
        sec = self._cfg.get("dns", {})
        provider = sec.get("provider", "")
        if not provider:
            return "[red]Not configured \u2014 nothing to test.[/red]"
        if provider != "bind9":
            return f"[yellow]Provider '{provider}' \u2014 no live test available yet.[/yellow]"
        server = sec.get("server", "")
        port = sec.get("port", 53)
        tsig_name = sec.get("tsig_key_name", "")
        tsig_secret = sec.get("tsig_key_secret", "")
        tsig_algo = sec.get("tsig_algorithm", "hmac-sha256")
        from infraforge.dns_client import DNSClient
        client = DNSClient(server, port, tsig_name, tsig_secret, tsig_algo)
        client.check_health()
        zones = sec.get("zones", [])
        if not zones and sec.get("zone"):
            zones = [sec["zone"]]
        lines = [f"[bold green]DNS server reachable![/bold green]  ({server}:{port})\n"]
        for zone in zones:
            try:
                soa = client.get_zone_soa(zone)
                serial = soa.get("serial", "?") if isinstance(soa, dict) else "?"
                lines.append(f"  [green]\u2713[/green] {zone}  (serial: {serial})")
            except Exception as e:
                from rich.markup import escape
                lines.append(f"  [red]\u2717[/red] {zone}  ({escape(str(e))})")
        return "\n".join(lines)

    def _test_ipam(self) -> str:
        sec = self._cfg.get("ipam", {})
        url = sec.get("url", "")
        if not url:
            return "[red]Not configured \u2014 nothing to test.[/red]"
        from infraforge.config import Config
        cfg = Config.load()
        from infraforge.ipam_client import IPAMClient
        client = IPAMClient(cfg)
        client.check_health()
        sections = client.get_sections()
        subnets = client.get_subnets()
        return (
            f"[bold green]IPAM connected![/bold green]  ({url})\n\n"
            f"  Sections: {len(sections)}  \u2502  Subnets: {len(subnets)}"
        )

    def _test_terraform(self) -> str:
        # Binary-missing case is handled by _run_test -> _offer_install
        import subprocess
        result = subprocess.run(
            ["terraform", "version"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            from rich.markup import escape
            ver = escape(result.stdout.strip().split("\n")[0])
            workspace = escape(self._cfg.get("terraform", {}).get("workspace", "./terraform"))
            return f"[bold green]Terraform available![/bold green]\n\n  {ver}\n  Workspace: {workspace}"
        from rich.markup import escape
        return f"[red]terraform exited with code {result.returncode}[/red]\n{escape(result.stderr)}"

    def _test_ansible(self) -> str:
        # Binary-missing case is handled by _run_test -> _offer_install
        import subprocess
        result = subprocess.run(
            ["ansible", "--version"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            from rich.markup import escape
            ver = escape(result.stdout.strip().split("\n")[0])
            pdir = escape(self._cfg.get("ansible", {}).get("playbook_dir", "./ansible/playbooks"))
            return f"[bold green]Ansible available![/bold green]\n\n  {ver}\n  Playbook dir: {pdir}"
        from rich.markup import escape
        return f"[red]ansible exited with code {result.returncode}[/red]\n{escape(result.stderr)}"

    def _test_ai(self) -> str:
        sec = self._cfg.get("ai", {})
        key = sec.get("api_key", "")
        if not key:
            return "[red]Not configured \u2014 nothing to test.[/red]"
        import urllib.request
        import json
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models?limit=1",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        models = data.get("data", [])
        model_name = sec.get("model", "unknown")
        return (
            f"[bold green]API key valid![/bold green]\n\n"
            f"  Model: {model_name}\n"
            f"  API access confirmed ({len(models)}+ models available)"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setup-launch-btn":
            self.action_launch_main()

    def action_launch_main(self) -> None:
        """Save config and exit setup to launch the main InfraForge app."""
        # Check all modules are configured
        ok_count = sum(1 for cid, _, _ in COMPONENTS if _check_component(self._cfg, cid)[0])
        if ok_count < len(COMPONENTS):
            self.notify("Not all modules are configured yet.", severity="warning")
            return
        _save_config(self._cfg)
        self.app.exit(result="launch_main")

    def action_save_exit(self) -> None:
        _save_config(self._cfg)
        self.notify("Configuration saved!", title="Saved")
        self.app.exit()

    def action_quit_setup(self) -> None:
        self.app.exit()


# ── Standalone launcher ────────────────────────────────────────────

def run_setup_tui() -> None:
    """Launch the Textual-based setup wizard standalone."""
    from infraforge.app import _CUSTOM_THEMES

    class _SetupApp(App):
        TITLE = "InfraForge"
        SUB_TITLE = "Setup Wizard"
        CSS_PATH = "../../styles/app.tcss"

        def on_mount(self) -> None:
            for t in _CUSTOM_THEMES:
                self.register_theme(t)
            self.theme = "midnight"
            self.push_screen(SetupScreen())

    result = _SetupApp().run()

    if result == "launch_main":
        # User chose to launch main app — import and run it
        from infraforge.config import Config, ConfigError
        try:
            config = Config.load()
        except ConfigError as e:
            from rich.console import Console
            Console().print(f"[bold red]Config error:[/bold red] {e}")
            return
        from infraforge.app import InfraForgeApp
        InfraForgeApp(config=config).run()
