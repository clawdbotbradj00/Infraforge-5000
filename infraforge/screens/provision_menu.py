"""VM Provisioning Menu — sub-menu for templates and custom VM creation."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, ListView, ListItem, Label
from textual.containers import Container


class ProvisionMenuScreen(Screen):
    """Sub-menu for VM provisioning — templates and custom."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("1", "deploy_dns_server", "1:DNS Server", show=True),
        Binding("3", "custom_vm", "3:Custom VM", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="provision-menu-container"):
            yield Static("VM Provisioning", classes="section-title")
            yield Static(
                "  Select a provisioning template or build a custom VM.",
                id="provision-menu-hint",
            )
            yield ListView(
                ListItem(
                    Label("[dim bold]  PROVISIONING TEMPLATES[/dim bold]"),
                    id="nav-group-templates",
                    disabled=True,
                ),
                ListItem(
                    Label("  [1]  Deploy DNS Server      \u2014  Provision Ubuntu VM with BIND9 DNS"),
                    id="nav-dns-server",
                ),
                ListItem(
                    Label("[dim]  [2]  (More templates coming soon)[/dim]"),
                    id="nav-placeholder",
                    disabled=True,
                ),
                ListItem(
                    Label(" "),
                    id="nav-spacer",
                    disabled=True,
                ),
                ListItem(
                    Label("[dim bold]  CUSTOM[/dim bold]"),
                    id="nav-group-custom",
                    disabled=True,
                ),
                ListItem(
                    Label("  [3]  Custom VM              \u2014  Full VM creation wizard"),
                    id="nav-custom-vm",
                ),
                id="provision-nav-menu",
            )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id == "nav-dns-server":
            self.action_deploy_dns_server()
        elif item_id == "nav-custom-vm":
            self.action_custom_vm()

    def action_deploy_dns_server(self) -> None:
        from infraforge.screens.dns_server_wizard import DNSServerWizardScreen
        self.app.push_screen(DNSServerWizardScreen())

    def action_custom_vm(self) -> None:
        from infraforge.screens.new_vm import NewVMScreen
        self.app.push_screen(NewVMScreen())

    def action_go_back(self) -> None:
        self.app.pop_screen()
