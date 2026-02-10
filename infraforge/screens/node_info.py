"""Node info screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.containers import Container, VerticalScroll
from textual import work

from infraforge.models import NodeInfo, StorageInfo


def format_bytes(b: int) -> str:
    if b == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def make_bar(percent: float, width: int = 25) -> str:
    filled = int(percent / 100 * width)
    empty = width - filled
    if percent > 80:
        color = "red"
    elif percent > 60:
        color = "yellow"
    else:
        color = "green"
    return f"[{color}]{'█' * filled}{'░' * empty}[/{color}] {percent:5.1f}%"


class NodeInfoScreen(Screen):
    """Screen showing cluster node details."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="node-container"):
            yield Static("Cluster Nodes", classes="section-title")
            yield Static("Loading...", id="node-content", markup=True)
            yield Static("\nStorage", classes="section-title")
            yield Static("Loading...", id="storage-content", markup=True)
            yield Static("\nProxmox Version", classes="section-title")
            yield Static("Loading...", id="version-content", markup=True)
        yield Footer()

    def on_mount(self):
        self.load_data()

    @work(thread=True)
    def load_data(self):
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=3) as pool:
                fut_nodes = pool.submit(self.app.proxmox.get_node_info)
                fut_stor = pool.submit(self.app.proxmox.get_storage_info)
                fut_ver = pool.submit(self.app.proxmox.get_version)

                nodes = fut_nodes.result()
                storages = fut_stor.result()
                version = fut_ver.result()

            self.app.call_from_thread(self._update, nodes, storages, version)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    def _update(self, nodes: list[NodeInfo], storages: list[StorageInfo], version: dict):
        # Build node info
        node_lines = []
        for node in nodes:
            status_color = "green" if node.status == "online" else "red"
            node_lines.append(
                f"\n  [{status_color}]●[/{status_color}] [bold]{node.node}[/bold]"
                f"  —  {node.status}"
            )
            node_lines.append(f"    CPU:     {make_bar(node.cpu_percent)}  ({node.maxcpu} cores)")
            node_lines.append(f"    Memory:  {make_bar(node.mem_percent)}  ({format_bytes(node.mem)} / {format_bytes(node.maxmem)})")
            node_lines.append(f"    Disk:    {make_bar(node.disk_percent)}  ({format_bytes(node.disk)} / {format_bytes(node.maxdisk)})")
            node_lines.append(f"    Uptime:  {node.uptime_str}")

        self.query_one("#node-content", Static).update("\n".join(node_lines) if node_lines else "  No nodes found")

        # Build storage info
        storage_lines = []
        # Group by node
        by_node: dict[str, list[StorageInfo]] = {}
        for s in storages:
            by_node.setdefault(s.node, []).append(s)

        for node_name, node_storages in sorted(by_node.items()):
            storage_lines.append(f"\n  [bold]{node_name}[/bold]")
            for s in sorted(node_storages, key=lambda x: x.storage):
                shared_flag = " [dim](shared)[/dim]" if s.shared else ""
                active_flag = "" if s.active else " [red](inactive)[/red]"

                if s.total > 0:
                    bar = make_bar(s.used_percent)
                    storage_lines.append(
                        f"    {s.storage:<15} [{s.storage_type}]{shared_flag}{active_flag}"
                    )
                    storage_lines.append(
                        f"      Usage:   {bar}  ({s.used_display} / {s.total_display})"
                    )
                    storage_lines.append(
                        f"      Content: {s.content}"
                    )
                else:
                    storage_lines.append(
                        f"    {s.storage:<15} [{s.storage_type}] {s.content}{shared_flag}{active_flag}"
                    )

        self.query_one("#storage-content", Static).update(
            "\n".join(storage_lines) if storage_lines else "  No storage found"
        )

        # Version info
        if version:
            ver_lines = []
            ver_lines.append(f"  [bold]Version:[/bold]  {version.get('version', 'N/A')}")
            ver_lines.append(f"  [bold]Release:[/bold]  {version.get('release', 'N/A')}")
            ver_lines.append(f"  [bold]Repo ID:[/bold] {version.get('repoid', 'N/A')}")
            self.query_one("#version-content", Static).update("\n".join(ver_lines))
        else:
            self.query_one("#version-content", Static).update("  Version info unavailable")

    def _show_error(self, error: str):
        self.query_one("#node-content", Static).update(f"  [red]Error: {error}[/red]")
        self.query_one("#storage-content", Static).update("")
        self.query_one("#version-content", Static).update("")

    def action_go_back(self):
        self.app.pop_screen()

    def action_refresh(self):
        self.load_data()
