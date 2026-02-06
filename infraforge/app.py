"""Main InfraForge Textual Application."""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, LoadingIndicator
from textual.containers import Container, Center, Middle
from textual import work

from infraforge.config import Config
from infraforge.preferences import Preferences
from infraforge.proxmox_client import ProxmoxClient, ProxmoxConnectionError


class ConnectingScreen(Screen):
    """Screen shown while connecting to Proxmox."""

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Middle():
                with Container(id="connecting-box"):
                    yield Static("⚡ Connecting to Proxmox...", id="connecting-msg")
                    yield LoadingIndicator()
        yield Footer()


class ConnectionErrorScreen(Screen):
    """Screen shown when connection fails."""

    BINDINGS = [
        Binding("r", "retry", "Retry"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, error_message: str):
        super().__init__()
        self.error_message = error_message

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Middle():
                with Container(id="error-box"):
                    yield Static("✗ Connection Failed", id="error-title")
                    yield Static(self.error_message, id="error-detail")
                    yield Static(
                        "\n[R] Retry  [Q] Quit\n"
                        "Check your config at ~/.config/infraforge/config.yaml",
                        id="error-help",
                    )
        yield Footer()

    def action_retry(self):
        self.app.pop_screen()
        self.app.connect_to_proxmox()

    def action_quit_app(self):
        self.app.exit()


class InfraForgeApp(App):
    """InfraForge - Proxmox VM Management TUI."""

    TITLE = "InfraForge"
    SUB_TITLE = "Proxmox VM Manager"
    CSS_PATH = "../styles/app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("question_mark", "help_screen", "Help", show=True),
        Binding("d", "dashboard", "Dashboard", show=True),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.proxmox = ProxmoxClient(config)
        self.preferences = Preferences.load()
        self._connected = False

    def on_mount(self):
        self.push_screen(ConnectingScreen())
        self.connect_to_proxmox()

    @work(thread=True)
    def connect_to_proxmox(self):
        """Connect to Proxmox in a background thread."""
        try:
            self.proxmox.connect()
            self._connected = True
            self.call_from_thread(self._on_connected)
        except ProxmoxConnectionError as e:
            self.call_from_thread(self._on_connection_error, str(e))
        except Exception as e:
            self.call_from_thread(self._on_connection_error, str(e))

    def _on_connected(self):
        """Called when Proxmox connection succeeds."""
        self.pop_screen()  # Remove connecting screen
        from infraforge.screens.dashboard import DashboardScreen
        self.push_screen(DashboardScreen())

    def _on_connection_error(self, error: str):
        """Called when Proxmox connection fails."""
        self.pop_screen()  # Remove connecting screen
        self.push_screen(ConnectionErrorScreen(error))

    def action_dashboard(self):
        """Go to dashboard."""
        if self._connected:
            # Pop all screens back to base, then push dashboard
            while len(self.screen_stack) > 1:
                self.pop_screen()
            from infraforge.screens.dashboard import DashboardScreen
            self.push_screen(DashboardScreen())

    def action_help_screen(self):
        """Show help screen."""
        from infraforge.screens.help_screen import HelpScreen
        self.push_screen(HelpScreen())
