"""VM Detail screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.containers import Container, Horizontal, VerticalScroll
from textual import work

from infraforge.models import VM, VMStatus, VMType


def format_bytes(b: int) -> str:
    """Format bytes to human readable."""
    if b == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


class VMDetailScreen(Screen):
    """Detail view for a specific VM."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self, vm: VM):
        super().__init__()
        self.vm = vm

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="vm-detail-container"):
            # Header
            status_color = {
                VMStatus.RUNNING: "green",
                VMStatus.STOPPED: "red",
                VMStatus.PAUSED: "yellow",
            }.get(self.vm.status, "white")

            yield Static(
                f"  {self.vm.status_icon} [{status_color}]{self.vm.status.value.upper()}[/{status_color}]"
                f"  │  [bold]{self.vm.name}[/bold]  │  {self.vm.type_label} {self.vm.vmid}"
                f"  │  Node: {self.vm.node}",
                id="vm-detail-header",
                markup=True,
            )

            # Current Status section
            with Container(classes="detail-section"):
                yield Static("Current Status", classes="detail-section-title")
                yield Static(self._build_status_text(), id="status-info", markup=True)

            # Configuration section
            with Container(classes="detail-section"):
                yield Static("Configuration", classes="detail-section-title")
                yield Static("Loading...", id="config-info", markup=True)

            # Snapshots section
            with Container(classes="detail-section"):
                yield Static("Snapshots", classes="detail-section-title")
                yield Static("Loading...", id="snapshots-info", markup=True)
        yield Footer()

    def on_mount(self):
        self.load_details()

    @work(thread=True)
    def load_details(self):
        """Load detailed VM information."""
        try:
            detail = self.app.proxmox.get_vm_detail(
                self.vm.node, self.vm.vmid, self.vm.vm_type
            )
            snapshots = self.app.proxmox.get_vm_snapshots(
                self.vm.node, self.vm.vmid, self.vm.vm_type
            )
            self.app.call_from_thread(self._update_details, detail, snapshots)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    def _build_status_text(self) -> str:
        vm = self.vm
        lines = []
        lines.append(f"  [bold]Status:[/bold]      {vm.status.value}")
        lines.append(f"  [bold]Type:[/bold]        {vm.type_label} ({'QEMU/KVM' if vm.vm_type == VMType.QEMU else 'LXC Container'})")
        lines.append(f"  [bold]Node:[/bold]        {vm.node}")

        if vm.status == VMStatus.RUNNING:
            lines.append(f"  [bold]CPU Usage:[/bold]   {vm.cpu_percent:.1f}% ({vm.cpus} cores)")
            lines.append(f"  [bold]Memory:[/bold]      {format_bytes(vm.mem)} / {format_bytes(vm.maxmem)} ({vm.mem_percent:.1f}%)")
            lines.append(f"  [bold]Uptime:[/bold]      {vm.uptime_str}")
            lines.append(f"  [bold]Net In:[/bold]      {format_bytes(vm.netin)}")
            lines.append(f"  [bold]Net Out:[/bold]     {format_bytes(vm.netout)}")
            if vm.pid:
                lines.append(f"  [bold]PID:[/bold]         {vm.pid}")
        else:
            lines.append(f"  [bold]Memory:[/bold]      {format_bytes(vm.maxmem)} (allocated)")
            lines.append(f"  [bold]Disk:[/bold]        {vm.disk_gb:.1f} GB")

        if vm.tags:
            lines.append(f"  [bold]Tags:[/bold]        {vm.tags}")

        return "\n".join(lines)

    def _update_details(self, detail: dict, snapshots: list):
        config = detail.get("config", {})

        # Build config text
        config_lines = []

        # CPU
        cores = config.get("cores", "N/A")
        sockets = config.get("sockets", 1)
        cpu_type = config.get("cpu", "default")
        config_lines.append(f"  [bold]CPU:[/bold]         {cores} cores x {sockets} socket(s)  [dim](type: {cpu_type})[/dim]")

        # Memory
        memory = config.get("memory", "N/A")
        balloon = config.get("balloon", "")
        mem_str = f"  [bold]Memory:[/bold]      {memory} MB"
        if balloon:
            mem_str += f"  [dim](balloon: {balloon} MB)[/dim]"
        config_lines.append(mem_str)

        # BIOS / Machine
        bios = config.get("bios", "seabios")
        machine = config.get("machine", "default")
        config_lines.append(f"  [bold]BIOS:[/bold]        {bios}  [dim](machine: {machine})[/dim]")

        # OS Type
        ostype = config.get("ostype", "N/A")
        config_lines.append(f"  [bold]OS Type:[/bold]     {ostype}")

        # Boot order
        boot = config.get("boot", "N/A")
        config_lines.append(f"  [bold]Boot:[/bold]        {boot}")

        # Disks
        config_lines.append(f"  [bold]─── Disks ───[/bold]")
        for key, val in sorted(config.items()):
            if key.startswith(("scsi", "virtio", "ide", "sata", "efidisk", "rootfs", "mp")):
                config_lines.append(f"  [bold]{key}:[/bold]  {val}")

        # Network
        config_lines.append(f"  [bold]─── Network ───[/bold]")
        for key, val in sorted(config.items()):
            if key.startswith("net"):
                config_lines.append(f"  [bold]{key}:[/bold]  {val}")

        # Other interesting config
        config_lines.append(f"  [bold]─── Other ───[/bold]")
        skip_keys = {"digest", "description"}
        shown_prefixes = ("scsi", "virtio", "ide", "sata", "efidisk", "net", "rootfs", "mp",
                          "cores", "sockets", "cpu", "memory", "balloon", "bios", "machine",
                          "ostype", "boot", "name")
        for key, val in sorted(config.items()):
            if key in skip_keys:
                continue
            if any(key.startswith(p) for p in shown_prefixes):
                continue
            val_str = str(val)
            if len(val_str) > 100:
                val_str = val_str[:100] + "..."
            config_lines.append(f"  [bold]{key}:[/bold]  {val_str}")

        self.query_one("#config-info", Static).update("\n".join(config_lines))

        # Build snapshots text
        if snapshots:
            snap_lines = []
            for snap in snapshots:
                name = snap.get("name", "?")
                if name == "current":
                    continue
                desc = snap.get("description", "")
                snaptime = snap.get("snaptime", "")
                vmstate = "with RAM" if snap.get("vmstate") else "disk only"
                snap_lines.append(f"  • [bold]{name}[/bold]  ({vmstate})")
                if desc:
                    snap_lines.append(f"    {desc}")
                if snaptime:
                    from datetime import datetime
                    try:
                        ts = datetime.fromtimestamp(snaptime).strftime("%Y-%m-%d %H:%M:%S")
                        snap_lines.append(f"    [dim]{ts}[/dim]")
                    except (ValueError, OSError):
                        pass

            if snap_lines:
                self.query_one("#snapshots-info", Static).update("\n".join(snap_lines))
            else:
                self.query_one("#snapshots-info", Static).update("  [dim]No snapshots[/dim]")
        else:
            self.query_one("#snapshots-info", Static).update("  [dim]No snapshots[/dim]")

    def _show_error(self, error: str):
        self.query_one("#config-info", Static).update(f"  [red]Error: {error}[/red]")
        self.query_one("#snapshots-info", Static).update("")

    def action_go_back(self):
        self.app.pop_screen()

    def action_refresh(self):
        self.load_details()
