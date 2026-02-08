"""Template Download Screen — browse and download VM cloud images and LXC templates.

Lets users browse a curated registry of popular QEMU cloud images and
Proxmox LXC container templates (from pveam), select a target node and
storage pool, and download them with live progress tracking.
"""

from __future__ import annotations

import time
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static
from textual import work

from infraforge.models import StorageInfo
from infraforge.screens.template_update_screen import WizItem


# ---------------------------------------------------------------------------
# Curated cloud image registry
# ---------------------------------------------------------------------------

CLOUD_IMAGES = [
    {
        "name": "Ubuntu 24.04 LTS (Noble)",
        "os": "ubuntu",
        "arch": "amd64",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "filename": "noble-server-cloudimg-amd64.img",
        "format": "qcow2",
        "description": "Ubuntu 24.04 LTS cloud image with cloud-init support",
    },
    {
        "name": "Ubuntu 22.04 LTS (Jammy)",
        "os": "ubuntu",
        "arch": "amd64",
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "filename": "jammy-server-cloudimg-amd64.img",
        "format": "qcow2",
        "description": "Ubuntu 22.04 LTS cloud image with cloud-init support",
    },
    {
        "name": "Debian 12 (Bookworm)",
        "os": "debian",
        "arch": "amd64",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "format": "qcow2",
        "description": "Debian 12 generic cloud image with cloud-init",
    },
    {
        "name": "Debian 11 (Bullseye)",
        "os": "debian",
        "arch": "amd64",
        "url": "https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2",
        "filename": "debian-11-genericcloud-amd64.qcow2",
        "format": "qcow2",
        "description": "Debian 11 generic cloud image with cloud-init",
    },
    {
        "name": "Rocky Linux 9",
        "os": "rocky",
        "arch": "x86_64",
        "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "filename": "Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "format": "qcow2",
        "description": "Rocky Linux 9 generic cloud image (RHEL-compatible)",
    },
    {
        "name": "AlmaLinux 9",
        "os": "almalinux",
        "arch": "x86_64",
        "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "filename": "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "format": "qcow2",
        "description": "AlmaLinux 9 generic cloud image (RHEL-compatible)",
    },
    {
        "name": "Fedora 40 Cloud",
        "os": "fedora",
        "arch": "x86_64",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2",
        "filename": "Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2",
        "format": "qcow2",
        "description": "Fedora 40 cloud base image",
    },
    {
        "name": "openSUSE Leap 15.6",
        "os": "opensuse",
        "arch": "x86_64",
        "url": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
        "filename": "openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
        "format": "qcow2",
        "description": "openSUSE Leap 15.6 minimal cloud image",
    },
]


