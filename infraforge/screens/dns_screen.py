"""Multi-zone DNS management screen for InfraForge.

Provides a hierarchical Tree view of DNS zones and records with a detail
panel.  Zones are expandable nodes that lazy-load their records (via AXFR)
on first expand.  Sorting and filtering apply to records within expanded
zones.  Full CRUD for records (add/edit/delete) and zone management
(add/remove) are available.
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
    Input,
    Button,
    Select,
    Label,
    Tree,
    TabbedContent,
    TabPane,
)
from textual.widgets._tree import TreeNode
from textual.containers import Container, Horizontal, Vertical
from textual import work, on

from rich.text import Text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SORT_FIELDS = ["name", "rtype", "value", "ttl"]
SORT_LABELS = ["Name", "Type", "Value", "TTL"]
FILTER_TYPES = [
    "all", "A", "AAAA", "CNAME", "PTR", "TXT", "MX", "SRV", "NS", "SOA",
]
FILTER_LABELS = ["All"] + FILTER_TYPES[1:]

RTYPE_COLORS = {
    "A": "green",
    "AAAA": "cyan",
    "CNAME": "yellow",
    "PTR": "magenta",
    "TXT": "bright_blue",
    "MX": "bright_magenta",
    "SRV": "bright_cyan",
    "NS": "blue",
    "SOA": "bright_black",
}

RTYPE_HINTS = {
    "A": "Maps a hostname to an IPv4 address.",
    "AAAA": "Maps a hostname to an IPv6 address.",
    "CNAME": "Alias that points one name to another.",
    "PTR": "Reverse lookup — maps an IP back to a hostname.",
    "TXT": "Arbitrary text; used for SPF, DKIM, domain verification.",
    "MX": "Specifies the mail server for a domain.",
    "SRV": "Locates servers for specific services (e.g. SIP, LDAP).",
    "NS": "Delegates a zone to an authoritative name server.",
    "SOA": "Start of Authority — defines zone metadata and serial.",
    "CAA": "Restricts which CAs can issue certificates for the domain.",
    "DNAME": "Alias for an entire subtree of the domain name space.",
    "NAPTR": "Rewrite rules for ENUM/SIP URI lookups.",
    "LOC": "Geographic location of a host (latitude/longitude).",
    "SSHFP": "SSH host key fingerprint for DANE verification.",
    "TLSA": "TLS certificate association for DANE.",
    "HINFO": "Host hardware and OS info (rarely used).",
    "RP": "Responsible person for a domain.",
    "AFSDB": "AFS database server location.",
    "DS": "DNSSEC delegation signer — links parent to child zone keys.",
    "DNSKEY": "DNSSEC public key for zone signing.",
    "RRSIG": "DNSSEC signature over a record set.",
    "NSEC": "DNSSEC authenticated denial of existence.",
    "NSEC3": "DNSSEC hashed denial of existence.",
}

RECORD_TYPES_FOR_INPUT = [
    ("A", "A"),
    ("AAAA", "AAAA"),
    ("CNAME", "CNAME"),
    ("PTR", "PTR"),
    ("TXT", "TXT"),
    ("MX", "MX"),
    ("SRV", "SRV"),
    ("NS", "NS"),
]


# ---------------------------------------------------------------------------
# Tree data model
# ---------------------------------------------------------------------------

@dataclass
class DNSNodeData:
    """Data attached to each node in the DNS tree."""
    kind: Literal["zone", "record", "placeholder"]
    record: object = None       # DNSRecord for record nodes, None for zones
    zone_name: str = ""
    soa: dict = field(default_factory=dict)
    records_loaded: bool = False


# ---------------------------------------------------------------------------
# Modal: Zone input screen
# ---------------------------------------------------------------------------

class ZoneInputScreen(ModalScreen[Optional[str]]):
    """Modal screen for adding a new DNS zone."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container"):
            with Vertical(classes="modal-box"):
                yield Static("Add DNS Zone", classes="modal-title")
                yield Label("Zone name (e.g. lab.local):")
                yield Input(
                    placeholder="example.com",
                    id="zone-name-input",
                )
                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", variant="primary", id="zone-save-btn")
                    yield Button("Cancel", variant="default", id="zone-cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#zone-name-input", Input).focus()

    @on(Button.Pressed, "#zone-save-btn")
    def _on_save(self, event: Button.Pressed) -> None:
        zone_name = self.query_one("#zone-name-input", Input).value.strip()
        if zone_name:
            self.dismiss(zone_name)
        else:
            self.query_one("#zone-name-input", Input).focus()

    @on(Button.Pressed, "#zone-cancel-btn")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#zone-name-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        zone_name = event.value.strip()
        if zone_name:
            self.dismiss(zone_name)


# ---------------------------------------------------------------------------
# Modal: Record input screen (add / edit)
# ---------------------------------------------------------------------------

