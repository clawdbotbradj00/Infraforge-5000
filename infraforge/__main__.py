"""Entry point for InfraForge CLI."""

import sys
from pathlib import Path


def main():
    """Main entry point."""
    from infraforge.config import Config, ConfigError

    # Check for setup mode
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from infraforge.setup_wizard import run_setup_wizard
        run_setup_wizard()
        return

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
    app = InfraForgeApp(config=config)
    app.run()


if __name__ == "__main__":
    main()
