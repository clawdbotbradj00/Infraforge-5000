"""VM List screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, DataTable
from textual.containers import Container, Horizontal
from textual import work

from rich.text import Text

from infraforge.models import VM, VMStatus, VMType


SORT_FIELDS = ["vmid", "name", "status", "node", "vm_type", "cpu_percent", "mem_percent", "disk_gb"]
SORT_LABELS = ["VMID", "Name", "Status", "Node", "Type", "CPU%", "Memory%", "Disk"]
FILTER_MODES = ["all", "running", "stopped"]
FILTER_LABELS = ["All", "Running", "Stopped"]
GROUP_MODES = ["none", "status", "node", "type"]
GROUP_LABELS = ["No Grouping", "By Status", "By Node", "By Type"]

STATUS_COLORS = {
    VMStatus.RUNNING: "green",
    VMStatus.STOPPED: "red",
    VMStatus.PAUSED: "yellow",
    VMStatus.SUSPENDED: "dark_orange",
    VMStatus.UNKNOWN: "bright_black",
}

NODE_COLORS = [
    "cyan",
    "magenta",
    "bright_blue",
    "bright_green",
    "bright_yellow",
    "bright_magenta",
    "bright_cyan",
]


def _field_index(fields: list[str], name: str, default: int = 0) -> int:
    try:
        return fields.index(name)
    except ValueError:
        return default


class VMListScreen(Screen):
    """Screen displaying all VMs and containers."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("g", "cycle_group", "Group", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._vms: list[VM] = []
        self._sort_index = 0
        self._sort_reverse = False
        self._filter_index = 0
        self._group_index = 0
        self._node_color_map: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="vm-list-container"):
            # Summary banner
            yield Static("", id="vm-summary-banner", markup=True)

            # Control bar
            with Horizontal(id="vm-controls"):
                yield Static("Filter: All", id="vm-filter-label")
                yield Static("Sort: VMID ▲", id="vm-sort-label")
                yield Static("Group: None", id="vm-group-label")
                yield Static("", id="vm-count-label")

            yield DataTable(id="vm-table")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#vm-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        # Add columns with Rich Text for colored headers
        table.add_column(Text("", style="bold"), key="icon", width=3)
        table.add_column(Text("VMID", style="bold cyan"), key="vmid", width=7)
        table.add_column(Text("Name", style="bold cyan"), key="name", width=30)
        table.add_column(Text("Type", style="bold cyan"), key="type", width=6)
        table.add_column(Text("Status", style="bold cyan"), key="status", width=10)
        table.add_column(Text("Node", style="bold cyan"), key="node", width=10)
        table.add_column(Text("CPU%", style="bold cyan"), key="cpu", width=7)
        table.add_column(Text("Memory", style="bold cyan"), key="mem", width=10)
        table.add_column(Text("Disk", style="bold cyan"), key="disk", width=10)
        table.add_column(Text("Uptime", style="bold cyan"), key="uptime", width=14)
        table.add_column(Text("Tags", style="bold cyan"), key="tags")

        # Restore saved preferences
        prefs = self.app.preferences.vm_list
        self._sort_index = _field_index(SORT_FIELDS, prefs.sort_field)
        self._sort_reverse = prefs.sort_reverse
        self._filter_index = _field_index(FILTER_MODES, prefs.filter_mode)
        self._group_index = _field_index(GROUP_MODES, prefs.group_mode)

        self.load_vms()

    def _assign_node_colors(self, vms: list[VM]):
        """Assign a unique color to each node name."""
        nodes = sorted(set(v.node for v in vms))
        self._node_color_map = {
            node: NODE_COLORS[i % len(NODE_COLORS)]
            for i, node in enumerate(nodes)
        }

    @work(thread=True)
    def load_vms(self):
        """Load VM data from Proxmox."""
        try:
            vms = self.app.proxmox.get_all_vms()
            self.app.call_from_thread(self._set_vms, vms)
        except Exception as e:
            self.app.call_from_thread(self._show_load_error, str(e))

    def _set_vms(self, vms: list[VM]):
        self._vms = vms
        self._assign_node_colors(vms)
        self._refresh_table()

    def _show_load_error(self, error: str):
        table = self.query_one("#vm-table", DataTable)
        table.clear()
        self.query_one("#vm-summary-banner", Static).update(
            f"[bold red]  Error loading VMs: {error}[/bold red]"
        )

    def _get_filtered_vms(self) -> list[VM]:
        mode = FILTER_MODES[self._filter_index]
        if mode == "running":
            vms = [v for v in self._vms if v.status == VMStatus.RUNNING]
        elif mode == "stopped":
            vms = [v for v in self._vms if v.status == VMStatus.STOPPED]
        else:
            vms = list(self._vms)
        return vms

    def _get_sort_key(self, vm: VM):
        field = SORT_FIELDS[self._sort_index]
        return getattr(vm, field, 0)

    def _refresh_table(self):
        table = self.query_one("#vm-table", DataTable)
        table.clear()

        vms = self._get_filtered_vms()

        # Update summary banner
        total = len(self._vms)
        running = sum(1 for v in self._vms if v.status == VMStatus.RUNNING)
        stopped = sum(1 for v in self._vms if v.status == VMStatus.STOPPED)
        showing = len(vms)
        nodes = len(set(v.node for v in self._vms))

        self.query_one("#vm-summary-banner", Static).update(
            f"  [bold]Virtual Machines[/bold]  [dim]|[/dim]  "
            f"[bold]{total}[/bold] total  [dim]|[/dim]  "
            f"[bold green]{running}[/bold green] running  [dim]|[/dim]  "
            f"[bold red]{stopped}[/bold red] stopped  [dim]|[/dim]  "
            f"[bold cyan]{nodes}[/bold cyan] nodes  [dim]|[/dim]  "
            f"Showing [bold]{showing}[/bold]"
        )

        # Sort
        try:
            vms.sort(key=self._get_sort_key, reverse=self._sort_reverse)
        except TypeError:
            vms.sort(key=lambda v: str(self._get_sort_key(v)), reverse=self._sort_reverse)

        # Group
        group_mode = GROUP_MODES[self._group_index]
        if group_mode != "none":
            grouped = {}
            for vm in vms:
                if group_mode == "status":
                    key = vm.status.value.upper()
                elif group_mode == "node":
                    key = vm.node
                elif group_mode == "type":
                    key = vm.type_label
                else:
                    key = ""
                grouped.setdefault(key, []).append(vm)

            for group_name, group_vms in sorted(grouped.items()):
                # Add group header row with styled separator
                header_text = Text(f" {group_name} ({len(group_vms)}) ", style="bold bright_white on dark_blue")
                divider = Text("─" * 40, style="dim")
                table.add_row(
                    Text(""), header_text,
                    divider, Text(""), Text(""), Text(""),
                    Text(""), Text(""), Text(""), Text(""), Text(""),
                    key=f"group_{group_name}",
                )
                for vm in group_vms:
                    self._add_vm_row(table, vm)
        else:
            for vm in vms:
                self._add_vm_row(table, vm)

        # Update control labels
        sort_arrow = "▼" if self._sort_reverse else "▲"
        filter_mode = FILTER_LABELS[self._filter_index]
        filter_colors = {"All": "white", "Running": "green", "Stopped": "red"}
        fc = filter_colors.get(filter_mode, "white")

        self.query_one("#vm-filter-label", Static).update(
            f"[bold]Filter:[/bold] [{fc}]{filter_mode}[/{fc}]"
        )
        self.query_one("#vm-sort-label", Static).update(
            f"[bold]Sort:[/bold] [cyan]{SORT_LABELS[self._sort_index]}[/cyan] {sort_arrow}"
        )
        self.query_one("#vm-group-label", Static).update(
            f"[bold]Group:[/bold] [magenta]{GROUP_LABELS[self._group_index]}[/magenta]"
        )
        self.query_one("#vm-count-label", Static).update(
            f"[dim]{showing} VMs[/dim]"
        )

    def _add_vm_row(self, table: DataTable, vm: VM):
        status_color = STATUS_COLORS.get(vm.status, "white")
        node_color = self._node_color_map.get(vm.node, "white")
        type_color = "bright_blue" if vm.vm_type == VMType.QEMU else "bright_magenta"

        # Status icon with color
        icon = Text(vm.status_icon, style=f"bold {status_color}")

        # VMID
        vmid = Text(str(vm.vmid), style="bold")

        # Name - bold for running VMs
        name_style = "bold" if vm.status == VMStatus.RUNNING else "dim"
        name = Text(vm.name, style=name_style)

        # Type badge
        vm_type = Text(vm.type_label, style=type_color)

        # Status with full color
        status = Text(vm.status.value.upper(), style=f"bold {status_color}")

        # Node with assigned color
        node = Text(vm.node, style=node_color)

        # CPU with color gradient
        cpu_val = vm.cpu_percent
        if cpu_val > 80:
            cpu_color = "bold red"
        elif cpu_val > 50:
            cpu_color = "yellow"
        elif cpu_val > 0:
            cpu_color = "green"
        else:
            cpu_color = "dim"
        cpu = Text(f"{cpu_val:.1f}%", style=cpu_color)

        # Memory with color
        mem_val = vm.mem_percent
        if mem_val > 80:
            mem_color = "bold red"
        elif mem_val > 50:
            mem_color = "yellow"
        elif mem_val > 0:
            mem_color = "green"
        else:
            mem_color = "dim"
        mem = Text(f"{vm.mem_gb:.1f} GB", style=mem_color)

        # Disk
        disk = Text(f"{vm.disk_gb:.1f} GB", style="bright_white" if vm.disk_gb > 0 else "dim")

        # Uptime
        if vm.status == VMStatus.RUNNING and vm.uptime > 0:
            uptime = Text(vm.uptime_str, style="green")
        else:
            uptime = Text("—", style="dim")

        # Tags
        if vm.tags:
            tag_parts = vm.tags.split(";") if ";" in vm.tags else vm.tags.split(",")
            tags = Text()
            for i, tag in enumerate(tag_parts):
                tag = tag.strip()
                if i > 0:
                    tags.append(" ")
                tags.append(f"[{tag}]", style="bold bright_yellow")
            tags = tags
        else:
            tags = Text("—", style="dim")

        table.add_row(
            icon, vmid, name, vm_type, status, node,
            cpu, mem, disk, uptime, tags,
            key=f"vm_{vm.vmid}_{vm.node}",
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Handle Enter key on a row."""
        row_key = str(event.row_key.value) if event.row_key else ""
        if row_key.startswith("vm_"):
            parts = row_key.split("_", 2)
            if len(parts) >= 3:
                vmid = int(parts[1])
                node = parts[2]
                # Find the VM
                vm = next((v for v in self._vms if v.vmid == vmid and v.node == node), None)
                if vm:
                    from infraforge.screens.vm_detail import VMDetailScreen
                    self.app.push_screen(VMDetailScreen(vm))

    def action_go_back(self):
        self.app.pop_screen()

    def _save_preferences(self) -> None:
        prefs = self.app.preferences.vm_list
        prefs.sort_field = SORT_FIELDS[self._sort_index]
        prefs.sort_reverse = self._sort_reverse
        prefs.filter_mode = FILTER_MODES[self._filter_index]
        prefs.group_mode = GROUP_MODES[self._group_index]
        self.app.preferences.save()

    def action_cycle_filter(self):
        self._filter_index = (self._filter_index + 1) % len(FILTER_MODES)
        self._refresh_table()
        self._save_preferences()

    def action_cycle_sort(self):
        old_index = self._sort_index
        self._sort_index = (self._sort_index + 1) % len(SORT_FIELDS)
        if self._sort_index == old_index:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_reverse = False
        self._refresh_table()
        self._save_preferences()

    def action_cycle_group(self):
        self._group_index = (self._group_index + 1) % len(GROUP_MODES)
        self._refresh_table()
        self._save_preferences()

    def action_refresh(self):
        self.load_vms()
