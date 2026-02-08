"""Data models for InfraForge."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"

    @classmethod
    def from_str(cls, s: str) -> "VMStatus":
        try:
            return cls(s.lower())
        except ValueError:
            return cls.UNKNOWN


class VMType(Enum):
    QEMU = "qemu"
    LXC = "lxc"


class TemplateType(Enum):
    VM = "vm"              # QEMU VM template
    CONTAINER = "container" # LXC container template (pveam)
    ISO = "iso"            # ISO image


@dataclass
class NodeInfo:
    node: str
    status: str = "unknown"
    cpu: float = 0.0
    maxcpu: int = 0
    mem: int = 0
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    uptime: int = 0
    ssl_fingerprint: str = ""
    cpu_model: str = ""

    @property
    def cpu_percent(self) -> float:
        return self.cpu * 100

    @property
    def mem_percent(self) -> float:
        if self.maxmem == 0:
            return 0.0
        return (self.mem / self.maxmem) * 100

    @property
    def disk_percent(self) -> float:
        if self.maxdisk == 0:
            return 0.0
        return (self.disk / self.maxdisk) * 100

    @property
    def mem_used_gib(self) -> float:
        """Memory used in GiB."""
        return self.mem / (1024 ** 3) if self.mem else 0.0

    @property
    def mem_total_gib(self) -> float:
        """Total memory in GiB."""
        return self.maxmem / (1024 ** 3) if self.maxmem else 0.0

    @property
    def disk_used_gib(self) -> float:
        """Disk used in GiB."""
        return self.disk / (1024 ** 3) if self.disk else 0.0

    @property
    def disk_total_gib(self) -> float:
        """Total disk in GiB."""
        return self.maxdisk / (1024 ** 3) if self.maxdisk else 0.0

    @property
    def uptime_str(self) -> str:
        if self.uptime == 0:
            return "N/A"
        days = self.uptime // 86400
        hours = (self.uptime % 86400) // 3600
        minutes = (self.uptime % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


@dataclass
class VM:
    vmid: int
    name: str
    status: VMStatus
    node: str
    vm_type: VMType
    cpu: float = 0.0
    cpus: int = 0
    mem: int = 0
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    uptime: int = 0
    netin: int = 0
    netout: int = 0
    pid: Optional[int] = None
    tags: str = ""
    template: bool = False

    # Detailed config (populated when drilling into a VM)
    config: dict = field(default_factory=dict)
    snapshots: list = field(default_factory=list)

    @property
    def mem_percent(self) -> float:
        if self.maxmem == 0:
            return 0.0
        return (self.mem / self.maxmem) * 100

    @property
    def cpu_percent(self) -> float:
        if self.cpus == 0:
            return 0.0
        return self.cpu * 100

    @property
    def mem_gb(self) -> float:
        return self.maxmem / (1024 ** 3)

    @property
    def disk_gb(self) -> float:
        return self.maxdisk / (1024 ** 3)

    @property
    def uptime_str(self) -> str:
        if self.uptime == 0:
            return "N/A"
        days = self.uptime // 86400
        hours = (self.uptime % 86400) // 3600
        minutes = (self.uptime % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def status_icon(self) -> str:
        return {
            VMStatus.RUNNING: "●",
            VMStatus.STOPPED: "○",
            VMStatus.PAUSED: "◑",
            VMStatus.SUSPENDED: "◐",
            VMStatus.UNKNOWN: "?",
        }.get(self.status, "?")

    @property
    def type_label(self) -> str:
        return "VM" if self.vm_type == VMType.QEMU else "CT"


@dataclass
class Template:
    name: str
    template_type: TemplateType
    node: str = ""
    storage: str = ""
    volid: str = ""
    size: int = 0
    # For pveam (appliance) templates
    description: str = ""
    os: str = ""
    section: str = ""
    package: str = ""
    architecture: str = ""
    headline: str = ""
    infopage: str = ""
    location: str = ""
    maintainer: str = ""
    source: str = ""
    version: str = ""
    sha512sum: str = ""
    # For VM templates
    vmid: Optional[int] = None

    @property
    def size_mb(self) -> float:
        return self.size / (1024 ** 2)

    @property
    def size_display(self) -> str:
        if self.size == 0:
            return "N/A"
        if self.size >= 1024 ** 3:
            return f"{self.size / (1024 ** 3):.1f} GB"
        return f"{self.size / (1024 ** 2):.1f} MB"

    @property
    def type_label(self) -> str:
        return {
            TemplateType.VM: "VM Template",
            TemplateType.CONTAINER: "CT Template",
            TemplateType.ISO: "ISO Image",
        }.get(self.template_type, "Unknown")


@dataclass
class StorageInfo:
    storage: str
    node: str
    storage_type: str = ""
    content: str = ""
    active: bool = True
    enabled: bool = True
    shared: bool = False
    total: int = 0
    used: int = 0
    avail: int = 0

    @property
    def used_percent(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.used / self.total) * 100

    @property
    def total_display(self) -> str:
        if self.total == 0:
            return "N/A"
        return f"{self.total / (1024 ** 3):.1f} GB"

    @property
    def used_display(self) -> str:
        if self.used == 0:
            return "N/A"
        return f"{self.used / (1024 ** 3):.1f} GB"

    @property
    def avail_display(self) -> str:
        if self.avail == 0:
            return "N/A"
        return f"{self.avail / (1024 ** 3):.1f} GB"


@dataclass
class NewVMSpec:
    """Specification for creating a new VM via Terraform provisioning."""
    name: str = ""
    vmid: Optional[int] = None
    node: str = ""
    template: str = ""
    template_volid: str = ""
    template_vmid: Optional[int] = None
    vm_type: VMType = VMType.QEMU
    cpu_cores: int = 2
    memory_mb: int = 2048
    disk_gb: int = 10
    storage: str = "local-lvm"
    network_bridge: str = "vmbr0"
    ip_address: str = ""
    subnet_mask: int = 24
    gateway: str = ""
    dns_name: str = ""
    dns_domain: str = ""
    dns_zone: str = ""
    ssh_keys: str = ""
    start_after_create: bool = True
    ansible_playbook: str = ""
    tags: str = ""
    description: str = ""
    subnet_id: str = ""
    subnet_cidr: str = ""
    unprivileged: bool = True
    vlan_tag: Optional[int] = None
    dns_servers: str = ""
