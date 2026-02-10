"""Cloudflare DNS API client for InfraForge.

Uses the Cloudflare v4 REST API to manage DNS records across one or more
zones.  Authentication is via a scoped API token (Bearer token).

This module intentionally uses only the Python standard library
(urllib.request, json, ssl) to avoid adding external dependencies.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any


from infraforge.dns_client import DNSRecord


class CloudflareError(Exception):
    """Cloudflare API error."""
    pass


class CloudflareClient:
    """Cloudflare DNS API client for InfraForge.

    Manages DNS records via the Cloudflare v4 API.  Requires a scoped
    API token with DNS read/edit permissions on the target zones.

    Constructor parameters:
      api_token: A Cloudflare API token (used as ``Authorization: Bearer <token>``).
    """

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str):
        """Initialize with a Cloudflare API token (Bearer token)."""
        if not api_token:
            raise CloudflareError("api_token is required")
        self._api_token = api_token
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def from_config(cls, config: Any) -> "CloudflareClient":
        """Create from InfraForge Config.  Uses ``config.cloudflare.api_token``."""
        return cls(api_token=config.cloudflare.api_token)

    # ------------------------------------------------------------------
    # Token verification
    # ------------------------------------------------------------------

    def verify_token(self) -> dict:
        """Verify the API token is valid.

        Calls ``GET /user/tokens/verify`` and returns the result dict
        (e.g. ``{"status": "active"}``).

        Raises:
          CloudflareError: If the token is invalid or the request fails.
        """
        result = self._request("GET", "/user/tokens/verify")
        return result.get("result", {})

    # ------------------------------------------------------------------
    # Zone management
    # ------------------------------------------------------------------

    def list_zones(self) -> list[dict]:
        """List all DNS zones accessible to the token.

        Paginates through ``GET /zones?per_page=50`` and returns a list
        of zone dicts with keys:

          - ``id``: Cloudflare zone ID
          - ``name``: Zone name (e.g. ``"example.com"``)
          - ``status``: Zone status (e.g. ``"active"``)
          - ``permissions``: List of permission strings from the API
          - ``access``: Convenience key -- ``"readwrite"`` if the token
            has ``#dns_records:edit``, ``"read"`` if it only has
            ``#dns_records:read``, otherwise ``"none"``
        """
        zones: list[dict] = []
        page = 1

        while True:
            resp = self._request("GET", f"/zones?per_page=50&page={page}")
            result_list = resp.get("result", [])
            if not result_list:
                break

            for zone in result_list:
                permissions = zone.get("permissions", [])
                if "#dns_records:edit" in permissions:
                    access = "readwrite"
                elif "#dns_records:read" in permissions:
                    access = "read"
                else:
                    access = "none"

                zones.append({
                    "id": zone["id"],
                    "name": zone["name"],
                    "status": zone.get("status", "unknown"),
                    "permissions": permissions,
                    "access": access,
                })

            # Check if there are more pages
            result_info = resp.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return zones

    # ------------------------------------------------------------------
    # Record queries
    # ------------------------------------------------------------------

    def list_records(
        self, zone_id: str, zone_name: str = ""
    ) -> list[dict]:
        """List all DNS records in a zone.

        Paginates through ``GET /zones/{zone_id}/dns_records?per_page=100``
        and returns a list of dicts, each containing:

          - ``record``: A :class:`DNSRecord` instance
          - ``cf_id``: The Cloudflare record ID (needed for update/delete)
          - ``proxied``: Whether Cloudflare proxying is enabled

        The ``DNSRecord.name`` is converted to a relative name by stripping
        the zone suffix (e.g. ``"web.example.com"`` becomes ``"web"``).
        If the record name equals the zone name, it is stored as ``"@"``.

        A Cloudflare TTL of ``1`` (meaning "auto") is mapped to ``300`` in
        the ``DNSRecord`` for display purposes.

        Parameters:
          zone_id: Cloudflare zone ID.
          zone_name: Zone domain name, used to compute relative record names.
              If empty, record names are returned as-is from the API.
        """
        all_records: list[dict] = []
        page = 1

        while True:
            resp = self._request(
                "GET",
                f"/zones/{zone_id}/dns_records?per_page=100&page={page}",
            )
            result_list = resp.get("result", [])
            if not result_list:
                break

            for rec in result_list:
                raw_name = rec.get("name", "")
                rtype = rec.get("type", "")
                value = rec.get("content", "")
                ttl = rec.get("ttl", 1)
                proxied = rec.get("proxied", False)
                cf_id = rec.get("id", "")

                # Convert absolute name to relative
                display_name = self._relative_name(raw_name, zone_name)

                # Map TTL=1 ("auto" in Cloudflare) to 300 for display
                display_ttl = 300 if ttl == 1 else ttl

                dns_record = DNSRecord(
                    name=display_name,
                    rtype=rtype,
                    value=value,
                    ttl=display_ttl,
                    zone=zone_name,
                )

                all_records.append({
                    "record": dns_record,
                    "cf_id": cf_id,
                    "proxied": proxied,
                })

            # Check for more pages
            result_info = resp.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_records

    # ------------------------------------------------------------------
    # Record management
    # ------------------------------------------------------------------

    def create_record(
        self,
        zone_id: str,
        name: str,
        rtype: str,
        value: str,
        ttl: int = 1,
        proxied: bool = False,
    ) -> dict:
        """Create a DNS record in a zone.

        Calls ``POST /zones/{zone_id}/dns_records`` with the given
        record parameters.

        Parameters:
          zone_id: Cloudflare zone ID.
          name: Record name (relative or absolute).
          rtype: Record type (e.g. ``"A"``, ``"CNAME"``).
          value: Record value / content.
          ttl: TTL in seconds.  Use ``1`` for Cloudflare "auto" TTL.
          proxied: Whether to enable Cloudflare proxying (orange cloud).

        Returns:
          The created record dict from the Cloudflare API response.

        Raises:
          CloudflareError: If the API request fails.
        """
        body = {
            "type": rtype,
            "name": name,
            "content": value,
            "ttl": ttl,
            "proxied": proxied,
        }
        resp = self._request("POST", f"/zones/{zone_id}/dns_records", data=body)
        return resp.get("result", {})

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        name: str,
        rtype: str,
        value: str,
        ttl: int = 1,
        proxied: bool = False,
    ) -> dict:
        """Update an existing DNS record.

        Calls ``PATCH /zones/{zone_id}/dns_records/{record_id}`` with
        the new record parameters.

        Parameters:
          zone_id: Cloudflare zone ID.
          record_id: Cloudflare record ID (from ``list_records`` ``cf_id``).
          name: Record name (relative or absolute).
          rtype: Record type.
          value: Record value / content.
          ttl: TTL in seconds.  Use ``1`` for Cloudflare "auto" TTL.
          proxied: Whether to enable Cloudflare proxying.

        Returns:
          The updated record dict from the Cloudflare API response.

        Raises:
          CloudflareError: If the API request fails.
        """
        body = {
            "type": rtype,
            "name": name,
            "content": value,
            "ttl": ttl,
            "proxied": proxied,
        }
        resp = self._request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            data=body,
        )
        return resp.get("result", {})

    def delete_record(self, zone_id: str, record_id: str) -> None:
        """Delete a DNS record.

        Calls ``DELETE /zones/{zone_id}/dns_records/{record_id}``.

        Parameters:
          zone_id: Cloudflare zone ID.
          record_id: Cloudflare record ID.

        Raises:
          CloudflareError: If the API request fails.
        """
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _relative_name(fqdn: str, zone_name: str) -> str:
        """Convert an absolute record name to a relative name.

        Strips the zone suffix from *fqdn*.  If *fqdn* equals *zone_name*,
        returns ``"@"`` (the zone apex).  If *zone_name* is empty, returns
        *fqdn* unchanged.

        Examples::

            _relative_name("web.example.com", "example.com")  -> "web"
            _relative_name("example.com", "example.com")       -> "@"
            _relative_name("sub.web.example.com", "example.com") -> "sub.web"
        """
        if not zone_name:
            return fqdn
        if fqdn == zone_name:
            return "@"
        suffix = f".{zone_name}"
        if fqdn.endswith(suffix):
            return fqdn[: -len(suffix)]
        return fqdn

    def _request(
        self, method: str, path: str, data: dict | None = None
    ) -> dict:
        """Make an authenticated API request to Cloudflare.

        Parameters:
          method: HTTP method (``GET``, ``POST``, ``PATCH``, ``DELETE``).
          path: API path relative to the base URL (e.g. ``"/zones"``).
          data: Optional JSON body for POST/PATCH requests.

        Returns:
          The parsed JSON response as a dict.

        Raises:
          CloudflareError: On HTTP errors, malformed responses, or API
              errors (``success == false``).
        """
        url = f"{self.BASE_URL}{path}"

        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

        body_bytes: bytes | None = None
        if data is not None:
            body_bytes = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Try to extract Cloudflare error details from the response body
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            cf_message = ""
            if error_body:
                try:
                    error_data = json.loads(error_body)
                    errors = error_data.get("errors", [])
                    if errors:
                        cf_message = "; ".join(
                            f"[{err.get('code', '?')}] {err.get('message', 'Unknown error')}"
                            for err in errors
                        )
                except json.JSONDecodeError:
                    pass

            if cf_message:
                raise CloudflareError(
                    f"Cloudflare API error (HTTP {e.code}): {cf_message}"
                )
            raise CloudflareError(
                f"Cloudflare API error (HTTP {e.code}): {error_body or e.reason}"
            )
        except urllib.error.URLError as e:
            raise CloudflareError(f"Failed to connect to Cloudflare API: {e.reason}")
        except Exception as e:
            raise CloudflareError(f"Cloudflare API request failed: {e}")

        # Parse JSON response
        if not resp_body:
            return {}

        try:
            result = json.loads(resp_body)
        except json.JSONDecodeError as e:
            raise CloudflareError(f"Invalid JSON response from Cloudflare: {e}")

        # Check for API-level errors
        if isinstance(result, dict) and not result.get("success", True):
            errors = result.get("errors", [])
            if errors:
                messages = "; ".join(
                    f"[{err.get('code', '?')}] {err.get('message', 'Unknown error')}"
                    for err in errors
                )
                raise CloudflareError(f"Cloudflare API error: {messages}")
            raise CloudflareError("Cloudflare API returned success=false with no details")

        return result
