"""AI Chat Modal screen for InfraForge.

Provides a full-screen modal overlay for chatting with the AI assistant.
The modal can be invoked from any screen by pressing ``/`` and supports
natural-language interaction including tool execution (VM management,
DNS, IPAM, navigation, etc.) via the Anthropic API.
"""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static, Input
from textual import work


class AIChatModal(ModalScreen):
    """Full-screen modal overlay for AI chat."""

    BINDINGS = [
        Binding("escape", "close_chat", "Close", show=True),
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

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]AI Assistant[/bold]    [dim]Press Esc to close[/dim]",
            id="ai-chat-title",
            markup=True,
        )
        with VerticalScroll(id="ai-chat-history"):
            pass  # Messages added dynamically
        yield Input(placeholder="Type a message...", id="ai-chat-input")

    def on_mount(self) -> None:
        """Focus the input and show a welcome message if history is empty."""
        self.query_one("#ai-chat-input", Input).focus()
        history = self.query_one("#ai-chat-history", VerticalScroll)
        if not history.children:
            self._add_ai_message(
                "Hello! I'm your InfraForge AI assistant. "
                "Ask me anything about your infrastructure, or tell me what to do."
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the chat input."""
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self._add_user_message(text)
        self._send_to_ai(text)

    def action_close_chat(self) -> None:
        """Close the modal and return to the underlying screen."""
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # AI communication
    # ------------------------------------------------------------------

    @work(thread=True)
    def _send_to_ai(self, text: str) -> None:
        """Send message to AI and handle response including tool calls."""
        try:
            ai_client = self.app.ai_client
            if not ai_client or not ai_client.is_configured:
                self.app.call_from_thread(
                    self._add_error_message,
                    "Claude Code CLI not found. Install it first: npm install -g @anthropic-ai/claude-code",
                )
                return

            self._process_response(ai_client, ai_client.chat(text))

        except Exception as e:
            self.app.call_from_thread(self._add_error_message, str(e))

    def _process_response(self, ai_client, response: list[dict]) -> None:
        """Walk response blocks, execute tools, and send results back."""
        tool_results: list[tuple[str, str]] = []

        for block in response:
            if block["type"] == "text":
                self.app.call_from_thread(self._add_ai_message, block["text"])
            elif block["type"] == "tool_use":
                tool_name = block["name"]
                tool_input = block["input"]

                self.app.call_from_thread(
                    self._add_tool_message, tool_name, tool_input
                )
                self.app.call_from_thread(self._hide_for_action)

                result = self._execute_tool(tool_name, tool_input)

                self.app.call_from_thread(self._show_after_action)
                tool_results.append((tool_name, result))

            elif block["type"] == "error":
                self.app.call_from_thread(
                    self._add_error_message, block["text"]
                )

        # If tools were executed, send results back and process continuation
        if tool_results:
            continuation = ai_client.send_tool_results(tool_results)
            self._process_response(ai_client, continuation)

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

            elif name == "list_vms":
                vms = self.app.proxmox.get_all_vms()
                result = []
                for vm in vms:
                    result.append(
                        {
                            "vmid": vm.vmid,
                            "name": vm.name,
                            "status": vm.status.value,
                            "node": vm.node,
                            "cpu_percent": round(vm.cpu_percent, 1),
                            "mem_gb": round(vm.mem_gb, 1),
                        }
                    )
                return json.dumps(result)

            elif name == "vm_action":
                vmid = inputs["vmid"]
                node = inputs["node"]
                action = inputs["action"]
                self.app.proxmox.vm_action(node, vmid, action)
                return f"Successfully executed {action} on VM {vmid}"

            elif name == "get_vm_detail":
                vmid = inputs["vmid"]
                node = inputs["node"]
                vm = self.app.proxmox.get_vm_status(node, vmid)
                return json.dumps(
                    {
                        "vmid": vm.vmid,
                        "name": vm.name,
                        "status": vm.status.value,
                        "node": vm.node,
                        "cpu_percent": round(vm.cpu_percent, 1),
                        "mem_gb": round(vm.mem_gb, 1),
                        "disk_gb": round(vm.disk_gb, 1),
                        "uptime": vm.uptime_str,
                        "tags": vm.tags,
                    }
                )

            elif name == "list_nodes":
                nodes = self.app.proxmox.get_node_info()
                result = []
                for n in nodes:
                    result.append(
                        {
                            "node": n.node,
                            "status": n.status,
                            "cpu_percent": round(n.cpu_percent, 1),
                            "mem_percent": round(n.mem_percent, 1),
                            "disk_percent": round(n.disk_percent, 1),
                            "uptime": n.uptime_str,
                        }
                    )
                return json.dumps(result)

            elif name == "list_dns_records":
                zone = inputs["zone"]
                from infraforge.dns_client import DNSClient

                dns_cfg = self.app.config.dns
                client = DNSClient(
                    dns_cfg.server,
                    dns_cfg.port,
                    dns_cfg.tsig_key_name,
                    dns_cfg.tsig_key_secret,
                    dns_cfg.tsig_algorithm,
                )
                records = client.get_zone_records(zone)
                result = [
                    {
                        "name": r.name,
                        "type": r.rtype,
                        "value": r.value,
                        "ttl": r.ttl,
                    }
                    for r in records
                ]
                return json.dumps(result)

            elif name == "add_dns_record":
                zone = inputs["zone"]
                from infraforge.dns_client import DNSClient

                dns_cfg = self.app.config.dns
                client = DNSClient(
                    dns_cfg.server,
                    dns_cfg.port,
                    dns_cfg.tsig_key_name,
                    dns_cfg.tsig_key_secret,
                    dns_cfg.tsig_algorithm,
                )
                client.add_record(
                    zone,
                    inputs["name"],
                    inputs["rtype"],
                    inputs["value"],
                    inputs.get("ttl", 3600),
                )
                return (
                    f"Added {inputs['rtype']} record: "
                    f"{inputs['name']}.{zone} -> {inputs['value']}"
                )

            elif name == "delete_dns_record":
                zone = inputs["zone"]
                from infraforge.dns_client import DNSClient

                dns_cfg = self.app.config.dns
                client = DNSClient(
                    dns_cfg.server,
                    dns_cfg.port,
                    dns_cfg.tsig_key_name,
                    dns_cfg.tsig_key_secret,
                    dns_cfg.tsig_algorithm,
                )
                client.delete_record(zone, inputs["name"], inputs["rtype"])
                return (
                    f"Deleted {inputs['rtype']} record: {inputs['name']}.{zone}"
                )

            elif name == "list_subnets":
                from infraforge.ipam_client import IPAMClient

                client = IPAMClient(self.app.config)
                subnets = client.get_all_subnets()
                result = [
                    {
                        "id": s.get("id"),
                        "subnet": s.get("subnet"),
                        "mask": s.get("mask"),
                        "description": s.get("description", ""),
                    }
                    for s in subnets
                ]
                return json.dumps(result)

            elif name == "list_addresses":
                from infraforge.ipam_client import IPAMClient

                client = IPAMClient(self.app.config)
                addresses = client.get_subnet_addresses(inputs["subnet_id"])
                result = [
                    {
                        "ip": a.get("ip"),
                        "hostname": a.get("hostname", ""),
                        "description": a.get("description", ""),
                        "tag": a.get("tag", ""),
                    }
                    for a in addresses
                ]
                return json.dumps(result)

            elif name == "list_templates":
                _, templates = self.app.proxmox.get_all_vms_and_templates()
                result = [
                    {
                        "vmid": t.vmid,
                        "name": t.name,
                        "node": t.node,
                        "type": t.type_label,
                    }
                    for t in templates
                ]
                return json.dumps(result)

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            return f"Error: {str(e)}"

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
    # Message display helpers
    # ------------------------------------------------------------------

    def _add_user_message(self, text: str) -> None:
        """Append a user message bubble to the chat history."""
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(
            f"[bold cyan]You:[/bold cyan]  {text}",
            classes="ai-msg-user",
            markup=True,
        )
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_ai_message(self, text: str) -> None:
        """Append an AI message bubble to the chat history."""
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(
            f"[bold green]AI:[/bold green]  {text}",
            classes="ai-msg-ai",
            markup=True,
        )
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_tool_message(self, tool_name: str, tool_input: dict) -> None:
        """Append a tool-execution status message to the chat history."""
        history = self.query_one("#ai-chat-history", VerticalScroll)
        desc = f"{tool_name}"
        if tool_input:
            # Brief summary of inputs
            parts = [f"{k}={v}" for k, v in tool_input.items()]
            desc += f"({', '.join(parts[:3])})"
        msg = Static(
            f"[dim italic]Running: {desc}[/dim italic]",
            classes="ai-msg-tool",
            markup=True,
        )
        history.mount(msg)
        history.scroll_end(animate=False)

    def _add_error_message(self, text: str) -> None:
        """Append an error message to the chat history."""
        history = self.query_one("#ai-chat-history", VerticalScroll)
        msg = Static(
            f"[bold red]Error:[/bold red]  {text}",
            classes="ai-msg-error",
            markup=True,
        )
        history.mount(msg)
        history.scroll_end(animate=False)
