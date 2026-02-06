"""Ansible Management screen for InfraForge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Tree, Input, Select, Button, TextArea
from textual.widgets._tree import TreeNode
from textual.containers import Container, Horizontal, Vertical, Center, Middle
from textual import work

from rich.text import Text


# ---------------------------------------------------------------------------
# Mock data for the UI prototype
# ---------------------------------------------------------------------------

SAMPLE_PLAYBOOKS = [
    {
        "name": "provision-webserver",
        "description": "Install nginx, configure SSL certs, set up reverse proxy",
        "category": "Provisioning",
        "hosts": "webservers",
        "tags": ["nginx", "ssl", "proxy"],
        "last_run": "2025-12-14 09:32",
        "last_status": "success",
        "tasks": 12,
    },
    {
        "name": "harden-ssh",
        "description": "Disable root login, set up key-only auth, configure fail2ban",
        "category": "Security",
        "hosts": "all",
        "tags": ["ssh", "security", "hardening"],
        "last_run": "2025-12-13 15:20",
        "last_status": "success",
        "tasks": 8,
    },
    {
        "name": "deploy-monitoring",
        "description": "Install Prometheus node exporter and Grafana agent on all nodes",
        "category": "Monitoring",
        "hosts": "all",
        "tags": ["prometheus", "grafana", "monitoring"],
        "last_run": "2025-12-10 22:00",
        "last_status": "failed",
        "tasks": 15,
    },
    {
        "name": "update-packages",
        "description": "Run apt update && apt upgrade on all Debian/Ubuntu hosts",
        "category": "Maintenance",
        "hosts": "debian",
        "tags": ["apt", "updates", "patching"],
        "last_run": "2025-12-12 03:00",
        "last_status": "success",
        "tasks": 4,
    },
    {
        "name": "configure-dns-client",
        "description": "Set resolv.conf, configure systemd-resolved for internal DNS",
        "category": "Provisioning",
        "hosts": "all",
        "tags": ["dns", "resolv", "networking"],
        "last_run": None,
        "last_status": "never",
        "tasks": 6,
    },
    {
        "name": "backup-databases",
        "description": "Dump MariaDB/PostgreSQL databases to NFS share, rotate old backups",
        "category": "Maintenance",
        "hosts": "db_servers",
        "tags": ["backup", "mariadb", "postgres"],
        "last_run": "2025-12-14 02:00",
        "last_status": "success",
        "tasks": 9,
    },
    {
        "name": "deploy-container-app",
        "description": "Pull latest Docker images, docker-compose up with health checks",
        "category": "Deployment",
        "hosts": "app_servers",
        "tags": ["docker", "compose", "deploy"],
        "last_run": "2025-12-13 18:45",
        "last_status": "success",
        "tasks": 7,
    },
    {
        "name": "rotate-certificates",
        "description": "Renew Let's Encrypt certs via certbot, restart affected services",
        "category": "Security",
        "hosts": "web_servers",
        "tags": ["ssl", "certbot", "letsencrypt"],
        "last_run": "2025-12-01 04:00",
        "last_status": "success",
        "tasks": 5,
    },
]

SAMPLE_INVENTORIES = [
    {"name": "production", "hosts": 14, "groups": ["webservers", "db_servers", "app_servers", "all"]},
    {"name": "staging", "hosts": 6, "groups": ["webservers", "db_servers", "all"]},
    {"name": "development", "hosts": 3, "groups": ["dev_boxes", "all"]},
]

SAMPLE_ROLES = [
    {"name": "common", "description": "Base packages, users, SSH keys, timezone", "tasks": 8},
    {"name": "nginx", "description": "Install and configure nginx reverse proxy", "tasks": 6},
    {"name": "docker", "description": "Install Docker CE and docker-compose", "tasks": 5},
    {"name": "monitoring", "description": "Prometheus node_exporter + Grafana agent", "tasks": 7},
    {"name": "hardening", "description": "CIS benchmark security hardening", "tasks": 14},
    {"name": "backup", "description": "Automated backup scripts and rotation", "tasks": 4},
]

CATEGORY_COLORS = {
    "Provisioning": "cyan",
    "Security": "red",
    "Monitoring": "magenta",
    "Maintenance": "yellow",
    "Deployment": "green",
}

STATUS_COLORS = {
    "success": "green",
    "failed": "red",
    "running": "yellow",
    "never": "bright_black",
}


@dataclass
class AnsibleNodeData:
    """Data attached to each node in the ansible tree."""
    kind: Literal["section", "category", "playbook", "inventory", "role", "placeholder"]
    record: dict = field(default_factory=dict)
    section: str = ""
    category: str = ""


# ---------------------------------------------------------------------------
# Label builders
# ---------------------------------------------------------------------------

_PB_NAME_WIDTH = 28


def _make_playbook_label(pb: dict) -> Text:
    """Build aligned label for a playbook leaf."""
    name = pb["name"]
    if len(name) > _PB_NAME_WIDTH:
        name_col = name[:_PB_NAME_WIDTH - 2] + ".."
    else:
        name_col = name.ljust(_PB_NAME_WIDTH)

    status = pb.get("last_status", "never")
    status_color = STATUS_COLORS.get(status, "bright_black")
    status_col = f"[{status}]".ljust(12)

    tasks_col = f"{pb.get('tasks', 0)} tasks".ljust(10)

    hosts_col = pb.get("hosts", "").ljust(16)

    label = Text()
    label.append(name_col, style="bold")
    label.append("    ", style="default")
    label.append(status_col, style=status_color)
    label.append(tasks_col, style="dim")
    label.append(hosts_col, style="cyan")
    return label


def _make_inventory_label(inv: dict) -> Text:
    name_col = inv["name"].ljust(20)
    hosts_col = f"{inv['hosts']} hosts".ljust(12)
    groups_col = f"{len(inv['groups'])} groups"
    label = Text()
    label.append(name_col, style="bold")
    label.append("    ", style="default")
    label.append(hosts_col, style="cyan")
    label.append(groups_col, style="dim")
    return label


def _make_role_label(role: dict) -> Text:
    name_col = role["name"].ljust(20)
    tasks_col = f"{role['tasks']} tasks".ljust(12)
    desc = role.get("description", "")
    if len(desc) > 40:
        desc = desc[:38] + ".."
    label = Text()
    label.append(name_col, style="bold magenta")
    label.append("    ", style="default")
    label.append(tasks_col, style="dim")
    label.append(desc, style="dim italic")
    return label


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------

class PlaybookInputScreen(Screen):
    """Modal for creating/editing a playbook."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, playbook: dict | None = None):
        super().__init__()
        self._playbook = playbook or {}
        self._editing = bool(playbook)

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Middle():
                with Container(classes="modal-box"):
                    title = "Edit Playbook" if self._editing else "New Playbook"
                    yield Static(f"[bold]{title}[/bold]", classes="modal-title", markup=True)
                    yield Static("[bold]Name:[/bold]", markup=True)
                    yield Input(
                        value=self._playbook.get("name", ""),
                        placeholder="e.g. deploy-webserver",
                        id="pb-name",
                    )
                    yield Static("[bold]Description:[/bold]", markup=True)
                    yield Input(
                        value=self._playbook.get("description", ""),
                        placeholder="What does this playbook do?",
                        id="pb-desc",
                    )
                    yield Static("[bold]Target hosts:[/bold]", markup=True)
                    yield Input(
                        value=self._playbook.get("hosts", "all"),
                        placeholder="e.g. webservers, all, db_servers",
                        id="pb-hosts",
                    )
                    yield Static("[bold]Category:[/bold]", markup=True)
                    yield Select(
                        [(c, c) for c in ["Provisioning", "Security", "Monitoring", "Maintenance", "Deployment"]],
                        value=self._playbook.get("category", "Provisioning"),
                        id="pb-category",
                    )
                    yield Static("[bold]Tags (comma separated):[/bold]", markup=True)
                    yield Input(
                        value=", ".join(self._playbook.get("tags", [])),
                        placeholder="e.g. nginx, ssl, deploy",
                        id="pb-tags",
                    )
                    yield Static("")
                    yield Static(
                        "[dim italic]AI Assist: In a future update, type a natural language "
                        "description and AI will generate the playbook YAML for you.[/dim italic]",
                        markup=True,
                    )
                    with Horizontal(classes="modal-buttons"):
                        yield Button("Save", variant="primary", id="pb-save")
                        yield Button("Cancel", variant="default", id="pb-cancel")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pb-save":
            self.app.pop_screen()
            self.notify("Playbook saved (prototype — not persisted)", timeout=3)
        elif event.button.id == "pb-cancel":
            self.app.pop_screen()

    def action_cancel(self):
        self.app.pop_screen()


