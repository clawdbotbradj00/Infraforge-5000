"""phpIPAM management screen for InfraForge.

Provides a hierarchical Tree view of subnets and addresses with a detail
panel, plus a flat DataTable for VLANs.  Subnets are expandable nodes
that lazy-load their addresses on first expand.  Sorting and filtering
are context-aware based on the highlighted node type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Header,
    Footer,
    Static,
    DataTable,
    Input,
    Button,
    Select,
    Label,
    Tree,
)
from textual.widgets._tree import TreeNode
from textual.containers import Container, Horizontal, Vertical
from textual import work, on

from rich.text import Text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIEW_TREE = "tree"
VIEW_VLANS = "vlans"
VIEWS = [VIEW_TREE, VIEW_VLANS]
VIEW_LABELS = ["Subnets & Addresses", "VLANs"]

# Sort fields for subnets (in tree view)
SUBNET_SORT_FIELDS = ["subnet", "description", "vlan", "usage"]
SUBNET_SORT_LABELS = ["Subnet", "Description", "VLAN", "Usage %"]

# Sort fields for addresses (within expanded subnets)
ADDRESS_SORT_FIELDS = ["ip", "hostname", "status", "description", "last_seen"]
ADDRESS_SORT_LABELS = ["IP", "Hostname", "Status", "Description", "Last Seen"]

# Sort fields for VLANs
VLAN_SORT_FIELDS = ["number", "name", "description"]
VLAN_SORT_LABELS = ["Number", "Name", "Description"]

# Filter modes for subnets
SUBNET_FILTER_MODES = ["all", "low", "medium", "high"]
SUBNET_FILTER_LABELS = ["All", "Low (<60%)", "Medium (60-80%)", "High (>80%)"]

# Filter modes for addresses
ADDRESS_FILTER_MODES = ["all", "active", "reserved", "offline", "dhcp"]
ADDRESS_FILTER_LABELS = ["All", "Active", "Reserved", "Offline", "DHCP"]

VLAN_FILTER_MODES = ["all"]
VLAN_FILTER_LABELS = ["All"]

# phpIPAM tag IDs mapped to status labels
TAG_STATUS_MAP = {
    "1": "Offline",
    "2": "Active",
    "3": "Reserved",
    "4": "DHCP",
}

STATUS_COLORS = {
    "Active": "#2E7D32",
    "Reserved": "yellow",
    "Offline": "red",
    "DHCP": "cyan",
    "Unknown": "bright_black",
}

TAG_VALUES = {
    "Offline": 1,
    "Active": 2,
    "Reserved": 3,
    "DHCP": 4,
}

TAG_OPTIONS_FOR_INPUT = [
    ("Active", "Active"),
    ("Reserved", "Reserved"),
    ("DHCP", "DHCP"),
    ("Offline", "Offline"),
]


# ---------------------------------------------------------------------------
# Tree data model
# ---------------------------------------------------------------------------

@dataclass
class IPAMNodeData:
    """Data attached to each node in the IPAM tree."""
    kind: Literal["subnet", "address", "placeholder"]
    record: dict = field(default_factory=dict)
    subnet_id: str = ""
    addresses_loaded: bool = False


# ---------------------------------------------------------------------------
# Modal: Address input screen (reserve / edit)
# ---------------------------------------------------------------------------

class AddressInputScreen(ModalScreen[Optional[dict]]):
    """Modal screen for reserving or editing an IP address."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        subnet_cidr: str = "",
        ip: str = "",
        hostname: str = "",
        description: str = "",
        tag: str = "Active",
        title: str = "Reserve IP Address",
        ip_editable: bool = True,
    ) -> None:
        super().__init__()
        self._subnet_cidr = subnet_cidr
        self._initial_ip = ip
        self._initial_hostname = hostname
        self._initial_description = description
        self._initial_tag = tag
        self._title = title
        self._ip_editable = ip_editable

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container"):
            with Vertical(classes="modal-box"):
                yield Static(self._title, classes="modal-title")
                if self._subnet_cidr:
                    yield Static(
                        f"[dim]Subnet: {self._subnet_cidr}[/dim]", markup=True,
                    )

                yield Label("IP Address:")
                yield Input(
                    value=self._initial_ip,
                    placeholder="10.0.0.50",
                    id="addr-ip-input",
                    disabled=not self._ip_editable,
                )

                yield Label("Hostname:")
                yield Input(
                    value=self._initial_hostname,
                    placeholder="webserver01",
                    id="addr-hostname-input",
                )

                yield Label("Description:")
                yield Input(
                    value=self._initial_description,
                    placeholder="Web server",
                    id="addr-desc-input",
                )

                yield Label("Status:")
                yield Select(
                    TAG_OPTIONS_FOR_INPUT,
                    value=self._initial_tag,
                    id="addr-tag-select",
                )

                with Horizontal(classes="modal-buttons"):
                    yield Button(
                        "Save", variant="primary", id="addr-save-btn",
                    )
                    yield Button(
                        "Cancel", variant="default", id="addr-cancel-btn",
                    )

    def on_mount(self) -> None:
        if self._ip_editable:
            self.query_one("#addr-ip-input", Input).focus()
        else:
            self.query_one("#addr-hostname-input", Input).focus()

    @on(Button.Pressed, "#addr-save-btn")
    def _on_save(self, event: Button.Pressed) -> None:
        self._try_submit()

    @on(Button.Pressed, "#addr-cancel-btn")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _try_submit(self) -> None:
        ip = self.query_one("#addr-ip-input", Input).value.strip()
        hostname = self.query_one("#addr-hostname-input", Input).value.strip()
        description = self.query_one("#addr-desc-input", Input).value.strip()
        tag_select = self.query_one("#addr-tag-select", Select)
        tag = str(tag_select.value) if tag_select.value is not Select.BLANK else "Active"

        if not ip:
            return

        self.dismiss({
            "ip": ip,
            "hostname": hostname,
            "description": description,
            "tag": tag,
        })


