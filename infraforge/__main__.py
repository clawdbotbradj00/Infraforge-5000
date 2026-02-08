"""Entry point for InfraForge CLI."""

import sys
from pathlib import Path


def main():
    """Main entry point."""
    from infraforge.config import Config, ConfigError

    # Check for setup mode
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from infraforge.screens.setup_screen import run_setup_tui
        run_setup_tui()
        return

    # Check for update mode
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        from infraforge.updater import perform_update
        success = perform_update()
        sys.exit(0 if success else 1)

    # Check for version browser mode
    if (len(sys.argv) > 1 and sys.argv[1] == "versions") or \
       (len(sys.argv) > 2 and sys.argv[1] == "list" and sys.argv[2] == "versions"):
        from infraforge.screens.version_list_screen import run_version_browser
        run_version_browser()
        return

    # Check for deploy dns-server mode
    if len(sys.argv) > 2 and sys.argv[1] == "deploy" and sys.argv[2] == "dns-server":
        start_screen = "dns-server-wizard"
    else:
        start_screen = None

    # Load config
    try:
        config = Config.load()
    except ConfigError as e:
        from rich.console import Console
        console = Console()
        console.print(f"\n[bold red]Configuration Error:[/bold red] {e}")
        console.print(
            "\n[yellow]Run the setup wizard to configure InfraForge:[/yellow]"
            "\n  [bold]infraforge setup[/bold]"
            "\n  [dim]or[/dim]"
            "\n  [bold]python -m infraforge setup[/bold]"
            "\n\nAlternatively, copy the example config:"
            "\n  [bold]cp config/config.example.yaml ~/.config/infraforge/config.yaml[/bold]"
        )
        sys.exit(1)

    # Launch the TUI app
    from infraforge.app import InfraForgeApp
    app = InfraForgeApp(config=config, start_screen=start_screen)
    app.run()


if __name__ == "__main__":
    main()
