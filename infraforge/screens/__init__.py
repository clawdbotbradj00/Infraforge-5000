"""InfraForge TUI screens."""

from infraforge.screens.dashboard import DashboardScreen
from infraforge.screens.vm_list import VMListScreen
from infraforge.screens.vm_detail import VMDetailScreen
from infraforge.screens.template_list import TemplateListScreen
from infraforge.screens.node_info import NodeInfoScreen
from infraforge.screens.help_screen import HelpScreen
from infraforge.screens.new_vm import NewVMScreen
from infraforge.screens.dns_screen import DNSScreen
from infraforge.screens.ipam_screen import IPAMScreen
from infraforge.screens.ansible_screen import AnsibleScreen

__all__ = [
    "DashboardScreen",
    "VMListScreen",
    "VMDetailScreen",
    "TemplateListScreen",
    "NodeInfoScreen",
    "HelpScreen",
    "NewVMScreen",
    "DNSScreen",
    "IPAMScreen",
    "AnsibleScreen",
]
