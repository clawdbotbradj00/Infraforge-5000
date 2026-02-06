"""BIND9 DNS client for InfraForge.

Uses dnspython for DNS queries, zone transfers, and dynamic updates
via TSIG-authenticated nsupdate protocol (RFC 2136).

Supports multi-zone DNS management: zone is passed per-method rather
than being fixed at construction time.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver
import dns.reversename
import dns.tsig
import dns.tsigkeyring
import dns.update
import dns.zone


class DNSError(Exception):
    """DNS operation error."""
    pass


@dataclass
class DNSRecord:
    """A single DNS record."""
    name: str       # e.g. "webserver" (relative) or "webserver.lab.local." (absolute)
    rtype: str      # e.g. "A", "AAAA", "CNAME", "PTR", "TXT"
    value: str      # e.g. "10.0.7.50"
    ttl: int = 3600
    zone: str = ""  # The zone this record belongs to


class DNSClient:
    """Client for interacting with a BIND9 DNS server via dynamic updates.

    Supports managing multiple DNS zones through a single client instance.
    Zone is passed as a parameter to every method rather than being fixed
    at construction time.

    Requires:
      - BIND9 server with ``allow-update`` or ``allow-transfer`` configured
        for the TSIG key
      - dnspython library (``pip install dnspython``)

    Constructor parameters:
      - server: IP/hostname of the BIND9 server
      - port: DNS port (default 53)
      - tsig_key_name: Name of the TSIG key
      - tsig_key_secret: Base64 TSIG key secret
      - tsig_algorithm: TSIG algorithm (default hmac-sha256)
    """

    def __init__(
        self,
        server: str,
        port: int = 53,
        tsig_key_name: str = "",
        tsig_key_secret: str = "",
        tsig_algorithm: str = "hmac-sha256",
    ):
        self.server = server
        self.port = port
        self._tsig_key_name = tsig_key_name
        self._tsig_key_secret = tsig_key_secret
        self._tsig_algorithm = tsig_algorithm

        # Build TSIG keyring
        self._keyring = None
        if self._tsig_key_name and self._tsig_key_secret:
            self._keyring = dns.tsigkeyring.from_text({
                self._tsig_key_name: self._tsig_key_secret,
            })

        self._tsig_algo = self._resolve_algorithm(self._tsig_algorithm)

    @classmethod
    def from_config(cls, config: Any) -> "DNSClient":
        """Create a DNSClient from an InfraForge Config object.

        Extracts DNS connection settings from ``config.dns`` and passes
        them as direct constructor arguments.
        """
        dns_cfg = config.dns
        return cls(
            server=dns_cfg.server,
            port=int(dns_cfg.port) if dns_cfg.port else 53,
            tsig_key_name=dns_cfg.tsig_key_name,
            tsig_key_secret=dns_cfg.tsig_key_secret,
            tsig_algorithm=dns_cfg.tsig_algorithm or "hmac-sha256",
        )

    @staticmethod
    def _resolve_algorithm(algo_str: str):
        """Convert algorithm string to dnspython constant."""
        algo_map = {
            "hmac-sha256": dns.tsig.HMAC_SHA256,
            "hmac-sha512": dns.tsig.HMAC_SHA512,
            "hmac-sha384": dns.tsig.HMAC_SHA384,
            "hmac-sha224": dns.tsig.HMAC_SHA224,
            "hmac-sha1": dns.tsig.HMAC_SHA1,
            "hmac-md5": dns.tsig.HMAC_MD5,
        }
        return algo_map.get(algo_str.lower(), dns.tsig.HMAC_SHA256)

    # ------------------------------------------------------------------
    # Health / connectivity
    # ------------------------------------------------------------------

    def check_health(self, zone: str) -> bool:
        """Check if the DNS server is reachable and responds to SOA queries
        for the given *zone*.
        """
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = [self.server]
            resolver.port = self.port
            resolver.lifetime = 5
            resolver.resolve(zone, "SOA")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Zone discovery / inspection
    # ------------------------------------------------------------------

    def check_zone(self, zone: str) -> dict | None:
        """Perform a SOA query for *zone* and return SOA details.

        Returns a dict with SOA fields if the zone exists and is reachable,
        or ``None`` if the zone cannot be found or is not accessible.
        """
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [self.server]
        resolver.port = self.port
        resolver.lifetime = 10

        try:
            answers = resolver.resolve(zone, "SOA")
            for rdata in answers:
                return {
                    "zone": zone,
                    "mname": str(rdata.mname),
                    "rname": str(rdata.rname),
                    "serial": rdata.serial,
                    "refresh": rdata.refresh,
                    "retry": rdata.retry,
                    "expire": rdata.expire,
                    "minimum": rdata.minimum,
                }
        except Exception:
            return None
        return None

    def get_server_zones(self, known_zones: list[str]) -> list[dict]:
        """Validate a list of zone names against the DNS server.

        For each zone in *known_zones*, performs a SOA query to determine
        whether the zone is served by this server.  This is how InfraForge
        discovers which zones are available.

        Returns a list of dicts, one per zone, each containing:
          - ``zone``: the zone name
          - ``reachable``: bool indicating whether the SOA query succeeded
          - ``soa``: dict of SOA fields (or ``None`` if unreachable)
        """
        results: list[dict] = []
        for zone_name in known_zones:
            soa = self.check_zone(zone_name)
            results.append({
                "zone": zone_name,
                "reachable": soa is not None,
                "soa": soa,
            })
        return results

    # ------------------------------------------------------------------
    # Zone creation (rndc addzone)
    # ------------------------------------------------------------------

    def create_zone(
        self,
        zone: str,
        master_ns: str,
        admin_email: str,
        serial: int = 1,
        refresh: int = 3600,
        retry: int = 900,
        expire: int = 604800,
        minimum: int = 86400,
        zone_file_path: str = "",
        rndc_path: str = "",
    ) -> None:
        """Create a new zone on the BIND9 server using ``rndc addzone``.

        This requires the BIND9 server to have ``allow-new-zones yes;``
        in its configuration.

        Parameters:
          zone: Zone name (e.g. ``"lab.local"``)
          master_ns: Primary nameserver FQDN (e.g. ``"ns1.lab.local."``)
          admin_email: Admin email in DNS format (e.g. ``"admin.lab.local."``)
          serial: Initial SOA serial number (default 1)
          refresh: SOA refresh interval in seconds
          retry: SOA retry interval in seconds
          expire: SOA expire interval in seconds
          minimum: SOA minimum TTL in seconds
          zone_file_path: Path for the zone file on the server.
              If empty, defaults to ``/var/lib/bind/<zone>.db``.
          rndc_path: Explicit path to the ``rndc`` binary.
              If empty, will be located via ``$PATH``.

        Raises:
          DNSError: If ``rndc`` is not found or the command fails.
        """
        # Locate rndc
        rndc_bin = rndc_path or shutil.which("rndc")
        if not rndc_bin:
            raise DNSError(
                "rndc binary not found. To create zones, install BIND9 "
                "utilities (e.g. 'apt install bind9utils' or "
                "'dnf install bind-utils') and ensure 'rndc' is on PATH, "
                "or pass rndc_path explicitly."
            )

        if not zone_file_path:
            zone_file_path = f"/var/lib/bind/{zone}.db"

        # Ensure trailing dot on NS and admin email for SOA
        if not master_ns.endswith("."):
            master_ns = master_ns + "."
        if not admin_email.endswith("."):
            admin_email = admin_email + "."

        # Build the zone configuration string for rndc addzone.
        # rndc addzone expects: zone "<name>" { type master; file "<path>"; };
        zone_config = (
            f'zone "{zone}" '
            f'{{ type master; file "{zone_file_path}"; '
            f'allow-update {{ any; }}; }};'
        )

        # Write a minimal zone file with SOA and NS records.
        # We write the zone file first, then tell BIND9 about it.
        soa_line = (
            f"@  IN SOA {master_ns} {admin_email} "
            f"( {serial} {refresh} {retry} {expire} {minimum} )"
        )
        zone_file_content = (
            f"$TTL 86400\n"
            f"$ORIGIN {zone}.\n"
            f"{soa_line}\n"
            f"@  IN NS  {master_ns}\n"
        )

        # Write the zone file
        try:
            with open(zone_file_path, "w") as fh:
                fh.write(zone_file_content)
        except OSError as e:
            raise DNSError(
                f"Failed to write zone file {zone_file_path}: {e}. "
                f"Ensure the directory exists and is writable by the "
                f"current user or run with appropriate permissions."
            )

        # Execute rndc addzone
        cmd = [rndc_bin, "-s", self.server, "addzone", zone, zone_config]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                raise DNSError(
                    f"rndc addzone failed (exit {result.returncode}): {stderr}"
                )
        except FileNotFoundError:
            raise DNSError(
                f"rndc binary not found at {rndc_bin}. "
                f"Install BIND9 utilities and try again."
            )
        except subprocess.TimeoutExpired:
            raise DNSError(
                f"rndc addzone timed out after 30 seconds when contacting "
                f"{self.server}."
            )
        except DNSError:
            raise
        except Exception as e:
            raise DNSError(f"rndc addzone failed: {e}")

    # ------------------------------------------------------------------
    # Record queries
    # ------------------------------------------------------------------

    def get_zone_records(self, zone: str) -> list[DNSRecord]:
        """Get all records in *zone* via AXFR (zone transfer).

        Requires the BIND9 server to allow transfers for our TSIG key.
        """
        records: list[DNSRecord] = []

        try:
            if self._keyring:
                xfr = dns.query.xfr(
                    self.server, zone,
                    keyring=self._keyring,
                    keyname=self._tsig_key_name,
                    keyalgorithm=self._tsig_algo,
                    port=self.port,
                    lifetime=15,
                )
            else:
                xfr = dns.query.xfr(
                    self.server, zone,
                    port=self.port,
                    lifetime=15,
                )

            zone_obj = dns.zone.from_xfr(xfr, relativize=True)

            for name, node in zone_obj.nodes.items():
                for rdataset in node.rdatasets:
                    rtype = dns.rdatatype.to_text(rdataset.rdtype)
                    for rdata in rdataset:
                        record_name = str(name)
                        if record_name == "@":
                            record_name = zone
                        records.append(DNSRecord(
                            name=record_name,
                            rtype=rtype,
                            value=str(rdata),
                            ttl=rdataset.ttl,
                            zone=zone,
                        ))

        except Exception as e:
            raise DNSError(f"Zone transfer failed for {zone}: {e}")

        return records

    def lookup_record(self, name: str, rtype: str = "A", zone: str = "") -> list[str]:
        """Query a specific record. Returns list of values.

        If *zone* is provided, the name is qualified against it.
        """
        fqdn = self._make_fqdn(name, zone) if zone else name
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [self.server]
        resolver.port = self.port
        resolver.lifetime = 10

        try:
            answers = resolver.resolve(fqdn, rtype)
            return [str(rdata) for rdata in answers]
        except dns.resolver.NXDOMAIN:
            return []
        except dns.resolver.NoAnswer:
            return []
        except Exception as e:
            raise DNSError(f"DNS lookup failed for {fqdn} {rtype}: {e}")

    def record_exists(self, name: str, rtype: str = "A", zone: str = "") -> bool:
        """Check if a DNS record exists."""
        return len(self.lookup_record(name, rtype, zone)) > 0

    # ------------------------------------------------------------------
    # Record management (dynamic updates via RFC 2136)
    # ------------------------------------------------------------------

    def create_record(
        self, name: str, rtype: str, value: str, ttl: int = 3600, zone: str = ""
    ) -> None:
        """Create a DNS record. Adds to existing records (does not replace).

        *zone* must be provided to identify which zone the update targets.
        """
        if not zone:
            raise DNSError("zone is required for create_record")
        update = self._make_update(zone)
        update.add(
            dns.name.from_text(name, None),
            ttl,
            rtype,
            value,
        )
        self._send_update(update)

    def update_record(
        self, name: str, rtype: str, value: str, ttl: int = 3600, zone: str = ""
    ) -> None:
        """Update a DNS record (replaces all existing records of this name+type).

        *zone* must be provided to identify which zone the update targets.
        """
        if not zone:
            raise DNSError("zone is required for update_record")
        update = self._make_update(zone)
        update.replace(
            dns.name.from_text(name, None),
            ttl,
            rtype,
            value,
        )
        self._send_update(update)

    def delete_record(
        self,
        name: str,
        rtype: str | None = None,
        value: str | None = None,
        zone: str = "",
    ) -> None:
        """Delete DNS record(s).

        - If rtype and value: delete specific record
        - If rtype only: delete all records of that type for the name
        - If neither: delete all records for the name

        *zone* must be provided to identify which zone the update targets.
        """
        if not zone:
            raise DNSError("zone is required for delete_record")
        update = self._make_update(zone)
        record_name = dns.name.from_text(name, None)

        if rtype and value:
            update.delete(record_name, rtype, value)
        elif rtype:
            update.delete(record_name, rtype)
        else:
            update.delete(record_name)

        self._send_update(update)

    def ensure_record(
        self, name: str, rtype: str, value: str, ttl: int = 3600, zone: str = ""
    ) -> str:
        """Create or update a record. Returns 'created', 'updated', or 'exists'.

        This is the primary method used by the New VM wizard.

        *zone* must be provided so the correct zone is queried and updated.
        """
        if not zone:
            raise DNSError("zone is required for ensure_record")
        existing = self.lookup_record(name, rtype, zone)

        if not existing:
            self.create_record(name, rtype, value, ttl, zone)
            return "created"

        if value in existing:
            return "exists"

        self.update_record(name, rtype, value, ttl, zone)
        return "updated"

    # ------------------------------------------------------------------
    # Zone info
    # ------------------------------------------------------------------

    def get_zone_soa(self, zone: str) -> dict:
        """Get SOA record for *zone* (serial, refresh, retry, etc)."""
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [self.server]
        resolver.port = self.port
        resolver.lifetime = 10

        try:
            answers = resolver.resolve(zone, "SOA")
            for rdata in answers:
                return {
                    "zone": zone,
                    "mname": str(rdata.mname),
                    "rname": str(rdata.rname),
                    "serial": rdata.serial,
                    "refresh": rdata.refresh,
                    "retry": rdata.retry,
                    "expire": rdata.expire,
                    "minimum": rdata.minimum,
                }
        except Exception as e:
            raise DNSError(f"Failed to get SOA for {zone}: {e}")
        return {}

    def get_record_count(self, zone: str) -> int:
        """Get approximate number of records in *zone*."""
        try:
            records = self.get_zone_records(zone)
            return len(records)
        except DNSError:
            return -1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_fqdn(self, name: str, zone: str) -> str:
        """Convert a short name to FQDN using *zone* if not already qualified."""
        if name.endswith("."):
            return name
        if "." not in name or not name.endswith(zone):
            return f"{name}.{zone}"
        return name

    def _make_update(self, zone: str) -> dns.update.Update:
        """Create a DNS Update message for *zone*, with TSIG if configured."""
        if self._keyring:
            return dns.update.Update(
                zone,
                keyring=self._keyring,
                keyname=self._tsig_key_name,
                keyalgorithm=self._tsig_algo,
            )
        return dns.update.Update(zone)

    def _send_update(self, update: dns.update.Update) -> None:
        """Send a dynamic DNS update to the server."""
        try:
            response = dns.query.tcp(update, self.server, port=self.port, timeout=10)
            rcode = response.rcode()
            if rcode != dns.rcode.NOERROR:
                rcode_text = dns.rcode.to_text(rcode)
                raise DNSError(f"DNS update failed: {rcode_text}")
        except dns.exception.DNSException as e:
            raise DNSError(f"DNS update failed: {e}")
        except DNSError:
            raise
        except Exception as e:
            raise DNSError(f"DNS update failed: {e}")
