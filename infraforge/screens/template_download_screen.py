"""Template Download Screen — browse and download VM cloud images and LXC templates.

Lets users browse a curated registry of popular QEMU cloud images and
Proxmox LXC container templates (from pveam), select a target node and
storage pool, and download them with live progress tracking.

Flow:
  1. Browse phase  — navigate the template list, press Enter to select
  2. Config phase  — pick target node and compatible storage
  3. Download       — live progress log
"""

from __future__ import annotations

import re
import time
import urllib.request
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
        "checksum_url": "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        "checksum_algo": "sha256sum",
    },
    {
        "name": "Ubuntu 22.04 LTS (Jammy)",
        "os": "ubuntu",
        "arch": "amd64",
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "filename": "jammy-server-cloudimg-amd64.img",
        "format": "qcow2",
        "description": "Ubuntu 22.04 LTS cloud image with cloud-init support",
        "checksum_url": "https://cloud-images.ubuntu.com/jammy/current/SHA256SUMS",
        "checksum_algo": "sha256sum",
    },
    {
        "name": "Debian 12 (Bookworm)",
        "os": "debian",
        "arch": "amd64",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "format": "qcow2",
        "description": "Debian 12 generic cloud image with cloud-init",
        "checksum_url": "https://cloud.debian.org/images/cloud/bookworm/latest/SHA512SUMS",
        "checksum_algo": "sha512sum",
    },
    {
        "name": "Debian 11 (Bullseye)",
        "os": "debian",
        "arch": "amd64",
        "url": "https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2",
        "filename": "debian-11-genericcloud-amd64.qcow2",
        "format": "qcow2",
        "description": "Debian 11 generic cloud image with cloud-init",
        "checksum_url": "https://cloud.debian.org/images/cloud/bullseye/latest/SHA512SUMS",
        "checksum_algo": "sha512sum",
    },
    {
        "name": "Rocky Linux 9",
        "os": "rocky",
        "arch": "x86_64",
        "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "filename": "Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        "format": "qcow2",
        "description": "Rocky Linux 9 generic cloud image (RHEL-compatible)",
        "checksum_url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/CHECKSUM",
        "checksum_algo": "sha256sum",
    },
    {
        "name": "AlmaLinux 9",
        "os": "almalinux",
        "arch": "x86_64",
        "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "filename": "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "format": "qcow2",
        "description": "AlmaLinux 9 generic cloud image (RHEL-compatible)",
        "checksum_url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/CHECKSUM",
        "checksum_algo": "sha256sum",
    },
    {
        "name": "Fedora 40 Cloud",
        "os": "fedora",
        "arch": "x86_64",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2",
        "filename": "Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2",
        "format": "qcow2",
        "description": "Fedora 40 cloud base image",
        "checksum_url": "https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-40-1.14-x86_64-CHECKSUM",
        "checksum_algo": "sha256sum",
    },
    {
        "name": "openSUSE Leap 15.6",
        "os": "opensuse",
        "arch": "x86_64",
        "url": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
        "filename": "openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
        "format": "qcow2",
        "description": "openSUSE Leap 15.6 minimal cloud image",
        "checksum_url": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2.sha256",
        "checksum_algo": "sha256sum",
    },
]

