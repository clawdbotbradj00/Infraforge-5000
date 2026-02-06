"""Template list screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, DataTable, TabbedContent, TabPane
from textual.containers import Container, Horizontal
from textual import work

from rich.text import Text

from infraforge.models import Template, TemplateType


# Sort definitions per tab
VM_SORT_FIELDS = ["vmid", "name", "node", "size"]
VM_SORT_LABELS = ["VMID", "Name", "Node", "Disk"]

CT_SORT_FIELDS = ["name", "storage", "node", "size"]
CT_SORT_LABELS = ["Name", "Storage", "Node", "Size"]

ISO_SORT_FIELDS = ["name", "storage", "node", "size"]
ISO_SORT_LABELS = ["Name", "Storage", "Node", "Size"]

# Group definitions per tab
VM_GROUP_MODES = ["none", "node"]
VM_GROUP_LABELS = ["No Grouping", "By Node"]

CT_GROUP_MODES = ["none", "node", "storage"]
CT_GROUP_LABELS = ["No Grouping", "By Node", "By Storage"]

ISO_GROUP_MODES = ["none", "node", "storage"]
ISO_GROUP_LABELS = ["No Grouping", "By Node", "By Storage"]


def _field_index(fields: list[str], name: str, default: int = 0) -> int:
    try:
        return fields.index(name)
    except ValueError:
        return default


def _sort_templates(templates: list[Template], field: str, reverse: bool) -> list[Template]:
    """Sort templates by the given field."""
    def key_fn(t: Template):
        if field == "vmid":
            return t.vmid or 0
        elif field == "name":
            return t.name.lower()
        elif field == "node":
            return t.node.lower()
        elif field == "storage":
            return t.storage.lower()
        elif field == "size":
            return t.size
        return ""
    return sorted(templates, key=key_fn, reverse=reverse)


def _group_key(t: Template, mode: str) -> str:
    if mode == "node":
        return t.node or "(unknown)"
    elif mode == "storage":
        return t.storage or "(unknown)"
    return ""


class TemplateListScreen(Screen):
    """Screen for browsing templates."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("g", "cycle_group", "Group", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._vm_templates: list[Template] = []
        self._ct_templates: list[Template] = []
        self._iso_images: list[Template] = []
        self._data_loaded = False
        # Per-tab sort state
        self._sort_indices = {"vm": 0, "ct": 0, "iso": 0}
        self._sort_reverse = {"vm": False, "ct": False, "iso": False}
        # Per-tab group state
        self._group_indices = {"vm": 0, "ct": 0, "iso": 0}

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="template-container"):
            yield Static("", id="template-banner", markup=True)
            with Horizontal(id="vm-controls"):
                yield Static("", id="template-sort-label")
                yield Static("", id="template-group-label")
            with TabbedContent(id="template-tabs"):
                with TabPane("VM Templates", id="tab-vm"):
                    yield DataTable(id="vm-template-table")
                with TabPane("Container Templates", id="tab-ct"):
                    yield DataTable(id="ct-template-table")
                with TabPane("ISO Images", id="tab-iso"):
                    yield DataTable(id="iso-table")
        yield Footer()

    def _active_tab(self) -> str:
        tc = self.query_one("#template-tabs", TabbedContent)
        active = tc.active
        if active == "tab-ct":
            return "ct"
        elif active == "tab-iso":
            return "iso"
        return "vm"

    def on_mount(self):
        vm_table = self.query_one("#vm-template-table", DataTable)
        vm_table.cursor_type = "row"
        vm_table.zebra_stripes = True
        vm_table.add_columns("VMID", "Name", "Node", "Disk")

        ct_table = self.query_one("#ct-template-table", DataTable)
        ct_table.cursor_type = "row"
        ct_table.zebra_stripes = True
        ct_table.add_columns("Name", "Storage", "Node", "Size")

        iso_table = self.query_one("#iso-table", DataTable)
        iso_table.cursor_type = "row"
        iso_table.zebra_stripes = True
        iso_table.add_columns("Name", "Storage", "Node", "Size")

        # Restore saved preferences
        tl_prefs = self.app.preferences.template_list
        for tab_key, sort_fields, group_modes in [
            ("vm", VM_SORT_FIELDS, VM_GROUP_MODES),
            ("ct", CT_SORT_FIELDS, CT_GROUP_MODES),
            ("iso", ISO_SORT_FIELDS, ISO_GROUP_MODES),
        ]:
            tab_prefs = getattr(tl_prefs, tab_key)
            self._sort_indices[tab_key] = _field_index(sort_fields, tab_prefs.sort_field)
            self._sort_reverse[tab_key] = tab_prefs.sort_reverse
            self._group_indices[tab_key] = _field_index(group_modes, tab_prefs.group_mode)

        self.query_one("#template-banner", Static).update(
            "  [bold yellow]Loading templates...[/bold yellow]"
        )
        self._update_controls()
        self.load_templates()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._update_controls()

    def _update_controls(self):
        tab = self._active_tab()
        if tab == "vm":
            sort_labels = VM_SORT_LABELS
            group_labels = VM_GROUP_LABELS
        elif tab == "ct":
            sort_labels = CT_SORT_LABELS
            group_labels = CT_GROUP_LABELS
        else:
            sort_labels = ISO_SORT_LABELS
            group_labels = ISO_GROUP_LABELS

        s_idx = self._sort_indices[tab]
        s_rev = self._sort_reverse[tab]
        arrow = "▼" if s_rev else "▲"
        g_idx = self._group_indices[tab]

        self.query_one("#template-sort-label", Static).update(
            f"[bold]Sort:[/bold] [cyan]{sort_labels[s_idx]}[/cyan] {arrow}"
        )
        self.query_one("#template-group-label", Static).update(
            f"[bold]Group:[/bold] [magenta]{group_labels[g_idx]}[/magenta]"
        )

    @work(thread=True)
    def load_templates(self):
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_vm = pool.submit(self.app.proxmox.get_vm_templates)
                fut_dl = pool.submit(self.app.proxmox.get_downloaded_templates)

                vm_templates = fut_vm.result()
                downloaded = fut_dl.result()

            ct_templates = [t for t in downloaded if t.template_type == TemplateType.CONTAINER]
            iso_images = [t for t in downloaded if t.template_type == TemplateType.ISO]

            self._vm_templates = vm_templates
            self._ct_templates = ct_templates
            self._iso_images = iso_images
            self._data_loaded = True

            self.app.call_from_thread(self._populate_all_tables)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    def _populate_all_tables(self):
        self._populate_vm_table()
        self._populate_ct_table()
        self._populate_iso_table()
        self._update_banner()
        self._update_controls()

    def _update_banner(self):
        vm_count = len(self._vm_templates)
        ct_count = len(self._ct_templates)
        iso_count = len(self._iso_images)

        self.query_one("#template-banner", Static).update(
            f"  [bold]Templates & Images[/bold]  [dim]|[/dim]  "
            f"[bold cyan]{vm_count}[/bold cyan] VM templates  [dim]|[/dim]  "
            f"[bold magenta]{ct_count}[/bold magenta] CT templates  [dim]|[/dim]  "
            f"[bold yellow]{iso_count}[/bold yellow] ISOs"
        )

    # ------------------------------------------------------------------
    # Table population helpers
    # ------------------------------------------------------------------

    def _add_group_header(self, table: DataTable, label: str, count: int, ncols: int):
        header = Text(f" {label} ({count}) ", style="bold bright_white on dark_blue")
        empties = [Text("")] * (ncols - 1)
        table.add_row(header, *empties, key=f"group_{label}_{id(table)}")

    def _populate_vm_table(self):
        table = self.query_one("#vm-template-table", DataTable)
        table.clear()
        field = VM_SORT_FIELDS[self._sort_indices["vm"]]
        reverse = self._sort_reverse["vm"]
        group_mode = VM_GROUP_MODES[self._group_indices["vm"]]
        templates = _sort_templates(self._vm_templates, field, reverse)

        if not templates:
            table.add_row("—", "No VM templates found", "", "", key="empty_vm")
            return

        if group_mode != "none":
            grouped: dict[str, list[Template]] = {}
            for t in templates:
                k = _group_key(t, group_mode)
                grouped.setdefault(k, []).append(t)
            for gname, group in sorted(grouped.items()):
                self._add_group_header(table, gname, len(group), 4)
                for t in group:
                    self._add_vm_row(table, t)
        else:
            for t in templates:
                self._add_vm_row(table, t)

    def _add_vm_row(self, table: DataTable, t: Template):
        table.add_row(
            Text(str(t.vmid or "—"), style="bold"),
            Text(t.name, style="bold bright_white"),
            Text(t.node, style="cyan"),
            Text(t.size_display, style="green" if t.size > 0 else "dim"),
            key=f"vmt_{t.vmid}_{t.node}",
        )

    def _populate_ct_table(self):
        table = self.query_one("#ct-template-table", DataTable)
        table.clear()
        field = CT_SORT_FIELDS[self._sort_indices["ct"]]
        reverse = self._sort_reverse["ct"]
        group_mode = CT_GROUP_MODES[self._group_indices["ct"]]
        templates = _sort_templates(self._ct_templates, field, reverse)

        if not templates:
            table.add_row("No downloaded container templates", "", "", "", key="empty_ct")
            return

        if group_mode != "none":
            grouped: dict[str, list[Template]] = {}
            for t in templates:
                k = _group_key(t, group_mode)
                grouped.setdefault(k, []).append(t)
            for gname, group in sorted(grouped.items()):
                self._add_group_header(table, gname, len(group), 4)
                for i, t in enumerate(group):
                    self._add_ct_row(table, t, i)
        else:
            for i, t in enumerate(templates):
                self._add_ct_row(table, t, i)

    def _add_ct_row(self, table: DataTable, t: Template, idx: int):
        table.add_row(
            Text(t.name, style="bold bright_magenta"),
            Text(t.storage, style="yellow"),
            Text(t.node, style="cyan"),
            Text(t.size_display, style="green" if t.size > 0 else "dim"),
            key=f"ct_{idx}_{t.node}_{t.storage}",
        )

    def _populate_iso_table(self):
        table = self.query_one("#iso-table", DataTable)
        table.clear()
        field = ISO_SORT_FIELDS[self._sort_indices["iso"]]
        reverse = self._sort_reverse["iso"]
        group_mode = ISO_GROUP_MODES[self._group_indices["iso"]]
        templates = _sort_templates(self._iso_images, field, reverse)

        if not templates:
            table.add_row("No ISO images found", "", "", "", key="empty_iso")
            return

        if group_mode != "none":
            grouped: dict[str, list[Template]] = {}
            for t in templates:
                k = _group_key(t, group_mode)
                grouped.setdefault(k, []).append(t)
            for gname, group in sorted(grouped.items()):
                self._add_group_header(table, gname, len(group), 4)
                for i, t in enumerate(group):
                    self._add_iso_row(table, t, i)
        else:
            for i, t in enumerate(templates):
                self._add_iso_row(table, t, i)

    def _add_iso_row(self, table: DataTable, t: Template, idx: int):
        name = t.name
        if name.endswith(".iso"):
            name_style = "bold bright_yellow"
        elif name.endswith(".img"):
            name_style = "bold bright_blue"
        else:
            name_style = "bold"
        table.add_row(
            Text(name, style=name_style),
            Text(t.storage, style="yellow"),
            Text(t.node, style="cyan"),
            Text(t.size_display, style="green" if t.size > 0 else "dim"),
            key=f"iso_{idx}_{t.node}_{t.storage}",
        )

    def _show_error(self, error: str):
        self.query_one("#template-banner", Static).update(
            f"  [bold red]Error loading templates: {error}[/bold red]"
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _save_preferences(self) -> None:
        tl_prefs = self.app.preferences.template_list
        for tab_key, sort_fields, group_modes in [
            ("vm", VM_SORT_FIELDS, VM_GROUP_MODES),
            ("ct", CT_SORT_FIELDS, CT_GROUP_MODES),
            ("iso", ISO_SORT_FIELDS, ISO_GROUP_MODES),
        ]:
            tab_prefs = getattr(tl_prefs, tab_key)
            tab_prefs.sort_field = sort_fields[self._sort_indices[tab_key]]
            tab_prefs.sort_reverse = self._sort_reverse[tab_key]
            tab_prefs.group_mode = group_modes[self._group_indices[tab_key]]
        self.app.preferences.save()

    def action_cycle_sort(self):
        tab = self._active_tab()
        if tab == "vm":
            fields = VM_SORT_FIELDS
        elif tab == "ct":
            fields = CT_SORT_FIELDS
        else:
            fields = ISO_SORT_FIELDS

        old_idx = self._sort_indices[tab]
        new_idx = (old_idx + 1) % len(fields)
        if new_idx == old_idx:
            self._sort_reverse[tab] = not self._sort_reverse[tab]
        else:
            self._sort_reverse[tab] = False
        self._sort_indices[tab] = new_idx
        self._repopulate_active()
        self._save_preferences()

    def action_cycle_group(self):
        tab = self._active_tab()
        if tab == "vm":
            modes = VM_GROUP_MODES
        elif tab == "ct":
            modes = CT_GROUP_MODES
        else:
            modes = ISO_GROUP_MODES

        self._group_indices[tab] = (self._group_indices[tab] + 1) % len(modes)
        self._repopulate_active()
        self._save_preferences()

    def _repopulate_active(self):
        tab = self._active_tab()
        if tab == "vm":
            self._populate_vm_table()
        elif tab == "ct":
            self._populate_ct_table()
        else:
            self._populate_iso_table()
        self._update_controls()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        row_key = str(event.row_key.value) if event.row_key else ""

        if row_key.startswith("empty_") or row_key.startswith("group_"):
            return

        template = None

        if row_key.startswith("vmt_"):
            parts = row_key.split("_", 2)
            if len(parts) >= 3:
                vmid = int(parts[1])
                template = next((t for t in self._vm_templates if t.vmid == vmid), None)

        elif row_key.startswith("ct_"):
            parts = row_key.split("_", 2)
            if len(parts) >= 3:
                idx = int(parts[1])
                if 0 <= idx < len(self._ct_templates):
                    template = self._ct_templates[idx]

        elif row_key.startswith("iso_"):
            parts = row_key.split("_", 2)
            if len(parts) >= 3:
                idx = int(parts[1])
                if 0 <= idx < len(self._iso_images):
                    template = self._iso_images[idx]

        if template:
            from infraforge.screens.template_detail import TemplateDetailScreen
            self.app.push_screen(TemplateDetailScreen(template))

    def action_go_back(self):
        self.app.pop_screen()

    def action_refresh(self):
        self.query_one("#template-banner", Static).update(
            "  [bold yellow]Refreshing...[/bold yellow]"
        )
        self.load_templates()
