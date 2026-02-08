"""Persistent user preferences for InfraForge."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import logging
import yaml

logger = logging.getLogger(__name__)

PREFERENCES_PATH = Path.home() / ".config" / "infraforge" / "preferences.yaml"


@dataclass
class VMListPrefs:
    sort_field: str = "vmid"
    sort_reverse: bool = False
    filter_mode: str = "all"
    group_mode: str = "none"


@dataclass
class TemplateTabPrefs:
    sort_field: str = ""
    sort_reverse: bool = False
    group_mode: str = "none"


@dataclass
class TemplateListPrefs:
    vm: TemplateTabPrefs = field(default_factory=lambda: TemplateTabPrefs(sort_field="vmid"))
    ct: TemplateTabPrefs = field(default_factory=lambda: TemplateTabPrefs(sort_field="name"))
    iso: TemplateTabPrefs = field(default_factory=lambda: TemplateTabPrefs(sort_field="name"))


@dataclass
class TemplateUpdatePrefs:
    ip_address: str = ""
    subnet_mask: int = 24
    gateway: str = ""
    dns_server: str = ""
    vlan_tag: str = ""
    cpu_cores: int = 2
    ram_gb: int = 4


@dataclass
class Preferences:
    vm_list: VMListPrefs = field(default_factory=VMListPrefs)
    template_list: TemplateListPrefs = field(default_factory=TemplateListPrefs)
    template_update: TemplateUpdatePrefs = field(default_factory=TemplateUpdatePrefs)
    theme: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Preferences:
        path = path or PREFERENCES_PATH
        if not path.exists():
            return cls()
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            logger.warning("Could not read preferences file %s; using defaults", path)
            return cls()
        return cls._from_dict(data)

    def save(self, path: Path | None = None) -> None:
        path = path or PREFERENCES_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                yaml.dump(asdict(self), f, default_flow_style=False, sort_keys=False)
        except Exception:
            logger.warning("Could not write preferences file %s", path)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> Preferences:
        prefs = cls()
        if isinstance(data.get("vm_list"), dict):
            vl = data["vm_list"]
            prefs.vm_list = VMListPrefs(
                sort_field=str(vl.get("sort_field", "vmid")),
                sort_reverse=bool(vl.get("sort_reverse", False)),
                filter_mode=str(vl.get("filter_mode", "all")),
                group_mode=str(vl.get("group_mode", "none")),
            )
        if isinstance(data.get("template_list"), dict):
            tl = data["template_list"]
            for tab_key in ("vm", "ct", "iso"):
                if isinstance(tl.get(tab_key), dict):
                    tab_data = tl[tab_key]
                    defaults = getattr(TemplateListPrefs(), tab_key)
                    setattr(prefs.template_list, tab_key, TemplateTabPrefs(
                        sort_field=str(tab_data.get("sort_field", defaults.sort_field)),
                        sort_reverse=bool(tab_data.get("sort_reverse", False)),
                        group_mode=str(tab_data.get("group_mode", "none")),
                    ))
        if isinstance(data.get("template_update"), dict):
            tu = data["template_update"]
            prefs.template_update = TemplateUpdatePrefs(
                ip_address=str(tu.get("ip_address", "")),
                subnet_mask=int(tu.get("subnet_mask", 24)),
                gateway=str(tu.get("gateway", "")),
                dns_server=str(tu.get("dns_server", "")),
                vlan_tag=str(tu.get("vlan_tag", "")),
                cpu_cores=int(tu.get("cpu_cores", 2)),
                ram_gb=int(tu.get("ram_gb", 4)),
            )
        if data.get("theme"):
            prefs.theme = str(data["theme"])
        return prefs
