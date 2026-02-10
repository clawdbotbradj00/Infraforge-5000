"""Template Export Screen — export a QEMU template as an .ifpkg package.

3-phase wizard:
  0. Confirm    — review template details, select backup storage, set filename
  1. Export     — automated vzdump, download, package creation (RichLog)
  2. Done       — success summary with file path and size
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual import work

from infraforge.models import Template
from infraforge.screens.template_update_screen import (
    WizItem,
    _stor_label,
    _stor_header,
)
from infraforge import template_package


PHASE_NAMES = ["Confirm", "Export", "Done"]


class TemplateExportScreen(Screen):
    """Guided wizard for exporting a QEMU template as an .ifpkg package."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back/Cancel", show=True),
    ]

    def __init__(self, template: Template):
        super().__init__()
        self._phase = 0
        self._cursor = 0
        self._items: list[WizItem] = []
        self._template = template
        self._storages: list = []
        self._data_loaded = False
        self._working = False
        self._done = False
        self._mount_gen = 0
        self._editing = False
        self._editing_key = ""
        self._storage: str = ""
        self._output_filename: str = f"{template.name}.ifpkg"
        self._export_result_path: str = ""
        self._export_result_size: str = ""

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
                yield Button("Export", variant="success", id="btn-next")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self):
        self._load_storage_data()
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

        # Phase 2 done — Enter/Escape returns
        if self._done:
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
            # No back navigation — phase 0 is the only interactive phase

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
        if item.group == "storage":
            self._storage = item.key

    def _apply_input_value(self, item: WizItem):
        if item.key == "output_filename":
            val = item.value.strip()
            if val and not val.endswith(".ifpkg"):
                val += ".ifpkg"
            self._output_filename = val

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_phase(self):
        self._items = []
        self._cursor = 0
        self._unfocus_next_btn()

        if self._phase == 0:
            self._build_confirm_items()
        elif self._phase == 1:
            self._build_export_phase()
            return  # export phase uses RichLog, not items
        elif self._phase == 2:
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
        if self._phase == 0:
            btn_next.label = "Export"
            btn_next.variant = "success"
            btn_next.disabled = False
        elif self._phase == 2:
            btn_next.label = "Done"
            btn_next.variant = "success"
            btn_next.disabled = False

        self._mount_items()

        nav = self._nav_indices()
        if nav:
            self._cursor = nav[0]
        self._refresh_lines()
        self._update_phase_hint()

        # If phase is already valid on entry, mark button as ready
        if self._phase == 0:
            valid, _ = self._validate_phase()
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
        if self._phase == 0:
            header.update("[b]EXPORT TEMPLATE AS PACKAGE[/b]")
        elif self._phase == 2:
            header.update("[b]EXPORT COMPLETE[/b]")

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
            0: "Space/Enter select  |  j/k navigate  |  Enter on filename to edit",
            2: "Press Enter or Escape to return",
        }
        self._set_hint(hints.get(self._phase, ""))

    # ------------------------------------------------------------------
    # Phase 0: Confirm
    # ------------------------------------------------------------------

    def _build_confirm_items(self):
        items = self._items
        t = self._template

        # Template details
        items.append(WizItem(kind="header", label="TEMPLATE DETAILS"))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]Name:[/dim]      "
                f"[bold bright_white]{t.name}[/bold bright_white]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]VMID:[/dim]      "
                f"[bold yellow]{t.vmid}[/bold yellow]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]Node:[/dim]      "
                f"[cyan]{t.node}[/cyan]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]Disk Size:[/dim] "
                f"[green]{t.size_display}[/green]"
            ),
        ))
        items.append(WizItem(kind="info", label=""))

        # Backup storage selection
        tmpl_node = t.node
        items.append(WizItem(
            kind="header",
            label=f"BACKUP STORAGE  [dim]on {tmpl_node}[/dim]" if tmpl_node else "BACKUP STORAGE",
        ))

        backup_storages = [
            s for s in self._storages
            if "backup" in s.content
            and (s.node == tmpl_node or s.shared)
        ]

        if backup_storages:
            items.append(WizItem(kind="info", label=_stor_header()))
            seen: set[str] = set()
            for s in backup_storages:
                if s.storage not in seen:
                    seen.add(s.storage)
                    items.append(WizItem(
                        kind="option", label=_stor_label(s),
                        key=s.storage, group="storage",
                        selected=self._storage == s.storage,
                    ))
        elif self._data_loaded:
            items.append(WizItem(
                kind="info",
                label=(
                    "[yellow]No backup-capable storage found on "
                    f"{tmpl_node}[/yellow]"
                ),
            ))
        else:
            items.append(WizItem(kind="info", label="Loading storage..."))

        items.append(WizItem(kind="info", label=""))

        # Output filename
        items.append(WizItem(kind="header", label="OUTPUT"))
        items.append(WizItem(
            kind="input", label="[cyan]Filename[/cyan]", key="output_filename",
            value=self._output_filename,
            meta={"placeholder": f"{t.name}.ifpkg"},
        ))

    # ------------------------------------------------------------------
    # Phase 1: Export (automated, RichLog)
    # ------------------------------------------------------------------

    def _build_export_phase(self):
        """Switch to RichLog for the export phase."""
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

        self._show_richlog("Exporting...")
        self._run_export()

    def _show_richlog(self, phase_title: str):
        """Clear wizard items and mount a RichLog widget."""
        for w in self.query(".wiz-line"):
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
    # Phase 2: Done
    # ------------------------------------------------------------------

    def _build_done_items(self):
        items = self._items

        items.append(WizItem(kind="header", label="EXPORT SUCCESSFUL"))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]Template:[/dim]  "
                f"[bold bright_white]{self._template.name}[/bold bright_white]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]File:[/dim]      "
                f"[bold green]{self._export_result_path}[/bold green]"
            ),
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[dim]Size:[/dim]      "
                f"[green]{self._export_result_size}[/green]"
            ),
        ))
        items.append(WizItem(kind="info", label=""))
        items.append(WizItem(
            kind="info",
            label="Press [b]Enter[/b] or [b]Escape[/b] to return",
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
            self._phase = 1
            self._render_phase()
        elif self._phase == 2:
            self.app.pop_screen()

    def action_handle_escape(self):
        if self._done:
            self.app.pop_screen()
        elif self._editing:
            self._cancel_edit()
        elif self._working:
            self.notify("Operation in progress...", severity="warning")
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed):
        if self._done:
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
            if not self._storage:
                return False, "Please select a backup storage"
            if not self._output_filename:
                return False, "Please enter an output filename"
            return True, ""
        return True, ""

    # ------------------------------------------------------------------
    # Background data loaders
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_storage_data(self):
        try:
            self._storages = self.app.proxmox.get_storage_info()
            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)
        except Exception:
            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)

    def _on_data_loaded(self):
        if self._phase == 0:
            self._render_phase()

    # ------------------------------------------------------------------
    # Phase 1: Export worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _run_export(self):
        self._working = True
        t = self._template
        node = t.node
        vmid = t.vmid
        storage = self._storage
        host = self.app.config.proxmox.host

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#deploy-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        backup_volid = ""
        local_temp_dir = None
        local_vma_path: Optional[Path] = None

        try:
            # Step 1: Create vzdump backup
            log("[bold]Creating vzdump backup...[/bold]")
            log(f"[dim]  Node: {node}, VMID: {vmid}, Storage: {storage}[/dim]")
            upid = self.app.proxmox.backup_vm(node, vmid, storage)
            log(f"[dim]  Task UPID: {upid}[/dim]")
            log("[dim]  Waiting for backup to complete...[/dim]\n")

            # Step 2: Poll task with log progress
            elapsed = 0
            timeout = 600  # 10 minutes max for backup
            last_log_line = 0
            while elapsed < timeout:
                # Check task status
                status = self.app.proxmox.get_task_status(node, upid)
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status != "OK":
                        log(f"[red]  Backup task failed: {exit_status}[/red]")
                        self._working = False
                        self._show_retry_hint()
                        return
                    break

                # Fetch and display task log lines
                try:
                    log_lines = self.app.proxmox.get_task_log(
                        node, upid, start=last_log_line, limit=50,
                    )
                    for entry in log_lines:
                        line_num = entry.get("n", 0)
                        line_text = entry.get("t", "")
                        if line_num >= last_log_line and line_text:
                            log(f"[dim]  {line_text}[/dim]")
                            last_log_line = line_num + 1
                except Exception:
                    pass

                time.sleep(2)
                elapsed += 2
            else:
                log(f"[red]  Backup task timed out after {timeout}s![/red]")
                self._working = False
                self._show_retry_hint()
                return

            # Fetch final log lines
            try:
                log_lines = self.app.proxmox.get_task_log(
                    node, upid, start=last_log_line, limit=100,
                )
                for entry in log_lines:
                    line_num = entry.get("n", 0)
                    line_text = entry.get("t", "")
                    if line_num >= last_log_line and line_text:
                        log(f"[dim]  {line_text}[/dim]")
                        last_log_line = line_num + 1
            except Exception:
                pass

            log("[green]  Backup complete![/green]\n")

            # Step 3: Parse the backup archive path from task log
            log("[bold]Locating backup archive...[/bold]")
            remote_path = ""
            archive_re = re.compile(
                r"creating vzdump archive '([^']+\.vma(?:\.\w+)?)'",
            )
            try:
                all_log = self.app.proxmox.get_task_log(
                    node, upid, start=0, limit=500,
                )
                for entry in all_log:
                    line_text = entry.get("t", "")
                    m = archive_re.search(line_text)
                    if m:
                        remote_path = m.group(1)
                        break
            except Exception as e:
                log(f"[red]  Failed to read task log: {e}[/red]")

            if not remote_path:
                log("[red]  Could not find backup archive path in task log![/red]")
                self._working = False
                self._show_retry_hint()
                return

            # Construct volid from remote path
            # Remote path like: /var/lib/vz/dump/vzdump-qemu-9000-2026_02_09-12_00_00.vma.zst
            # volid like: local:backup/vzdump-qemu-9000-2026_02_09-12_00_00.vma.zst
            archive_basename = os.path.basename(remote_path)
            backup_volid = f"{storage}:backup/{archive_basename}"
            log(f"[green]  Archive: {remote_path}[/green]")
            log(f"[dim]  Volume ID: {backup_volid}[/dim]\n")

            # Step 4: SCP download
            log("[bold]Downloading backup via SCP...[/bold]")
            local_temp_dir = tempfile.mkdtemp(prefix="infraforge-export-")
            local_vma_path = Path(local_temp_dir) / archive_basename
            log(f"[dim]  Remote: root@{host}:{remote_path}[/dim]")
            log(f"[dim]  Local:  {local_vma_path}[/dim]")

            scp_result = subprocess.run(
                [
                    "scp",
                    "-o", "StrictHostKeyChecking=accept-new",
                    f"root@{host}:{remote_path}",
                    str(local_vma_path),
                ],
                capture_output=True,
                timeout=600,
            )

            if scp_result.returncode != 0:
                stderr = scp_result.stderr.decode("utf-8", errors="replace").strip()
                log(f"[red]  SCP failed (exit {scp_result.returncode})[/red]")
                if stderr:
                    log(f"[red]  {stderr}[/red]")
                self._working = False
                self._show_retry_hint()
                return

            vma_size = local_vma_path.stat().st_size
            log(f"[green]  Downloaded: {_format_size(vma_size)}[/green]\n")

            # Step 5: Create .ifpkg package
            log("[bold]Creating .ifpkg package...[/bold]")
            exports_dir = template_package.get_exports_dir()
            output_path = exports_dir / self._output_filename
            log(f"[dim]  Output: {output_path}[/dim]")

            template_package.create_package(
                vma_path=local_vma_path,
                template_name=t.name,
                original_vmid=vmid,
                original_node=node,
                output_path=output_path,
            )

            pkg_size = output_path.stat().st_size
            log(f"[green]  Package created: {_format_size(pkg_size)}[/green]\n")

            # Step 6: Cleanup
            log("[bold]Cleaning up...[/bold]")

            # Delete backup on Proxmox
            try:
                self.app.proxmox.delete_volume(node, storage, backup_volid)
                log("[green]  Deleted remote backup[/green]")
            except Exception as e:
                log(f"[yellow]  Could not delete remote backup: {e}[/yellow]")

            # Delete local temp VMA file
            try:
                if local_vma_path and local_vma_path.exists():
                    local_vma_path.unlink()
                if local_temp_dir:
                    os.rmdir(local_temp_dir)
                log("[green]  Deleted local temp files[/green]")
            except Exception as e:
                log(f"[yellow]  Could not delete temp files: {e}[/yellow]")

            log("")
            log(
                f"[bold green]Export complete![/bold green]\n"
                f"[green]  File: {output_path}[/green]\n"
                f"[green]  Size: {_format_size(pkg_size)}[/green]"
            )

            self._export_result_path = str(output_path)
            self._export_result_size = _format_size(pkg_size)

        except subprocess.TimeoutExpired:
            log("[red]  SCP transfer timed out![/red]")
            self._working = False
            self._cleanup_temp(local_vma_path, local_temp_dir)
            self._show_retry_hint()
            return
        except Exception as e:
            log(f"\n[red]Export error: {e}[/red]")
            self._working = False
            self._cleanup_temp(local_vma_path, local_temp_dir)
            self._show_retry_hint()
            return

        self._working = False
        self._done = True

        # Transition to Phase 2 (Done)
        def _go_to_done():
            self._phase = 2
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
        self.app.call_from_thread(_go_to_done)

    def _cleanup_temp(
        self,
        local_vma_path: Optional[Path],
        local_temp_dir: Optional[str],
    ):
        """Best-effort cleanup of temp files on failure."""
        try:
            if local_vma_path and local_vma_path.exists():
                local_vma_path.unlink()
        except Exception:
            pass
        try:
            if local_temp_dir and os.path.isdir(local_temp_dir):
                os.rmdir(local_temp_dir)
        except Exception:
            pass

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


def _format_size(size_bytes: int) -> str:
    """Format a byte count into a human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"