class RunPlaybookScreen(Screen):
    """Modal for running a playbook with options."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, playbook: dict):
        super().__init__()
        self._playbook = playbook

    def compose(self) -> ComposeResult:
        pb = self._playbook
        yield Header()
        with Center():
            with Middle():
                with Container(classes="modal-box"):
                    yield Static("[bold]Run Playbook[/bold]", classes="modal-title", markup=True)
                    yield Static(
                        f"[bold]Playbook:[/bold]  {pb['name']}\n"
                        f"[bold]Hosts:[/bold]      {pb.get('hosts', 'all')}\n"
                        f"[bold]Tasks:[/bold]      {pb.get('tasks', '?')}",
                        markup=True,
                    )
                    yield Static("")
                    yield Static("[bold]Inventory:[/bold]", markup=True)
                    yield Select(
                        [(inv["name"], inv["name"]) for inv in SAMPLE_INVENTORIES],
                        value="production",
                        id="run-inventory",
                    )
                    yield Static("[bold]Extra variables (key=value):[/bold]", markup=True)
                    yield Input(
                        placeholder="e.g. env=staging version=1.2.3",
                        id="run-extra-vars",
                    )
                    yield Static("[bold]Options:[/bold]", markup=True)
                    yield Static(
                        "  [dim]--check (dry run)    --diff    --verbose[/dim]",
                        markup=True,
                    )
                    yield Static("")
                    yield Static(
                        "[dim italic]Execution engine coming in a future update. "
                        "Will stream ansible-playbook output in real time.[/dim italic]",
                        markup=True,
                    )
                    with Horizontal(classes="modal-buttons"):
                        yield Button("Run", variant="primary", id="run-go")
                        yield Button("Dry Run", variant="warning", id="run-check")
                        yield Button("Cancel", variant="default", id="run-cancel")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-go":
            self.app.pop_screen()
            self.notify("Playbook execution not yet implemented", timeout=3)
        elif event.button.id == "run-check":
            self.app.pop_screen()
            self.notify("Dry run not yet implemented", timeout=3)
        elif event.button.id == "run-cancel":
            self.app.pop_screen()

    def action_cancel(self):
        self.app.pop_screen()


class ConfirmScreen(Screen):
    """Generic confirmation modal."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str, callback):
        super().__init__()
        self._message = message
        self._callback = callback

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Middle():
                with Container(classes="modal-box"):
                    yield Static("[bold]Confirm[/bold]", classes="modal-title", markup=True)
                    yield Static(self._message, markup=True)
                    with Horizontal(classes="modal-buttons"):
                        yield Button("Yes", variant="error", id="confirm-yes")
                        yield Button("No", variant="default", id="confirm-no")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()
        if event.button.id == "confirm-yes":
            self._callback(True)
        else:
            self._callback(False)

    def action_cancel(self):
        self.app.pop_screen()
        self._callback(False)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class AnsibleScreen(Screen):
    """Ansible Management screen with tree layout and detail panel."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("a", "add_playbook", "Add", show=True),
        Binding("e", "edit_playbook", "Edit", show=True),
        Binding("d", "delete_playbook", "Delete", show=True),
        Binding("x", "run_playbook", "Run", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    SORT_FIELDS = ["name", "category", "status", "hosts", "tasks"]
    SORT_LABELS = ["Name", "Category", "Status", "Hosts", "Tasks"]

    def __init__(self):
        super().__init__()
        self._playbooks = list(SAMPLE_PLAYBOOKS)
        self._inventories = list(SAMPLE_INVENTORIES)
        self._roles = list(SAMPLE_ROLES)
        self._sort_index: int = 0
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="ansible-container"):
            yield Static("Ansible Management", classes="section-title")
            yield Static("", id="ansible-banner", markup=True)
            with Horizontal(id="ansible-controls"):
                yield Static("", id="ansible-sort-label", markup=True)
                yield Static("", id="ansible-count-label", markup=True)
            with Horizontal(id="ansible-main-content"):
                yield Tree("Ansible", id="ansible-tree")
                with Container(id="ansible-detail-panel"):
                    yield Static("[bold]Details[/bold]", id="ansible-detail-title", markup=True)
                    yield Static(
                        "[dim]Select an item to view details.[/dim]",
                        id="ansible-detail-content",
                        markup=True,
                    )
            yield Static("", id="ansible-status-bar", markup=True)
        yield Footer()

    def on_mount(self):
        self._build_tree()
        self._update_controls()
        self._update_banner()

    def _update_controls(self):
        arrow = "▼" if self._sort_reverse else "▲"
        label = self.SORT_LABELS[self._sort_index]
        self.query_one("#ansible-sort-label", Static).update(
            f"[bold]Sort:[/bold] [cyan]{label}[/cyan] {arrow}"
        )
        total = len(self._playbooks)
        self.query_one("#ansible-count-label", Static).update(
            f"[dim]{total} playbooks  |  {len(self._inventories)} inventories  |  {len(self._roles)} roles[/dim]"
        )

    def _update_banner(self):
        ans_cfg = self.app.config.ansible
        self.query_one("#ansible-banner", Static).update(
            f"[bold cyan]{len(self._playbooks)}[/bold cyan] playbooks  [dim]|[/dim]  "
            f"[bold magenta]{len(self._inventories)}[/bold magenta] inventories  [dim]|[/dim]  "
            f"[bold yellow]{len(self._roles)}[/bold yellow] roles  [dim]|[/dim]  "
            f"[dim]Dir: {ans_cfg.playbook_dir}[/dim]"
        )

    def _sort_playbooks(self) -> list[dict]:
        field = self.SORT_FIELDS[self._sort_index]
        if field == "name":
            key = lambda p: p["name"].lower()
        elif field == "category":
            key = lambda p: p.get("category", "").lower()
        elif field == "status":
            key = lambda p: p.get("last_status", "never")
        elif field == "hosts":
            key = lambda p: p.get("hosts", "").lower()
        elif field == "tasks":
            key = lambda p: p.get("tasks", 0)
        else:
            key = lambda p: p["name"].lower()
        return sorted(self._playbooks, key=key, reverse=self._sort_reverse)

    def _build_tree(self):
        tree = self.query_one("#ansible-tree", Tree)

        expanded: set[str] = set()
        for node in tree.root.children:
            if node.data and node.data.kind == "section" and node.is_expanded:
                expanded.add(node.data.section)

        tree.clear()

        # ── Playbooks section ──
        pb_label = Text()
        pb_label.append(f"Playbooks  [{len(self._playbooks)}]", style="bold cyan")
        pb_section = tree.root.add(pb_label, data=AnsibleNodeData(kind="section", section="playbooks"))

        sorted_pbs = self._sort_playbooks()
        categories: dict[str, list[dict]] = {}
        for pb in sorted_pbs:
            cat = pb.get("category", "Uncategorized")
            categories.setdefault(cat, []).append(pb)

        for cat_name, cat_pbs in sorted(categories.items()):
            color = CATEGORY_COLORS.get(cat_name, "white")
            cat_label = Text()
            cat_label.append(f"{cat_name}  [{len(cat_pbs)}]", style=f"bold {color}")
            cat_node = pb_section.add(
                cat_label,
                data=AnsibleNodeData(kind="category", section="playbooks", category=cat_name),
            )
            for pb in cat_pbs:
                cat_node.add_leaf(
                    _make_playbook_label(pb),
                    data=AnsibleNodeData(kind="playbook", record=pb, category=cat_name),
                )

        # ── Inventories section ──
        tree.root.add_leaf(Text(""), data=AnsibleNodeData(kind="placeholder"))

        inv_label = Text()
        inv_label.append(f"Inventories  [{len(self._inventories)}]", style="bold magenta")
        inv_section = tree.root.add(inv_label, data=AnsibleNodeData(kind="section", section="inventories"))

        for inv in self._inventories:
            inv_section.add_leaf(
                _make_inventory_label(inv),
                data=AnsibleNodeData(kind="inventory", record=inv),
            )

        # ── Roles section ──
        tree.root.add_leaf(Text(""), data=AnsibleNodeData(kind="placeholder"))

        role_label = Text()
        role_label.append(f"Roles  [{len(self._roles)}]", style="bold yellow")
        role_section = tree.root.add(role_label, data=AnsibleNodeData(kind="section", section="roles"))

        for role in self._roles:
            role_section.add_leaf(
                _make_role_label(role),
                data=AnsibleNodeData(kind="role", record=role),
            )

        # Expand sections
        if not expanded:
            pb_section.expand()
            for child in pb_section.children:
                if child.data and child.data.kind == "category":
                    child.expand()
            inv_section.expand()
            role_section.expand()
        else:
            if "playbooks" in expanded:
                pb_section.expand()
                for child in pb_section.children:
                    if child.data and child.data.kind == "category":
                        child.expand()
            if "inventories" in expanded:
                inv_section.expand()
            if "roles" in expanded:
                role_section.expand()

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, "kind"):
            self._clear_detail_panel()
            return

        if node.data.kind == "playbook":
            self._show_playbook_detail(node.data.record)
        elif node.data.kind == "inventory":
            self._show_inventory_detail(node.data.record)
        elif node.data.kind == "role":
            self._show_role_detail(node.data.record)
        elif node.data.kind == "category":
            self._show_category_detail(node.data.category)
        elif node.data.kind == "section":
            self._show_section_detail(node.data.section)
        else:
            self._clear_detail_panel()

    def _show_playbook_detail(self, pb: dict) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)
        title.update("[bold]Playbook Details[/bold]")

        status = pb.get("last_status", "never")
        status_color = STATUS_COLORS.get(status, "bright_black")
        cat_color = CATEGORY_COLORS.get(pb.get("category", ""), "white")

        tags = ", ".join(pb.get("tags", []))
        last_run = pb.get("last_run") or "Never"

        lines = [
            f"[bold]Name:[/bold]        {pb['name']}",
            f"[bold]Description:[/bold] {pb.get('description', '-')}",
            f"[bold]Category:[/bold]    [{cat_color}]{pb.get('category', '-')}[/{cat_color}]",
            f"[bold]Hosts:[/bold]       [cyan]{pb.get('hosts', 'all')}[/cyan]",
            f"[bold]Tasks:[/bold]       {pb.get('tasks', '?')}",
            f"[bold]Tags:[/bold]        {tags or '-'}",
            "",
            f"[bold]Last run:[/bold]    {last_run}",
            f"[bold]Status:[/bold]      [{status_color}]{status}[/{status_color}]",
            "",
            "[dim italic]Press [x] to run, [e] to edit, [d] to delete.[/dim italic]",
        ]
        detail.update("\n".join(lines))

    def _show_inventory_detail(self, inv: dict) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)
        title.update("[bold]Inventory Details[/bold]")

        groups = ", ".join(inv.get("groups", []))
        lines = [
            f"[bold]Name:[/bold]     {inv['name']}",
            f"[bold]Hosts:[/bold]    [cyan]{inv.get('hosts', 0)}[/cyan]",
            f"[bold]Groups:[/bold]   {groups}",
            "",
            "[dim italic]Inventory management coming in a future update.[/dim italic]",
        ]
        detail.update("\n".join(lines))

    def _show_role_detail(self, role: dict) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)
        title.update("[bold]Role Details[/bold]")

        lines = [
            f"[bold]Name:[/bold]        {role['name']}",
            f"[bold]Description:[/bold] {role.get('description', '-')}",
            f"[bold]Tasks:[/bold]       {role.get('tasks', '?')}",
            "",
            "[dim italic]Role browsing coming in a future update.[/dim italic]",
        ]
        detail.update("\n".join(lines))

    def _show_category_detail(self, category: str) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)
        color = CATEGORY_COLORS.get(category, "white")
        title.update(f"[bold]{category}[/bold]")

        pbs = [p for p in self._playbooks if p.get("category") == category]
        lines = [
            f"[bold]Category:[/bold]   [{color}]{category}[/{color}]",
            f"[bold]Playbooks:[/bold]  [cyan]{len(pbs)}[/cyan]",
        ]
        if pbs:
            lines.append("")
            for pb in pbs:
                s = pb.get("last_status", "never")
                sc = STATUS_COLORS.get(s, "bright_black")
                lines.append(f"  [{sc}]●[/{sc}] {pb['name']}")
        detail.update("\n".join(lines))

    def _show_section_detail(self, section: str) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)

        if section == "playbooks":
            title.update("[bold]Playbooks[/bold]")
            success = sum(1 for p in self._playbooks if p.get("last_status") == "success")
            failed = sum(1 for p in self._playbooks if p.get("last_status") == "failed")
            lines = [
                f"[bold]Total:[/bold]     [cyan]{len(self._playbooks)}[/cyan]",
                f"[bold]Succeeded:[/bold] [green]{success}[/green]",
                f"[bold]Failed:[/bold]    [red]{failed}[/red]",
                "",
                "[dim italic]Press [a] to create a new playbook.[/dim italic]",
            ]
        elif section == "inventories":
            title.update("[bold]Inventories[/bold]")
            total_hosts = sum(i.get("hosts", 0) for i in self._inventories)
            lines = [
                f"[bold]Files:[/bold]       [cyan]{len(self._inventories)}[/cyan]",
                f"[bold]Total hosts:[/bold] [cyan]{total_hosts}[/cyan]",
                "",
                "[dim italic]Inventory management coming in a future update.[/dim italic]",
            ]
        elif section == "roles":
            title.update("[bold]Roles[/bold]")
            total_tasks = sum(r.get("tasks", 0) for r in self._roles)
            lines = [
                f"[bold]Roles:[/bold]       [cyan]{len(self._roles)}[/cyan]",
                f"[bold]Total tasks:[/bold] [cyan]{total_tasks}[/cyan]",
                "",
                "[dim italic]Role browsing coming in a future update.[/dim italic]",
            ]
        else:
            self._clear_detail_panel()
            return

        detail.update("\n".join(lines))

    def _clear_detail_panel(self) -> None:
        self.query_one("#ansible-detail-title", Static).update("[bold]Details[/bold]")
        self.query_one("#ansible-detail-content", Static).update(
            "[dim]Select an item to view details.[/dim]"
        )

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _get_highlighted_playbook(self) -> dict | None:
        tree = self.query_one("#ansible-tree", Tree)
        try:
            node = tree.get_node_at_line(tree.cursor_line)
        except Exception:
            return None
        if node and node.data and node.data.kind == "playbook":
            return node.data.record
        return None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self):
        self.app.pop_screen()

    def action_add_playbook(self):
        self.app.push_screen(PlaybookInputScreen())

    def action_edit_playbook(self):
        pb = self._get_highlighted_playbook()
        if pb:
            self.app.push_screen(PlaybookInputScreen(playbook=pb))
        else:
            self.notify("Highlight a playbook to edit", timeout=2)

    def action_delete_playbook(self):
        pb = self._get_highlighted_playbook()
        if not pb:
            self.notify("Highlight a playbook to delete", timeout=2)
            return

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.notify(f"Deleted {pb['name']} (prototype — not persisted)", timeout=3)

        self.app.push_screen(
            ConfirmScreen(f"Delete playbook [bold]{pb['name']}[/bold]?", _on_confirm)
        )

    def action_run_playbook(self):
        pb = self._get_highlighted_playbook()
        if pb:
            self.app.push_screen(RunPlaybookScreen(pb))
        else:
            self.notify("Highlight a playbook to run", timeout=2)

    def action_cycle_sort(self):
        old = self._sort_index
        self._sort_index = (old + 1) % len(self.SORT_FIELDS)
        if self._sort_index == old:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_reverse = False
        self._build_tree()
        self._update_controls()

    def action_refresh(self):
        self.notify("Refreshed (using sample data)", timeout=2)
        self._build_tree()
        self._update_controls()
        self._update_banner()