class RecordInputScreen(ModalScreen[Optional[dict]]):
    """Modal screen for adding or editing a DNS record."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        zone: str,
        name: str = "",
        rtype: str = "A",
        value: str = "",
        ttl: str = "3600",
        title: str = "Add DNS Record",
    ) -> None:
        super().__init__()
        self._zone = zone
        self._initial_name = name
        self._initial_rtype = rtype
        self._initial_value = value
        self._initial_ttl = ttl
        self._title = title

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container"):
            with Vertical(classes="modal-box"):
                yield Static(self._title, classes="modal-title")
                yield Static(f"[dim]Zone: {self._zone}[/dim]", markup=True)

                yield Label("Name (hostname):")
                yield Input(
                    value=self._initial_name,
                    placeholder="webserver",
                    id="rec-name-input",
                )

                yield Label("Type:")
                yield Select(
                    RECORD_TYPES_FOR_INPUT,
                    value=self._initial_rtype,
                    id="rec-type-select",
                )

                yield Label("Value:")
                yield Input(
                    value=self._initial_value,
                    placeholder="10.0.0.50",
                    id="rec-value-input",
                )

                yield Label("TTL (seconds):")
                yield Input(
                    value=self._initial_ttl,
                    placeholder="3600",
                    id="rec-ttl-input",
                )

                with Horizontal(classes="modal-buttons"):
                    yield Button("Save", variant="primary", id="rec-save-btn")
                    yield Button(
                        "Cancel", variant="default", id="rec-cancel-btn",
                    )

    def on_mount(self) -> None:
        self.query_one("#rec-name-input", Input).focus()

    @on(Button.Pressed, "#rec-save-btn")
    def _on_save(self, event: Button.Pressed) -> None:
        self._try_submit()

    @on(Button.Pressed, "#rec-cancel-btn")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _try_submit(self) -> None:
        name = self.query_one("#rec-name-input", Input).value.strip()
        rtype_select = self.query_one("#rec-type-select", Select)
        rtype = str(rtype_select.value) if rtype_select.value is not Select.BLANK else "A"
        value = self.query_one("#rec-value-input", Input).value.strip()
        ttl_str = self.query_one("#rec-ttl-input", Input).value.strip()

        if not name or not value:
            return

        try:
            ttl = int(ttl_str) if ttl_str else 3600
        except ValueError:
            ttl = 3600

        self.dismiss({
            "name": name,
            "rtype": rtype,
            "value": value,
            "ttl": ttl,
        })


# ---------------------------------------------------------------------------
# Modal: Confirmation dialog
# ---------------------------------------------------------------------------

class ConfirmScreen(ModalScreen[bool]):
    """Simple yes/no confirmation modal."""

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
# Main DNS Screen
# ---------------------------------------------------------------------------

class DNSScreen(Screen):
    """Screen for viewing and managing DNS records across multiple zones."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "add_record", "Add", show=True),
        Binding("e", "edit_record", "Edit", show=True),
        Binding("d", "delete_record", "Delete", show=True),
        Binding("z", "add_zone", "+Zone", show=True),
        Binding("Z", "remove_zone", "-Zone", show=True),
        Binding("tab", "next_zone", "Next Zone", show=False),
        Binding("shift+tab", "prev_zone", "Prev Zone", show=False),
        Binding("1", "select_zone_1", show=False),
        Binding("2", "select_zone_2", show=False),
        Binding("3", "select_zone_3", show=False),
        Binding("4", "select_zone_4", show=False),
        Binding("5", "select_zone_5", show=False),
        Binding("6", "select_zone_6", show=False),
        Binding("7", "select_zone_7", show=False),
        Binding("8", "select_zone_8", show=False),
        Binding("9", "select_zone_9", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._zones: list[str] = []
        self._active_zone_index: int = 0
        self._dns_healthy: bool = False
        self._loading: bool = False

        # Per-zone caches
        self._records_cache: dict[str, list] = {}
        self._soa_cache: dict[str, dict] = {}

        # Sort / filter state for records
        self._record_sort_index: int = 0
        self._record_sort_reverse: bool = False
        self._record_filter_index: int = 0

        self._cf_tab: object = None  # CloudflareTab instance if available

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="dns-container"):
            yield Static("DNS Management", classes="section-title")

            with TabbedContent(id="dns-tabs"):
                with TabPane("Local DNS", id="tab-bind9"):
                    # Zone selector bar
                    yield Horizontal(id="dns-zone-bar")

                    # Zone info banner
                    yield Static(
                        "Loading DNS zone info...", id="dns-zone-info", markup=True,
                    )

                    # Controls bar
                    with Horizontal(id="dns-controls"):
                        yield Static("Filter: All", id="dns-filter-label")
                        yield Static("Sort: Name", id="dns-sort-label")
                        yield Static("", id="dns-count-label")

                    # Tree + detail panel
                    with Horizontal(id="dns-main-content"):
                        yield Tree("DNS", id="dns-tree")
                        with Container(id="dns-detail-panel"):
                            yield Static(
                                "[bold]Details[/bold]",
                                id="dns-detail-title", markup=True,
                            )
                            yield Static(
                                "[dim]Select an item to view details.[/dim]",
                                id="dns-detail-content", markup=True,
                            )

                    # Status bar
                    yield Static("", id="dns-status-bar", markup=True)

                with TabPane("Cloudflare", id="tab-cloudflare"):
                    pass  # Will be populated in on_mount
        yield Footer()

    def on_mount(self) -> None:
        # Determine which tabs are available
        dns_cfg = self.app.config.dns
        has_bind9 = bool(dns_cfg.provider == "bind9" and dns_cfg.server)
        has_cf = bool(getattr(self.app.config, 'cloudflare', None) and self.app.config.cloudflare.api_token)

        tabs = self.query_one("#dns-tabs", TabbedContent)

        # Hide tabs that aren't configured
        if not has_bind9:
            tabs.hide_tab("tab-bind9")
        if not has_cf:
            tabs.hide_tab("tab-cloudflare")

        # If neither is configured, show the not-configured message in BIND9 tab
        if not has_bind9 and not has_cf:
            tabs.show_tab("tab-bind9")
            self._show_not_configured()
            return

        # Set up BIND9 tab
        if has_bind9:
            tree = self.query_one("#dns-tree", Tree)
            tree.show_root = False
            tree.guide_depth = 3

            self._init_zones()
            self._render_zone_bar()

            if self._zones:
                self._build_zone_tree()
                self._expand_active_zone()
            else:
                self._auto_discover_zones()

        # Set up Cloudflare tab
        if has_cf:
            from infraforge.screens.dns_cloudflare_tab import CloudflareTab
            cf_pane = self.query_one("#tab-cloudflare", TabPane)
            self._cf_tab = CloudflareTab()
            cf_pane.mount(self._cf_tab)

        # If only CF is configured, switch to that tab
        if has_cf and not has_bind9:
            tabs.active = "tab-cloudflare"

    # ------------------------------------------------------------------
    # Zone management helpers
    # ------------------------------------------------------------------

    def _init_zones(self) -> None:
        dns_cfg = self.app.config.dns
        zones: list[str] = getattr(dns_cfg, "zones", None) or []
        self._zones = list(zones)
        if self._zones:
            self._active_zone_index = 0

    def _persist_zones(self) -> None:
        dns_cfg = self.app.config.dns
        dns_cfg.zones = list(self._zones)

    @property
    def _active_zone(self) -> str:
        if not self._zones:
            return ""
        return self._zones[self._active_zone_index]

    def _render_zone_bar(self) -> None:
        bar = self.query_one("#dns-zone-bar", Horizontal)
        bar.remove_children()

        if not self._zones:
            bar.mount(
                Static(
                    "[dim]No zones configured.  Press [bold]z[/bold] to add one.[/dim]",
                    markup=True,
                )
            )
            return

        for idx, zone in enumerate(self._zones):
            number = idx + 1
            if idx == self._active_zone_index:
                label_text = f"[bold][{number}] {zone}[/bold]"
                classes = "dns-zone-btn -active"
            else:
                label_text = f"[dim][{number}] {zone}[/dim]"
                classes = "dns-zone-btn"
            btn = Static(label_text, markup=True, classes=classes)
            bar.mount(btn)

    # ------------------------------------------------------------------
    # Tree: build from zone list
    # ------------------------------------------------------------------

    def _build_zone_tree(self) -> None:
        """Build the zone tree. Each zone is an expandable node."""
        tree = self.query_one("#dns-tree", Tree)

        # Preserve expansion state
        expanded_zones: set[str] = set()
        for node in self._iter_zone_nodes(tree.root):
            if node.data and node.data.kind == "zone" and node.is_expanded:
                expanded_zones.add(node.data.zone_name)

        tree.clear()

        for zone_name in self._zones:
            soa = self._soa_cache.get(zone_name, {})
            record_count = len(self._records_cache.get(zone_name, []))
            label = _make_zone_label(zone_name, soa, record_count)

            node_data = DNSNodeData(
                kind="zone",
                zone_name=zone_name,
                soa=soa,
                records_loaded=zone_name in self._records_cache,
            )
            zone_node = tree.root.add(label, data=node_data)

            if zone_name in self._records_cache:
                self._populate_record_nodes(zone_node, self._records_cache[zone_name])
            else:
                zone_node.add_leaf(
                    Text("Loading records...", style="dim"),
                    data=DNSNodeData(kind="placeholder", zone_name=zone_name),
                )

            if zone_name in expanded_zones:
                zone_node.expand()

        self._update_controls()

    def _iter_zone_nodes(self, root: TreeNode) -> list[TreeNode]:
        """Collect all zone-type nodes in the tree."""
        result = []
        for child in root.children:
            if child.data and hasattr(child.data, 'kind') and child.data.kind == "zone":
                result.append(child)
        return result

    def _expand_active_zone(self) -> None:
        """Expand the currently active zone in the tree."""
        if not self._zones:
            return
        zone_name = self._active_zone
        tree = self.query_one("#dns-tree", Tree)
        for node in self._iter_zone_nodes(tree.root):
            if node.data and node.data.zone_name == zone_name:
                node.expand()
                tree.select_node(node)
                break

    # ------------------------------------------------------------------
    # Tree: lazy-load records on expand
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        # Only handle events from the BIND9 tree, not the CF tree
        if getattr(event.control, "id", None) != "dns-tree":
            return
        node = event.node
        if node.data is None or not hasattr(node.data, 'kind'):
            return
        if node.data.kind != "zone":
            return
        if node.data.records_loaded:
            return
        self._lazy_load_records(node)

    @work(thread=True)
    def _lazy_load_records(self, node: TreeNode) -> None:
        zone_name = node.data.zone_name
        self.app.call_from_thread(
            self._set_status, f"Loading records for {zone_name}...",
        )

        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)

            # Health check
            healthy = client.check_health(zone_name)
            self._dns_healthy = healthy

            if not healthy:
                self.app.call_from_thread(
                    self._set_status,
                    f"[red]Cannot reach DNS server for {zone_name}[/red]",
                )
                return

            # Fetch SOA
            try:
                soa = client.get_zone_soa(zone_name)
                self._soa_cache[zone_name] = soa
            except DNSError:
                pass

            # Fetch records via AXFR
            records = client.get_zone_records(zone_name)
            self._records_cache[zone_name] = records

            self.app.call_from_thread(self._populate_record_nodes, node, records)
            self.app.call_from_thread(self._update_zone_node_label, node)
            self.app.call_from_thread(self._update_zone_info)
            self.app.call_from_thread(self._update_controls)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | Zone: [bold]{zone_name}[/bold] | "
                f"{len(records)} records loaded",
            )
        except Exception as exc:
            from rich.markup import escape
            safe = escape(str(exc))
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load records for {zone_name}: {safe}[/red]",
            )

    def _populate_record_nodes(
        self, parent_node: TreeNode, records: list,
    ) -> None:
        """Remove placeholder and add sorted/filtered record leaf nodes."""
        parent_node.remove_children()

        sorted_records = self._sort_records(records)
        filtered_records = self._filter_records(sorted_records)

        for rec in filtered_records:
            label = _make_record_label(rec)
            data = DNSNodeData(
                kind="record",
                record=rec,
                zone_name=parent_node.data.zone_name,
            )
            parent_node.add_leaf(label, data=data)

        if not filtered_records:
            parent_node.add_leaf(
                Text("(no records)", style="dim italic"),
                data=DNSNodeData(kind="placeholder", zone_name=parent_node.data.zone_name),
            )

        parent_node.data.records_loaded = True

    def _update_zone_node_label(self, node: TreeNode) -> None:
        """Update a zone node's label to reflect loaded record count."""
        zone_name = node.data.zone_name
        soa = self._soa_cache.get(zone_name, {})
        record_count = len(self._records_cache.get(zone_name, []))
        node.set_label(_make_zone_label(zone_name, soa, record_count))

    def _refresh_zone_records(self, zone_name: str) -> None:
        """Re-fetch and re-render records for a specific zone node."""
        tree = self.query_one("#dns-tree", Tree)
        for node in self._iter_zone_nodes(tree.root):
            if node.data and node.data.zone_name == zone_name:
                node.data.records_loaded = False
                if node.is_expanded:
                    self._lazy_load_records(node)
                break

    # ------------------------------------------------------------------
    # Tree: detail panel on highlight
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Only handle events from the BIND9 tree, not the CF tree
        if getattr(event.control, "id", None) != "dns-tree":
            return
        node = event.node
        if node.data is None or not hasattr(node.data, 'kind'):
            self._clear_detail_panel()
            return

        if node.data.kind == "zone":
            self._show_zone_detail(node.data.zone_name)
        elif node.data.kind == "record":
            self._show_record_detail(node.data.record)
        else:
            self._clear_detail_panel()

        self._update_controls()

    def _show_zone_detail(self, zone_name: str) -> None:
        detail = self.query_one("#dns-detail-content", Static)
        title = self.query_one("#dns-detail-title", Static)
        title.update("[bold]Zone Details[/bold]")

        dns_cfg = self.app.config.dns
        soa = self._soa_cache.get(zone_name, {})
        records = self._records_cache.get(zone_name, [])
        record_count = len(records)

        # Count records by type
        type_counts: dict[str, int] = {}
        for rec in records:
            type_counts[rec.rtype] = type_counts.get(rec.rtype, 0) + 1
        type_summary = "  ".join(
            f"[{RTYPE_COLORS.get(t, 'white')}]{t}: {c}[/{RTYPE_COLORS.get(t, 'white')}]"
            for t, c in sorted(type_counts.items())
        )

        lines = [
            f"[bold]Zone:[/bold]        {zone_name}",
            f"[bold]Server:[/bold]      {dns_cfg.server}:{dns_cfg.port}",
            f"[bold]Records:[/bold]     [cyan]{record_count}[/cyan]",
            "",
        ]

        if soa:
            lines.extend([
                f"[bold]Serial:[/bold]      [cyan]{soa.get('serial', '?')}[/cyan]",
                f"[bold]Primary NS:[/bold]  [cyan]{soa.get('mname', '?')}[/cyan]",
                f"[bold]Admin:[/bold]       [cyan]{soa.get('rname', '?')}[/cyan]",
                f"[bold]Refresh:[/bold]     [cyan]{soa.get('refresh', '?')}s[/cyan]",
                f"[bold]Retry:[/bold]       [cyan]{soa.get('retry', '?')}s[/cyan]",
                f"[bold]Expire:[/bold]      [cyan]{soa.get('expire', '?')}s[/cyan]",
                "",
            ])

        if type_summary:
            lines.append(f"[bold]Types:[/bold]       {type_summary}")

        detail.update("\n".join(lines))

    def _show_record_detail(self, record) -> None:
        detail = self.query_one("#dns-detail-content", Static)
        title = self.query_one("#dns-detail-title", Static)
        title.update("[bold]Record Details[/bold]")

        color = RTYPE_COLORS.get(record.rtype, "white")

        hint = RTYPE_HINTS.get(record.rtype, "")

        lines = [
            f"[bold]Name:[/bold]    {record.name}",
            f"[bold]Type:[/bold]    [{color}]{record.rtype}[/{color}]",
            f"[bold]Value:[/bold]   {record.value}",
            f"[bold]TTL:[/bold]     {record.ttl}s",
            f"[bold]Zone:[/bold]    {record.zone}",
        ]
        if hint:
            lines.append("")
            lines.append(f"[dim italic]{hint}[/dim italic]")
        detail.update("\n".join(lines))

    def _clear_detail_panel(self) -> None:
        self.query_one("#dns-detail-title", Static).update("[bold]Details[/bold]")
        self.query_one("#dns-detail-content", Static).update(
            "[dim]Select an item to view details.[/dim]"
        )

    # ------------------------------------------------------------------
    # Tree: context helpers
    # ------------------------------------------------------------------

    def _get_highlighted_node(self) -> TreeNode | None:
        tree = self.query_one("#dns-tree", Tree)
        cursor = tree.cursor_line
        if cursor < 0:
            return None
        try:
            return tree.get_node_at_line(cursor)
        except Exception:
            return None

    def _get_context_zone(self) -> str | None:
        """Return the zone name for the highlighted node."""
        node = self._get_highlighted_node()
        if node is None or node.data is None or not hasattr(node.data, 'kind'):
            return None
        if node.data.kind in ("zone", "record"):
            return node.data.zone_name
        return None

    def _get_context_record(self):
        """Return the DNSRecord if a record node is highlighted."""
        node = self._get_highlighted_node()
        if node is None or node.data is None or not hasattr(node.data, 'kind'):
            return None
        if node.data.kind == "record":
            return node.data.record
        return None

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _show_not_configured(self) -> None:
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(
            "[yellow]No DNS providers configured.[/yellow]\n\n"
            "[dim]To enable DNS management, configure one or both providers:[/dim]\n\n"
            "[bold]Local DNS (BIND9):[/bold]\n"
            "[dim]  Run 'infraforge setup' → DNS[/dim]\n\n"
            "[bold]Cloudflare:[/bold]\n"
            "[dim]  Run 'infraforge setup' → Cloudflare[/dim]\n"
        )

    def _show_no_zones(self) -> None:
        dns_cfg = self.app.config.dns
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(
            f"[bold]DNS Server:[/bold]  [green]{dns_cfg.server}:{dns_cfg.port}[/green]\n"
            f"[bold]Provider:[/bold]   [green]{dns_cfg.provider}[/green]\n"
            f"[bold]TSIG Key:[/bold]   [green]{dns_cfg.tsig_key_name or '(none)'}[/green]\n\n"
            "[yellow]No zones configured yet.[/yellow]\n\n"
            "Press [bold cyan]z[/bold cyan] to add a zone from your DNS server."
        )
        self._set_status("[dim]Press z to add a zone.[/dim]")

    def _show_error(self, error: str) -> None:
        zone_info = self.query_one("#dns-zone-info", Static)
        from rich.markup import escape
        safe = escape(str(error))
        zone_info.update(f"[red]Error: {safe}[/red]")
        self._set_status(f"[red]Error: {safe}[/red]")

    def _set_status(self, text: str) -> None:
        self.query_one("#dns-status-bar", Static).update(text)

    def _update_zone_info(self) -> None:
        """Update the zone info banner with aggregate or active-zone stats."""
        zone_info = self.query_one("#dns-zone-info", Static)
        dns_cfg = self.app.config.dns

        if not self._zones:
            return

        total_records = sum(len(v) for v in self._records_cache.values())
        loaded_zones = len(self._records_cache)

        status = "[green]Connected[/green]" if self._dns_healthy else "[yellow]Checking...[/yellow]"

        lines = [
            f"[bold]Server:[/bold]  [green]{dns_cfg.server}:{dns_cfg.port}[/green]"
            f"    [bold]Status:[/bold]  {status}"
            f"    [bold]TSIG Key:[/bold]  [green]{dns_cfg.tsig_key_name or '(none)'}[/green]",
            f"[bold]Zones:[/bold]   [cyan]{len(self._zones)}[/cyan] configured, "
            f"[cyan]{loaded_zones}[/cyan] loaded"
            f"    [bold]Total Records:[/bold]  [cyan]{total_records}[/cyan]",
        ]

        # Show active zone SOA if available
        active_soa = self._soa_cache.get(self._active_zone, {})
        if active_soa:
            lines.append(
                f"[bold]Active:[/bold]  [bold]{self._active_zone}[/bold]"
                f"    [bold]Serial:[/bold]  [cyan]{active_soa.get('serial', '?')}[/cyan]"
                f"    [bold]Primary NS:[/bold]  [cyan]{active_soa.get('mname', '?')}[/cyan]"
            )

        zone_info.update("\n".join(lines))

    def _update_controls(self) -> None:
        filter_label = self.query_one("#dns-filter-label", Static)
        sort_label = self.query_one("#dns-sort-label", Static)
        count_label = self.query_one("#dns-count-label", Static)

        current_filter = FILTER_LABELS[self._record_filter_index]
        current_sort = SORT_LABELS[self._record_sort_index]
        direction = " \u25bc" if self._record_sort_reverse else " \u25b2"

        filter_label.update(f"Filter: [bold]{current_filter}[/bold]")
        sort_label.update(f"Sort: [bold]{current_sort}{direction}[/bold]")

        total = sum(len(v) for v in self._records_cache.values())
        count_label.update(
            f"[dim]{total} records across {len(self._zones)} zones[/dim]"
        )

    # ------------------------------------------------------------------
    # Sorting / filtering helpers
    # ------------------------------------------------------------------

    def _sort_records(self, records: list) -> list:
        result = list(records)
        sort_field = SORT_FIELDS[self._record_sort_index]
        if sort_field == "ttl":
            result.sort(key=lambda r: r.ttl, reverse=self._record_sort_reverse)
        else:
            result.sort(
                key=lambda r: getattr(r, sort_field, "").lower(),
                reverse=self._record_sort_reverse,
            )
        return result

    def _filter_records(self, records: list) -> list:
        if self._record_filter_index == 0:
            return records
        filter_type = FILTER_TYPES[self._record_filter_index]
        return [r for r in records if r.rtype == filter_type]

    def _re_sort_all_expanded_records(self) -> None:
        """Re-sort/re-filter record children of all expanded zone nodes."""
        tree = self.query_one("#dns-tree", Tree)
        for node in self._iter_zone_nodes(tree.root):
            if node.data and node.data.records_loaded and node.is_expanded:
                records = self._records_cache.get(node.data.zone_name, [])
                self._populate_record_nodes(node, records)

    # ------------------------------------------------------------------
    # Actions: Navigation
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # Actions: Sort / Filter / Refresh
    # ------------------------------------------------------------------

    def _is_cf_tab_active(self) -> bool:
        """Check if the Cloudflare tab is currently active."""
        try:
            tabs = self.query_one("#dns-tabs", TabbedContent)
            return tabs.active == "tab-cloudflare"
        except Exception:
            return False

    def action_cycle_sort(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.cycle_sort()
            return
        fields = SORT_FIELDS
        if self._record_sort_index == len(fields) - 1 and not self._record_sort_reverse:
            self._record_sort_reverse = True
        elif self._record_sort_reverse:
            self._record_sort_reverse = False
            self._record_sort_index = (self._record_sort_index + 1) % len(fields)
        else:
            self._record_sort_index = (self._record_sort_index + 1) % len(fields)
        self._re_sort_all_expanded_records()
        self._update_controls()

    def action_cycle_filter(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.cycle_filter()
            return
        self._record_filter_index = (self._record_filter_index + 1) % len(FILTER_TYPES)
        self._re_sort_all_expanded_records()
        self._update_controls()

    def action_refresh(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.refresh_data()
            return
        if self._loading:
            return
        self._set_status("[dim]Refreshing...[/dim]")
        self._records_cache.clear()
        self._soa_cache.clear()

        if self._zones:
            self._build_zone_tree()
            self._expand_active_zone()
        else:
            dns_cfg = self.app.config.dns
            if dns_cfg.provider == "bind9" and dns_cfg.server:
                self._auto_discover_zones()

    # ------------------------------------------------------------------
    # Actions: Zone switching
    # ------------------------------------------------------------------

    def _switch_to_zone(self, index: int) -> None:
        """Switch to the zone at the given index — scroll tree and expand."""
        if not self._zones:
            return
        if index < 0 or index >= len(self._zones):
            return

        self._active_zone_index = index
        self._render_zone_bar()

        zone_name = self._zones[index]
        tree = self.query_one("#dns-tree", Tree)
        for node in self._iter_zone_nodes(tree.root):
            if node.data and node.data.zone_name == zone_name:
                tree.select_node(node)
                if not node.is_expanded:
                    node.expand()
                break

    def action_next_zone(self) -> None:
        if self._zones:
            new_index = (self._active_zone_index + 1) % len(self._zones)
            self._switch_to_zone(new_index)

    def action_prev_zone(self) -> None:
        if self._zones:
            new_index = (self._active_zone_index - 1) % len(self._zones)
            self._switch_to_zone(new_index)

    def action_select_zone_1(self) -> None:
        self._switch_to_zone(0)

    def action_select_zone_2(self) -> None:
        self._switch_to_zone(1)

    def action_select_zone_3(self) -> None:
        self._switch_to_zone(2)

    def action_select_zone_4(self) -> None:
        self._switch_to_zone(3)

    def action_select_zone_5(self) -> None:
        self._switch_to_zone(4)

    def action_select_zone_6(self) -> None:
        self._switch_to_zone(5)

    def action_select_zone_7(self) -> None:
        self._switch_to_zone(6)

    def action_select_zone_8(self) -> None:
        self._switch_to_zone(7)

    def action_select_zone_9(self) -> None:
        self._switch_to_zone(8)

    # ------------------------------------------------------------------
    # Actions: Zone CRUD
    # ------------------------------------------------------------------

    def action_add_zone(self) -> None:
        def _on_zone_result(zone_name: Optional[str]) -> None:
            if zone_name is None:
                return
            zone_name = zone_name.strip().rstrip(".")
            if not zone_name:
                return
            if zone_name in self._zones:
                self._set_status(
                    f"[yellow]Zone {zone_name} is already in the list.[/yellow]"
                )
                return
            self._validate_and_add_zone(zone_name)

        self.app.push_screen(ZoneInputScreen(), callback=_on_zone_result)

    @work(thread=True)
    def _validate_and_add_zone(self, zone_name: str) -> None:
        self.app.call_from_thread(
            self._set_status, f"Validating zone {zone_name}...",
        )

        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)
            soa = client.check_zone(zone_name)

            if soa is None:
                self.app.call_from_thread(
                    self._set_status,
                    f"[red]Zone {zone_name} not found on server (no SOA).[/red]",
                )
                return

            self.app.call_from_thread(self._finalize_add_zone, zone_name)

        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to validate zone {zone_name}: {exc}[/red]",
            )

    def _finalize_add_zone(self, zone_name: str) -> None:
        self._zones.append(zone_name)
        new_index = len(self._zones) - 1
        self._active_zone_index = new_index
        self._persist_zones()
        self._render_zone_bar()
        self._build_zone_tree()
        self._expand_active_zone()

    def action_remove_zone(self) -> None:
        if not self._zones:
            self._set_status("[yellow]No zones to remove.[/yellow]")
            return

        zone = self._active_zone

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._zones.remove(zone)
            self._records_cache.pop(zone, None)
            self._soa_cache.pop(zone, None)

            if self._zones:
                self._active_zone_index = min(
                    self._active_zone_index, len(self._zones) - 1,
                )
            else:
                self._active_zone_index = 0
            self._persist_zones()
            self._render_zone_bar()

            if self._zones:
                self._build_zone_tree()
                self._expand_active_zone()
            else:
                tree = self.query_one("#dns-tree", Tree)
                tree.clear()
                zone_info = self.query_one("#dns-zone-info", Static)
                zone_info.update(
                    "[yellow]No zones configured. Press [bold]z[/bold] to add one.[/yellow]"
                )
                self._set_status("[dim]No active zone.[/dim]")
                self._clear_detail_panel()

        self.app.push_screen(
            ConfirmScreen(
                f"Remove zone [bold]{zone}[/bold] from the managed list?\n\n"
                "[dim]This only removes it from InfraForge. "
                "The zone itself is not deleted from the DNS server.[/dim]",
                title="Remove Zone",
            ),
            callback=_on_confirm,
        )

    # ------------------------------------------------------------------
    # Actions: Record CRUD
    # ------------------------------------------------------------------

    def action_add_record(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.add_record()
            return
        zone = self._get_context_zone() or self._active_zone
        if not zone:
            self._set_status("[yellow]No active zone. Add a zone first.[/yellow]")
            return

        def _on_record_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_create_record(
                result["name"], result["rtype"], result["value"], result["ttl"],
                zone,
            )

        self.app.push_screen(
            RecordInputScreen(zone=zone, title="Add DNS Record"),
            callback=_on_record_result,
        )

    @work(thread=True)
    def _do_create_record(
        self, name: str, rtype: str, value: str, ttl: int, zone: str,
    ) -> None:
        self.app.call_from_thread(
            self._set_status,
            f"Creating {rtype} record {name} -> {value} ...",
        )
        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)
            client.create_record(name, rtype, value, ttl, zone)

            # Refresh cache
            try:
                records = client.get_zone_records(zone)
                self._records_cache[zone] = records
            except DNSError:
                pass

            self.app.call_from_thread(self._refresh_zone_records, zone)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Created {rtype} record: {name} -> {value}[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to create record: {exc}[/red]",
            )

    def action_edit_record(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.edit_record()
            return
        record = self._get_context_record()
        if record is None:
            self._set_status("[yellow]Highlight a record to edit.[/yellow]")
            return

        zone = record.zone or self._get_context_zone() or self._active_zone

        def _on_edit_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_update_record(
                record,
                result["name"], result["rtype"], result["value"], result["ttl"],
                zone,
            )

        self.app.push_screen(
            RecordInputScreen(
                zone=zone,
                name=record.name,
                rtype=record.rtype,
                value=record.value,
                ttl=str(record.ttl),
                title="Edit DNS Record",
            ),
            callback=_on_edit_result,
        )

    @work(thread=True)
    def _do_update_record(
        self,
        old_record,
        name: str,
        rtype: str,
        value: str,
        ttl: int,
        zone: str,
    ) -> None:
        self.app.call_from_thread(
            self._set_status,
            f"Updating record {name} ({rtype}) ...",
        )
        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)

            if old_record.name != name or old_record.rtype != rtype:
                client.delete_record(
                    old_record.name,
                    old_record.rtype,
                    old_record.value,
                    zone,
                )
                client.create_record(name, rtype, value, ttl, zone)
            else:
                client.update_record(name, rtype, value, ttl, zone)

            # Refresh cache
            try:
                records = client.get_zone_records(zone)
                self._records_cache[zone] = records
            except DNSError:
                pass

            self.app.call_from_thread(self._refresh_zone_records, zone)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Updated {rtype} record: {name} -> {value}[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to update record: {exc}[/red]",
            )

    def action_delete_record(self) -> None:
        if self._is_cf_tab_active() and self._cf_tab:
            self._cf_tab.delete_record()
            return
        record = self._get_context_record()
        if record is None:
            self._set_status("[yellow]Highlight a record to delete.[/yellow]")
            return

        zone = record.zone or self._get_context_zone() or self._active_zone

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._do_delete_record(record, zone)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete this record?\n\n"
                f"  [bold]{record.name}[/bold]  {record.rtype}  "
                f"{record.value}  (TTL {record.ttl})\n\n"
                "[dim]This will remove the record from the DNS server.[/dim]",
                title="Delete Record",
            ),
            callback=_on_confirm,
        )

    @work(thread=True)
    def _do_delete_record(self, record, zone: str) -> None:
        self.app.call_from_thread(
            self._set_status,
            f"Deleting {record.rtype} record {record.name} ...",
        )
        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)
            client.delete_record(
                record.name,
                record.rtype,
                record.value,
                zone,
            )

            # Refresh cache
            try:
                records = client.get_zone_records(zone)
                self._records_cache[zone] = records
            except DNSError:
                pass

            self.app.call_from_thread(self._refresh_zone_records, zone)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Deleted {record.rtype} record: {record.name} -> {record.value}[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to delete record: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Auto-discover zones
    # ------------------------------------------------------------------

    @work(thread=True)
    def _auto_discover_zones(self) -> None:
        dns_cfg = self.app.config.dns

        self.app.call_from_thread(
            self._set_status,
            f"Discovering zones from {dns_cfg.server}..."
        )
        zone_info = self.query_one("#dns-zone-info", Static)
        self.app.call_from_thread(
            zone_info.update,
            f"[bold]DNS Server:[/bold]  [green]{dns_cfg.server}:{dns_cfg.port}[/green]\n"
            f"[bold]TSIG Key:[/bold]   [green]{dns_cfg.tsig_key_name or '(none)'}[/green]\n\n"
            "[dim]Discovering zones...[/dim]"
        )

        try:
            from infraforge.dns_client import DNSClient

            client = DNSClient.from_config(self.app.config)

            hints = []
            if dns_cfg.domain:
                hints.append(dns_cfg.domain)

            discovered = client.discover_zones(hints=hints)

            if discovered:
                self._zones = discovered
                self._active_zone_index = 0
                self._persist_zones()
                self._dns_healthy = True
                self.app.call_from_thread(self._render_zone_bar)
                self.app.call_from_thread(self._build_zone_tree)
                self.app.call_from_thread(self._expand_active_zone)
                self.app.call_from_thread(
                    self._set_status,
                    f"Found {len(discovered)} zone(s) — expanding {discovered[0]}..."
                )
            else:
                self.app.call_from_thread(self._show_no_zones)

        except Exception:
            self.app.call_from_thread(self._show_no_zones)


# ---------------------------------------------------------------------------
# Label builders
# ---------------------------------------------------------------------------

def _make_zone_label(zone_name: str, soa: dict, record_count: int) -> Text:
    """Build a Rich Text label for a zone tree node."""
    label = Text()
    label.append("\U0001f310 ", style="dim")  # globe icon
    label.append(zone_name, style="bold")
    if record_count > 0:
        label.append(f"  [{record_count} records]", style="dim")
    elif soa:
        label.append("  [loaded]", style="dim green")
    else:
        label.append("  [not loaded]", style="dim")
    return label


def _make_record_label(record) -> Text:
    """Build a Rich Text label for a DNS record leaf node."""
    color = RTYPE_COLORS.get(record.rtype, "white")
    # Fixed-width columns so entries align vertically
    type_col = f"[{record.rtype}]".ljust(8)
    name_col = record.name.ljust(28)
    value_col = record.value.ljust(32)
    ttl_col = f"TTL={record.ttl}"

    label = Text()
    label.append(type_col, style=color)
    label.append(name_col, style="bold")
    label.append(value_col)
    label.append(ttl_col, style="dim")
    return label
