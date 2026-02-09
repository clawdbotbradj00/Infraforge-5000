"""DNS Server Wizard — guided provisioning of a BIND9 DNS server VM.

8-step wizard: Overview, VM Identity, Network, Resources, DNS Config, Access,
Review & Deploy, Completion.

Uses the same cursor-based keyboard navigation pattern as NewVMScreen:
  - WizItem lines rendered as Static widgets
  - Arrow keys navigate, Space/Enter activate
  - Hidden Input for inline text editing
  - @work(thread=True) for async operations
  - self.app.call_from_thread() for UI updates from workers
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import uuid
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


DNS_WIZARD_STEPS = [
    "Overview",
    "Identity",
    "Network",
    "Resources",
    "DNS Config",
    "Access",
    "Review",
    "Complete",
]


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


class DNSServerWizardScreen(Screen):
    """Guided wizard for provisioning a BIND9 DNS server VM."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back/Cancel", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._step = 0
        self.spec = NewVMSpec()
        self._cursor = 0
        self._items: list[WizItem] = []
        self._editing = False
        self._editing_key = ""
        # Data caches — avoid shadowing Widget._nodes
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
        self._data_loaded = False
        self._mount_gen = 0
        # DNS server-specific config
        self._dns_zones_str = ""
        self._dns_forwarders_str = "8.8.8.8, 1.1.1.1"
        self._dns_allow_recursion = True
        self._dns_allow_query = "any"
        # Auth
        self._auth_method = "ssh_key"  # "ssh_key" or "password"
        self._root_password = ""
        # Deployment results
        self._deploy_results: dict = {}
        self._deploy_log_path: Optional[Path] = None
        self._show_private_key: bool = False
        self._ip_from_ipam: bool = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="wizard-container"):
            with Horizontal(id="wizard-progress"):
                for i, name in enumerate(DNS_WIZARD_STEPS):
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
        defaults = self.app.config.defaults
        self.spec.cpu_cores = 2
        self.spec.memory_mb = 2048
        self.spec.disk_gb = 20
        self.spec.storage = defaults.storage
        self.spec.network_bridge = defaults.network_bridge
        self.spec.start_after_create = True
        self.spec.vm_type = VMType.LXC
        if self.app.config.dns.domain:
            self.spec.dns_domain = self.app.config.dns.domain
        dns_zones = self.app.config.dns.zones
        if dns_zones:
            self.spec.dns_zone = dns_zones[0]
            self.spec.dns_domain = dns_zones[0]
            self._dns_zones_str = ", ".join(dns_zones)

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
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self.app.pop_screen()
                return

            nav = self._nav_indices()
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
                self._activate_completion_item()
            return

        if self._deploying:
            return

        nav = self._nav_indices()
        if not nav:
            if event.key == "enter":
                event.prevent_default()
                event.stop()
                self._go_next()
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
    # Completion step actions
    # ------------------------------------------------------------------

    def _activate_completion_item(self):
        """Handle Enter/Space on items in the completion step."""
        if self._cursor < 0 or self._cursor >= len(self._items):
            return
        item = self._items[self._cursor]
        if item.kind != "option":
            return

        if item.key == "show_key":
            self._show_private_key = not self._show_private_key
            self._render_step()
        elif item.key == "copy_key":
            key_content = self._read_private_key()
            if key_content:
                try:
                    self.app.copy_to_clipboard(key_content)
                    self.notify(
                        "Private key copied to clipboard",
                        title="Copied",
                    )
                except Exception:
                    self.notify(
                        "Clipboard not available in this terminal",
                        severity="warning",
                    )
            else:
                self.notify("Could not read private key", severity="error")
        elif item.key == "done":
            self.app.pop_screen()

    def _get_private_key_path(self) -> str:
        """Return the path to the private key used for this deployment."""
        for label, pubkey in self._ssh_keys:
            if pubkey == self.spec.ssh_keys:
                key_file = Path.home() / ".ssh" / label.replace(".pub", "")
                if key_file.exists():
                    return str(key_file)
        return ""

    def _read_private_key(self) -> str:
        """Read the private key file contents."""
        key_path = self._get_private_key_path()
        if not key_path:
            return ""
        try:
            return Path(key_path).read_text().strip()
        except Exception:
            return ""

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
        if item.meta.get("password"):
            inp.password = True
        else:
            inp.password = False
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
        inp.password = False
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
        inp.password = False
        inp.blur()
        self._update_step_hint()

    # ------------------------------------------------------------------
    # Apply values
    # ------------------------------------------------------------------

    def _apply_selection(self, item: WizItem):
        if item.group == "node":
            new_node = item.key
            if new_node != self.spec.node:
                self.spec.node = new_node
                self.spec.template = ""
                self.spec.template_volid = ""
                self.spec.template_vmid = None
        elif item.group == "template":
            m = item.meta
            if m.get("type") == "ct":
                self.spec.vm_type = VMType.LXC
                self.spec.template_volid = m.get("volid", "")
                self.spec.template = m.get("name", "")
                self.spec.template_vmid = None
            elif m.get("type") == "vm":
                self.spec.vm_type = VMType.QEMU
                self.spec.template = m.get("name", "")
                self.spec.template_vmid = m.get("vmid")
                self.spec.template_volid = ""
            elif m.get("type") == "cloud_image":
                self.spec.vm_type = VMType.QEMU
                self.spec.template_volid = m.get("volid", "")
                self.spec.template = m.get("name", "")
                self.spec.template_vmid = None
        elif item.group == "subnet":
            self._apply_subnet_selection(item)
        elif item.group == "ip":
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
        elif item.group == "auth_method":
            self._auth_method = item.key

    def _apply_toggle(self, item: WizItem):
        if item.key == "allow_recursion":
            self._dns_allow_recursion = item.selected
        elif item.key == "start_after_create":
            self.spec.start_after_create = item.selected
        elif item.key == "unprivileged":
            self.spec.unprivileged = item.selected

    def _apply_input_value(self, item: WizItem):
        key = item.key
        val = item.value.strip()

        if key == "hostname":
            hostname = val.lower()
            self.spec.name = hostname
            self.spec.dns_name = hostname
            if self._dns_check_timer:
                self._dns_check_timer.stop()
            if hostname:
                self._dns_check_timer = self.set_timer(0.8, self._trigger_dns_check)
        elif key == "domain":
            self.spec.dns_domain = val
            self.spec.dns_zone = val
        elif key == "manual_ip":
            self.spec.ip_address = val
            self._ip_from_ipam = False
        elif key == "subnet_cidr":
            self.spec.subnet_cidr = val
            # Auto-derive gateway
            try:
                net = ipaddress.IPv4Network(val, strict=False)
                self.spec.gateway = str(list(net.hosts())[0])
                self.spec.subnet_mask = net.prefixlen
            except (ValueError, IndexError):
                pass
        elif key == "gateway":
            self.spec.gateway = val
        elif key == "vlan_tag":
            self.spec.vlan_tag = int(val) if val else None
            self.app.preferences.new_vm.vlan_tag = val
            self.app.preferences.save()
        elif key == "dns_servers":
            self.spec.dns_servers = val
            self.app.preferences.new_vm.dns_servers = val
            self.app.preferences.save()
        elif key == "cpu_cores":
            try:
                self.spec.cpu_cores = int(val) if val else 2
            except ValueError:
                pass
        elif key == "memory_mb":
            try:
                self.spec.memory_mb = int(val) if val else 2048
            except ValueError:
                pass
        elif key == "disk_gb":
            try:
                self.spec.disk_gb = int(val) if val else 20
            except ValueError:
                pass
        elif key == "dns_zones":
            self._dns_zones_str = val
        elif key == "dns_forwarders":
            self._dns_forwarders_str = val
        elif key == "dns_allow_query":
            self._dns_allow_query = val
        elif key == "root_password":
            self._root_password = val
        elif key == "ssh_key_paste":
            self.spec.ssh_keys = val

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

        builders = [
            self._build_overview_items,       # 0
            self._build_identity_items,       # 1
            self._build_network_items,        # 2
            self._build_resources_items,      # 3
            self._build_dns_config_items,     # 4
            self._build_access_items,         # 5
            self._build_review_items,         # 6
            self._build_completion_items,     # 7
        ]
        builders[self._step]()

        # Update step indicators
        for i in range(len(DNS_WIZARD_STEPS)):
            ind = self.query_one(f"#step-ind-{i}")
            cls = "wizard-step"
            if i < self._step:
                cls += " -completed"
            elif i == self._step:
                cls += " -active"
            ind.set_classes(cls)

        # Update nav buttons
        btn_next = self.query_one("#btn-next", Button)
        if self._step == 6:
            btn_next.label = "Deploy"
            btn_next.variant = "success"
        elif self._step == 7:
            btn_next.label = "Done"
            btn_next.variant = "success"
        elif self._step == 0:
            btn_next.label = "Begin"
            btn_next.variant = "primary"
        else:
            btn_next.label = "Next"
            btn_next.variant = "primary"

        self._mount_items()

        nav = self._nav_indices()
        if nav:
            self._cursor = nav[0]
        self._refresh_lines()
        self._update_step_hint()

        # If step is already valid, show button as ready
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
        header.update(
            f"[b]Step {self._step + 1}: {DNS_WIZARD_STEPS[self._step]}[/b]"
        )

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

        if item.kind == "separator":
            return " [dim]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/dim]"

        if item.kind == "header":
            return f" [bold cyan]{item.label}[/bold cyan]"

        if item.kind == "info":
            return f"   [dim]{item.label}[/dim]"

        cur = "[bold]>[/bold]" if is_cur else " "

        if item.kind == "option":
            mark = "[green]\u25cf[/green]" if item.selected else "[dim]\u25cb[/dim]"
            lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
            # Highlight auto-suggested IP in yellow
            if (
                item.group == "ip"
                and item.selected
                and self._available_ips
                and item.key == self._available_ips[0]
            ):
                if is_cur:
                    lbl = f"[bold yellow]{item.label}[/bold yellow]  [dim yellow]\u2190 suggested[/dim yellow]"
                else:
                    lbl = f"[yellow]{item.label}[/yellow]  [dim yellow]\u2190 suggested[/dim yellow]"
            return f" {cur} {mark}  {lbl}"

        if item.kind == "input":
            val = item.value if item.value else (
                f"[dim]{item.meta.get('placeholder', '...')}[/dim]"
            )
            if item.meta.get("password") and item.value:
                val = "*" * min(len(item.value), 12)
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
            self.query_one(
                f"#wiz-line-{gen}-{self._cursor}", Static
            ).scroll_visible()
        except Exception:
            pass

    def _maybe_focus_next(self):
        valid, _ = self._validate_step()
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

    def _update_step_hint(self):
        hints = {
            0: "Press Enter or Next to begin the wizard",
            1: "Space to edit  |  Enter confirm  |  Backspace back",
            2: "Space select subnet/IP  |  Enter confirm  |  Backspace back",
            3: "Space edit/select  |  Enter confirm  |  Backspace back",
            4: "Space edit/toggle  |  Enter confirm  |  Backspace back",
            5: "Space select key  |  Enter confirm  |  Backspace back",
            6: "Enter to deploy  |  Backspace back",
            7: "Enter=Activate  Escape=Dashboard",
        }
        self._set_hint(hints.get(self._step, ""))

    # ------------------------------------------------------------------
    # Step 0: Overview
    # ------------------------------------------------------------------

    def _build_overview_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="DNS SERVER WIZARD"))
        items.append(WizItem(
            kind="info",
            label="This wizard will create an Ubuntu VM and configure it as",
        ))
        items.append(WizItem(
            kind="info",
            label="a production-ready BIND9 DNS server.",
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(kind="header", label="WHAT WILL BE PROVISIONED"))
        items.append(WizItem(
            kind="info",
            label="[bold]1.[/bold]  Ubuntu VM via Terraform on your Proxmox cluster",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]2.[/bold]  BIND9 DNS server installed and configured via Ansible",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]3.[/bold]  Forward and reverse DNS zones created",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]4.[/bold]  UFW firewall configured (SSH + DNS ports)",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]5.[/bold]  DNS/IPAM records registered (if configured)",
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(kind="header", label="REQUIREMENTS"))
        items.append(WizItem(
            kind="info",
            label="  - Terraform CLI installed",
        ))
        items.append(WizItem(
            kind="info",
            label="  - Ansible CLI installed",
        ))
        items.append(WizItem(
            kind="info",
            label="  - An Ubuntu/Debian VM template on Proxmox",
        ))
        items.append(WizItem(
            kind="info",
            label="  - SSH key for VM access",
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="info",
            label="[bold]Press Enter or click Next to begin.[/bold]",
        ))

    # ------------------------------------------------------------------
    # Step 1: VM Identity
    # ------------------------------------------------------------------

    def _build_identity_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="HOSTNAME"))
        items.append(WizItem(
            kind="input", label="Hostname", key="hostname",
            value=self.spec.name,
            meta={"placeholder": "e.g. ns1"},
        ))

        if self.spec.dns_name:
            if self._dns_check_result is not None:
                if self._dns_check_result:
                    ips = ", ".join(self._dns_check_result)
                    items.append(WizItem(
                        kind="info",
                        label=f"[yellow]DNS exists: "
                              f"{self.spec.dns_name} -> {ips}[/yellow]",
                    ))
                else:
                    items.append(WizItem(
                        kind="info",
                        label="[green]Available - no existing DNS record[/green]",
                    ))

        items.append(WizItem(kind="header", label="DOMAIN"))
        items.append(WizItem(
            kind="input", label="Domain", key="domain",
            value=self.spec.dns_domain,
            meta={"placeholder": "e.g. easypl.net"},
        ))

        zone = self.spec.dns_zone or self.spec.dns_domain
        if self.spec.dns_name and zone:
            fqdn = f"{self.spec.dns_name}.{zone}"
            items.append(WizItem(
                kind="info", label=f"FQDN: [b]{fqdn}[/b]",
            ))

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
            if self._data_loaded:
                items.append(WizItem(
                    kind="info", label="[yellow]No nodes available[/yellow]",
                ))
            else:
                items.append(WizItem(
                    kind="info", label="Loading nodes...",
                ))

    # ------------------------------------------------------------------
    # Step 2: Network
    # ------------------------------------------------------------------

    def _build_network_items(self):
        items = self._items

        # IPAM subnets if available
        if self._subnets:
            items.append(WizItem(kind="header", label="IPAM SUBNETS"))
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
                ))

        if self._available_ips:
            items.append(WizItem(kind="header", label="AVAILABLE IPs"))
            for ip in self._available_ips[:15]:
                items.append(WizItem(
                    kind="option", label=ip, key=ip, group="ip",
                    selected=self.spec.ip_address == ip,
                ))

        items.append(WizItem(kind="separator", label=""))

        items.append(WizItem(kind="header", label="MANUAL OVERRIDES"))
        items.append(WizItem(
            kind="input", label="IP Address", key="manual_ip",
            value=self.spec.ip_address,
            meta={"placeholder": "e.g. 10.0.200.10"},
        ))

        items.append(WizItem(kind="header", label="SUBNET / CIDR"))
        items.append(WizItem(
            kind="input", label="Subnet/CIDR", key="subnet_cidr",
            value=self.spec.subnet_cidr,
            meta={"placeholder": "e.g. 10.0.200.0/24"},
        ))

        items.append(WizItem(kind="header", label="GATEWAY"))
        items.append(WizItem(
            kind="input", label="Gateway", key="gateway",
            value=self.spec.gateway,
            meta={"placeholder": "e.g. 10.0.200.1 (auto-derived from subnet)"},
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

    # ------------------------------------------------------------------
    # Step 3: Resources
    # ------------------------------------------------------------------

    def _build_resources_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="COMPUTE"))
        items.append(WizItem(
            kind="input", label="CPU Cores", key="cpu_cores",
            value=str(self.spec.cpu_cores),
            meta={"placeholder": "2"},
        ))
        items.append(WizItem(
            kind="input", label="Memory (MB)", key="memory_mb",
            value=str(self.spec.memory_mb),
            meta={"placeholder": "2048"},
        ))
        items.append(WizItem(
            kind="input", label="Disk (GB)", key="disk_gb",
            value=str(self.spec.disk_gb),
            meta={"placeholder": "20"},
        ))

        items.append(WizItem(kind="header", label="TEMPLATE"))
        items.append(WizItem(
            kind="info",
            label="[dim]Select an Ubuntu or Debian template for the DNS server.[/dim]",
        ))

        selected_node = self.spec.node

        # Cloud images (QEMU-bootable .img/.qcow2 from ISO storage)
        cloud_images = [
            t for t in self._templates
            if t.template_type == TemplateType.ISO
            and t.node == selected_node
            and any(t.name.lower().endswith(ext) for ext in ('.img', '.qcow2'))
            and self._is_ubuntu_or_debian(t.name)
        ]

        # Filter to Ubuntu/Debian VM templates only
        vm_templates = [
            t for t in self._templates
            if t.template_type == TemplateType.VM
            and t.node == selected_node
            and self._is_ubuntu_or_debian(t.name)
        ]

        if cloud_images:
            items.append(WizItem(
                kind="header",
                label=f"QEMU CLOUD IMAGES  [dim]on {selected_node}[/dim]",
            ))
            for t in cloud_images:
                lbl = t.name
                if t.storage:
                    lbl += f"  [dim]({t.storage}  {t.size_display})[/dim]"
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=f"cloud:{t.volid or t.name}", group="template",
                    selected=(
                        self.spec.template_volid == (t.volid or t.name)
                        and self.spec.vm_type == VMType.QEMU
                    ),
                    meta={"type": "cloud_image", "volid": t.volid or t.name,
                          "name": t.name},
                ))

        if vm_templates:
            items.append(WizItem(
                kind="header",
                label=f"QEMU VM TEMPLATES  [dim]on {selected_node}[/dim]",
            ))
            for t in vm_templates:
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

        # Also show other templates (non-Ubuntu/Debian) in case needed
        other_cloud = [
            t for t in self._templates
            if t.template_type == TemplateType.ISO
            and t.node == selected_node
            and any(t.name.lower().endswith(ext) for ext in ('.img', '.qcow2'))
            and not self._is_ubuntu_or_debian(t.name)
        ]
        all_vm = [
            t for t in self._templates
            if t.template_type == TemplateType.VM
            and t.node == selected_node
            and not self._is_ubuntu_or_debian(t.name)
        ]

        if other_cloud or all_vm:
            items.append(WizItem(
                kind="header",
                label="OTHER TEMPLATES",
            ))
            for t in other_cloud:
                lbl = t.name
                if t.storage:
                    lbl += f"  [dim]({t.storage}  {t.size_display})[/dim]"
                items.append(WizItem(
                    kind="option", label=lbl,
                    key=f"cloud:{t.volid or t.name}", group="template",
                    selected=(
                        self.spec.template_volid == (t.volid or t.name)
                        and self.spec.vm_type == VMType.QEMU
                    ),
                    meta={"type": "cloud_image", "volid": t.volid or t.name,
                          "name": t.name},
                ))
            for t in all_vm:
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

        if not cloud_images and not vm_templates and not other_cloud and not all_vm:
            if self._data_loaded:
                items.append(WizItem(
                    kind="info",
                    label=f"[yellow]No templates found on "
                          f"{selected_node}[/yellow]",
                ))
                items.append(WizItem(
                    kind="info",
                    label="[dim]  Download an Ubuntu template via pveam, or "
                          "select a different node.[/dim]",
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

        items.append(WizItem(kind="header", label="STORAGE"))
        node_storages = [
            s for s in self._storages
            if s.node == self.spec.node or s.shared
        ]
        if node_storages:
            seen: set[str] = set()
            for s in node_storages:
                if s.storage not in seen:
                    seen.add(s.storage)
                    lbl = (
                        f"{s.storage}  [dim]({s.storage_type}  "
                        f"{s.avail_display} free)[/dim]"
                    )
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
            items.append(WizItem(
                kind="info", label="Loading storage...",
            ))

    # ------------------------------------------------------------------
    # Step 4: DNS Server Config
    # ------------------------------------------------------------------

    def _build_dns_config_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="ZONES TO MANAGE"))
        items.append(WizItem(
            kind="info",
            label="[dim]Comma-separated list of DNS zones this server will be "
                  "authoritative for.[/dim]",
        ))
        items.append(WizItem(
            kind="input", label="Zones", key="dns_zones",
            value=self._dns_zones_str,
            meta={"placeholder": "e.g. example.com, lab.local"},
        ))

        items.append(WizItem(kind="header", label="FORWARDERS"))
        items.append(WizItem(
            kind="info",
            label="[dim]Upstream DNS servers for recursive queries.[/dim]",
        ))
        items.append(WizItem(
            kind="input", label="Forwarders", key="dns_forwarders",
            value=self._dns_forwarders_str,
            meta={"placeholder": "e.g. 8.8.8.8, 1.1.1.1"},
        ))

        items.append(WizItem(kind="header", label="RECURSION"))
        items.append(WizItem(
            kind="toggle", label="Allow recursion",
            key="allow_recursion",
            selected=self._dns_allow_recursion,
        ))

        items.append(WizItem(kind="header", label="QUERY ACCESS"))
        items.append(WizItem(
            kind="info",
            label="[dim]ACL for which clients can query this server.[/dim]",
        ))
        items.append(WizItem(
            kind="input", label="Allow query from", key="dns_allow_query",
            value=self._dns_allow_query,
            meta={"placeholder": "e.g. any, 10.0.0.0/8, localhost"},
        ))

    # ------------------------------------------------------------------
    # Step 5: Access
    # ------------------------------------------------------------------

    def _build_access_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="AUTHENTICATION METHOD"))
        items.append(WizItem(
            kind="option", label="SSH Key",
            key="ssh_key", group="auth_method",
            selected=self._auth_method == "ssh_key",
        ))
        items.append(WizItem(
            kind="option", label="Password",
            key="password", group="auth_method",
            selected=self._auth_method == "password",
        ))

        if self._auth_method == "ssh_key":
            items.append(WizItem(kind="header", label="SSH KEYS"))
            if self._ssh_keys:
                for label, pubkey in self._ssh_keys:
                    short = (
                        pubkey[:60] + "..." if len(pubkey) > 60 else pubkey
                    )
                    items.append(WizItem(
                        kind="option",
                        label=f"{label}  [dim]{short}[/dim]",
                        key=label, group="ssh_key",
                        selected=self.spec.ssh_keys == pubkey,
                        meta={"pubkey": pubkey},
                    ))
            else:
                items.append(WizItem(
                    kind="info",
                    label="No SSH keys found in ~/.ssh/",
                ))

            items.append(WizItem(kind="header", label="PASTE KEY"))
            items.append(WizItem(
                kind="input", label="SSH Public Key", key="ssh_key_paste",
                value="" if (
                    self._ssh_keys and self.spec.ssh_keys
                ) else self.spec.ssh_keys,
                meta={"placeholder": "ssh-ed25519 AAAA... or ssh-rsa AAAA..."},
            ))
        else:
            items.append(WizItem(kind="header", label="ROOT PASSWORD"))
            items.append(WizItem(
                kind="input", label="Password", key="root_password",
                value=self._root_password,
                meta={"placeholder": "Enter root password", "password": True},
            ))

    # ------------------------------------------------------------------
    # Step 6: Review & Deploy
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
        zone = s.dns_zone or s.dns_domain
        fqdn = f"{s.dns_name}.{zone}" if s.dns_name and zone else "(none)"
        auth = "SSH Key" if self._auth_method == "ssh_key" else "Password"
        mem_gb = s.memory_mb / 1024
        ip_display = f"{ip}/{s.subnet_mask}" if s.ip_address else ip
        vlan_display = str(s.vlan_tag) if s.vlan_tag else "None"

        items.append(WizItem(kind="header", label="VM CONFIGURATION"))
        items.append(WizItem(
            kind="info", label=f"Hostname:     [b]{name}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"FQDN:         [b]{fqdn}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Node:         [b]{node}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Type:         [b]{vm_type}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Template:     [b]{template}[/b]",
        ))

        items.append(WizItem(kind="header", label="RESOURCES"))
        items.append(WizItem(
            kind="info",
            label=f"CPU:          [b]{s.cpu_cores} cores[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Memory:       [b]{s.memory_mb} MB "
                  f"({mem_gb:.1f} GB)[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Disk:         [b]{s.disk_gb} GB[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Storage:      [b]{s.storage}[/b]",
        ))

        items.append(WizItem(kind="header", label="NETWORK"))
        items.append(WizItem(
            kind="info", label=f"IP Address:   [b]{ip_display}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Gateway:      [b]{gw}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"VLAN:         [b]{vlan_display}[/b]",
        ))
        dns_svr = s.dns_servers or "1.1.1.1,8.8.8.8"
        items.append(WizItem(
            kind="info", label=f"DNS Servers:  [b]{dns_svr}[/b]",
        ))

        items.append(WizItem(kind="header", label="DNS SERVER"))
        zones = [
            z.strip() for z in self._dns_zones_str.split(",") if z.strip()
        ]
        items.append(WizItem(
            kind="info",
            label=f"Zones:        [b]{', '.join(zones) or '(none)'}[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Forwarders:   [b]{self._dns_forwarders_str}[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Recursion:    [b]"
                  f"{'Enabled' if self._dns_allow_recursion else 'Disabled'}"
                  f"[/b]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Allow query:  [b]{self._dns_allow_query}[/b]",
        ))

        items.append(WizItem(kind="header", label="ACCESS"))
        items.append(WizItem(
            kind="info", label=f"Auth method:  [b]{auth}[/b]",
        ))
        if self._auth_method == "ssh_key" and s.ssh_keys:
            short = s.ssh_keys[:50] + "..." if len(s.ssh_keys) > 50 else s.ssh_keys
            items.append(WizItem(
                kind="info", label=f"SSH Key:      [dim]{short}[/dim]",
            ))

        items.append(WizItem(kind="header", label="DEPLOYMENT PLAN"))
        items.append(WizItem(
            kind="info",
            label="[bold]1.[/bold]  Terraform: Create VM on Proxmox",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]2.[/bold]  Wait for VM to become accessible via SSH",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]3.[/bold]  Ansible: Install and configure BIND9",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]4.[/bold]  Register DNS records (if configured)",
        ))
        items.append(WizItem(
            kind="info",
            label="[bold]5.[/bold]  Register IP in IPAM (if configured)",
        ))

    # ------------------------------------------------------------------
    # Step 7: Completion
    # ------------------------------------------------------------------

    def _build_completion_items(self):
        items = self._items
        r = self._deploy_results

        if r.get("success"):
            items.append(WizItem(
                kind="header", label="DEPLOYMENT SUCCESSFUL",
            ))
        else:
            items.append(WizItem(
                kind="header", label="DEPLOYMENT RESULTS",
            ))

        # VM info
        items.append(WizItem(kind="header", label="VM STATUS"))
        items.append(WizItem(
            kind="info",
            label=f"Status:     [bold green]Running[/bold green]"
            if r.get("vm_created") else
            f"Status:     [bold red]Creation failed[/bold red]",
        ))
        ip = self.spec.ip_address or "(unknown)"
        items.append(WizItem(
            kind="info", label=f"IP Address: [b]{ip}[/b]",
        ))
        items.append(WizItem(
            kind="info", label=f"Node:       [b]{self.spec.node}[/b]",
        ))

        # SSH command
        items.append(WizItem(kind="header", label="SSH ACCESS"))
        key_path = self._get_private_key_path()
        ssh_user = self._detect_ssh_user()
        if self._auth_method == "ssh_key" and key_path:
            items.append(WizItem(
                kind="info",
                label=f"  [bold]ssh -i {key_path} {ssh_user}@{ip}[/bold]",
            ))
        else:
            items.append(WizItem(
                kind="info",
                label=f"  [bold]ssh {ssh_user}@{ip}[/bold]",
            ))
        if self._auth_method == "password":
            items.append(WizItem(
                kind="info",
                label="  [dim]Login with the root password you set[/dim]",
            ))

        # Private key display/copy options
        if self._auth_method == "ssh_key" and key_path:
            items.append(WizItem(kind="info", label=""))
            items.append(WizItem(kind="header", label="PRIVATE KEY"))
            items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]To connect from another machine (e.g. Windows + "
                    f"MobaXterm/PuTTY), you need this private key:[/dim]"
                ),
            ))
            items.append(WizItem(
                kind="info",
                label=f"  [dim]{key_path}[/dim]",
            ))

            if self._show_private_key:
                toggle_label = (
                    "[bold yellow]Hide Private Key[/bold yellow]"
                )
            else:
                toggle_label = (
                    "[bold cyan]Show Private Key[/bold cyan]"
                )
            items.append(WizItem(
                kind="option", label=toggle_label, key="show_key",
            ))
            items.append(WizItem(
                kind="option",
                label="[bold cyan]Copy Private Key to Clipboard[/bold cyan]",
                key="copy_key",
            ))

            if self._show_private_key:
                key_content = self._read_private_key()
                if key_content:
                    items.append(WizItem(kind="info", label=""))
                    items.append(WizItem(
                        kind="info",
                        label="[bold yellow]--- BEGIN PRIVATE KEY ---[/bold yellow]",
                    ))
                    for line in key_content.splitlines():
                        items.append(WizItem(
                            kind="info",
                            label=f"  {line}",
                        ))
                    items.append(WizItem(
                        kind="info",
                        label="[bold yellow]--- END PRIVATE KEY ---[/bold yellow]",
                    ))

        # DNS info
        if r.get("ansible_ok"):
            zones = [
                z.strip()
                for z in self._dns_zones_str.split(",")
                if z.strip()
            ]
            items.append(WizItem(kind="header", label="DNS SERVER INFO"))
            items.append(WizItem(
                kind="info",
                label=f"Zones:      [b]{', '.join(zones)}[/b]",
            ))
            items.append(WizItem(
                kind="info",
                label=f"Listening:  [b]{ip}:53 (TCP/UDP)[/b]",
            ))

            items.append(WizItem(kind="header", label="MANAGEMENT COMMANDS"))
            items.append(WizItem(
                kind="info",
                label="  [bold]systemctl status bind9[/bold]  "
                      "[dim]- Check service status[/dim]",
            ))
            items.append(WizItem(
                kind="info",
                label="  [bold]named-checkconf[/bold]          "
                      "[dim]- Validate BIND9 config[/dim]",
            ))
            items.append(WizItem(
                kind="info",
                label="  [bold]rndc reload[/bold]              "
                      "[dim]- Reload zones without restart[/dim]",
            ))
            for z in zones[:3]:
                items.append(WizItem(
                    kind="info",
                    label=f"  [bold]dig @{ip} {z} SOA[/bold]  "
                          f"[dim]- Test zone {z}[/dim]",
                ))

            items.append(WizItem(kind="header", label="CLIENT CONFIGURATION"))
            items.append(WizItem(
                kind="info",
                label=f"Point clients to [b]{ip}[/b] as their DNS server:",
            ))
            items.append(WizItem(
                kind="info",
                label=f"  /etc/resolv.conf:  [bold]nameserver {ip}[/bold]",
            ))
        elif r.get("vm_created") and not r.get("ansible_ok"):
            items.append(WizItem(kind="header", label="ANSIBLE STATUS"))
            items.append(WizItem(
                kind="info",
                label="[yellow]BIND9 configuration failed. The VM was created "
                      "but Ansible did not complete.[/yellow]",
            ))
            items.append(WizItem(
                kind="info",
                label=f"[dim]You can SSH in and run the playbook "
                      f"manually:[/dim]",
            ))
            items.append(WizItem(
                kind="info",
                label=f"  [bold]ansible-playbook "
                      f"ansible/playbooks/bind9-server.yml "
                      f"-i '{ip},' -u root[/bold]",
            ))

        # DNS/IPAM registration
        if r.get("dns_registered"):
            items.append(WizItem(kind="header", label="DNS REGISTRATION"))
            items.append(WizItem(
                kind="info",
                label=f"[green]DNS record created: "
                      f"{r.get('dns_fqdn', '')} -> {ip}[/green]",
            ))
        if r.get("ipam_registered"):
            items.append(WizItem(kind="header", label="IPAM REGISTRATION"))
            items.append(WizItem(
                kind="info",
                label=f"[green]IP reserved in IPAM: {ip}[/green]",
            ))

        # Deployment log file
        if self._deploy_log_path:
            items.append(WizItem(kind="header", label="DEPLOYMENT LOG"))
            items.append(WizItem(
                kind="info",
                label=f"Log: [b]{self._deploy_log_path}[/b]",
            ))

        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="option",
            label="[bold white on dark_green]  Done — Return to Dashboard  [/bold white on dark_green]",
            key="done",
        ))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_next(self):
        if self._step == 7:
            # Completion step — go back to dashboard
            self.app.pop_screen()
            return

        if self._step < 6:
            valid, msg = self._validate_step()
            if not valid:
                self.notify(msg, severity="error")
                return
            self._step += 1
            self._render_step()
        elif self._step == 6:
            # Deploy
            self._deploy()

    def _go_back(self):
        if self._step > 0 and self._step < 7:
            self._step -= 1
            self._render_step()

    def action_handle_escape(self):
        if self._deploy_done:
            self.app.pop_screen()
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
            # Overview — always valid
            return True, ""

        elif self._step == 1:
            # Identity
            if not self.spec.name:
                return False, "Please enter a hostname"
            if len(self.spec.name) > 1:
                if not re.match(
                    r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$', self.spec.name
                ):
                    return False, (
                        "Hostname: lowercase alphanumeric + hyphens only"
                    )
            elif len(self.spec.name) == 1:
                if not self.spec.name.isalnum():
                    return False, (
                        "Hostname must start with a letter or digit"
                    )
            if not self.spec.dns_domain:
                return False, "Please enter a domain"
            if not self.spec.node:
                return False, "Please select a target node"
            return True, ""

        elif self._step == 2:
            # Network — IP is recommended but not required (DHCP possible)
            return True, ""

        elif self._step == 3:
            # Resources
            if self.spec.cpu_cores < 1:
                return False, "CPU cores must be at least 1"
            if self.spec.memory_mb < 256:
                return False, "Memory must be at least 256 MB"
            if self.spec.disk_gb < 5:
                return False, "Disk must be at least 5 GB for a DNS server"
            if not self.spec.template and not self.spec.template_volid:
                return False, "Please select a template"
            if not self.spec.storage:
                return False, "Please select a storage pool"
            return True, ""

        elif self._step == 4:
            # DNS Config
            zones = [
                z.strip()
                for z in self._dns_zones_str.split(",")
                if z.strip()
            ]
            if not zones:
                return False, (
                    "Please enter at least one DNS zone to manage"
                )
            fwds = [
                f.strip()
                for f in self._dns_forwarders_str.split(",")
                if f.strip()
            ]
            if not fwds:
                return False, (
                    "Please enter at least one forwarder"
                )
            return True, ""

        elif self._step == 5:
            # Access
            if self._auth_method == "ssh_key" and not self.spec.ssh_keys:
                return False, "Please select or paste an SSH public key"
            if self._auth_method == "password" and not self._root_password:
                return False, "Please enter a root password"
            return True, ""

        elif self._step == 6:
            # Review — validate all previous steps
            for prev_step in range(6):
                old_step = self._step
                self._step = prev_step
                valid, msg = self._validate_step()
                self._step = old_step
                if not valid:
                    return False, f"Step {prev_step + 1}: {msg}"
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
        if self._step in (1, 3):
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
                self.app.call_from_thread(self._focus_first_available_ip)
        except Exception:
            self._available_ips = []

    def _focus_first_available_ip(self):
        """Move cursor to the first available IP option after IPs load."""
        if not self._available_ips:
            return
        for i, item in enumerate(self._items):
            if item.group == "ip" and item.key == self._available_ips[0]:
                self._cursor = i
                self._refresh_lines()
                self._scroll_to_cursor()
                break

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
        infra_keys_dir = (
            Path.home() / ".config" / "infraforge" / "ssh_keys"
        )
        if infra_keys_dir.exists():
            for priv in sorted(infra_keys_dir.glob("*_rsa")):
                pub_path = priv.parent / (priv.name + ".pub")
                if not pub_path.exists():
                    continue
                try:
                    content = pub_path.read_text().strip()
                    if content:
                        keys.append(
                            (f"infraforge: {priv.stem}", content)
                        )
                except Exception:
                    pass
        self._ssh_keys = keys

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
            existing = client.lookup_record(
                self.spec.dns_name, "A", zone,
            )
            self._dns_check_result = existing
            if self._step == 1:
                self.app.call_from_thread(self._render_step)
        except Exception:
            self._dns_check_result = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_ubuntu_or_debian(name: str) -> bool:
        """Check if a template name indicates Ubuntu or Debian."""
        lower = name.lower()
        return "ubuntu" in lower or "debian" in lower

    def _detect_ssh_user(self) -> str:
        """Detect the default SSH user based on the template/image name.

        Cloud images ship with a distro-specific default user rather than
        allowing direct root login.  LXC containers and unknown images
        fall back to root.
        """
        name = (self.spec.template or self.spec.template_volid or "").lower()
        if "ubuntu" in name:
            return "ubuntu"
        elif "debian" in name:
            return "debian"
        elif "centos" in name or "rocky" in name or "alma" in name:
            return "cloud-user"
        elif "fedora" in name:
            return "fedora"
        # LXC containers and unknown templates -> root
        return "root"

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------

    @work(thread=True)
    def _deploy(self):
        self._deploying = True
        self._deploy_results = {}
        deploy_start = time.monotonic()

        # --- Set up deployment log file ---
        log_dir = Path.home() / ".config" / "infraforge" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = uuid.uuid4().hex[:6]
        hostname = self.spec.name or "unknown"
        log_filename = f"deploy_{hostname}_{timestamp}_{run_id}.log"
        log_file_path = log_dir / log_filename
        self._deploy_log_path = log_file_path

        # Also create/update a 'latest' symlink for convenience
        latest_link = log_dir / "latest.log"
        try:
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(log_file_path)
        except OSError:
            pass  # symlink creation is best-effort

        lf = open(log_file_path, "w")

        def _strip_markup(text: str) -> str:
            """Remove Rich markup tags for plain-text log output."""
            return re.sub(r'\[/?[^\]]*\]', '', text)

        def log(msg: str):
            # Write plain text to the log file
            clean = _strip_markup(msg)
            lf.write(clean + "\n")
            lf.flush()
            # Write Rich markup to the UI
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
                    "[b]Deploying DNS Server...[/b]"
                )
                self.query_one("#btn-next", Button).disabled = True
                self._set_hint("Deployment in progress...")
            self.app.call_from_thread(_show)

        show_log()
        # Small pause so the UI can mount the RichLog
        time.sleep(0.3)

        # --- Write deployment metadata header to log ---
        lf.write("=" * 60 + "\n")
        lf.write("  InfraForge DNS Server Deployment Log\n")
        lf.write("=" * 60 + "\n")
        lf.write(f"  Run ID:       {run_id}\n")
        lf.write(f"  Timestamp:    {datetime.now().isoformat()}\n")
        lf.write(f"  Hostname:     {hostname}\n")
        lf.write(f"  VM Type:      {self.spec.vm_type.value if self.spec.vm_type else 'N/A'}\n")
        lf.write(f"  Node:         {self.spec.node or 'N/A'}\n")
        lf.write(f"  Template:     {self.spec.template_volid or 'N/A'}\n")
        lf.write(f"  IP Address:   {self.spec.ip_address or 'DHCP'}\n")
        lf.write(f"  CPU Cores:    {self.spec.cpu_cores}\n")
        lf.write(f"  Memory (MB):  {self.spec.memory_mb}\n")
        lf.write(f"  Disk (GB):    {self.spec.disk_gb}\n")
        lf.write(f"  Storage:      {self.spec.storage or 'N/A'}\n")
        lf.write(f"  Bridge:       {self.spec.network_bridge or 'N/A'}\n")
        lf.write(f"  DNS Domain:   {self.spec.dns_domain or 'N/A'}\n")
        lf.write(f"  DNS Zones:    {self._dns_zones_str or 'N/A'}\n")
        lf.write(f"  Forwarders:   {self._dns_forwarders_str or 'N/A'}\n")
        lf.write(f"  Auth Method:  {self._auth_method}\n")
        lf.write(f"  Log File:     {log_file_path}\n")
        lf.write("=" * 60 + "\n\n")
        lf.flush()

        try:
            from infraforge.terraform_manager import TerraformManager
            tf = TerraformManager(self.app.config)

            # ==============================================================
            # Phase 1: Pre-flight checks
            # ==============================================================
            log("[bold]============================================[/bold]")
            log("[bold]  Phase 1: Pre-flight Checks[/bold]")
            log("[bold]============================================[/bold]\n")

            log("[bold]Checking Terraform CLI...[/bold]")
            installed, version = tf.check_terraform_installed()
            if not installed:
                log(f"[red]  Terraform not found: {version}[/red]")
                log("[yellow]  Install: "
                    "https://developer.hashicorp.com/terraform/install"
                    "[/yellow]")
                self._deploying = False
                self._show_deploy_done()
                return
            log(f"[green]  {version}[/green]\n")

            log("[bold]Checking Ansible CLI...[/bold]")
            ansible_ok = self._check_ansible_installed()
            if not ansible_ok:
                log("[red]  ansible-playbook not found[/red]")
                log("[yellow]  Install: pip install ansible  |  "
                    "apt install ansible[/yellow]")
                self._deploying = False
                self._show_deploy_done()
                return
            log("[green]  ansible-playbook found[/green]\n")

            log("[bold]Validating Proxmox environment...[/bold]")
            checks = tf.validate_pre_deploy(self.spec, log_fn=log)
            all_ok = True
            for name, passed, detail in checks:
                if passed:
                    log(f"[green]  {name}[/green] -- {detail}")
                else:
                    log(f"[red]  {name}[/red]")
                    for dline in detail.split("\n"):
                        log(f"[red]    {dline}[/red]")
                    all_ok = False
            if not all_ok:
                log("\n[red]Pre-flight failed. Fix issues and retry.[/red]")
                self._deploying = False
                self._show_deploy_done()
                return
            log("")

            # ==============================================================
            # Phase 2: Terraform deployment
            # ==============================================================
            log("[bold]============================================[/bold]")
            log("[bold]  Phase 2: Terraform Deployment[/bold]")
            log("[bold]============================================[/bold]\n")

            # API credentials
            log("[bold]Setting up Terraform API credentials...[/bold]")
            pve_cfg = self.app.config.proxmox
            if pve_cfg.auth_method == "password":
                token_id, token_secret = "", ""
                log(f"[green]  Using password auth "
                    f"({pve_cfg.user})[/green]\n")
            else:
                token_id, token_secret, token_msg = (
                    tf.ensure_terraform_token()
                )
                if token_id and token_secret:
                    log(f"[green]  {token_msg}[/green]\n")
                else:
                    log(f"[yellow]  {token_msg}[/yellow]")
                    log("[dim]    Falling back to password auth[/dim]\n")
                    token_id, token_secret = "", ""

            # Ensure the spec has DNS server tag
            self.spec.description = (
                "BIND9 DNS Server - provisioned by InfraForge"
            )
            self.spec.tags = "dns,bind9,infraforge"

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
                log("[red]Provider mirror failed![/red]")
                if output:
                    err_title, guidance = tf.parse_terraform_error(output)
                    if guidance:
                        log(f"[yellow]{err_title}[/yellow]")
                        for gline in guidance.split("\n"):
                            log(f"[yellow]{gline}[/yellow]")
                self._deploying = False
                self._show_deploy_done()
                return
            log("[green]  Providers cached[/green]\n")

            log("[bold]Running terraform init...[/bold]")
            ok, output = tf.terraform_init(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-5:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                log("[red]terraform init failed![/red]")
                self._deploying = False
                self._show_deploy_done()
                return
            log("[green]  Init successful[/green]\n")

            log("[bold]Running terraform plan...[/bold]")
            ok, output = tf.terraform_plan(deploy_dir)
            if output.strip():
                for line in output.strip().split("\n")[-10:]:
                    log(f"[dim]  {line}[/dim]")
            if not ok:
                log("[red]terraform plan failed![/red]")
                if output:
                    err_title, guidance = tf.parse_terraform_error(output)
                    if guidance:
                        log(f"[yellow]{err_title}[/yellow]")
                        for gline in guidance.split("\n"):
                            log(f"[yellow]{gline}[/yellow]")
                self._deploying = False
                self._show_deploy_done()
                return
            log("[green]  Plan successful[/green]\n")

            log("[bold]Running terraform apply...[/bold]")
            log("[dim]  Polling Proxmox for real-time task progress...[/dim]")

            # Start Proxmox progress monitor to track clone/create tasks
            progress_monitor = None
            try:
                from infraforge.proxmox_progress import ProxmoxProgressMonitor
                proxmox_client = self.app.proxmox
                progress_monitor = ProxmoxProgressMonitor(
                    proxmox_client,
                    self.spec.node,
                    log_fn=log,
                    poll_interval=2.0,
                )
                progress_monitor.start()
            except Exception:
                pass  # Monitor is optional — deployment works without it

            def _on_apply_line(line: str):
                stripped = line.strip()
                if stripped:
                    safe = stripped.replace("[", "\\[")
                    log(f"[dim]  {safe}[/dim]")

            ok, output = tf.terraform_apply_streaming(
                deploy_dir, line_callback=_on_apply_line,
            )

            # Stop the progress monitor
            if progress_monitor is not None:
                try:
                    progress_monitor.stop()
                except Exception:
                    pass

            if not ok:
                log("[red]terraform apply failed![/red]")
                if output:
                    err_title, guidance = tf.parse_terraform_error(output)
                    if guidance:
                        log(f"[yellow]{err_title}[/yellow]")
                        for gline in guidance.split("\n"):
                            log(f"[yellow]{gline}[/yellow]")
                self._deploying = False
                self._show_deploy_done()
                return

            log("\n[bold green]  VM created successfully![/bold green]\n")
            self._deploy_results["vm_created"] = True

            # ==============================================================
            # Phase 3: Wait for SSH connectivity
            # ==============================================================
            log("[bold]============================================[/bold]")
            log("[bold]  Phase 3: Waiting for SSH Connectivity[/bold]")
            log("[bold]============================================[/bold]\n")

            if self.spec.ip_address:
                ssh_ready = self._wait_for_ssh(
                    self.spec.ip_address, log_fn=log,
                )
                if not ssh_ready:
                    log("[yellow]  SSH not reachable after timeout. "
                        "Ansible may fail.[/yellow]")
                    log("[dim]  The VM might still be booting. "
                        "You can try running the playbook "
                        "manually later.[/dim]\n")
                else:
                    log("[green]  SSH is ready![/green]\n")
            else:
                log("[yellow]  No IP address set (DHCP). "
                    "Skipping SSH wait.[/yellow]")
                log("[dim]  You will need to run Ansible manually "
                    "once you know the IP.[/dim]\n")

            # ==============================================================
            # Phase 4: Ansible configuration
            # ==============================================================
            log("[bold]============================================[/bold]")
            log("[bold]  Phase 4: Ansible BIND9 Configuration[/bold]")
            log("[bold]============================================[/bold]\n")

            if self.spec.ip_address:
                modal_launched = self._launch_ansible_modal(log_fn=log)
                if modal_launched:
                    log("[bold green]  Ansible configuration deferred "
                        "to modal.[/bold green]")
                    log("[dim]  The BIND9 playbook will run in the "
                        "Ansible Run Modal window.[/dim]\n")
                    self._deploy_results["ansible_ok"] = True
                else:
                    log("\n[yellow]  Could not launch Ansible modal. "
                        "The VM was created but BIND9 may not be "
                        "fully configured.[/yellow]\n")
                    self._deploy_results["ansible_ok"] = False
            else:
                log("[yellow]  Skipping Ansible — no IP address "
                    "available.[/yellow]\n")
                self._deploy_results["ansible_ok"] = False

            # ==============================================================
            # Phase 5: Post-deployment (DNS + IPAM)
            # ==============================================================
            log("[bold]============================================[/bold]")
            log("[bold]  Phase 5: Post-Deployment Registration[/bold]")
            log("[bold]============================================[/bold]\n")

            dns_cfg = self.app.config.dns
            ipam_cfg = self.app.config.ipam

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
                    log(f"[green]  DNS {result}: {fqdn} -> "
                        f"{self.spec.ip_address}[/green]\n")
                    self._deploy_results["dns_registered"] = True
                    self._deploy_results["dns_fqdn"] = fqdn
                except Exception as e:
                    log(f"[yellow]  DNS failed: {e}[/yellow]\n")
            else:
                log("[dim]  DNS: skipped (not configured)[/dim]\n")

            # IPAM reservation
            if (
                ipam_cfg.url
                and self.spec.ip_address
                and self.spec.subnet_id
            ):
                log("[bold]Reserving IP in IPAM...[/bold]")
                try:
                    from infraforge.ipam_client import IPAMClient
                    ipam = IPAMClient(self.app.config)
                    ipam.create_address(
                        self.spec.ip_address,
                        self.spec.subnet_id,
                        hostname=self.spec.name,
                        description="BIND9 DNS Server - InfraForge",
                    )
                    log(f"[green]  IP {self.spec.ip_address} reserved "
                        f"in {self.spec.subnet_cidr}[/green]\n")
                    self._deploy_results["ipam_registered"] = True
                except Exception as e:
                    log(f"[yellow]  IPAM reservation failed: "
                        f"{e}[/yellow]\n")
            else:
                log("[dim]  IPAM: skipped (not configured)[/dim]\n")

            # Final summary
            elapsed = time.monotonic() - deploy_start
            elapsed_str = f"{elapsed:.1f}s"
            if self._deploy_results.get("ansible_ok"):
                self._deploy_results["success"] = True
                log("\n[bold green]============================================"
                    "[/bold green]")
                log("[bold green]  DNS Server Deployment Complete!"
                    "[/bold green]")
                log("[bold green]============================================"
                    "[/bold green]")
            else:
                log("\n[bold yellow]========================================"
                    "====[/bold yellow]")
                log("[bold yellow]  Deployment finished with warnings"
                    "[/bold yellow]")
                log("[bold yellow]========================================"
                    "====[/bold yellow]")

            log(f"\n[dim]Total duration: {elapsed_str}[/dim]")
            log(f"[dim]Log: {log_file_path}[/dim]")

            # Write final status to log file
            lf.write("\n" + "=" * 60 + "\n")
            lf.write("  Deployment Summary\n")
            lf.write("=" * 60 + "\n")
            lf.write(f"  Status:       {'SUCCESS' if self._deploy_results.get('success') else 'COMPLETED WITH WARNINGS'}\n")
            lf.write(f"  VM Created:   {'Yes' if self._deploy_results.get('vm_created') else 'No'}\n")
            lf.write(f"  Ansible OK:   {'Yes' if self._deploy_results.get('ansible_ok') else 'No'}\n")
            lf.write(f"  DNS Record:   {'Yes' if self._deploy_results.get('dns_registered') else 'No/Skipped'}\n")
            lf.write(f"  IPAM Reserve: {'Yes' if self._deploy_results.get('ipam_registered') else 'No/Skipped'}\n")
            lf.write(f"  Duration:     {elapsed_str}\n")
            lf.write(f"  Completed:    {datetime.now().isoformat()}\n")
            lf.write("=" * 60 + "\n")
            lf.flush()

        except Exception as e:
            log(f"\n[red]Deployment error: {e}[/red]")
            # Write error to log file
            elapsed = time.monotonic() - deploy_start
            lf.write(f"\nDEPLOYMENT ERROR: {e}\n")
            lf.write(f"Duration before failure: {elapsed:.1f}s\n")
            lf.write(f"Completed: {datetime.now().isoformat()}\n")
            lf.flush()
        finally:
            lf.close()

        self._deploying = False
        self._deploy_done = True
        self._show_deploy_done()

    def _show_deploy_done(self):
        """Transition to the completion step from the worker thread."""
        def _update():
            self._step = 7
            self._render_step()
            try:
                btn = self.query_one("#btn-next", Button)
                btn.label = "Done"
                btn.disabled = False
                btn.variant = "success"
                btn.add_class("-ready")
                btn.focus()
            except Exception:
                pass
            self._set_hint(
                "[bold green]Done![/bold green]  "
                "Press [b]Escape[/b] to return to dashboard"
            )
        self.app.call_from_thread(_update)

    # ------------------------------------------------------------------
    # SSH connectivity check
    # ------------------------------------------------------------------

    @staticmethod
    def _wait_for_ssh(
        ip: str,
        port: int = 22,
        timeout: int = 120,
        log_fn=None,
    ) -> bool:
        """Wait for SSH to become accessible on the target IP.

        Retries every 5 seconds until timeout.
        """
        start = time.monotonic()
        attempt = 0
        while (time.monotonic() - start) < timeout:
            attempt += 1
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((ip, port))
                sock.close()
                if result == 0:
                    return True
            except Exception:
                pass
            elapsed = int(time.monotonic() - start)
            if log_fn and attempt % 2 == 0:
                log_fn(
                    f"[dim]  Waiting for SSH on {ip}:{port}... "
                    f"({elapsed}s)[/dim]"
                )
            time.sleep(5)
        return False

    # ------------------------------------------------------------------
    # Ansible execution
    # ------------------------------------------------------------------

    @staticmethod
    def _check_ansible_installed() -> bool:
        """Check if ansible-playbook CLI is available."""
        try:
            result = subprocess.run(
                ["ansible-playbook", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, Exception):
            return False

    def _launch_ansible_modal(self, log_fn=None) -> bool:
        """Launch AnsibleRunModal for the bind9-server.yml playbook.

        Builds PlaybookInfo, CredentialProfile, and extra_vars, then pushes
        the modal.  Returns True if the modal was launched successfully.
        """
        from infraforge.ansible_runner import PlaybookInfo, _parse_playbook
        from infraforge.credential_manager import CredentialProfile

        playbook_dir = Path(self.app.config.ansible.playbook_dir).resolve()
        playbook_path = playbook_dir / "bind9-server.yml"

        if not playbook_path.exists():
            if log_fn:
                log_fn(
                    f"[red]  Playbook not found: {playbook_path}[/red]"
                )
            return False

        ip = self.spec.ip_address
        if not ip:
            if log_fn:
                log_fn("[red]  No IP address — cannot run Ansible[/red]")
            return False

        # Build PlaybookInfo from the playbook file
        pb = _parse_playbook(playbook_path, playbook_path.parent / "logs")
        if not pb:
            pb = PlaybookInfo(
                path=playbook_path,
                filename=playbook_path.name,
                name=playbook_path.stem,
                hosts="targets",
                task_count=0,
                description="",
                has_roles=False,
                last_run=None,
                last_status="never",
            )

        # Build extra_vars with DNS configuration
        zones = [
            z.strip()
            for z in self._dns_zones_str.split(",")
            if z.strip()
        ]
        forwarders = [
            f.strip()
            for f in self._dns_forwarders_str.split(",")
            if f.strip()
        ]

        extra_vars = {
            "dns_zones": zones,
            "dns_forwarders": forwarders,
            "dns_allow_recursion": self._dns_allow_recursion,
            "dns_allow_query": self._dns_allow_query,
            "dns_server_ip": ip,
            "dns_hostname": self.spec.name,
            "dns_domain": self.spec.dns_domain,
        }

        # Build CredentialProfile from the wizard's SSH/auth settings
        ssh_user = self._detect_ssh_user()
        private_key_path = ""

        if self._auth_method == "ssh_key" and self.spec.ssh_keys:
            # Find the private key corresponding to the public key
            for label, pubkey in self._ssh_keys:
                if pubkey == self.spec.ssh_keys:
                    key_file = (
                        Path.home() / ".ssh" / label.replace(".pub", "")
                    )
                    if key_file.exists():
                        private_key_path = str(key_file)
                    break

        if self._auth_method == "ssh_key":
            cred = CredentialProfile(
                name="dns-deploy-key",
                auth_type="ssh_key",
                username=ssh_user,
                private_key_path=private_key_path,
                become=ssh_user != "root",
            )
        else:
            cred = CredentialProfile(
                name="dns-deploy-password",
                auth_type="password",
                username=ssh_user,
                password=self._root_password,
                become=ssh_user != "root",
            )

        if log_fn:
            log_fn(f"[dim]  SSH user: {ssh_user}[/dim]")
            if ssh_user != "root":
                log_fn("[dim]  Privilege escalation: become (sudo)[/dim]")
            log_fn(f"[dim]  Playbook: {playbook_path.name}[/dim]")
            log_fn(f"[dim]  Target: {ip}[/dim]")
            log_fn(
                "[bold]  Launching Ansible Run Modal...[/bold]\n"
            )

        # Push the AnsibleRunModal with pre-populated targets, credential, and extra_vars
        from infraforge.screens.ansible_run_modal import AnsibleRunModal

        modal = AnsibleRunModal(
            playbook=pb,
            target_ips=[ip],
            credential=cred,
            extra_vars=extra_vars,
        )
        self.app.call_from_thread(self.app.push_screen, modal)
        return True