# ---------------------------------------------------------------------------
# Modal: Confirmation dialog
# ---------------------------------------------------------------------------

class IPAMConfirmScreen(ModalScreen[bool]):
    """Simple yes/no confirmation modal for IPAM operations."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
    ]

    def __init__(self, message: str, title: str = "Confirm") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container"):
            with Vertical(classes="modal-box"):
                yield Static(self._title, classes="modal-title")
                yield Static(self._message, markup=True)
                with Horizontal(classes="modal-buttons"):
                    yield Button("Yes", variant="error", id="confirm-yes-btn")
                    yield Button("No", variant="default", id="confirm-no-btn")

    @on(Button.Pressed, "#confirm-yes-btn")
    def _on_yes(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no-btn")
    def _on_no(self, event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Main IPAM Screen
# ---------------------------------------------------------------------------

class IPAMScreen(Screen):
    """Screen for viewing and managing phpIPAM subnets, addresses, and VLANs."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("tab", "next_tab", "Next Tab", show=True),
        Binding("shift+tab", "prev_tab", "Prev Tab", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "add_item", "Add", show=True),
        Binding("e", "edit_item", "Edit", show=True),
        Binding("d", "delete_item", "Delete", show=True),
        Binding("x", "scan_subnet", "Scan", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._active_view: str = VIEW_TREE
        self._loading: bool = False
        self._ipam_healthy: bool = False

        # Data stores
        self._subnets: list[dict] = []
        self._vlans: list[dict] = []
        self._address_cache: dict[str, list[dict]] = {}

        # Sort / filter state — subnets
        self._subnet_sort_index: int = 0
        self._subnet_sort_reverse: bool = False
        self._subnet_filter_index: int = 0

        # Sort / filter state — addresses
        self._address_sort_index: int = 0
        self._address_sort_reverse: bool = False
        self._address_filter_index: int = 0

        # Sort / filter state — VLANs
        self._vlan_sort_index: int = 0
        self._vlan_sort_reverse: bool = False
        self._vlan_filter_index: int = 0

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="ipam-container"):
            yield Static("IPAM Management", classes="section-title")

            # Overview panel
            yield Static(
                "Loading IPAM info...", id="ipam-overview", markup=True,
            )

            # Tab bar
            yield Horizontal(id="ipam-tab-bar")

            # Controls bar
            with Horizontal(id="ipam-controls"):
                yield Static("Filter: All", id="ipam-filter-label")
                yield Static("Sort: Subnet", id="ipam-sort-label")
                yield Static("", id="ipam-count-label")

            # Tree view (subnets & addresses)
            with Horizontal(id="ipam-main-content"):
                yield Tree("IPAM", id="ipam-tree")
                with Container(id="ipam-detail-panel"):
                    yield Static("[bold]Details[/bold]", id="ipam-detail-title", markup=True)
                    yield Static(
                        "[dim]Select an item to view details.[/dim]",
                        id="ipam-detail-content",
                        markup=True,
                    )

            # Data table (VLANs only)
            yield DataTable(id="ipam-table", cursor_type="row")

            # Status bar
            yield Static("", id="ipam-status-bar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ipam-table", DataTable)
        table.zebra_stripes = True
        table.display = False  # Hidden initially (tree view is default)

        tree = self.query_one("#ipam-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3

        self._render_tab_bar()

        # Check if IPAM is configured
        ipam_cfg = self.app.config.ipam
        if not ipam_cfg.url or not ipam_cfg.provider:
            self._show_not_configured()
        else:
            self._load_initial_data()

    # ------------------------------------------------------------------
    # Tab bar rendering
    # ------------------------------------------------------------------

    def _render_tab_bar(self) -> None:
        bar = self.query_one("#ipam-tab-bar", Horizontal)
        bar.remove_children()

        for idx, label in enumerate(VIEW_LABELS):
            view_key = VIEWS[idx]
            if view_key == self._active_view:
                label_text = f"[bold][{label}][/bold]"
                classes = "ipam-tab-btn -active"
            else:
                label_text = f"[dim][{label}][/dim]"
                classes = "ipam-tab-btn"
            btn = Static(label_text, markup=True, classes=classes)
            bar.mount(btn)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self) -> None:
        self._loading = True
        self.app.call_from_thread(
            self._set_status, "Connecting to phpIPAM...",
        )

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)

            healthy = client.check_health()
            self._ipam_healthy = healthy

            if not healthy:
                self.app.call_from_thread(
                    self._show_error,
                    "Cannot reach phpIPAM server. Check URL and credentials.",
                )
                self._loading = False
                return

            self.app.call_from_thread(
                self._set_status, "Loading subnets...",
            )

            try:
                subnets = client.get_subnets()
                self._subnets = subnets if isinstance(subnets, list) else []
            except IPAMError as exc:
                self._subnets = []
                self.app.call_from_thread(
                    self._show_error, f"Failed to load subnets: {exc}",
                )
                self._loading = False
                return

            try:
                vlans = client.get_vlans()
                self._vlans = vlans if isinstance(vlans, list) else []
            except IPAMError:
                self._vlans = []

            self.app.call_from_thread(self._update_overview)
            self.app.call_from_thread(self._build_subnet_tree)
            self.app.call_from_thread(self._update_controls)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {len(self._subnets)} subnets loaded",
            )

        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))
        finally:
            self._loading = False

    @work(thread=True)
    def _load_vlans(self) -> None:
        self._loading = True
        self.app.call_from_thread(self._set_status, "Loading VLANs...")

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            vlans = client.get_vlans()
            self._vlans = vlans if isinstance(vlans, list) else []

            self.app.call_from_thread(self._populate_vlan_table)
            self.app.call_from_thread(self._update_controls)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {len(self._vlans)} VLANs loaded",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load VLANs: {exc}[/red]",
            )
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # Tree: build from subnet data
    # ------------------------------------------------------------------

    def _build_subnet_tree(self) -> None:
        """Build the hierarchical subnet tree, preserving expansion state."""
        tree = self.query_one("#ipam-tree", Tree)

        # Remember which subnets were expanded
        expanded_ids: set[str] = set()
        for node in self._iter_subnet_nodes(tree.root):
            if node.data and node.data.kind == "subnet" and node.is_expanded:
                expanded_ids.add(node.data.subnet_id)

        tree.clear()

        filtered = self._get_filtered_subnets()

        # Build parent-child map from masterSubnetId
        subnet_by_id = {str(s.get("id", "")): s for s in filtered}
        children_map: dict[str, list[dict]] = {}
        top_level: list[dict] = []

        for s in filtered:
            master = str(s.get("masterSubnetId", "0"))
            if master == "0" or master not in subnet_by_id:
                top_level.append(s)
            else:
                children_map.setdefault(master, []).append(s)

        def add_subnet_node(parent_node: TreeNode, subnet: dict) -> None:
            subnet_id = str(subnet.get("id", ""))
            label = _make_subnet_label(subnet, self._vlans)
            node_data = IPAMNodeData(
                kind="subnet",
                record=subnet,
                subnet_id=subnet_id,
                addresses_loaded=False,
            )
            child_node = parent_node.add(label, data=node_data)
            # Placeholder so expand arrow appears
            child_node.add_leaf(
                Text("Loading addresses...", style="dim"),
                data=IPAMNodeData(kind="placeholder"),
            )

            # Recursively add child subnets
            for child_subnet in children_map.get(subnet_id, []):
                add_subnet_node(child_node, child_subnet)

            # Re-expand if it was previously expanded
            if subnet_id in expanded_ids:
                child_node.expand()

        for subnet in top_level:
            add_subnet_node(tree.root, subnet)

        self._update_controls()

    def _iter_subnet_nodes(self, root: TreeNode) -> list[TreeNode]:
        """Collect all subnet-type nodes in the tree."""
        result = []
        for child in root.children:
            if child.data and hasattr(child.data, 'kind') and child.data.kind == "subnet":
                result.append(child)
                result.extend(self._iter_subnet_nodes(child))
        return result

    # ------------------------------------------------------------------
    # Tree: lazy-load addresses on expand
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, 'kind'):
            return
        if node.data.kind != "subnet":
            return
        if node.data.addresses_loaded:
            return
        self._lazy_load_addresses(node)

    @work(thread=True)
    def _lazy_load_addresses(self, node: TreeNode) -> None:
        subnet_id = node.data.subnet_id
        subnet = node.data.record
        subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
        self.app.call_from_thread(
            self._set_status, f"Loading addresses for {subnet_cidr}...",
        )

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            addresses = client.get_subnet_addresses(subnet_id)
            if not isinstance(addresses, list):
                addresses = []

            self._address_cache[subnet_id] = addresses
            self.app.call_from_thread(self._populate_address_nodes, node, addresses)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {subnet_cidr} | "
                f"{len(addresses)} addresses",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load addresses: {exc}[/red]",
            )

    def _populate_address_nodes(
        self, parent_node: TreeNode, addresses: list[dict],
    ) -> None:
        """Remove placeholder and add sorted/filtered address leaf nodes."""
        parent_node.remove_children()

        sorted_addrs = self._sort_addresses(addresses)
        filtered_addrs = self._filter_addresses(sorted_addrs)

        for addr in filtered_addrs:
            label = _make_address_label(addr)
            data = IPAMNodeData(
                kind="address",
                record=addr,
                subnet_id=parent_node.data.subnet_id,
            )
            parent_node.add_leaf(label, data=data)

        if not filtered_addrs:
            parent_node.add_leaf(
                Text("(no addresses)", style="dim italic"),
                data=IPAMNodeData(kind="placeholder"),
            )

        parent_node.data.addresses_loaded = True

    def _refresh_subnet_addresses(self, subnet_id: str) -> None:
        """Re-fetch and re-render addresses for a specific subnet node."""
        tree = self.query_one("#ipam-tree", Tree)
        for node in self._iter_subnet_nodes(tree.root):
            if node.data and node.data.subnet_id == subnet_id:
                node.data.addresses_loaded = False
                if node.is_expanded:
                    self._lazy_load_addresses(node)
                break

    # ------------------------------------------------------------------
    # Tree: detail panel on highlight
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, 'kind'):
            self._clear_detail_panel()
            return

        if node.data.kind == "subnet":
            self._show_subnet_detail(node.data.record)
        elif node.data.kind == "address":
            self._show_address_detail(node.data.record, node.data.subnet_id)
        else:
            self._clear_detail_panel()

    def _show_subnet_detail(self, subnet: dict) -> None:
        detail = self.query_one("#ipam-detail-content", Static)
        title = self.query_one("#ipam-detail-title", Static)
        title.update("[bold]Subnet Details[/bold]")

        cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
        desc = subnet.get("description") or "-"
        vlan_id = str(subnet.get("vlanId") or "0")
        vlan_display = self._vlan_display(vlan_id)
        master = str(subnet.get("masterSubnetId", "0"))
        master_display = "Top-level" if master == "0" else f"Subnet #{master}"
        section_id = subnet.get("sectionId", "?")

        usage = subnet.get("usage", {})
        used = _safe_int(usage.get("used", 0))
        maxhosts = _safe_int(usage.get("maxhosts", 0))
        pct = (used / maxhosts * 100) if maxhosts > 0 else 0.0
        util_color = _utilization_color(pct)

        is_folder = str(subnet.get("isFolder", "0")) == "1"
        ping_scan = str(subnet.get("pingSubnet", "0")) == "1"
        last_scan = subnet.get("lastScan") or "-"

        lines = [
            f"[bold]Subnet:[/bold]      {cidr}",
            f"[bold]Description:[/bold] {desc}",
            f"[bold]VLAN:[/bold]        {vlan_display}",
            f"[bold]Section ID:[/bold]  {section_id}",
            f"[bold]Parent:[/bold]      {master_display}",
            f"[bold]Is Folder:[/bold]   {'Yes' if is_folder else 'No'}",
            "",
            f"[bold]Usage:[/bold]       [{util_color}]{used} / {maxhosts} ({pct:.1f}%)[/{util_color}]",
            f"[bold]Ping Scan:[/bold]   {'Enabled' if ping_scan else 'Disabled'}",
            f"[bold]Last Scan:[/bold]   {last_scan}",
        ]
        detail.update("\n".join(lines))

    def _show_address_detail(self, addr: dict, subnet_id: str) -> None:
        detail = self.query_one("#ipam-detail-content", Static)
        title = self.query_one("#ipam-detail-title", Static)
        title.update("[bold]Address Details[/bold]")

        ip = addr.get("ip", "?")
        hostname = addr.get("hostname") or "-"
        status = _addr_status_label(addr)
        color = STATUS_COLORS.get(status, "bright_black")
        desc = addr.get("description") or "-"
        mac = addr.get("mac") or "-"
        last_seen = addr.get("lastSeen") or "-"
        owner = addr.get("owner") or "-"
        note = addr.get("note") or "-"

        # Find parent subnet CIDR
        subnet_cidr = f"Subnet #{subnet_id}"
        for s in self._subnets:
            if str(s.get("id", "")) == subnet_id:
                subnet_cidr = f"{s.get('subnet', '?')}/{s.get('mask', '?')}"
                break

        lines = [
            f"[bold]IP Address:[/bold]  {ip}",
            f"[bold]Hostname:[/bold]    {hostname}",
            f"[bold]Status:[/bold]      [{color}]{status}[/{color}]",
            f"[bold]Description:[/bold] {desc}",
            f"[bold]MAC:[/bold]         {mac}",
            f"[bold]Last Seen:[/bold]   {last_seen}",
            f"[bold]Owner:[/bold]       {owner}",
            f"[bold]Note:[/bold]        {note}",
            "",
            f"[bold]Subnet:[/bold]      {subnet_cidr}",
        ]
        detail.update("\n".join(lines))

    def _clear_detail_panel(self) -> None:
        self.query_one("#ipam-detail-title", Static).update("[bold]Details[/bold]")
        self.query_one("#ipam-detail-content", Static).update(
            "[dim]Select an item to view details.[/dim]"
        )

    def _vlan_display(self, vlan_id: str) -> str:
        if not vlan_id or vlan_id == "0":
            return "-"
        for v in self._vlans:
            if str(v.get("id", "")) == vlan_id:
                vnum = str(v.get("number", ""))
                vname = v.get("name", "")
                return f"{vnum}" + (f" ({vname})" if vname else "")
        return vlan_id

    # ------------------------------------------------------------------
    # Tree: context helpers
    # ------------------------------------------------------------------

    def _get_highlighted_node(self) -> TreeNode | None:
        tree = self.query_one("#ipam-tree", Tree)
        cursor = tree.cursor_line
        if cursor < 0:
            return None
        try:
            return tree.get_node_at_line(cursor)
        except Exception:
            return None

    def _get_context_subnet(self) -> dict | None:
        """Return the subnet dict for the highlighted node (or its parent)."""
        node = self._get_highlighted_node()
        if node is None or node.data is None or not hasattr(node.data, 'kind'):
            return None
        if node.data.kind == "subnet":
            return node.data.record
        elif node.data.kind == "address":
            parent = node.parent
            while parent:
                if parent.data and hasattr(parent.data, 'kind') and parent.data.kind == "subnet":
                    return parent.data.record
                parent = parent.parent
        return None

    def _get_context_address(self) -> dict | None:
        """Return the address dict if an address node is highlighted."""
        node = self._get_highlighted_node()
        if node is None or node.data is None or not hasattr(node.data, 'kind'):
            return None
        if node.data.kind == "address":
            return node.data.record
        return None

    # ------------------------------------------------------------------
    # Display update helpers
    # ------------------------------------------------------------------

    def _show_not_configured(self) -> None:
        overview = self.query_one("#ipam-overview", Static)
        overview.update(
            "[yellow]phpIPAM is not configured.[/yellow]\n\n"
            "[dim]To enable IPAM management, add the following to your config:[/dim]\n\n"
            "[dim]ipam:[/dim]\n"
            "[dim]  provider: phpipam[/dim]\n"
            "[dim]  url: https://ipam.example.com[/dim]\n"
            "[dim]  app_id: infraforge[/dim]\n"
            "[dim]  token: your-api-token[/dim]\n"
            "[dim]  # Or use username/password:[/dim]\n"
            "[dim]  # username: admin[/dim]\n"
            "[dim]  # password: secret[/dim]\n"
            "[dim]  verify_ssl: false[/dim]\n\n"
            "[dim]Run 'infraforge setup' to configure interactively.[/dim]"
        )

    def _show_error(self, error: str) -> None:
        overview = self.query_one("#ipam-overview", Static)
        from rich.markup import escape
        safe = escape(str(error))
        overview.update(f"[red]Error: {safe}[/red]")
        self._set_status(f"[red]Error: {safe}[/red]")

    def _set_status(self, text: str) -> None:
        self.query_one("#ipam-status-bar", Static).update(text)

    def _update_overview(self) -> None:
        """Update the overview panel with summary statistics."""
        overview = self.query_one("#ipam-overview", Static)
        ipam_cfg = self.app.config.ipam

        total_subnets = len(self._subnets)
        total_vlans = len(self._vlans)

        # Detect empty state
        if total_subnets == 0 and total_vlans == 0 and self._ipam_healthy:
            self._show_empty_state()
            return

        # Calculate total addresses and utilization
        total_used = 0
        total_max = 0
        for subnet in self._subnets:
            usage = subnet.get("usage", {})
            used = _safe_int(usage.get("used", 0))
            maxhosts = _safe_int(usage.get("maxhosts", 0))
            total_used += used
            total_max += maxhosts

        if total_max > 0:
            overall_pct = (total_used / total_max) * 100
        else:
            overall_pct = 0.0

        util_color = _utilization_color(overall_pct)

        status_text = "[green]Connected[/green]" if self._ipam_healthy else "[red]Disconnected[/red]"

        lines = [
            f"[bold]phpIPAM:[/bold]  [green]{ipam_cfg.url}[/green]"
            f"    [bold]Status:[/bold]  {status_text}"
            f"    [bold]App ID:[/bold]  [cyan]{ipam_cfg.app_id or 'infraforge'}[/cyan]",
            f"[bold]Subnets:[/bold]  [cyan]{total_subnets}[/cyan]"
            f"    [bold]VLANs:[/bold]  [cyan]{total_vlans}[/cyan]"
            f"    [bold]Addresses:[/bold]  [cyan]{total_used}[/cyan] / [cyan]{total_max}[/cyan]"
            f"    [bold]Utilization:[/bold]  [{util_color}]{overall_pct:.1f}%[/{util_color}]",
        ]

        overview.update("\n".join(lines))

    def _show_empty_state(self) -> None:
        """Show getting-started guidance when phpIPAM is connected but empty."""
        overview = self.query_one("#ipam-overview", Static)
        ipam_cfg = self.app.config.ipam

        overview.update(
            f"[bold]phpIPAM:[/bold]  [green]{ipam_cfg.url}[/green]"
            f"    [bold]Status:[/bold]  [green]Connected[/green]"
            f"    [bold]App ID:[/bold]  [cyan]{ipam_cfg.app_id or 'infraforge'}[/cyan]\n\n"
            "[yellow bold]Getting Started[/yellow bold]\n\n"
            "Your IPAM is connected but has no subnets or VLANs yet.\n"
            "To begin managing IP addresses, set up your network in the phpIPAM web UI:\n\n"
            f"  1. Open [bold cyan]{ipam_cfg.url}[/bold cyan]\n"
            "  2. [bold]Create a section[/bold] (Administration > Sections) to group your subnets\n"
            "  3. [bold]Add subnets[/bold] (Subnets > + Add subnet) for your network ranges\n"
            "  4. [bold]Add VLANs[/bold] (optional: Subnets > VLANs > + Add VLAN)\n"
            "  5. Come back here and press [bold]r[/bold] to refresh\n\n"
            "[dim]Subnets, addresses, and VLANs will appear here once configured.[/dim]"
        )
        self._set_status(
            "[green]Connected[/green] | No subnets configured yet - see instructions above"
        )

    def _update_controls(self) -> None:
        """Update the sort/filter/count labels for the active view."""
        filter_label = self.query_one("#ipam-filter-label", Static)
        sort_label = self.query_one("#ipam-sort-label", Static)
        count_label = self.query_one("#ipam-count-label", Static)

        if self._active_view == VIEW_TREE:
            # Determine context from highlighted node
            node = self._get_highlighted_node()
            is_address = (
                node is not None
                and node.data is not None
                and hasattr(node.data, 'kind')
                and node.data.kind == "address"
            )

            if is_address:
                current_filter = ADDRESS_FILTER_LABELS[self._address_filter_index]
                current_sort = ADDRESS_SORT_LABELS[self._address_sort_index]
                direction = " \u25bc" if self._address_sort_reverse else " \u25b2"
                total = sum(len(v) for v in self._address_cache.values())
                shown = total  # approximate
            else:
                current_filter = SUBNET_FILTER_LABELS[self._subnet_filter_index]
                current_sort = SUBNET_SORT_LABELS[self._subnet_sort_index]
                direction = " \u25bc" if self._subnet_sort_reverse else " \u25b2"
                total = len(self._subnets)
                filtered = self._get_filtered_subnets()
                shown = len(filtered)

        else:  # VLANs
            current_filter = VLAN_FILTER_LABELS[self._vlan_filter_index]
            current_sort = VLAN_SORT_LABELS[self._vlan_sort_index]
            direction = " \u25bc" if self._vlan_sort_reverse else " \u25b2"
            filtered = self._get_filtered_vlans()
            total = len(self._vlans)
            shown = len(filtered)

        filter_label.update(f"Filter: [bold]{current_filter}[/bold]")
        sort_label.update(f"Sort: [bold]{current_sort}{direction}[/bold]")

        if shown == total:
            count_label.update(f"[dim]{total} items[/dim]")
        else:
            count_label.update(f"[dim]{shown} / {total} items[/dim]")

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------

    def _get_filtered_subnets(self) -> list[dict]:
        subnets = list(self._subnets)

        mode = SUBNET_FILTER_MODES[self._subnet_filter_index]
        if mode == "low":
            subnets = [s for s in subnets if _subnet_usage_pct(s) < 60]
        elif mode == "medium":
            subnets = [
                s for s in subnets
                if 60 <= _subnet_usage_pct(s) <= 80
            ]
        elif mode == "high":
            subnets = [s for s in subnets if _subnet_usage_pct(s) > 80]

        field = SUBNET_SORT_FIELDS[self._subnet_sort_index]
        if field == "subnet":
            subnets.sort(
                key=lambda s: _ip_sort_key(s.get("subnet", "0.0.0.0")),
                reverse=self._subnet_sort_reverse,
            )
        elif field == "description":
            subnets.sort(
                key=lambda s: (s.get("description") or "").lower(),
                reverse=self._subnet_sort_reverse,
            )
        elif field == "vlan":
            subnets.sort(
                key=lambda s: _safe_int(s.get("vlanId", 0)),
                reverse=self._subnet_sort_reverse,
            )
        elif field == "usage":
            subnets.sort(
                key=_subnet_usage_pct,
                reverse=self._subnet_sort_reverse,
            )

        return subnets

    def _sort_addresses(self, addresses: list[dict]) -> list[dict]:
        """Sort addresses based on current address sort state."""
        result = list(addresses)
        field = ADDRESS_SORT_FIELDS[self._address_sort_index]
        if field == "ip":
            result.sort(
                key=lambda a: _ip_sort_key(a.get("ip", "0.0.0.0")),
                reverse=self._address_sort_reverse,
            )
        elif field == "hostname":
            result.sort(
                key=lambda a: (a.get("hostname") or "").lower(),
                reverse=self._address_sort_reverse,
            )
        elif field == "status":
            result.sort(
                key=lambda a: _addr_status_label(a),
                reverse=self._address_sort_reverse,
            )
        elif field == "description":
            result.sort(
                key=lambda a: (a.get("description") or "").lower(),
                reverse=self._address_sort_reverse,
            )
        elif field == "last_seen":
            result.sort(
                key=lambda a: a.get("lastSeen") or "",
                reverse=self._address_sort_reverse,
            )
        return result

    def _filter_addresses(self, addresses: list[dict]) -> list[dict]:
        """Filter addresses based on current address filter state."""
        mode = ADDRESS_FILTER_MODES[self._address_filter_index]
        if mode == "all":
            return addresses
        target = mode.capitalize()
        return [a for a in addresses if _addr_status_label(a) == target]

    def _get_filtered_vlans(self) -> list[dict]:
        vlans = list(self._vlans)

        field = VLAN_SORT_FIELDS[self._vlan_sort_index]
        if field == "number":
            vlans.sort(
                key=lambda v: _safe_int(v.get("number", 0)),
                reverse=self._vlan_sort_reverse,
            )
        elif field == "name":
            vlans.sort(
                key=lambda v: (v.get("name") or "").lower(),
                reverse=self._vlan_sort_reverse,
            )
        elif field == "description":
            vlans.sort(
                key=lambda v: (v.get("description") or "").lower(),
                reverse=self._vlan_sort_reverse,
            )

        return vlans

    # ------------------------------------------------------------------
    # VLAN table population (kept from original)
    # ------------------------------------------------------------------

    def _setup_vlan_columns(self) -> None:
        table = self.query_one("#ipam-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Number", "Name", "Description", "ID")

    def _populate_vlan_table(self) -> None:
        table = self.query_one("#ipam-table", DataTable)
        table.clear()
        self._update_controls()

        filtered = self._get_filtered_vlans()
        for vlan in filtered:
            number = str(vlan.get("number", "?"))
            name = vlan.get("name") or ""
            description = vlan.get("description") or ""
            vlan_id = str(vlan.get("id", ""))

            number_text = Text(number, style="bold cyan")
            name_text = Text(name)
            desc_text = Text(description)
            id_text = Text(vlan_id, style="dim")

            table.add_row(
                number_text, name_text, desc_text, id_text,
                key=f"vlan_{vlan_id}",
            )

    def _get_selected_vlan(self) -> dict | None:
        table = self.query_one("#ipam-table", DataTable)
        try:
            cursor_key = str(table.coordinate_to_cell_key(
                table.cursor_coordinate,
            ).row_key)
        except Exception:
            return None

        if not cursor_key.startswith("vlan_"):
            return None

        vlan_id = cursor_key[len("vlan_"):]
        for v in self._vlans:
            if str(v.get("id", "")) == vlan_id:
                return v
        return None

    # ------------------------------------------------------------------
    # Actions: Navigation
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # Actions: Tab switching
    # ------------------------------------------------------------------

    def _switch_to_view(self, view: str) -> None:
        if view == self._active_view or view not in VIEWS:
            return

        self._active_view = view
        self._render_tab_bar()

        main_content = self.query_one("#ipam-main-content", Horizontal)
        table = self.query_one("#ipam-table", DataTable)

        if view == VIEW_TREE:
            main_content.display = True
            table.display = False
            self._build_subnet_tree()
        elif view == VIEW_VLANS:
            main_content.display = False
            table.display = True
            self._setup_vlan_columns()
            if not self._vlans:
                self._load_vlans()
            else:
                self._populate_vlan_table()

        self._update_controls()

    def action_next_tab(self) -> None:
        current_idx = VIEWS.index(self._active_view)
        new_idx = (current_idx + 1) % len(VIEWS)
        self._switch_to_view(VIEWS[new_idx])

    def action_prev_tab(self) -> None:
        current_idx = VIEWS.index(self._active_view)
        new_idx = (current_idx - 1) % len(VIEWS)
        self._switch_to_view(VIEWS[new_idx])

    # ------------------------------------------------------------------
    # Actions: Sort / Filter / Refresh
    # ------------------------------------------------------------------

    def action_cycle_sort(self) -> None:
        if self._active_view == VIEW_TREE:
            node = self._get_highlighted_node()
            is_address = (
                node is not None
                and node.data is not None
                and hasattr(node.data, 'kind')
                and node.data.kind == "address"
            )

            if is_address:
                # Cycle address sort
                fields = ADDRESS_SORT_FIELDS
                if self._address_sort_index == len(fields) - 1 and not self._address_sort_reverse:
                    self._address_sort_reverse = True
                elif self._address_sort_reverse:
                    self._address_sort_reverse = False
                    self._address_sort_index = (self._address_sort_index + 1) % len(fields)
                else:
                    self._address_sort_index = (self._address_sort_index + 1) % len(fields)
                self._re_sort_all_expanded_addresses()
            else:
                # Cycle subnet sort
                fields = SUBNET_SORT_FIELDS
                if self._subnet_sort_index == len(fields) - 1 and not self._subnet_sort_reverse:
                    self._subnet_sort_reverse = True
                elif self._subnet_sort_reverse:
                    self._subnet_sort_reverse = False
                    self._subnet_sort_index = (self._subnet_sort_index + 1) % len(fields)
                else:
                    self._subnet_sort_index = (self._subnet_sort_index + 1) % len(fields)
                self._build_subnet_tree()

        elif self._active_view == VIEW_VLANS:
            fields = VLAN_SORT_FIELDS
            if self._vlan_sort_index == len(fields) - 1 and not self._vlan_sort_reverse:
                self._vlan_sort_reverse = True
            elif self._vlan_sort_reverse:
                self._vlan_sort_reverse = False
                self._vlan_sort_index = (self._vlan_sort_index + 1) % len(fields)
            else:
                self._vlan_sort_index = (self._vlan_sort_index + 1) % len(fields)
            self._populate_vlan_table()

        self._update_controls()

    def _re_sort_all_expanded_addresses(self) -> None:
        """Re-sort address children of all expanded subnet nodes."""
        tree = self.query_one("#ipam-tree", Tree)
        for node in self._iter_subnet_nodes(tree.root):
            if node.data and node.data.addresses_loaded and node.is_expanded:
                addresses = self._address_cache.get(node.data.subnet_id, [])
                self._populate_address_nodes(node, addresses)

    def action_cycle_filter(self) -> None:
        if self._active_view == VIEW_TREE:
            node = self._get_highlighted_node()
            is_address = (
                node is not None
                and node.data is not None
                and hasattr(node.data, 'kind')
                and node.data.kind == "address"
            )

            if is_address:
                self._address_filter_index = (
                    (self._address_filter_index + 1) % len(ADDRESS_FILTER_MODES)
                )
                self._re_sort_all_expanded_addresses()
            else:
                self._subnet_filter_index = (
                    (self._subnet_filter_index + 1) % len(SUBNET_FILTER_MODES)
                )
                self._build_subnet_tree()

        elif self._active_view == VIEW_VLANS:
            self._vlan_filter_index = (
                (self._vlan_filter_index + 1) % len(VLAN_FILTER_MODES)
            )
            self._populate_vlan_table()

        self._update_controls()

    def action_refresh(self) -> None:
        if self._loading:
            return
        self._set_status("[dim]Refreshing...[/dim]")
        self._address_cache.clear()

        if self._active_view == VIEW_TREE:
            self._load_initial_data()
        elif self._active_view == VIEW_VLANS:
            self._load_vlans()

    # ------------------------------------------------------------------
    # Actions: CRUD -- Add
    # ------------------------------------------------------------------

    def action_add_item(self) -> None:
        if self._active_view == VIEW_TREE:
            subnet = self._get_context_subnet()
            if subnet is None:
                self._set_status(
                    "[yellow]Highlight a subnet to add an address.[/yellow]"
                )
                return
            self._add_address(subnet)
        elif self._active_view == VIEW_VLANS:
            self._set_status(
                "[yellow]VLAN creation is not yet available from this screen. "
                "Use the phpIPAM web UI.[/yellow]"
            )

    def _add_address(self, subnet: dict) -> None:
        subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"

        def _on_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_reserve_address(
                result["ip"],
                subnet,
                result["hostname"],
                result["description"],
                result["tag"],
            )

        self._suggest_and_open_address_modal(subnet, subnet_cidr, _on_result)

    @work(thread=True)
    def _suggest_and_open_address_modal(
        self, subnet: dict, subnet_cidr: str, callback,
    ) -> None:
        suggested_ip = ""
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            suggested_ip = client.get_first_free_ip(subnet.get("id", ""))
        except Exception:
            pass

        self.app.call_from_thread(
            self.app.push_screen,
            AddressInputScreen(
                subnet_cidr=subnet_cidr,
                ip=suggested_ip,
                title="Reserve IP Address",
            ),
            callback,
        )

    @work(thread=True)
    def _do_reserve_address(
        self,
        ip: str,
        subnet: dict,
        hostname: str,
        description: str,
        tag_label: str,
    ) -> None:
        subnet_id = str(subnet.get("id", ""))
        self.app.call_from_thread(
            self._set_status,
            f"Reserving {ip} in subnet...",
        )
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            tag_value = TAG_VALUES.get(tag_label, 2)
            client.create_address(
                ip=ip,
                subnet_id=subnet_id,
                hostname=hostname,
                description=description,
                tag=tag_value,
            )

            self.app.call_from_thread(
                self._set_status,
                f"[green]Reserved {ip} ({tag_label})[/green]",
            )
            self.app.call_from_thread(self._refresh_subnet_addresses, subnet_id)
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to reserve {ip}: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: CRUD -- Edit
    # ------------------------------------------------------------------

    def action_edit_item(self) -> None:
        if self._active_view == VIEW_TREE:
            self._edit_address()
        else:
            self._set_status("[yellow]Edit is only available for addresses in the tree view.[/yellow]")

    def _edit_address(self) -> None:
        addr = self._get_context_address()
        if addr is None:
            self._set_status("[yellow]Highlight an address to edit.[/yellow]")
            return

        current_status = _addr_status_label(addr)
        subnet = self._get_context_subnet()
        subnet_cidr = ""
        if subnet:
            subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"

        def _on_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_edit_address(addr, result)

        self.app.push_screen(
            AddressInputScreen(
                subnet_cidr=subnet_cidr,
                ip=addr.get("ip", ""),
                hostname=addr.get("hostname") or "",
                description=addr.get("description") or "",
                tag=current_status,
                title="Edit IP Address",
                ip_editable=False,
            ),
            callback=_on_result,
        )

    @work(thread=True)
    def _do_edit_address(self, addr: dict, result: dict) -> None:
        addr_id = addr.get("id", "")
        subnet_id = str(addr.get("subnetId", ""))
        self.app.call_from_thread(
            self._set_status,
            f"Updating {addr.get('ip', '?')}...",
        )
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            tag_value = TAG_VALUES.get(result["tag"], 2)
            payload = {
                "hostname": result["hostname"],
                "description": result["description"],
                "tag": str(tag_value),
            }
            client._patch(f"/addresses/{addr_id}/", payload)

            self.app.call_from_thread(
                self._set_status,
                f"[green]Updated {addr.get('ip', '?')}[/green]",
            )
            self.app.call_from_thread(self._refresh_subnet_addresses, subnet_id)
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to update address: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: CRUD -- Delete (release)
    # ------------------------------------------------------------------

    def action_delete_item(self) -> None:
        if self._active_view == VIEW_TREE:
            self._release_address()
        else:
            self._set_status(
                "[yellow]Delete is only available for addresses in the tree view.[/yellow]"
            )

    def _release_address(self) -> None:
        addr = self._get_context_address()
        if addr is None:
            self._set_status("[yellow]Highlight an address to release.[/yellow]")
            return

        ip = addr.get("ip", "?")
        hostname = addr.get("hostname") or "-"
        status = _addr_status_label(addr)

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._do_release_address(addr)

        self.app.push_screen(
            IPAMConfirmScreen(
                f"Release this IP address?\n\n"
                f"  [bold]{ip}[/bold]  {status}  {hostname}\n\n"
                "[dim]This will remove the address from phpIPAM.[/dim]",
                title="Release IP Address",
            ),
            callback=_on_confirm,
        )

    @work(thread=True)
    def _do_release_address(self, addr: dict) -> None:
        addr_id = addr.get("id", "")
        subnet_id = str(addr.get("subnetId", ""))
        ip = addr.get("ip", "?")
        self.app.call_from_thread(
            self._set_status, f"Releasing {ip}...",
        )
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            client._delete(f"/addresses/{addr_id}/")

            self.app.call_from_thread(
                self._set_status,
                f"[green]Released {ip}[/green]",
            )
            self.app.call_from_thread(self._refresh_subnet_addresses, subnet_id)
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to release {ip}: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: Scan subnet
    # ------------------------------------------------------------------

    def action_scan_subnet(self) -> None:
        if self._active_view != VIEW_TREE:
            self._set_status(
                "[yellow]Switch to Subnets & Addresses view to scan.[/yellow]"
            )
            return

        subnet = self._get_context_subnet()
        if subnet is None:
            self._set_status("[yellow]Highlight a subnet to scan.[/yellow]")
            return

        subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._do_enable_scan(subnet)

        self.app.push_screen(
            IPAMConfirmScreen(
                f"Enable scanning on subnet [bold]{subnet_cidr}[/bold]?\n\n"
                "[dim]This will enable ping scanning and host discovery.\n"
                "The phpIPAM scan agent will pick it up on its next run.[/dim]",
                title="Enable Subnet Scan",
            ),
            callback=_on_confirm,
        )

    @work(thread=True)
    def _do_enable_scan(self, subnet: dict) -> None:
        subnet_id = subnet.get("id", "")
        subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
        self.app.call_from_thread(
            self._set_status,
            f"Enabling scan on {subnet_cidr}...",
        )
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            client.enable_subnet_scanning(subnet_id)

            self.app.call_from_thread(
                self._set_status,
                f"[green]Scanning enabled on {subnet_cidr} -- "
                f"agent will run on next cycle.[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to enable scan: {exc}[/red]",
            )


