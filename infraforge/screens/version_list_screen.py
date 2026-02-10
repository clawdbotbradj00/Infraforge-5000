"""Version List Screen -- browse and install InfraForge releases."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Static
from textual import work

from rich.text import Text

from infraforge import __version__
from infraforge.updater import (
    fetch_all_releases,
    get_pinned_version,
    perform_update_to_version,
    unpin_version,
    perform_update,
)


class ReleaseDetailModal(ModalScreen):
    """Modal overlay showing full release notes for a single version."""

    BINDINGS = [
        Binding("escape", "close_modal", "Close", show=True),
    ]

    DEFAULT_CSS = """
    ReleaseDetailModal {
        align: center middle;
    }

    #release-modal-container {
        width: 80%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #release-title {
        text-style: bold;
        color: $accent;
        margin: 0 0 1 0;
    }

    #release-meta {
        color: $text-muted;
        margin: 0 0 1 0;
    }

    #release-body {
        height: 1fr;
        border: round $primary-background;
        padding: 1 2;
        margin: 0 0 1 0;
    }

    #release-actions {
        height: 3;
        layout: horizontal;
        content-align: center middle;
    }
    """

    def __init__(self, release: dict) -> None:
        super().__init__()
        self._release = release

    def compose(self) -> ComposeResult:
        release = self._release
        tag = release.get("tag", "unknown")
        title = release.get("name", "") or tag
        date = release.get("published", "")[:10] if release.get("published") else "Unknown"
        body = release.get("body", "") or "(No release notes)"

        with Vertical(id="release-modal-container"):
            yield Static(
                f"[bold]{self._esc(title)}[/bold]  [dim]({tag})[/dim]",
                id="release-title",
                markup=True,
            )
            yield Static(
                f"[dim]Published:[/dim] {date}",
                id="release-meta",
                markup=True,
            )
            with VerticalScroll(id="release-body"):
                yield Static(
                    self._esc(body),
                    id="release-body-text",
                    markup=False,
                )
            with Container(id="release-actions"):
                yield Button(
                    f"Install {tag}",
                    id="install-btn",
                    variant="primary",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "install-btn":
            tag = self._release.get("tag", "")
            self.dismiss(tag)

    def action_close_modal(self) -> None:
        self.dismiss(None)

    @staticmethod
    def _esc(text: str) -> str:
        """Escape Rich markup brackets in dynamic text."""
        return text.replace("[", "\\[")


class VersionListScreen(Screen):
    """Screen displaying all InfraForge releases with install capability."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("enter", "view_detail", "View Details", show=True),
        Binding("i", "install", "Install Selected", show=True),
        Binding("u", "update_latest", "Update to Latest", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    #version-status {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    #version-table {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._releases: list[dict] = []
        self._pinned_version: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="version-container"):
            yield Static(
                f"[bold]InfraForge Versions[/bold]  [dim]|[/dim]  "
                f"Installed: [bold green]{__version__}[/bold green]",
                id="version-status",
                markup=True,
            )
            yield DataTable(id="version-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#version-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        table.add_column(Text("Version", style="bold cyan"), key="version", width=18)
        table.add_column(Text("Date", style="bold cyan"), key="date", width=14)
        table.add_column(Text("Title", style="bold cyan"), key="title")

        self._load_releases()

    @work(thread=True)
    def _load_releases(self) -> None:
        """Fetch all releases from GitHub in a background thread."""
        try:
            releases = fetch_all_releases()
            pinned = get_pinned_version()
            self.app.call_from_thread(self._populate_table, releases, pinned)
        except Exception as e:
            self.app.call_from_thread(self._show_load_error, str(e))

    def _populate_table(self, releases: list[dict], pinned: str | None) -> None:
        """Fill the DataTable with release data. Called on the main thread."""
        self._releases = releases or []
        self._pinned_version = pinned
        table = self.query_one("#version-table", DataTable)
        table.clear()

        if not self._releases:
            self.query_one("#version-status", Static).update(
                f"[bold]InfraForge Versions[/bold]  [dim]|[/dim]  "
                f"Installed: [bold green]{__version__}[/bold green]  [dim]|[/dim]  "
                f"[dim italic]No releases found[/dim italic]"
            )
            return

        current_normalized = __version__.lstrip("vV")
        pinned_normalized = pinned.lstrip("vV") if pinned else None

        for release in self._releases:
            tag = release.get("tag", "unknown")
            tag_normalized = tag.lstrip("vV")
            title = release.get("name", "") or tag
            date = release.get("published", "")[:10] if release.get("published") else ""

            # Build styled version column
            version_text = Text()
            is_current = tag_normalized == current_normalized
            is_pinned = pinned_normalized is not None and tag_normalized == pinned_normalized

            if is_pinned:
                version_text.append("\U0001f512 ", style="bold yellow")

            if is_current:
                version_text.append(tag, style="bold green")
                version_text.append(" (installed)", style="green")
            else:
                version_text.append(tag, style="bold")

            # Date column
            date_text = Text(date, style="dim")

            # Title column
            title_text = Text(self._esc_plain(title))

            table.add_row(
                version_text, date_text, title_text,
                key=tag,
            )

        release_count = len(self._releases)
        status_parts = [
            f"[bold]InfraForge Versions[/bold]  [dim]|[/dim]  "
            f"Installed: [bold green]{__version__}[/bold green]  [dim]|[/dim]  "
            f"[dim]{release_count} releases[/dim]",
        ]
        if pinned:
            status_parts.append(f"  [dim]|[/dim]  Pinned: [bold yellow]{pinned}[/bold yellow]")
        self.query_one("#version-status", Static).update("".join(status_parts))

    def _show_load_error(self, error: str) -> None:
        """Display a loading error in the status bar."""
        self.query_one("#version-status", Static).update(
            f"[bold]InfraForge Versions[/bold]  [dim]|[/dim]  "
            f"[bold red]Error: {error}[/bold red]"
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_view_detail(self) -> None:
        """Show the release detail modal for the currently selected row."""
        release = self._get_selected_release()
        if release is None:
            return
        self.app.push_screen(
            ReleaseDetailModal(release),
            callback=self._on_detail_dismissed,
        )

    def action_install(self) -> None:
        """Install the version selected in the table."""
        release = self._get_selected_release()
        if release is None:
            return
        tag = release.get("tag", "")
        if not tag:
            return
        self.notify(
            f"Installing {tag} -- this will checkout git tag {tag}...",
            title="Version Install",
        )
        self._run_install(tag)

    def action_update_latest(self) -> None:
        """Update to the latest release (git pull + pip install)."""
        self.notify("Updating to latest version...", title="Update")
        self._run_update_latest()

    def action_refresh(self) -> None:
        """Re-fetch the release list."""
        self.query_one("#version-status", Static).update(
            f"[bold]InfraForge Versions[/bold]  [dim]|[/dim]  "
            f"[bold yellow]Refreshing...[/bold yellow]"
        )
        table = self.query_one("#version-table", DataTable)
        table.clear()
        self._load_releases()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key press on a table row (shows detail modal)."""
        self.action_view_detail()

    # ------------------------------------------------------------------
    # Background operations
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="version-install")
    def _run_install(self, tag: str) -> None:
        """Install a specific version in a background thread."""
        try:
            success = perform_update_to_version(tag)
            if success:
                self.app.call_from_thread(
                    self.notify,
                    f"Installed {tag} -- restart InfraForge to use this version.",
                    title="Install Complete",
                )
            else:
                self.app.call_from_thread(
                    self.notify,
                    f"Failed to install {tag}. Check terminal output for details.",
                    title="Install Failed",
                    severity="error",
                )
        except Exception as e:
            self.app.call_from_thread(
                self.notify,
                f"Install error: {e}",
                title="Install Error",
                severity="error",
            )

    @work(thread=True, exclusive=True, group="version-install")
    def _run_update_latest(self) -> None:
        """Pull latest and reinstall in a background thread."""
        try:
            success = perform_update()
            if success:
                self.app.call_from_thread(
                    self.notify,
                    "Updated to latest -- restart InfraForge to use the new version.",
                    title="Update Complete",
                )
            else:
                self.app.call_from_thread(
                    self.notify,
                    "Update failed. Check terminal output for details.",
                    title="Update Failed",
                    severity="error",
                )
        except Exception as e:
            self.app.call_from_thread(
                self.notify,
                f"Update error: {e}",
                title="Update Error",
                severity="error",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_selected_release(self) -> dict | None:
        """Return the release dict for the currently highlighted row."""
        table = self.query_one("#version-table", DataTable)
        if not self._releases:
            self.notify("No releases loaded.", severity="warning")
            return None
        try:
            row_key = table.cursor_row
        except Exception:
            self.notify("No row selected.", severity="warning")
            return None

        if row_key < 0 or row_key >= len(self._releases):
            self.notify("No row selected.", severity="warning")
            return None

        return self._releases[row_key]

    def _on_detail_dismissed(self, result: str | None) -> None:
        """Callback when the release detail modal is dismissed.

        If the user pressed 'Install', *result* is the version tag string.
        """
        if result:
            self.notify(
                f"Installing {result} -- this will checkout git tag {result}...",
                title="Version Install",
            )
            self._run_install(result)

    @staticmethod
    def _esc_plain(text: str) -> str:
        """Strip Rich markup brackets from text intended for plain display."""
        return text.replace("[", "\\[")


def run_version_browser() -> None:
    """Launch a standalone Textual app showing the version browser.

    Called from ``infraforge versions`` / ``infraforge list versions``.
    """
    from textual.app import App

    from infraforge.app import _CUSTOM_THEMES

    class _VersionBrowserApp(App):
        TITLE = "InfraForge"
        SUB_TITLE = f"v{__version__}"
        CSS_PATH = "../../styles/app.tcss"

        def on_mount(self) -> None:
            for t in _CUSTOM_THEMES:
                self.register_theme(t)
            self.theme = "midnight"
            self.push_screen(VersionListScreen())

    _VersionBrowserApp().run()
