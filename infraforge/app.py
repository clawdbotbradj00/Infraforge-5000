"""Main InfraForge Textual Application."""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Header, Footer, Static, LoadingIndicator
from textual.containers import Container, Center, Middle
from textual import work

from infraforge import __version__
from infraforge.config import Config
from infraforge.preferences import Preferences
from infraforge.proxmox_client import ProxmoxClient, ProxmoxConnectionError
from infraforge.ai_client import AIClient


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


_CUSTOM_THEMES = [
    # ── Dark backgrounds, light text ──────────────────────────────
    Theme(
        name="midnight",
        primary="#5B9BD5",
        secondary="#4472C4",
        accent="#FFC000",
        warning="#FFC000",
        error="#FF6B6B",
        success="#70AD47",
        foreground="#D6E4F0",
        background="#0C1021",
        surface="#131B30",
        panel="#1A2742",
        dark=True,
        variables={
            "block-cursor-background": "#FFC000",
            "block-cursor-foreground": "#0C1021",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#997300",
            "block-cursor-blurred-foreground": "#0C1021",
            "footer-key-foreground": "#FFC000",
            "footer-background": "#1A2742",
            "input-selection-background": "#4472C4 35%",
            "button-color-foreground": "#0C1021",
        },
    ),
    Theme(
        name="matrix",
        primary="#00FF41",
        secondary="#008F11",
        accent="#33FF33",
        warning="#CCFF00",
        error="#FF0033",
        success="#00FF41",
        foreground="#00FF41",
        background="#000000",
        surface="#0A0A0A",
        panel="#0D1A0D",
        dark=True,
        variables={
            "block-cursor-background": "#00FF41",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#006B1A",
            "block-cursor-blurred-foreground": "#000000",
            "footer-key-foreground": "#33FF33",
            "input-selection-background": "#008F11 40%",
            "button-color-foreground": "#000000",
        },
    ),
    Theme(
        name="amber",
        primary="#FFB000",
        secondary="#CC8800",
        accent="#FFCC00",
        warning="#FFEE00",
        error="#FF4400",
        success="#FFB000",
        foreground="#FFB000",
        background="#000000",
        surface="#0A0800",
        panel="#1A1000",
        dark=True,
        variables={
            "block-cursor-background": "#FFB000",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#805800",
            "block-cursor-blurred-foreground": "#000000",
            "footer-key-foreground": "#FFCC00",
            "input-selection-background": "#CC8800 40%",
            "button-color-foreground": "#000000",
        },
    ),
    Theme(
        name="elementary",
        primary="#00BCFF",
        secondary="#F78FE7",
        accent="#00D3D0",
        warning="#D0BC00",
        error="#FF8059",
        success="#44BC44",
        foreground="#F2F2F2",
        background="#101010",
        surface="#1A1A1A",
        panel="#242424",
        dark=True,
        variables={
            "block-cursor-background": "#F2F2F2",
            "block-cursor-foreground": "#101010",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#888888",
            "block-cursor-blurred-foreground": "#101010",
            "footer-key-foreground": "#00BCFF",
            "input-selection-background": "#00BCFF 30%",
            "button-color-foreground": "#101010",
        },
    ),
    Theme(
        name="dark-pastel",
        primary="#61AFEF",
        secondary="#C678DD",
        accent="#56B6C2",
        warning="#E5C07B",
        error="#E06C75",
        success="#98C379",
        foreground="#FFFFFF",
        background="#000000",
        surface="#0C0C0C",
        panel="#1C1C1C",
        dark=True,
        variables={
            "block-cursor-background": "#FFFFFF",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#888888",
            "block-cursor-blurred-foreground": "#000000",
            "footer-key-foreground": "#61AFEF",
            "input-selection-background": "#61AFEF 30%",
            "button-color-foreground": "#000000",
        },
    ),
    Theme(
        name="borland",
        primary="#FFFF55",
        secondary="#55FFFF",
        accent="#FFFF55",
        warning="#FFFF55",
        error="#FF5555",
        success="#55FF55",
        foreground="#FFFFFF",
        background="#0000A4",
        surface="#0000CC",
        panel="#000088",
        dark=True,
        variables={
            "block-cursor-background": "#FFFF55",
            "block-cursor-foreground": "#0000A4",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#999933",
            "block-cursor-blurred-foreground": "#0000A4",
            "footer-key-foreground": "#FFFF55",
            "footer-background": "#000088",
            "input-selection-background": "#55FFFF 35%",
            "button-color-foreground": "#0000A4",
        },
    ),
    # ── Light backgrounds, dark text ──────────────────────────────
    Theme(
        name="paper",
        primary="#0451A5",
        secondary="#267F99",
        accent="#AF00DB",
        warning="#BF8803",
        error="#CD3131",
        success="#008000",
        foreground="#1E1E1E",
        background="#FFFFFF",
        surface="#F3F3F3",
        panel="#E8E8E8",
        dark=False,
        variables={
            "block-cursor-background": "#0451A5",
            "block-cursor-foreground": "#FFFFFF",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": "#6699CC",
            "block-cursor-blurred-foreground": "#FFFFFF",
            "footer-key-foreground": "#0451A5",
            "footer-background": "#E8E8E8",
            "input-selection-background": "#ADD6FF",
            "button-color-foreground": "#FFFFFF",
        },
    ),
]

_THEME_CYCLE = [t.name for t in _CUSTOM_THEMES]


class InfraForgeApp(App):
    """InfraForge - Proxmox VM Management TUI."""

    TITLE = "InfraForge"
    SUB_TITLE = f"v{__version__}"
    CSS_PATH = "../styles/app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("question_mark", "help_screen", "Help", show=True),
        Binding("d", "dashboard", "Dashboard", show=True),
        Binding("T", "cycle_theme", "Theme", show=True, priority=True),
        Binding("slash", "open_ai_chat", "AI Chat", show=True),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.proxmox = ProxmoxClient(config)
        self.preferences = Preferences.load()
        self.ai_client: AIClient | None = None
        self._connected = False

    def on_mount(self):
        for t in _CUSTOM_THEMES:
            self.register_theme(t)
        saved = self.preferences.theme
        if saved and saved in self.available_themes:
            self.theme = saved
        else:
            self.theme = _THEME_CYCLE[0]
        self.push_screen(ConnectingScreen())
        self.connect_to_proxmox()

    def action_cycle_theme(self) -> None:
        """Cycle through high-contrast themes."""
        current = self.theme
        try:
            idx = _THEME_CYCLE.index(current)
            next_theme = _THEME_CYCLE[(idx + 1) % len(_THEME_CYCLE)]
        except ValueError:
            next_theme = _THEME_CYCLE[0]
        self.theme = next_theme
        self.preferences.theme = next_theme
        self.preferences.save()
        self.notify(f"Theme: {next_theme}", timeout=2)

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
        self.ai_client = AIClient(self.config)
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

    def action_open_ai_chat(self) -> None:
        """Open the AI chat overlay."""
        from infraforge.screens.ai_chat_modal import AIChatModal
        self.push_screen(AIChatModal())
