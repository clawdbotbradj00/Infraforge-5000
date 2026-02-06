"""DNS management screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, DataTable
from textual.containers import Container, Horizontal, VerticalScroll
from textual import work

from rich.text import Text


SORT_FIELDS = ["name", "rtype", "value", "ttl"]
SORT_LABELS = ["Name", "Type", "Value", "TTL"]
FILTER_TYPES = ["all", "A", "AAAA", "CNAME", "PTR", "TXT", "MX", "SRV", "NS", "SOA"]
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


class DNSScreen(Screen):
    """Screen for viewing and managing DNS records."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._records = []
        self._soa = {}
        self._sort_index = 0
        self._sort_reverse = False
        self._filter_index = 0
        self._record_count = 0
        self._dns_healthy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="dns-container"):
            yield Static("DNS Management", classes="section-title")

            # Zone info banner
            yield Static("Loading DNS zone info...", id="dns-zone-info", markup=True)

            # Controls bar
            with Horizontal(id="dns-controls"):
                yield Static("Filter: All", id="dns-filter-label")
                yield Static("Sort: Name", id="dns-sort-label")
                yield Static("", id="dns-count-label")

            # Records table
            yield DataTable(id="dns-table", cursor_type="row")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#dns-table", DataTable)
        table.add_columns("Name", "Type", "Value", "TTL")
        self.load_dns_data()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @work(thread=True)
    def load_dns_data(self):
        """Load DNS zone info and records."""
        dns_cfg = self.app.config.dns

        if dns_cfg.provider != "bind9" or not dns_cfg.server:
            self.app.call_from_thread(self._show_not_configured)
            return

        try:
            from infraforge.dns_client import DNSClient, DNSError

            client = DNSClient(self.app.config)

            # Check health first
            healthy = client.check_health()
            self._dns_healthy = healthy

            if not healthy:
                self.app.call_from_thread(
                    self._show_error,
                    f"Cannot reach DNS server at {dns_cfg.server}:{dns_cfg.port}",
                )
                return

            # Load SOA and records in sequence (both need the connection)
            try:
                soa = client.get_zone_soa()
                self._soa = soa
            except DNSError:
                self._soa = {}

            try:
                records = client.get_zone_records()
                self._records = records
            except DNSError as e:
                self._records = []
                self.app.call_from_thread(
                    self._show_error,
                    f"Zone transfer failed: {e}",
                )
                return

            self.app.call_from_thread(self._update_display)

        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    # ------------------------------------------------------------------
    # Display updates
    # ------------------------------------------------------------------

    def _show_not_configured(self):
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(
            "[yellow]BIND9 DNS is not configured.[/yellow]\n\n"
            "[dim]To enable DNS management, add the following to your config:[/dim]\n\n"
            "[dim]dns:[/dim]\n"
            "[dim]  provider: bind9[/dim]\n"
            "[dim]  server: 10.0.0.1[/dim]\n"
            "[dim]  zone: lab.local[/dim]\n"
            "[dim]  domain: lab.local[/dim]\n"
            "[dim]  tsig_key_name: infraforge-key[/dim]\n"
            "[dim]  tsig_key_secret: <base64-key>[/dim]\n"
            "[dim]  tsig_algorithm: hmac-sha256[/dim]\n\n"
            "[dim]Run 'infraforge setup' to configure interactively.[/dim]"
        )

    def _show_error(self, error: str):
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update(f"[red]Error: {error}[/red]")

    def _update_display(self):
        self._update_zone_info()
        self._update_controls()
        self._populate_table()

    def _update_zone_info(self):
        zone_info = self.query_one("#dns-zone-info", Static)
        dns_cfg = self.app.config.dns

        lines = [
            f"[bold]Zone:[/bold]    [green]{dns_cfg.zone}[/green]"
            f"    [bold]Server:[/bold]  [green]{dns_cfg.server}:{dns_cfg.port}[/green]"
            f"    [bold]Status:[/bold]  [green]Connected[/green]",
        ]

        if self._soa:
            lines.append(
                f"[bold]Serial:[/bold]  [cyan]{self._soa.get('serial', '?')}[/cyan]"
                f"    [bold]Primary NS:[/bold]  [cyan]{self._soa.get('mname', '?')}[/cyan]"
                f"    [bold]Refresh:[/bold]  [cyan]{self._soa.get('refresh', '?')}s[/cyan]"
            )

        zone_info.update("\n".join(lines))

    def _update_controls(self):
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

    def _get_filtered_records(self):
        records = self._records

        # Apply type filter
        if self._filter_index > 0:
            filter_type = FILTER_TYPES[self._filter_index]
            records = [r for r in records if r.rtype == filter_type]

        # Apply sort
        sort_field = SORT_FIELDS[self._sort_index]
        if sort_field == "ttl":
            records = sorted(records, key=lambda r: r.ttl, reverse=self._sort_reverse)
        else:
            records = sorted(
                records,
                key=lambda r: getattr(r, sort_field, "").lower(),
                reverse=self._sort_reverse,
            )

        return records

    def _populate_table(self):
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
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self):
        self.app.pop_screen()

    def action_cycle_sort(self):
        if self._sort_index == len(SORT_FIELDS) - 1 and not self._sort_reverse:
            self._sort_reverse = True
        elif self._sort_reverse:
            self._sort_reverse = False
            self._sort_index = (self._sort_index + 1) % len(SORT_FIELDS)
        else:
            self._sort_index = (self._sort_index + 1) % len(SORT_FIELDS)

        self._populate_table()

    def action_cycle_filter(self):
        self._filter_index = (self._filter_index + 1) % len(FILTER_TYPES)
        self._populate_table()

    def action_refresh(self):
        zone_info = self.query_one("#dns-zone-info", Static)
        zone_info.update("[dim]Refreshing...[/dim]")
        self.load_dns_data()
