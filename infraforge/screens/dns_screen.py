"""Multi-zone DNS management screen for InfraForge.

Supports full CRUD operations on DNS records across multiple zones,
with zone switching, sort/filter, and modal input screens for
adding/editing records and zones.

CSS additions needed in styles/app.tcss:
------------------------------------------------------------------------
#dns-zone-bar {
    layout: horizontal;
    height: 3;
    margin: 0 0 0 0;
    border: round $primary-background;
    padding: 0 1;
}

.dns-zone-btn {
    width: auto;
    padding: 1 2;
    color: $text-muted;
    text-style: italic;
}

.dns-zone-btn.-active {
    color: $accent;
    text-style: bold;
    background: $primary-background;
}

#dns-status-bar {
    height: 1;
    margin: 1 0 0 0;
    background: $primary-background;
    color: $text-muted;
    padding: 0 1;
}

/* Modal input screens */
.modal-container {
    align: center middle;
}

.modal-box {
    width: 60;
    height: auto;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}

.modal-title {
    text-style: bold;
    color: $accent;
    text-align: center;
    width: 100%;
    margin: 0 0 1 0;
}

.modal-buttons {
    layout: horizontal;
    height: 3;
    margin: 1 0 0 0;
    content-align: center middle;
}
------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Callable, Optional

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
)
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
    """Modal screen for adding or editing a DNS record.

    On dismiss, returns a dict with keys: name, rtype, value, ttl
    or None if cancelled.
    """

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
        self._records: list = []  # list[DNSRecord]
        self._soa: dict = {}
        self._sort_index: int = 0
        self._sort_reverse: bool = False
        self._filter_index: int = 0
        self._dns_healthy: bool = False
        self._loading: bool = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="dns-container"):
            yield Static("DNS Management", classes="section-title")

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

            # Records table
            yield DataTable(id="dns-table", cursor_type="row")

            # Status bar
            yield Static("", id="dns-status-bar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dns-table", DataTable)
        table.add_columns("Name", "Type", "Value", "TTL")
        table.zebra_stripes = True

        # Initialise zone list from config
        self._init_zones()
        self._render_zone_bar()

        if self._zones:
            self.load_zone_data()
        else:
            dns_cfg = self.app.config.dns
            if dns_cfg.provider == "bind9" and dns_cfg.server:
                self._auto_discover_zones()
            else:
                self._show_not_configured()

    # ------------------------------------------------------------------
    # Zone management helpers
    # ------------------------------------------------------------------

    def _init_zones(self) -> None:
        """Populate the zone list from config.

        Uses ``self.app.config.dns.zones`` (list).
        """
        dns_cfg = self.app.config.dns
        zones: list[str] = getattr(dns_cfg, "zones", None) or []
        self._zones = list(zones)
        if self._zones:
            self._active_zone_index = 0

    def _persist_zones(self) -> None:
        """Write the current zone list back to config."""
        dns_cfg = self.app.config.dns
        dns_cfg.zones = list(self._zones)

    @property
    def _active_zone(self) -> str:
        if not self._zones:
            return ""
        return self._zones[self._active_zone_index]

    def _render_zone_bar(self) -> None:
        """Rebuild the zone selector bar widgets."""
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
    # Data loading
    # ------------------------------------------------------------------

    @work(thread=True)
    def load_zone_data(self) -> None:
        """Load DNS zone info and records for the active zone."""
        self._loading = True
        dns_cfg = self.app.config.dns
        zone = self._active_zone

        if not zone:
            self.app.call_from_thread(self._show_not_configured)
            self._loading = False
            return

        if dns_cfg.provider != "bind9" or not dns_cfg.server:
            self.app.call_from_thread(self._show_not_configured)
            self._loading = False
            return

        self.app.call_from_thread(self._set_status, "Connecting to DNS server...")

        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)

            # Health check
            healthy = client.check_health(zone)
            self._dns_healthy = healthy

            if not healthy:
                self.app.call_from_thread(
                    self._show_error,
                    f"Cannot reach DNS server at {dns_cfg.server}:{dns_cfg.port}",
                )
                self._loading = False
                return

            self.app.call_from_thread(
                self._set_status, f"Loading zone {zone}...",
            )

            # SOA
            try:
                soa = client.get_zone_soa(zone)
                self._soa = soa
            except DNSError:
                self._soa = {}

            # Records via AXFR
            try:
                records = client.get_zone_records(zone)
                self._records = records
            except DNSError as exc:
                self._records = []
                self.app.call_from_thread(
                    self._show_error, f"Zone transfer failed: {exc}",
                )
                self._loading = False
                return

            self.app.call_from_thread(self._update_display)

        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # Display update helpers
    # ------------------------------------------------------------------

    @work(thread=True)
    def _auto_discover_zones(self) -> None:
        """Auto-discover zones from the DNS server when none are configured."""
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

            # Build hints: domain is the primary candidate
            hints = []
            if dns_cfg.domain:
                hints.append(dns_cfg.domain)

            discovered = client.discover_zones(hints=hints)

            if discovered:
                self._zones = discovered
                self._active_zone_index = 0
                self._persist_zones()
                self.app.call_from_thread(self._render_zone_bar)
                # Now load the first zone's data
                self._loading = True
                self.app.call_from_thread(
                    self._set_status,
                    f"Found {len(discovered)} zone(s) — loading {discovered[0]}..."
                )

                from infraforge.dns_client import DNSError

                zone = self._active_zone
                try:
                    soa = client.get_zone_soa(zone)
                    self._soa = soa
                except DNSError:
                    self._soa = {}

                try:
                    records = client.get_zone_records(zone)
                    self._records = records
                except DNSError as exc:
                    self._records = []
                    self.app.call_from_thread(
                        self._show_error, f"Zone transfer failed: {exc}"
                    )
                    self._loading = False
                    return

                self.app.call_from_thread(self._update_display)
                self._loading = False
            else:
                self.app.call_from_thread(self._show_no_zones)

        except Exception as exc:
            self.app.call_from_thread(self._show_no_zones)

    def _show_no_zones(self) -> None:
        """DNS is configured but no zones are added yet."""
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

    def _show_not_configured(self) -> None:
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(
            "[yellow]BIND9 DNS is not configured.[/yellow]\n\n"
            "[dim]To enable DNS management, add the following to your config:[/dim]\n\n"
            "[dim]dns:[/dim]\n"
            "[dim]  provider: bind9[/dim]\n"
            "[dim]  server: 10.0.0.1[/dim]\n"
            "[dim]  zones:[/dim]\n"
            "[dim]    - lab.local[/dim]\n"
            "[dim]  domain: lab.local[/dim]\n"
            "[dim]  tsig_key_name: infraforge-key[/dim]\n"
            "[dim]  tsig_key_secret: <base64-key>[/dim]\n"
            "[dim]  tsig_algorithm: hmac-sha256[/dim]\n\n"
            "[dim]Run 'infraforge setup' to configure interactively.[/dim]"
        )

    def _show_error(self, error: str) -> None:
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(f"[red]Error: {error}[/red]")
        self._set_status(f"[red]Error: {error}[/red]")

    def _set_status(self, text: str) -> None:
        self.query_one("#dns-status-bar", Static).update(text)

    def _update_display(self) -> None:
        self._update_zone_info()
        self._update_controls()
        self._populate_table()
        self._set_status(
            f"[green]Connected[/green] | Zone: [bold]{self._active_zone}[/bold] "
            f"| {len(self._records)} records loaded"
        )

    def _update_zone_info(self) -> None:
        zone_info = self.query_one("#dns-zone-info", Static)
        dns_cfg = self.app.config.dns

        lines = [
            f"[bold]Zone:[/bold]    [green]{self._active_zone}[/green]"
            f"    [bold]Server:[/bold]  [green]{dns_cfg.server}:{dns_cfg.port}[/green]"
            f"    [bold]Status:[/bold]  [green]Connected[/green]"
            f"    [bold]Records:[/bold]  [cyan]{len(self._records)}[/cyan]",
        ]

        if self._soa:
            lines.append(
                f"[bold]Serial:[/bold]  [cyan]{self._soa.get('serial', '?')}[/cyan]"
                f"    [bold]Primary NS:[/bold]  [cyan]{self._soa.get('mname', '?')}[/cyan]"
                f"    [bold]Refresh:[/bold]  [cyan]{self._soa.get('refresh', '?')}s[/cyan]"
            )

        zone_info.update("\n".join(lines))

    def _update_controls(self) -> None:
        filter_label = self.query_one("#dns-filter-label", Static)
        sort_label = self.query_one("#dns-sort-label", Static)
        count_label = self.query_one("#dns-count-label", Static)

        current_filter = FILTER_LABELS[self._filter_index]
        current_sort = SORT_LABELS[self._sort_index]
        direction = " ▼" if self._sort_reverse else " ▲"

        filter_label.update(f"Filter: [bold]{current_filter}[/bold]")
        sort_label.update(f"Sort: [bold]{current_sort}{direction}[/bold]")

        filtered = self._get_filtered_records()
        total = len(self._records)
        shown = len(filtered)
        if shown == total:
            count_label.update(f"[dim]{total} records[/dim]")
        else:
            count_label.update(f"[dim]{shown} / {total} records[/dim]")

    def _get_filtered_records(self) -> list:
        records = self._records

        # Apply type filter
        if self._filter_index > 0:
            filter_type = FILTER_TYPES[self._filter_index]
            records = [r for r in records if r.rtype == filter_type]

        # Apply sort
        sort_field = SORT_FIELDS[self._sort_index]
        if sort_field == "ttl":
            records = sorted(
                records, key=lambda r: r.ttl, reverse=self._sort_reverse,
            )
        else:
            records = sorted(
                records,
                key=lambda r: getattr(r, sort_field, "").lower(),
                reverse=self._sort_reverse,
            )

        return records

    def _populate_table(self) -> None:
        table = self.query_one("#dns-table", DataTable)
        table.clear()

        filtered = self._get_filtered_records()
        self._update_controls()

        for record in filtered:
            color = RTYPE_COLORS.get(record.rtype, "white")

            name_text = Text(record.name)
            rtype_text = Text(record.rtype)
            rtype_text.stylize(color)
            value_text = Text(record.value)
            ttl_text = Text(str(record.ttl))

            table.add_row(
                name_text,
                rtype_text,
                value_text,
                ttl_text,
                key=f"dns_{record.name}_{record.rtype}_{record.value}",
            )

    # ------------------------------------------------------------------
    # Record selection helper
    # ------------------------------------------------------------------

    def _get_selected_record(self):
        """Return the DNSRecord corresponding to the currently selected row,
        or None if nothing is selected."""
        table = self.query_one("#dns-table", DataTable)
        try:
            row_key = table.get_row_at(table.cursor_row)
        except Exception:
            return None

        cursor_key = str(table.coordinate_to_cell_key(
            table.cursor_coordinate,
        ).row_key)

        # Row keys have the form dns_{name}_{rtype}_{value}
        if not cursor_key.startswith("dns_"):
            return None

        # Walk filtered records to find the match by index
        filtered = self._get_filtered_records()
        idx = table.cursor_row
        if 0 <= idx < len(filtered):
            return filtered[idx]
        return None

    # ------------------------------------------------------------------
    # Actions: Navigation
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # Actions: Sort / Filter / Refresh
    # ------------------------------------------------------------------

    def action_cycle_sort(self) -> None:
        if self._sort_index == len(SORT_FIELDS) - 1 and not self._sort_reverse:
            self._sort_reverse = True
        elif self._sort_reverse:
            self._sort_reverse = False
            self._sort_index = (self._sort_index + 1) % len(SORT_FIELDS)
        else:
            self._sort_index = (self._sort_index + 1) % len(SORT_FIELDS)

        self._populate_table()

    def action_cycle_filter(self) -> None:
        self._filter_index = (self._filter_index + 1) % len(FILTER_TYPES)
        self._populate_table()

    def action_refresh(self) -> None:
        if self._loading:
            return
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update("[dim]Refreshing...[/dim]")
        self._set_status("[dim]Refreshing...[/dim]")
        self.load_zone_data()

    # ------------------------------------------------------------------
    # Actions: Zone switching
    # ------------------------------------------------------------------

    def _switch_to_zone(self, index: int) -> None:
        """Switch to the zone at the given index and reload."""
        if not self._zones:
            return
        if index < 0 or index >= len(self._zones):
            return
        if index == self._active_zone_index and self._records:
            return  # already on this zone

        self._active_zone_index = index
        self._records = []
        self._soa = {}
        self._render_zone_bar()
        self._persist_zones()

        table = self.query_one("#dns-table", DataTable)
        table.clear()

        self.load_zone_data()

    def action_next_zone(self) -> None:
        if self._zones:
            new_index = (self._active_zone_index + 1) % len(self._zones)
            self._switch_to_zone(new_index)

    def action_prev_zone(self) -> None:
        if self._zones:
            new_index = (self._active_zone_index - 1) % len(self._zones)
            self._switch_to_zone(new_index)

    # Number-key zone selectors (1-9)
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
        """Open the zone input modal to add a new zone."""

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
            # Validate via SOA query in background
            self._validate_and_add_zone(zone_name)

        self.app.push_screen(ZoneInputScreen(), callback=_on_zone_result)

    @work(thread=True)
    def _validate_and_add_zone(self, zone_name: str) -> None:
        """Check that the zone exists on the server, then add it."""
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

            # Zone is valid -- add and switch
            self.app.call_from_thread(self._finalize_add_zone, zone_name)

        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to validate zone {zone_name}: {exc}[/red]",
            )

    def _finalize_add_zone(self, zone_name: str) -> None:
        """Add the zone to the list and switch to it (main-thread)."""
        self._zones.append(zone_name)
        new_index = len(self._zones) - 1
        self._active_zone_index = new_index
        self._persist_zones()
        self._render_zone_bar()
        self._records = []
        self._soa = {}
        self.load_zone_data()

    def action_remove_zone(self) -> None:
        """Remove the current zone from the managed list (with confirmation)."""
        if not self._zones:
            self._set_status("[yellow]No zones to remove.[/yellow]")
            return

        zone = self._active_zone

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._zones.remove(zone)
            if self._zones:
                self._active_zone_index = min(
                    self._active_zone_index, len(self._zones) - 1,
                )
            else:
                self._active_zone_index = 0
            self._persist_zones()
            self._render_zone_bar()

            if self._zones:
                self._records = []
                self._soa = {}
                self.load_zone_data()
            else:
                # No zones left
                self._records = []
                self._soa = {}
                table = self.query_one("#dns-table", DataTable)
                table.clear()
                zone_info = self.query_one("#dns-zone-info", Static)
                zone_info.update(
                    "[yellow]No zones configured. Press [bold]z[/bold] to add one.[/yellow]"
                )
                self._set_status("[dim]No active zone.[/dim]")

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
        """Open the record input modal to create a new DNS record."""
        if not self._active_zone:
            self._set_status("[yellow]No active zone. Add a zone first.[/yellow]")
            return

        def _on_record_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_create_record(
                result["name"],
                result["rtype"],
                result["value"],
                result["ttl"],
            )

        self.app.push_screen(
            RecordInputScreen(
                zone=self._active_zone,
                title="Add DNS Record",
            ),
            callback=_on_record_result,
        )

    @work(thread=True)
    def _do_create_record(
        self, name: str, rtype: str, value: str, ttl: int,
    ) -> None:
        self.app.call_from_thread(
            self._set_status,
            f"Creating {rtype} record {name} -> {value} ...",
        )
        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)
            client.create_record(name, rtype, value, ttl, self._active_zone)

            # Refresh records
            try:
                records = client.get_zone_records(self._active_zone)
                self._records = records
            except DNSError:
                pass

            self.app.call_from_thread(self._update_display)
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
        """Edit the currently selected DNS record."""
        record = self._get_selected_record()
        if record is None:
            self._set_status("[yellow]No record selected.[/yellow]")
            return

        def _on_edit_result(result: Optional[dict]) -> None:
            if result is None:
                return
            self._do_update_record(
                record,
                result["name"],
                result["rtype"],
                result["value"],
                result["ttl"],
            )

        self.app.push_screen(
            RecordInputScreen(
                zone=self._active_zone,
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
    ) -> None:
        self.app.call_from_thread(
            self._set_status,
            f"Updating record {name} ({rtype}) ...",
        )
        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient.from_config(self.app.config)

            # If name or type changed, delete old and create new
            if old_record.name != name or old_record.rtype != rtype:
                client.delete_record(
                    old_record.name,
                    old_record.rtype,
                    old_record.value,
                    self._active_zone,
                )
                client.create_record(name, rtype, value, ttl, self._active_zone)
            else:
                client.update_record(name, rtype, value, ttl, self._active_zone)

            # Refresh records via AXFR
            try:
                records = client.get_zone_records(self._active_zone)
                self._records = records
            except DNSError:
                pass

            self.app.call_from_thread(self._update_display)
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
        """Delete the currently selected DNS record (with confirmation)."""
        record = self._get_selected_record()
        if record is None:
            self._set_status("[yellow]No record selected.[/yellow]")
            return

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self._do_delete_record(record)

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
    def _do_delete_record(self, record) -> None:
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
                self._active_zone,
            )

            # Refresh records via AXFR
            try:
                records = client.get_zone_records(self._active_zone)
                self._records = records
            except DNSError:
                pass

            self.app.call_from_thread(self._update_display)
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
    # DataTable row selection (Enter key)
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a record row -- show details in status bar."""
        record = self._get_selected_record()
        if record is None:
            return
        self._set_status(
            f"[bold]{record.name}[/bold]  "
            f"[{RTYPE_COLORS.get(record.rtype, 'white')}]{record.rtype}[/{RTYPE_COLORS.get(record.rtype, 'white')}]  "
            f"{record.value}  TTL={record.ttl}"
        )
