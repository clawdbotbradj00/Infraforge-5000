"""InfraForge Package (.ifpkg) format handling.

An .ifpkg file is a tar archive containing:
  - manifest.json  — metadata about the template
  - backup.vma.zst — the vzdump backup (VMA format, zstd compressed)
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from infraforge import __version__

# Default exports directory (XDG-compliant)
_EXPORTS_DIR = Path.home() / ".local" / "share" / "infraforge" / "exports"

MANIFEST_FORMAT = "ifpkg-v1"
MANIFEST_FILENAME = "manifest.json"
BACKUP_FILENAME = "backup.vma.zst"


def get_exports_dir() -> Path:
    """Return the exports directory, creating it if needed."""
    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return _EXPORTS_DIR


def create_package(
    vma_path: Path,
    template_name: str,
    original_vmid: int,
    original_node: str,
    output_path: Path,
    description: str = "",
) -> Path:
    """Create an .ifpkg tar from a VMA backup file.

    Args:
        vma_path: Path to the local .vma.zst backup file.
        template_name: Name of the source template.
        original_vmid: VMID of the source template.
        original_node: Proxmox node the template lived on.
        output_path: Where to write the .ifpkg file.
        description: Optional description for the manifest.

    Returns:
        Path to the created .ifpkg file.
    """
    manifest = {
        "format": MANIFEST_FORMAT,
        "template_name": template_name,
        "original_vmid": original_vmid,
        "original_node": original_node,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "infraforge_version": __version__,
        "backup_filename": BACKUP_FILENAME,
        "disk_size_bytes": vma_path.stat().st_size,
        "description": description,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(output_path, "w") as tar:
        # Add manifest.json
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        import io

        info = tarfile.TarInfo(name=MANIFEST_FILENAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

        # Add the VMA backup
        tar.add(str(vma_path), arcname=BACKUP_FILENAME)

    return output_path


def read_manifest(package_path: Path) -> Optional[dict]:
    """Read and validate manifest.json from an .ifpkg file.

    Returns:
        The manifest dict, or None if the file is invalid.
    """
    try:
        with tarfile.open(package_path, "r") as tar:
            try:
                member = tar.getmember(MANIFEST_FILENAME)
            except KeyError:
                return None
            f = tar.extractfile(member)
            if f is None:
                return None
            data = json.loads(f.read().decode("utf-8"))
            if not isinstance(data, dict):
                return None
            if data.get("format") != MANIFEST_FORMAT:
                return None
            # Check required fields
            for key in ("template_name", "backup_filename"):
                if key not in data:
                    return None
            return data
    except (tarfile.TarError, json.JSONDecodeError, OSError):
        return None


def extract_backup(package_path: Path, dest_dir: Path) -> Path:
    """Extract the VMA backup file from an .ifpkg package.

    Args:
        package_path: Path to the .ifpkg file.
        dest_dir: Directory to extract the backup into.

    Returns:
        Path to the extracted VMA file.

    Raises:
        ValueError: If the package is invalid or missing the backup.
    """
    manifest = read_manifest(package_path)
    if manifest is None:
        raise ValueError(f"Invalid .ifpkg package: {package_path}")

    backup_name = manifest.get("backup_filename", BACKUP_FILENAME)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / backup_name

    with tarfile.open(package_path, "r") as tar:
        try:
            member = tar.getmember(backup_name)
        except KeyError:
            raise ValueError(f"Package missing backup file: {backup_name}")

        # Security: ensure no path traversal
        if member.name != backup_name or ".." in member.name or member.name.startswith("/"):
            raise ValueError(f"Unsafe path in package: {member.name}")

        with tar.extractfile(member) as src, open(dest_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                dst.write(chunk)

    return dest_path


def scan_packages(directory: Optional[Path] = None) -> list[dict]:
    """Scan a directory for .ifpkg files and read their manifests.

    Args:
        directory: Directory to scan. Defaults to the exports directory.

    Returns:
        List of dicts with keys: path (Path), manifest (dict).
        Sorted by creation date, newest first.
    """
    if directory is None:
        directory = get_exports_dir()

    if not directory.is_dir():
        return []

    packages = []
    for p in directory.glob("*.ifpkg"):
        manifest = read_manifest(p)
        if manifest is not None:
            packages.append({
                "path": p,
                "manifest": manifest,
            })

    # Sort by creation date, newest first
    packages.sort(
        key=lambda x: x["manifest"].get("created_at", ""),
        reverse=True,
    )
    return packages
