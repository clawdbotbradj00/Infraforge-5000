"""Dashboard screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, ListView, ListItem, Label
from textual.containers import Container, Horizontal, Vertical
from textual import work

import time

from infraforge.models import VMStatus


class DashboardScreen(Screen):
    """Main dashboard screen."""

    BINDINGS = [
        Binding("v", "view_vms", "VMs", show=True),
        Binding("t", "view_templates", "Templates", show=True),
        Binding("n", "view_nodes", "Nodes", show=True),
        Binding("x", "manage_dns", "DNS", show=True),
        Binding("i", "manage_ipam", "IPAM", show=True),
        Binding("c", "create_vm", "New VM", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="dashboard-container"):
            yield Static("", id="update-banner", markup=True, classes="hidden")
            yield Static("Dashboard", classes="section-title")
            with Horizontal(id="stats-row"):
                with Container(classes="stat-card"):
                    yield Static("0", id="stat-total-value", classes="stat-value")
                    yield Static("Total VMs", classes="stat-label")
                with Container(classes="stat-card"):
                    yield Static("0", id="stat-running-value", classes="stat-value status-running")
                    yield Static("Running", classes="stat-label")
                with Container(classes="stat-card"):
                    yield Static("0", id="stat-stopped-value", classes="stat-value status-stopped")
                    yield Static("Stopped", classes="stat-label")
                with Container(classes="stat-card"):
                    yield Static("0", id="stat-templates-value", classes="stat-value")
                    yield Static("Templates", classes="stat-label")

            yield Static("Cluster Nodes", classes="section-title")
            yield Container(id="node-summary")

            yield Static("Navigation", classes="section-title")
            yield ListView(
                ListItem(Label("  [V]  Virtual Machines  —  View and manage all VMs and containers"), id="nav-vms"),
                ListItem(Label("  [T]  Templates         —  Browse VM and container templates"), id="nav-templates"),
                ListItem(Label("  [N]  Node Info         —  Cluster node details and resources"), id="nav-nodes"),
                ListItem(Label("  [X]  DNS Management    —  View and manage DNS records"), id="nav-dns"),
                ListItem(Label("  [I]  IPAM Management   —  Manage IP addresses and subnets"), id="nav-ipam"),
                ListItem(Label("  [C]  Create New VM     —  Spin up a new virtual machine"), id="nav-create"),
                id="nav-menu",
            )
        yield Footer()

    def on_mount(self):
        self._start_auto_refresh()
        self._check_for_update()

    def on_screen_resume(self):
        """Refresh data when returning to the dashboard from another screen."""
        self._start_auto_refresh()

    @work(thread=True, exclusive=True, group="dashboard-refresh")
    def _start_auto_refresh(self):
        """Background worker that refreshes dashboard data every 10 seconds."""
        while True:
            try:
                self._do_load_data()
            except Exception as e:
                self.app.call_from_thread(self._show_error, str(e))
            time.sleep(10)

    def _do_load_data(self):
        """Fetch fresh data from Proxmox and update the UI."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_vms = pool.submit(self.app.proxmox.get_all_vms_and_templates)
            fut_nodes = pool.submit(self.app.proxmox.get_node_info, True)

            vms, templates = fut_vms.result()
            nodes = fut_nodes.result()

        total = len(vms)
        running = sum(1 for v in vms if v.status == VMStatus.RUNNING)
        stopped = sum(1 for v in vms if v.status == VMStatus.STOPPED)
        template_count = len(templates)

        self.app.call_from_thread(self._update_stats, total, running, stopped, template_count)
        self.app.call_from_thread(self._update_nodes, nodes)

    def load_data(self):
        """Manual refresh (called by action_refresh)."""
        self._start_auto_refresh()

    def _update_stats(self, total: int, running: int, stopped: int, templates: int):
        self.query_one("#stat-total-value", Static).update(str(total))
        self.query_one("#stat-running-value", Static).update(str(running))
        self.query_one("#stat-stopped-value", Static).update(str(stopped))
        self.query_one("#stat-templates-value", Static).update(str(templates))

    def _update_nodes(self, nodes):
        container = self.query_one("#node-summary")
        container.remove_children()

        for node in nodes:
            cpu_bar = self._make_bar(node.cpu_percent)
            mem_bar = self._make_bar(node.mem_percent)
            disk_bar = self._make_bar(node.disk_percent)

            status_color = "green" if node.status == "online" else "red"

            node_text = (
                f"  [{status_color}]●[/{status_color}] [bold]{node.node}[/bold]"
                f"  │  CPU: {cpu_bar} {node.cpu_percent:5.1f}%"
                f"  │  Mem: {mem_bar} {node.mem_percent:5.1f}%"
                f"  │  Disk: {disk_bar} {node.disk_percent:5.1f}%"
                f"  │  Up: {node.uptime_str}"
            )
            container.mount(Static(node_text, markup=True))

    def _make_bar(self, percent: float, width: int = 15) -> str:
        filled = int(percent / 100 * width)
        empty = width - filled
        if percent > 80:
            color = "red"
        elif percent > 60:
            color = "yellow"
        else:
            color = "green"
        return f"[{color}]{'█' * filled}{'░' * empty}[/{color}]"

    def _show_error(self, error: str):
        container = self.query_one("#node-summary")
        container.remove_children()
        container.mount(Static(f"  [red]Error loading data: {error}[/red]", markup=True))

    def on_list_view_selected(self, event: ListView.Selected):
        item_id = event.item.id
        if item_id == "nav-vms":
            self.action_view_vms()
        elif item_id == "nav-templates":
            self.action_view_templates()
        elif item_id == "nav-nodes":
            self.action_view_nodes()
        elif item_id == "nav-dns":
            self.action_manage_dns()
        elif item_id == "nav-ipam":
            self.action_manage_ipam()
        elif item_id == "nav-create":
            self.action_create_vm()

    def action_view_vms(self):
        from infraforge.screens.vm_list import VMListScreen
        self.app.push_screen(VMListScreen())

    def action_view_templates(self):
        from infraforge.screens.template_list import TemplateListScreen
        self.app.push_screen(TemplateListScreen())

    def action_view_nodes(self):
        from infraforge.screens.node_info import NodeInfoScreen
        self.app.push_screen(NodeInfoScreen())

    def action_manage_dns(self):
        from infraforge.screens.dns_screen import DNSScreen
        self.app.push_screen(DNSScreen())

    def action_manage_ipam(self):
        from infraforge.screens.ipam_screen import IPAMScreen
        self.app.push_screen(IPAMScreen())

    def action_create_vm(self):
        from infraforge.screens.new_vm import NewVMScreen
        self.app.push_screen(NewVMScreen())

    @work(thread=True)
    def _check_for_update(self):
        """Check GitHub for a newer release in the background."""
        try:
            from infraforge.updater import check_for_update
            result = check_for_update()
            if result:
                self.app.call_from_thread(self._show_update_banner, result)
        except Exception:
            pass

    def _show_update_banner(self, result: dict):
        latest = result.get("latest", "?")
        current = result.get("current", "?")
        # Update header subtitle to show update notice
        self.app.sub_title = f"v{current} → v{latest} available!  Run: infraforge update"
        # Also show the dashboard banner
        banner = self.query_one("#update-banner", Static)
        banner.update(
            f"  [bold yellow]New version available![/bold yellow]  "
            f"[bold cyan]v{latest}[/bold cyan]  [dim](you have v{current})[/dim]  "
            f"[bold yellow]—[/bold yellow]  "
            f"Run [bold white on dark_green] infraforge update [/bold white on dark_green] to upgrade"
        )
        banner.remove_class("hidden")

    def action_refresh(self):
        self.load_data()
