"""phpIPAM management screen for InfraForge.

Provides a full IPAM management interface with three views (Subnets,
Addresses, VLANs), sort/filter controls, and CRUD operations for
IP address reservation, subnet scanning, and VLAN management.

CSS additions needed in styles/app.tcss:
------------------------------------------------------------------------
#ipam-container {
    padding: 1 2;
}

#ipam-overview {
    margin: 0 0 1 0;
    border: round $primary-background;
    padding: 1 2;
}

#ipam-tab-bar {
    layout: horizontal;
    height: 3;
    margin: 0 0 0 0;
    border: round $primary-background;
    padding: 0 1;
}

.ipam-tab-btn {
    width: auto;
    padding: 1 2;
    color: $text-muted;
    text-style: italic;
}

.ipam-tab-btn.-active {
    color: $accent;
    text-style: bold;
    background: $primary-background;
}

#ipam-controls {
    layout: horizontal;
    height: 3;
    margin: 0 0 0 0;
    border: round $primary-background;
    padding: 0 1;
}

#ipam-filter-label {
    width: auto;
    padding: 1 1;
    color: $text;
}

#ipam-sort-label {
    width: auto;
    padding: 1 1;
    color: $text;
    margin: 0 0 0 2;
}

#ipam-count-label {
    width: 1fr;
    padding: 1 1;
    color: $text-muted;
    text-align: right;
}

#ipam-table {
    height: 1fr;
    margin: 1 0 0 0;
}

#ipam-status-bar {
    height: 1;
    margin: 1 0 0 0;
    background: $primary-background;
    color: $text-muted;
    padding: 0 1;
}
------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Optional

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

# View tabs
VIEW_SUBNETS = "subnets"
VIEW_ADDRESSES = "addresses"
VIEW_VLANS = "vlans"
VIEWS = [VIEW_SUBNETS, VIEW_ADDRESSES, VIEW_VLANS]
VIEW_LABELS = ["Subnets", "Addresses", "VLANs"]

# Sort fields per view
SUBNET_SORT_FIELDS = ["subnet", "description", "vlan", "usage"]
SUBNET_SORT_LABELS = ["Subnet", "Description", "VLAN", "Usage %"]

ADDRESS_SORT_FIELDS = ["ip", "hostname", "status", "description", "last_seen"]
ADDRESS_SORT_LABELS = ["IP", "Hostname", "Status", "Description", "Last Seen"]

VLAN_SORT_FIELDS = ["number", "name", "description"]
VLAN_SORT_LABELS = ["Number", "Name", "Description"]

# Filter modes per view
SUBNET_FILTER_MODES = ["all", "low", "medium", "high"]
SUBNET_FILTER_LABELS = ["All", "Low (<60%)", "Medium (60-80%)", "High (>80%)"]

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
    "Active": "green",
    "Reserved": "yellow",
    "Offline": "red",
    "DHCP": "cyan",
    "Unknown": "bright_black",
}

# phpIPAM tag values for create_address
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
# Modal: Address input screen (reserve / edit)
# ---------------------------------------------------------------------------

class AddressInputScreen(ModalScreen[Optional[dict]]):
    """Modal screen for reserving or editing an IP address.

    On dismiss, returns a dict with keys: ip, hostname, description, tag
    or None if cancelled.
    """

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
        Binding("enter", "select_row", "Select", show=False),
        Binding("x", "scan_subnet", "Scan", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # View state
        self._active_view: str = VIEW_SUBNETS
        self._loading: bool = False
        self._ipam_healthy: bool = False

        # Data stores
        self._subnets: list[dict] = []
        self._addresses: list[dict] = []
        self._vlans: list[dict] = []

        # Current subnet context for address view
        self._selected_subnet: dict | None = None

        # Sort / filter state per view
        self._subnet_sort_index: int = 0
        self._subnet_sort_reverse: bool = False
        self._subnet_filter_index: int = 0

        self._address_sort_index: int = 0
        self._address_sort_reverse: bool = False
        self._address_filter_index: int = 0

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

            # Data table
            yield DataTable(id="ipam-table", cursor_type="row")

            # Status bar
            yield Static("", id="ipam-status-bar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ipam-table", DataTable)
        table.zebra_stripes = True

        self._render_tab_bar()
        self._setup_table_columns()

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
        """Rebuild the tab selector bar."""
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
    # Table column setup
    # ------------------------------------------------------------------

    def _setup_table_columns(self) -> None:
        """Set up DataTable columns for the active view."""
        table = self.query_one("#ipam-table", DataTable)
        table.clear(columns=True)

        if self._active_view == VIEW_SUBNETS:
            table.add_columns("Subnet", "Description", "VLAN", "Usage %", "Used", "Total")
        elif self._active_view == VIEW_ADDRESSES:
            table.add_columns("IP Address", "Hostname", "Status", "Description", "Last Seen")
        elif self._active_view == VIEW_VLANS:
            table.add_columns("Number", "Name", "Description", "ID")

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self) -> None:
        """Load overview data and the default view (subnets)."""
        self._loading = True
        self.app.call_from_thread(
            self._set_status, "Connecting to phpIPAM...",
        )

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)

            # Health check
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

            # Load subnets
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

            # Load VLANs in the background too
            try:
                vlans = client.get_vlans()
                self._vlans = vlans if isinstance(vlans, list) else []
            except IPAMError:
                self._vlans = []

            self.app.call_from_thread(self._update_overview)
            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {len(self._subnets)} subnets loaded",
            )

        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))
        finally:
            self._loading = False

    @work(thread=True)
    def _load_subnets(self) -> None:
        """Reload subnets from phpIPAM."""
        self._loading = True
        self.app.call_from_thread(self._set_status, "Loading subnets...")

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            subnets = client.get_subnets()
            self._subnets = subnets if isinstance(subnets, list) else []

            self.app.call_from_thread(self._update_overview)
            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {len(self._subnets)} subnets loaded",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load subnets: {exc}[/red]",
            )
        finally:
            self._loading = False

    @work(thread=True)
    def _load_addresses(self, subnet: dict) -> None:
        """Load addresses for a specific subnet."""
        self._loading = True
        subnet_id = subnet.get("id", "")
        subnet_cidr = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
        self.app.call_from_thread(
            self._set_status, f"Loading addresses for {subnet_cidr}...",
        )

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            addresses = client.get_subnet_addresses(subnet_id)
            self._addresses = addresses if isinstance(addresses, list) else []

            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Connected[/green] | {subnet_cidr} | "
                f"{len(self._addresses)} addresses",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to load addresses: {exc}[/red]",
            )
        finally:
            self._loading = False

    @work(thread=True)
    def _load_vlans(self) -> None:
        """Reload VLANs from phpIPAM."""
        self._loading = True
        self.app.call_from_thread(self._set_status, "Loading VLANs...")

        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            vlans = client.get_vlans()
            self._vlans = vlans if isinstance(vlans, list) else []

            self.app.call_from_thread(self._populate_table)
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
        overview.update(f"[red]Error: {error}[/red]")
        self._set_status(f"[red]Error: {error}[/red]")

    def _set_status(self, text: str) -> None:
        self.query_one("#ipam-status-bar", Static).update(text)

    def _update_overview(self) -> None:
        """Update the overview panel with summary statistics."""
        overview = self.query_one("#ipam-overview", Static)
        ipam_cfg = self.app.config.ipam

        total_subnets = len(self._subnets)
        total_vlans = len(self._vlans)

        # Detect empty state — connected but nothing configured yet
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
            f"  1. Open [link={ipam_cfg.url}][bold cyan]{ipam_cfg.url}[/bold cyan][/link]\n"
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

        if self._active_view == VIEW_SUBNETS:
            current_filter = SUBNET_FILTER_LABELS[self._subnet_filter_index]
            current_sort = SUBNET_SORT_LABELS[self._subnet_sort_index]
            direction = " \u25bc" if self._subnet_sort_reverse else " \u25b2"
            filtered = self._get_filtered_subnets()
            total = len(self._subnets)
            shown = len(filtered)
        elif self._active_view == VIEW_ADDRESSES:
            current_filter = ADDRESS_FILTER_LABELS[self._address_filter_index]
            current_sort = ADDRESS_SORT_LABELS[self._address_sort_index]
            direction = " \u25bc" if self._address_sort_reverse else " \u25b2"
            filtered = self._get_filtered_addresses()
            total = len(self._addresses)
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

        # Apply usage filter
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

        # Apply sort
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

    def _get_filtered_addresses(self) -> list[dict]:
        addresses = list(self._addresses)

        mode = ADDRESS_FILTER_MODES[self._address_filter_index]
        if mode != "all":
            target = mode.capitalize()  # "Active", "Reserved", etc.
            addresses = [
                a for a in addresses
                if _addr_status_label(a) == target
            ]

        field = ADDRESS_SORT_FIELDS[self._address_sort_index]
        if field == "ip":
            addresses.sort(
                key=lambda a: _ip_sort_key(a.get("ip", "0.0.0.0")),
                reverse=self._address_sort_reverse,
            )
        elif field == "hostname":
            addresses.sort(
                key=lambda a: (a.get("hostname") or "").lower(),
                reverse=self._address_sort_reverse,
            )
        elif field == "status":
            addresses.sort(
                key=lambda a: _addr_status_label(a),
                reverse=self._address_sort_reverse,
            )
        elif field == "description":
            addresses.sort(
                key=lambda a: (a.get("description") or "").lower(),
                reverse=self._address_sort_reverse,
            )
        elif field == "last_seen":
            addresses.sort(
                key=lambda a: a.get("lastSeen") or "",
                reverse=self._address_sort_reverse,
            )

        return addresses

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
    # Table population
    # ------------------------------------------------------------------

    def _populate_table(self) -> None:
        """Populate the DataTable based on the active view."""
        table = self.query_one("#ipam-table", DataTable)
        table.clear()

        self._update_controls()

        if self._active_view == VIEW_SUBNETS:
            self._populate_subnets(table)
        elif self._active_view == VIEW_ADDRESSES:
            self._populate_addresses(table)
        elif self._active_view == VIEW_VLANS:
            self._populate_vlans(table)

    def _populate_subnets(self, table: DataTable) -> None:
        filtered = self._get_filtered_subnets()

        # Build a VLAN lookup from loaded VLANs
        vlan_map: dict[str, str] = {}
        for v in self._vlans:
            vid = str(v.get("id", ""))
            vnum = str(v.get("number", ""))
            vname = v.get("name", "")
            if vid:
                vlan_map[vid] = f"{vnum}" + (f" ({vname})" if vname else "")

        for subnet in filtered:
            subnet_str = f"{subnet.get('subnet', '?')}/{subnet.get('mask', '?')}"
            description = subnet.get("description") or ""
            vlan_id = str(subnet.get("vlanId") or "")
            vlan_display = vlan_map.get(vlan_id, vlan_id) if vlan_id and vlan_id != "0" else "-"

            usage = subnet.get("usage", {})
            used = _safe_int(usage.get("used", 0))
            maxhosts = _safe_int(usage.get("maxhosts", 0))

            if maxhosts > 0:
                pct = (used / maxhosts) * 100
            else:
                pct = 0.0

            util_color = _utilization_color(pct)

            subnet_text = Text(subnet_str, style="bold")
            desc_text = Text(description)
            vlan_text = Text(vlan_display)

            pct_text = Text(f"{pct:.1f}%")
            pct_text.stylize(util_color)

            used_text = Text(str(used))
            total_text = Text(str(maxhosts))

            table.add_row(
                subnet_text, desc_text, vlan_text,
                pct_text, used_text, total_text,
                key=f"subnet_{subnet.get('id', '')}",
            )

    def _populate_addresses(self, table: DataTable) -> None:
        filtered = self._get_filtered_addresses()

        for addr in filtered:
            ip = addr.get("ip", "?")
            hostname = addr.get("hostname") or ""
            status = _addr_status_label(addr)
            description = addr.get("description") or ""
            last_seen = addr.get("lastSeen") or "-"

            color = STATUS_COLORS.get(status, "bright_black")

            ip_text = Text(ip, style="bold")
            hostname_text = Text(hostname)
            status_text = Text(status)
            status_text.stylize(color)
            desc_text = Text(description)
            seen_text = Text(last_seen, style="dim")

            table.add_row(
                ip_text, hostname_text, status_text,
                desc_text, seen_text,
                key=f"addr_{addr.get('id', '')}",
            )

    def _populate_vlans(self, table: DataTable) -> None:
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

    # ------------------------------------------------------------------
    # Row selection helpers
    # ------------------------------------------------------------------

    def _get_selected_subnet(self) -> dict | None:
        """Return the subnet dict for the currently selected table row."""
        table = self.query_one("#ipam-table", DataTable)
        try:
            cursor_key = str(table.coordinate_to_cell_key(
                table.cursor_coordinate,
            ).row_key)
        except Exception:
            return None

        if not cursor_key.startswith("subnet_"):
            return None

        subnet_id = cursor_key[len("subnet_"):]
        for s in self._subnets:
            if str(s.get("id", "")) == subnet_id:
                return s
        return None

    def _get_selected_address(self) -> dict | None:
        """Return the address dict for the currently selected table row."""
        table = self.query_one("#ipam-table", DataTable)
        try:
            cursor_key = str(table.coordinate_to_cell_key(
                table.cursor_coordinate,
            ).row_key)
        except Exception:
            return None

        if not cursor_key.startswith("addr_"):
            return None

        addr_id = cursor_key[len("addr_"):]
        for a in self._addresses:
            if str(a.get("id", "")) == addr_id:
                return a
        return None

    def _get_selected_vlan(self) -> dict | None:
        """Return the VLAN dict for the currently selected table row."""
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
        """Go back: if in address view, return to subnets; else pop screen."""
        if self._active_view == VIEW_ADDRESSES:
            self._selected_subnet = None
            self._addresses = []
            self._active_view = VIEW_SUBNETS
            self._render_tab_bar()
            self._setup_table_columns()
            self._populate_table()
            self._set_status(
                f"[green]Connected[/green] | {len(self._subnets)} subnets",
            )
        else:
            self.app.pop_screen()

    # ------------------------------------------------------------------
    # Actions: Tab switching
    # ------------------------------------------------------------------

    def _switch_to_view(self, view: str) -> None:
        """Switch to a different view tab."""
        if view == self._active_view:
            return
        if view not in VIEWS:
            return

        self._active_view = view
        self._render_tab_bar()
        self._setup_table_columns()

        if view == VIEW_SUBNETS:
            self._selected_subnet = None
            self._addresses = []
            self._populate_table()
        elif view == VIEW_ADDRESSES:
            if self._selected_subnet:
                self._load_addresses(self._selected_subnet)
            else:
                # No subnet selected -- show empty with hint
                self._populate_table()
                self._set_status(
                    "[yellow]Select a subnet first (switch to Subnets tab and press Enter).[/yellow]"
                )
        elif view == VIEW_VLANS:
            if not self._vlans:
                self._load_vlans()
            else:
                self._populate_table()

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
        if self._active_view == VIEW_SUBNETS:
            fields = SUBNET_SORT_FIELDS
            if self._subnet_sort_index == len(fields) - 1 and not self._subnet_sort_reverse:
                self._subnet_sort_reverse = True
            elif self._subnet_sort_reverse:
                self._subnet_sort_reverse = False
                self._subnet_sort_index = (self._subnet_sort_index + 1) % len(fields)
            else:
                self._subnet_sort_index = (self._subnet_sort_index + 1) % len(fields)
        elif self._active_view == VIEW_ADDRESSES:
            fields = ADDRESS_SORT_FIELDS
            if self._address_sort_index == len(fields) - 1 and not self._address_sort_reverse:
                self._address_sort_reverse = True
            elif self._address_sort_reverse:
                self._address_sort_reverse = False
                self._address_sort_index = (self._address_sort_index + 1) % len(fields)
            else:
                self._address_sort_index = (self._address_sort_index + 1) % len(fields)
        elif self._active_view == VIEW_VLANS:
            fields = VLAN_SORT_FIELDS
            if self._vlan_sort_index == len(fields) - 1 and not self._vlan_sort_reverse:
                self._vlan_sort_reverse = True
            elif self._vlan_sort_reverse:
                self._vlan_sort_reverse = False
                self._vlan_sort_index = (self._vlan_sort_index + 1) % len(fields)
            else:
                self._vlan_sort_index = (self._vlan_sort_index + 1) % len(fields)

        self._populate_table()

    def action_cycle_filter(self) -> None:
        if self._active_view == VIEW_SUBNETS:
            self._subnet_filter_index = (
                (self._subnet_filter_index + 1) % len(SUBNET_FILTER_MODES)
            )
        elif self._active_view == VIEW_ADDRESSES:
            self._address_filter_index = (
                (self._address_filter_index + 1) % len(ADDRESS_FILTER_MODES)
            )
        elif self._active_view == VIEW_VLANS:
            self._vlan_filter_index = (
                (self._vlan_filter_index + 1) % len(VLAN_FILTER_MODES)
            )
        self._populate_table()

    def action_refresh(self) -> None:
        if self._loading:
            return
        self._set_status("[dim]Refreshing...[/dim]")

        if self._active_view == VIEW_SUBNETS:
            self._load_subnets()
        elif self._active_view == VIEW_ADDRESSES:
            if self._selected_subnet:
                self._load_addresses(self._selected_subnet)
        elif self._active_view == VIEW_VLANS:
            self._load_vlans()

    # ------------------------------------------------------------------
    # Actions: Row selection (Enter)
    # ------------------------------------------------------------------

    def action_select_row(self) -> None:
        """Handle Enter key -- drill into subnet or show address details."""
        if self._active_view == VIEW_SUBNETS:
            subnet = self._get_selected_subnet()
            if subnet is None:
                return
            # Switch to address view for this subnet
            self._selected_subnet = subnet
            self._active_view = VIEW_ADDRESSES
            self._render_tab_bar()
            self._setup_table_columns()
            self._load_addresses(subnet)

        elif self._active_view == VIEW_ADDRESSES:
            addr = self._get_selected_address()
            if addr is None:
                return
            # Show details in status bar
            status = _addr_status_label(addr)
            color = STATUS_COLORS.get(status, "bright_black")
            self._set_status(
                f"[bold]{addr.get('ip', '?')}[/bold]  "
                f"[{color}]{status}[/{color}]  "
                f"Host: {addr.get('hostname') or '-'}  "
                f"Desc: {addr.get('description') or '-'}  "
                f"MAC: {addr.get('mac') or '-'}  "
                f"Last Seen: {addr.get('lastSeen') or '-'}"
            )

        elif self._active_view == VIEW_VLANS:
            vlan = self._get_selected_vlan()
            if vlan is None:
                return
            self._set_status(
                f"VLAN [bold]{vlan.get('number', '?')}[/bold]  "
                f"Name: {vlan.get('name') or '-'}  "
                f"Desc: {vlan.get('description') or '-'}  "
                f"ID: {vlan.get('id', '?')}"
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a table row via the DataTable event."""
        self.action_select_row()

    # ------------------------------------------------------------------
    # Actions: CRUD -- Add
    # ------------------------------------------------------------------

    def action_add_item(self) -> None:
        """Add a new item in the current view."""
        if self._active_view == VIEW_ADDRESSES:
            self._add_address()
        elif self._active_view == VIEW_SUBNETS:
            self._set_status(
                "[yellow]Subnet creation is not yet available from this screen. "
                "Use the phpIPAM web UI.[/yellow]"
            )
        elif self._active_view == VIEW_VLANS:
            self._set_status(
                "[yellow]VLAN creation is not yet available from this screen. "
                "Use the phpIPAM web UI.[/yellow]"
            )

    def _add_address(self) -> None:
        """Open the address input modal to reserve an IP."""
        if not self._selected_subnet:
            self._set_status(
                "[yellow]Select a subnet first (press Escape and Enter on a subnet).[/yellow]"
            )
            return

        subnet = self._selected_subnet
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

        # Try to suggest the first free IP
        self._suggest_and_open_address_modal(subnet, subnet_cidr, _on_result)

    @work(thread=True)
    def _suggest_and_open_address_modal(
        self, subnet: dict, subnet_cidr: str, callback,
    ) -> None:
        """Fetch the first free IP and open the address modal."""
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
        """Reserve an IP address in phpIPAM."""
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
                subnet_id=subnet.get("id", ""),
                hostname=hostname,
                description=description,
                tag=tag_value,
            )

            # Reload addresses
            addresses = client.get_subnet_addresses(subnet.get("id", ""))
            self._addresses = addresses if isinstance(addresses, list) else []

            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Reserved {ip} ({tag_label})[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to reserve {ip}: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: CRUD -- Edit
    # ------------------------------------------------------------------

    def action_edit_item(self) -> None:
        """Edit the selected item."""
        if self._active_view == VIEW_ADDRESSES:
            self._edit_address()
        else:
            self._set_status("[yellow]Edit is only available in the Addresses view.[/yellow]")

    def _edit_address(self) -> None:
        """Open modal to edit an existing address (hostname/description/status)."""
        addr = self._get_selected_address()
        if addr is None:
            self._set_status("[yellow]No address selected.[/yellow]")
            return

        current_status = _addr_status_label(addr)
        subnet_cidr = ""
        if self._selected_subnet:
            subnet_cidr = (
                f"{self._selected_subnet.get('subnet', '?')}"
                f"/{self._selected_subnet.get('mask', '?')}"
            )

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
        """Update an address in phpIPAM."""
        addr_id = addr.get("id", "")
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

            # Reload addresses
            if self._selected_subnet:
                addresses = client.get_subnet_addresses(
                    self._selected_subnet.get("id", ""),
                )
                self._addresses = addresses if isinstance(addresses, list) else []

            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Updated {addr.get('ip', '?')}[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to update address: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: CRUD -- Delete (release)
    # ------------------------------------------------------------------

    def action_delete_item(self) -> None:
        """Delete/release the selected item."""
        if self._active_view == VIEW_ADDRESSES:
            self._release_address()
        else:
            self._set_status(
                "[yellow]Delete is only available in the Addresses view (release an IP).[/yellow]"
            )

    def _release_address(self) -> None:
        """Release (delete) an IP address with confirmation."""
        addr = self._get_selected_address()
        if addr is None:
            self._set_status("[yellow]No address selected.[/yellow]")
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
        """Delete an address from phpIPAM."""
        addr_id = addr.get("id", "")
        ip = addr.get("ip", "?")
        self.app.call_from_thread(
            self._set_status, f"Releasing {ip}...",
        )
        try:
            from infraforge.ipam_client import IPAMClient, IPAMError

            client = IPAMClient(self.app.config)
            client._delete(f"/addresses/{addr_id}/")

            # Reload addresses
            if self._selected_subnet:
                addresses = client.get_subnet_addresses(
                    self._selected_subnet.get("id", ""),
                )
                self._addresses = addresses if isinstance(addresses, list) else []

            self.app.call_from_thread(self._populate_table)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Released {ip}[/green]",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Failed to release {ip}: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Actions: Scan subnet
    # ------------------------------------------------------------------

    def action_scan_subnet(self) -> None:
        """Trigger subnet scanning on the selected subnet."""
        if self._active_view == VIEW_SUBNETS:
            subnet = self._get_selected_subnet()
        elif self._active_view == VIEW_ADDRESSES and self._selected_subnet:
            subnet = self._selected_subnet
        else:
            self._set_status(
                "[yellow]Select a subnet to scan (Subnets or Addresses view).[/yellow]"
            )
            return

        if subnet is None:
            self._set_status("[yellow]No subnet selected.[/yellow]")
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
        """Enable scanning on a subnet via the IPAM API."""
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
# Utility functions
# ---------------------------------------------------------------------------

def _safe_int(value) -> int:
    """Safely convert a value to int, returning 0 on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _subnet_usage_pct(subnet: dict) -> float:
    """Calculate usage percentage for a subnet."""
    usage = subnet.get("usage", {})
    used = _safe_int(usage.get("used", 0))
    maxhosts = _safe_int(usage.get("maxhosts", 0))
    if maxhosts <= 0:
        return 0.0
    return (used / maxhosts) * 100


def _utilization_color(pct: float) -> str:
    """Return a Rich color name based on utilization percentage."""
    if pct > 80:
        return "red"
    elif pct > 60:
        return "yellow"
    return "green"


def _addr_status_label(addr: dict) -> str:
    """Return a human-readable status label for an address dict."""
    tag = str(addr.get("tag", ""))
    return TAG_STATUS_MAP.get(tag, "Unknown")


def _ip_sort_key(ip_str: str) -> tuple:
    """Convert an IP address string to a sortable tuple of ints."""
    try:
        parts = ip_str.split(".")
        return tuple(int(p) for p in parts)
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)
