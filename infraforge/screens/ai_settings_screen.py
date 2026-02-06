"""AI Settings screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Input, Button
from textual.containers import Container, Horizontal, Vertical
from textual import work

from rich.text import Text


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

            yield Static("")  # spacer
            yield Static("[bold]Provider[/bold]", markup=True)
            yield Static("", id="ai-provider-value", markup=True)

            yield Static("")
            yield Static("[bold]Model[/bold]", markup=True)
            yield Static("", id="ai-model-value", markup=True)

            yield Static("")
            yield Static("[bold]API Key[/bold]", markup=True)
            yield Static("", id="ai-key-value", markup=True)

            yield Static("")
            yield Static("[bold]Chat History[/bold]", markup=True)
            yield Static("", id="ai-history-value", markup=True)

            yield Static("")
            with Horizontal(id="ai-settings-actions"):
                yield Button("Clear Chat History", id="ai-clear-history", variant="warning")
                yield Button("Test Connection", id="ai-test-connection", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        ai_cfg = self.app.config.ai

        if ai_cfg.api_key:
            masked = ai_cfg.api_key[:12] + "..." + ai_cfg.api_key[-4:] if len(ai_cfg.api_key) > 16 else "****"
            self.query_one("#ai-settings-status", Static).update(
                "[green]Configured[/green]"
            )
            self.query_one("#ai-key-value", Static).update(f"  [dim]{masked}[/dim]")
        else:
            self.query_one("#ai-settings-status", Static).update(
                "[yellow]Not configured[/yellow]  [dim]Run[/dim] [bold]infraforge setup[/bold] [dim]to add your API key[/dim]"
            )
            self.query_one("#ai-key-value", Static).update("  [dim]Not set[/dim]")

        self.query_one("#ai-provider-value", Static).update(
            f"  {ai_cfg.provider or '[dim]Not set[/dim]'}"
        )
        self.query_one("#ai-model-value", Static).update(
            f"  {ai_cfg.model or '[dim]Not set[/dim]'}"
        )

        # Chat history count
        if hasattr(self.app, 'ai_client') and self.app.ai_client:
            msg_count = len(self.app.ai_client._messages)
            self.query_one("#ai-history-value", Static).update(
                f"  {msg_count} messages"
            )
        else:
            self.query_one("#ai-history-value", Static).update("  [dim]No client initialized[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-clear-history":
            if hasattr(self.app, 'ai_client') and self.app.ai_client:
                self.app.ai_client.clear_history()
                self._refresh_display()
                self.query_one("#ai-history-value", Static).update("  [green]History cleared[/green]")
        elif event.button.id == "ai-test-connection":
            self._test_connection()

    @work(thread=True)
    def _test_connection(self) -> None:
        try:
            if not hasattr(self.app, 'ai_client') or not self.app.ai_client:
                self.app.call_from_thread(
                    self.query_one("#ai-settings-status", Static).update,
                    "[red]No AI client[/red]  [dim]Run infraforge setup[/dim]"
                )
                return
            # Quick test: send a minimal message
            response = self.app.ai_client.chat("Say 'OK' in one word.")
            for block in response:
                if block.get("type") == "text":
                    self.app.call_from_thread(
                        self.query_one("#ai-settings-status", Static).update,
                        f"[green]Connected![/green]  [dim]Response: {block['text'][:50]}[/dim]"
                    )
                    # Clear the test message from history
                    self.app.ai_client._messages = self.app.ai_client._messages[:-2]
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
