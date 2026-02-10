"""Module dependency map and availability checks for InfraForge."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infraforge.config import Config

# Nav item ID -> required modules
NAV_DEPENDENCIES: dict[str, list[str]] = {
    "nav-vms": ["proxmox"],
    "nav-templates": ["proxmox"],
    "nav-nodes": ["proxmox"],
    "nav-dns": ["dns"],
    "nav-ipam": ["ipam"],
    "nav-create": ["proxmox", "terraform"],
    "nav-ansible": ["ansible"],
    "nav-ai-settings": ["ai"],
}

# Human-readable display names
MODULE_NAMES: dict[str, str] = {
    "proxmox": "Proxmox",
    "dns": "DNS",
    "ipam": "IPAM",
    "terraform": "Terraform",
    "ansible": "Ansible",
    "ai": "AI",
}


def check_module_available(config: Config, module: str) -> bool:
    """Check whether a module is functional enough to use."""
    if module == "proxmox":
        return bool(config.proxmox.host)
    elif module == "dns":
        if not config.dns.provider:
            return False
        if config.dns.provider == "bind9" and not config.dns.server:
            return False
        return True
    elif module == "ipam":
        return bool(config.ipam.url)
    elif module == "terraform":
        return shutil.which("terraform") is not None
    elif module == "ansible":
        return shutil.which("ansible") is not None
    elif module == "ai":
        return bool(config.ai.api_key)
    return False


def get_all_module_status(config: Config) -> dict[str, bool]:
    """Return availability status for all modules."""
    return {
        m: check_module_available(config, m)
        for m in MODULE_NAMES
    }


def get_disabled_nav_items(config: Config) -> dict[str, list[str]]:
    """Return nav_id -> list of missing module names for disabled items."""
    status = get_all_module_status(config)
    disabled: dict[str, list[str]] = {}
    for nav_id, deps in NAV_DEPENDENCIES.items():
        missing = [m for m in deps if not status.get(m, False)]
        if missing:
            disabled[nav_id] = missing
    return disabled
