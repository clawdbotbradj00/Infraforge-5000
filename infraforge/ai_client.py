"""AI client for InfraForge — shells out to the ``claude`` CLI.

Uses the locally installed Claude Code CLI for authentication and
inference, so no separate API key is required.  Falls back to direct
Anthropic API calls if a key is configured and the CLI is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess


# ---------------------------------------------------------------------------
# System prompt — describes InfraForge copilot and available action markers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the InfraForge copilot embedded in a Proxmox management TUI.

BEHAVIOR:
- Every message includes live infrastructure state. Use ONLY that data.
- NEVER describe code, internals, or how things work.
- NEVER say "let me look at" or "I need to check". You already have
  everything you need in the state data and tools below.
- When the user asks you to CREATE, ADD, DELETE, or CHANGE something,
  immediately emit the action marker. Do not hesitate or ask for
  confirmation unless critical info is missing.
- When the user asks a QUESTION, answer from the state data. Be terse.

FORMAT (narrow terminal, ~60 chars wide):
- Plain text only. No markdown. No **, ##, ```, no bullet symbols.
- Use "- " dashes for lists. Keep lines under 70 chars.
- 1-3 sentences unless more detail is requested.

ACTIONS — emit on their own line, exactly this format:
<<<ACTION:tool_name:{"param":"value"}>>>

DNS:
<<<ACTION:create_dns_record:{"zone":"Z","name":"N","rtype":"A","value":"V","ttl":3600}>>>
<<<ACTION:update_dns_record:{"zone":"Z","name":"N","rtype":"A","value":"V","ttl":3600}>>>
<<<ACTION:delete_dns_record:{"zone":"Z","name":"N","rtype":"A","value":"V"}>>>

IPAM — IDs come from IPAM SECTIONS/SUBNETS in the state data:
<<<ACTION:create_ipam_section:{"name":"N","description":"D"}>>>
<<<ACTION:create_ipam_subnet:{"subnet":"10.0.7.0","mask":24,"section_id":1,"description":"D"}>>>
<<<ACTION:create_ipam_address:{"ip":"10.0.7.50","subnet_id":3,"hostname":"H","description":"D","tag":2}>>>
<<<ACTION:create_ipam_vlan:{"number":100,"name":"N","description":"D"}>>>
  tag: 1=Offline 2=Used 3=Reserved 4=DHCP
  vlan_id is optional on create_ipam_subnet

NAVIGATION:
<<<ACTION:navigate_to:{"screen":"S"}>>>
  S: dashboard vm_list templates nodes dns ipam ansible new_vm help

ACTION RULES:
- Explain what you are doing in plain text, then emit the marker.
- JSON must be valid, single-line.
- Get IDs (section_id, subnet_id) from the state data at the top.
- If the user gives enough info, ACT. Do not ask unnecessary questions.
"""

# Regex that extracts   <<<ACTION:name:{...}>>>   markers
_ACTION_RE = re.compile(r"<<<ACTION:(\w+):(.*?)>>>")