class TemplateDownloadScreen(Screen):
    """Browse and download VM cloud images and LXC container templates."""

    DEFAULT_CSS = """
    #dl-status {
        height: auto;
        min-height: 1;
        padding: 0 2;
        margin: 0 0 1 0;
        background: $primary-background;
        color: $text;
    }

    #dl-content {
        height: 1fr;
        border: round $primary-background;
        padding: 1 2;
    }

    #dl-hint {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    #dl-log {
        height: 1fr;
        border: round $primary-background;
        padding: 1 2;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("n", "cycle_node", "Node", show=True),
        Binding("t", "cycle_storage", "Storage", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("enter", "download", "Download", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._cloud_images: list[dict] = list(CLOUD_IMAGES)
        self._lxc_templates: list = []
        self._node_list: list[str] = []
        self._storage_list: list[StorageInfo] = []
        self._selected_node: str = ""
        self._selected_storage: str = ""
        self._items: list[WizItem] = []
        self._cursor: int = 0
        self._downloading: bool = False
        self._mount_gen: int = 0
        self._data_loaded: bool = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="wizard-container"):
            yield Static("", id="dl-status", markup=True)
            with VerticalScroll(id="dl-content"):
                pass  # items mounted dynamically
            yield Static("", id="dl-hint", markup=True)
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self):
        self._set_hint("Loading...")
        self._load_initial_data()

    # ------------------------------------------------------------------
    # Keyboard navigation
    # ------------------------------------------------------------------

    def on_key(self, event) -> None:
        if self._downloading:
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

        elif event.key in ("down", "j"):
            event.prevent_default()
            event.stop()
            nxt = self._nav_move(nav, 1)
            if nxt is not None:
                self._cursor = nxt
                self._refresh_lines()
                self._scroll_to_cursor()

    def _nav_indices(self) -> list[int]:
        return [
            i for i, it in enumerate(self._items)
            if it.kind == "option" and it.enabled
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
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self):
        if self._downloading:
            self.notify("Download in progress...", severity="warning")
            return
        self.app.pop_screen()

    def action_cycle_node(self):
        if self._downloading or not self._node_list:
            return
        if self._selected_node in self._node_list:
            idx = self._node_list.index(self._selected_node)
            self._selected_node = self._node_list[(idx + 1) % len(self._node_list)]
        else:
            self._selected_node = self._node_list[0]
        # Reset storage selection for new node
        self._pick_default_storage()
        self._build_and_mount()

    def action_cycle_storage(self):
        if self._downloading:
            return
        compatible = self._compatible_storages()
        if not compatible:
            return
        names = [s.storage for s in compatible]
        if self._selected_storage in names:
            idx = names.index(self._selected_storage)
            self._selected_storage = names[(idx + 1) % len(names)]
        else:
            self._selected_storage = names[0]
        self._update_status()

    def action_refresh(self):
        if self._downloading:
            return
        self._data_loaded = False
        self._set_hint("Refreshing...")
        self._load_initial_data()

    def action_download(self):
        if self._downloading:
            self.notify("Download already in progress", severity="warning")
            return
        if self._cursor < 0 or self._cursor >= len(self._items):
            return
        item = self._items[self._cursor]
        if item.kind != "option":
            return

        if not self._selected_node:
            self.notify("No node available", severity="error")
            return
        if not self._selected_storage:
            self.notify("No compatible storage selected (press t to cycle)", severity="error")
            return

        meta = item.meta
        if meta.get("type") == "cloud":
            self._do_download_cloud(meta)
        elif meta.get("type") == "lxc":
            self._do_download_lxc(meta)

    # ------------------------------------------------------------------
    # Build items
    # ------------------------------------------------------------------

    def _build_items(self):
        self._items = []
        items = self._items

        # Section: QEMU Cloud Images
        items.append(WizItem(
            kind="header",
            label="QEMU CLOUD IMAGES",
        ))
        if self._cloud_images:
            for i, img in enumerate(self._cloud_images):
                name_col = img["name"].ljust(32)
                arch_col = img["arch"].ljust(10)
                desc_short = img["description"][:40]
                label = (
                    f"[bold bright_white]{name_col}[/bold bright_white]"
                    f"[cyan]{arch_col}[/cyan]"
                    f"[dim]{desc_short}[/dim]"
                )
                items.append(WizItem(
                    kind="option",
                    label=label,
                    key=f"cloud:{i}",
                    meta={
                        "type": "cloud",
                        "index": i,
                        "url": img["url"],
                        "filename": img["filename"],
                        "name": img["name"],
                    },
                ))
        else:
            items.append(WizItem(kind="info", label="[dim]No cloud images in registry[/dim]"))

        items.append(WizItem(kind="info", label=""))

        # Section: Proxmox Container Templates
        items.append(WizItem(
            kind="header",
            label="PROXMOX CONTAINER TEMPLATES",
        ))
        if self._lxc_templates:
            # Group by section/os
            by_section: dict[str, list] = {}
            section_order: list[str] = []
            for t in self._lxc_templates:
                sec = t.section or "system"
                if sec not in by_section:
                    by_section[sec] = []
                    section_order.append(sec)
                by_section[sec].append(t)

            for sec in section_order:
                templates = by_section[sec]
                for t in templates:
                    name_col = t.name.ljust(40)
                    section_col = (t.section or "system").ljust(12)
                    headline_col = t.headline or t.os
                    label = (
                        f"[bold bright_magenta]{name_col}[/bold bright_magenta]"
                        f"[yellow]{section_col}[/yellow]"
                        f"[dim]{headline_col}[/dim]"
                    )
                    items.append(WizItem(
                        kind="option",
                        label=label,
                        key=f"lxc:{t.name}",
                        meta={
                            "type": "lxc",
                            "template_name": t.name,
                            "name": t.headline or t.name,
                        },
                    ))
        elif self._data_loaded:
            items.append(WizItem(
                kind="info",
                label="[yellow]No container templates available[/yellow]",
            ))
            items.append(WizItem(
                kind="info",
                label="[dim]  Ensure pveam index is up to date on your node[/dim]",
            ))
        else:
            items.append(WizItem(kind="info", label="Loading templates..."))

    def _build_and_mount(self):
        """Rebuild items, mount widgets, and update UI."""
        self._build_items()
        self._mount_items()
        nav = self._nav_indices()
        if nav:
            # Keep cursor in range
            if self._cursor not in nav:
                self._cursor = nav[0]
        self._refresh_lines()
        self._update_status()
        self._update_hint()

    # ------------------------------------------------------------------
    # Rendering (reuses template_update_screen patterns)
    # ------------------------------------------------------------------

    def _mount_items(self):
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query("#dl-log"):
            w.remove()

        self._mount_gen += 1
        gen = self._mount_gen

        scroll = self.query_one("#dl-content", VerticalScroll)
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
            lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
            return f" {cur}  {lbl}"

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

    def _update_status(self):
        node_info = (
            f"[bold]Node:[/bold] [cyan]{self._selected_node}[/cyan]"
            if self._selected_node
            else "[bold]Node:[/bold] [dim]none[/dim]"
        )
        storage_info = (
            f"[bold]Storage:[/bold] [cyan]{self._selected_storage}[/cyan]"
            if self._selected_storage
            else "[bold]Storage:[/bold] [dim]none[/dim]"
        )
        cloud_count = len(self._cloud_images)
        lxc_count = len(self._lxc_templates)
        counts = (
            f"[dim]{cloud_count} cloud images, "
            f"{lxc_count} container templates[/dim]"
        )
        try:
            self.query_one("#dl-status", Static).update(
                f"  DOWNLOAD TEMPLATES & IMAGES  |  "
                f"{node_info}  {storage_info}  |  {counts}"
            )
        except Exception:
            pass

    def _set_hint(self, text: str):
        try:
            self.query_one("#dl-hint", Static).update(f"[dim]{text}[/dim]")
        except Exception:
            pass

    def _update_hint(self):
        if self._downloading:
            self._set_hint("Download in progress...")
        else:
            self._set_hint(
                "Enter=Download  n=Node  t=Storage  r=Refresh  Esc=Back"
            )

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    def _compatible_storages(self) -> list[StorageInfo]:
        """Return storages on the selected node that can hold images or templates."""
        if not self._selected_node:
            return []
        compatible = []
        for s in self._storage_list:
            if s.node != self._selected_node and not s.shared:
                continue
            if not s.active or not s.enabled:
                continue
            content = s.content or ""
            # Accept storages that support iso, images, or vztmpl
            if any(ct in content for ct in ("iso", "images", "vztmpl")):
                compatible.append(s)
        # Deduplicate by storage name (shared storages appear on multiple nodes)
        seen: set[str] = set()
        deduped: list[StorageInfo] = []
        for s in compatible:
            if s.storage not in seen:
                seen.add(s.storage)
                deduped.append(s)
        return deduped

    def _pick_default_storage(self):
        """Pick the first compatible storage for the selected node."""
        compatible = self._compatible_storages()
        if compatible:
            self._selected_storage = compatible[0].storage
        else:
            self._selected_storage = ""

    # ------------------------------------------------------------------
    # Background data loader
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        try:
            # Fetch online nodes
            nodes = self.app.proxmox._online_node_names()
            self._node_list = nodes
            if nodes and not self._selected_node:
                self._selected_node = nodes[0]

            # Fetch storage pools
            self._storage_list = self.app.proxmox.get_storage_info()

            # Pick default storage if none selected
            if not self._selected_storage:
                self._pick_default_storage()

            # Fetch available LXC templates from Proxmox appliance index
            self._lxc_templates = self.app.proxmox.get_appliance_templates(
                node=self._selected_node
            )

            self._data_loaded = True
            self.app.call_from_thread(self._on_data_loaded)
        except Exception as e:
            self._data_loaded = True

            def _show_err():
                self._set_hint(f"[red]Error loading data: {e}[/red]")
                self._build_and_mount()

            self.app.call_from_thread(_show_err)

    def _on_data_loaded(self):
        self._build_and_mount()

    # ------------------------------------------------------------------
    # Download: cloud image
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_download_cloud(self, meta: dict):
        self._downloading = True
        self.app.call_from_thread(self._show_download_log)

        url = meta["url"]
        filename = meta["filename"]
        name = meta["name"]
        node = self._selected_node
        storage = self._selected_storage

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#dl-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        try:
            log(f"[bold]Downloading cloud image: {name}[/bold]")
            log(f"[dim]  URL: {url}[/dim]")
            log(f"[dim]  Target: {node}:{storage} as {filename}[/dim]")
            log("")

            upid = self.app.proxmox.download_url_to_storage(
                node, storage, url, filename, content="iso",
            )
            log(f"[dim]  Task started: {upid}[/dim]")
            log("[dim]  Polling for progress...[/dim]\n")

            last_line = 0
            while True:
                status = self.app.proxmox.get_task_status(node, upid)
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status == "OK":
                        log("[green]Download complete![/green]\n")
                        log(
                            f"[bold green]{name}[/bold green] is now available "
                            f"in [cyan]{storage}[/cyan] on [cyan]{node}[/cyan] "
                            f"as an ISO image."
                        )
                        log(
                            "[dim]To use as a VM template: create a VM, import "
                            "this disk via 'qm importdisk', then convert to "
                            "template.[/dim]"
                        )
                    else:
                        log(f"[red]Download failed: {exit_status}[/red]")
                    break

                # Get latest log lines
                try:
                    log_lines = self.app.proxmox.get_task_log(
                        node, upid, start=last_line
                    )
                    for entry in log_lines:
                        text = entry.get("t", "")
                        if text:
                            log(f"[dim]{text}[/dim]")
                        n = entry.get("n", last_line)
                        if n >= last_line:
                            last_line = n + 1
                except Exception:
                    pass

                time.sleep(2)

        except Exception as e:
            log(f"\n[red]Download error: {e}[/red]")

        self._downloading = False
        self.app.call_from_thread(self._on_download_done)

    # ------------------------------------------------------------------
    # Download: LXC template
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_download_lxc(self, meta: dict):
        self._downloading = True
        self.app.call_from_thread(self._show_download_log)

        template_name = meta["template_name"]
        display_name = meta["name"]
        node = self._selected_node
        storage = self._selected_storage

        def log(msg: str):
            def _update():
                try:
                    self.query_one("#dl-log", RichLog).write(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_update)

        try:
            log(f"[bold]Downloading container template: {display_name}[/bold]")
            log(f"[dim]  Template: {template_name}[/dim]")
            log(f"[dim]  Target: {node}:{storage}[/dim]")
            log("")

            upid = self.app.proxmox.download_appliance_template(
                node, storage, template_name,
            )
            log(f"[dim]  Task started: {upid}[/dim]")
            log("[dim]  Polling for progress...[/dim]\n")

            last_line = 0
            while True:
                status = self.app.proxmox.get_task_status(node, upid)
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status == "OK":
                        log("[green]Download complete![/green]\n")
                        log(
                            f"[bold green]{display_name}[/bold green] is now "
                            f"available in [cyan]{storage}[/cyan] on "
                            f"[cyan]{node}[/cyan]."
                        )
                        log(
                            "[dim]You can now create LXC containers using "
                            "this template.[/dim]"
                        )
                    else:
                        log(f"[red]Download failed: {exit_status}[/red]")
                    break

                # Get latest log lines
                try:
                    log_lines = self.app.proxmox.get_task_log(
                        node, upid, start=last_line
                    )
                    for entry in log_lines:
                        text = entry.get("t", "")
                        if text:
                            log(f"[dim]{text}[/dim]")
                        n = entry.get("n", last_line)
                        if n >= last_line:
                            last_line = n + 1
                except Exception:
                    pass

                time.sleep(2)

        except Exception as e:
            log(f"\n[red]Download error: {e}[/red]")

        self._downloading = False
        self.app.call_from_thread(self._on_download_done)

    # ------------------------------------------------------------------
    # Download UI helpers
    # ------------------------------------------------------------------

    def _show_download_log(self):
        """Replace item list with a RichLog for download progress."""
        for w in self.query(".wiz-line"):
            w.remove()
        for w in self.query("#dl-log"):
            w.remove()

        scroll = self.query_one("#dl-content", VerticalScroll)
        scroll.mount(RichLog(markup=True, id="dl-log"))
        self._set_hint("Download in progress...")

    def _on_download_done(self):
        """Restore hint after download finishes."""
        self._set_hint(
            "[bold green]Done![/bold green]  "
            "Press [b]r[/b] to refresh list  |  Esc=Back"
        )
