"""Cloudflare DNS management tab for InfraForge.

Provides a Container widget with zone tree, detail panel, and CRUD
for managing Cloudflare DNS records. Embedded in the DNS screen's
TabbedContent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode
from textual import work

from rich.text import Text

from infraforge.screens.dns_screen import (
    SORT_FIELDS, SORT_LABELS, FILTER_TYPES, FILTER_LABELS,
    RTYPE_COLORS, RTYPE_HINTS, RECORD_TYPES_FOR_INPUT,
    RecordInputScreen, ConfirmScreen,
    _make_record_label,  # reuse the record label builder
)


# ---------------------------------------------------------------------------
# Tree data model (similar to dns_screen but includes CF metadata)
# ---------------------------------------------------------------------------

@dataclass
class CFNodeData:
    """Data attached to each node in the Cloudflare DNS tree."""
    kind: Literal["zone", "record", "placeholder"]
    zone_id: str = ""
    zone_name: str = ""
    access: str = ""  # "readwrite" or "read"
    record: object = None  # DNSRecord for record nodes
    cf_id: str = ""  # Cloudflare record ID
    proxied: bool = False
    records_loaded: bool = False


# ---------------------------------------------------------------------------
# Cloudflare Tab Widget
# ---------------------------------------------------------------------------

class CloudflareTab(Container):
    """Container widget for Cloudflare DNS management."""

    def __init__(self) -> None:
        super().__init__()
        self._cf_zones: list[dict] = []  # [{id, name, status, access, permissions}, ...]
        self._active_zone_index: int = 0
        self._records_cache: dict[str, list[dict]] = {}  # zone_id -> [{record, cf_id, proxied}, ...]
        self._record_sort_index: int = 0
        self._record_sort_reverse: bool = False
        self._record_filter_index: int = 0
        self._loading: bool = False

    def compose(self) -> ComposeResult:
        # Zone selector bar
        yield Horizontal(id="cf-zone-bar")

        # Zone info banner
        yield Static("Loading Cloudflare zones...", id="cf-zone-info", markup=True)

        # Controls bar
        with Horizontal(id="cf-controls"):
            yield Static("Filter: All", id="cf-filter-label")
            yield Static("Sort: Name", id="cf-sort-label")
            yield Static("", id="cf-count-label")

        # Tree + detail panel
        with Horizontal(id="cf-main-content"):
            yield Tree("Cloudflare DNS", id="cf-tree")
            with Container(id="cf-detail-panel"):
                yield Static("[bold]Details[/bold]", id="cf-detail-title", markup=True)
                yield Static(
                    "[dim]Select an item to view details.[/dim]",
                    id="cf-detail-content", markup=True,
                )

        # Status bar
        yield Static("", id="cf-status-bar", markup=True)

    def on_mount(self) -> None:
        tree = self.query_one("#cf-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self._load_zones()

    # ------------------------------------------------------------------
    # Zone loading
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_zones(self) -> None:
        """Fetch zones from Cloudflare API."""
        self.app.call_from_thread(self._set_status, "[dim]Connecting to Cloudflare...[/dim]")
        try:
            from infraforge.cloudflare_client import CloudflareClient
            client = CloudflareClient.from_config(self.app.config)
            zones = client.list_zones()
            self._cf_zones = zones
            if zones:
                self._active_zone_index = 0
                self.app.call_from_thread(self._render_zone_bar)
                self.app.call_from_thread(self._build_zone_tree)
                self.app.call_from_thread(self._expand_active_zone)
                self.app.call_from_thread(
                    self._set_status,
                    f"[green]Connected[/green] | {len(zones)} zone(s)"
                )
            else:
                self.app.call_from_thread(
                    self.query_one("#cf-zone-info", Static).update,
                    "[yellow]No Cloudflare zones found.[/yellow]\n"
                    "[dim]Check your API token permissions.[/dim]"
                )
        except Exception as e:
            from rich.markup import escape
            self.app.call_from_thread(
                self._set_status,
                f"[red]Cloudflare error: {escape(str(e))}[/red]"
            )

    # ------------------------------------------------------------------
    # Zone bar rendering
    # ------------------------------------------------------------------

    def _render_zone_bar(self) -> None:
        bar = self.query_one("#cf-zone-bar", Horizontal)
        bar.remove_children()
        if not self._cf_zones:
            bar.mount(Static("[dim]No Cloudflare zones.[/dim]", markup=True))
            return
        for idx, zone in enumerate(self._cf_zones):
            access = zone.get("access", "read")
            access_icon = "\U0001f513" if access == "readwrite" else "\U0001f512"
            if idx == self._active_zone_index:
                label_text = f"[bold]{access_icon} {zone['name']}[/bold]"
                classes = "dns-zone-btn -active"
            else:
                label_text = f"[dim]{access_icon} {zone['name']}[/dim]"
                classes = "dns-zone-btn"
            bar.mount(Static(label_text, markup=True, classes=classes))

    # ------------------------------------------------------------------
    # Tree building
    # ------------------------------------------------------------------

    def _build_zone_tree(self) -> None:
        tree = self.query_one("#cf-tree", Tree)
        tree.clear()
        for zone in self._cf_zones:
            access = zone.get("access", "read")
            access_tag = " [RW]" if access == "readwrite" else " [RO]"
            record_count = len(self._records_cache.get(zone["id"], []))

            label = Text()
            label.append("\u2601 ", style="cyan")
            label.append(zone["name"], style="bold")
            if record_count > 0:
                label.append(f"  [{record_count} records]", style="dim")
            label.append(access_tag, style="green" if access == "readwrite" else "yellow")

            node_data = CFNodeData(
                kind="zone",
                zone_id=zone["id"],
                zone_name=zone["name"],
                access=access,
                records_loaded=zone["id"] in self._records_cache,
            )
            zone_node = tree.root.add(label, data=node_data)

            if zone["id"] in self._records_cache:
                self._populate_record_nodes(zone_node, self._records_cache[zone["id"]])
            else:
                zone_node.add_leaf(
                    Text("Loading records...", style="dim"),
                    data=CFNodeData(kind="placeholder", zone_id=zone["id"], zone_name=zone["name"]),
                )

    def _expand_active_zone(self) -> None:
        if not self._cf_zones:
            return
        zone = self._cf_zones[self._active_zone_index]
        tree = self.query_one("#cf-tree", Tree)
        for node in tree.root.children:
            if node.data and node.data.kind == "zone" and node.data.zone_id == zone["id"]:
                node.expand()
                tree.select_node(node)
                break

    # ------------------------------------------------------------------
    # Lazy-load records on expand
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
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
        zone_id = node.data.zone_id
        zone_name = node.data.zone_name
        self.app.call_from_thread(self._set_status, f"Loading records for {zone_name}...")
        try:
            from infraforge.cloudflare_client import CloudflareClient
            client = CloudflareClient.from_config(self.app.config)
            records = client.list_records(zone_id, zone_name)
            self._records_cache[zone_id] = records
            self.app.call_from_thread(self._populate_record_nodes, node, records)
            self.app.call_from_thread(self._update_zone_node_label, node)
            self.app.call_from_thread(self._update_zone_info)
            self.app.call_from_thread(self._update_controls)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {zone_name}: {len(records)} records"
            )
        except Exception as e:
            from rich.markup import escape
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load {zone_name}: {escape(str(e))}[/red]"
            )

    def _populate_record_nodes(self, parent_node: TreeNode, records: list[dict]) -> None:
        parent_node.remove_children()

        # Apply sort and filter
        sorted_recs = self._sort_records(records)
        filtered_recs = self._filter_records(sorted_recs)

        for entry in filtered_recs:
            rec = entry["record"]
            proxied = entry.get("proxied", False)
            cf_id = entry.get("cf_id", "")

            label = _make_cf_record_label(rec, proxied)
            data = CFNodeData(
                kind="record",
                zone_id=parent_node.data.zone_id,
                zone_name=parent_node.data.zone_name,
                access=parent_node.data.access,
                record=rec,
                cf_id=cf_id,
                proxied=proxied,
            )
            parent_node.add_leaf(label, data=data)

        if not filtered_recs:
            parent_node.add_leaf(
                Text("(no records)", style="dim italic"),
                data=CFNodeData(kind="placeholder", zone_id=parent_node.data.zone_id, zone_name=parent_node.data.zone_name),
            )
        parent_node.data.records_loaded = True

    def _update_zone_node_label(self, node: TreeNode) -> None:
        zone = node.data
        access_tag = " [RW]" if zone.access == "readwrite" else " [RO]"
        record_count = len(self._records_cache.get(zone.zone_id, []))
        label = Text()
        label.append("\u2601 ", style="cyan")
        label.append(zone.zone_name, style="bold")
        if record_count > 0:
            label.append(f"  [{record_count} records]", style="dim")
        label.append(access_tag, style="green" if zone.access == "readwrite" else "yellow")
        node.set_label(label)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, 'kind'):
            return
        if node.data.kind == "zone":
            self._show_zone_detail(node.data)
        elif node.data.kind == "record":
            self._show_record_detail(node.data)

    def _show_zone_detail(self, data: CFNodeData) -> None:
        title = self.query_one("#cf-detail-title", Static)
        detail = self.query_one("#cf-detail-content", Static)
        title.update("[bold]Zone Details[/bold]")

        records = self._records_cache.get(data.zone_id, [])
        type_counts: dict[str, int] = {}
        for entry in records:
            rtype = entry["record"].rtype
            type_counts[rtype] = type_counts.get(rtype, 0) + 1
        type_summary = "  ".join(
            f"[{RTYPE_COLORS.get(t, 'white')}]{t}: {c}[/{RTYPE_COLORS.get(t, 'white')}]"
            for t, c in sorted(type_counts.items())
        )

        access_color = "green" if data.access == "readwrite" else "yellow"
        access_label = "Read / Write" if data.access == "readwrite" else "Read Only"

        lines = [
            f"[bold]Zone:[/bold]       {data.zone_name}",
            f"[bold]Provider:[/bold]   [cyan]Cloudflare[/cyan]",
            f"[bold]Access:[/bold]     [{access_color}]{access_label}[/{access_color}]",
            f"[bold]Records:[/bold]    [cyan]{len(records)}[/cyan]",
            "",
        ]
        if type_summary:
            lines.append(f"[bold]Types:[/bold]      {type_summary}")
        detail.update("\n".join(lines))

    def _show_record_detail(self, data: CFNodeData) -> None:
        title = self.query_one("#cf-detail-title", Static)
        detail = self.query_one("#cf-detail-content", Static)
        title.update("[bold]Record Details[/bold]")

        rec = data.record
        color = RTYPE_COLORS.get(rec.rtype, "white")
        hint = RTYPE_HINTS.get(rec.rtype, "")
        proxied_str = "[orange1]Proxied[/orange1]" if data.proxied else "[dim]DNS Only[/dim]"

        lines = [
            f"[bold]Name:[/bold]      {rec.name}",
            f"[bold]Type:[/bold]      [{color}]{rec.rtype}[/{color}]",
            f"[bold]Value:[/bold]     {rec.value}",
            f"[bold]TTL:[/bold]       {'Auto' if rec.ttl == 1 else f'{rec.ttl}s'}",
            f"[bold]Proxied:[/bold]   {proxied_str}",
            f"[bold]Zone:[/bold]      {data.zone_name}",
            f"[bold]CF ID:[/bold]     [dim]{data.cf_id[:12]}...[/dim]" if data.cf_id else "",
        ]
        if hint:
            lines.append("")
            lines.append(f"[dim italic]{hint}[/dim italic]")
        detail.update("\n".join(lines))

    # ------------------------------------------------------------------
    # Sort / filter
    # ------------------------------------------------------------------

    def _sort_records(self, records: list[dict]) -> list[dict]:
        result = list(records)
        sort_field = SORT_FIELDS[self._record_sort_index]
        if sort_field == "ttl":
            result.sort(key=lambda e: e["record"].ttl, reverse=self._record_sort_reverse)
        else:
            result.sort(
                key=lambda e: getattr(e["record"], sort_field, "").lower(),
                reverse=self._record_sort_reverse,
            )
        return result

    def _filter_records(self, records: list[dict]) -> list[dict]:
        if self._record_filter_index == 0:
            return records
        filter_type = FILTER_TYPES[self._record_filter_index]
        return [e for e in records if e["record"].rtype == filter_type]

    def _update_controls(self) -> None:
        try:
            filter_label = self.query_one("#cf-filter-label", Static)
            sort_label = self.query_one("#cf-sort-label", Static)
            count_label = self.query_one("#cf-count-label", Static)
        except Exception:
            return
        current_filter = FILTER_LABELS[self._record_filter_index]
        current_sort = SORT_LABELS[self._record_sort_index]
        direction = " \u25bc" if self._record_sort_reverse else " \u25b2"
        filter_label.update(f"Filter: [bold]{current_filter}[/bold]")
        sort_label.update(f"Sort: [bold]{current_sort}{direction}[/bold]")
        total = sum(len(v) for v in self._records_cache.values())
        count_label.update(f"[dim]{total} records across {len(self._cf_zones)} zones[/dim]")

    def _update_zone_info(self) -> None:
        zone_info = self.query_one("#cf-zone-info", Static)
        if not self._cf_zones:
            return
        total_records = sum(len(v) for v in self._records_cache.values())
        rw = sum(1 for z in self._cf_zones if z.get("access") == "readwrite")
        ro = len(self._cf_zones) - rw
        lines = [
            f"[bold]Provider:[/bold]  [cyan]Cloudflare[/cyan]"
            f"    [bold]Zones:[/bold]  [cyan]{len(self._cf_zones)}[/cyan] "
            f"([green]{rw} RW[/green], [yellow]{ro} RO[/yellow])"
            f"    [bold]Total Records:[/bold]  [cyan]{total_records}[/cyan]",
        ]
        zone_info.update("\n".join(lines))

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#cf-status-bar", Static).update(text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public action methods (called by parent DNSScreen)
    # ------------------------------------------------------------------

    def cycle_sort(self) -> None:
        fields = SORT_FIELDS
        if self._record_sort_index == len(fields) - 1 and not self._record_sort_reverse:
            self._record_sort_reverse = True
        elif self._record_sort_reverse:
            self._record_sort_reverse = False
            self._record_sort_index = (self._record_sort_index + 1) % len(fields)
        else:
            self._record_sort_index = (self._record_sort_index + 1) % len(fields)
        self._re_sort_all()
        self._update_controls()

    def cycle_filter(self) -> None:
        self._record_filter_index = (self._record_filter_index + 1) % len(FILTER_TYPES)
        self._re_sort_all()
        self._update_controls()

    def refresh_data(self) -> None:
        self._records_cache.clear()
        self._load_zones()

    def _re_sort_all(self) -> None:
        tree = self.query_one("#cf-tree", Tree)
        for node in tree.root.children:
            if node.data and node.data.kind == "zone" and node.data.records_loaded and node.is_expanded:
                records = self._records_cache.get(node.data.zone_id, [])
                self._populate_record_nodes(node, records)

    def _get_highlighted_node(self) -> TreeNode | None:
        tree = self.query_one("#cf-tree", Tree)
        cursor = tree.cursor_line
        if cursor < 0:
            return None
        try:
            return tree.get_node_at_line(cursor)
        except Exception:
            return None

    def _get_context_zone_data(self) -> CFNodeData | None:
        """Get zone data for the current context."""
        node = self._get_highlighted_node()
        if node is None or node.data is None:
            return None
        if node.data.kind == "zone":
            return node.data
        if node.data.kind == "record":
            # Find parent zone
            for znode in self.query_one("#cf-tree", Tree).root.children:
                if znode.data and znode.data.zone_id == node.data.zone_id:
                    return znode.data
        return None

    def add_record(self) -> None:
        """Add a new DNS record to the active zone."""
        zone_data = self._get_context_zone_data()
        if not zone_data:
            if self._cf_zones:
                zone_data = CFNodeData(
                    kind="zone",
                    zone_id=self._cf_zones[self._active_zone_index]["id"],
                    zone_name=self._cf_zones[self._active_zone_index]["name"],
                    access=self._cf_zones[self._active_zone_index].get("access", "read"),
                )
            else:
                self._set_status("[yellow]No zones available.[/yellow]")
                return

        if zone_data.access != "readwrite":
            self._set_status(f"[yellow]{zone_data.zone_name} is read-only.[/yellow]")
            return

        def _on_result(result: dict | None) -> None:
            if result is None:
                return
            self._do_create_record(
                zone_data.zone_id, zone_data.zone_name,
                result["name"], result["rtype"], result["value"], result["ttl"],
            )

        self.app.push_screen(
            RecordInputScreen(zone=zone_data.zone_name, title="Add Cloudflare DNS Record"),
            callback=_on_result,
        )

    @work(thread=True)
    def _do_create_record(self, zone_id: str, zone_name: str, name: str, rtype: str, value: str, ttl: int) -> None:
        self.app.call_from_thread(self._set_status, f"Creating {rtype} record {name}...")
        try:
            from infraforge.cloudflare_client import CloudflareClient
            client = CloudflareClient.from_config(self.app.config)
            # Qualify name with zone
            fqdn = f"{name}.{zone_name}" if name != "@" and not name.endswith(zone_name) else (zone_name if name == "@" else name)
            client.create_record(zone_id, fqdn, rtype, value, ttl=ttl)
            # Refresh
            records = client.list_records(zone_id, zone_name)
            self._records_cache[zone_id] = records
            self.app.call_from_thread(self._refresh_zone_node, zone_id)
            self.app.call_from_thread(self._set_status, f"[green]Created {rtype}: {name} -> {value}[/green]")
        except Exception as e:
            from rich.markup import escape
            self.app.call_from_thread(self._set_status, f"[red]Failed: {escape(str(e))}[/red]")

    def edit_record(self) -> None:
        """Edit the highlighted record."""
        node = self._get_highlighted_node()
        if not node or not node.data or node.data.kind != "record":
            self._set_status("[yellow]Select a record to edit.[/yellow]")
            return
        if node.data.access != "readwrite":
            self._set_status(f"[yellow]{node.data.zone_name} is read-only.[/yellow]")
            return

        rec = node.data.record
        cf_id = node.data.cf_id
        zone_id = node.data.zone_id
        zone_name = node.data.zone_name

        def _on_result(result: dict | None) -> None:
            if result is None:
                return
            self._do_update_record(
                zone_id, zone_name, cf_id,
                result["name"], result["rtype"], result["value"], result["ttl"],
            )

        self.app.push_screen(
            RecordInputScreen(
                zone=zone_name, name=rec.name, rtype=rec.rtype,
                value=rec.value, ttl=str(rec.ttl),
                title="Edit Cloudflare DNS Record",
            ),
            callback=_on_result,
        )

    @work(thread=True)
    def _do_update_record(self, zone_id: str, zone_name: str, cf_id: str, name: str, rtype: str, value: str, ttl: int) -> None:
        self.app.call_from_thread(self._set_status, f"Updating {rtype} record {name}...")
        try:
            from infraforge.cloudflare_client import CloudflareClient
            client = CloudflareClient.from_config(self.app.config)
            fqdn = f"{name}.{zone_name}" if name != "@" and not name.endswith(zone_name) else (zone_name if name == "@" else name)
            client.update_record(zone_id, cf_id, fqdn, rtype, value, ttl=ttl)
            records = client.list_records(zone_id, zone_name)
            self._records_cache[zone_id] = records
            self.app.call_from_thread(self._refresh_zone_node, zone_id)
            self.app.call_from_thread(self._set_status, f"[green]Updated {rtype}: {name} -> {value}[/green]")
        except Exception as e:
            from rich.markup import escape
            self.app.call_from_thread(self._set_status, f"[red]Failed: {escape(str(e))}[/red]")

    def delete_record(self) -> None:
        """Delete the highlighted record."""
        node = self._get_highlighted_node()
        if not node or not node.data or node.data.kind != "record":
            self._set_status("[yellow]Select a record to delete.[/yellow]")
            return
        if node.data.access != "readwrite":
            self._set_status(f"[yellow]{node.data.zone_name} is read-only.[/yellow]")
            return

        rec = node.data.record
        cf_id = node.data.cf_id
        zone_id = node.data.zone_id
        zone_name = node.data.zone_name

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._do_delete_record(zone_id, zone_name, cf_id, rec)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete this Cloudflare record?\n\n"
                f"  [bold]{rec.name}[/bold]  {rec.rtype}  {rec.value}\n\n"
                "[dim]This will remove the record from Cloudflare.[/dim]",
                title="Delete Record",
            ),
            callback=_on_confirm,
        )

    @work(thread=True)
    def _do_delete_record(self, zone_id: str, zone_name: str, cf_id: str, rec) -> None:
        self.app.call_from_thread(self._set_status, f"Deleting {rec.rtype} record {rec.name}...")
        try:
            from infraforge.cloudflare_client import CloudflareClient
            client = CloudflareClient.from_config(self.app.config)
            client.delete_record(zone_id, cf_id)
            records = client.list_records(zone_id, zone_name)
            self._records_cache[zone_id] = records
            self.app.call_from_thread(self._refresh_zone_node, zone_id)
            self.app.call_from_thread(self._set_status, f"[green]Deleted {rec.rtype}: {rec.name}[/green]")
        except Exception as e:
            from rich.markup import escape
            self.app.call_from_thread(self._set_status, f"[red]Failed: {escape(str(e))}[/red]")

    def _refresh_zone_node(self, zone_id: str) -> None:
        tree = self.query_one("#cf-tree", Tree)
        for node in tree.root.children:
            if node.data and node.data.zone_id == zone_id:
                records = self._records_cache.get(zone_id, [])
                self._populate_record_nodes(node, records)
                self._update_zone_node_label(node)
                break
        self._update_controls()
        self._update_zone_info()


# ---------------------------------------------------------------------------
# Label builder
# ---------------------------------------------------------------------------

def _make_cf_record_label(record, proxied: bool = False) -> Text:
    """Build a Rich Text label for a Cloudflare DNS record."""
    from infraforge.screens.dns_screen import RTYPE_COLORS
    color = RTYPE_COLORS.get(record.rtype, "white")
    type_col = f"[{record.rtype}]".ljust(8)
    name_col = record.name.ljust(28)
    value_col = record.value.ljust(32)
    proxy_col = "\u26a1" if proxied else "  "
    ttl_col = "Auto" if record.ttl == 1 else f"TTL={record.ttl}"

    label = Text()
    label.append(type_col, style=color)
    label.append(name_col, style="bold")
    label.append(value_col)
    label.append(f"{proxy_col} ", style="orange1" if proxied else "dim")
    label.append(ttl_col, style="dim")
    return label
