"""New VM creation wizard — keyboard-first, cursor-based navigation.

6-step wizard: Template, Identity, Network, Resources, Access, Review.
Uses Static line widgets navigated with arrow keys / Space / Enter,
following the pattern from ansible_run_modal.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual import work

from infraforge.models import NewVMSpec, VMType, TemplateType
from infraforge.ansible_runner import PlaybookInfo, discover_playbooks


WIZARD_STEPS = ["Template", "Identity", "Network", "Resources", "Access", "Review"]


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


class NewVMScreen(Screen):
    """Guided wizard for creating a new VM with Terraform provisioning."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back/Cancel", show=True),
    ]

    def __init__(self, template_name: str = ""):
        super().__init__()
        self._step = 0
        self.spec = NewVMSpec()
        self._cursor = 0
        self._items: list[WizItem] = []
        self._editing = False
        self._editing_key = ""
        # Data caches
        self._pve_nodes: list = []
        self._templates: list = []
        self._storages: list = []
        self._subnets: list = []
        self._available_ips: list[str] = []
        self._ssh_keys: list[tuple[str, str]] = []
        self._dns_check_result = None
        self._dns_check_timer = None
        self._deploying = False
        self._deploy_done = False
        self._post_deploy = False
        self._post_deploy_running = False
        self._available_playbooks: list[PlaybookInfo] = []
        self._initial_template = template_name
        self._saved_templates: list[dict] = []
        self._data_loaded = False
        self._mount_gen = 0
        self._net_mode = ""  # "ipam" or "manual" — dims the other section
        self._ip_from_ipam = False
        self._vm_count = 1
        self._batch_specs: list[NewVMSpec] = []  # per-VM overrides (name, ip_address, dns_name)

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="wizard-container"):
            with Horizontal(id="wizard-progress"):
                for i, name in enumerate(WIZARD_STEPS):
                    cls = "wizard-step"
                    if i == 0:
                        cls += " -active"
                    yield Static(f" {i + 1}. {name} ", classes=cls, id=f"step-ind-{i}")

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
        defaults = self.app.config.defaults
        self.spec.cpu_cores = defaults.cpu_cores
        self.spec.memory_mb = defaults.memory_mb
        self.spec.disk_gb = 10
        self.spec.storage = defaults.storage
        self.spec.network_bridge = defaults.network_bridge
        self.spec.start_after_create = defaults.start_on_create
        if self.app.config.dns.domain:
            self.spec.dns_domain = self.app.config.dns.domain
        dns_zones = self.app.config.dns.zones
        if dns_zones:
            self.spec.dns_zone = dns_zones[0]
            self.spec.dns_domain = dns_zones[0]

        # Load saved DNS servers preference, or use default
        saved_dns = self.app.preferences.new_vm.dns_servers
        self.spec.dns_servers = saved_dns if saved_dns else "1.1.1.1,8.8.8.8"

        # Load saved VLAN tag preference
        saved_vlan = self.app.preferences.new_vm.vlan_tag
        if saved_vlan:
            self.spec.vlan_tag = int(saved_vlan)

        self._load_initial_data()
        self._load_ipam_subnets()
        self._scan_ssh_keys()
        self._load_saved_templates()
        self._render_step()

    # ------------------------------------------------------------------
    # Keyboard navigation
    # ------------------------------------------------------------------

    def on_key(self, event) -> None:
        if self._editing:
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self._cancel_edit()
            return

        if self._deploy_done:
            if event.key in ("enter", "escape"):
                event.prevent_default()
                event.stop()
                self.app.pop_screen()
            return

        if self._deploying or self._post_deploy_running:
            return

        if self._post_deploy:
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

            elif event.key in ("down", "j"):
                event.prevent_default()
                event.stop()
                nxt = self._nav_move(nav, 1)
                if nxt is not None:
                    self._cursor = nxt
                    self._refresh_lines()
                    self._scroll_to_cursor()

            elif event.key in ("enter", "space"):
                event.prevent_default()
                event.stop()
                if 0 <= self._cursor < len(self._items):
                    item = self._items[self._cursor]
                    if item.kind == "option":
                        # Select this item
                        for it in self._items:
                            if it.group == item.group:
                                it.selected = False
                        item.selected = True
                        self._refresh_lines()
                        if item.key == "__skip__":
                            self._finish_post_deploy()
                        else:
                            self._run_selected_playbook(item.key)

            elif event.key == "escape":
                event.prevent_default()
                event.stop()
                self._finish_post_deploy()
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
            # Enter on an item activates it; otherwise go next
            if 0 <= self._cursor < len(self._items):
                item = self._items[self._cursor]
                if item.kind in ("option", "input", "toggle"):
                    self._activate_item()
                else:
                    self._go_next()
            else:
                self._go_next()

        elif event.key == "backspace":
            event.prevent_default()
            event.stop()
            if self._step > 0:
                self._go_back()

    def _nav_indices(self) -> list[int]:
        return [
            i for i, it in enumerate(self._items)
            if it.kind in ("option", "input", "toggle") and it.enabled
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

        elif item.kind == "toggle":
            item.selected = not item.selected
            self._apply_toggle(item)
            self._refresh_lines()
            self._maybe_focus_next()

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
        self._update_step_hint()
        self._maybe_focus_next()

    def _cancel_edit(self):
        self._editing = False
        self._editing_key = ""
        self.query_one("#wiz-edit-label", Static).add_class("hidden")
        inp = self.query_one("#wiz-edit-input", Input)
        inp.add_class("hidden")
        inp.blur()
        self._update_step_hint()

    # ------------------------------------------------------------------
    # Apply values to spec
    # ------------------------------------------------------------------

    def _apply_selection(self, item: WizItem):
        if item.group == "node":
            new_node = item.key
            if new_node != self.spec.node:
                self.spec.node = new_node
                # Template/storage may differ per node — clear stale picks
                self.spec.template = ""
                self.spec.template_volid = ""
                self._render_step()  # rebuild with filtered templates
                # Restore cursor to the selected node instead of top
                for i, it in enumerate(self._items):
                    if it.group == "node" and it.selected:
                        self._cursor = i
                        break
                self._refresh_lines()
            return
        elif item.group == "template":
            m = item.meta
            if m.get("type") == "ct":
                self.spec.vm_type = VMType.LXC
                self.spec.template_volid = m.get("volid", "")
                self.spec.template = m.get("name", "")
            elif m.get("type") == "vm":
                self.spec.vm_type = VMType.QEMU
                self.spec.template = m.get("name", "")
                self.spec.template_vmid = m.get("vmid")
                self.spec.template_volid = ""
        elif item.group == "saved_spec":
            self._apply_saved_template(item.key)
        elif item.group == "dns_zone":
            self.spec.dns_zone = item.key
            self.spec.dns_domain = item.key
            if self.spec.dns_name:
                self._trigger_dns_check()
        elif item.group == "subnet":
            self._net_mode = "ipam"
            self._apply_subnet_selection(item)
        elif item.group == "ip":
            self._net_mode = "ipam"
            self.spec.ip_address = item.key
            self._ip_from_ipam = True
            # Update the manual IP input field to reflect the selection
            for it in self._items:
                if it.key == "manual_ip":
                    it.value = item.key
                    break
            self._refresh_lines()
        elif item.group == "storage":
            self.spec.storage = item.key
        elif item.group == "ssh_key":
            self.spec.ssh_keys = item.meta.get("pubkey", "")

    def _apply_toggle(self, item: WizItem):
        if item.key == "start_after_create":
            self.spec.start_after_create = item.selected
        elif item.key == "unprivileged":
            self.spec.unprivileged = item.selected

    def _apply_input_value(self, item: WizItem):
        if item.key == "vm_count":
            try:
                val = int(item.value) if item.value else 1
                self._vm_count = max(1, min(val, 20))
            except ValueError:
                pass
        elif item.key == "hostname":
            hostname = item.value.strip().lower()
            self.spec.name = hostname
            self.spec.dns_name = hostname
            if self._dns_check_timer:
                self._dns_check_timer.stop()
            if hostname:
                self._dns_check_timer = self.set_timer(0.8, self._trigger_dns_check)
            if self._vm_count > 1:
                self._ensure_batch_specs()
        elif item.key.startswith("batch_hostname_"):
            idx = int(item.key.split("_")[-1])
            if 0 <= idx < len(self._batch_specs):
                hostname = item.value.strip().lower()
                self._batch_specs[idx].name = hostname
                self._batch_specs[idx].dns_name = hostname
        elif item.key.startswith("batch_ip_"):
            idx = int(item.key.split("_")[-1])
            if 0 <= idx < len(self._batch_specs):
                self._batch_specs[idx].ip_address = item.value.strip()
        elif item.key == "manual_ip":
            self._net_mode = "manual"
            self.spec.ip_address = item.value.strip()
            self._ip_from_ipam = False
        elif item.key == "gateway":
            self._net_mode = "manual"
            self.spec.gateway = item.value.strip()
        elif item.key == "bridge":
            self._net_mode = "manual"
            self.spec.network_bridge = item.value.strip()
        elif item.key == "cpu_cores":
            try:
                self.spec.cpu_cores = int(item.value) if item.value else 2
            except ValueError:
                pass
        elif item.key == "memory_mb":
            try:
                self.spec.memory_mb = int(item.value) if item.value else 2048
            except ValueError:
                pass
        elif item.key == "disk_gb":
            try:
                self.spec.disk_gb = int(item.value) if item.value else 10
            except ValueError:
                pass
        elif item.key == "vlan_tag":
            val = item.value.strip()
            self.spec.vlan_tag = int(val) if val else None
            self.app.preferences.new_vm.vlan_tag = val
            self.app.preferences.save()
        elif item.key == "dns_servers":
            val = item.value.strip()
            self.spec.dns_servers = val
            # Persist to preferences for next time
            self.app.preferences.new_vm.dns_servers = val
            self.app.preferences.save()
        elif item.key == "ssh_key_paste":
            self.spec.ssh_keys = item.value.strip()
        elif item.key == "save_spec_name":
            if item.value.strip():
                self._save_spec(item.value.strip())

    def _apply_subnet_selection(self, item: WizItem):
        subnet_id = item.key
        self.spec.subnet_id = subnet_id
        for s in self._subnets:
            if str(s.get("id", "")) == subnet_id:
                cidr = f"{s.get('subnet', '')}/{s.get('mask', '')}"
                self.spec.subnet_cidr = cidr
                self.spec.subnet_mask = int(s.get("mask", 24))
                subnet_base = s.get("subnet", "")
                if subnet_base:
                    parts = subnet_base.split(".")
                    if len(parts) == 4:
                        parts[3] = "1"
                        self.spec.gateway = ".".join(parts)
                break
        self._load_available_ips(subnet_id)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_step(self):
        self._items = []
        self._cursor = 0
        self._unfocus_next_btn()

        [
            self._build_template_items,
            self._build_identity_items,
            self._build_network_items,
            self._build_resources_items,
            self._build_access_items,
            self._build_review_items,
        ][self._step]()

        # Update step indicators
        for i in range(len(WIZARD_STEPS)):
            ind = self.query_one(f"#step-ind-{i}")
            cls = "wizard-step"
            if i < self._step:
                cls += " -completed"
            elif i == self._step:
                cls += " -active"
            ind.set_classes(cls)

        # Update nav buttons
        btn_next = self.query_one("#btn-next", Button)
        if self._step == len(WIZARD_STEPS) - 1:
            btn_next.label = "Deploy"
            btn_next.variant = "success"
        else:
            btn_next.label = "Next"
            btn_next.variant = "primary"

        self._mount_items()

        nav = self._nav_indices()
        if nav:
            self._cursor = nav[0]
        self._refresh_lines()
        self._update_step_hint()

        # If step is already valid on entry, show button as ready (but keep
        # cursor on items so the user can still browse/edit optional fields)
        valid, _ = self._validate_step()
        if valid:
            self.query_one("#btn-next", Button).add_class("-ready")

    def _mount_items(self):
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query("#deploy-log"):
            w.remove()

        self._mount_gen += 1
        gen = self._mount_gen

        scroll = self.query_one("#wizard-content", VerticalScroll)
        header = self.query_one("#wiz-phase-header", Static)
        header.update(f"[b]Step {self._step + 1}: {WIZARD_STEPS[self._step]}[/b]")

        for idx, item in enumerate(self._items):
            line = Static(
                self._format_line(idx, item),
                markup=True,
                id=f"wiz-line-{gen}-{idx}",
                classes="wiz-line",
            )
            scroll.mount(line)

    def _format_line(self, idx: int, item: WizItem) -> str:
        is_cur = idx == self._cursor

        # Check if this item belongs to a dimmed network section
        section = item.meta.get("section", "")
        dimmed = (
            self._net_mode != ""
            and section != ""
            and section != self._net_mode
        )

        if item.kind == "separator":
            return " [dim]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/dim]"

        if item.kind == "header":
            if dimmed:
                return f" [dim yellow]{item.label}[/dim yellow]"
            return f" [bold cyan]{item.label}[/bold cyan]"

        if item.kind == "info":
            return f"   [dim]{item.label}[/dim]"

        cur = "[bold]>[/bold]" if is_cur else " "

        if item.kind == "option":
            if dimmed:
                mark = "[dim yellow]\u25cb[/dim yellow]"
                lbl = f"[dim yellow]{item.label}[/dim yellow]"
                return f" {cur} {mark}  {lbl}"
            mark = "[green]\u25cf[/green]" if item.selected else "[dim]\u25cb[/dim]"
            lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
            return f" {cur} {mark}  {lbl}"

        if item.kind == "input":
            if dimmed:
                lbl = f"[dim yellow]{item.label}:[/dim yellow]"
                val = f"[dim yellow]{item.value or '...'}[/dim yellow]"
                return f" {cur}    {lbl}  {val}"
            val = item.value if item.value else f"[dim]{item.meta.get('placeholder', '...')}[/dim]"
            # Highlight IP field yellow when populated from IPAM list
            if item.key == "manual_ip" and self._ip_from_ipam and item.value:
                lbl = f"[bold]{item.label}:[/bold]" if is_cur else f"{item.label}:"
                return f" {cur}    {lbl}  [yellow]{val}[/yellow] [dim](from IPAM)[/dim]"
            lbl = f"[bold]{item.label}:[/bold]" if is_cur else f"{item.label}:"
            return f" {cur}    {lbl}  {val}"

        if item.kind == "toggle":
            if item.selected:
                mark = "[bold green]\u2713 ON[/bold green]"
            else:
                mark = "[bold red]\u2717 OFF[/bold red]"
            lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
            return f" {cur} {mark}  {lbl}"

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
            self.query_one(f"#wiz-line-{gen}-{self._cursor}", Static).scroll_visible()
        except Exception:
            pass

    def _maybe_focus_next(self):
        """If current step requirements are met, focus the Next/Deploy button."""
        valid, _ = self._validate_step()
        if not valid:
            return
        self._cursor = len(self._items)  # move past all items
        self._refresh_lines()            # clear cursor highlight
        btn = self.query_one("#btn-next", Button)
        btn.add_class("-ready")
        btn.focus()

    def _unfocus_next_btn(self):
        """Remove focus highlight from the Next button when cursor returns to items."""
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

    def _update_step_hint(self):
        hints = {
            0: "Select node first, then pick a template  |  Space/Enter select",
            1: "Space to edit  |  Enter confirm  |  Backspace back",
            2: "Space select subnet/IP  |  Enter confirm  |  Backspace back",
            3: "Space edit/toggle  |  Enter confirm  |  Backspace back",
            4: "Space select key  |  Enter confirm  |  Backspace back",
            5: "Enter to deploy  |  Space to save spec  |  Backspace back",
        }
        self._set_hint(hints.get(self._step, ""))

    # ------------------------------------------------------------------
    # Step 0: Template & Node
    # ------------------------------------------------------------------

    def _build_template_items(self):
        items = self._items

        # ── VM count ──
        items.append(WizItem(kind="header", label="VM COUNT"))
        items.append(WizItem(
            kind="input", label="Number of VMs", key="vm_count",
            value=str(self._vm_count),
            meta={"placeholder": "1"},
        ))

        # ── Node selection (pick first) ──
        items.append(WizItem(kind="header", label="TARGET NODE"))
        if self._pve_nodes:
            for n in self._pve_nodes:
                sel = self.spec.node == n.node or (
                    not self.spec.node and n == self._pve_nodes[0]
                )
                if sel and not self.spec.node:
                    self.spec.node = n.node
                items.append(WizItem(
                    kind="option",
                    label=f"{n.node}  [dim]cpu: {n.cpu_percent:.0f}%  "
                          f"mem: {n.mem_percent:.0f}%[/dim]",
                    key=n.node, group="node", selected=sel,
                ))
        else:
            items.append(WizItem(kind="info", label="Loading nodes..."))

        # ── Templates filtered to selected node ──
        selected_node = self.spec.node
        ct = [
            t for t in self._templates
            if t.template_type == TemplateType.CONTAINER
            and t.node == selected_node
        ]
        vm = [
            t for t in self._templates
            if t.template_type == TemplateType.VM
            and t.node == selected_node
        ]

        if ct:
            items.append(WizItem(
                kind="header",
                label=f"LXC CONTAINER TEMPLATES  [dim]on {selected_node}[/dim]",
            ))
            for t in ct:
                lbl = t.name
                if t.storage:
                    lbl += f"  [dim]({t.storage}  {t.size_display})[/dim]"
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=f"ct:{t.volid or t.name}", group="template",
                    selected=(
                        self.spec.template_volid == (t.volid or t.name)
                        and self.spec.vm_type == VMType.LXC
                    ),
                    meta={"type": "ct", "volid": t.volid or t.name,
                          "name": t.name},
                ))

        if vm:
            items.append(WizItem(
                kind="header",
                label=f"QEMU VM TEMPLATES  [dim]on {selected_node}[/dim]",
            ))
            for t in vm:
                lbl = t.name
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=f"vm:{t.name}:{t.vmid or ''}", group="template",
                    selected=(
                        self.spec.template == t.name
                        and self.spec.vm_type == VMType.QEMU
                    ),
                    meta={"type": "vm", "name": t.name, "vmid": t.vmid},
                ))

        if not ct and not vm:
            items.append(WizItem(kind="header", label="TEMPLATES"))
            if self._data_loaded:
                items.append(WizItem(
                    kind="info",
                    label=f"[yellow]No templates on {selected_node}[/yellow]",
                ))
                items.append(WizItem(
                    kind="info",
                    label="[dim]  Download via pveam or select a "
                          "different node[/dim]",
                ))
            else:
                items.append(WizItem(
                    kind="info", label="Loading templates...",
                ))

        # Show deployment type indicator based on current selection
        if self.spec.template or self.spec.template_volid:
            if self.spec.vm_type == VMType.LXC:
                items.append(WizItem(kind="info", label=""))
                items.append(WizItem(
                    kind="info",
                    label="[bold yellow]\u2192 Will deploy as:[/bold yellow] [bold]LXC Container[/bold]",
                ))
            else:
                items.append(WizItem(kind="info", label=""))
                items.append(WizItem(
                    kind="info",
                    label="[bold yellow]\u2192 Will deploy as:[/bold yellow] [bold]QEMU Virtual Machine[/bold]",
                ))

        # ── Saved specs (not node-specific) ──
        if self._saved_templates:
            items.append(WizItem(kind="header", label="SAVED SPECS"))
            for t in self._saved_templates:
                lbl = (
                    f"{t['name']}  [dim]({t['vm_type'].upper()}, "
                    f"{t['cpu_cores']}c/{t['memory_mb']}MB/"
                    f"{t['disk_gb']}GB)[/dim]"
                )
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=t["name"], group="saved_spec",
                ))


    # ------------------------------------------------------------------
    # Step 1: Identity & DNS
    # ------------------------------------------------------------------

    def _build_identity_items(self):
        items = self._items

        if self._vm_count > 1:
            items.append(WizItem(kind="header", label=f"HOSTNAMES ({self._vm_count} VMs)"))
            items.append(WizItem(
                kind="input", label="Base Name", key="hostname",
                value=self.spec.name,
                meta={"placeholder": "e.g. web (generates web-01, web-02...)"},
            ))
            # Ensure batch_specs list has the right length
            self._ensure_batch_specs()
            for i, bs in enumerate(self._batch_specs):
                items.append(WizItem(
                    kind="input", label=f"  VM {i+1}", key=f"batch_hostname_{i}",
                    value=bs.name,
                    meta={"placeholder": f"{self.spec.name or 'vm'}-{i+1:02d}"},
                ))
        else:
            items.append(WizItem(kind="header", label="HOSTNAME"))
            items.append(WizItem(
                kind="input", label="Hostname", key="hostname",
                value=self.spec.name,
                meta={"placeholder": "e.g. ubuntu-web-01"},
            ))

        if self.spec.dns_name:
            if self._dns_check_result is not None:
                if self._dns_check_result:
                    ips = ", ".join(self._dns_check_result)
                    items.append(WizItem(
                        kind="info",
                        label=f"[yellow]DNS exists: {self.spec.dns_name} -> {ips}[/yellow]",
                    ))
                else:
                    items.append(WizItem(
                        kind="info",
                        label=f"[green]Available — no existing DNS record[/green]",
                    ))

        dns_zones = self.app.config.dns.zones
        if dns_zones:
            items.append(WizItem(kind="header", label="DNS ZONE"))
            for z in dns_zones:
                items.append(WizItem(
                    kind="option", label=z, key=z, group="dns_zone",
                    selected=self.spec.dns_zone == z,
                ))

        zone = self.spec.dns_zone or self.spec.dns_domain
        if self.spec.dns_name and zone:
            fqdn = f"{self.spec.dns_name}.{zone}"
            items.append(WizItem(kind="info", label=f"FQDN: [b]{fqdn}[/b]"))


    def _ensure_batch_specs(self):
        """Ensure batch_specs list matches vm_count with auto-generated names/IPs."""
        base = self.spec.name or "vm"
        while len(self._batch_specs) < self._vm_count:
            idx = len(self._batch_specs)
            s = NewVMSpec()
            s.name = f"{base}-{idx+1:02d}"
            s.dns_name = s.name
            self._batch_specs.append(s)
        self._batch_specs = self._batch_specs[:self._vm_count]
        # Regenerate names if base changed
        for i, bs in enumerate(self._batch_specs):
            if not bs.name or bs.name.startswith(("vm-", f"{base}-")) or not bs.name.rstrip("0123456789-"):
                bs.name = f"{base}-{i+1:02d}"
                bs.dns_name = bs.name

    def _resolve_batch_specs(self) -> list[NewVMSpec]:
        """Merge shared spec with per-VM overrides for deployment."""
        self._ensure_batch_specs()
        resolved = []
        for bs in self._batch_specs:
            spec = deepcopy(self.spec)
            spec.name = bs.name
            spec.dns_name = bs.dns_name
            if bs.ip_address:
                spec.ip_address = bs.ip_address
            resolved.append(spec)
        return resolved

    # ------------------------------------------------------------------
    # Step 2: Network & IPAM
    # ------------------------------------------------------------------

    def _build_network_items(self):
        items = self._items

        if self._subnets:
            items.append(WizItem(
                kind="header", label="IPAM GUIDED",
                meta={"section": "ipam"},
            ))
            for s in self._subnets:
                cidr = f"{s.get('subnet', '')}/{s.get('mask', '')}"
                desc = s.get("description", "")
                usage = s.get("usage", {})
                free_pct = usage.get("freehosts_percent", "?")
                lbl = cidr
                if desc:
                    lbl += f"  [dim]{desc}[/dim]"
                lbl += f"  [dim]({free_pct}% free)[/dim]"
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=str(s.get("id", "")), group="subnet",
                    selected=self.spec.subnet_id == str(s.get("id", "")),
                    meta={**s, "section": "ipam"},
                ))
        else:
            items.append(WizItem(
                kind="header", label="IPAM GUIDED",
                meta={"section": "ipam"},
            ))
            items.append(WizItem(
                kind="info", label="No IPAM subnets available",
                meta={"section": "ipam"},
            ))

        if self._available_ips:
            items.append(WizItem(
                kind="header", label="AVAILABLE IPs",
                meta={"section": "ipam"},
            ))
            for ip in self._available_ips[:20]:
                items.append(WizItem(
                    kind="option", label=ip, key=ip, group="ip",
                    selected=self.spec.ip_address == ip,
                    meta={"section": "ipam"},
                ))

        items.append(WizItem(kind="separator", label="",
            meta={"section": "manual"},
        ))
        items.append(WizItem(
            kind="header", label="MANUAL OVERRIDES",
            meta={"section": "manual"},
        ))
        items.append(WizItem(
            kind="input", label="IP Address", key="manual_ip",
            value=self.spec.ip_address,
            meta={"placeholder": "e.g. 10.0.100.50 (empty for DHCP)", "section": "manual"},
        ))
        items.append(WizItem(
            kind="input", label="Gateway", key="gateway",
            value=self.spec.gateway,
            meta={"placeholder": "e.g. 10.0.100.1", "section": "manual"},
        ))
        items.append(WizItem(
            kind="input", label="Bridge", key="bridge",
            value=self.spec.network_bridge,
            meta={"placeholder": "e.g. vmbr0", "section": "manual"},
        ))

        items.append(WizItem(kind="header", label="VLAN"))
        items.append(WizItem(
            kind="input", label="VLAN Tag", key="vlan_tag",
            value=str(self.spec.vlan_tag) if self.spec.vlan_tag else "",
            meta={"placeholder": "e.g. 30 (optional)"},
        ))

        items.append(WizItem(kind="header", label="DNS SERVERS"))
        items.append(WizItem(
            kind="input", label="DNS Servers", key="dns_servers",
            value=self.spec.dns_servers,
            meta={"placeholder": "e.g. 10.0.200.2,1.1.1.1 (default: 1.1.1.1,8.8.8.8)"},
        ))

        if self._vm_count > 1:
            items.append(WizItem(kind="header", label=f"IP ASSIGNMENTS ({self._vm_count} VMs)"))
            self._ensure_batch_specs()
            for i, bs in enumerate(self._batch_specs):
                # Auto-assign sequential IPs if not manually set
                if not bs.ip_address and self.spec.ip_address:
                    try:
                        parts = self.spec.ip_address.split(".")
                        parts[3] = str(int(parts[3]) + i)
                        bs.ip_address = ".".join(parts)
                    except (IndexError, ValueError):
                        pass
                items.append(WizItem(
                    kind="input", label=f"  {bs.name}", key=f"batch_ip_{i}",
                    value=bs.ip_address,
                    meta={"placeholder": "auto"},
                ))


    # ------------------------------------------------------------------
    # Step 3: Resources
    # ------------------------------------------------------------------

    def _build_resources_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="COMPUTE"))
        items.append(WizItem(
            kind="input", label="CPU Cores", key="cpu_cores",
            value=str(self.spec.cpu_cores), meta={"placeholder": "2"},
        ))
        items.append(WizItem(
            kind="input", label="Memory (MB)", key="memory_mb",
            value=str(self.spec.memory_mb), meta={"placeholder": "2048"},
        ))
        items.append(WizItem(
            kind="input", label="Disk (GB)", key="disk_gb",
            value=str(self.spec.disk_gb), meta={"placeholder": "10"},
        ))

        items.append(WizItem(
            kind="header",
            label=f"STORAGE POOL  [dim]on {self.spec.node}[/dim]",
        ))
        node_storages = [
            s for s in self._storages
            if s.node == self.spec.node or s.shared
        ]
        if node_storages:
            seen: set[str] = set()
            for s in node_storages:
                if s.storage not in seen:
                    seen.add(s.storage)
                    lbl = (f"{s.storage}  [dim]({s.storage_type}  "
                           f"{s.avail_display} free)[/dim]")
                    items.append(WizItem(
                        kind="option", label=lbl,
                        key=s.storage, group="storage",
                        selected=self.spec.storage == s.storage,
                    ))
        elif self._storages:
            items.append(WizItem(
                kind="info",
                label=f"[yellow]No storage pools on "
                      f"{self.spec.node}[/yellow]",
            ))
        else:
            items.append(WizItem(kind="info", label="Loading storage..."))

        items.append(WizItem(kind="header", label="OPTIONS"))
        items.append(WizItem(
            kind="toggle", label="Start after create",
            key="start_after_create", selected=self.spec.start_after_create,
        ))
        if self.spec.vm_type == VMType.LXC:
            items.append(WizItem(
                kind="toggle", label="Unprivileged container",
                key="unprivileged", selected=self.spec.unprivileged,
            ))


    # ------------------------------------------------------------------
    # Step 4: SSH Access
    # ------------------------------------------------------------------

    def _build_access_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="SSH KEYS"))
        if self._ssh_keys:
            for label, pubkey in self._ssh_keys:
                short = pubkey[:60] + "..." if len(pubkey) > 60 else pubkey
                items.append(WizItem(
                    kind="option",
                    label=f"{label}  [dim]{short}[/dim]",
                    key=label, group="ssh_key",
                    selected=self.spec.ssh_keys == pubkey,
                    meta={"pubkey": pubkey},
                ))
        else:
            items.append(WizItem(kind="info", label="No SSH keys found in ~/.ssh/"))

        items.append(WizItem(kind="header", label="PASTE KEY"))
        items.append(WizItem(
            kind="input", label="SSH Public Key", key="ssh_key_paste",
            value="" if (self._ssh_keys and self.spec.ssh_keys) else self.spec.ssh_keys,
            meta={"placeholder": "ssh-ed25519 AAAA... or ssh-rsa AAAA..."},
        ))


    # ------------------------------------------------------------------
    # Step 5: Review & Deploy
    # ------------------------------------------------------------------

    def _build_review_items(self):
        items = self._items
        s = self.spec
        node = s.node or "(not set)"
        name = s.name or "(not set)"
        template = s.template or s.template_volid or "(not set)"
        vm_type = "LXC Container" if s.vm_type == VMType.LXC else "QEMU VM"
        ip = s.ip_address or "DHCP"
        gw = s.gateway or "Auto"
        bridge = s.network_bridge
        zone = s.dns_zone or s.dns_domain
        fqdn = f"{s.dns_name}.{zone}" if s.dns_name and zone else "(none)"
        ssh = "Yes" if s.ssh_keys else "No"
        start = "Yes" if s.start_after_create else "No"
        mem_gb = s.memory_mb / 1024
        ip_display = f"{ip}/{s.subnet_mask}" if s.ip_address else ip

        items.append(WizItem(kind="header", label="VM SPECIFICATION"))
        items.append(WizItem(kind="info", label=f"Hostname:     [b]{name}[/b]"))
        items.append(WizItem(kind="info", label=f"Node:         [b]{node}[/b]"))
        items.append(WizItem(kind="info", label=f"Type:         [b]{vm_type}[/b]"))
        items.append(WizItem(kind="info", label=f"Template:     [b]{template}[/b]"))

        items.append(WizItem(kind="header", label="RESOURCES"))
        items.append(WizItem(kind="info", label=f"CPU:          [b]{s.cpu_cores} cores[/b]"))
        items.append(WizItem(kind="info", label=f"Memory:       [b]{s.memory_mb} MB ({mem_gb:.1f} GB)[/b]"))
        items.append(WizItem(kind="info", label=f"Disk:         [b]{s.disk_gb} GB[/b]"))
        items.append(WizItem(kind="info", label=f"Storage:      [b]{s.storage}[/b]"))

        items.append(WizItem(kind="header", label="NETWORK"))
        items.append(WizItem(kind="info", label=f"Bridge:       [b]{bridge}[/b]"))
        items.append(WizItem(kind="info", label=f"IP Address:   [b]{ip_display}[/b]"))
        items.append(WizItem(kind="info", label=f"Gateway:      [b]{gw}[/b]"))
        vlan_display = str(s.vlan_tag) if s.vlan_tag else "None"
        items.append(WizItem(kind="info", label=f"VLAN:         [b]{vlan_display}[/b]"))
        dns_svr = s.dns_servers or "1.1.1.1,8.8.8.8"
        items.append(WizItem(kind="info", label=f"DNS Servers:  [b]{dns_svr}[/b]"))
        items.append(WizItem(kind="info", label=f"FQDN:         [b]{fqdn}[/b]"))

        if self._vm_count > 1:
            self._ensure_batch_specs()
            items.append(WizItem(kind="header", label=f"VM ASSIGNMENTS ({self._vm_count} VMs)"))
            for i, bs in enumerate(self._batch_specs):
                ip_disp = bs.ip_address or "DHCP"
                fqdn_disp = f"{bs.dns_name}.{zone}" if bs.dns_name and zone else ""
                items.append(WizItem(
                    kind="info",
                    label=f"  {i+1:>2}.  [b]{bs.name}[/b]  {ip_disp}  [dim]{fqdn_disp}[/dim]",
                ))

        items.append(WizItem(kind="header", label="ACCESS"))
        items.append(WizItem(kind="info", label=f"SSH Key:      [b]{ssh}[/b]"))
        items.append(WizItem(kind="info", label=f"Auto-start:   [b]{start}[/b]"))

        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)
            if self._vm_count > 1:
                resolved = self._resolve_batch_specs()
                tf_preview = tf.get_batch_deployment_tf(resolved)
            else:
                tf_preview = tf.get_deployment_tf(s)
            items.append(WizItem(kind="header", label="TERRAFORM CONFIGURATION"))
            for line in tf_preview.split("\n"):
                items.append(WizItem(kind="info", label=f"[dim]{line}[/dim]"))
        except Exception:
            pass

        items.append(WizItem(kind="header", label="SAVE"))
        items.append(WizItem(
            kind="input", label="Save as Spec", key="save_spec_name",
            value="", meta={"placeholder": "Enter spec name to save..."},
        ))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_next(self):
        if self._step < len(WIZARD_STEPS) - 1:
            valid, msg = self._validate_step()
            if not valid:
                self.notify(msg, severity="error")
                return
            self._step += 1
            self._render_step()
        else:
            self._deploy()

    def _go_back(self):
        if self._step > 0:
            self._step -= 1
            self._render_step()

    def action_handle_escape(self):
        if self._deploy_done:
            self.app.pop_screen()
        elif self._post_deploy_running:
            self.notify("Playbook running...", severity="warning")
        elif self._post_deploy:
            self._finish_post_deploy()
        elif self._editing:
            self._cancel_edit()
        elif self._deploying:
            self.notify("Deployment in progress...", severity="warning")
        elif self._step > 0:
            self._go_back()
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed):
        if self._deploy_done:
            self.app.pop_screen()
            return
        if self._post_deploy:
            if event.button.id in ("btn-cancel", "btn-next"):
                self._finish_post_deploy()
            return
        if event.button.id == "btn-cancel":
            if self._deploying:
                self.notify("Deployment in progress...", severity="warning")
                return
            self.app.pop_screen()
        elif event.button.id == "btn-next":
            if not self._deploying:
                self._go_next()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_step(self) -> tuple[bool, str]:
        if self._step == 0:
            if self._vm_count < 1 or self._vm_count > 20:
                return False, "VM count must be between 1 and 20"
            if not self.spec.node:
                return False, "Please select a target node"
            if not self.spec.template and not self.spec.template_volid:
                return False, "Please select a template"
            return True, ""
        elif self._step == 1:
            if not self.spec.name:
                return False, "Please enter a hostname"
            if len(self.spec.name) > 1:
                if not re.match(r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$', self.spec.name):
                    return False, "Hostname: lowercase alphanumeric + hyphens only"
            elif len(self.spec.name) == 1:
                if not self.spec.name.isalnum():
                    return False, "Hostname must start with a letter or digit"
            if self._vm_count > 1:
                self._ensure_batch_specs()
                for i, bs in enumerate(self._batch_specs):
                    if not bs.name:
                        return False, f"VM {i+1}: hostname required"
                # Check for duplicates
                names = [bs.name for bs in self._batch_specs]
                if len(names) != len(set(names)):
                    return False, "Duplicate hostnames in batch"
            return True, ""
        elif self._step == 3:
            if self.spec.cpu_cores < 1:
                return False, "CPU cores must be at least 1"
            if self.spec.memory_mb < 128:
                return False, "Memory must be at least 128 MB"
            if self.spec.disk_gb < 1:
                return False, "Disk must be at least 1 GB"
            if not self.spec.storage:
                return False, "Please select a storage pool"
            return True, ""
        return True, ""

    # ------------------------------------------------------------------
    # Background data loaders
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        try:
            self._pve_nodes = self.app.proxmox.get_node_info()
            self._templates = (
                self.app.proxmox.get_vm_templates()
                + self.app.proxmox.get_downloaded_templates()
            )
            self._storages = self.app.proxmox.get_storage_info()
            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)
        except Exception:
            pass

    def _on_data_loaded(self):
        if self._step == 0:
            self._render_step()

    @work(thread=True)
    def _load_ipam_subnets(self):
        ipam_cfg = self.app.config.ipam
        if not ipam_cfg.url:
            return
        try:
            from infraforge.ipam_client import IPAMClient
            ipam = IPAMClient(self.app.config)
            self._subnets = ipam.get_subnets()
            if self._step == 2:
                self.app.call_from_thread(self._render_step)
        except Exception:
            self._subnets = []

    @work(thread=True)
    def _load_available_ips(self, subnet_id: str):
        try:
            from infraforge.ipam_client import IPAMClient
            ipam = IPAMClient(self.app.config)
            ips = ipam.get_available_ips(subnet_id)
            self._available_ips = ips
            if ips:
                self.spec.ip_address = ips[0]
                self._ip_from_ipam = True
            if self._step == 2:
                self.app.call_from_thread(self._render_step)
        except Exception:
            self._available_ips = []

    def _scan_ssh_keys(self):
        keys: list[tuple[str, str]] = []
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists():
            for pub in sorted(ssh_dir.glob("*.pub")):
                try:
                    content = pub.read_text().strip()
                    if content:
                        keys.append((pub.name, content))
                except Exception:
                    pass
        infra_keys_dir = Path.home() / ".config" / "infraforge" / "ssh_keys"
        if infra_keys_dir.exists():
            for priv in sorted(infra_keys_dir.glob("*_rsa")):
                pub_path = priv.parent / (priv.name + ".pub")
                if not pub_path.exists():
                    continue
                try:
                    content = pub_path.read_text().strip()
                    if content:
                        keys.append((f"infraforge: {priv.stem}", content))
                except Exception:
                    pass
        self._ssh_keys = keys

    def _load_saved_templates(self):
        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)
            self._saved_templates = tf.list_templates()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # DNS collision check
    # ------------------------------------------------------------------

    def _trigger_dns_check(self):
        if self.spec.dns_name:
            self._check_dns_collision()

    @work(thread=True)
    def _check_dns_collision(self):
        dns_cfg = self.app.config.dns
        if not dns_cfg.provider or not dns_cfg.server:
            return
        try:
            from infraforge.dns_client import DNSClient
            client = DNSClient.from_config(self.app.config)
            zone = self.spec.dns_zone or dns_cfg.domain
            existing = client.lookup_record(self.spec.dns_name, "A", zone)
            self._dns_check_result = existing
            if self._step == 1:
                self.app.call_from_thread(self._render_step)
        except Exception:
            self._dns_check_result = None

    # ------------------------------------------------------------------
    # Saved template management
    # ------------------------------------------------------------------

    def _apply_saved_template(self, name: str):
        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)
            spec = tf.load_template(name)
            if not spec:
                self.notify(f"Spec '{name}' not found", severity="error")
                return
            self.spec = spec
            self.notify(f"Spec '{name}' loaded", severity="information")
            self._render_step()
        except Exception as e:
            self.notify(f"Failed to load: {e}", severity="error")

    def _save_spec(self, name: str):
        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)
            tf.save_template(name, self.spec)
            self.notify(f"Spec '{name}' saved", severity="information")
            self._load_saved_templates()
        except Exception as e:
            self.notify(f"Failed to save: {e}", severity="error")

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------

    @work(thread=True)
    def _deploy(self):
        self._deploying = True

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        def show_log():
            def _show():
                for w in self.query(".wiz-line"):
                    w.remove()
                scroll = self.query_one("#wizard-content", VerticalScroll)
                scroll.mount(RichLog(markup=True, id="deploy-log"))
                self.query_one("#wiz-phase-header", Static).update(
                    "[b]Deploying...[/b]"
                )
                self.query_one("#btn-next", Button).disabled = True
                self._set_hint("Deployment in progress...")
            self.app.call_from_thread(_show)

        def _fail(title: str, output: str = ""):
            """Log a failure with parsed guidance if available."""
            log(f"\n[red]{title}[/red]")
            if output:
                err_title, guidance = tf.parse_terraform_error(output)
                if guidance:
                    log(f"[yellow]{err_title}[/yellow]")
                    for gline in guidance.split("\n"):
                        log(f"[yellow]{gline}[/yellow]")
                else:
                    log("[dim]Check the error output above for details.[/dim]")
            self._deploying = False

        show_log()

        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)

            # ── Pre-flight Checks ─────────────────────────────────
            log("[bold]━━━ Pre-flight Checks ━━━[/bold]\n")

            log("[bold]Checking Terraform CLI...[/bold]")
            installed, version = tf.check_terraform_installed()
            if not installed:
                log(f"[red]  ✗ Terraform not found: {version}[/red]")
                log("[yellow]  Install: https://developer.hashicorp.com/"
                    "terraform/install[/yellow]")
                self._deploying = False
                return
            log(f"[green]  ✓ {version}[/green]\n")

            log("[bold]Validating Proxmox environment...[/bold]")
            checks = tf.validate_pre_deploy(self.spec, log_fn=log)
            all_ok = True
            for name, passed, detail in checks:
                if passed:
                    log(f"[green]  ✓ {name}[/green] — {detail}")
                else:
                    log(f"[red]  ✗ {name}[/red]")
                    for dline in detail.split("\n"):
                        log(f"[red]    {dline}[/red]")
                    all_ok = False
            if not all_ok:
                log("\n[red]Pre-flight failed. Fix the issues above "
                    "and retry.[/red]")
                self._deploying = False
                return
            log("")

            # ── API Credential Setup ──────────────────────────────
            log("[bold]Setting up Terraform API credentials...[/bold]")
            pve_cfg = self.app.config.proxmox
            if pve_cfg.auth_method == "password":
                # Use password auth directly — avoids Telmate provider
                # token permission-check bugs with root@pam
                token_id, token_secret = "", ""
                log(f"[green]  ✓ Using password auth ({pve_cfg.user})[/green]\n")
            else:
                token_id, token_secret, token_msg = tf.ensure_terraform_token()
                if token_id and token_secret:
                    log(f"[green]  ✓ {token_msg}[/green]\n")
                else:
                    log(f"[yellow]  ⚠ {token_msg}[/yellow]")
                    log("[dim]    Falling back to password auth[/dim]\n")
                    token_id, token_secret = "", ""

            # ── Terraform Deployment ──────────────────────────────
            log("[bold]━━━ Terraform Deployment ━━━[/bold]\n")

            if self._vm_count > 1:
                resolved_specs = self._resolve_batch_specs()
                log(f"[bold]━━━ Batch Deployment ({len(resolved_specs)} VMs) ━━━[/bold]\n")

                log("[bold]Creating batch deployment files...[/bold]")
                deploy_dir = tf.create_batch_deployment(
                    resolved_specs, token_id, token_secret,
                )
                log(f"[dim]  {deploy_dir}[/dim]\n")
            else:
                log("[bold]Creating deployment files...[/bold]")
                deploy_dir = tf.create_deployment(
                    self.spec, token_id, token_secret,
                )
                log(f"[dim]  {deploy_dir}[/dim]\n")

            log("[bold]Caching provider plugins...[/bold]")
            ok, output = tf.ensure_provider_mirror(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-3:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                _fail("✗ Provider mirror failed!", output)
                return
            log("[green]  ✓ Providers cached[/green]\n")

            log("[bold]Running terraform init...[/bold]")
            ok, output = tf.terraform_init(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-5:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                _fail("✗ terraform init failed!", output)
                return
            log("[green]  ✓ Init successful[/green]\n")

            log("[bold]Running terraform plan...[/bold]")
            ok, output = tf.terraform_plan(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-10:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                _fail("✗ terraform plan failed!", output)
                return
            log("[green]  ✓ Plan successful[/green]\n")

            log("[bold]Running terraform apply...[/bold]")
            ok, output = tf.terraform_apply(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-10:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                _fail("✗ terraform apply failed!", output)
                return
            if self._vm_count > 1:
                log(f"\n[bold green]  ✓ {len(resolved_specs)} VMs created successfully![/bold green]\n")
            else:
                log("\n[bold green]  ✓ VM created successfully![/bold green]\n")

            # ── Post-Deployment ───────────────────────────────────
            log("[bold]━━━ Post-Deployment ━━━[/bold]\n")

            dns_cfg = self.app.config.dns
            ipam_cfg = self.app.config.ipam

            if self._vm_count > 1:
                for rs in resolved_specs:
                    # DNS
                    if dns_cfg.provider and dns_cfg.server and rs.dns_name and rs.ip_address:
                        try:
                            from infraforge.dns_client import DNSClient
                            dns = DNSClient.from_config(self.app.config)
                            zone = rs.dns_zone or dns_cfg.domain
                            result = dns.ensure_record(rs.dns_name, "A", rs.ip_address, 3600, zone)
                            fqdn = f"{rs.dns_name}.{zone}"
                            log(f"[green]  \u2713 DNS: {fqdn} -> {rs.ip_address}[/green]")
                        except Exception as e:
                            log(f"[yellow]  \u26a0 DNS {rs.dns_name}: {e}[/yellow]")
                    # IPAM
                    if ipam_cfg.url and rs.ip_address and rs.subnet_id:
                        try:
                            from infraforge.ipam_client import IPAMClient
                            ipam = IPAMClient(self.app.config)
                            ipam.create_address(rs.ip_address, rs.subnet_id, hostname=rs.name, description="Created by InfraForge")
                            log(f"[green]  \u2713 IPAM: {rs.ip_address} reserved[/green]")
                        except Exception as e:
                            log(f"[yellow]  \u26a0 IPAM {rs.ip_address}: {e}[/yellow]")
            else:
                # DNS record
                if (
                    dns_cfg.provider and dns_cfg.server
                    and self.spec.dns_name and self.spec.ip_address
                ):
                    log("[bold]Creating DNS record...[/bold]")
                    try:
                        from infraforge.dns_client import DNSClient
                        dns = DNSClient.from_config(self.app.config)
                        zone = self.spec.dns_zone or dns_cfg.domain
                        result = dns.ensure_record(
                            self.spec.dns_name, "A",
                            self.spec.ip_address, 3600, zone,
                        )
                        fqdn = f"{self.spec.dns_name}.{zone}"
                        log(f"[green]  \u2713 DNS {result}: {fqdn} -> "
                            f"{self.spec.ip_address}[/green]\n")
                    except Exception as e:
                        log(f"[yellow]  \u26a0 DNS failed: {e}[/yellow]\n")
                else:
                    log("[dim]  DNS: skipped (not configured)[/dim]\n")

                # IPAM reservation
                if ipam_cfg.url and self.spec.ip_address and self.spec.subnet_id:
                    log("[bold]Reserving IP in IPAM...[/bold]")
                    try:
                        from infraforge.ipam_client import IPAMClient
                        ipam = IPAMClient(self.app.config)
                        ipam.create_address(
                            self.spec.ip_address, self.spec.subnet_id,
                            hostname=self.spec.name,
                            description="Created by InfraForge",
                        )
                        log(
                            f"[green]  \u2713 IP {self.spec.ip_address} reserved in "
                            f"{self.spec.subnet_cidr}[/green]\n"
                        )
                    except Exception as e:
                        log(f"[yellow]  \u26a0 IPAM reservation failed: {e}[/yellow]\n")
                else:
                    log("[dim]  IPAM: skipped (not configured)[/dim]\n")

            log("\n[bold green]━━━ Deployment Complete! ━━━[/bold green]")
            deploy_succeeded = True

        except Exception as e:
            log(f"\n[red]Deployment error: {e}[/red]")
            deploy_succeeded = False

        self._deploying = False

        # --- Post-deploy: offer Ansible playbook selection ---
        if deploy_succeeded:
            try:
                playbook_dir = self.app.config.ansible.playbook_dir
                self._available_playbooks = discover_playbooks(playbook_dir)
            except Exception:
                self._available_playbooks = []

            if self._available_playbooks:
                self.app.call_from_thread(self._show_post_deploy_options)
                return

        # No playbooks or deploy failed — go straight to done
        self._deploy_done = True

        def _show_done():
            self._set_hint("[bold green]Done![/bold green]  Press [b]Enter[/b] to return to dashboard")
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

    # ------------------------------------------------------------------
    # Post-deploy: Ansible playbook selection
    # ------------------------------------------------------------------

    def _show_post_deploy_options(self):
        """Switch the UI to the post-deploy playbook selection phase."""
        self._post_deploy = True

        # Clear deploy log and mount WizItem-based selection
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query("#deploy-log"):
            w.remove()

        self._items = []
        self._cursor = 0
        self._build_post_deploy_items()

        self._mount_gen += 1
        gen = self._mount_gen

        scroll = self.query_one("#wizard-content", VerticalScroll)
        header = self.query_one("#wiz-phase-header", Static)
        header.update("[b]Post-Deployment[/b]")

        for idx, item in enumerate(self._items):
            line = Static(
                self._format_line(idx, item),
                markup=True,
                id=f"wiz-line-{gen}-{idx}",
                classes="wiz-line",
            )
            scroll.mount(line)

        nav = self._nav_indices()
        if nav:
            self._cursor = nav[0]
        self._refresh_lines()

        self._set_hint("Arrow keys navigate  |  Enter to select  |  Escape to skip")

        try:
            btn_next = self.query_one("#btn-next", Button)
            btn_next.label = "Skip"
            btn_next.disabled = False
            btn_next.variant = "default"
            btn_cancel = self.query_one("#btn-cancel", Button)
            btn_cancel.add_class("hidden")
        except Exception:
            pass

        # Hide step indicators during post-deploy
        for i in range(len(WIZARD_STEPS)):
            try:
                ind = self.query_one(f"#step-ind-{i}")
                ind.set_classes("wizard-step -completed")
            except Exception:
                pass

    def _build_post_deploy_items(self):
        """Build WizItem list for the post-deploy playbook selection."""
        items = self._items

        items.append(WizItem(
            kind="header",
            label="RUN AN ANSIBLE PLAYBOOK AGAINST THE NEW VM?",
        ))

        vm_ip = self.spec.ip_address or "(no IP)"
        vm_name = self.spec.name or "(unnamed)"
        items.append(WizItem(
            kind="info",
            label=f"Target: [b]{vm_name}[/b]  ({vm_ip})",
        ))
        items.append(WizItem(kind="info", label=""))

        items.append(WizItem(kind="header", label="AVAILABLE PLAYBOOKS"))
        for pb in self._available_playbooks:
            desc = pb.description if pb.description != pb.name else ""
            task_info = f"{pb.task_count} tasks"
            if desc:
                lbl = f"{pb.filename}  [dim]{desc}  ({task_info})[/dim]"
            else:
                lbl = f"{pb.filename}  [dim]({task_info})[/dim]"
            items.append(WizItem(
                kind="option",
                label=lbl,
                key=str(pb.path),
                group="post_deploy_playbook",
            ))

        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(kind="header", label="SKIP"))
        items.append(WizItem(
            kind="option",
            label="Skip  [dim]Don't run any playbook[/dim]",
            key="__skip__",
            group="post_deploy_playbook",
        ))

    @work(thread=True)
    def _run_selected_playbook(self, playbook_path_str: str):
        """Run the selected Ansible playbook against the new VM."""
        self._post_deploy = False
        self._post_deploy_running = True

        playbook_path = Path(playbook_path_str)
        ip = self.spec.ip_address
        if not ip:
            self._post_deploy_running = False
            self.app.call_from_thread(self._finish_post_deploy)
            return

        def _show_run_log():
            for w in self.query(".wiz-line"):
                w.remove()
            for w in self.query("#deploy-log"):
                w.remove()
            scroll = self.query_one("#wizard-content", VerticalScroll)
            scroll.mount(RichLog(markup=True, id="deploy-log"))
            self.query_one("#wiz-phase-header", Static).update(
                f"[b]Running: {playbook_path.name}[/b]"
            )
            try:
                btn = self.query_one("#btn-next", Button)
                btn.label = "Running..."
                btn.disabled = True
            except Exception:
                pass
            self._set_hint("Ansible playbook running...")

        self.app.call_from_thread(_show_run_log)

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        log(f"[bold]━━━ Ansible Playbook: {playbook_path.name} ━━━[/bold]\n")
        log(f"[bold]Target:[/bold] {self.spec.name} ({ip})")

        # Determine the SSH private key to use
        private_key_path = self._find_ssh_private_key()
        if private_key_path:
            log(f"[bold]SSH Key:[/bold] {private_key_path}\n")
        else:
            log("[dim]SSH Key: none (using ssh-agent or default)[/dim]\n")

        # Build the ansible-playbook command
        cmd = [
            "ansible-playbook",
            str(playbook_path),
            "-i", f"{ip},",
            "-u", "root",
        ]
        if private_key_path:
            cmd.extend(["--private-key", private_key_path])

        log(f"[dim]$ {' '.join(cmd)}[/dim]\n")

        run_env = {
            **os.environ,
            "ANSIBLE_FORCE_COLOR": "false",
            "ANSIBLE_HOST_KEY_CHECKING": "False",
        }

        # Write log to file
        log_dir = playbook_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file_path = log_dir / f"{playbook_path.stem}_{timestamp}.log"

        exit_code = 1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=run_env,
            )

            with open(log_file_path, "w") as lf:
                lf.write(f"# InfraForge Post-Deploy Ansible Run\n")
                lf.write(f"# Command: {' '.join(cmd)}\n")
                lf.write(f"# Target: {self.spec.name} ({ip})\n")
                lf.write(f"# Started: {datetime.now().isoformat()}\n\n")

                for line in proc.stdout:
                    lf.write(line)
                    lf.flush()
                    # Escape Rich markup in raw ansible output
                    safe_line = line.rstrip("\n").replace("[", "\\[")
                    log(safe_line)

                proc.wait()
                exit_code = proc.returncode
                lf.write(f"\n# Exit code: {exit_code}\n")

        except FileNotFoundError:
            log("\n[red]ansible-playbook not found. Install Ansible first.[/red]")
        except Exception as e:
            log(f"\n[red]Error running playbook: {e}[/red]")

        if exit_code == 0:
            log(f"\n[bold green]Playbook completed successfully (exit code 0)[/bold green]")
        else:
            log(f"\n[bold red]Playbook finished with exit code {exit_code}[/bold red]")
        log(f"[dim]Log saved to {log_file_path}[/dim]")

        self._post_deploy_running = False
        self._deploy_done = True

        def _show_done():
            self._set_hint(
                "[bold green]Done![/bold green]  Press [b]Enter[/b] to return to dashboard"
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

    def _finish_post_deploy(self):
        """Skip playbook selection and go straight to done."""
        self._post_deploy = False
        self._deploy_done = True
        self._set_hint(
            "[bold green]Done![/bold green]  Press [b]Enter[/b] to return to dashboard"
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

    def _find_ssh_private_key(self) -> str:
        """Find the SSH private key corresponding to the selected public key.

        Returns the path to the private key file, or empty string if none found.
        """
        pubkey_content = self.spec.ssh_keys
        if not pubkey_content:
            return ""

        # Check ~/.ssh/ for matching pub key
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists():
            for pub in ssh_dir.glob("*.pub"):
                try:
                    if pub.read_text().strip() == pubkey_content:
                        priv = pub.with_suffix("")
                        if priv.exists():
                            return str(priv)
                except Exception:
                    pass

        # Check infraforge ssh_keys directory
        infra_keys_dir = Path.home() / ".config" / "infraforge" / "ssh_keys"
        if infra_keys_dir.exists():
            for pub in infra_keys_dir.glob("*.pub"):
                try:
                    if pub.read_text().strip() == pubkey_content:
                        priv = pub.with_suffix("")
                        if priv.exists():
                            return str(priv)
                except Exception:
                    pass

        return ""
