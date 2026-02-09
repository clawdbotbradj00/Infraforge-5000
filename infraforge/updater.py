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
ALL_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=50"
CACHE_PATH = Path.home() / ".config" / "infraforge" / ".update_cache.json"
PIN_PATH = Path.home() / ".config" / "infraforge" / ".version_pin"
CACHE_TTL = 600  # 10 minutes


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


def _github_auth_header() -> dict:
    """Try to get GitHub auth from git credential store."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("password="):
                return {"Authorization": f"token {line.split('=', 1)[1]}"}
    except Exception:
        pass
    return {}


def fetch_all_releases() -> list[dict]:
    """Fetch all releases from GitHub API (up to 50).

    Returns a list of dicts with keys: tag, name, body, published, url, prerelease.
    Returns an empty list on any error.
    """
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "InfraForge",
        }
        headers.update(_github_auth_header())

        req = urllib.request.Request(ALL_RELEASES_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        releases = []
        for rel in data:
            published = rel.get("published_at", "")
            if published:
                published = published[:10]  # trim to just date
            releases.append({
                "tag": rel.get("tag_name", "").lstrip("vV"),
                "name": rel.get("name", ""),
                "body": rel.get("body", ""),
                "published": published,
                "url": rel.get("html_url", ""),
                "prerelease": rel.get("prerelease", False),
            })
        return releases
    except Exception:
        return []


def pin_version(version: str) -> None:
    """Write the pinned version string to the version pin file."""
    try:
        PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        PIN_PATH.write_text(version.strip())
    except Exception:
        pass


def unpin_version() -> None:
    """Delete the version pin file if it exists."""
    try:
        PIN_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def get_pinned_version() -> str | None:
    """Read and return the pinned version, or None if not pinned."""
    try:
        if PIN_PATH.exists():
            content = PIN_PATH.read_text().strip()
            if content:
                return content
    except Exception:
        pass
    return None


def perform_update_to_version(version: str) -> bool:
    """Check out a specific version tag and reinstall.

    Fetches tags, checks out the specified version, reinstalls the package,
    pins the version, and clears the update cache.

    Returns True on success.
    """
    from rich.console import Console
    console = Console()

    repo_root = _find_repo_root()
    if repo_root is None:
        console.print(
            "\n[bold red]Cannot update:[/bold red] "
            "InfraForge was not installed from a git clone."
            "\n\nTo update manually:"
            "\n  [bold]pip install --upgrade infraforge[/bold]"
            "\n  [dim]or re-clone the repository[/dim]"
        )
        return False

    console.print(f"\n[bold cyan]Updating InfraForge to v{version}...[/bold cyan]")
    console.print(f"  [dim]Repository: {repo_root}[/dim]\n")

    # git fetch --tags
    console.print("[bold]Fetching tags...[/bold]")
    result = subprocess.run(
        ["git", "fetch", "--tags", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]git fetch failed:[/red] {result.stderr.strip()}")
        return False

    # git checkout v{version} (try with and without v prefix)
    console.print(f"[bold]Checking out v{version}...[/bold]")
    result = subprocess.run(
        ["git", "checkout", f"v{version}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Try without v prefix
        result = subprocess.run(
            ["git", "checkout", version],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(
                f"[red]git checkout failed:[/red] {result.stderr.strip()}"
                f"\n[dim]Tag 'v{version}' or '{version}' not found.[/dim]"
            )
            return False
    console.print(f"  {result.stdout.strip()}")

    # pip install -e . -q
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

    # Pin the version
    pin_version(version)

    # Clear update cache
    try:
        CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass

    console.print(
        f"\n[bold green]Update complete![/bold green] "
        f"Now on version {version}. Restart InfraForge to use this version."
    )
    return True


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

    # Updating to latest means we are no longer pinned
    unpin_version()

    # Show new version
    console.print(
        f"\n[bold green]Update complete![/bold green] "
        f"Restart InfraForge to use the new version."
    )
    return True
