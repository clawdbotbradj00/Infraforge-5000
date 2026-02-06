"""Gather live infrastructure context for the AI copilot.

Before each AI message we call ``gather_context(app)`` to build a plain-text
snapshot of every data source the user can see in the TUI.  This text is
prepended to the user's prompt so the model already has the data and never
needs to "fetch" anything via tool calls.

All backend calls are issued in parallel via a ThreadPoolExecutor, and
results are cached for 30 seconds to avoid hammering the APIs on rapid
successive messages.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infraforge.app import InfraForgeApp

from infraforge.models import VMStatus

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_timestamp: float = 0.0
_cache_result: str = ""
_CACHE_TTL: float = 30.0  # seconds


# ---------------------------------------------------------------------------
# Individual data-source formatters
# ---------------------------------------------------------------------------

def _fetch_vms(app: "InfraForgeApp") -> str:
    """Fetch and format all virtual machines."""
    try:
        vms = app.proxmox.get_all_vms()
    except Exception as exc:
        return f"=== VIRTUAL MACHINES ===\n  Error: {exc}\n"

    total = len(vms)
    counts: dict[str, int] = {}
    for vm in vms:
        status_val = vm.status.value if isinstance(vm.status, VMStatus) else str(vm.status)
        counts[status_val] = counts.get(status_val, 0) + 1

    summary_parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    summary = ", ".join(summary_parts)

    lines: list[str] = []
    lines.append(f"=== VIRTUAL MACHINES ({total} total: {summary}) ===")
    lines.append(f"  {'VMID':<6}{'Name':<21}{'Type':<6}{'Status':<9}{'Node':<7}{'CPU%':<7}{'Mem(GB)':<8}")

    for vm in sorted(vms, key=lambda v: v.vmid):
        status_val = vm.status.value if isinstance(vm.status, VMStatus) else str(vm.status)
        vtype = vm.vm_type.value if hasattr(vm.vm_type, "value") else str(vm.vm_type)
        lines.append(
            f"  {vm.vmid:<6}{vm.name:<21}{vtype:<6}{status_val:<9}{vm.node:<7}"
            f"{vm.cpu_percent:<7.1f}{vm.mem_gb:<8.1f}"
        )

    lines.append("")
    return "\n".join(lines)


def _fetch_nodes(app: "InfraForgeApp") -> str:
    """Fetch and format cluster node information."""
    try:
        nodes = app.proxmox.get_node_info()
    except Exception as exc:
        return f"=== CLUSTER NODES ===\n  Error: {exc}\n"

    lines: list[str] = []

    # Proxmox version
    try:
        ver = app.proxmox.get_version()
        pve_ver = ver.get("version", "?")
        pve_rel = ver.get("release", "")
        lines.append(f"=== PROXMOX VE {pve_ver} {pve_rel} ===")
    except Exception:
        pass

    lines.append("=== CLUSTER NODES ===")
    lines.append(
        f"  {'Node':<8}{'Status':<9}{'CPU%':<7}{'Mem%':<7}{'Disk%':<7}{'Uptime':<12}"
    )

    for node in sorted(nodes, key=lambda n: n.node):
        lines.append(
            f"  {node.node:<8}{node.status:<9}{node.cpu_percent:<7.1f}"
            f"{node.mem_percent:<7.1f}{node.disk_percent:<7.1f}{node.uptime_str:<12}"
        )

    lines.append("")
    return "\n".join(lines)


def _fetch_templates(app: "InfraForgeApp") -> str:
    """Fetch and format VM/CT templates."""
    try:
        _vms, templates = app.proxmox.get_all_vms_and_templates()
    except Exception as exc:
        return f"=== TEMPLATES ===\n  Error: {exc}\n"

    lines: list[str] = []
    lines.append("=== TEMPLATES ===")

    if not templates:
        lines.append("  (none)")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"  {'VMID':<6}{'Name':<21}{'Node':<7}{'Type':<12}")

    for tpl in sorted(templates, key=lambda t: (t.vmid or 0)):
        vmid_str = str(tpl.vmid) if tpl.vmid is not None else "-"
        lines.append(
            f"  {vmid_str:<6}{tpl.name:<21}{tpl.node:<7}{tpl.type_label:<12}"
        )

    lines.append("")
    return "\n".join(lines)


def _fetch_ipam(app: "InfraForgeApp") -> str:
    """Fetch and format IPAM sections, subnets, VLANs and addresses."""
    # Check if IPAM is configured
    ipam_cfg = getattr(app.config, "ipam", None)
    if ipam_cfg is None or not getattr(ipam_cfg, "url", ""):
        return "=== IPAM ===\n  Not configured\n"

    try:
        from infraforge.ipam_client import IPAMClient
    except ImportError:
        return "=== IPAM ===\n  Not configured (ipam_client not available)\n"

    try:
        client = IPAMClient(app.config)
    except Exception as exc:
        return f"=== IPAM ===\n  Error: {exc}\n"

    parts: list[str] = []

    # ── Sections (needed for create_subnet) ──
    try:
        sections = client.get_sections()
    except Exception:
        sections = []

    sect_lines: list[str] = ["=== IPAM SECTIONS ==="]
    if sections:
        sect_lines.append(f"  {'ID':<5}{'Name':<25}{'Description'}")
        for s in sections:
            sid = s.get("id", "?")
            sname = s.get("name", "?")
            sdesc = s.get("description", "")
            sect_lines.append(f"  {sid:<5}{sname:<25}{sdesc}")
    else:
        sect_lines.append("  (none)")
    sect_lines.append("")
    parts.append("\n".join(sect_lines))

    # ── VLANs ──
    try:
        vlans = client.get_vlans()
    except Exception:
        vlans = []

    if vlans:
        vlan_lines: list[str] = ["=== IPAM VLANS ==="]
        vlan_lines.append(f"  {'ID':<5}{'Number':<8}{'Name':<20}{'Description'}")
        for v in vlans[:50]:
            vid = v.get("vlanId", "?")
            vnum = v.get("number", "?")
            vname = v.get("name", "")
            vdesc = v.get("description", "")
            vlan_lines.append(f"  {vid:<5}{vnum:<8}{vname:<20}{vdesc}")
        if len(vlans) > 50:
            vlan_lines.append(f"  ... and {len(vlans) - 50} more VLANs")
        vlan_lines.append("")
        parts.append("\n".join(vlan_lines))

    # ── Subnets + addresses ──
    try:
        subnets = client.get_subnets()
    except Exception as exc:
        parts.append(f"=== IPAM SUBNETS ===\n  Error: {exc}\n")
        return "\n".join(parts)

    sub_lines: list[str] = ["=== IPAM SUBNETS ==="]

    if not subnets:
        sub_lines.append("  (none)")
        sub_lines.append("")
        parts.append("\n".join(sub_lines))
        return "\n".join(parts)

    for subnet in subnets:
        subnet_addr = subnet.get("subnet", "?")
        mask = subnet.get("mask", "?")
        subnet_id = subnet.get("id", "?")
        section_id = subnet.get("sectionId", "?")
        description = subnet.get("description", "")
        usage = subnet.get("usage", {})
        used = usage.get("used", "?")
        maxhosts = usage.get("maxhosts", "?")

        desc_part = f' "{description}"' if description else ""
        sub_lines.append(
            f"  id={subnet_id} section={section_id} "
            f"{subnet_addr}/{mask}{desc_part} ({used}/{maxhosts} used)"
        )

        # Fetch addresses for this subnet (limit to 30)
        if subnet_id and subnet_id != "?":
            try:
                addresses = client.get_subnet_addresses(str(subnet_id))
                for addr in addresses[:30]:
                    aid = addr.get("id", "?")
                    ip = addr.get("ip", "?")
                    hostname = addr.get("hostname", "")
                    tag = addr.get("tag", "")
                    tag_str = tag if isinstance(tag, str) else str(tag)
                    sub_lines.append(f"    id={aid:<4} {ip:<17}{hostname:<20}{tag_str}")
                if len(addresses) > 30:
                    sub_lines.append(f"    ... and {len(addresses) - 30} more addresses")
            except Exception:
                sub_lines.append("    (could not fetch addresses)")

    sub_lines.append("")
    parts.append("\n".join(sub_lines))
    return "\n".join(parts)


def _fetch_dns(app: "InfraForgeApp") -> str:
    """Fetch and format DNS zone records."""
    # Check if DNS is configured
    dns_cfg = getattr(app.config, "dns", None)
    if dns_cfg is None or not getattr(dns_cfg, "server", ""):
        return "=== DNS ===\n  Not configured\n"

    try:
        from infraforge.dns_client import DNSClient
    except ImportError:
        return "=== DNS ===\n  Not configured (dnspython not installed)\n"

    try:
        client = DNSClient(
            server=dns_cfg.server,
            port=dns_cfg.port,
            tsig_key_name=dns_cfg.tsig_key_name,
            tsig_key_secret=dns_cfg.tsig_key_secret,
            tsig_algorithm=dns_cfg.tsig_algorithm,
        )
    except Exception as exc:
        return f"=== DNS ===\n  Error creating client: {exc}\n"

    # Determine zones to query
    zones = list(dns_cfg.zones) if dns_cfg.zones else []
    if not zones:
        try:
            zones = client.discover_zones()
        except Exception:
            zones = []

    if not zones:
        return "=== DNS ===\n  No zones discovered\n"

    sections: list[str] = []

    for zone in zones:
        try:
            records = client.get_zone_records(zone)
        except Exception as exc:
            sections.append(f"=== DNS: {zone} ===\n  Error: {exc}\n")
            continue

        total = len(records)
        lines: list[str] = []
        lines.append(f"=== DNS: {zone} ({total} records) ===")

        for rec in records[:75]:
            lines.append(
                f"  {rec.rtype:<7}{rec.name:<12}{rec.value:<27}TTL={rec.ttl}"
            )
        if total > 75:
            lines.append(f"  ... and {total - 75} more records")

        lines.append("")
        sections.append("\n".join(lines))

    return "\n".join(sections) if sections else "=== DNS ===\n  No zones discovered\n"


def _fetch_storage(app: "InfraForgeApp") -> str:
    """Fetch and format storage pool summary."""
    try:
        storages = app.proxmox.get_storage_info()
    except Exception as exc:
        return f"=== STORAGE ===\n  Error: {exc}\n"

    if not storages:
        return ""

    lines: list[str] = ["=== STORAGE ==="]
    lines.append(
        f"  {'Node':<8}{'Pool':<14}{'Type':<8}{'Used':<10}{'Total':<10}{'%':<5}{'Content'}"
    )

    for s in sorted(storages, key=lambda x: (x.node, x.storage)):
        total_gb = s.total / (1024**3) if s.total else 0
        used_gb = s.used / (1024**3) if s.used else 0
        pct = (s.used / s.total * 100) if s.total else 0
        lines.append(
            f"  {s.node:<8}{s.storage:<14}{s.storage_type:<8}"
            f"{used_gb:<10.1f}{total_gb:<10.1f}{pct:<5.0f}{s.content}"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def gather_context(app: "InfraForgeApp") -> str:
    """Gather a plain-text snapshot of all live infrastructure data.

    Returns a formatted string suitable for prepending to an AI prompt.
    Results are cached for 30 seconds to avoid excessive API calls.
    """
    global _cache_timestamp, _cache_result

    now = time.monotonic()

    with _cache_lock:
        if _cache_result and (now - _cache_timestamp) < _CACHE_TTL:
            return _cache_result

    # Fetch all sources in parallel
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures: dict[Future, str] = {
            executor.submit(_fetch_vms, app): "vms",
            executor.submit(_fetch_nodes, app): "nodes",
            executor.submit(_fetch_templates, app): "templates",
            executor.submit(_fetch_storage, app): "storage",
            executor.submit(_fetch_ipam, app): "ipam",
            executor.submit(_fetch_dns, app): "dns",
        }

        for future in futures:
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = f"=== {key.upper()} ===\n  Error: {exc}\n"

    # Assemble in a stable order
    output = "\n".join([
        results.get("vms", ""),
        results.get("nodes", ""),
        results.get("storage", ""),
        results.get("templates", ""),
        results.get("ipam", ""),
        results.get("dns", ""),
    ])

    with _cache_lock:
        _cache_timestamp = time.monotonic()
        _cache_result = output

    return output
