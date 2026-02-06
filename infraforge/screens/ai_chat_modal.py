"""AI Chat Modal screen for InfraForge.

Provides a full-screen modal overlay for chatting with the AI assistant.
The modal can be invoked from any screen by pressing ``/`` and supports
natural-language interaction including tool execution (VM management,
DNS, IPAM, navigation, etc.) via the Claude Code CLI.

Chat history is stored on ``app._ai_chat_history`` so it persists across
open/close cycles.  Press Ctrl+N to start a fresh conversation.
"""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static, Input
from textual import work

from infraforge.ai_context import gather_context


# -- Lightweight message record stored on the app --------------------------

def _msg(role: str, text: str, markup: str = "", css_class: str = "") -> dict:
    """Create a chat history entry."""
    return {"role": role, "text": text, "markup": markup, "css_class": css_class}


class AIChatModal(ModalScreen):
    """Full-screen modal overlay for AI chat."""

    BINDINGS = [
        Binding("escape", "close_chat", "Close", show=True),
        Binding("ctrl+n", "new_chat", "New Chat", show=True),
    ]

    DEFAULT_CSS = """
    AIChatModal {
        align: center middle;
    }

    #ai-chat-title {
        dock: top;
        width: 100%;
        height: 3;
        background: $primary-background;
        color: $text;
        text-style: bold;
        content-align: center middle;
        padding: 1 2;
        border-bottom: solid $primary;
    }

    #ai-chat-history {
        height: 1fr;
        padding: 1 2;
        background: $surface;
    }

    .ai-msg-user {
        text-align: right;
        margin: 0 0 1 8;
    }

    .ai-msg-ai {
        text-align: left;
        margin: 0 8 1 0;
    }

    .ai-msg-tool {
        text-align: center;
        margin: 0 4 1 4;
    }

    .ai-msg-error {
        text-align: left;
        margin: 0 4 1 0;
    }

    #ai-chat-input {
        dock: bottom;
        width: 100%;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._streaming_widget: Static | None = None

    # ------------------------------------------------------------------
    # Persistent history helpers (stored on app)
    # ------------------------------------------------------------------

    def _get_history(self) -> list[dict]:
        """Return the app-level chat history list, creating if needed."""
        if not hasattr(self.app, "_ai_chat_history"):
            self.app._ai_chat_history = []
        return self.app._ai_chat_history

    def _append_history(self, entry: dict) -> None:
        self._get_history().append(entry)

    # ------------------------------------------------------------------
    # Compose / Mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]AI Assistant[/bold]  "
            "[dim]Esc[/dim] close  [dim]Ctrl+N[/dim] new chat",
            id="ai-chat-title",
            markup=True,
        )
        with VerticalScroll(id="ai-chat-history"):
            pass  # Messages replayed in on_mount
        yield Input(placeholder="Type a message...", id="ai-chat-input")

    def on_mount(self) -> None:
        """Replay stored history into the UI, or show welcome."""
        self.query_one("#ai-chat-input", Input).focus()
        history = self._get_history()
        if history:
            self._replay_history(history)
        else:
            welcome = (
                "Hello! I'm your InfraForge AI assistant. "
                "Ask me anything about your infrastructure, "
                "or tell me what to do."
            )
            self._add_ai_message(welcome)

    def _replay_history(self, history: list[dict]) -> None:
        """Mount Static widgets for every stored message."""
        container = self.query_one("#ai-chat-history", VerticalScroll)
        for entry in history:
            widget = Static(entry["markup"], classes=entry["css_class"], markup=True)
            container.mount(widget)
        container.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the chat input."""
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self._add_user_message(text)
        self._send_to_ai(text)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_close_chat(self) -> None:
        """Close the modal and return to the underlying screen."""
        self.app.pop_screen()

    def action_new_chat(self) -> None:
        """Clear chat history and start a fresh conversation."""
        # Clear stored history
        self.app._ai_chat_history = []
        # Clear the AI client session so it starts fresh
        ai_client = getattr(self.app, "ai_client", None)
        if ai_client:
            ai_client.clear_history()
        # Clear UI
        container = self.query_one("#ai-chat-history", VerticalScroll)
        container.remove_children()
        # Show welcome
        self._add_ai_message(
            "New conversation started. How can I help?"
        )
        self.query_one("#ai-chat-input", Input).focus()

    # ------------------------------------------------------------------
    # AI communication — streaming
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="ai-chat")
    def _send_to_ai(self, text: str) -> None:
        """Send message to AI with streaming response."""
        try:
            ai_client = self.app.ai_client
            if not ai_client or not ai_client.is_configured:
                self.app.call_from_thread(
                    self._add_error_message,
                    "Claude Code CLI not found. Install: "
                    "npm install -g @anthropic-ai/claude-code",
                )
                return

            # Gather live infrastructure context
            context = gather_context(self.app)
            self._stream_and_handle(ai_client, text, context=context)

        except Exception as e:
            self.app.call_from_thread(self._hide_thinking)
            self.app.call_from_thread(self._add_error_message, str(e))

    def _stream_and_handle(self, ai_client, message: str, depth: int = 0, context: str = "") -> None:
        """Stream a message to AI, execute tools, send results back.

        This is the core loop — every round (initial message and tool-result
        continuations) is streamed so the user always sees live feedback.
        Recurses when tool calls produce results that need a follow-up.
        """
        if depth > 5:
            self.app.call_from_thread(
                self._add_error_message, "Too many tool rounds — stopping"
            )
            return

        # Show thinking indicator
        self.app.call_from_thread(self._show_thinking)

        # Stream response
        full_text = ""
        first_chunk = True
        for chunk in ai_client.chat_stream(message, context=context):
            if first_chunk:
                self.app.call_from_thread(self._replace_thinking_with_response)
                first_chunk = False
            full_text += chunk
            self.app.call_from_thread(self._update_streaming_message, full_text)

        if first_chunk:
            # No chunks received at all
            self.app.call_from_thread(self._hide_thinking)
            if not full_text:
                self.app.call_from_thread(
                    self._add_error_message, "No response received"
                )
                return

        # Parse for action markers
        blocks = ai_client.parse_response(full_text)
        tool_blocks = [b for b in blocks if b["type"] == "tool_use"]

        if not tool_blocks:
            # Pure text response — persist it and we're done
            self.app.call_from_thread(self._persist_streaming_message, full_text)
            self._streaming_widget = None
            return

        # Strip action markers from displayed text
        text_parts = [b["text"] for b in blocks if b["type"] == "text"]
        clean_text = "\n".join(text_parts).strip()
        if clean_text:
            self.app.call_from_thread(
                self._update_streaming_message, clean_text
            )
            self.app.call_from_thread(self._persist_streaming_message, clean_text)
        else:
            self.app.call_from_thread(self._remove_streaming_message)

        # Done with this streaming widget — tools will add their own messages
        self._streaming_widget = None

        # Execute tools
        tool_results: list[tuple[str, str]] = []
        for block in tool_blocks:
            tool_name = block["name"]
            tool_input = block["input"]

            self.app.call_from_thread(
                self._add_tool_message, tool_name, tool_input
            )

            # Only hide modal for navigation (not data queries)
            is_nav = tool_name == "navigate_to"
            if is_nav:
                self.app.call_from_thread(self._hide_for_action)

            result = self._execute_tool(tool_name, tool_input)

            if is_nav:
                self.app.call_from_thread(self._show_after_action)

            tool_results.append((tool_name, result))

        # Send tool results back — stream the continuation too
        if tool_results:
            parts = []
            for name, result in tool_results:
                parts.append(f"[Tool result for {name}]: {result}")
            continuation_msg = "\n".join(parts)
            self._stream_and_handle(ai_client, continuation_msg, depth + 1)

    # ------------------------------------------------------------------
    # Streaming UI helpers
    # ------------------------------------------------------------------

    def _show_thinking(self) -> None:
        """Show a thinking indicator in the chat."""
        history = self.query_one("#ai-chat-history", VerticalScroll)
        self._streaming_widget = Static(
            "[bold green]AI:[/bold green]  [dim italic]Thinking...[/dim italic]",
            classes="ai-msg-ai",
            markup=True,
        )
        history.mount(self._streaming_widget)
        history.scroll_end(animate=False)

    def _replace_thinking_with_response(self) -> None:
        """Replace thinking text with empty response, ready for streaming."""
        if self._streaming_widget:
            self._streaming_widget.update(
                "[bold green]AI:[/bold green]  "
            )

    def _hide_thinking(self) -> None:
        """Remove thinking indicator without showing a response."""
        if self._streaming_widget:
            self._streaming_widget.remove()
            self._streaming_widget = None

    def _update_streaming_message(self, text: str) -> None:
        """Update the streaming message widget with accumulated text."""
        if self._streaming_widget:
            self._streaming_widget.update(
                f"[bold green]AI:[/bold green]  {self._esc(text)}"
            )
            history = self.query_one("#ai-chat-history", VerticalScroll)
            history.scroll_end(animate=False)

    def _persist_streaming_message(self, text: str) -> None:
        """Save the final streaming message text to persistent history."""
        markup = f"[bold green]AI:[/bold green]  {self._esc(text)}"
        self._append_history(_msg("ai", text, markup=markup, css_class="ai-msg-ai"))

    def _remove_streaming_message(self) -> None:
        """Remove the streaming message (e.g., only contained action markers)."""
        if self._streaming_widget:
            self._streaming_widget.remove()
            self._streaming_widget = None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, inputs: dict) -> str:
        """Execute an AI tool and return the result as a string."""
        try:
            if name == "navigate_to":
                screen = inputs["screen"]
                self.app.call_from_thread(self._navigate_to, screen)
                return f"Navigated to {screen}"

            # ── DNS mutations ──────────────────────────────────────
            elif name in ("create_dns_record", "update_dns_record", "delete_dns_record"):
                return self._exec_dns(name, inputs)

            # ── IPAM mutations ─────────────────────────────────────
            elif name in (
                "create_ipam_section",
                "create_ipam_subnet",
                "create_ipam_address",
                "create_ipam_vlan",
            ):
                return self._exec_ipam(name, inputs)

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            return f"Error: {str(e)}"

    # ------------------------------------------------------------------
    # DNS tool execution
    # ------------------------------------------------------------------

    def _exec_dns(self, name: str, inputs: dict) -> str:
        """Handle DNS mutation tools."""
        from infraforge.dns_client import DNSClient

        dns_cfg = self.app.config.dns
        client = DNSClient(
            dns_cfg.server,
            dns_cfg.port,
            dns_cfg.tsig_key_name,
            dns_cfg.tsig_key_secret,
            dns_cfg.tsig_algorithm,
        )

        zone = inputs["zone"]
        rec_name = inputs.get("name", "")
        rtype = inputs.get("rtype")
        value = inputs.get("value")
        ttl = inputs.get("ttl", 3600)

        if name == "create_dns_record":
            client.create_record(rec_name, rtype, value, ttl=ttl, zone=zone)
            return f"Created {rtype} record: {rec_name}.{zone} -> {value}"

        elif name == "update_dns_record":
            client.update_record(rec_name, rtype, value, ttl=ttl, zone=zone)
            return f"Updated {rtype} record: {rec_name}.{zone} -> {value}"

        elif name == "delete_dns_record":
            client.delete_record(rec_name, rtype=rtype, value=value, zone=zone)
            parts = [f"Deleted records for {rec_name}.{zone}"]
            if rtype:
                parts[0] = f"Deleted {rtype} record: {rec_name}.{zone}"
            return parts[0]

        return "Unknown DNS action"

    # ------------------------------------------------------------------
    # IPAM tool execution
    # ------------------------------------------------------------------

    def _exec_ipam(self, name: str, inputs: dict) -> str:
        """Handle IPAM mutation tools."""
        from infraforge.ipam_client import IPAMClient

        client = IPAMClient(self.app.config)

        if name == "create_ipam_section":
            result = client.create_section(
                inputs["name"],
                description=inputs.get("description", ""),
            )
            sect_id = result.get("id", "?")
            return f"Created IPAM section '{inputs['name']}' (id={sect_id})"

        elif name == "create_ipam_subnet":
            result = client.create_subnet(
                subnet=inputs["subnet"],
                mask=int(inputs["mask"]),
                section_id=inputs["section_id"],
                description=inputs.get("description", ""),
                vlan_id=inputs.get("vlan_id"),
            )
            sub_id = result.get("id", "?")
            return (
                f"Created subnet {inputs['subnet']}/{inputs['mask']} "
                f"in section {inputs['section_id']} (subnet_id={sub_id})"
            )

        elif name == "create_ipam_address":
            result = client.create_address(
                ip=inputs["ip"],
                subnet_id=inputs["subnet_id"],
                hostname=inputs.get("hostname", ""),
                description=inputs.get("description", ""),
                tag=inputs.get("tag", 2),
            )
            return f"Created IP reservation {inputs['ip']} in subnet {inputs['subnet_id']}"

        elif name == "create_ipam_vlan":
            result = client.create_vlan(
                number=int(inputs["number"]),
                name=inputs.get("name", ""),
                description=inputs.get("description", ""),
            )
            vlan_id = result.get("id", "?")
            return f"Created VLAN {inputs['number']} (id={vlan_id})"

        return "Unknown IPAM action"

    # ------------------------------------------------------------------
    # Navigation helper
    # ------------------------------------------------------------------

    def _navigate_to(self, screen: str) -> None:
        """Navigate to a screen. Called from main thread."""
        screen_map = {
            "dashboard": None,  # Pop to dashboard
            "vm_list": "infraforge.screens.vm_list:VMListScreen",
            "templates": "infraforge.screens.template_list:TemplateListScreen",
            "nodes": "infraforge.screens.node_info:NodeInfoScreen",
            "dns": "infraforge.screens.dns_screen:DNSScreen",
            "ipam": "infraforge.screens.ipam_screen:IPAMScreen",
            "ansible": "infraforge.screens.ansible_screen:AnsibleScreen",
            "new_vm": "infraforge.screens.new_vm:NewVMScreen",
            "help": "infraforge.screens.help_screen:HelpScreen",
        }

        if screen == "dashboard":
            # Pop all screens back to dashboard
            while len(self.app.screen_stack) > 1:
                self.app.pop_screen()
            return

        spec = screen_map.get(screen)
        if spec:
            module_path, class_name = spec.split(":")
            import importlib

            mod = importlib.import_module(module_path)
            screen_cls = getattr(mod, class_name)
            self.app.push_screen(screen_cls())

    # ------------------------------------------------------------------
    # Show / hide during tool execution
    # ------------------------------------------------------------------

    def _hide_for_action(self) -> None:
        """Temporarily hide the modal during tool execution."""
        self.styles.display = "none"

    def _show_after_action(self) -> None:
        """Show the modal again after tool execution."""
        self.styles.display = "block"
        # Scroll to bottom
        scroll = self.query_one("#ai-chat-history", VerticalScroll)
        scroll.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Message display helpers (all persist to app-level history)
    # ------------------------------------------------------------------

    @staticmethod
    def _esc(text: str) -> str:
        """Escape Rich markup brackets in dynamic text."""
        return text.replace("[", "\\[")

    def _add_user_message(self, text: str) -> None:
        """Append a user message bubble to the chat history."""
        markup = f"[bold cyan]You:[/bold cyan]  {self._esc(text)}"
        css_class = "ai-msg-user"
        self._append_history(_msg("user", text, markup=markup, css_class=css_class))
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(markup, classes=css_class, markup=True)
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_ai_message(self, text: str) -> None:
        """Append an AI message bubble to the chat history."""
        markup = f"[bold green]AI:[/bold green]  {self._esc(text)}"
        css_class = "ai-msg-ai"
        self._append_history(_msg("ai", text, markup=markup, css_class=css_class))
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(markup, classes=css_class, markup=True)
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_tool_message(self, tool_name: str, tool_input: dict) -> None:
        """Append a tool-execution status message to the chat history."""
        desc = f"{tool_name}"
        if tool_input:
            parts = [f"{k}={v}" for k, v in tool_input.items()]
            desc += f"({', '.join(parts[:3])})"
        markup = f"[dim italic]Running: {self._esc(desc)}[/dim italic]"
        css_class = "ai-msg-tool"
        self._append_history(_msg("tool", desc, markup=markup, css_class=css_class))
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(markup, classes=css_class, markup=True)
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_error_message(self, text: str) -> None:
        """Append an error message to the chat history."""
        markup = f"[bold red]Error:[/bold red]  {self._esc(text)}"
        css_class = "ai-msg-error"
        self._append_history(_msg("error", text, markup=markup, css_class=css_class))
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(markup, classes=css_class, markup=True)
        history.mount(msg)
        history.scroll_end(animate=False)
