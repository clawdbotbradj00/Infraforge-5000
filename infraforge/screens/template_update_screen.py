"""Template Update Screen — clone, update via SSH, re-template workflow.

5-phase wizard:
  0. Select Template  — pick a QEMU VM template
  1. Configure        — staging VM settings (CPU, RAM, storage, network)
  2. Clone            — automated clone + boot (RichLog)
  3. Update           — wait for user to SSH and do updates
  4. Finalize         — stop, delete old template, convert to template (RichLog)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual import work

from infraforge.models import TemplateType


PHASE_NAMES = ["Select", "Configure", "Clone", "Update", "Finalize"]

SORT_FIELDS = ["name", "node", "vmid"]
SORT_LABELS = ["Name", "Node", "VMID"]
GROUP_MODES = ["none", "node"]
GROUP_LABELS = ["None", "Node"]

_VMID_W = 8
_NAME_W = 36
_NODE_W = 14

# Storage table column widths
_STOR_NAME_W = 18
_STOR_TYPE_W = 12
_STOR_FREE_W = 12
_STOR_TOTAL_W = 12
_STOR_USED_W = 10


def _stor_label(s) -> str:
    """Build a colored, columnar label for a storage pool."""
    name = (s.storage or "—")
    if len(name) > _STOR_NAME_W - 1:
        name = name[: _STOR_NAME_W - 2] + ".."
    name = name.ljust(_STOR_NAME_W)

    stype = (s.storage_type or "—").ljust(_STOR_TYPE_W)
    free = (s.avail_display or "—").ljust(_STOR_FREE_W)
    total = (s.total_display or "—").ljust(_STOR_TOTAL_W)

    pct = s.used_percent
    if pct > 85:
        pct_color = "red"
    elif pct > 60:
        pct_color = "yellow"
    else:
        pct_color = "green"
    used_txt = f"{pct:.0f}%".ljust(_STOR_USED_W)

    shared = "  [magenta]shared[/magenta]" if s.shared else ""
    return (
        f"[bold bright_white]{name}[/bold bright_white]"
        f"[dim]{stype}[/dim]"
        f"[green]{free}[/green]"
        f"[dim]{total}[/dim]"
        f"[{pct_color}]{used_txt}[/{pct_color}]"
        f"{shared}"
    )


def _stor_header() -> str:
    """Column header for storage table (3-space offset for option alignment)."""
    return (
        f"   [dim]"
        f"{'Name'.ljust(_STOR_NAME_W)}"
        f"{'Type'.ljust(_STOR_TYPE_W)}"
        f"{'Free'.ljust(_STOR_FREE_W)}"
        f"{'Total'.ljust(_STOR_TOTAL_W)}"
        f"{'Used'.ljust(_STOR_USED_W)}"
        f"[/dim]"
    )


# Regex to strip accumulated suffixes: -staging, -terraform, -MMDDYYYY, -YYYYMMDD
_SUFFIX_RE = re.compile(r"(-staging|-terraform|-\d{8})$")


def _base_template_name(name: str) -> str:
    """Strip accumulated -staging / -terraform / -timestamp suffixes to get the base name."""
    prev = None
    while name != prev:
        prev = name
        name = _SUFFIX_RE.sub("", name)
    return name


def _make_staging_name(template_name: str) -> str:
    """Generate a staging VM name from a template name."""
    return f"{_base_template_name(template_name)}-staging"


def _make_final_name(template_name: str) -> str:
    """Generate the final template name: {base}-MMDDYYYY."""
    base = _base_template_name(template_name)
    stamp = datetime.now().strftime("%m%d%Y")
    return f"{base}-{stamp}"


def _tmpl_label(t, show_node: bool = True) -> str:
    """Build a colored, columnar label for a VM template."""
    vmid = str(t.vmid or "—").ljust(_VMID_W)
    name = t.name
    if len(name) > _NAME_W:
        name = name[: _NAME_W - 2] + ".."
    name = name.ljust(_NAME_W)
    size = t.size_display
    parts = (
        f"[bold yellow]{vmid}[/bold yellow]"
        f"[bold bright_white]{name}[/bold bright_white]"
    )
    if show_node:
        node = t.node.ljust(_NODE_W)
        parts += f"  [cyan]{node}[/cyan]"
    parts += f"  [green]{size}[/green]"
    return parts


@dataclass
class WizItem:
    """A single line in the wizard."""
    kind: str          # header, option, input, toggle, info
    label: str
    key: str = ""
    group: str = ""    # radio-group name (options in same group are exclusive)
    value: str = ""
    selected: bool = False
    enabled: bool = True
    meta: dict = field(default_factory=dict)


class TemplateUpdateScreen(Screen):
    """Guided wizard for updating a VM template via staging VM."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back/Cancel", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("g", "cycle_group", "Group", show=True),
    ]

    def __init__(self, template=None):
        super().__init__()
        self._phase = 0
        self._cursor = 0
        self._items: list[WizItem] = []
        self._templates: list = []
        self._storages: list = []
        self._initial_template = template  # pre-selected Template object
        self._selected_template: Optional[dict] = None  # {name, vmid, node}
        self._staging_vmid: int = 0
        self._staging_name: str = ""
        self._cpu_cores: int = 2
        self._ram_gb: int = 4
        self._storage: str = ""
        self._vlan_tag: str = "30"
        self._ip: str = "10.0.3.251"
        self._mask: int = 24
        self._gateway: str = "10.0.3.1"
        self._dns: str = "10.0.3.3"
        self._waiting_for_user = False
        self._finalize_done = False
        self._working = False
        self._mount_gen = 0
        self._data_loaded = False
        self._editing = False
        self._editing_key = ""
        self._orphaned_vms: list[dict] = []
        self._resuming = False
        self._sort_index = 0
        self._sort_reverse = False
        self._group_index = 1  # default: group by node
        self._clone_warnings: list[str] = []
        self._finalize_warnings: list[str] = []
        self._clone_ai_summary: str = ""
        self._finalize_ai_summary: str = ""
        self._cta_timer = None
        self._cta_bright = True

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="wizard-container"):
            with Horizontal(id="wizard-progress"):
                for i, name in enumerate(PHASE_NAMES):
                    cls = "wizard-step"
                    if i == 0:
                        cls += " -active"
                    yield Static(
                        f" {i + 1}. {name} ",
                        classes=cls,
                        id=f"step-ind-{i}",
                    )

            with VerticalScroll(id="wizard-content"):
                yield Static("", id="wiz-phase-header", markup=True)

            yield Static("", id="wiz-edit-label", markup=True, classes="hidden")
            yield Input(id="wiz-edit-input", classes="hidden")
            yield Static("", id="wiz-hint", markup=True)

            with Horizontal(id="wizard-actions"):
                yield Button("Cancel", variant="error", id="btn-cancel")
                yield Button("Next", variant="primary", id="btn-next")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self):
        # Load persistent defaults from preferences
        tu = self.app.preferences.template_update
        if tu.ip_address:
            self._ip = tu.ip_address
        if tu.subnet_mask:
            self._mask = tu.subnet_mask
        if tu.gateway:
            self._gateway = tu.gateway
        if tu.dns_server:
            self._dns = tu.dns_server
        if tu.vlan_tag:
            self._vlan_tag = tu.vlan_tag
        if tu.cpu_cores >= 1:
            self._cpu_cores = tu.cpu_cores
        if tu.ram_gb >= 1:
            self._ram_gb = tu.ram_gb

        # If a template was pre-selected, populate selection and skip to configure
        if self._initial_template is not None:
            t = self._initial_template
            self._selected_template = {
                "name": t.name,
                "vmid": t.vmid,
                "node": t.node,
            }
            self._staging_name = _make_staging_name(t.name)
        self._load_initial_data()
        self._render_phase()

    # ------------------------------------------------------------------
    # Keyboard navigation
    # ------------------------------------------------------------------

    def on_key(self, event) -> None:
        # Handle editing mode
        if self._editing:
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self._cancel_edit()
            return

        # Phase 4 done — Enter/Escape returns to dashboard
        if self._finalize_done:
            if event.key in ("enter", "escape"):
                event.prevent_default()
                event.stop()
                self.app.pop_screen()
            return

        # Phase 3 — waiting for user to finish SSH updates
        if self._waiting_for_user:
            if event.key == "enter":
                event.prevent_default()
                event.stop()
                self._waiting_for_user = False
                self._phase = 4
                self._render_phase()
            elif event.key == "escape":
                event.prevent_default()
                event.stop()
                self.app.pop_screen()
            return

        # During automated phases, ignore input
        if self._working:
            return

        nav = self._nav_indices()
        if not nav:
            return

        if event.key in ("up", "k"):
            event.prevent_default()
            event.stop()
            nxt = self._nav_move(nav, -1)
            if nxt is not None:
                self._cursor = nxt
                self._refresh_lines()
                self._scroll_to_cursor()
                self._unfocus_next_btn()

        elif event.key in ("down", "j"):
            event.prevent_default()
            event.stop()
            nxt = self._nav_move(nav, 1)
            if nxt is not None:
                self._cursor = nxt
                self._refresh_lines()
                self._scroll_to_cursor()
                self._unfocus_next_btn()

        elif event.key == "space":
            event.prevent_default()
            event.stop()
            if self._cursor >= len(self._items):
                self._go_next()
            else:
                self._activate_item()

        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            if 0 <= self._cursor < len(self._items):
                item = self._items[self._cursor]
                if item.kind in ("option", "input"):
                    self._activate_item()
                else:
                    self._go_next()
            else:
                self._go_next()

        elif event.key == "backspace":
            event.prevent_default()
            event.stop()
            if self._phase > 0 and not self._working:
                self._go_back()

    def _nav_indices(self) -> list[int]:
        return [
            i for i, it in enumerate(self._items)
            if it.kind in ("option", "input") and it.enabled
        ]

    def _nav_move(self, nav: list[int], direction: int) -> Optional[int]:
        if self._cursor in nav:
            idx = nav.index(self._cursor)
            new_idx = idx + direction
            if 0 <= new_idx < len(nav):
                return nav[new_idx]
        else:
            if direction > 0:
                for n in nav:
                    if n > self._cursor:
                        return n
            else:
                for n in reversed(nav):
                    if n < self._cursor:
                        return n
        return None

    # ------------------------------------------------------------------
    # Item activation
    # ------------------------------------------------------------------

    def _activate_item(self):
        if self._cursor < 0 or self._cursor >= len(self._items):
            return
        item = self._items[self._cursor]

        if item.kind == "option":
            if item.group:
                for it in self._items:
                    if it.group == item.group:
                        it.selected = False
            item.selected = True
            self._apply_selection(item)
            self._refresh_lines()
            self._maybe_focus_next()

        elif item.kind == "input":
            self._start_edit(item)

    # ------------------------------------------------------------------
    # Text editing (hidden Input)
    # ------------------------------------------------------------------

    def _start_edit(self, item: WizItem):
        self._editing = True
        self._editing_key = item.key
        lbl = self.query_one("#wiz-edit-label", Static)
        inp = self.query_one("#wiz-edit-input", Input)
        lbl.update(f"[b]{item.label}:[/b]")
        lbl.remove_class("hidden")
        inp.value = item.value
        inp.placeholder = item.meta.get("placeholder", "")
        inp.remove_class("hidden")
        inp.focus()
        self._set_hint("Enter to confirm, Escape to cancel")

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "wiz-edit-input" and self._editing:
            self._finish_edit(event.input.value)

    def _finish_edit(self, value: str):
        for item in self._items:
            if item.key == self._editing_key:
                item.value = value.strip()
                self._apply_input_value(item)
                break
        self._editing = False
        self._editing_key = ""
        self.query_one("#wiz-edit-label", Static).add_class("hidden")
        inp = self.query_one("#wiz-edit-input", Input)
        inp.add_class("hidden")
        inp.blur()
        self._refresh_lines()
        self._update_phase_hint()
        self._maybe_focus_next()

    def _cancel_edit(self):
        self._editing = False
        self._editing_key = ""
        self.query_one("#wiz-edit-label", Static).add_class("hidden")
        inp = self.query_one("#wiz-edit-input", Input)
        inp.add_class("hidden")
        inp.blur()
        self._update_phase_hint()

    # ------------------------------------------------------------------
    # Apply values
    # ------------------------------------------------------------------

    def _apply_selection(self, item: WizItem):
        if item.group == "template":
            m = item.meta
            if m.get("resume"):
                self._selected_template = {
                    "name": m.get("name", ""),
                    "vmid": m.get("vmid"),
                    "node": m.get("node", ""),
                }
                self._staging_vmid = m.get("staging_vmid", 0)
                self._staging_name = m.get("staging_name", "")
                self._resuming = True
                try:
                    btn = self.query_one("#btn-next", Button)
                    btn.label = "Resume"
                    btn.variant = "success"
                except Exception:
                    pass
            else:
                self._selected_template = {
                    "name": m.get("name", ""),
                    "vmid": m.get("vmid"),
                    "node": m.get("node", ""),
                }
                self._staging_name = _make_staging_name(m.get("name", ""))
                self._resuming = False
                try:
                    btn = self.query_one("#btn-next", Button)
                    btn.label = "Next"
                    btn.variant = "primary"
                except Exception:
                    pass
        elif item.group == "storage":
            self._storage = item.key

    def _apply_input_value(self, item: WizItem):
        if item.key == "cpu_cores":
            try:
                self._cpu_cores = int(item.value) if item.value else 2
            except ValueError:
                pass
        elif item.key == "ram_gb":
            try:
                self._ram_gb = int(item.value) if item.value else 4
            except ValueError:
                pass
        elif item.key == "vlan_tag":
            self._vlan_tag = item.value.strip()
        elif item.key == "ip_address":
            self._ip = item.value.strip()
        elif item.key == "subnet_mask":
            try:
                self._mask = int(item.value) if item.value else 24
            except ValueError:
                pass
        elif item.key == "gateway":
            self._gateway = item.value.strip()
        elif item.key == "dns_server":
            self._dns = item.value.strip()
        elif item.key == "staging_name":
            self._staging_name = item.value.strip()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_phase(self):
        self._items = []
        self._cursor = 0
        self._unfocus_next_btn()
        self._stop_cta_pulse()

        if self._phase == 0:
            self._build_select_items()
        elif self._phase == 1:
            self._build_configure_items()
        elif self._phase == 2:
            self._build_clone_phase()
            return  # clone phase uses RichLog, not items
        elif self._phase == 3:
            self._build_waiting_items()
        elif self._phase == 4:
            self._build_finalize_phase()
            return  # finalize phase uses RichLog, not items

        # Update step indicators
        for i in range(len(PHASE_NAMES)):
            try:
                ind = self.query_one(f"#step-ind-{i}")
                cls = "wizard-step"
                if i < self._phase:
                    cls += " -completed"
                elif i == self._phase:
                    cls += " -active"
                ind.set_classes(cls)
            except Exception:
                pass

        # Update nav buttons
        btn_next = self.query_one("#btn-next", Button)
        if self._phase == 1:
            btn_next.label = "Clone & Boot"
            btn_next.variant = "success"
        elif self._phase == 3:
            btn_next.label = "Next"
            btn_next.variant = "primary"
            btn_next.disabled = True
        else:
            btn_next.label = "Next"
            btn_next.variant = "primary"
            btn_next.disabled = False

        self._mount_items()

        if self._phase == 3:
            self._start_cta_pulse()

        nav = self._nav_indices()
        if nav:
            self._cursor = nav[0]
        self._refresh_lines()
        self._update_phase_hint()

        # If phase is already valid on entry, mark button as ready
        if self._phase in (0, 1):
            valid, _ = self._validate_phase()
            if valid:
                self.query_one("#btn-next", Button).add_class("-ready")

    def _mount_items(self):
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query(".wiz-cta"):
            w.remove()
        for w in self.query("#deploy-log"):
            w.remove()

        self._mount_gen += 1
        gen = self._mount_gen

        scroll = self.query_one("#wizard-content", VerticalScroll)
        header = self.query_one("#wiz-phase-header", Static)
        if self._phase == 0:
            header.update("[b]SELECT TEMPLATE TO UPDATE[/b]")
        elif self._phase == 1:
            header.update("[b]CONFIGURE STAGING VM[/b]")
        elif self._phase == 3:
            header.update("[b]STAGING VM READY[/b]")

        for idx, item in enumerate(self._items):
            if item.kind == "cta":
                cta = Static(
                    item.label,
                    markup=True,
                    id=f"wiz-cta-{gen}",
                    classes="wiz-cta",
                )
                scroll.mount(cta)
            else:
                line = Static(
                    self._format_line(idx, item),
                    markup=True,
                    id=f"wiz-line-{gen}-{idx}",
                    classes="wiz-line",
                )
                scroll.mount(line)

    def _format_line(self, idx: int, item: WizItem) -> str:
        is_cur = idx == self._cursor

        if item.kind == "header":
            return f" [bold cyan]{item.label}[/bold cyan]"

        if item.kind == "info":
            return f"   [dim]{item.label}[/dim]"

        cur = "[bold]>[/bold]" if is_cur else " "

        if item.kind == "option":
            mark = "[green]\u25cf[/green]" if item.selected else "[dim]\u25cb[/dim]"
            lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
            return f" {cur} {mark}  {lbl}"

        if item.kind == "input":
            val = (
                item.value
                if item.value
                else f"[dim]{item.meta.get('placeholder', '...')}[/dim]"
            )
            lbl = f"[bold]{item.label}:[/bold]" if is_cur else f"{item.label}:"
            return f" {cur}    {lbl}  {val}"

        return f"   {item.label}"

    def _refresh_lines(self):
        gen = self._mount_gen
        for idx, item in enumerate(self._items):
            try:
                w = self.query_one(f"#wiz-line-{gen}-{idx}", Static)
                w.update(self._format_line(idx, item))
            except Exception:
                pass

    def _scroll_to_cursor(self):
        gen = self._mount_gen
        try:
            self.query_one(
                f"#wiz-line-{gen}-{self._cursor}", Static
            ).scroll_visible()
        except Exception:
            pass

    def _start_cta_pulse(self):
        """Start a slow pulse on the CTA banner."""
        self._cta_bright = True
        self._cta_timer = self.set_interval(1.5, self._toggle_cta_pulse)

    def _stop_cta_pulse(self):
        """Stop the CTA pulse timer."""
        if self._cta_timer:
            self._cta_timer.stop()
            self._cta_timer = None

    def _toggle_cta_pulse(self):
        """Toggle the CTA banner between bright and dim states."""
        gen = self._mount_gen
        try:
            cta = self.query_one(f"#wiz-cta-{gen}", Static)
            self._cta_bright = not self._cta_bright
            if self._cta_bright:
                cta.remove_class("-dim")
            else:
                cta.add_class("-dim")
        except Exception:
            pass

    def _maybe_focus_next(self):
        """If current phase requirements are met, focus the action button."""
        valid, _ = self._validate_phase()
        if not valid:
            return
        self._cursor = len(self._items)
        self._refresh_lines()
        btn = self.query_one("#btn-next", Button)
        btn.add_class("-ready")
        btn.focus()

    def _unfocus_next_btn(self):
        try:
            btn = self.query_one("#btn-next", Button)
            btn.remove_class("-ready")
            btn.blur()
        except Exception:
            pass

    def _set_hint(self, text: str):
        try:
            self.query_one("#wiz-hint", Static).update(f"[dim]{text}[/dim]")
        except Exception:
            pass

    def _update_phase_hint(self):
        hints = {
            0: "Space/Enter select  |  s=sort  g=group",
            1: "Space to edit  |  Enter confirm  |  Backspace back",
            3: ("Press Enter when updates are complete  |  "
                "Escape to abort (keeps staging VM)"),
        }
        self._set_hint(hints.get(self._phase, ""))

    def action_cycle_sort(self):
        if self._phase != 0 or self._working:
            return
        old = self._sort_index
        self._sort_index = (old + 1) % len(SORT_FIELDS)
        if self._sort_index == old:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_reverse = False
        self._render_phase()

    def action_cycle_group(self):
        if self._phase != 0 or self._working:
            return
        self._group_index = (self._group_index + 1) % len(GROUP_MODES)
        self._render_phase()

    # ------------------------------------------------------------------
    # Phase 0: Select Template
    # ------------------------------------------------------------------

    def _build_select_items(self):
        items = self._items

        # Show orphaned staging VMs first
        if self._orphaned_vms:
            items.append(WizItem(
                kind="header",
                label="RESUME INTERRUPTED UPDATE",
            ))
            for vm in self._orphaned_vms:
                status_color = "green" if vm["status"] == "running" else "yellow"
                vmid_col = str(vm["vmid"]).ljust(_VMID_W)
                name_col = vm["name"]
                if len(name_col) > _NAME_W:
                    name_col = name_col[: _NAME_W - 2] + ".."
                name_col = name_col.ljust(_NAME_W)
                node_col = vm["node"].ljust(_NODE_W)
                lbl = (
                    f"[bold yellow]{vmid_col}[/bold yellow]"
                    f"[bold bright_white]{name_col}[/bold bright_white]"
                    f"  [cyan]{node_col}[/cyan]"
                    f"  [{status_color}]{vm['status']}[/{status_color}]"
                )
                items.append(WizItem(
                    kind="option",
                    label=lbl,
                    key=f"resume:{vm['vmid']}",
                    group="template",
                    selected=False,
                    meta={
                        "resume": True,
                        "name": vm["template_name"],
                        "vmid": vm["template_vmid"],
                        "node": vm["template_node"],
                        "staging_vmid": vm["vmid"],
                        "staging_name": vm["name"],
                        "staging_node": vm["node"],
                    },
                ))
            items.append(WizItem(kind="info", label=""))

        # Sort/group status line
        sort_arrow = "▼" if self._sort_reverse else "▲"
        sort_lbl = SORT_LABELS[self._sort_index]
        group_lbl = GROUP_LABELS[self._group_index]
        items.append(WizItem(
            kind="info",
            label=(
                f"[bold]Sort:[/bold] [cyan]{sort_lbl}[/cyan] {sort_arrow}"
                f"    [bold]Group:[/bold] [cyan]{group_lbl}[/cyan]"
                f"    [dim](s=sort  g=group)[/dim]"
            ),
        ))

        vm_templates = [
            t for t in self._templates
            if t.template_type == TemplateType.VM
        ]

        # Sort
        sort_field = SORT_FIELDS[self._sort_index]
        def _sort_key(t):
            if sort_field == "name":
                return t.name.lower()
            elif sort_field == "node":
                return t.node.lower()
            elif sort_field == "vmid":
                return t.vmid or 0
            return ""
        vm_templates = sorted(vm_templates, key=_sort_key, reverse=self._sort_reverse)

        # Group
        group_mode = GROUP_MODES[self._group_index]
        if group_mode == "node" and vm_templates:
            nodes_seen: list[str] = []
            by_node: dict[str, list] = {}
            for t in vm_templates:
                if t.node not in by_node:
                    by_node[t.node] = []
                    nodes_seen.append(t.node)
                by_node[t.node].append(t)
            for ni, node_name in enumerate(nodes_seen):
                items.append(WizItem(
                    kind="header",
                    label=f"NODE: {node_name}  [dim]({len(by_node[node_name])} templates)[/dim]",
                ))
                if ni == 0:
                    hdr = (
                        f"   [dim]{'VMID'.ljust(_VMID_W)}"
                        f"{'Name'.ljust(_NAME_W)}"
                        f"  Size[/dim]"
                    )
                    items.append(WizItem(kind="info", label=hdr))
                for t in by_node[node_name]:
                    sel = (
                        self._selected_template is not None
                        and self._selected_template.get("vmid") == t.vmid
                    )
                    items.append(WizItem(
                        kind="option",
                        label=_tmpl_label(t, show_node=False),
                        key=f"tmpl:{t.vmid}",
                        group="template",
                        selected=sel,
                        meta={"name": t.name, "vmid": t.vmid, "node": t.node},
                    ))
        elif vm_templates:
            # Column headers (3 extra spaces to align with option prefix)
            hdr = (
                f"   [dim]{'VMID'.ljust(_VMID_W)}"
                f"{'Name'.ljust(_NAME_W)}"
                f"  {'Node'.ljust(_NODE_W)}"
                f"  Size[/dim]"
            )
            items.append(WizItem(kind="info", label=hdr))
            for t in vm_templates:
                sel = (
                    self._selected_template is not None
                    and self._selected_template.get("vmid") == t.vmid
                )
                items.append(WizItem(
                    kind="option",
                    label=_tmpl_label(t, show_node=True),
                    key=f"tmpl:{t.vmid}",
                    group="template",
                    selected=sel,
                    meta={"name": t.name, "vmid": t.vmid, "node": t.node},
                ))
        elif self._data_loaded:
            items.append(WizItem(
                kind="info",
                label="[yellow]No QEMU VM templates found[/yellow]",
            ))
            items.append(WizItem(
                kind="info",
                label="[dim]  Create a VM template in Proxmox first[/dim]",
            ))
        else:
            items.append(WizItem(kind="info", label="Loading templates..."))

    # ------------------------------------------------------------------
    # Phase 1: Configure Staging VM
    # ------------------------------------------------------------------

    def _build_configure_items(self):
        items = self._items

        if self._selected_template:
            t_name = self._selected_template.get("name", "")
            t_vmid = self._selected_template.get("vmid", "")
            t_node = self._selected_template.get("node", "")
            items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]Template:[/dim]  "
                    f"[bold yellow]{t_vmid}[/bold yellow]  "
                    f"[bold bright_white]{t_name}[/bold bright_white]  "
                    f"[dim]on[/dim] [cyan]{t_node}[/cyan]"
                ),
            ))
            items.append(WizItem(kind="info", label=""))

        items.append(WizItem(kind="header", label="COMPUTE"))
        items.append(WizItem(
            kind="input", label="[cyan]CPU Cores[/cyan]", key="cpu_cores",
            value=str(self._cpu_cores),
            meta={"placeholder": "2"},
        ))
        items.append(WizItem(
            kind="input", label="[cyan]RAM (GB)[/cyan]", key="ram_gb",
            value=str(self._ram_gb),
            meta={"placeholder": "4"},
        ))

        # Storage filtered to the template's node
        tmpl_node = (
            self._selected_template["node"]
            if self._selected_template
            else ""
        )
        items.append(WizItem(
            kind="header",
            label=f"STORAGE  [dim]on {tmpl_node}[/dim]" if tmpl_node else "STORAGE",
        ))
        node_storages = [
            s for s in self._storages
            if s.node == tmpl_node or s.shared
        ]
        if node_storages:
            items.append(WizItem(kind="info", label=_stor_header()))
            seen: set[str] = set()
            for s in node_storages:
                if s.storage not in seen:
                    seen.add(s.storage)
                    items.append(WizItem(
                        kind="option", label=_stor_label(s),
                        key=s.storage, group="storage",
                        selected=self._storage == s.storage,
                    ))
        elif self._storages:
            items.append(WizItem(
                kind="info",
                label=f"[yellow]No storage pools on {tmpl_node}[/yellow]",
            ))
        else:
            items.append(WizItem(kind="info", label="Loading storage..."))

        items.append(WizItem(kind="header", label="VLAN"))
        items.append(WizItem(
            kind="input", label="[cyan]VLAN Tag[/cyan]", key="vlan_tag",
            value=self._vlan_tag,
            meta={"placeholder": "(optional)"},
        ))

        items.append(WizItem(kind="header", label="NETWORK"))
        items.append(WizItem(
            kind="input", label="[cyan]IP Address[/cyan]", key="ip_address",
            value=self._ip,
            meta={"placeholder": "10.0.3.251"},
        ))
        items.append(WizItem(
            kind="input", label="[cyan]Subnet Mask[/cyan]", key="subnet_mask",
            value=str(self._mask),
            meta={"placeholder": "/24"},
        ))
        items.append(WizItem(
            kind="input", label="[cyan]Gateway[/cyan]", key="gateway",
            value=self._gateway,
            meta={"placeholder": "10.0.3.1"},
        ))
        items.append(WizItem(
            kind="input", label="[cyan]DNS Server[/cyan]", key="dns_server",
            value=self._dns,
            meta={"placeholder": "10.0.3.3"},
        ))

        items.append(WizItem(kind="header", label="STAGING VM"))
        items.append(WizItem(
            kind="input", label="[cyan]Staging VM Name[/cyan]", key="staging_name",
            value=self._staging_name,
            meta={"placeholder": "e.g. ubuntu-22-staging"},
        ))

    # ------------------------------------------------------------------
    # Phase 2: Cloning & Booting (automated, RichLog)
    # ------------------------------------------------------------------

    def _build_clone_phase(self):
        """Switch to RichLog for the clone phase."""
        # Update step indicators
        for i in range(len(PHASE_NAMES)):
            try:
                ind = self.query_one(f"#step-ind-{i}")
                cls = "wizard-step"
                if i < self._phase:
                    cls += " -completed"
                elif i == self._phase:
                    cls += " -active"
                ind.set_classes(cls)
            except Exception:
                pass

        self._show_richlog("Cloning & Booting...")
        self._run_clone()

    def _build_finalize_phase(self):
        """Switch to RichLog for the finalize phase."""
        # Update step indicators
        for i in range(len(PHASE_NAMES)):
            try:
                ind = self.query_one(f"#step-ind-{i}")
                cls = "wizard-step"
                if i < self._phase:
                    cls += " -completed"
                elif i == self._phase:
                    cls += " -active"
                ind.set_classes(cls)
            except Exception:
                pass

        self._show_richlog("Finalizing...")
        self._run_finalize()

    def _show_richlog(self, phase_title: str):
        """Clear wizard items and mount a RichLog widget."""
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query(".wiz-cta"):
            w.remove()
        for w in self.query("#deploy-log"):
            w.remove()
        scroll = self.query_one("#wizard-content", VerticalScroll)
        scroll.mount(RichLog(markup=True, id="deploy-log"))
        self.query_one("#wiz-phase-header", Static).update(
            f"[b]{phase_title}[/b]"
        )
        self.query_one("#btn-next", Button).disabled = True
        self._set_hint("Operation in progress...")

    # ------------------------------------------------------------------
    # Phase 3: Waiting for user
    # ------------------------------------------------------------------

    def _build_waiting_items(self):
        items = self._items
        tmpl = self._selected_template or {}

        items.append(WizItem(kind="header", label="STAGING VM READY"))
        items.append(WizItem(
            kind="info",
            label=f"VM Name:   [b]{self._staging_name}[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"VMID:      [b]{self._staging_vmid}[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"IP:        [b]{self._ip}[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Node:      [b]{tmpl.get('node', '')}[/b]",
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="cta",
            label=(
                "SSH to this VM and perform your updates.\n"
                "\n"
                "When finished, press [b]Enter[/b] to finalize."
            ),
        ))

        if self._clone_ai_summary:
            items.append(WizItem(kind="info", label=""))
            items.append(WizItem(kind="header", label="ISSUES DETECTED"))
            items.append(WizItem(
                kind="info",
                label=f"[yellow]{self._clone_ai_summary}[/yellow]",
            ))
        elif self._clone_warnings:
            items.append(WizItem(kind="info", label=""))
            items.append(WizItem(kind="header", label="ISSUES DETECTED"))
            for w in self._clone_warnings:
                items.append(WizItem(
                    kind="info",
                    label=f"[yellow]- {w}[/yellow]",
                ))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_next(self):
        if self._phase == 0:
            valid, msg = self._validate_phase()
            if not valid:
                self.notify(msg, severity="error")
                return
            if self._resuming:
                # Skip configure and clone — go straight to waiting
                self._phase = 3
                self._render_phase()
                self._waiting_for_user = True
                self._set_hint(
                    "Press Enter when updates are complete  |  "
                    "Escape to abort (keeps staging VM)"
                )
                return
            self._phase = 1
            self._render_phase()
        elif self._phase == 1:
            valid, msg = self._validate_phase()
            if not valid:
                self.notify(msg, severity="error")
                return
            # Save current values as defaults for next time
            self._save_update_prefs()
            self._phase = 2
            self._render_phase()
        # Phases 2, 3, 4 are handled by automated workers / key handlers

    def _go_back(self):
        if self._phase == 1:
            self._phase = 0
            self._render_phase()

    def action_handle_escape(self):
        if self._finalize_done:
            self.app.pop_screen()
        elif self._editing:
            self._cancel_edit()
        elif self._working:
            self.notify("Operation in progress...", severity="warning")
        elif self._waiting_for_user:
            # Leave staging VM running for manual cleanup
            self._stop_cta_pulse()
            self.app.pop_screen()
        elif self._phase > 0 and self._phase <= 1:
            self._go_back()
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed):
        if self._finalize_done:
            self.app.pop_screen()
            return
        if event.button.id == "btn-cancel":
            if self._working:
                self.notify("Operation in progress...", severity="warning")
                return
            self.app.pop_screen()
        elif event.button.id == "btn-next":
            if not self._working:
                self._go_next()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_phase(self) -> tuple[bool, str]:
        if self._phase == 0:
            if not self._selected_template:
                return False, "Please select a template"
            return True, ""
        elif self._phase == 1:
            if self._cpu_cores < 1:
                return False, "CPU cores must be at least 1"
            if self._ram_gb < 1:
                return False, "RAM must be at least 1 GB"
            if not self._staging_name:
                return False, "Please enter a staging VM name"
            if not self._ip:
                return False, "Please enter an IP address"
            return True, ""
        return True, ""

    def _save_update_prefs(self):
        """Persist current configure values as defaults."""
        tu = self.app.preferences.template_update
        tu.ip_address = self._ip
        tu.subnet_mask = self._mask
        tu.gateway = self._gateway
        tu.dns_server = self._dns
        tu.vlan_tag = self._vlan_tag
        tu.cpu_cores = self._cpu_cores
        tu.ram_gb = self._ram_gb
        self.app.preferences.save()

    # ------------------------------------------------------------------
    # Background data loaders
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        try:
            self._templates = self.app.proxmox.get_vm_templates()
            self._storages = self.app.proxmox.get_storage_info()

            # Detect orphaned staging VMs (name ends with "-staging")
            all_vms = self.app.proxmox.get_all_qemu_vms()
            template_map = {
                t.name: {"vmid": t.vmid, "node": t.node}
                for t in self._templates
                if t.template_type == TemplateType.VM
            }
            orphans = []
            for vm in all_vms:
                name = vm.get("name", "")
                if name.endswith("-staging"):
                    base_name = name[: -len("-staging")]
                    if base_name in template_map:
                        tmpl_info = template_map[base_name]
                        orphans.append({
                            "name": name,
                            "vmid": vm["vmid"],
                            "node": vm["node"],
                            "status": vm["status"],
                            "template_name": base_name,
                            "template_vmid": tmpl_info["vmid"],
                            "template_node": tmpl_info["node"],
                        })
            self._orphaned_vms = orphans

            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)
        except Exception:
            pass

    def _on_data_loaded(self):
        if self._phase == 0:
            self._render_phase()

    # ------------------------------------------------------------------
    # Phase 2: Clone worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _run_clone(self):
        self._working = True
        self._clone_warnings = []
        tmpl = self._selected_template
        if not tmpl:
            return

        node = tmpl["node"]
        template_vmid = tmpl["vmid"]
        template_name = tmpl["name"]
        ram_mb = self._ram_gb * 1024

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        try:
            # Step 1: Get next VMID
            log("[bold]Getting next available VMID...[/bold]")
            new_vmid = self.app.proxmox.get_next_vmid()
            self._staging_vmid = new_vmid
            log(f"[green]  Got VMID: {new_vmid}[/green]\n")

            # Step 2: Clone template
            log(
                f"[bold]Cloning template {template_name} "
                f"(VMID {template_vmid})...[/bold]"
            )
            upid = self.app.proxmox.clone_vm(
                node, template_vmid, new_vmid, self._staging_name, full=True,
            )
            log("[dim]  Waiting for clone task to complete...[/dim]")
            ok = self.app.proxmox.wait_for_task(node, upid, timeout=300)
            if not ok:
                log("[red]  Clone task failed or timed out![/red]")
                self._working = False
                self._show_retry_hint()
                return
            log("[green]  Clone complete![/green]\n")

            # Step 3: Configure staging VM
            log("[bold]Configuring staging VM...[/bold]")

            # Ensure cloud-init drive exists so ipconfig0 is applied
            storage_id = self._storage or "local-lvm"
            log("[dim]  Checking for cloud-init drive...[/dim]")
            try:
                vm_cfg = self.app.proxmox.get_vm_config(node, new_vmid)
                ci_bus = None
                for key, val in vm_cfg.items():
                    if re.match(r"(ide|scsi|sata|virtio)\d+", key):
                        if isinstance(val, str) and "cloudinit" in val:
                            ci_bus = key
                            break
                if ci_bus:
                    log(f"[green]  Cloud-init drive found on {ci_bus}[/green]")
                else:
                    log("[dim]  No cloud-init drive — attaching on ide2...[/dim]")
                    try:
                        self.app.proxmox.set_vm_config(
                            node, new_vmid,
                            ide2=f"{storage_id}:cloudinit",
                        )
                        log("[green]  Cloud-init drive attached on ide2[/green]")
                    except Exception:
                        try:
                            self.app.proxmox.set_vm_config(
                                node, new_vmid,
                                scsi1=f"{storage_id}:cloudinit",
                            )
                            log("[green]  Cloud-init drive attached on scsi1[/green]")
                        except Exception as ci_err:
                            log(f"[yellow]  cloud-init drive: {ci_err} "
                                f"(IP may need manual config)[/yellow]")
                            self._clone_warnings.append(
                                f"Cloud-init drive: {ci_err}")
            except Exception as cfg_err:
                log(f"[dim]  Could not read VM config ({cfg_err}), "
                    f"attempting cloud-init attach...[/dim]")
                try:
                    self.app.proxmox.set_vm_config(
                        node, new_vmid,
                        ide2=f"{storage_id}:cloudinit",
                    )
                except Exception:
                    pass

            # Set CPU, RAM, network, and cloud-init IP config
            net0_val = "virtio,bridge=vmbr0"
            if self._vlan_tag:
                net0_val += f",tag={self._vlan_tag}"
            self.app.proxmox.set_vm_config(
                node, new_vmid,
                cores=self._cpu_cores,
                memory=ram_mb,
                ipconfig0=f"ip={self._ip}/{self._mask},gw={self._gateway}",
                nameserver=self._dns,
                net0=net0_val,
            )
            log(f"[green]  Configured: {self._cpu_cores} cores, "
                f"{self._ram_gb} GB RAM, IP {self._ip}/{self._mask}, "
                f"VLAN {self._vlan_tag or 'none'}[/green]\n")

            # Step 4: Start VM
            log("[bold]Starting staging VM...[/bold]")
            upid = self.app.proxmox.start_vm(node, new_vmid)
            ok = self.app.proxmox.wait_for_task(node, upid, timeout=60)
            if not ok:
                log("[red]  Start task failed or timed out![/red]")
                self._working = False
                self._show_retry_hint()
                return
            log("[green]  Start command sent![/green]\n")

            # Step 5: Wait for VM to come online
            log("[bold]Waiting for VM to come online...[/bold]")
            elapsed = 0
            max_wait = 60
            online = False
            while elapsed < max_wait:
                try:
                    status = self.app.proxmox.get_vm_status(node, new_vmid)
                    if status.get("status") == "running":
                        online = True
                        break
                except Exception:
                    pass
                time.sleep(3)
                elapsed += 3
                log(f"[dim]  Polling... ({elapsed}s)[/dim]")

            if not online:
                log("[yellow]  VM did not reach running state within "
                    f"{max_wait}s (may still be booting)[/yellow]\n")
                self._clone_warnings.append(f"VM did not reach running state within {max_wait}s")
            else:
                log("[green]  VM is online![/green]\n")

            log(f"\n[bold green]VM is online! VMID: {new_vmid}, "
                f"IP: {self._ip}[/bold green]\n")

        except Exception as e:
            log(f"\n[red]Clone error: {e}[/red]")
            self._working = False
            self._show_retry_hint()
            return

        self._working = False

        # Diagnose any clone warnings via AI
        if self._clone_warnings:
            ai = getattr(self.app, "ai_client", None)
            if ai and getattr(ai, "diagnose_issues", None):
                try:
                    self._clone_ai_summary = ai.diagnose_issues(
                        self._clone_warnings, "cloning and booting staging VM"
                    )
                except Exception:
                    pass

        # Transition to Phase 3 (waiting for user)
        def _go_to_waiting():
            self._phase = 3
            self._render_phase()
            self._waiting_for_user = True
            self._set_hint(
                "Press Enter when updates are complete  |  "
                "Escape to abort (keeps staging VM)"
            )
        self.app.call_from_thread(_go_to_waiting)

    def _show_retry_hint(self):
        def _hint():
            self._set_hint(
                "[red]Failed![/red]  Press Escape to go back"
            )
            try:
                btn = self.query_one("#btn-next", Button)
                btn.disabled = True
            except Exception:
                pass
        self.app.call_from_thread(_hint)

    # ------------------------------------------------------------------
    # Phase 4: Finalize worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _run_finalize(self):
        self._working = True
        self._finalize_warnings = []
        tmpl = self._selected_template
        if not tmpl:
            return

        node = tmpl["node"]
        original_vmid = tmpl["vmid"]
        new_vmid = self._staging_vmid

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        try:
            # Step 1: Check VM status
            log("[bold]Checking VM status...[/bold]")
            status = self.app.proxmox.get_vm_status(node, new_vmid)
            current = status.get("status", "unknown")
            log(f"[dim]  Current status: {current}[/dim]\n")

            # Step 2: Shut down if running
            if current == "running":
                log("[bold]Shutting down staging VM...[/bold]")
                upid = self.app.proxmox.stop_vm(node, new_vmid)
                ok = self.app.proxmox.wait_for_task(node, upid, timeout=60)
                if not ok:
                    log("[yellow]  Stop task may have timed out, "
                        "polling status...[/yellow]")
                    self._finalize_warnings.append("Stop task may have timed out")

                # Poll until stopped
                elapsed = 0
                max_wait = 60
                stopped = False
                while elapsed < max_wait:
                    try:
                        st = self.app.proxmox.get_vm_status(node, new_vmid)
                        if st.get("status") == "stopped":
                            stopped = True
                            break
                    except Exception:
                        pass
                    time.sleep(3)
                    elapsed += 3
                    log(f"[dim]  Waiting for shutdown... ({elapsed}s)[/dim]")

                if not stopped:
                    log("[red]  VM did not stop within "
                        f"{max_wait}s![/red]")
                    self._working = False
                    self._show_retry_hint()
                    return
                log("[green]  VM stopped.[/green]\n")
            else:
                log("[dim]  VM already stopped.[/dim]\n")

            # Step 3: Remove old template
            log(
                f"[bold]Removing old template "
                f"(VMID {original_vmid})...[/bold]"
            )
            upid = self.app.proxmox.delete_vm(node, original_vmid)
            ok = self.app.proxmox.wait_for_task(node, upid, timeout=120)
            if not ok:
                log("[red]  Delete task failed or timed out![/red]")
                self._working = False
                self._show_retry_hint()
                return
            log("[green]  Old template removed.[/green]\n")

            # Step 4: Convert staging VM to template
            log("[bold]Converting staging VM to template...[/bold]")
            self.app.proxmox.convert_to_template(node, new_vmid)
            log("[green]  Conversion complete![/green]\n")

            # Step 5: Rename to base-MMDDYYYY
            final_name = _make_final_name(tmpl["name"])
            log(f"[bold]Renaming template to {final_name}...[/bold]")
            self.app.proxmox.set_vm_config(node, new_vmid, name=final_name)
            log(f"[green]  Renamed to {final_name}[/green]\n")

            log(
                f"\n[bold green]Template update complete! "
                f"{final_name} (VMID {new_vmid})[/bold green]"
            )

        except Exception as e:
            log(f"\n[red]Finalize error: {e}[/red]")
            self._working = False
            self._show_retry_hint()
            return

        self._working = False

        # Diagnose any finalize warnings via AI
        if self._finalize_warnings:
            ai = getattr(self.app, "ai_client", None)
            if ai and getattr(ai, "diagnose_issues", None):
                try:
                    self._finalize_ai_summary = ai.diagnose_issues(
                        self._finalize_warnings, "finalizing template update"
                    )
                except Exception:
                    pass

        if self._finalize_ai_summary:
            log(f"\n[yellow bold]Issues noted:[/yellow bold]")
            log(f"[yellow]{self._finalize_ai_summary}[/yellow]")
        elif self._finalize_warnings:
            log(f"\n[yellow bold]Issues noted:[/yellow bold]")
            for w in self._finalize_warnings:
                log(f"[yellow]- {w}[/yellow]")

        self._finalize_done = True

        def _show_done():
            self._set_hint(
                "[bold green]Done![/bold green]  "
                "Press [b]Enter[/b] to return to dashboard"
            )
            try:
                btn = self.query_one("#btn-next", Button)
                btn.label = "Done"
                btn.disabled = False
                btn.variant = "success"
                btn.add_class("-ready")
                btn.focus()
            except Exception:
                pass
        self.app.call_from_thread(_show_done)