class AIClient:
    """AI client that delegates to the ``claude`` CLI.

    Conversation state is maintained via ``--resume <session_id>`` so the
    full chat history is preserved across turns without us having to
    replay it manually.
    """

    def __init__(self, config=None) -> None:
        self._claude_path: str | None = shutil.which("claude")
        self._session_id: str | None = None
        self._model: str = ""
        self._turn_count: int = 0
        self._custom_system_prompt: str = ""
        if config and hasattr(config, "ai"):
            self._model = config.ai.model or ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when the ``claude`` CLI is available."""
        return self._claude_path is not None

    def chat(self, user_message: str, app_context: dict | None = None) -> list[dict]:
        """Send *user_message* and return parsed response blocks.

        Returns
        -------
        list[dict]
            Each element is one of:
            - ``{"type": "text",     "text": "..."}``
            - ``{"type": "tool_use", "name": "...", "input": {...}, "id": "..."}``
            - ``{"type": "error",    "text": "..."}``
        """
        if not self.is_configured:
            return [{"type": "error", "text": "claude CLI not found. Install Claude Code first."}]

        prompt = user_message
        if app_context:
            prompt = f"[App context: {json.dumps(app_context)}]\n\n{prompt}"

        result_text = self._run_claude(prompt)
        self._turn_count += 1
        return self._parse_response(result_text)

    def send_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        """Send tool execution results back and get AI's continuation.

        Parameters
        ----------
        results:
            List of ``(tool_name, result_string)`` pairs.

        Returns the AI's follow-up response blocks.
        """
        parts = []
        for name, result in results:
            parts.append(f"[Tool result for {name}]: {result}")
        message = "\n".join(parts)
        return self.chat(message)

    def clear_history(self) -> None:
        """Reset the conversation (starts a new session)."""
        self._session_id = None
        self._turn_count = 0

    def get_system_prompt(self) -> str:
        return self._custom_system_prompt or SYSTEM_PROMPT

    def chat_stream(self, user_message: str, context: str = ""):
        """Yield text chunks as they stream from the claude CLI.

        Parameters
        ----------
        user_message:
            The user's message text.
        context:
            Pre-loaded infrastructure state to include in the prompt.
            When provided, it is wrapped in state delimiters before the
            user message so the copilot can answer from real data.

        After iteration completes, session_id and turn_count are updated.
        Caller should accumulate chunks and call ``parse_response()`` on
        the full text to extract action markers.
        """
        if not self.is_configured:
            yield "[Error: claude CLI not found]"
            return

        prompt = user_message
        if context:
            prompt = (
                "[Current infrastructure state]\n"
                f"{context}\n"
                "[End state]\n\n"
                f"{user_message}"
            )

        # Try streaming
        yielded_any = False
        for chunk in self._run_claude_stream(prompt):
            yielded_any = True
            yield chunk

        if yielded_any:
            self._turn_count += 1
            return

        # Fallback: blocking json mode
        result_text = self._run_claude(prompt)
        self._turn_count += 1
        if result_text:
            yield result_text

    def parse_response(self, text: str) -> list[dict]:
        """Public wrapper for ``_parse_response``."""
        return self._parse_response(text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_claude(self, prompt: str) -> str:
        """Shell out to ``claude -p`` and return the result text."""
        cmd = [self._claude_path, "-p", prompt, "--output-format", "json",
               "--max-turns", "1",
               "--system-prompt", self.get_system_prompt()]

        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        if self._model:
            cmd.extend(["--model", self._model])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "[Error: claude CLI timed out after 120 seconds]"
        except FileNotFoundError:
            return "[Error: claude CLI not found]"

        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:300] if proc.stderr else "unknown error"
            return f"[Error: claude exited with code {proc.returncode}: {stderr}]"

        # Parse JSON output
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Fallback: treat stdout as raw text
            return proc.stdout.strip()

        # Capture session ID for conversation continuity
        if data.get("session_id"):
            self._session_id = data["session_id"]

        return data.get("result", proc.stdout.strip())

    def _parse_response(self, text: str) -> list[dict]:
        """Parse response text for action markers and plain text."""
        blocks: list[dict] = []
        last_end = 0
        tool_idx = 0

        for match in _ACTION_RE.finditer(text):
            # Collect any plain text before this marker
            before = text[last_end:match.start()].strip()
            if before:
                blocks.append({"type": "text", "text": before})

            tool_name = match.group(1)
            try:
                tool_input = json.loads(match.group(2))
            except json.JSONDecodeError:
                tool_input = {}

            blocks.append({
                "type": "tool_use",
                "name": tool_name,
                "input": tool_input,
                "id": f"tool_{self._turn_count}_{tool_idx}",
            })
            tool_idx += 1
            last_end = match.end()

        # Remaining text after the last marker
        remaining = text[last_end:].strip()
        if remaining:
            blocks.append({"type": "text", "text": remaining})

        # If nothing was parsed, return the whole text
        if not blocks:
            blocks.append({"type": "text", "text": text})

        return blocks

    def _run_claude_stream(self, prompt: str):
        """Stream response from ``claude -p`` using ``stream-json`` output."""
        cmd = [self._claude_path, "-p", prompt, "--output-format", "stream-json",
               "--verbose", "--max-turns", "1",
               "--system-prompt", self.get_system_prompt()]

        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        if self._model:
            cmd.extend(["--model", self._model])

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except FileNotFoundError:
            yield "[Error: claude CLI not found]"
            return

        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Final result — capture session ID
                if data.get("type") == "result":
                    if data.get("session_id"):
                        self._session_id = data["session_id"]
                    continue

                # Extract text content from this event
                text = self._extract_stream_text(data)
                if text:
                    yield text

            proc.wait(timeout=10)
            if proc.returncode and proc.returncode != 0:
                stderr = ""
                if proc.stderr:
                    stderr = proc.stderr.read()[:300]
                yield f"\n[Error: exit code {proc.returncode}: {stderr}]"
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            yield f"\n[Error: {str(e)[:100]}]"

    @staticmethod
    def _extract_stream_text(data: dict) -> str:
        """Extract text content from a stream-json event."""
        evt_type = data.get("type", "")

        # Streaming delta (token-by-token)
        if evt_type == "content_block_delta":
            return data.get("delta", {}).get("text", "")

        # Complete assistant message (non-streaming fallback)
        if evt_type == "assistant":
            msg = data.get("message", {})
            content = msg.get("content", [])
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "".join(parts)

        return ""
