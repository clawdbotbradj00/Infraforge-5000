"""Template list screen for InfraForge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Tree
from textual.widgets._tree import TreeNode
from textual.containers import Container, Horizontal
from textual import work

from rich.text import Text

from infraforge.models import Template, TemplateType


SORT_FIELDS = ["name", "node", "storage", "size"]
SORT_LABELS = ["Name", "Node", "Storage", "Size"]

TYPE_COLORS = {
    TemplateType.VM: "cyan",
    TemplateType.CONTAINER: "magenta",
    TemplateType.ISO: "yellow",
}

TEMPLATE_HINTS = {
    TemplateType.VM: "QEMU virtual machine marked as a template for cloning.",
    TemplateType.CONTAINER: "LXC container template downloaded via pveam.",
    TemplateType.ISO: "ISO image stored on the cluster for VM installation.",
}


@dataclass
class TemplateNodeData:
    """Data attached to each node in the template tree."""
    kind: Literal["category", "template", "placeholder"]
    record: Template | None = None
    category: str = ""  # "vm", "ct", "iso"


def _sort_templates(templates: list[Template], field: str, reverse: bool) -> list[Template]:
    """Sort templates by the given field."""
    def key_fn(t: Template):
        if field == "name":
            return t.name.lower()
        elif field == "node":
            return t.node.lower()
        elif field == "storage":
            return t.storage.lower()
        elif field == "size":
            return t.size
        return ""
    return sorted(templates, key=key_fn, reverse=reverse)


_NAME_WIDTH = 44


def _truncate(name: str, width: int = _NAME_WIDTH) -> str:
    """Truncate name to width, adding '..' if it overflows."""
    if len(name) <= width:
        return name.ljust(width)
    return name[: width - 2] + ".."


def _make_vm_label(t: Template) -> Text:
    """Build aligned label for a VM template leaf."""
    vmid_col = str(t.vmid or "—").ljust(8)
    name_col = _truncate(t.name)
    node_col = t.node.ljust(14)
    size_col = t.size_display
    label = Text()
    label.append(vmid_col, style="bold")
    label.append(name_col, style="bold bright_white")
    label.append("    ", style="default")
    label.append(node_col, style="cyan")
    label.append(size_col, style="green" if t.size > 0 else "dim")
    return label


def _make_ct_label(t: Template) -> Text:
    """Build aligned label for a container template leaf."""
    name_col = _truncate(t.name)
    storage_col = t.storage.ljust(14)
    node_col = t.node.ljust(14)
    size_col = t.size_display
    label = Text()
    label.append(name_col, style="bold bright_magenta")
    label.append("    ", style="default")
    label.append(storage_col, style="yellow")
    label.append(node_col, style="cyan")
    label.append(size_col, style="green" if t.size > 0 else "dim")
    return label


def _make_iso_label(t: Template) -> Text:
    """Build aligned label for an ISO image leaf."""
    name = t.name
    if name.endswith(".iso"):
        name_style = "bold bright_yellow"
    elif name.endswith(".img"):
        name_style = "bold bright_blue"
    else:
        name_style = "bold"
    name_col = _truncate(name)
    storage_col = t.storage.ljust(14)
    node_col = t.node.ljust(14)
    size_col = t.size_display
    label = Text()
    label.append(name_col, style=name_style)
    label.append("    ", style="default")
    label.append(storage_col, style="yellow")
    label.append(node_col, style="cyan")
    label.append(size_col, style="green" if t.size > 0 else "dim")
    return label


class TemplateListScreen(Screen):
    """Screen for browsing templates in a tree layout."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._vm_templates: list[Template] = []
        self._ct_templates: list[Template] = []
        self._iso_images: list[Template] = []
        self._data_loaded = False
        self._sort_index: int = 0
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="template-container"):
            yield Static("Templates & Images", classes="section-title")
            yield Static("", id="template-banner", markup=True)
            with Horizontal(id="template-controls"):
                yield Static("", id="template-sort-label")
                yield Static("", id="template-count-label")
            with Horizontal(id="template-main-content"):
                yield Tree("Templates", id="template-tree")
                with Container(id="template-detail-panel"):
                    yield Static("[bold]Details[/bold]", id="template-detail-title", markup=True)
                    yield Static(
                        "[dim]Select a template to view details.[/dim]",
                        id="template-detail-content",
                        markup=True,
                    )
            yield Static("", id="template-status-bar", markup=True)
        yield Footer()

    def on_mount(self):
        prefs = self.app.preferences.template_list
        # Use VM tab prefs for global sort (simplify from per-tab)
        sf = prefs.vm.sort_field
        try:
            self._sort_index = SORT_FIELDS.index(sf)
        except ValueError:
            self._sort_index = 0
        self._sort_reverse = prefs.vm.sort_reverse
        self._update_controls()
        self.query_one("#template-banner", Static).update(
            "[bold yellow]Loading templates...[/bold yellow]"
        )
        self.load_templates()

    def _update_controls(self):
        arrow = "▼" if self._sort_reverse else "▲"
        self.query_one("#template-sort-label", Static).update(
            f"[bold]Sort:[/bold] [cyan]{SORT_LABELS[self._sort_index]}[/cyan] {arrow}"
        )
        total = len(self._vm_templates) + len(self._ct_templates) + len(self._iso_images)
        self.query_one("#template-count-label", Static).update(
            f"[dim]{total} items[/dim]"
        )

    @work(thread=True)
    def load_templates(self):
        try:
            from concurrent.futures import ThreadPoolExecutor

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

            self.app.call_from_thread(self._build_tree)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    def _build_tree(self):
        """Build the template tree with category parent nodes."""
        tree = self.query_one("#template-tree", Tree)

        # Remember expansion state
        expanded: set[str] = set()
        for node in tree.root.children:
            if node.data and node.data.kind == "category" and node.is_expanded:
                expanded.add(node.data.category)

        tree.clear()

        field = SORT_FIELDS[self._sort_index]
        reverse = self._sort_reverse

        categories = [
            ("vm", f"VM Templates  [{len(self._vm_templates)}]",
             _sort_templates(self._vm_templates, field, reverse), _make_vm_label),
            ("ct", f"CT Templates  [{len(self._ct_templates)}]",
             _sort_templates(self._ct_templates, field, reverse), _make_ct_label),
            ("iso", f"ISO Images  [{len(self._iso_images)}]",
             _sort_templates(self._iso_images, field, reverse), _make_iso_label),
        ]

        first = True
        for cat_key, cat_label, templates, label_fn in categories:
            if not first:
                tree.root.add_leaf(Text(""), data=TemplateNodeData(kind="placeholder"))
            first = False

            cat_data = TemplateNodeData(kind="category", category=cat_key)
            color = TYPE_COLORS.get(
                {"vm": TemplateType.VM, "ct": TemplateType.CONTAINER, "iso": TemplateType.ISO}[cat_key],
                "white",
            )
            cat_text = Text()
            cat_text.append(cat_label, style=f"bold {color}")
            cat_node = tree.root.add(cat_text, data=cat_data)

            if templates:
                for t in templates:
                    tpl_data = TemplateNodeData(kind="template", record=t, category=cat_key)
                    cat_node.add_leaf(label_fn(t), data=tpl_data)
            else:
                cat_node.add_leaf(
                    Text("(none)", style="dim italic"),
                    data=TemplateNodeData(kind="placeholder"),
                )

            # Re-expand or auto-expand on first load
            if cat_key in expanded or not expanded:
                cat_node.expand()

        self._update_banner()
        self._update_controls()

    def _update_banner(self):
        vm_count = len(self._vm_templates)
        ct_count = len(self._ct_templates)
        iso_count = len(self._iso_images)
        self.query_one("#template-banner", Static).update(
            f"[bold cyan]{vm_count}[/bold cyan] VM templates  [dim]|[/dim]  "
            f"[bold magenta]{ct_count}[/bold magenta] CT templates  [dim]|[/dim]  "
            f"[bold yellow]{iso_count}[/bold yellow] ISOs"
        )

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, "kind"):
            self._clear_detail_panel()
            return

        if node.data.kind == "category":
            self._show_category_detail(node.data.category)
        elif node.data.kind == "template" and node.data.record:
            self._show_template_detail(node.data.record)
        else:
            self._clear_detail_panel()

    def _show_category_detail(self, category: str) -> None:
        detail = self.query_one("#template-detail-content", Static)
        title = self.query_one("#template-detail-title", Static)

        if category == "vm":
            title.update("[bold]VM Templates[/bold]")
            templates = self._vm_templates
            ttype = TemplateType.VM
        elif category == "ct":
            title.update("[bold]CT Templates[/bold]")
            templates = self._ct_templates
            ttype = TemplateType.CONTAINER
        else:
            title.update("[bold]ISO Images[/bold]")
            templates = self._iso_images
            ttype = TemplateType.ISO

        nodes = sorted(set(t.node for t in templates))
        storages = sorted(set(t.storage for t in templates if t.storage))

        lines = [
            f"[bold]Count:[/bold]     [cyan]{len(templates)}[/cyan]",
        ]
        if nodes:
            lines.append(f"[bold]Nodes:[/bold]     {', '.join(nodes)}")
        if storages:
            lines.append(f"[bold]Storage:[/bold]   {', '.join(storages)}")

        hint = TEMPLATE_HINTS.get(ttype, "")
        if hint:
            lines.append("")
            lines.append(f"[dim italic]{hint}[/dim italic]")

        detail.update("\n".join(lines))

    def _show_template_detail(self, t: Template) -> None:
        detail = self.query_one("#template-detail-content", Static)
        title = self.query_one("#template-detail-title", Static)

        color = TYPE_COLORS.get(t.template_type, "white")
        title.update(f"[bold]{t.type_label}[/bold]")

        lines = []

        if t.template_type == TemplateType.VM:
            if t.vmid:
                lines.append(f"[bold]VMID:[/bold]        {t.vmid}")
            lines.append(f"[bold]Name:[/bold]        {t.name}")
            lines.append(f"[bold]Node:[/bold]        [{color}]{t.node}[/{color}]")
            lines.append(f"[bold]Disk Size:[/bold]   {t.size_display}")
            if t.description and t.description != t.name:
                lines.append(f"[bold]Description:[/bold] {t.description}")

        elif t.template_type == TemplateType.CONTAINER:
            lines.append(f"[bold]Name:[/bold]        {t.name}")
            if t.volid:
                lines.append(f"[bold]Volume ID:[/bold]  {t.volid}")
            lines.append(f"[bold]Storage:[/bold]     [{color}]{t.storage}[/{color}]")
            lines.append(f"[bold]Node:[/bold]        {t.node}")
            lines.append(f"[bold]Size:[/bold]        {t.size_display}")
            if t.package:
                lines.append(f"[bold]Package:[/bold]     {t.package}")
            if t.os:
                lines.append(f"[bold]OS:[/bold]          {t.os}")
            if t.version:
                lines.append(f"[bold]Version:[/bold]     {t.version}")
            if t.headline:
                lines.append(f"[bold]Summary:[/bold]     {t.headline}")

        elif t.template_type == TemplateType.ISO:
            lines.append(f"[bold]Name:[/bold]        {t.name}")
            if t.volid:
                lines.append(f"[bold]Volume ID:[/bold]  {t.volid}")
            lines.append(f"[bold]Storage:[/bold]     [{color}]{t.storage}[/{color}]")
            lines.append(f"[bold]Node:[/bold]        {t.node}")
            lines.append(f"[bold]Size:[/bold]        {t.size_display}")

        hint = TEMPLATE_HINTS.get(t.template_type, "")
        if hint:
            lines.append("")
            lines.append(f"[dim italic]{hint}[/dim italic]")

        detail.update("\n".join(lines))

    def _clear_detail_panel(self) -> None:
        self.query_one("#template-detail-title", Static).update("[bold]Details[/bold]")
        self.query_one("#template-detail-content", Static).update(
            "[dim]Select a template to view details.[/dim]"
        )

    def _show_error(self, error: str):
        self.query_one("#template-banner", Static).update(
            f"[bold red]Error loading templates: {error}[/bold red]"
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _save_preferences(self) -> None:
        prefs = self.app.preferences.template_list
        prefs.vm.sort_field = SORT_FIELDS[self._sort_index]
        prefs.vm.sort_reverse = self._sort_reverse
        self.app.preferences.save()

    def action_cycle_sort(self):
        old_idx = self._sort_index
        new_idx = (old_idx + 1) % len(SORT_FIELDS)
        if new_idx == old_idx:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_reverse = False
        self._sort_index = new_idx
        if self._data_loaded:
            self._build_tree()
        self._save_preferences()

    def action_go_back(self):
        self.app.pop_screen()

    def action_refresh(self):
        self.query_one("#template-banner", Static).update(
            "[bold yellow]Refreshing...[/bold yellow]"
        )
        self.load_templates()