# ---------------------------------------------------------------------------
# Label builders
# ---------------------------------------------------------------------------

def _make_subnet_label(subnet: dict, vlans: list[dict]) -> Text:
    """Build a Rich Text label for a subnet tree node."""
    cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
    desc = subnet.get("description") or ""
    is_folder = str(subnet.get("isFolder", "0")) == "1"

    usage = subnet.get("usage", {})
    used = _safe_int(usage.get("used", 0))
    maxhosts = _safe_int(usage.get("maxhosts", 0))
    pct = (used / maxhosts * 100) if maxhosts > 0 else 0.0
    util_color = _utilization_color(pct)

    label = Text()
    if is_folder:
        label.append("\U0001f4c1 ", style="dim")  # folder icon
        label.append(desc or cidr, style="bold")
    else:
        cidr_col = cidr.ljust(20)
        desc_col = desc.ljust(24) if desc else "".ljust(24)
        usage_col = f"[{used}/{maxhosts}]".ljust(14)
        pct_col = f"{pct:.0f}%"
        label.append(cidr_col, style="bold")
        label.append(desc_col, style="dim")
        label.append("\t", style="default")
        label.append(usage_col, style=util_color)
        label.append("\t", style="default")
        label.append(pct_col, style=util_color)

    return label


def _make_address_label(addr: dict) -> Text:
    """Build a Rich Text label for an address leaf node."""
    ip = addr.get("ip", "?")
    hostname = addr.get("hostname") or ""
    status = _addr_status_label(addr)
    color = STATUS_COLORS.get(status, "bright_black")

    ip_col = ip.ljust(18)
    host_col = hostname.ljust(28)
    status_col = f"[{status}]"

    label = Text()
    label.append(ip_col, style="bold")
    label.append(host_col)
    label.append(status_col, style=color)
    return label


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _subnet_usage_pct(subnet: dict) -> float:
    usage = subnet.get("usage", {})
    used = _safe_int(usage.get("used", 0))
    maxhosts = _safe_int(usage.get("maxhosts", 0))
    if maxhosts <= 0:
        return 0.0
    return (used / maxhosts) * 100


def _utilization_color(pct: float) -> str:
    if pct > 80:
        return "red"
    elif pct > 60:
        return "yellow"
    return "green"


def _addr_status_label(addr: dict) -> str:
    tag = str(addr.get("tag", ""))
    return TAG_STATUS_MAP.get(tag, "Unknown")


def _ip_sort_key(ip_str: str) -> tuple:
    try:
        parts = ip_str.split(".")
        return tuple(int(p) for p in parts)
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)
