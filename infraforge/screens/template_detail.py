"""Template detail screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.containers import Container, VerticalScroll

from infraforge.models import Template, TemplateType


class TemplateDetailScreen(Screen):
    """Detail view for a template."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
    ]

    def __init__(self, template: Template):
        super().__init__()
        self.template = template

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="vm-detail-container"):
            t = self.template

            # Header
            type_label = t.type_label
            yield Static(
                f"  [bold]{t.name}[/bold]  │  {type_label}",
                id="vm-detail-header",
                markup=True,
            )

            if t.template_type == TemplateType.VM:
                yield from self._compose_vm_template()
            elif t.template_type == TemplateType.CONTAINER:
                yield from self._compose_ct_template()
            elif t.template_type == TemplateType.ISO:
                yield from self._compose_iso()
        yield Footer()

    def _compose_vm_template(self) -> ComposeResult:
        t = self.template
        with Container(classes="detail-section"):
            yield Static("VM Template Details", classes="detail-section-title")
            lines = []
            if t.vmid:
                lines.append(f"  [bold]VMID:[/bold]          {t.vmid}")
            lines.append(f"  [bold]Name:[/bold]          {t.name}")
            lines.append(f"  [bold]Node:[/bold]          {t.node}")
            lines.append(f"  [bold]Disk Size:[/bold]     {t.size_display}")
            if t.description and t.description != t.name:
                lines.append(f"  [bold]Description:[/bold]   {t.description}")
            yield Static("\n".join(lines), markup=True)

    def _compose_ct_template(self) -> ComposeResult:
        t = self.template

        # Check if this is a downloaded template or an available appliance
        if t.package or t.headline:
            # Appliance template from pveam
            with Container(classes="detail-section"):
                yield Static("Appliance Template", classes="detail-section-title")
                lines = []
                lines.append(f"  [bold]Package:[/bold]       {t.package or t.name}")
                if t.headline:
                    lines.append(f"  [bold]Summary:[/bold]       {t.headline}")
                if t.os:
                    lines.append(f"  [bold]OS:[/bold]            {t.os}")
                if t.version:
                    lines.append(f"  [bold]Version:[/bold]       {t.version}")
                if t.section:
                    lines.append(f"  [bold]Section:[/bold]       {t.section}")
                if t.architecture:
                    lines.append(f"  [bold]Architecture:[/bold] {t.architecture}")
                if t.source:
                    lines.append(f"  [bold]Source:[/bold]        {t.source}")
                if t.maintainer:
                    lines.append(f"  [bold]Maintainer:[/bold]   {t.maintainer}")
                if t.infopage:
                    lines.append(f"  [bold]Info:[/bold]          {t.infopage}")
                if t.location:
                    lines.append(f"  [bold]Download URL:[/bold] {t.location}")
                yield Static("\n".join(lines), markup=True)

            if t.description:
                with Container(classes="detail-section"):
                    yield Static("Description", classes="detail-section-title")
                    # Wrap long descriptions
                    desc = t.description.replace("\\n", "\n")
                    yield Static(f"  {desc}", markup=False)

            if t.sha512sum:
                with Container(classes="detail-section"):
                    yield Static("Checksum", classes="detail-section-title")
                    yield Static(f"  [dim]SHA-512: {t.sha512sum}[/dim]", markup=True)

        else:
            # Downloaded container template
            with Container(classes="detail-section"):
                yield Static("Container Template", classes="detail-section-title")
                lines = []
                lines.append(f"  [bold]Name:[/bold]          {t.name}")
                if t.volid:
                    lines.append(f"  [bold]Volume ID:[/bold]    {t.volid}")
                lines.append(f"  [bold]Storage:[/bold]       {t.storage}")
                lines.append(f"  [bold]Node:[/bold]          {t.node}")
                lines.append(f"  [bold]Size:[/bold]          {t.size_display}")
                yield Static("\n".join(lines), markup=True)

    def _compose_iso(self) -> ComposeResult:
        t = self.template
        with Container(classes="detail-section"):
            yield Static("ISO Image", classes="detail-section-title")
            lines = []
            lines.append(f"  [bold]Name:[/bold]          {t.name}")
            if t.volid:
                lines.append(f"  [bold]Volume ID:[/bold]    {t.volid}")
            lines.append(f"  [bold]Storage:[/bold]       {t.storage}")
            lines.append(f"  [bold]Node:[/bold]          {t.node}")
            lines.append(f"  [bold]Size:[/bold]          {t.size_display}")
            yield Static("\n".join(lines), markup=True)

    def action_go_back(self):
        self.app.pop_screen()
