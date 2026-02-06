"""Ansible Management screen for InfraForge.

Discovers playbook YAML files from the configured ``playbook_dir``,
displays them in a tree with metadata parsed from each file, and
lets users run playbooks against dynamically targeted hosts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Tree
from textual.containers import Container, Horizontal
from textual import work

from rich.text import Text

from infraforge.ansible_runner import PlaybookInfo, discover_playbooks


# ---------------------------------------------------------------------------
# Tree node data
# ---------------------------------------------------------------------------

@dataclass
class AnsibleNodeData:
    kind: Literal["playbook", "placeholder"]
    playbook: PlaybookInfo | None = None


# ---------------------------------------------------------------------------
# Label builder
# ---------------------------------------------------------------------------

_PB_NAME_WIDTH = 30
STATUS_COLORS = {"success": "green", "failed": "red", "never": "bright_black"}


def _make_playbook_label(pb: PlaybookInfo) -> Text:
    """Build aligned label for a playbook leaf."""
    name = pb.filename
    if len(name) > _PB_NAME_WIDTH:
        name_col = name[: _PB_NAME_WIDTH - 2] + ".."
    else:
        name_col = name.ljust(_PB_NAME_WIDTH)

    status_color = STATUS_COLORS.get(pb.last_status, "bright_black")
    status_col = f"[{pb.last_status}]".ljust(12)

    tasks_col = f"{pb.task_count} tasks".ljust(10)
    hosts_col = pb.hosts[:16].ljust(16)

    label = Text()
    label.append(name_col, style="bold")
    label.append("  ", style="default")
    label.append(status_col, style=status_color)
    label.append(tasks_col, style="dim")
    label.append(hosts_col, style="cyan")
    return label


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class AnsibleScreen(Screen):
    """Ansible Management screen — discovers and runs playbooks."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("x", "run_playbook", "Run", show=True),
        Binding("enter", "run_playbook", "Run", show=False),
        Binding("l", "view_log", "Log", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    SORT_FIELDS = ["filename", "name", "status", "tasks"]
    SORT_LABELS = ["Filename", "Name", "Status", "Tasks"]

    def __init__(self) -> None:
        super().__init__()
        self._playbooks: list[PlaybookInfo] = []
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
                yield Tree("Playbooks", id="ansible-tree")
                with Container(id="ansible-detail-panel"):
                    yield Static(
                        "[bold]Details[/bold]",
                        id="ansible-detail-title",
                        markup=True,
                    )
                    yield Static(
                        "[dim]Select a playbook to view details.[/dim]",
                        id="ansible-detail-content",
                        markup=True,
                    )
            yield Static("", id="ansible-status-bar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#ansible-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        self._scan_playbooks()

    # ------------------------------------------------------------------
    # Background scanning
    # ------------------------------------------------------------------

    @work(thread=True)
    def _scan_playbooks(self) -> None:
        self.app.call_from_thread(self._set_status, "Scanning playbook directory...")
        playbook_dir = self.app.config.ansible.playbook_dir
        try:
            playbooks = discover_playbooks(playbook_dir)
            self._playbooks = playbooks
            self.app.call_from_thread(self._build_tree)
            self.app.call_from_thread(self._update_banner)
            self.app.call_from_thread(self._update_controls)
            if playbooks:
                self.app.call_from_thread(
                    self._set_status,
                    f"[green]Found {len(playbooks)} playbook(s)[/green] in {playbook_dir}",
                )
            else:
                self.app.call_from_thread(
                    self._set_status,
                    f"[yellow]No playbooks found[/yellow] in {playbook_dir}  "
                    "[dim]— drop .yml files there to get started[/dim]",
                )
        except Exception as exc:
            self.app.call_from_thread(
                self._set_status,
                f"[red]Error scanning playbooks: {exc}[/red]",
            )

    # ------------------------------------------------------------------
    # Tree
    # ------------------------------------------------------------------

    def _build_tree(self) -> None:
        tree = self.query_one("#ansible-tree", Tree)
        tree.clear()

        sorted_pbs = self._sort_playbooks()

        if not sorted_pbs:
            tree.root.add_leaf(
                Text("(no playbooks found)", style="dim italic"),
                data=AnsibleNodeData(kind="placeholder"),
            )
            return

        for pb in sorted_pbs:
            tree.root.add_leaf(
                _make_playbook_label(pb),
                data=AnsibleNodeData(kind="playbook", playbook=pb),
            )

    def _sort_playbooks(self) -> list[PlaybookInfo]:
        field = self.SORT_FIELDS[self._sort_index]
        if field == "filename":
            key = lambda p: p.filename.lower()
        elif field == "name":
            key = lambda p: p.name.lower()
        elif field == "status":
            order = {"success": 0, "failed": 1, "never": 2}
            key = lambda p: order.get(p.last_status, 3)
        elif field == "tasks":
            key = lambda p: p.task_count
        else:
            key = lambda p: p.filename.lower()
        return sorted(self._playbooks, key=key, reverse=self._sort_reverse)

    # ------------------------------------------------------------------
    # Controls / banner
    # ------------------------------------------------------------------

    def _update_controls(self) -> None:
        arrow = "\u25bc" if self._sort_reverse else "\u25b2"
        label = self.SORT_LABELS[self._sort_index]
        self.query_one("#ansible-sort-label", Static).update(
            f"[bold]Sort:[/bold] [cyan]{label}[/cyan] {arrow}"
        )
        total = len(self._playbooks)
        self.query_one("#ansible-count-label", Static).update(
            f"[dim]{total} playbook(s)[/dim]"
        )

    def _update_banner(self) -> None:
        ans_cfg = self.app.config.ansible
        playbook_dir = Path(ans_cfg.playbook_dir).expanduser().resolve()
        self.query_one("#ansible-banner", Static).update(
            f"[bold cyan]{len(self._playbooks)}[/bold cyan] playbooks  "
            f"[dim]|  Dir: {playbook_dir}[/dim]"
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#ansible-status-bar", Static).update(text)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node
        if node.data is None or not hasattr(node.data, "kind"):
            self._clear_detail()
            return
        if node.data.kind == "playbook" and node.data.playbook:
            self._show_playbook_detail(node.data.playbook)
        else:
            self._clear_detail()

    def _show_playbook_detail(self, pb: PlaybookInfo) -> None:
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)
        title.update("[bold]Playbook Details[/bold]")

        status_color = STATUS_COLORS.get(pb.last_status, "bright_black")
        last_run = pb.last_run or "Never"
        roles_text = "Yes" if pb.has_roles else "No"

        lines = [
            f"[bold]File:[/bold]        {pb.filename}",
            f"[bold]Path:[/bold]        {pb.path}",
            f"[bold]Name:[/bold]        {pb.name}",
            f"[bold]Description:[/bold] {pb.description}",
            f"[bold]Hosts:[/bold]       [cyan]{pb.hosts}[/cyan]",
            f"[bold]Tasks:[/bold]       {pb.task_count}",
            f"[bold]Uses Roles:[/bold]  {roles_text}",
            "",
            f"[bold]Last Run:[/bold]    {last_run}",
            f"[bold]Status:[/bold]      [{status_color}]{pb.last_status}[/{status_color}]",
            "",
            "[dim italic]Press x to run  |  l to view log  |  r to refresh[/dim italic]",
        ]
        detail.update("\n".join(lines))

    def _show_log_detail(self, pb: PlaybookInfo) -> None:
        """Show the tail of the most recent log file in the detail panel."""
        detail = self.query_one("#ansible-detail-content", Static)
        title = self.query_one("#ansible-detail-title", Static)

        log_dir = pb.path.parent / "logs"
        if not log_dir.is_dir():
            detail.update("[dim]No logs directory found.[/dim]")
            return

        log_files = sorted(log_dir.glob(f"{pb.path.stem}_*.log"), reverse=True)
        if not log_files:
            detail.update("[dim]No log files found for this playbook.[/dim]")
            return

        latest = log_files[0]
        title.update(f"[bold]Log: {latest.name}[/bold]")

        try:
            content = latest.read_text()
            tail_lines = content.splitlines()[-60:]
            # Escape Rich markup in log content
            escaped = "\n".join(
                line.replace("[", "\\[") for line in tail_lines
            )
            detail.update(f"[dim]{escaped}[/dim]")
        except Exception as e:
            detail.update(f"[red]Error reading log: {e}[/red]")

    def _clear_detail(self) -> None:
        self.query_one("#ansible-detail-title", Static).update("[bold]Details[/bold]")
        self.query_one("#ansible-detail-content", Static).update(
            "[dim]Select a playbook to view details.[/dim]"
        )

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _get_highlighted_playbook(self) -> PlaybookInfo | None:
        tree = self.query_one("#ansible-tree", Tree)
        try:
            node = tree.get_node_at_line(tree.cursor_line)
        except Exception:
            return None
        if node and node.data and node.data.kind == "playbook":
            return node.data.playbook
        return None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_run_playbook(self) -> None:
        pb = self._get_highlighted_playbook()
        if pb:
            from infraforge.screens.ansible_run_modal import AnsibleRunModal
            self.app.push_screen(AnsibleRunModal(pb))
        else:
            self.notify("Select a playbook to run", timeout=2)

    def action_view_log(self) -> None:
        pb = self._get_highlighted_playbook()
        if not pb:
            self.notify("Select a playbook first", timeout=2)
            return
        if pb.last_status == "never":
            self.notify("No logs — this playbook has never been run", timeout=2)
            return
        self._show_log_detail(pb)

    def action_cycle_sort(self) -> None:
        old = self._sort_index
        self._sort_index = (old + 1) % len(self.SORT_FIELDS)
        if self._sort_index == old:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_reverse = False
        self._build_tree()
        self._update_controls()

    def action_refresh(self) -> None:
        self._scan_playbooks()
