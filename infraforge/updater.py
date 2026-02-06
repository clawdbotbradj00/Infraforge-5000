"""Auto-update checker and updater for InfraForge."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GITHUB_REPO = "clawdbotbradj00/InfraForge"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CACHE_PATH = Path.home() / ".config" / "infraforge" / ".update_cache.json"
CACHE_TTL = 3600  # 1 hour


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '0.1.0' or 'v0.1.0' into a tuple."""
    v = v.lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) or (0,)


def _read_cache() -> dict | None:
    """Read cached update check result if fresh enough."""
    try:
        if not CACHE_PATH.exists():
            return None
        data = json.loads(CACHE_PATH.read_text())
        if time.time() - data.get("checked_at", 0) < CACHE_TTL:
            return data.get("result")
    except Exception:
        pass
    return None


def _write_cache(result: dict | None) -> None:
    """Cache update check result."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "checked_at": time.time(),
            "result": result,
        }))
    except Exception:
        pass


def check_for_update(skip_cache: bool = False) -> dict | None:
    """Check GitHub for a newer release.

    Returns a dict with 'latest', 'current', 'url', 'body' keys if an
    update is available, or None if current version is up-to-date.
    Silently returns None on any error.
    """
    if not skip_cache:
        cached = _read_cache()
        if cached is not None:
            return cached if cached else None

    try:
        from infraforge import __version__

        req = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "InfraForge"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        tag = data.get("tag_name", "")
        latest = _parse_version(tag)
        current = _parse_version(__version__)

        if latest > current:
            result = {
                "latest": tag.lstrip("vV"),
                "current": __version__,
                "url": data.get("html_url", ""),
                "body": data.get("body", ""),
            }
        else:
            result = None

        _write_cache(result or {})
        return result

    except Exception:
        _write_cache({})
        return None


def _find_repo_root() -> Path | None:
    """Find the git repo root for the installed package."""
    pkg_dir = Path(__file__).resolve().parent
    # Walk up looking for .git
    for parent in [pkg_dir] + list(pkg_dir.parents):
        if (parent / ".git").exists():
            return parent
    return None


def perform_update() -> bool:
    """Pull latest code and reinstall.

    Returns True on success.
    """
    from rich.console import Console
    console = Console()

    repo_root = _find_repo_root()
    if repo_root is None:
        console.print(
            "\n[bold red]Cannot auto-update:[/bold red] "
            "InfraForge was not installed from a git clone."
            "\n\nTo update manually:"
            "\n  [bold]pip install --upgrade infraforge[/bold]"
            "\n  [dim]or re-clone the repository[/dim]"
        )
        return False

    console.print(f"\n[bold cyan]Updating InfraForge...[/bold cyan]")
    console.print(f"  [dim]Repository: {repo_root}[/dim]\n")

    # git pull
    console.print("[bold]Pulling latest changes...[/bold]")
    result = subprocess.run(
        ["git", "pull", "origin", "main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]git pull failed:[/red] {result.stderr.strip()}")
        return False
    console.print(f"  {result.stdout.strip()}")

    # pip install -e .
    console.print("\n[bold]Reinstalling package...[/bold]")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "-q"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]pip install failed:[/red] {result.stderr.strip()}")
        return False

    # Clear update cache so banner disappears on next launch
    try:
        CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass

    # Show new version
    console.print(
        f"\n[bold green]Update complete![/bold green] "
        f"Restart InfraForge to use the new version."
    )
    return True
