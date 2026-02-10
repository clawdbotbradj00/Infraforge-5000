"""SSH Key Authentication Setup Modal.

Guides the user through setting up SSH key auth to a Proxmox node.
Used by template export/import screens when key-based SSH access
is not yet configured for the target host.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Container
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static
from textual import work

from infraforge.ssh_helper import (
    test_ssh,
    find_ssh_keys,
    copy_ssh_key,
    generate_ssh_key,
    ensure_sshpass,
)


class SSHSetupModal(ModalScreen[bool]):
    """Modal that walks the user through SSH key auth setup for a Proxmox node.

    Parameters
    ----------
    host:
        The IP address or hostname of the Proxmox node.

    Returns ``True`` via ``dismiss(True)`` when SSH key auth is successfully
    configured, or ``False`` on cancel.
    """

    DEFAULT_CSS = """
SSHSetupModal {
    align: center middle;
}
#ssh-setup-box {
    width: 70;
    height: auto;
    max-height: 80%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
#ssh-log {
    height: 8;
    margin: 1 0;
    border: tall $primary-background;
    background: $primary-background;
}
#ssh-password-input {
    margin: 0 0 1 0;
}
"""

    def __init__(self, host: str) -> None:
        super().__init__()
        self._host = host

    def compose(self) -> ComposeResult:
        with Container(id="ssh-setup-box"):
            yield Static(
                "[bold]SSH Key Authentication Setup[/bold]",
                markup=True,
            )
            yield Static(
                f"\nSSH key auth is required to transfer files to\n"
                f"your Proxmox node at [bold]{self._host}[/bold].\n",
                markup=True,
            )
            yield Static("", id="ssh-key-status", markup=True)
            yield Static(
                f"\nRoot password for [bold]{self._host}[/bold]:",
                markup=True,
            )
            yield Input(password=True, id="ssh-password-input")
            yield RichLog(markup=True, id="ssh-log")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", variant="error", id="btn-cancel")
                yield Button("Setup SSH Key", variant="success", id="btn-setup")

    def on_mount(self) -> None:
        """Check for existing SSH keys and update the status widget."""
        keys = find_ssh_keys()
        status_widget = self.query_one("#ssh-key-status", Static)
        if keys:
            key_path = keys[0]
            status_widget.update(
                f"[green]\u2713[/green] SSH Key: [bold]{key_path}[/bold]"
            )
        else:
            status_widget.update(
                "[yellow]\u26a0[/yellow] No SSH key found \u2014 one will be generated"
            )
        self.query_one("#ssh-password-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "btn-cancel":
            self.dismiss(False)
        elif btn_id == "btn-setup":
            self._run_setup()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ssh-password-input":
            self._run_setup()

    def _log(self, message: str) -> None:
        """Write a message to the RichLog widget from the main thread."""
        try:
            self.query_one("#ssh-log", RichLog).write(message)
        except Exception:
            pass

    @work(thread=True)
    def _run_setup(self) -> None:
        """Perform SSH key setup in a background thread."""
        # Get the password from the input
        password = self.query_one("#ssh-password-input", Input).value.strip()
        if not password:
            self.app.call_from_thread(
                self._log, "[red]Please enter the root password.[/red]"
            )
            return

        # Disable the Setup button to prevent double-clicks
        def _disable_btn() -> None:
            btn = self.query_one("#btn-setup", Button)
            btn.disabled = True
            btn.label = "Setting up..."

        self.app.call_from_thread(_disable_btn)
        self.app.call_from_thread(
            self._log, "[bold cyan]Starting SSH key setup...[/bold cyan]"
        )

        # Step 1: Check for SSH keys; generate if none exist
        keys = find_ssh_keys()
        if not keys:
            self.app.call_from_thread(
                self._log, "Generating SSH keypair..."
            )
            ok, msg, pub_path = generate_ssh_key()
            if not ok:
                self.app.call_from_thread(
                    self._log, f"[red]Key generation failed: {msg}[/red]"
                )
                self.app.call_from_thread(self._re_enable_btn)
                return
            self.app.call_from_thread(
                self._log, f"[green]\u2713[/green] {msg}"
            )
            # Update the status widget with the new key
            if pub_path:
                self.app.call_from_thread(
                    self._update_key_status,
                    f"[green]\u2713[/green] SSH Key: [bold]{pub_path}[/bold]",
                )

        # Step 2: Ensure sshpass is available
        self.app.call_from_thread(
            self._log, "Ensuring sshpass is available..."
        )
        ok, msg = ensure_sshpass()
        if not ok:
            self.app.call_from_thread(
                self._log, f"[red]sshpass setup failed: {msg}[/red]"
            )
            self.app.call_from_thread(self._re_enable_btn)
            return
        self.app.call_from_thread(
            self._log, f"[green]\u2713[/green] sshpass: {msg}"
        )

        # Step 3: Copy the SSH key to the remote host
        self.app.call_from_thread(
            self._log,
            f"Copying SSH key to [bold]root@{self._host}[/bold]...",
        )
        ok, msg = copy_ssh_key(self._host, password)
        if ok:
            self.app.call_from_thread(
                self._log,
                f"[bold green]\u2713 {msg}[/bold green]",
            )
            self.app.call_from_thread(lambda: self.dismiss(True))
        else:
            self.app.call_from_thread(
                self._log, f"[red]\u2717 {msg}[/red]"
            )
            self.app.call_from_thread(self._re_enable_btn)

    def _re_enable_btn(self) -> None:
        """Re-enable the Setup button so the user can retry."""
        btn = self.query_one("#btn-setup", Button)
        btn.disabled = False
        btn.label = "Setup SSH Key"

    def _update_key_status(self, text: str) -> None:
        """Update the SSH key status Static widget."""
        self.query_one("#ssh-key-status", Static).update(text)
