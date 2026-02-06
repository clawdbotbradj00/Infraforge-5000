"""Help screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.containers import VerticalScroll


HELP_TEXT = """\
[bold cyan]InfraForge — Proxmox VM Management TUI[/bold cyan]

[bold]Global Keys[/bold]
  [bold cyan]q[/bold cyan]           Quit the application
  [bold cyan]d[/bold cyan]           Go to Dashboard
  [bold cyan]?[/bold cyan]           Show this help screen
  [bold cyan]Escape[/bold cyan]      Go back / Close current screen
  [bold cyan]Backspace[/bold cyan]   Go back

[bold]Dashboard[/bold]
  [bold cyan]v[/bold cyan]           View Virtual Machines list
  [bold cyan]t[/bold cyan]           View Templates
  [bold cyan]n[/bold cyan]           View Node Info
  [bold cyan]x[/bold cyan]           DNS Management
  [bold cyan]c[/bold cyan]           Create New VM (wizard)
  [bold cyan]r[/bold cyan]           Refresh data
  [bold cyan]Enter[/bold cyan]       Select menu item
  [bold cyan]↑ ↓[/bold cyan]         Navigate menu

[bold]VM List[/bold]
  [bold cyan]f[/bold cyan]           Cycle filter (All / Running / Stopped)
  [bold cyan]s[/bold cyan]           Cycle sort field
  [bold cyan]g[/bold cyan]           Cycle grouping (None / Status / Node / Type)
  [bold cyan]r[/bold cyan]           Refresh VM list
  [bold cyan]Enter[/bold cyan]       View VM details
  [bold cyan]↑ ↓[/bold cyan]         Navigate rows

[bold]VM Detail[/bold]
  [bold cyan]r[/bold cyan]           Refresh details
  [bold cyan]↑ ↓[/bold cyan]         Scroll content

[bold]Templates[/bold]
  [bold cyan]Tab[/bold cyan]         Switch between template types
  [bold cyan]Enter[/bold cyan]       View template details
  [bold cyan]r[/bold cyan]           Refresh

[bold]Node Info[/bold]
  [bold cyan]r[/bold cyan]           Refresh node data
  [bold cyan]↑ ↓[/bold cyan]         Scroll content

[bold]DNS Management[/bold]
  [bold cyan]Tab[/bold cyan]         Next zone
  [bold cyan]Shift+Tab[/bold cyan]   Previous zone
  [bold cyan]1-9[/bold cyan]         Jump to zone by number
  [bold cyan]z[/bold cyan]           Add a zone
  [bold cyan]Z[/bold cyan]           Remove current zone
  [bold cyan]a[/bold cyan]           Add a DNS record
  [bold cyan]e[/bold cyan]           Edit selected record
  [bold cyan]d[/bold cyan]           Delete selected record
  [bold cyan]s[/bold cyan]           Cycle sort field (Name / Type / Value / TTL)
  [bold cyan]f[/bold cyan]           Cycle filter by record type
  [bold cyan]r[/bold cyan]           Refresh records from server

[bold]New VM Wizard[/bold]
  [bold cyan]Enter[/bold cyan]       Next step / Confirm selection
  [bold cyan]Escape[/bold cyan]      Cancel wizard
  [bold cyan]↑ ↓[/bold cyan]         Navigate options

[bold]About[/bold]
  InfraForge connects to your Proxmox VE cluster via the API
  to provide a rich terminal interface for managing your
  virtual infrastructure.

  Configuration: ~/.config/infraforge/config.yaml
  Setup wizard:  infraforge setup
"""


class HelpScreen(Screen):
    """Help screen showing keybindings and navigation."""

    BINDINGS = [
        Binding("escape", "go_back", "Close", show=True),
        Binding("backspace", "go_back", "Close", show=False),
        Binding("question_mark", "go_back", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="help-container"):
            yield Static(HELP_TEXT, markup=True)
        yield Footer()

    def action_go_back(self):
        self.app.pop_screen()