def _fetch_checksum(checksum_url: str, filename: str) -> str | None:
    """Fetch a checksum file and extract the hash for the given filename.

    Supports standard formats:
      - 'HASH  filename'  (sha256sum/sha512sum output)
      - 'SHA256 (filename) = HASH'  (BSD/Fedora/Rocky style)
      - Single hash on its own line (openSUSE .sha256 files)
    Returns the hex hash string, or None on failure.
    """
    try:
        req = urllib.request.Request(checksum_url, headers={"User-Agent": "InfraForge"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # BSD style: SHA256 (filename) = HASH
        m = re.match(r'SHA(?:256|512)\s+\((.+?)\)\s*=\s*([0-9a-fA-F]+)', line)
        if m and m.group(1) == filename:
            return m.group(2).lower()
        # Standard: HASH  filename  or  HASH *filename
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*").strip() == filename:
            return parts[0].lower()
        # Single-hash file (e.g. openSUSE .sha256)
        if len(parts) == 1 and re.fullmatch(r'[0-9a-fA-F]{64,128}', parts[0]):
            return parts[0].lower()
    return None


# Block-based storage types that cannot accept file uploads
_BLOCK_STORAGE_TYPES = frozenset({
    "lvm", "lvmthin", "zfs", "zfspool", "rbd", "iscsi", "iscsidirect",
})

# Storage column widths for config phase display
_STOR_NAME_W = 18
_STOR_TYPE_W = 12
_STOR_FREE_W = 12
_STOR_TOTAL_W = 12
_STOR_USED_W = 10


def _stor_label(s: StorageInfo) -> str:
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
    """Column header for storage table."""
    return (
        f"   [dim]"
        f"{'Name'.ljust(_STOR_NAME_W)}"
        f"{'Type'.ljust(_STOR_TYPE_W)}"
        f"{'Free'.ljust(_STOR_FREE_W)}"
        f"{'Total'.ljust(_STOR_TOTAL_W)}"
        f"{'Used'.ljust(_STOR_USED_W)}"
        f"[/dim]"
    )


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
        Binding("r", "refresh", "Refresh", show=True),
        Binding("enter", "select", "Select", show=True),
    ]

    def __init__(self):
        super().__init__()
        self._cloud_images: list[dict] = list(CLOUD_IMAGES)
        self._lxc_templates: list = []
        self._node_list: list[str] = []
        self._storage_list: list[StorageInfo] = []
        self._items: list[WizItem] = []
        self._cursor: int = 0
        self._downloading: bool = False
        self._mount_gen: int = 0
        self._data_loaded: bool = False

        # Phase management: "browse" (template list) or "config" (node/storage)
        self._phase: str = "browse"
        self._pending_download: dict | None = None

        # Selections for config phase
        self._selected_dl_node: str = ""
        self._selected_dl_storage: str = ""

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

        elif event.key == "space":
            event.prevent_default()
            event.stop()
            if self._phase == "config":
                self._activate_item()
            elif self._phase == "browse":
                self._enter_config_phase()

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
    # Item activation (radio selection in config phase)
    # ------------------------------------------------------------------

    def _activate_item(self):
        """Handle Enter/Space on an item in config phase."""
        if self._cursor < 0 or self._cursor >= len(self._items):
            return
        item = self._items[self._cursor]

        # Confirm button
        if item.kind == "option" and item.key == "_confirm":
            self._confirm_download()
            return

        if item.kind != "option" or not item.group:
            return

        # Deselect all in same group, select this one
        for it in self._items:
            if it.group == item.group:
                it.selected = False
        item.selected = True

        if item.group == "dl_node":
            self._selected_dl_node = item.key
            # Rebuild storage list for the new node, then focus first storage
            self._rebuild_storage_options()
            return  # _rebuild_storage_options handles cursor + refresh
        elif item.group == "dl_storage":
            self._selected_dl_storage = item.key
            # Auto-focus the confirm button
            self._focus_confirm_button()

        self._refresh_lines()

    def _rebuild_storage_options(self):
        """After node change, rebuild only the storage radio options."""
        if not self._pending_download:
            return

        content_type = self._content_type_for_download(self._pending_download)
        compatible = self._compatible_storages(content_type, self._selected_dl_node)

        # Find the storage section in _items and replace it
        stor_header_idx: int | None = None
        stor_end_idx: int | None = None
        for i, it in enumerate(self._items):
            if it.kind == "header" and it.key == "_stor_header":
                stor_header_idx = i
            elif stor_header_idx is not None and it.kind == "header" and it.key != "_stor_header":
                stor_end_idx = i
                break
        if stor_header_idx is None:
            return
        if stor_end_idx is None:
            stor_end_idx = len(self._items)

        # Build new storage items
        new_items: list[WizItem] = []
        new_items.append(WizItem(
            kind="header",
            label="TARGET STORAGE",
            key="_stor_header",
        ))
        new_items.append(WizItem(kind="info", label=_stor_header()))

        if compatible:
            # Auto-select first if current selection is not in new list
            names = [s.storage for s in compatible]
            if self._selected_dl_storage not in names:
                self._selected_dl_storage = names[0]

            for s in compatible:
                sel = s.storage == self._selected_dl_storage
                new_items.append(WizItem(
                    kind="option",
                    label=_stor_label(s),
                    key=s.storage,
                    group="dl_storage",
                    selected=sel,
                ))
        else:
            self._selected_dl_storage = ""
            new_items.append(WizItem(
                kind="info",
                label="[red]No compatible storage found for this node[/red]",
            ))
            new_items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]  Need a file-based storage with "
                    f"'{content_type}' content support[/dim]"
                ),
            ))

        # Confirm button
        new_items.append(WizItem(kind="info", label=""))
        if self._selected_dl_storage:
            new_items.append(WizItem(
                kind="option",
                label=(
                    "[bold white on dark_green]  CONFIRM DOWNLOAD  "
                    "[/bold white on dark_green]"
                ),
                key="_confirm",
            ))
        else:
            new_items.append(WizItem(
                kind="info",
                label=(
                    "[dim]Select a compatible storage above "
                    "to enable download[/dim]"
                ),
            ))

        # Replace the storage section in _items
        self._items[stor_header_idx:stor_end_idx] = new_items

        # Re-mount everything to get correct widget IDs
        self._mount_items()

        # Keep cursor in valid range
        nav = self._nav_indices()
        if nav and self._cursor not in nav:
            # Try to land on first storage option
            for idx in nav:
                if (idx < len(self._items)
                        and self._items[idx].group == "dl_storage"):
                    self._cursor = idx
                    break
            else:
                self._cursor = nav[-1]

        self._refresh_lines()
        self._scroll_to_cursor()
        self._update_status()

    def _focus_confirm_button(self):
        """Move cursor to the CONFIRM button item."""
        for i, it in enumerate(self._items):
            if it.key == "_confirm":
                self._cursor = i
                self._refresh_lines()
                self._scroll_to_cursor()
                return

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self):
        if self._downloading:
            self.notify("Download in progress...", severity="warning")
            return
        if self._phase == "config":
            # Return to browse phase
            self._phase = "browse"
            self._pending_download = None
            self._build_and_mount()
            return
        self.app.pop_screen()

    def action_refresh(self):
        if self._downloading:
            return
        if self._phase == "config":
            return
        self._data_loaded = False
        self._set_hint("Refreshing...")
        self._load_initial_data()

    def action_select(self):
        """Handle Enter binding — selects in browse, activates in config."""
        if self._downloading:
            self.notify("Download already in progress", severity="warning")
            return

        if self._phase == "browse":
            self._enter_config_phase()
        elif self._phase == "config":
            self._activate_item()

    def _enter_config_phase(self):
        """Validate selection in browse phase and transition to config."""
        if self._cursor < 0 or self._cursor >= len(self._items):
            return
        item = self._items[self._cursor]
        if item.kind != "option":
            return

        meta = item.meta
        if not meta:
            return

        # Store what was selected
        self._pending_download = dict(meta)

        # Default node/storage selections
        if self._node_list:
            self._selected_dl_node = self._node_list[0]
        else:
            self._selected_dl_node = ""

        # Pick default storage for the default node
        content_type = self._content_type_for_download(meta)
        compatible = self._compatible_storages(content_type, self._selected_dl_node)
        if compatible:
            self._selected_dl_storage = compatible[0].storage
        else:
            self._selected_dl_storage = ""

        # Switch phase and build config UI
        self._phase = "config"
        self._build_and_mount()

    def _confirm_download(self):
        """Validate config selections and start the download."""
        if not self._pending_download:
            return
        if not self._selected_dl_node:
            self.notify("No node selected", severity="error")
            return
        if not self._selected_dl_storage:
            self.notify("No compatible storage selected", severity="error")
            return

        meta = self._pending_download
        if meta.get("type") == "cloud":
            self._do_download_cloud(meta)
        elif meta.get("type") == "lxc":
            self._do_download_lxc(meta)

    # ------------------------------------------------------------------
    # Build items — browse phase
    # ------------------------------------------------------------------

    def _build_browse_items(self):
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

    # ------------------------------------------------------------------
    # Build items — config phase (node + storage selection)
    # ------------------------------------------------------------------

    def _build_config_items(self):
        self._items = []
        items = self._items
        meta = self._pending_download or {}

        dl_type = meta.get("type", "")
        dl_name = meta.get("name", "unknown")
        type_label = "Cloud Image" if dl_type == "cloud" else "Container Template"
        content_type = self._content_type_for_download(meta)

        # Header: what is being downloaded
        items.append(WizItem(
            kind="header",
            label="DOWNLOAD CONFIGURATION",
        ))
        items.append(WizItem(
            kind="info",
            label=(
                f"[bold bright_white]{dl_name}[/bold bright_white]"
                f"  [dim]({type_label})[/dim]"
            ),
        ))
        items.append(WizItem(kind="info", label=""))

        # Node selection
        items.append(WizItem(
            kind="header",
            label="TARGET NODE",
        ))
        if self._node_list:
            for node_name in self._node_list:
                sel = node_name == self._selected_dl_node
                items.append(WizItem(
                    kind="option",
                    label=f"[bold bright_white]{node_name}[/bold bright_white]",
                    key=node_name,
                    group="dl_node",
                    selected=sel,
                ))
        else:
            items.append(WizItem(
                kind="info",
                label="[red]No online nodes found[/red]",
            ))

        items.append(WizItem(kind="info", label=""))

        # Storage selection
        items.append(WizItem(
            kind="header",
            label="TARGET STORAGE",
            key="_stor_header",
        ))
        items.append(WizItem(kind="info", label=_stor_header()))

        compatible = self._compatible_storages(content_type, self._selected_dl_node)
        if compatible:
            # Ensure current selection is valid
            names = [s.storage for s in compatible]
            if self._selected_dl_storage not in names:
                self._selected_dl_storage = names[0]

            for s in compatible:
                sel = s.storage == self._selected_dl_storage
                items.append(WizItem(
                    kind="option",
                    label=_stor_label(s),
                    key=s.storage,
                    group="dl_storage",
                    selected=sel,
                ))
        else:
            self._selected_dl_storage = ""
            items.append(WizItem(
                kind="info",
                label="[red]No compatible storage found for this node[/red]",
            ))
            items.append(WizItem(
                kind="info",
                label=(
                    f"[dim]  Need a file-based storage with "
                    f"'{content_type}' content support[/dim]"
                ),
            ))

        # Confirm button
        items.append(WizItem(kind="info", label=""))
        if self._selected_dl_storage:
            items.append(WizItem(
                kind="option",
                label=(
                    "[bold white on dark_green]  CONFIRM DOWNLOAD  [/bold white on dark_green]"
                ),
                key="_confirm",
            ))
        else:
            items.append(WizItem(
                kind="info",
                label="[dim]Select a node and storage above to enable download[/dim]",
            ))

    # ------------------------------------------------------------------
    # Common build + mount
    # ------------------------------------------------------------------

    def _build_and_mount(self):
        """Rebuild items for the current phase, mount widgets, update UI."""
        if self._phase == "browse":
            self._build_browse_items()
        else:
            self._build_config_items()

        self._mount_items()
        nav = self._nav_indices()
        if nav:
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
            if item.group:
                # Radio button style (config phase)
                mark = (
                    "[green]\u25cf[/green]" if item.selected
                    else "[dim]\u25cb[/dim]"
                )
                lbl = f"[bold]{item.label}[/bold]" if is_cur else item.label
                return f" {cur} {mark}  {lbl}"
            else:
                # Simple selectable (browse phase)
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
        if self._phase == "config" and self._pending_download:
            dl_name = self._pending_download.get("name", "")
            node_info = (
                f"[bold]Node:[/bold] [cyan]{self._selected_dl_node}[/cyan]"
                if self._selected_dl_node
                else "[bold]Node:[/bold] [dim]none[/dim]"
            )
            storage_info = (
                f"[bold]Storage:[/bold] [cyan]{self._selected_dl_storage}[/cyan]"
                if self._selected_dl_storage
                else "[bold]Storage:[/bold] [dim]none[/dim]"
            )
            try:
                self.query_one("#dl-status", Static).update(
                    f"  DOWNLOAD: {dl_name}  |  "
                    f"{node_info}  {storage_info}"
                )
            except Exception:
                pass
        else:
            cloud_count = len(self._cloud_images)
            lxc_count = len(self._lxc_templates)
            counts = (
                f"[dim]{cloud_count} cloud images, "
                f"{lxc_count} container templates[/dim]"
            )
            try:
                self.query_one("#dl-status", Static).update(
                    f"  DOWNLOAD TEMPLATES & IMAGES  |  {counts}"
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
        elif self._phase == "config":
            self._set_hint(
                "Enter=Select  Backspace=Back"
            )
        else:
            self._set_hint(
                "Enter=Select  r=Refresh  Esc=Back"
            )

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _content_type_for_download(meta: dict) -> str:
        """Return the required content type for a download meta dict."""
        if meta.get("type") == "cloud":
            return "iso"
        return "vztmpl"

    def _compatible_storages(
        self, content_type: str, node: str = ""
    ) -> list[StorageInfo]:
        """Return storages on the given node that support the content type.

        Only file-based storages are returned; block-based storage types
        (lvm, lvmthin, zfs, zfspool, rbd, iscsi, iscsidirect) are excluded
        because Proxmox cannot upload files to them.

        Args:
            content_type: Required content type — "iso" for cloud images,
                          "vztmpl" for LXC templates.
            node: Target node name. If empty, returns nothing.
        """
        if not node:
            return []
        compatible = []
        for s in self._storage_list:
            # Must be on the target node (or shared)
            if s.node != node and not s.shared:
                continue
            if not s.active or not s.enabled:
                continue
            # Exclude block-based storage types
            if s.storage_type in _BLOCK_STORAGE_TYPES:
                continue
            # Must support the requested content type
            content = s.content or ""
            if content_type not in content:
                continue
            compatible.append(s)

        # Deduplicate by storage name (shared storages appear on multiple nodes)
        seen: set[str] = set()
        deduped: list[StorageInfo] = []
        for s in compatible:
            if s.storage not in seen:
                seen.add(s.storage)
                deduped.append(s)
        return deduped

    # ------------------------------------------------------------------
    # Background data loader
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        try:
            # Fetch online nodes
            node_list = self.app.proxmox._online_node_names()
            self._node_list = node_list

            # Fetch storage pools
            self._storage_list = self.app.proxmox.get_storage_info()

            # Fetch available LXC templates from Proxmox appliance index
            fetch_node = node_list[0] if node_list else ""
            if fetch_node:
                self._lxc_templates = self.app.proxmox.get_appliance_templates(
                    node=fetch_node
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
        node = self._selected_dl_node
        storage = self._selected_dl_storage

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

            # Fetch integrity checksum from distro's official checksum file
            checksum_url = meta.get("checksum_url")
            checksum_algo = meta.get("checksum_algo")
            checksum_hash = None
            if checksum_url:
                log(f"[dim]  Fetching checksum from {checksum_url}...[/dim]")
                checksum_hash = _fetch_checksum(checksum_url, filename)
                if checksum_hash:
                    log(f"[dim]  Checksum ({checksum_algo}): {checksum_hash[:16]}...[/dim]")
                else:
                    log("[yellow]  Warning: Could not extract checksum — downloading without verification[/yellow]")
            log("")

            upid = self.app.proxmox.download_url_to_storage(
                node, storage, url, filename, content="iso",
                checksum=checksum_hash, checksum_algorithm=checksum_algo,
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
        node = self._selected_dl_node
        storage = self._selected_dl_storage

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
        self._phase = "browse"
        self._pending_download = None
        self._set_hint(
            "[bold green]Done![/bold green]  "
            "Press [b]r[/b] to refresh list  |  Esc=Back"
        )
