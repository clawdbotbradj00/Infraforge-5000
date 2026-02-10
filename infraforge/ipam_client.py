"""phpIPAM API client for InfraForge."""

from __future__ import annotations

from typing import Any

import requests

from infraforge.config import Config


class IPAMError(Exception):
    """IPAM API error."""
    pass


class IPAMClient:
    """Client for interacting with phpIPAM REST API.

    phpIPAM exposes a REST API (typically at ``/api/{app_id}/...``).
    Authentication can use either:
      - An API token (``token`` in config)
      - Username/password to obtain a session token

    See: https://phpipam.net/api/api_documentation/
    """

    def __init__(self, config: Config):
        self.config = config
        icfg = config.ipam
        self.base_url = icfg.url.rstrip("/")
        self.app_id = icfg.app_id or "infraforge"
        self._token: str | None = icfg.token or None
        self._username = icfg.username
        self._password = icfg.password
        self._verify_ssl = icfg.verify_ssl
        self._session = requests.Session()
        self._session.verify = self._verify_ssl

        if not self._verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/api/{self.app_id}"

    def _ensure_auth(self) -> None:
        """Ensure we have a valid API token."""
        if self._token:
            return

        if not self._username or not self._password:
            raise IPAMError(
                "phpIPAM requires either an API token or username/password. "
                "Check your config."
            )

        # Authenticate via user credentials to get a token
        url = f"{self.api_base}/user/"
        try:
            resp = self._session.post(
                url,
                auth=(self._username, self._password),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                raise IPAMError(f"phpIPAM auth failed: {data.get('message', 'unknown error')}")

            self._token = data["data"]["token"]
        except requests.RequestException as e:
            raise IPAMError(f"Failed to authenticate with phpIPAM: {e}")

    def _headers(self) -> dict[str, str]:
        self._ensure_auth()
        return {
            "token": self._token or "",
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """Perform a GET request against the phpIPAM API."""
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.get(url, headers=self._headers(), params=params, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise IPAMError(f"phpIPAM API error: {body.get('message', 'unknown')}")
            return body.get("data", [])
        except requests.RequestException as e:
            raise IPAMError(f"phpIPAM request failed ({endpoint}): {e}")

    def _post(self, endpoint: str, payload: dict | None = None) -> Any:
        """Perform a POST request against the phpIPAM API."""
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.post(
                url, headers=self._headers(), json=payload or {}, timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise IPAMError(f"phpIPAM API error: {body.get('message', 'unknown')}")
            return body.get("data", {})
        except requests.RequestException as e:
            raise IPAMError(f"phpIPAM request failed ({endpoint}): {e}")

    def _patch(self, endpoint: str, payload: dict | None = None) -> Any:
        """Perform a PATCH request against the phpIPAM API."""
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.patch(
                url, headers=self._headers(), json=payload or {}, timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise IPAMError(f"phpIPAM API error: {body.get('message', 'unknown')}")
            return body.get("data", {})
        except requests.RequestException as e:
            raise IPAMError(f"phpIPAM request failed ({endpoint}): {e}")

    def _delete(self, endpoint: str) -> Any:
        """Perform a DELETE request against the phpIPAM API."""
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.delete(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise IPAMError(f"phpIPAM API error: {body.get('message', 'unknown')}")
            return body.get("data", {})
        except requests.RequestException as e:
            raise IPAMError(f"phpIPAM request failed ({endpoint}): {e}")

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def get_sections(self) -> list[dict]:
        """Get all IPAM sections."""
        return self._get("/sections/")

    # ------------------------------------------------------------------
    # Subnets
    # ------------------------------------------------------------------

    def get_subnets(self, section_id: int | str | None = None) -> list[dict]:
        """Get subnets, optionally filtered by section.

        Each subnet dict includes keys like:
            id, subnet, mask, sectionId, description, vlanId,
            masterSubnetId, usage (dict with used, maxhosts, freehosts_percent), etc.
        """
        if section_id is not None:
            data = self._get(f"/sections/{section_id}/subnets/")
        else:
            # Get all sections first, then aggregate subnets
            sections = self.get_sections()
            data = []
            for section in sections:
                try:
                    section_subnets = self._get(f"/sections/{section['id']}/subnets/")
                    if isinstance(section_subnets, list):
                        data.extend(section_subnets)
                except IPAMError:
                    continue

        # Enrich each subnet with usage data
        enriched = []
        for subnet in (data if isinstance(data, list) else []):
            try:
                usage = self._get(f"/subnets/{subnet['id']}/usage/")
                subnet["usage"] = usage if isinstance(usage, dict) else {}
            except IPAMError:
                subnet["usage"] = {}
            enriched.append(subnet)

        return enriched

    def get_subnet(self, subnet_id: int | str) -> dict:
        """Get a single subnet by ID."""
        return self._get(f"/subnets/{subnet_id}/")

    def get_subnet_addresses(self, subnet_id: int | str) -> list[dict]:
        """Get all addresses in a subnet."""
        try:
            return self._get(f"/subnets/{subnet_id}/addresses/")
        except IPAMError:
            return []

    # ------------------------------------------------------------------
    # IP Addresses
    # ------------------------------------------------------------------

    def get_available_ips(self, subnet_id: int | str, count: int = 20) -> list[str]:
        """Get available (free) IP addresses in a subnet.

        phpIPAM doesn't have a direct "list N free IPs" endpoint, so we use
        the first-free endpoint repeatedly, or calculate from the address list.
        """
        # Method: get all addresses in the subnet and find gaps
        subnet = self.get_subnet(subnet_id)
        if not isinstance(subnet, dict):
            return []

        import ipaddress

        network_str = f"{subnet.get('subnet', '0.0.0.0')}/{subnet.get('mask', 24)}"
        try:
            network = ipaddress.ip_network(network_str, strict=False)
        except ValueError:
            return []

        existing = self.get_subnet_addresses(subnet_id)
        used_ips = set()
        for addr in existing:
            try:
                used_ips.add(ipaddress.ip_address(addr.get("ip", "")))
            except ValueError:
                continue

        available = []
        for host in network.hosts():
            if host not in used_ips:
                available.append(str(host))
                if len(available) >= count:
                    break

        return available

    def get_first_free_ip(self, subnet_id: int | str) -> str:
        """Get the first available IP in a subnet."""
        try:
            result = self._get(f"/subnets/{subnet_id}/first_free/")
            if isinstance(result, str):
                return result
            return str(result)
        except IPAMError:
            # Fallback to manual calculation
            ips = self.get_available_ips(subnet_id, count=1)
            return ips[0] if ips else ""

    def search_ip(self, ip: str) -> dict | None:
        """Search for an IP address across all subnets.

        Uses the phpIPAM ``/addresses/search/{ip}/`` endpoint.
        Returns the address dict (with ``ip``, ``hostname``,
        ``description``, ``subnetId``, etc.) or ``None`` if not found.
        """
        try:
            result = self._get(f"/addresses/search/{ip}/")
            if isinstance(result, list) and result:
                return result[0]
            if isinstance(result, dict):
                return result
        except IPAMError:
            pass
        return None

    def create_address(
        self,
        ip: str,
        subnet_id: int | str,
        hostname: str = "",
        description: str = "",
        tag: int = 2,  # 2 = "Used" in phpIPAM default tags
    ) -> dict:
        """Reserve/create an IP address in phpIPAM.

        This will be called after VM creation to register the IP.
        """
        payload = {
            "ip": ip,
            "subnetId": str(subnet_id),
            "hostname": hostname,
            "description": description or f"Created by InfraForge",
            "tag": str(tag),
        }
        return self._post("/addresses/", payload)

    def delete_address(self, address_id: int | str) -> dict:
        """Delete an IP address reservation."""
        return self._delete(f"/addresses/{address_id}/")

    # ------------------------------------------------------------------
    # VLANs
    # ------------------------------------------------------------------

    def get_vlans(self) -> list[dict]:
        """Get all VLANs."""
        try:
            return self._get("/vlans/")
        except IPAMError:
            return []

    def get_vlan(self, vlan_id: int | str) -> dict:
        """Get a single VLAN."""
        return self._get(f"/vlans/{vlan_id}/")

    # ------------------------------------------------------------------
    # Nameservers
    # ------------------------------------------------------------------

    def get_nameservers(self) -> list[dict]:
        """Get configured nameserver sets."""
        try:
            return self._get("/tools/nameservers/")
        except IPAMError:
            return []

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def check_health(self) -> bool:
        """Check if phpIPAM API is reachable and responding."""
        try:
            self._ensure_auth()
            self._get("/sections/")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Section management
    # ------------------------------------------------------------------

    def create_section(self, name: str, description: str = "") -> dict:
        """Create a new IPAM section."""
        payload = {
            "name": name,
            "description": description or f"Managed by InfraForge",
            "showVLAN": "1",
            "showVRF": "0",
        }
        return self._post("/sections/", payload)

    def delete_section(self, section_id: int | str) -> dict:
        """Delete an IPAM section and all its subnets."""
        return self._delete(f"/sections/{section_id}/")

    def find_section_by_name(self, name: str) -> dict | None:
        """Find a section by name, returns None if not found."""
        try:
            sections = self.get_sections()
            for s in sections:
                if s.get("name", "").lower() == name.lower():
                    return s
        except IPAMError:
            pass
        return None

    # ------------------------------------------------------------------
    # Subnet management
    # ------------------------------------------------------------------

    def create_subnet(
        self,
        subnet: str,
        mask: int,
        section_id: int | str,
        description: str = "",
        vlan_id: int | str | None = None,
        scan_agent_id: int | str = 1,
        ping_subnet: bool = True,
        discover_subnet: bool = True,
    ) -> dict:
        """Create a subnet in phpIPAM with optional scanning enabled.

        Args:
            subnet: Network address (e.g. "10.0.7.0")
            mask: CIDR prefix length (e.g. 24)
            section_id: Section to place the subnet in
            description: Human-readable description
            vlan_id: Optional VLAN ID to associate
            scan_agent_id: Scan agent ID (1 = default cron agent)
            ping_subnet: Enable ping scanning
            discover_subnet: Enable host discovery
        """
        payload: dict[str, Any] = {
            "subnet": subnet,
            "mask": str(mask),
            "sectionId": str(section_id),
            "description": description or f"{subnet}/{mask}",
            "pingSubnet": "1" if ping_subnet else "0",
            "discoverSubnet": "1" if discover_subnet else "0",
            "scanAgent": str(scan_agent_id),
        }
        if vlan_id is not None:
            payload["vlanId"] = str(vlan_id)
        return self._post("/subnets/", payload)

    def delete_subnet(self, subnet_id: int | str) -> dict:
        """Delete a subnet and all its addresses."""
        return self._delete(f"/subnets/{subnet_id}/")

    def enable_subnet_scanning(
        self,
        subnet_id: int | str,
        scan_agent_id: int | str = 1,
    ) -> dict:
        """Enable ping scanning and discovery on an existing subnet."""
        payload = {
            "pingSubnet": "1",
            "discoverSubnet": "1",
            "scanAgent": str(scan_agent_id),
        }
        return self._patch(f"/subnets/{subnet_id}/", payload)

    # ------------------------------------------------------------------
    # VLAN management
    # ------------------------------------------------------------------

    def create_vlan(
        self,
        number: int,
        name: str = "",
        description: str = "",
    ) -> dict:
        """Create a VLAN."""
        payload = {
            "number": str(number),
            "name": name or f"VLAN {number}",
            "description": description,
        }
        return self._post("/vlans/", payload)

    def delete_vlan(self, vlan_id: int | str) -> dict:
        """Delete a VLAN."""
        return self._delete(f"/vlans/{vlan_id}/")

    def find_vlan_by_number(self, number: int) -> dict | None:
        """Find a VLAN by number."""
        vlans = self.get_vlans()
        for v in vlans:
            if str(v.get("number", "")) == str(number):
                return v
        return None
