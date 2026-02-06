"""Credential management for Ansible playbook execution.

Stores named credential profiles (password or SSH key) in a YAML file
with restricted file permissions (``0o600``).  Supports SSH key generation
via paramiko.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CREDENTIALS_DIR = Path.home() / ".config" / "infraforge"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.yaml"
SSH_KEYS_DIR = CREDENTIALS_DIR / "ssh_keys"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CredentialProfile:
    """A named, reusable credential profile for Ansible."""

    name: str
    auth_type: str = "password"       # "password" or "ssh_key"
    username: str = "root"
    # Password auth
    password: str = ""
    # SSH key auth
    private_key_path: str = ""
    passphrase: str = ""
    # Become / privilege escalation
    become: bool = True
    become_method: str = "sudo"
    become_pass: str = ""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CredentialManager:
    """CRUD operations for credential profiles + SSH key generation."""

    def __init__(
        self,
        credentials_file: Path = CREDENTIALS_FILE,
        ssh_keys_dir: Path = SSH_KEYS_DIR,
    ) -> None:
        self._credentials_file = credentials_file
        self._ssh_keys_dir = ssh_keys_dir

    # -- helpers ----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self._ssh_keys_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._ssh_keys_dir, 0o700)
        except OSError:
            pass

    # -- profile CRUD -----------------------------------------------------

    def load_profiles(self) -> list[CredentialProfile]:
        """Load all credential profiles from disk."""
        if not self._credentials_file.exists():
            return []
        try:
            with open(self._credentials_file) as f:
                data = yaml.safe_load(f) or {}
            raw_profiles = data.get("profiles", [])
            profiles: list[CredentialProfile] = []
            for entry in raw_profiles:
                if not isinstance(entry, dict):
                    continue
                profiles.append(CredentialProfile(
                    name=entry.get("name", "unnamed"),
                    auth_type=entry.get("auth_type", "password"),
                    username=entry.get("username", "root"),
                    password=entry.get("password", ""),
                    private_key_path=entry.get("private_key_path", ""),
                    passphrase=entry.get("passphrase", ""),
                    become=entry.get("become", True),
                    become_method=entry.get("become_method", "sudo"),
                    become_pass=entry.get("become_pass", ""),
                ))
            return profiles
        except Exception as exc:
            logger.warning("Failed to load credentials: %s", exc)
            return []

    def save_profiles(self, profiles: list[CredentialProfile]) -> None:
        """Persist all profiles to disk with secure permissions."""
        self._ensure_dirs()
        data: dict[str, Any] = {
            "profiles": [asdict(p) for p in profiles],
        }
        with open(self._credentials_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        try:
            os.chmod(self._credentials_file, 0o600)
        except OSError:
            pass

    def get_profile(self, name: str) -> CredentialProfile | None:
        """Look up a single profile by name."""
        for p in self.load_profiles():
            if p.name == name:
                return p
        return None

    def add_profile(self, profile: CredentialProfile) -> None:
        """Add a new profile (appends to list)."""
        profiles = self.load_profiles()
        profiles.append(profile)
        self.save_profiles(profiles)

    def delete_profile(self, name: str) -> None:
        """Remove a profile by name."""
        profiles = [p for p in self.load_profiles() if p.name != name]
        self.save_profiles(profiles)

    # -- SSH key generation -----------------------------------------------

    def generate_ssh_key(
        self,
        name: str,
        bits: int = 4096,
        passphrase: str = "",
    ) -> tuple[Path, str]:
        """Generate an RSA key pair and return ``(private_key_path, public_key_str)``.

        The private key is saved to ``~/.config/infraforge/ssh_keys/{name}_rsa``
        with ``0o600`` permissions.
        """
        import paramiko

        self._ensure_dirs()

        key = paramiko.RSAKey.generate(bits)
        key_path = self._ssh_keys_dir / f"{name}_rsa"
        key.write_private_key_file(
            str(key_path),
            password=passphrase or None,
        )
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass

        public_key = f"{key.get_name()} {key.get_base64()} infraforge-{name}"
        return key_path, public_key
