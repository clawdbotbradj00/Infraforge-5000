"""BIND9 DNS client for InfraForge.

Uses dnspython for DNS queries, zone transfers, and dynamic updates
via TSIG-authenticated nsupdate protocol (RFC 2136).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import dns.name
import dns.query
import dns.resolver
import dns.tsigkeyring
import dns.update
import dns.zone
import dns.rdatatype
import dns.reversename


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


class DNSClient:
    """Client for interacting with a BIND9 DNS server via dynamic updates.

    Requires:
      - BIND9 server with ``allow-update`` or ``allow-transfer`` configured
        for the TSIG key
      - dnspython library (``pip install dnspython``)

    Config fields used (from DNSConfig):
      - server: IP/hostname of the BIND9 server
      - port: DNS port (default 53)
      - zone: DNS zone to manage (e.g. "lab.local")
      - tsig_key_name: Name of the TSIG key
      - tsig_key_secret: Base64 TSIG key secret
      - tsig_algorithm: TSIG algorithm (default hmac-sha256)
    """

    def __init__(self, config: Any):
        dns_cfg = config.dns
        self.server = dns_cfg.server
        self.port = int(dns_cfg.port) if dns_cfg.port else 53
        self.zone_name = dns_cfg.zone
        self.domain = dns_cfg.domain or dns_cfg.zone
        self._tsig_key_name = dns_cfg.tsig_key_name
        self._tsig_key_secret = dns_cfg.tsig_key_secret
        self._tsig_algorithm = dns_cfg.tsig_algorithm or "hmac-sha256"

        # Build TSIG keyring
        self._keyring = None
        if self._tsig_key_name and self._tsig_key_secret:
            self._keyring = dns.tsigkeyring.from_text({
                self._tsig_key_name: self._tsig_key_secret,
            })

        self._tsig_algo = self._resolve_algorithm(self._tsig_algorithm)

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

    def check_health(self) -> bool:
        """Check if the DNS server is reachable and responds to queries."""
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = [self.server]
            resolver.port = self.port
            resolver.lifetime = 5
            resolver.resolve(self.zone_name, "SOA")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Record queries
    # ------------------------------------------------------------------

    def get_zone_records(self) -> list[DNSRecord]:
        """Get all records in the zone via AXFR (zone transfer).

        Requires the BIND9 server to allow transfers for our TSIG key.
        """
        records = []
        zone_origin = dns.name.from_text(self.zone_name)

        try:
            if self._keyring:
                xfr = dns.query.xfr(
                    self.server, self.zone_name,
                    keyring=self._keyring,
                    keyname=self._tsig_key_name,
                    keyalgorithm=self._tsig_algo,
                    port=self.port,
                    lifetime=15,
                )
            else:
                xfr = dns.query.xfr(
                    self.server, self.zone_name,
                    port=self.port,
                    lifetime=15,
                )

            zone = dns.zone.from_xfr(xfr, relativize=True)

            for name, node in zone.nodes.items():
                for rdataset in node.rdatasets:
                    rtype = dns.rdatatype.to_text(rdataset.rdtype)
                    for rdata in rdataset:
                        record_name = str(name)
                        if record_name == "@":
                            record_name = self.zone_name
                        records.append(DNSRecord(
                            name=record_name,
                            rtype=rtype,
                            value=str(rdata),
                            ttl=rdataset.ttl,
                        ))

        except Exception as e:
            raise DNSError(f"Zone transfer failed for {self.zone_name}: {e}")

        return records

    def lookup_record(self, name: str, rtype: str = "A") -> list[str]:
        """Query a specific record. Returns list of values."""
        fqdn = self._make_fqdn(name)
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

    def record_exists(self, name: str, rtype: str = "A") -> bool:
        """Check if a DNS record exists."""
        return len(self.lookup_record(name, rtype)) > 0

    # ------------------------------------------------------------------
    # Record management (dynamic updates via RFC 2136)
    # ------------------------------------------------------------------

    def create_record(self, name: str, rtype: str, value: str, ttl: int = 3600) -> None:
        """Create a DNS record. Adds to existing records (does not replace)."""
        update = self._make_update()
        update.add(
            dns.name.from_text(name, None),
            ttl,
            rtype,
            value,
        )
        self._send_update(update)

    def update_record(self, name: str, rtype: str, value: str, ttl: int = 3600) -> None:
        """Update a DNS record (replaces all existing records of this name+type)."""
        update = self._make_update()
        update.replace(
            dns.name.from_text(name, None),
            ttl,
            rtype,
            value,
        )
        self._send_update(update)

    def delete_record(self, name: str, rtype: str | None = None, value: str | None = None) -> None:
        """Delete DNS record(s).

        - If rtype and value: delete specific record
        - If rtype only: delete all records of that type for the name
        - If neither: delete all records for the name
        """
        update = self._make_update()
        record_name = dns.name.from_text(name, None)

        if rtype and value:
            update.delete(record_name, rtype, value)
        elif rtype:
            update.delete(record_name, rtype)
        else:
            update.delete(record_name)

        self._send_update(update)

    def ensure_record(self, name: str, rtype: str, value: str, ttl: int = 3600) -> str:
        """Create or update a record. Returns 'created', 'updated', or 'exists'.

        This is the primary method used by the New VM wizard.
        """
        existing = self.lookup_record(name, rtype)

        if not existing:
            self.create_record(name, rtype, value, ttl)
            return "created"

        if value in existing:
            return "exists"

        self.update_record(name, rtype, value, ttl)
        return "updated"

    # ------------------------------------------------------------------
    # Zone info
    # ------------------------------------------------------------------

    def get_zone_soa(self) -> dict:
        """Get SOA record for the zone (serial, refresh, retry, etc)."""
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [self.server]
        resolver.port = self.port
        resolver.lifetime = 10

        try:
            answers = resolver.resolve(self.zone_name, "SOA")
            for rdata in answers:
                return {
                    "zone": self.zone_name,
                    "mname": str(rdata.mname),
                    "rname": str(rdata.rname),
                    "serial": rdata.serial,
                    "refresh": rdata.refresh,
                    "retry": rdata.retry,
                    "expire": rdata.expire,
                    "minimum": rdata.minimum,
                }
        except Exception as e:
            raise DNSError(f"Failed to get SOA for {self.zone_name}: {e}")
        return {}

    def get_record_count(self) -> int:
        """Get approximate number of records in the zone."""
        try:
            records = self.get_zone_records()
            return len(records)
        except DNSError:
            return -1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_fqdn(self, name: str) -> str:
        """Convert a short name to FQDN if not already."""
        if name.endswith("."):
            return name
        if "." not in name or not name.endswith(self.zone_name):
            return f"{name}.{self.zone_name}"
        return name

    def _make_update(self) -> dns.update.Update:
        """Create a DNS Update message with TSIG if configured."""
        if self._keyring:
            return dns.update.Update(
                self.zone_name,
                keyring=self._keyring,
                keyname=self._tsig_key_name,
                keyalgorithm=self._tsig_algo,
            )
        return dns.update.Update(self.zone_name)

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
        except Exception as e:
            raise DNSError(f"DNS update failed: {e}")
