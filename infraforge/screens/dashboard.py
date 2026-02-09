"""Dashboard screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, ListView, ListItem, Label
from textual.containers import Container, Horizontal, Vertical
from textual import work

import time

from infraforge.models import VMStatus

NODE_SORT_FIELDS = ["name", "status", "cpu", "mem", "disk", "uptime"]
NODE_SORT_LABELS = ["Name", "Status", "CPU %", "Mem %", "Disk %", "Uptime"]


class DashboardScreen(Screen):
    """Main dashboard screen."""

    BINDINGS = [
        Binding("1", "view_vms", "1:VMs", show=True),
        Binding("2", "view_templates", "2:Templates", show=True),
        Binding("3", "view_nodes", "3:Nodes", show=True),
        Binding("4", "manage_dns", "4:DNS", show=True),
        Binding("5", "manage_ipam", "5:IPAM", show=True),
        Binding("6", "create_vm", "6:Provision", show=True),
        Binding("7", "manage_ansible", "7:Ansible", show=True),
        Binding("8", "ai_settings", "8:AI", show=True),
        Binding("s", "cycle_node_sort", "Sort", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("D", "download_template", "Download Templates", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._node_sort_index: int = 0
        self._node_sort_reverse: bool = False
        self._disabled_items: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="dashboard-container"):
            yield Static("", id="update-banner", markup=True, classes="hidden")
            yield Static("", id="ai-setup-banner", markup=True, classes="hidden")
            yield Static("", id="setup-banner", markup=True, classes="hidden")
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

            with Horizontal(id="node-header-row"):
                yield Static("Cluster Nodes", classes="section-title")
                yield Static("  Sort: [bold]Name \u25b2[/bold]", id="node-sort-label", markup=True)
            yield Horizontal(id="node-summary")

            yield Static("Navigation", classes="section-title")
            yield ListView(
                ListItem(Label("[dim bold]  INFRASTRUCTURE[/dim bold]"), id="nav-group-infra", disabled=True),
                ListItem(Label("  [1]  Virtual Machines  —  View and manage all VMs and containers"), id="nav-vms"),
                ListItem(Label("  [2]  Templates         —  Browse VM and container templates"), id="nav-templates"),
                ListItem(Label("  [3]  Node Info         —  Cluster node details and resources"), id="nav-nodes"),
                ListItem(Label(" "), id="nav-spacer-1", disabled=True),
                ListItem(Label("[dim bold]  NETWORKING[/dim bold]"), id="nav-group-net", disabled=True),
                ListItem(Label("  [4]  DNS Management    —  View and manage DNS records"), id="nav-dns"),
                ListItem(Label("  [5]  IPAM Management   —  Manage IP addresses and subnets"), id="nav-ipam"),
                ListItem(Label(" "), id="nav-spacer-2", disabled=True),
                ListItem(Label("[dim bold]  PROVISIONING[/dim bold]"), id="nav-group-prov", disabled=True),
                ListItem(Label("  [6]  Provision VM      —  Templates and custom VM creation"), id="nav-create"),
                ListItem(Label("  [7]  Ansible           —  Manage playbooks and automation"), id="nav-ansible"),
                ListItem(Label(" "), id="nav-spacer-3", disabled=True),
                ListItem(Label("[dim bold]  SETTINGS[/dim bold]"), id="nav-group-settings", disabled=True),
                ListItem(Label("  [8]  AI Settings       —  Configure AI assistant and model"), id="nav-ai-settings"),
                id="nav-menu",
            )
        yield Footer()

    def on_mount(self):
        self._start_auto_refresh()
        self._check_for_update()
        self._check_ai_config()
        self._check_modules()

    def on_screen_resume(self):
        """Refresh data when returning to the dashboard from another screen."""
        self._start_auto_refresh()
        self._check_modules()

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

    def _sort_nodes(self, nodes):
        """Sort nodes based on current sort field and direction."""
        field = NODE_SORT_FIELDS[self._node_sort_index]
        if field == "name":
            key = lambda n: n.node.lower()
        elif field == "status":
            key = lambda n: (0 if n.status == "online" else 1, n.node.lower())
        elif field == "cpu":
            key = lambda n: n.cpu_percent
        elif field == "mem":
            key = lambda n: n.mem_percent
        elif field == "disk":
            key = lambda n: n.disk_percent
        elif field == "uptime":
            key = lambda n: n.uptime
        else:
            key = lambda n: n.node.lower()
        return sorted(nodes, key=key, reverse=self._node_sort_reverse)

    def _update_sort_label(self):
        label = NODE_SORT_LABELS[self._node_sort_index]
        direction = " \u25bc" if self._node_sort_reverse else " \u25b2"
        self.query_one("#node-sort-label", Static).update(
            f"  Sort: [bold]{label}{direction}[/bold]"
        )

    def action_cycle_node_sort(self):
        """Cycle through node sort fields and direction."""
        fields = NODE_SORT_FIELDS
        if self._node_sort_index == len(fields) - 1 and not self._node_sort_reverse:
            self._node_sort_reverse = True
        elif self._node_sort_reverse:
            self._node_sort_reverse = False
            self._node_sort_index = (self._node_sort_index + 1) % len(fields)
        else:
            self._node_sort_index = (self._node_sort_index + 1) % len(fields)
        self._update_sort_label()
        # Re-render with current cached nodes (next refresh will also use new sort)
        if hasattr(self, '_last_nodes') and self._last_nodes:
            self._render_node_list(self._last_nodes)

    def _update_nodes(self, nodes):
        self._last_nodes = nodes
        self._render_node_list(nodes)

    def _render_node_list(self, nodes):
        container = self.query_one("#node-summary")
        container.remove_children()

        sorted_nodes = self._sort_nodes(nodes)
        for node in sorted_nodes:
            status_color = "green" if node.status == "online" else "red"
            status_dot = f"[{status_color}]●[/{status_color}]"

            # CPU model — compact
            cpu_model = node.cpu_model if node.cpu_model else "—"
            if len(cpu_model) > 30:
                cpu_model = cpu_model[:28] + ".."

            cpu_bar = self._make_bar(node.cpu_percent)
            mem_bar = self._make_bar(node.mem_percent)
            disk_bar = self._make_bar(node.disk_percent)

            node_text = (
                f" {status_dot} [bold]{node.node}[/bold]  [dim]Up:[/dim] {node.uptime_str}\n"
                f" [bold cyan]CPU[/bold cyan]  {cpu_bar} {node.cpu_percent:4.1f}%  [dim]{cpu_model} • {node.maxcpu}c[/dim]\n"
                f" [bold cyan]Mem[/bold cyan]  {mem_bar} {node.mem_percent:4.1f}%  [dim]{node.mem_used_gib:.1f}/{node.mem_total_gib:.1f} GiB[/dim]\n"
                f" [bold cyan]Disk[/bold cyan] {disk_bar} {node.disk_percent:4.1f}%  [dim]{node.disk_used_gib:.1f}/{node.disk_total_gib:.1f} GiB[/dim]"
            )
            container.mount(Static(node_text, markup=True, classes="node-card"))

    def _make_bar(self, percent: float, width: int = 20) -> str:
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

    # ── Module availability ─────────────────────────────────────

    # Original label text for each nav item (used to rebuild on refresh)
    _NAV_LABELS: dict[str, str] = {
        "nav-vms":         "  \\[1]  Virtual Machines  —  View and manage all VMs and containers",
        "nav-templates":   "  \\[2]  Templates         —  Browse VM and container templates",
        "nav-nodes":       "  \\[3]  Node Info         —  Cluster node details and resources",
        "nav-dns":         "  \\[4]  DNS Management    —  View and manage DNS records",
        "nav-ipam":        "  \\[5]  IPAM Management   —  Manage IP addresses and subnets",
        "nav-create":      "  \\[6]  Provision VM      —  Templates and custom VM creation",
        "nav-ansible":     "  \\[7]  Ansible           —  Manage playbooks and automation",
        "nav-ai-settings": "  \\[8]  AI Settings       —  Configure AI assistant and model",
    }

    def _check_modules(self) -> None:
        """Check module availability and update nav items + banner."""
        try:
            from infraforge.module_status import get_disabled_nav_items, MODULE_NAMES

            disabled = get_disabled_nav_items(self.app.config)
            self._disabled_items = disabled

            for nav_id, original in self._NAV_LABELS.items():
                try:
                    item = self.query_one(f"#{nav_id}", ListItem)
                    label = item.query_one(Label)
                    if nav_id in disabled:
                        names = ", ".join(MODULE_NAMES[m] for m in disabled[nav_id])
                        label.update(
                            f"[dim red]{original}[/dim red]  "
                            f"[bold red]\u2717[/bold red] [dim]Requires: {names}[/dim]"
                        )
                    else:
                        label.update(original)
                except Exception:
                    pass

            banner = self.query_one("#setup-banner", Static)
            if disabled:
                banner.update(
                    "  Some modules are not fully configured.  "
                    "Functionality may be limited.  "
                    "Run infraforge setup to correct."
                )
                banner.remove_class("hidden")
            else:
                banner.add_class("hidden")
        except Exception:
            pass

    def _is_nav_disabled(self, nav_id: str) -> bool:
        """Check if a nav item is disabled; show notification if so."""
        if nav_id in self._disabled_items:
            from infraforge.module_status import MODULE_NAMES
            missing = ", ".join(MODULE_NAMES[m] for m in self._disabled_items[nav_id])
            self.notify(
                f"Not available \u2014 requires: {missing}. Run 'infraforge setup' to configure.",
                severity="error",
                timeout=5,
            )
            return True
        return False

    # ── Navigation ─────────────────────────────────────────────

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
        elif item_id == "nav-ansible":
            self.action_manage_ansible()
        elif item_id == "nav-ai-settings":
            self.action_ai_settings()

    def action_view_vms(self):
        if self._is_nav_disabled("nav-vms"):
            return
        from infraforge.screens.vm_list import VMListScreen
        self.app.push_screen(VMListScreen())

    def action_view_templates(self):
        if self._is_nav_disabled("nav-templates"):
            return
        from infraforge.screens.template_list import TemplateListScreen
        self.app.push_screen(TemplateListScreen())

    def action_view_nodes(self):
        if self._is_nav_disabled("nav-nodes"):
            return
        from infraforge.screens.node_info import NodeInfoScreen
        self.app.push_screen(NodeInfoScreen())

    def action_manage_dns(self):
        if self._is_nav_disabled("nav-dns"):
            return
        from infraforge.screens.dns_screen import DNSScreen
        self.app.push_screen(DNSScreen())

    def action_manage_ipam(self):
        if self._is_nav_disabled("nav-ipam"):
            return
        from infraforge.screens.ipam_screen import IPAMScreen
        self.app.push_screen(IPAMScreen())

    def action_create_vm(self):
        if self._is_nav_disabled("nav-create"):
            return
        from infraforge.screens.provision_menu import ProvisionMenuScreen
        self.app.push_screen(ProvisionMenuScreen())

    def action_manage_ansible(self):
        if self._is_nav_disabled("nav-ansible"):
            return
        from infraforge.screens.ansible_screen import AnsibleScreen
        self.app.push_screen(AnsibleScreen())

    def action_ai_settings(self):
        if self._is_nav_disabled("nav-ai-settings"):
            return
        from infraforge.screens.ai_settings_screen import AISettingsScreen
        self.app.push_screen(AISettingsScreen())

    @work(thread=True, exclusive=True, group="update-check")
    def _check_for_update(self):
        """Check GitHub for a newer release on startup and every 10 minutes."""
        while True:
            try:
                from infraforge.updater import check_for_update
                result = check_for_update()
                if result:
                    self.app.call_from_thread(self._show_update_banner, result)
            except Exception:
                pass
            time.sleep(600)  # Re-check every 10 minutes

    def _check_ai_config(self):
        """Show a hint banner if AI is not configured."""
        try:
            import shutil
            if not shutil.which("claude"):
                banner = self.query_one("#ai-setup-banner", Static)
                banner.update(
                    "  AI features available!  "
                    "Install Claude Code:  npm install -g @anthropic-ai/claude-code  "
                    "then press / to chat"
                )
                banner.remove_class("hidden")
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

    def action_download_template(self):
        """Open the template download screen."""
        from infraforge.screens.template_download_screen import TemplateDownloadScreen
        self.app.push_screen(TemplateDownloadScreen())

    def action_refresh(self):
        self.load_data()
