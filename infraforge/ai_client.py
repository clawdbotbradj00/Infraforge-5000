"""Anthropic API client for InfraForge AI chat feature.

Uses only stdlib (urllib.request, json) -- no external SDK dependencies.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
import urllib.error


SYSTEM_PROMPT = """\
You are the AI assistant built into InfraForge, a Proxmox VM management TUI.
You help users manage their virtual infrastructure through natural conversation.

Available actions you can take:

NAVIGATION:
- navigate_to: Go to any screen (dashboard, vm_list, templates, nodes, dns, ipam, ansible, new_vm, help)

VM MANAGEMENT:
- list_vms: Get a list of all VMs with status, node, CPU, memory info
- vm_action: Start, stop, reboot, shutdown a VM by VMID
- get_vm_detail: Get detailed info about a specific VM

NODE INFO:
- list_nodes: Get cluster node status and resource usage

DNS MANAGEMENT:
- list_dns_records: List DNS records for a zone
- add_dns_record: Add a new DNS record
- delete_dns_record: Delete a DNS record

IPAM:
- list_subnets: List IP subnets
- list_addresses: List IP addresses in a subnet

TEMPLATES:
- list_templates: List available VM/CT templates

When you need to perform an action, use the appropriate tool. When chatting,
be concise and helpful. You have full context of the InfraForge application."""

TOOLS = [
    {
        "name": "navigate_to",
        "description": "Navigate to a specific screen in InfraForge",
        "input_schema": {
            "type": "object",
            "properties": {
                "screen": {
                    "type": "string",
                    "enum": [
                        "dashboard",
                        "vm_list",
                        "templates",
                        "nodes",
                        "dns",
                        "ipam",
                        "ansible",
                        "new_vm",
                        "help",
                    ],
                    "description": "The screen to navigate to",
                }
            },
            "required": ["screen"],
        },
    },
    {
        "name": "list_vms",
        "description": "List all VMs and containers with their status, node, CPU, memory usage",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "vm_action",
        "description": "Perform an action on a VM (start, stop, reboot, shutdown)",
        "input_schema": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "The VM ID"},
                "node": {"type": "string", "description": "The node the VM is on"},
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "reboot", "shutdown"],
                    "description": "Action to perform",
                },
            },
            "required": ["vmid", "node", "action"],
        },
    },
    {
        "name": "get_vm_detail",
        "description": "Get detailed information about a specific VM",
        "input_schema": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "The VM ID"},
                "node": {"type": "string", "description": "The node the VM is on"},
            },
            "required": ["vmid", "node"],
        },
    },
    {
        "name": "list_nodes",
        "description": "List all cluster nodes with status and resource usage",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_dns_records",
        "description": "List DNS records for a specific zone",
        "input_schema": {
            "type": "object",
            "properties": {
                "zone": {"type": "string", "description": "The DNS zone name"}
            },
            "required": ["zone"],
        },
    },
    {
        "name": "add_dns_record",
        "description": "Add a new DNS record",
        "input_schema": {
            "type": "object",
            "properties": {
                "zone": {"type": "string", "description": "The DNS zone"},
                "name": {
                    "type": "string",
                    "description": "Record name (e.g. 'webserver')",
                },
                "rtype": {
                    "type": "string",
                    "description": "Record type (A, AAAA, CNAME, etc.)",
                },
                "value": {
                    "type": "string",
                    "description": "Record value (IP, hostname, etc.)",
                },
                "ttl": {
                    "type": "integer",
                    "description": "TTL in seconds",
                    "default": 3600,
                },
            },
            "required": ["zone", "name", "rtype", "value"],
        },
    },
    {
        "name": "delete_dns_record",
        "description": "Delete a DNS record",
        "input_schema": {
            "type": "object",
            "properties": {
                "zone": {"type": "string", "description": "The DNS zone"},
                "name": {"type": "string", "description": "Record name"},
                "rtype": {"type": "string", "description": "Record type"},
            },
            "required": ["zone", "name", "rtype"],
        },
    },
    {
        "name": "list_subnets",
        "description": "List all IP subnets from IPAM",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_addresses",
        "description": "List IP addresses in a subnet",
        "input_schema": {
            "type": "object",
            "properties": {
                "subnet_id": {
                    "type": "string",
                    "description": "The subnet ID",
                }
            },
            "required": ["subnet_id"],
        },
    },
    {
        "name": "list_templates",
        "description": "List available VM and container templates",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class AIClient:
    """Anthropic API client for InfraForge AI chat.

    Uses urllib.request to call the Anthropic Messages API directly --
    no external SDK required.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, config) -> None:
        """Initialise the client from the application Config object.

        Parameters
        ----------
        config:
            The app's Config object.  Expected attributes:
            ``config.ai.api_key``, ``config.ai.model``,
            ``config.ai.provider``.
        """
        self._api_key: str = config.ai.api_key
        self._model: str = config.ai.model or "claude-sonnet-4-5-20250929"
        self._provider: str = config.ai.provider
        self._messages: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(self, user_message: str, app_context: dict | None = None) -> list[dict]:
        """Send a user message and return parsed response blocks.

        Parameters
        ----------
        user_message:
            The text the user typed.
        app_context:
            Optional dict of application state to include with the
            user message (e.g. current screen, selected VM).

        Returns
        -------
        list[dict]
            A list of response blocks.  Each block is one of:
            - ``{"type": "text", "text": "..."}``
            - ``{"type": "tool_use", "name": "...", "input": {...}, "id": "..."}``
            - ``{"type": "error", "text": "..."}``
        """
        # Build user content ------------------------------------------------
        content = user_message
        if app_context:
            context_str = json.dumps(app_context, indent=2)
            content = (
                f"[App context: {context_str}]\n\n{user_message}"
            )

        self._messages.append({"role": "user", "content": content})

        # Call API -----------------------------------------------------------
        try:
            response = self._call_api(self._messages, self.get_system_prompt())
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode()
            except Exception:
                pass
            error_msg = f"API error: {exc.code} {exc.reason}"
            if error_body:
                error_msg += f" - {error_body}"
            return [{"type": "error", "text": error_msg}]
        except urllib.error.URLError as exc:
            return [{"type": "error", "text": f"Network error: {exc.reason}"}]
        except Exception as exc:
            return [{"type": "error", "text": f"Network error: {exc}"}]

        # Parse response -----------------------------------------------------
        blocks: list[dict] = []
        content_blocks = response.get("content", [])

        for block in content_blocks:
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                blocks.append(
                    {
                        "type": "tool_use",
                        "name": block["name"],
                        "input": block["input"],
                        "id": block["id"],
                    }
                )

        # Append assistant turn to history so subsequent calls continue the
        # conversation correctly.
        self._messages.append({"role": "assistant", "content": content_blocks})

        return blocks

    def process_tool_result(self, tool_use_id: str, result: str) -> None:
        """Append a tool_result message to history.

        This allows the next ``chat()`` call to continue the
        conversation after a tool use round-trip.

        Parameters
        ----------
        tool_use_id:
            The ``id`` from the tool_use block that was executed.
        result:
            The string result of running the tool.
        """
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                    }
                ],
            }
        )

    def get_system_prompt(self) -> str:
        """Return the system prompt describing InfraForge capabilities."""
        return SYSTEM_PROMPT

    def clear_history(self) -> None:
        """Reset the conversation message history."""
        self._messages = []

    @property
    def is_configured(self) -> bool:
        """Return True if the API key is set."""
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_api(self, messages: list[dict], system: str) -> dict:
        """Call the Anthropic Messages API via urllib.

        Parameters
        ----------
        messages:
            The conversation history in Anthropic message format.
        system:
            The system prompt text.

        Returns
        -------
        dict
            The parsed JSON response from the API.
        """
        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
            "tools": TOOLS,
        }

        data = json.dumps(payload).encode()

        req = urllib.request.Request(
            self.API_URL,
            data=data,
            headers={
                "X-Api-Key": self._api_key,
                "anthropic-version": self.API_VERSION,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
