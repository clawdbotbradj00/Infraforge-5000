"""AI Settings screen for InfraForge."""

import shutil

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Button
from textual.containers import Container, Horizontal
from textual import work


class AISettingsScreen(Screen):
    """Screen for configuring AI provider settings."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="ai-settings-container"):
            yield Static("[bold]AI Configuration[/bold]", markup=True, classes="section-title")
            yield Static("", id="ai-settings-status", markup=True)

            yield Static("")
            yield Static("[bold]Claude CLI[/bold]", markup=True)
            yield Static("", id="ai-cli-value", markup=True)

            yield Static("")
            yield Static("[bold]Model Preference[/bold]", markup=True)
            yield Static("", id="ai-model-value", markup=True)

            yield Static("")
            yield Static("[bold]Session[/bold]", markup=True)
            yield Static("", id="ai-session-value", markup=True)

            yield Static("")
            yield Static("[bold]Turns[/bold]", markup=True)
            yield Static("", id="ai-turns-value", markup=True)

            yield Static("")
            with Horizontal(id="ai-settings-actions"):
                yield Button("Clear Chat History", id="ai-clear-history", variant="warning")
                yield Button("Test Connection", id="ai-test-connection", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        claude_path = shutil.which("claude")
        ai_client = getattr(self.app, "ai_client", None)

        if claude_path:
            self.query_one("#ai-settings-status", Static).update(
                "[green]Ready[/green]  [dim]Press / anywhere to open AI chat[/dim]"
            )
            self.query_one("#ai-cli-value", Static).update(f"  {claude_path}")
        else:
            self.query_one("#ai-settings-status", Static).update(
                "[yellow]Not installed[/yellow]  [dim]Run:[/dim] "
                "[bold]npm install -g @anthropic-ai/claude-code[/bold]"
            )
            self.query_one("#ai-cli-value", Static).update("  [dim]Not found in PATH[/dim]")

        model = ""
        if ai_client and ai_client._model:
            model = ai_client._model
        elif hasattr(self.app, "config"):
            model = self.app.config.ai.model
        self.query_one("#ai-model-value", Static).update(
            f"  {model or '[dim]Default (set via infraforge setup)[/dim]'}"
        )

        if ai_client:
            sid = ai_client._session_id or "[dim]New session (not started)[/dim]"
            self.query_one("#ai-session-value", Static).update(f"  {sid}")
            self.query_one("#ai-turns-value", Static).update(
                f"  {ai_client._turn_count} turns"
            )
        else:
            self.query_one("#ai-session-value", Static).update("  [dim]No client[/dim]")
            self.query_one("#ai-turns-value", Static).update("  [dim]—[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-clear-history":
            ai_client = getattr(self.app, "ai_client", None)
            if ai_client:
                ai_client.clear_history()
                self._refresh_display()
                self.query_one("#ai-session-value", Static).update(
                    "  [green]Session reset[/green]"
                )
        elif event.button.id == "ai-test-connection":
            self._test_connection()

    @work(thread=True)
    def _test_connection(self) -> None:
        try:
            ai_client = getattr(self.app, "ai_client", None)
            if not ai_client or not ai_client.is_configured:
                self.app.call_from_thread(
                    self.query_one("#ai-settings-status", Static).update,
                    "[red]Claude CLI not found[/red]"
                )
                return
            response = ai_client.chat("Say 'OK' in one word.")
            for block in response:
                if block.get("type") == "text":
                    self.app.call_from_thread(
                        self.query_one("#ai-settings-status", Static).update,
                        f"[green]Connected![/green]  [dim]{block['text'][:60]}[/dim]"
                    )
                    return
                elif block.get("type") == "error":
                    self.app.call_from_thread(
                        self.query_one("#ai-settings-status", Static).update,
                        f"[red]Error:[/red] {block['text'][:80]}"
                    )
                    return
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#ai-settings-status", Static).update,
                f"[red]Error:[/red] {str(e)[:80]}"
            )

    def action_go_back(self) -> None:
        self.app.pop_screen()
