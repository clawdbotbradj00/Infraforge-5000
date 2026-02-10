"""Host enrichment for Ansible run modal.

After a ping sweep discovers alive hosts, this module enriches each IP
with data from three sources:

1. **DNS** — reverse PTR lookup for hostname
2. **IPAM** — phpIPAM address search for hostname + description
3. **nmap** — OS detection via ``nmap -O`` (sudo) or ``-sV`` (fallback)

All functions are synchronous, designed to be called from Textual
``@work(thread=True)`` workers.  No Textual imports here — pure logic.
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HostInfo:
    """Enrichment data for a single IP address."""

    ip: str
    dns_hostname: str = ""
    ipam_hostname: str = ""
    ipam_description: str = ""
    os_guess: str = ""
    # Status tracking (simple strings: pending/running/done/skipped/error)
    dns_status: str = "pending"
    ipam_status: str = "pending"
    nmap_status: str = "pending"

    @property
    def best_hostname(self) -> str:
        """Return the best available hostname, preferring DNS over IPAM."""
        return self.dns_hostname or self.ipam_hostname or ""


# ---------------------------------------------------------------------------
# nmap OS detection
# ---------------------------------------------------------------------------

def check_nmap_available() -> tuple[bool, bool]:
    """Check if nmap is installed and if passwordless sudo works.

    Returns ``(nmap_found, sudo_works)``.
    """
    nmap_path = shutil.which("nmap")
    if not nmap_path:
        return False, False

    try:
        result = subprocess.run(
            ["sudo", "-n", "nmap", "-V"],
            capture_output=True, text=True, timeout=5,
        )
        sudo_works = result.returncode == 0
    except Exception:
        sudo_works = False

    return True, sudo_works


def nmap_os_detect(ip: str, sudo_works: bool = False) -> str:
    """Run nmap OS detection on a single IP.

    With sudo: ``nmap -O --osscan-guess`` for real OS fingerprinting.
    Without sudo: ``nmap -sV`` for service version detection (fallback).

    Returns the OS guess string or empty string on failure.
    """
    if sudo_works:
        cmd = [
            "sudo", "-n", "nmap", "-O", "--osscan-guess",
            "--top-ports", "20", "-T4", "--max-retries", "1", ip,
        ]
    else:
        cmd = [
            "nmap", "-sV", "--top-ports", "10", "-T4",
            "--max-retries", "1", ip,
        ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        return _parse_nmap_os(result.stdout, is_os_detect=sudo_works)
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return ""


def _parse_nmap_os(output: str, is_os_detect: bool) -> str:
    """Extract the OS guess from nmap output."""
    if is_os_detect:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("OS details:"):
                return line.split(":", 1)[1].strip().split(",")[0].strip()
            if line.startswith("Aggressive OS guesses:"):
                guesses = line.split(":", 1)[1].strip()
                first = guesses.split(",")[0].strip()
                if "(" in first:
                    first = first[: first.rfind("(")].strip()
                return first
    else:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Service Info:"):
                parts = line.split("OS:", 1)
                if len(parts) > 1:
                    return parts[1].split(";")[0].strip()
    return ""


# ---------------------------------------------------------------------------
# Per-source enrichment helpers
# ---------------------------------------------------------------------------

def _enrich_dns(ip: str, client: Any, info: HostInfo) -> tuple[str, str]:
    """Enrich a single IP with DNS reverse lookup."""
    info.dns_status = "running"
    try:
        hostname = client.reverse_lookup(ip)
        info.dns_hostname = hostname
        info.dns_status = "done"
    except Exception:
        info.dns_status = "error"
    return (ip, "dns")


def _enrich_ipam(ip: str, client: Any, info: HostInfo) -> tuple[str, str]:
    """Enrich a single IP with phpIPAM address data."""
    info.ipam_status = "running"
    try:
        addr = client.search_ip(ip)
        if addr:
            info.ipam_hostname = addr.get("hostname", "") or ""
            info.ipam_description = addr.get("description", "") or ""
        info.ipam_status = "done"
    except Exception:
        info.ipam_status = "error"
    return (ip, "ipam")


def _enrich_nmap(ip: str, info: HostInfo, sudo_works: bool) -> tuple[str, str]:
    """Enrich a single IP with nmap OS detection."""
    info.nmap_status = "running"
    try:
        os_guess = nmap_os_detect(ip, sudo_works=sudo_works)
        info.os_guess = os_guess
        info.nmap_status = "done"
    except Exception:
        info.nmap_status = "error"
    return (ip, "nmap")


# ---------------------------------------------------------------------------
# Enrichment orchestrator
# ---------------------------------------------------------------------------

def enrich_hosts(
    ips: list[str],
    dns_client: Any | None = None,
    ipam_client: Any | None = None,
    enable_nmap: bool = False,
    sudo_works: bool = False,
    callback: Callable[[str, HostInfo], None] | None = None,
) -> dict[str, HostInfo]:
    """Enrich a list of IPs with DNS, IPAM, and nmap data.

    Parameters
    ----------
    ips:
        IP addresses to enrich.
    dns_client:
        Optional ``DNSClient`` instance.  ``None`` skips DNS enrichment.
    ipam_client:
        Optional ``IPAMClient`` instance.  ``None`` skips IPAM enrichment.
    enable_nmap:
        Whether to run nmap OS detection.
    sudo_works:
        Whether passwordless sudo is available for nmap -O.
    callback:
        Called with ``(ip, host_info)`` after each source completes for
        a host, enabling progressive UI updates.

    Returns
    -------
    dict mapping IP -> HostInfo with all enrichment data populated.
    """
    if not ips:
        return {}

    results: dict[str, HostInfo] = {ip: HostInfo(ip=ip) for ip in ips}

    # Mark skipped sources
    for info in results.values():
        if not dns_client:
            info.dns_status = "skipped"
        if not ipam_client:
            info.ipam_status = "skipped"
        if not enable_nmap:
            info.nmap_status = "skipped"

    # Phase 1: DNS + IPAM in parallel (fast, network-bound)
    fast_tasks = []
    with ThreadPoolExecutor(max_workers=min(20, len(ips) * 2 or 1)) as pool:
        for ip in ips:
            if dns_client:
                fast_tasks.append(
                    pool.submit(_enrich_dns, ip, dns_client, results[ip])
                )
            if ipam_client:
                fast_tasks.append(
                    pool.submit(_enrich_ipam, ip, ipam_client, results[ip])
                )

        for future in as_completed(fast_tasks):
            try:
                ip_result = future.result()
                if callback and ip_result:
                    callback(ip_result[0], results[ip_result[0]])
            except Exception:
                pass

    # Phase 2: nmap (slow, needs rate-limiting)
    if enable_nmap:
        nmap_tasks = []
        with ThreadPoolExecutor(max_workers=min(5, len(ips) or 1)) as pool:
            for ip in ips:
                nmap_tasks.append(
                    pool.submit(_enrich_nmap, ip, results[ip], sudo_works)
                )

            for future in as_completed(nmap_tasks):
                try:
                    ip_result = future.result()
                    if callback and ip_result:
                        callback(ip_result[0], results[ip_result[0]])
                except Exception:
                    pass

    return results
