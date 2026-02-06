"""Configuration management for InfraForge."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


class ConfigError(Exception):
    """Configuration error."""
    pass


@dataclass
class ProxmoxConfig:
    host: str = ""
    port: int = 8006
    user: str = "root@pam"
    auth_method: str = "token"  # "token" or "password"
    token_name: str = ""
    token_value: str = ""
    password: str = ""
    verify_ssl: bool = False


@dataclass
class DNSConfig:
    provider: str = ""          # "bind9", "cloudflare", "route53", etc.
    server: str = ""            # BIND9 server IP / hostname
    port: int = 53              # DNS port
    zones: list = field(default_factory=list)  # List of zone names e.g. ["lab.local", "dev.local"]
    domain: str = ""            # Default domain for FQDN construction
    tsig_key_name: str = ""
    tsig_key_secret: str = ""
    tsig_algorithm: str = "hmac-sha256"
    api_key: str = ""           # Generic API key (Cloudflare, etc.)

    def add_zone(self, zone: str) -> None:
        if zone not in self.zones:
            self.zones.append(zone)

    def remove_zone(self, zone: str) -> None:
        if zone in self.zones:
            self.zones.remove(zone)


@dataclass
class TerraformConfig:
    workspace: str = "./terraform"
    state_backend: str = "local"


@dataclass
class AnsibleConfig:
    playbook_dir: str = "./ansible/playbooks"
    inventory_dir: str = "./ansible/inventory"


@dataclass
class IPAMConfig:
    provider: str = ""  # "phpipam"
    url: str = ""
    app_id: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = False


@dataclass
class AIConfig:
    provider: str = ""          # "anthropic"
    api_key: str = ""
    model: str = "claude-sonnet-4-5-20250929"


@dataclass
class DefaultsConfig:
    cpu_cores: int = 2
    memory_mb: int = 2048
    disk_gb: int = 20
    storage: str = "local-lvm"
    network_bridge: str = "vmbr0"
    os_type: str = "l26"
    start_on_create: bool = True


@dataclass
class Config:
    proxmox: ProxmoxConfig = field(default_factory=ProxmoxConfig)
    dns: DNSConfig = field(default_factory=DNSConfig)
    ipam: IPAMConfig = field(default_factory=IPAMConfig)
    terraform: TerraformConfig = field(default_factory=TerraformConfig)
    ansible: AnsibleConfig = field(default_factory=AnsibleConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    ai: AIConfig = field(default_factory=AIConfig)

    CONFIG_PATHS = [
        Path.home() / ".config" / "infraforge" / "config.yaml",
        Path.home() / ".config" / "infraforge" / "config.yml",
        Path("config") / "config.yaml",
        Path("config") / "config.yml",
    ]

    @classmethod
    def find_config_file(cls) -> Optional[Path]:
        """Find the first existing config file."""
        for path in cls.CONFIG_PATHS:
            if path.exists():
                return path
        return None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load configuration from YAML file."""
        if path is None:
            path = cls.find_config_file()

        if path is None:
            raise ConfigError(
                "No configuration file found. Searched:\n"
                + "\n".join(f"  - {p}" for p in cls.CONFIG_PATHS)
            )

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            raise ConfigError(f"Failed to read config file {path}: {e}")

        config = cls()

        # Parse proxmox section
        if "proxmox" in data:
            pve = data["proxmox"]
            config.proxmox = ProxmoxConfig(
                host=str(pve.get("host", "")),
                port=int(pve.get("port", 8006)),
                user=str(pve.get("user", "root@pam")),
                auth_method=str(pve.get("auth_method", "token")),
                token_name=str(pve.get("token_name", "")),
                token_value=str(pve.get("token_value", "")),
                password=str(pve.get("password", "")),
                verify_ssl=bool(pve.get("verify_ssl", False)),
            )

        # Parse dns section
        if "dns" in data:
            dns = data["dns"]
            # Support both new "zones" list and old single "zone" string
            zones_val = dns.get("zones", [])
            if not zones_val and dns.get("zone"):
                zones_val = [str(dns.get("zone"))]
            if not isinstance(zones_val, list):
                zones_val = [str(zones_val)]
            # Fall back: use old "zone" for domain if domain is empty
            domain_val = str(dns.get("domain", ""))
            if not domain_val and dns.get("zone"):
                domain_val = str(dns.get("zone"))
            config.dns = DNSConfig(
                provider=str(dns.get("provider", "")),
                server=str(dns.get("server", "")),
                port=int(dns.get("port", 53)),
                zones=zones_val,
                domain=domain_val,
                tsig_key_name=str(dns.get("tsig_key_name", "")),
                tsig_key_secret=str(dns.get("tsig_key_secret", "")),
                tsig_algorithm=str(dns.get("tsig_algorithm", "hmac-sha256")),
                api_key=str(dns.get("api_key", "")),
            )

        # Parse terraform section
        if "terraform" in data:
            tf = data["terraform"]
            config.terraform = TerraformConfig(
                workspace=str(tf.get("workspace", "./terraform")),
                state_backend=str(tf.get("state_backend", "local")),
            )

        # Parse ansible section
        if "ansible" in data:
            ans = data["ansible"]
            config.ansible = AnsibleConfig(
                playbook_dir=str(ans.get("playbook_dir", "./ansible/playbooks")),
                inventory_dir=str(ans.get("inventory_dir", "./ansible/inventory")),
            )

        # Parse ipam section
        if "ipam" in data:
            ipam = data["ipam"]
            config.ipam = IPAMConfig(
                provider=str(ipam.get("provider", "")),
                url=str(ipam.get("url", "")),
                app_id=str(ipam.get("app_id", "")),
                token=str(ipam.get("token", "")),
                username=str(ipam.get("username", "")),
                password=str(ipam.get("password", "")),
                verify_ssl=bool(ipam.get("verify_ssl", False)),
            )

        # Parse ai section
        if "ai" in data:
            ai = data["ai"]
            config.ai = AIConfig(
                provider=str(ai.get("provider", "")),
                api_key=str(ai.get("api_key", "")),
                model=str(ai.get("model", "claude-sonnet-4-5-20250929")),
            )

        # Parse defaults section
        if "defaults" in data:
            defs = data["defaults"]
            config.defaults = DefaultsConfig(
                cpu_cores=int(defs.get("cpu_cores", 2)),
                memory_mb=int(defs.get("memory_mb", 2048)),
                disk_gb=int(defs.get("disk_gb", 20)),
                storage=str(defs.get("storage", "local-lvm")),
                network_bridge=str(defs.get("network_bridge", "vmbr0")),
                os_type=str(defs.get("os_type", "l26")),
                start_on_create=bool(defs.get("start_on_create", True)),
            )

        # Validate required fields
        if not config.proxmox.host:
            raise ConfigError("Proxmox host is required in configuration")

        return config
