"""Template Import Screen — import QEMU templates from .ifpkg files.

4-phase wizard:
  0. Select Package  — pick an .ifpkg file or download from URL
  1. Configure       — target node, storage, VMID, template name
  2. Import          — extract, upload, restore, convert (RichLog)
  3. Done            — success summary
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual import work

from infraforge.ssh_helper import test_ssh
from infraforge.screens.template_update_screen import (
    WizItem,
    _stor_label,
    _stor_header,
)
from infraforge.template_package import (
    get_exports_dir,
    read_manifest,
    extract_backup,
    scan_packages,
)


PHASE_NAMES = ["Select", "Configure", "Import", "Done"]

# Column widths for package listing
_PKG_NAME_W = 30
_PKG_DATE_W = 12
_PKG_SIZE_W = 12

# Transfer chunk size (256 KB)
_CHUNK_SIZE = 256 * 1024


def _human_size(nbytes: int) -> str:
    """Format byte count into human-readable string."""
    if nbytes <= 0:
        return "N/A"
    if nbytes >= 1024 ** 3:
        return f"{nbytes / (1024 ** 3):.1f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / (1024 ** 2):.1f} MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f} KB"
    return f"{nbytes} B"


def _progress_bar(current: int, total: int, start_time: float, width: int = 40) -> str:
    """Build a colored progress bar string with stats."""
    if total <= 0:
        return f"[bold cyan]Transferring... {_human_size(current)}[/bold cyan]"
    pct = min(current / total, 1.0)
    filled = int(width * pct)
    bar_filled = "\u2501" * filled
    bar_empty = "\u2591" * (width - filled)
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = int((total - current) / speed) if speed > 0 else 0
    eta_str = f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{eta}s"
    return (
        f"[bold green]{bar_filled}[/bold green][dim]{bar_empty}[/dim] "
        f"{int(pct * 100)}% \u2502 {_human_size(current)} / {_human_size(total)} \u2502 "
        f"{_human_size(int(speed))}/s \u2502 ETA {eta_str}"
    )


def _pkg_label(pkg: dict) -> str:
    """Build a colored, columnar label for a package."""
    manifest = pkg["manifest"]
    name = manifest.get("template_name", "unknown")
    if len(name) > _PKG_NAME_W - 1:
        name = name[: _PKG_NAME_W - 2] + ".."
    name = name.ljust(_PKG_NAME_W)

    created = manifest.get("created_at", "")
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_str = "\u2014"
    else:
        date_str = "\u2014"
    date_str = date_str.ljust(_PKG_DATE_W)

    size_bytes = manifest.get("disk_size_bytes", 0)
    size_str = _human_size(size_bytes).ljust(_PKG_SIZE_W)

    return (
        f"[bold bright_white]{name}[/bold bright_white]"
        f"[dim]{date_str}[/dim]"
        f"[green]{size_str}[/green]"
    )


def _pkg_header() -> str:
    """Column header for package table (3-space offset for option alignment)."""
    return (
        f"   [dim]"
        f"{'Name'.ljust(_PKG_NAME_W)}"
        f"{'Date'.ljust(_PKG_DATE_W)}"
        f"{'Size'.ljust(_PKG_SIZE_W)}"
        f"[/dim]"
    )


class TemplateImportScreen(Screen):
    """Guided wizard for importing a QEMU template from an .ifpkg package."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back/Cancel", show=True),
        Binding("x", "cleanup_staging", "Clean Up", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._phase = 0
        self._cursor = 0
        self._items: list[WizItem] = []
        self._packages: list[dict] = []
        self._storages: list = []
        self._online_nodes: list[str] = []
        self._selected_package: Optional[dict] = None
        self._selected_node: str = ""
        self._selected_storage: str = ""
        self._vmid: str = ""
        self._template_name: str = ""
        self._working = False
        self._import_done = False
        self._mount_gen = 0
        self._data_loaded = False
        self._editing = False
        self._editing_key = ""
        self._downloading = False

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
                yield Static(
                    "", id="transfer-progress", markup=True, classes="hidden"
                )

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
        self._scan_packages()
        self._load_initial_data()
        self._render_phase()

    def _get_exports_override(self) -> str:
        """Get the configured exports directory override."""
        try:
            return self.app.config.defaults.exports_dir
        except Exception:
            return ""

    def _scan_packages(self):
        """Scan the exports directory for .ifpkg files."""
        try:
            self._packages = scan_packages(
                get_exports_dir(self._get_exports_override()),
            )
        except Exception:
            self._packages = []

    # ------------------------------------------------------------------
    # Transfer progress helpers
    # ------------------------------------------------------------------

    def _show_transfer_progress(self, text: str):
        """Show and update the transfer progress widget (must be called on UI thread)."""
        try:
            w = self.query_one("#transfer-progress", Static)
            w.update(text)
            w.remove_class("hidden")
        except Exception:
            pass

    def _hide_transfer_progress(self):
        """Hide the transfer progress widget (must be called on UI thread)."""
        try:
            w = self.query_one("#transfer-progress", Static)
            w.add_class("hidden")
            w.update("")
        except Exception:
            pass

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

        # Phase 3 done — Enter/Escape returns
        if self._import_done:
            if event.key in ("enter", "escape"):
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
        if item.group == "package":
            pkg_path = item.meta.get("path")
            if pkg_path:
                for pkg in self._packages:
                    if str(pkg["path"]) == str(pkg_path):
                        self._selected_package = pkg
                        self._template_name = pkg["manifest"].get(
                            "template_name", ""
                        )
                        break
        elif item.group == "node":
            new_node = item.key
            if new_node != self._selected_node:
                self._selected_node = new_node
                self._selected_storage = ""
                # Rebuild configure items to refresh storage list
                self._items = []
                self._build_configure_items()
                self._mount_items()
                nav = self._nav_indices()
                if nav:
                    self._cursor = nav[0]
                self._refresh_lines()
                self._update_phase_hint()
                return
        elif item.group == "storage":
            self._selected_storage = item.key

    def _apply_input_value(self, item: WizItem):
        if item.key == "vmid":
            self._vmid = item.value.strip()
        elif item.key == "template_name":
            self._template_name = item.value.strip()
        elif item.key == "download_url":
            url = item.value.strip()
            if url:
                self._download_package(url)
                # Clear the value so it doesn't persist in the UI
                item.value = ""

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_phase(self):
        self._items = []
        self._cursor = 0
        self._unfocus_next_btn()

        if self._phase == 0:
            self._build_select_items()
        elif self._phase == 1:
            self._build_configure_items()
        elif self._phase == 2:
            self._build_import_phase()
            return  # import phase uses RichLog, not items
        elif self._phase == 3:
            self._build_done_items()

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
            btn_next.label = "Import"
            btn_next.variant = "success"
        elif self._phase == 3:
            btn_next.label = "Done"
            btn_next.variant = "success"
            btn_next.disabled = False
            btn_next.add_class("-ready")
        else:
            btn_next.label = "Next"
            btn_next.variant = "primary"
            btn_next.disabled = False

        self._mount_items()

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
            header.update("[b]SELECT PACKAGE[/b]")
        elif self._phase == 1:
            header.update("[b]CONFIGURE IMPORT[/b]")
        elif self._phase == 3:
            header.update("[b]IMPORT COMPLETE[/b]")

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
            0: "Space/Enter select  |  Paste a URL and press Enter to download",
            1: "Space to edit  |  Enter confirm  |  Backspace back",
            3: "Press Enter or Escape to return",
        }
        self._set_hint(hints.get(self._phase, ""))

    # ------------------------------------------------------------------
    # Phase 0: Select Package
    # ------------------------------------------------------------------

    def _build_select_items(self):
        items = self._items
        exports_dir = get_exports_dir(self._get_exports_override())

        items.append(WizItem(
            kind="info",
            label=(
                f"[bold]Staging directory:[/bold] [cyan]{exports_dir}[/cyan]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                "[dim]Template packages here are staging copies for import. "
                "They can be cleaned up after use.[/dim]"
            ),
        ))
        items.append(WizItem(kind="info", label=""))

        # URL download input
        items.append(WizItem(
            kind="input",
            label="[cyan]Download URL[/cyan]",
            key="download_url",
            value="",
            meta={"placeholder": "https://example.com/template.ifpkg"},
        ))
        items.append(WizItem(kind="info", label=""))

        if self._packages:
            items.append(WizItem(kind="header", label="AVAILABLE PACKAGES"))
            items.append(WizItem(kind="info", label=_pkg_header()))
            for pkg in self._packages:
                pkg_path = pkg["path"]
                sel = (
                    self._selected_package is not None
                    and str(self._selected_package["path"]) == str(pkg_path)
                )
                items.append(WizItem(
                    kind="option",
                    label=_pkg_label(pkg),
                    key=f"pkg:{pkg_path.name}",
                    group="package",
                    selected=sel,
                    meta={"path": str(pkg_path)},
                ))
        else:
            items.append(WizItem(
                kind="info",
                label="[yellow]No packages found[/yellow]",
            ))
            items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]  Place .ifpkg files in {exports_dir} "
                    f"or download via URL above[/dim]"
                ),
            ))

    # ------------------------------------------------------------------
    # Phase 1: Configure
    # ------------------------------------------------------------------

    def _build_configure_items(self):
        items = self._items

        if self._selected_package:
            manifest = self._selected_package["manifest"]
            pkg_name = manifest.get("template_name", "unknown")
            items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]Package:[/dim]  "
                    f"[bold bright_white]{pkg_name}[/bold bright_white]  "
                    f"[dim]({_human_size(manifest.get('disk_size_bytes', 0))})[/dim]"
                ),
            ))
            items.append(WizItem(kind="info", label=""))

        # Target node
        items.append(WizItem(kind="header", label="TARGET NODE"))
        if self._online_nodes:
            for node_name in self._online_nodes:
                items.append(WizItem(
                    kind="option",
                    label=f"[bold bright_white]{node_name}[/bold bright_white]",
                    key=node_name,
                    group="node",
                    selected=self._selected_node == node_name,
                ))
        else:
            items.append(WizItem(
                kind="info",
                label="[yellow]No online nodes found[/yellow]",
            ))

        # Target storage (filtered to selected node or shared)
        items.append(WizItem(
            kind="header",
            label=(
                f"TARGET STORAGE  [dim]on {self._selected_node}[/dim]"
                if self._selected_node
                else "TARGET STORAGE"
            ),
        ))

        if self._selected_node:
            node_storages = [
                s for s in self._storages
                if s.node == self._selected_node or s.shared
            ]
            if node_storages:
                items.append(WizItem(kind="info", label=_stor_header()))
                seen: set[str] = set()
                for s in node_storages:
                    if s.storage not in seen:
                        seen.add(s.storage)
                        items.append(WizItem(
                            kind="option",
                            label=_stor_label(s),
                            key=s.storage,
                            group="storage",
                            selected=self._selected_storage == s.storage,
                        ))
            else:
                items.append(WizItem(
                    kind="info",
                    label=(
                        f"[yellow]No storage pools on "
                        f"{self._selected_node}[/yellow]"
                    ),
                ))
        elif self._storages:
            items.append(WizItem(
                kind="info",
                label="[dim]Select a node first[/dim]",
            ))
        else:
            items.append(WizItem(
                kind="info",
                label="Loading storage...",
            ))

        # VMID input
        items.append(WizItem(kind="header", label="VMID"))
        items.append(WizItem(
            kind="input",
            label="[cyan]VMID[/cyan]",
            key="vmid",
            value=self._vmid,
            meta={"placeholder": "(auto-assigned)"},
        ))

        # Template name input
        items.append(WizItem(kind="header", label="TEMPLATE NAME"))
        items.append(WizItem(
            kind="input",
            label="[cyan]Template Name[/cyan]",
            key="template_name",
            value=self._template_name,
            meta={"placeholder": "e.g. ubuntu-22-template"},
        ))

    # ------------------------------------------------------------------
    # Phase 2: Import (automated, RichLog)
    # ------------------------------------------------------------------

    def _build_import_phase(self):
        """Switch to RichLog for the import phase."""
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

        self._show_richlog("Importing Template...")
        self._run_import()

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
    # Phase 3: Done
    # ------------------------------------------------------------------

    def _build_done_items(self):
        items = self._items
        items.append(WizItem(kind="header", label="IMPORT COMPLETE"))
        items.append(WizItem(
            kind="info",
            label=(
                f"Template [bold bright_white]{self._template_name}[/bold bright_white] "
                f"imported successfully as VMID [bold yellow]{self._vmid}[/bold yellow]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=f"Node: [cyan]{self._selected_node}[/cyan]",
        ))
        items.append(WizItem(
            kind="info",
            label=f"Storage: [cyan]{self._selected_storage}[/cyan]",
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(kind="header", label="STAGING CLEANUP"))
        items.append(WizItem(
            kind="info",
            label=(
                "[dim]The imported template is now on your Proxmox node.[/dim]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                "[dim]The .ifpkg staging file can be safely deleted to reclaim disk space.[/dim]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                "[dim]Use [bold]x[/bold] on the package list to clean up all staging files.[/dim]"
            ),
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="info",
            label="[dim]Press Enter or Escape to return[/dim]",
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
            # Pre-populate VMID if not set
            if not self._vmid:
                try:
                    next_id = self.app.proxmox.get_next_vmid()
                    self._vmid = str(next_id)
                except Exception:
                    pass
            # Pre-populate template name from manifest
            if not self._template_name and self._selected_package:
                self._template_name = self._selected_package["manifest"].get(
                    "template_name", ""
                )
            self._phase = 1
            self._render_phase()
        elif self._phase == 1:
            valid, msg = self._validate_phase()
            if not valid:
                self.notify(msg, severity="error")
                return
            # Check SSH connectivity before proceeding to import
            self._check_ssh_before_import()
        elif self._phase == 3:
            self.app.pop_screen()

    def _go_back(self):
        if self._phase == 1:
            self._phase = 0
            self._render_phase()

    def _resolve_node_host(self) -> str:
        """Resolve the SSH host for the selected target node.

        Uses /cluster/status to find the node IP (important for
        non-shared storage where the file must be on that node).
        Falls back to config.proxmox.host.
        """
        if self._selected_node:
            node_ip = self.app.proxmox.get_node_ip(self._selected_node)
            if node_ip:
                return node_ip
        return self.app.config.proxmox.host

    @work(thread=True)
    def _check_ssh_before_import(self):
        """Test SSH connectivity; if it fails, prompt user to set it up."""
        host = self._resolve_node_host()

        # Show checking hint
        def _set():
            self._set_hint("[dim]Checking SSH connectivity...[/dim]")
            self.query_one("#btn-next", Button).disabled = True
        self.app.call_from_thread(_set)

        if test_ssh(host):
            def _proceed():
                self._phase = 2
                self._render_phase()
            self.app.call_from_thread(_proceed)
        else:
            def _show_modal():
                # Re-enable button before showing modal
                try:
                    btn = self.query_one("#btn-next", Button)
                    btn.disabled = False
                except Exception:
                    pass
                from infraforge.screens.ssh_setup_modal import SSHSetupModal
                self.app.push_screen(
                    SSHSetupModal(host),
                    callback=self._on_ssh_setup_done,
                )
            self.app.call_from_thread(_show_modal)

    def _on_ssh_setup_done(self, success: bool) -> None:
        """Called after SSH setup modal is dismissed."""
        if success:
            self._phase = 2
            self._render_phase()
        else:
            self.notify("SSH key auth is required for template import", severity="warning")
            self._update_phase_hint()

    def action_cleanup_staging(self):
        """Show cleanup confirmation for staging directory."""
        if self._phase != 0:
            return
        if self._working or self._downloading:
            return

        from infraforge.template_package import get_exports_dir
        exports_dir = get_exports_dir(self._get_exports_override())

        # Calculate total size of .ifpkg files
        total_size = 0
        pkg_files = list(exports_dir.glob("*.ifpkg"))
        for f in pkg_files:
            try:
                total_size += f.stat().st_size
            except OSError:
                pass

        if not pkg_files:
            self.notify("No packages to clean up", severity="information")
            return

        # Format size
        if total_size >= 1024 ** 3:
            size_str = f"{total_size / (1024 ** 3):.2f} GB"
        elif total_size >= 1024 ** 2:
            size_str = f"{total_size / (1024 ** 2):.1f} MB"
        else:
            size_str = f"{total_size / 1024:.1f} KB"

        count = len(pkg_files)

        # Push a confirmation screen
        self.app.push_screen(
            CleanupConfirmModal(count=count, size_str=size_str, directory=str(exports_dir)),
            callback=self._on_cleanup_confirmed,
        )

    def _on_cleanup_confirmed(self, confirmed: bool) -> None:
        """Delete all .ifpkg files if confirmed."""
        if not confirmed:
            return
        from infraforge.template_package import get_exports_dir
        exports_dir = get_exports_dir(self._get_exports_override())
        deleted = 0
        for f in exports_dir.glob("*.ifpkg"):
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
        self._scan_packages()
        self._selected_package = None
        self._render_phase()
        self.notify(f"Deleted {deleted} package(s)", severity="information")

    def action_handle_escape(self):
        if self._import_done:
            self.app.pop_screen()
        elif self._editing:
            self._cancel_edit()
        elif self._working:
            self.notify("Operation in progress...", severity="warning")
        elif self._phase > 0 and self._phase <= 1:
            self._go_back()
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed):
        if self._import_done:
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
            if not self._selected_package:
                return False, "Please select a package"
            return True, ""
        elif self._phase == 1:
            if not self._selected_node:
                return False, "Please select a target node"
            if not self._selected_storage:
                return False, "Please select a target storage"
            if not self._template_name:
                return False, "Please enter a template name"
            if self._vmid:
                try:
                    int(self._vmid)
                except ValueError:
                    return False, "VMID must be a number"
            return True, ""
        return True, ""

    # ------------------------------------------------------------------
    # Background data loaders
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        try:
            raw_nodes = self.app.proxmox.get_nodes()
            self._online_nodes = [
                n["node"] for n in raw_nodes
                if n.get("status") == "online"
            ]
            self._storages = self.app.proxmox.get_storage_info()
            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)
        except Exception:
            pass

    def _on_data_loaded(self):
        if self._phase in (0, 1):
            self._render_phase()

    # ------------------------------------------------------------------
    # URL download worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _download_package(self, url: str):
        if self._downloading:
            return
        self._downloading = True

        def _notify(msg: str, severity: str = "information"):
            def _do():
                self.notify(msg, severity=severity)
            self.app.call_from_thread(_do)

        exports_dir = get_exports_dir(self._get_exports_override())

        # Derive filename from URL
        url_path = url.split("?")[0].split("#")[0]
        filename = url_path.split("/")[-1] if "/" in url_path else "download.ifpkg"
        if not filename.endswith(".ifpkg"):
            filename += ".ifpkg"
        dest_path = exports_dir / filename

        try:
            _notify(f"Downloading {filename}...")

            req = urllib.request.Request(url, headers={
                "User-Agent": "InfraForge-Template-Import/1.0",
            })

            # Show the progress widget
            self.app.call_from_thread(
                lambda: self._show_transfer_progress(
                    "[bold cyan]Connecting...[/bold cyan]"
                )
            )

            with urllib.request.urlopen(req, timeout=300) as resp:
                content_length = resp.headers.get("Content-Length")
                total_size = int(content_length) if content_length else 0
                downloaded = 0
                start_time = time.time()

                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Update progress bar
                        bar_text = _progress_bar(downloaded, total_size, start_time)
                        self.app.call_from_thread(
                            lambda t=bar_text: self._show_transfer_progress(t)
                        )

            # Hide progress widget
            self.app.call_from_thread(self._hide_transfer_progress)

            # Validate the downloaded file
            manifest = read_manifest(dest_path)
            if manifest is None:
                # Invalid package — delete and notify
                try:
                    dest_path.unlink()
                except OSError:
                    pass
                _notify("Downloaded file is not a valid .ifpkg package", "error")
                self._downloading = False
                return

            # Valid — re-scan and re-render with new package selected
            self._packages = scan_packages(
                get_exports_dir(self._get_exports_override()),
            )

            # Auto-select the newly downloaded package
            for pkg in self._packages:
                if str(pkg["path"]) == str(dest_path):
                    self._selected_package = pkg
                    self._template_name = manifest.get("template_name", "")
                    break

            _notify(f"Downloaded {filename} successfully")

            def _rerender():
                if self._phase == 0:
                    self._render_phase()
            self.app.call_from_thread(_rerender)

        except Exception as e:
            # Hide progress widget on error
            self.app.call_from_thread(self._hide_transfer_progress)
            # Clean up partial download
            try:
                if dest_path.exists():
                    dest_path.unlink()
            except OSError:
                pass
            _notify(f"Download failed: {e}", "error")

        self._downloading = False

    # ------------------------------------------------------------------
    # Phase 2: Import worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _run_import(self):
        self._working = True
        pkg = self._selected_package
        if not pkg:
            return

        package_path = Path(pkg["path"])
        node = self._selected_node
        storage = self._selected_storage
        host = self._resolve_node_host()
        template_name = self._template_name

        # Resolve VMID
        vmid = 0
        if self._vmid:
            try:
                vmid = int(self._vmid)
            except ValueError:
                pass

        temp_dir = None

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        try:
            # Step 0: Get VMID if not set
            if vmid <= 0:
                log("[bold]Getting next available VMID...[/bold]")
                vmid = self.app.proxmox.get_next_vmid()
                self._vmid = str(vmid)
                log(f"[green]  Got VMID: {vmid}[/green]\n")

            # Step 1: Extract VMA from package
            log("[bold]Extracting backup from package...[/bold]")
            temp_dir = tempfile.mkdtemp(prefix="infraforge-import-")
            local_vma = extract_backup(package_path, Path(temp_dir))

            # Rename to vzdump naming convention so qmrestore can parse it
            ts = datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
            vzdump_name = f"vzdump-qemu-{vmid}-{ts}.vma.zst"
            renamed_vma = local_vma.parent / vzdump_name
            local_vma.rename(renamed_vma)
            local_vma = renamed_vma

            vma_filename = local_vma.name
            log(f"[green]  Extracted: {vma_filename}[/green]")
            log(f"[dim]  Size: {_human_size(local_vma.stat().st_size)}[/dim]\n")

            # Step 2: Resolve storage path on the Proxmox node
            log("[bold]Resolving storage path on node...[/bold]")
            storage_dir = None
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-o", "StrictHostKeyChecking=accept-new",
                        f"root@{host}",
                        "pvesm", "path", f"{storage}:backup/dummy.vma",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Output is full path like /var/lib/vz/dump/dummy.vma
                    # Strip the filename to get the directory
                    resolved = result.stdout.strip()
                    storage_dir = os.path.dirname(resolved)
                    log(f"[green]  Storage path: {storage_dir}[/green]\n")
            except Exception as e:
                log(f"[dim]  pvesm path lookup failed: {e}[/dim]")

            if not storage_dir:
                # Fallback: try common paths
                log("[dim]  Trying fallback path /var/lib/vz/dump/...[/dim]")
                try:
                    result = subprocess.run(
                        [
                            "ssh",
                            "-o", "StrictHostKeyChecking=accept-new",
                            f"root@{host}",
                            "test", "-d", "/var/lib/vz/dump/",
                        ],
                        capture_output=True,
                        timeout=15,
                    )
                    if result.returncode == 0:
                        storage_dir = "/var/lib/vz/dump"
                        log(f"[green]  Using fallback: {storage_dir}[/green]\n")
                    else:
                        log("[red]  Fallback path does not exist[/red]")
                        raise RuntimeError(
                            "Could not resolve storage dump directory on node"
                        )
                except subprocess.TimeoutExpired:
                    raise RuntimeError(
                        "SSH timeout resolving storage path on node"
                    )

            # Step 3: Upload VMA to Proxmox node via SSH + cat (with progress)
            remote_path = f"{storage_dir}/{vma_filename}"
            file_size = local_vma.stat().st_size
            log(f"[bold]Uploading VMA to {node}...[/bold]")
            log(f"[dim]  {local_vma} -> root@{host}:{remote_path}[/dim]")
            log(f"[dim]  Size: {_human_size(file_size)}[/dim]")

            # Show initial progress bar
            self.app.call_from_thread(
                lambda: self._show_transfer_progress(
                    "[bold cyan]Starting upload...[/bold cyan]"
                )
            )

            try:
                proc = subprocess.Popen(
                    [
                        "ssh",
                        "-o", "StrictHostKeyChecking=accept-new",
                        f"root@{host}",
                        f"cat > {remote_path}",
                    ],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                uploaded = 0
                start_time = time.time()

                with open(local_vma, "rb") as f:
                    while True:
                        chunk = f.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
                        uploaded += len(chunk)

                        # Update progress bar
                        bar_text = _progress_bar(uploaded, file_size, start_time)
                        self.app.call_from_thread(
                            lambda t=bar_text: self._show_transfer_progress(t)
                        )

                proc.stdin.close()
                proc.wait(timeout=60)

                # Hide progress bar
                self.app.call_from_thread(self._hide_transfer_progress)

                if proc.returncode != 0:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
                    log(f"[red]  Upload failed: {stderr}[/red]")
                    raise RuntimeError(f"SSH upload failed: {stderr}")

                elapsed = time.time() - start_time
                avg_speed = file_size / elapsed if elapsed > 0 else 0
                log(
                    f"[green]  Upload complete! "
                    f"({_human_size(file_size)} in {elapsed:.1f}s, "
                    f"{_human_size(int(avg_speed))}/s)[/green]\n"
                )

            except subprocess.TimeoutExpired:
                # Hide progress bar on timeout
                self.app.call_from_thread(self._hide_transfer_progress)
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("SSH upload timed out")
            except RuntimeError:
                raise
            except Exception as e:
                # Hide progress bar on error
                self.app.call_from_thread(self._hide_transfer_progress)
                raise RuntimeError(f"SSH upload failed: {e}")

            # Step 4: qmrestore via Proxmox API
            log(f"[bold]Restoring backup as VM {vmid}...[/bold]")
            log(f"[dim]  Archive: {remote_path}[/dim]")
            log(f"[dim]  Storage: {storage}[/dim]")
            upid = self.app.proxmox.restore_qemu(
                node, remote_path, vmid, storage,
            )
            log(f"[dim]  Task: {upid}[/dim]")

            # Step 5: Poll restore task
            log("[dim]  Waiting for restore to complete...[/dim]")
            elapsed = 0
            max_wait = 600
            last_log_line = 0
            while elapsed < max_wait:
                try:
                    task_status = self.app.proxmox.get_task_status(node, upid)
                    if task_status.get("status") == "stopped":
                        exit_status = task_status.get("exitstatus", "")
                        if exit_status == "OK":
                            log("[green]  Restore complete![/green]\n")
                        else:
                            log(f"[red]  Restore task exited: {exit_status}[/red]")
                            # Fetch task log for diagnostics
                            try:
                                task_log = self.app.proxmox.get_task_log(
                                    node, upid, start=0, limit=50
                                )
                                for entry in task_log:
                                    log(f"[dim]    {entry.get('t', '')}[/dim]")
                            except Exception:
                                pass
                            raise RuntimeError(
                                f"Restore task failed: {exit_status}"
                            )
                        break
                except RuntimeError:
                    raise
                except Exception:
                    pass

                # Log progress from task log
                try:
                    task_log = self.app.proxmox.get_task_log(
                        node, upid, start=last_log_line, limit=20
                    )
                    for entry in task_log:
                        line_num = entry.get("n", 0)
                        if line_num > last_log_line:
                            last_log_line = line_num
                            text = entry.get("t", "").strip()
                            if text:
                                log(f"[dim]    {text}[/dim]")
                except Exception:
                    pass

                time.sleep(3)
                elapsed += 3
            else:
                raise RuntimeError(
                    f"Restore task timed out after {max_wait}s"
                )

            # Step 6: Convert to template
            log("[bold]Converting to template...[/bold]")
            self.app.proxmox.convert_to_template(node, vmid)
            log("[green]  Converted to template![/green]\n")

            # Step 7: Rename template
            log(f"[bold]Setting template name to {template_name}...[/bold]")
            self.app.proxmox.set_vm_config(node, vmid, name=template_name)
            log(f"[green]  Renamed to {template_name}[/green]\n")

            # Step 8: Cleanup
            log("[bold]Cleaning up...[/bold]")

            # Remove uploaded VMA from Proxmox node
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-o", "StrictHostKeyChecking=accept-new",
                        f"root@{host}",
                        "rm", "-f", remote_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    log("[green]  Removed VMA from node[/green]")
                else:
                    log(
                        f"[yellow]  Could not remove VMA from node: "
                        f"{result.stderr.strip()}[/yellow]"
                    )
            except Exception as e:
                log(f"[yellow]  Could not remove VMA from node: {e}[/yellow]")

            # Remove local temp dir
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    log("[green]  Removed local temp files[/green]")
                except Exception:
                    pass

            log(
                f"\n[bold green]Import complete! "
                f"{template_name} (VMID {vmid}) on {node}[/bold green]"
            )

        except Exception as e:
            # Ensure progress bar is hidden on any error
            self.app.call_from_thread(self._hide_transfer_progress)
            log(f"\n[red]Import error: {e}[/red]")
            # Cleanup temp dir on failure too
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
            self._working = False
            self._show_retry_hint()
            return

        self._working = False
        self._import_done = True

        def _show_done():
            self._phase = 3
            self._render_phase()
            self._set_hint(
                "[bold green]Done![/bold green]  "
                "Press [b]Enter[/b] or [b]Escape[/b] to return"
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


class CleanupConfirmModal(ModalScreen[bool]):
    """Confirm cleanup of staging directory."""

    DEFAULT_CSS = """
    CleanupConfirmModal {
        align: center middle;
    }
    #cleanup-box {
        width: 65;
        height: auto;
        max-height: 16;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, count: int, size_str: str, directory: str) -> None:
        super().__init__()
        self._count = count
        self._size_str = size_str
        self._directory = directory

    def compose(self) -> ComposeResult:
        with Container(id="cleanup-box"):
            yield Static(
                f"[bold yellow]Clean Up Staging Directory[/bold yellow]\n\n"
                f"Delete [bold]{self._count}[/bold] package(s) "
                f"([bold red]{self._size_str}[/bold red]) from:\n"
                f"[cyan]{self._directory}[/cyan]\n\n"
                f"[dim]These are staging copies used for transferring templates\n"
                f"between Proxmox nodes. Templates already imported to Proxmox\n"
                f"are not affected.[/dim]",
                markup=True,
            )
            with Horizontal(classes="modal-buttons"):
                yield Button(
                    f"Delete All ({self._size_str})",
                    id="btn-confirm",
                    variant="error",
                )
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(False)
