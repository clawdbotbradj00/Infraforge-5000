"""New VM creation wizard screen for InfraForge."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Header, Footer, Static, Input, Select, Button,
    RadioButton, RadioSet, DataTable, Label,
)
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual import work, on

from infraforge.models import NewVMSpec, VMType


WIZARD_STEPS = [
    "Basics",
    "Template",
    "Resources",
    "Network",
    "IPAM",
    "DNS",
    "Provision",
    "Review",
]


class NewVMScreen(Screen):
    """Guided wizard for creating a new VM."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("backspace", "prev_step", "Back", show=True, priority=True),
    ]

    def __init__(self):
        super().__init__()
        self._step = 0
        self.spec = NewVMSpec()
        self._nodes: list[str] = []
        self._templates = []
        self._storages = []
        self._subnets = []  # phpIPAM subnets
        self._available_ips = []  # Available IPs from IPAM

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="wizard-container"):
            # Step progress bar
            with Horizontal(id="wizard-progress"):
                for i, step_name in enumerate(WIZARD_STEPS):
                    classes = "wizard-step"
                    if i == 0:
                        classes += " -active"
                    yield Static(
                        f"{i + 1}. {step_name}",
                        classes=classes,
                        id=f"step-indicator-{i}",
                    )

            # Step content area
            with VerticalScroll(id="wizard-content"):
                yield Static("Loading...", id="wizard-step-content", markup=True)

            # Action buttons
            with Horizontal(id="wizard-actions"):
                yield Button("Cancel", variant="error", id="btn-cancel")
                yield Button("Back", id="btn-back")
                yield Button("Next", variant="primary", id="btn-next")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self):
        # Apply defaults from config
        defaults = self.app.config.defaults
        self.spec.cpu_cores = defaults.cpu_cores
        self.spec.memory_mb = defaults.memory_mb
        self.spec.disk_gb = defaults.disk_gb
        self.spec.storage = defaults.storage
        self.spec.network_bridge = defaults.network_bridge
        self.spec.start_after_create = defaults.start_on_create

        if self.app.config.dns.domain:
            self.spec.dns_domain = self.app.config.dns.domain

        self._render_step()
        self._load_initial_data()

    # ------------------------------------------------------------------
    # Background data loaders
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_initial_data(self):
        """Pre-load data needed across multiple wizard steps."""
        try:
            nodes = self.app.proxmox.get_node_info()
            self._nodes = [n.node for n in nodes]

            templates = (
                self.app.proxmox.get_vm_templates()
                + self.app.proxmox.get_downloaded_templates()
            )
            self._templates = templates

            storages = self.app.proxmox.get_storage_info()
            self._storages = storages

            # If we are still on the first step, re-render with loaded data
            if self._step == 0:
                self.app.call_from_thread(self._render_step)
        except Exception:
            pass

    @work(thread=True)
    def _load_ipam_subnets(self):
        """Load subnets from phpIPAM."""
        try:
            from infraforge.ipam_client import IPAMClient  # type: ignore[import-untyped]
            ipam = IPAMClient(self.app.config)
            subnets = ipam.get_subnets()
            self._subnets = subnets
            self.app.call_from_thread(self._render_step)
        except Exception:
            self._subnets = []
            self.app.call_from_thread(self._render_step)

    @work(thread=True)
    def _load_available_ips(self, subnet_id: str):
        """Load available IPs for a given subnet from phpIPAM."""
        try:
            from infraforge.ipam_client import IPAMClient  # type: ignore[import-untyped]
            ipam = IPAMClient(self.app.config)
            ips = ipam.get_available_ips(subnet_id)
            self._available_ips = ips
            self.app.call_from_thread(self._render_step)
        except Exception:
            self._available_ips = []
            self.app.call_from_thread(self._render_step)

    # ------------------------------------------------------------------
    # Step rendering
    # ------------------------------------------------------------------

    def _render_step(self):
        """Render the current wizard step content."""
        content = self.query_one("#wizard-step-content", Static)

        # Update step indicators
        for i in range(len(WIZARD_STEPS)):
            indicator = self.query_one(f"#step-indicator-{i}", Static)
            classes = "wizard-step"
            if i < self._step:
                classes += " -completed"
            elif i == self._step:
                classes += " -active"
            indicator.set_classes(classes)

        # Update navigation buttons
        btn_back = self.query_one("#btn-back", Button)
        btn_next = self.query_one("#btn-next", Button)
        btn_back.disabled = self._step == 0

        if self._step == len(WIZARD_STEPS) - 1:
            btn_next.label = "Create VM"
            btn_next.variant = "success"
        else:
            btn_next.label = "Next"
            btn_next.variant = "primary"

        # Dispatch to per-step renderer
        step_renderers = [
            self._render_basics,
            self._render_template,
            self._render_resources,
            self._render_network,
            self._render_ipam,
            self._render_dns,
            self._render_provision,
            self._render_review,
        ]

        text = step_renderers[self._step]()
        content.update(text)

    # -- Step 1: Basics ----------------------------------------------------

    def _render_basics(self) -> str:
        lines = [
            "[bold cyan]Step 1: Basic Information[/bold cyan]\n",
            "[bold]VM Name:[/bold]",
            f"  Current: [green]{self.spec.name or '(not set)'}[/green]",
            "  [dim]Press 'e' to edit, or type in the field below[/dim]\n",
            "[bold]VM Type:[/bold]",
            f"  Current: [green]{self.spec.vm_type.value.upper()}[/green]",
            "  [dim]Options: QEMU (full VM) or LXC (container)[/dim]\n",
            "[bold]Target Node:[/bold]",
        ]
        if self._nodes:
            lines.append(
                f"  Current: [green]{self.spec.node or self._nodes[0]}[/green]"
            )
            lines.append(f"  Available: {', '.join(self._nodes)}")
        else:
            lines.append("  [yellow]Loading nodes...[/yellow]")

        lines.extend([
            "\n[bold]VMID:[/bold]",
            f"  Current: [green]{self.spec.vmid or 'Auto-assign'}[/green]",
            "  [dim]Leave empty for automatic assignment[/dim]",
            "\n[dim]--- Use the input fields that will appear here ---[/dim]",
            "[dim]This is a preview of the wizard flow. Interactive inputs[/dim]",
            "[dim]will be wired up when Terraform integration is complete.[/dim]",
        ])
        return "\n".join(lines)

    # -- Step 2: Template --------------------------------------------------

    def _render_template(self) -> str:
        lines = [
            "[bold cyan]Step 2: Template Selection[/bold cyan]\n",
            f"[bold]Selected:[/bold] [green]{self.spec.template or '(none)'}[/green]\n",
        ]

        if self._templates:
            lines.append("[bold]Available Templates:[/bold]\n")

            vm_tmpls = [
                t for t in self._templates if t.template_type.value == "vm"
            ]
            ct_tmpls = [
                t for t in self._templates if t.template_type.value == "container"
            ]
            iso_tmpls = [
                t for t in self._templates if t.template_type.value == "iso"
            ]

            if vm_tmpls:
                lines.append("  [bold]VM Templates:[/bold]")
                for t in vm_tmpls:
                    lines.append(
                        f"    * {t.name} (VMID: {t.vmid}, Node: {t.node})"
                    )

            if ct_tmpls:
                lines.append("\n  [bold]Container Templates:[/bold]")
                for t in ct_tmpls:
                    lines.append(
                        f"    * {t.name} ({t.storage}, {t.size_display})"
                    )

            if iso_tmpls:
                lines.append("\n  [bold]ISO Images:[/bold]")
                for t in iso_tmpls:
                    lines.append(
                        f"    * {t.name} ({t.storage}, {t.size_display})"
                    )
        else:
            lines.append("[yellow]Loading templates...[/yellow]")

        return "\n".join(lines)

    # -- Step 3: Resources -------------------------------------------------

    def _render_resources(self) -> str:
        mem_gb = self.spec.memory_mb / 1024
        lines = [
            "[bold cyan]Step 3: Resources[/bold cyan]\n",
            f"[bold]CPU Cores:[/bold]     [green]{self.spec.cpu_cores}[/green]",
            f"[bold]Memory:[/bold]        [green]{self.spec.memory_mb} MB[/green] ({mem_gb:.1f} GB)",
            f"[bold]Disk Size:[/bold]     [green]{self.spec.disk_gb} GB[/green]",
            f"[bold]Storage Pool:[/bold]  [green]{self.spec.storage}[/green]",
        ]

        if self._storages:
            lines.append("\n[bold]Available Storage Pools:[/bold]")
            for s in self._storages:
                if s.total > 0:
                    lines.append(
                        f"    * {s.storage} ({s.storage_type}) -- "
                        f"{s.avail_display} available of {s.total_display}"
                    )
                else:
                    lines.append(f"    * {s.storage} ({s.storage_type})")

        start_label = "Yes" if self.spec.start_after_create else "No"
        lines.append(
            f"\n[bold]Start on Create:[/bold] [green]{start_label}[/green]"
        )

        return "\n".join(lines)

    # -- Step 4: Network ---------------------------------------------------

    def _render_network(self) -> str:
        ip_display = self.spec.ip_address or "Not set (DHCP)"
        gw_display = self.spec.gateway or "Not set"
        lines = [
            "[bold cyan]Step 4: Network Configuration[/bold cyan]\n",
            f"[bold]Network Bridge:[/bold]  [green]{self.spec.network_bridge}[/green]",
            "  [dim]Common: vmbr0 (default), vmbr1, etc.[/dim]\n",
            "[bold]VLAN Tag:[/bold]        [green](none)[/green]",
            "  [dim]Optional -- leave empty for untagged[/dim]\n",
            "[bold]IP Assignment:[/bold]",
            "  [dim]* DHCP (automatic)[/dim]",
            "  [dim]* Static (manual entry)[/dim]",
            "  [dim]* IPAM (phpIPAM -- configure in next step)[/dim]\n",
            f"[bold]IP Address:[/bold]     [green]{ip_display}[/green]",
            f"[bold]Gateway:[/bold]        [green]{gw_display}[/green]",
        ]
        return "\n".join(lines)

    # -- Step 5: IPAM ------------------------------------------------------

    def _render_ipam(self) -> str:
        ipam_cfg = self.app.config.ipam
        lines = [
            "[bold cyan]Step 5: IP Address Management (phpIPAM)[/bold cyan]\n",
        ]

        if not ipam_cfg.url:
            lines.extend([
                "[yellow]phpIPAM is not configured.[/yellow]",
                "[dim]To enable IPAM integration, add the following to your config:[/dim]\n",
                "[dim]ipam:[/dim]",
                "[dim]  provider: phpipam[/dim]",
                "[dim]  url: https://ipam.example.com[/dim]",
                "[dim]  app_id: infraforge[/dim]",
                "[dim]  token: your-api-token[/dim]",
                "[dim]  verify_ssl: false[/dim]\n",
                "[dim]You can still enter an IP address manually in the Network step.[/dim]",
            ])
        else:
            lines.extend([
                f"[bold]IPAM Provider:[/bold]  [green]{ipam_cfg.provider}[/green]",
                f"[bold]Server:[/bold]         [green]{ipam_cfg.url}[/green]\n",
            ])

            if self._subnets:
                lines.append("[bold]Available Subnets:[/bold]\n")
                for subnet in self._subnets:
                    desc = subnet.get("description", "")
                    cidr = (
                        subnet.get("subnet", "")
                        + "/"
                        + str(subnet.get("mask", ""))
                    )
                    usage = subnet.get("usage", {})
                    used = usage.get("used", "?")
                    total = usage.get("maxhosts", "?")
                    free_pct = usage.get("freehosts_percent", "?")

                    desc_str = f" -- {desc}" if desc else ""
                    lines.append(
                        f"    * [bold]{cidr}[/bold]{desc_str}"
                        f"  [{used}/{total} used, {free_pct}% free]"
                    )

                if self._available_ips:
                    lines.append("\n[bold]Available IP Addresses:[/bold] (first 20)")
                    for ip in self._available_ips[:20]:
                        lines.append(f"    * {ip}")
                else:
                    lines.append("\n[dim]Select a subnet to see available IPs[/dim]")
            else:
                lines.append("[yellow]Loading subnets from phpIPAM...[/yellow]")
                # Trigger loading in the background
                self._load_ipam_subnets()

        selected_ip = self.spec.ip_address or "(not selected)"
        lines.append(
            f"\n[bold]Selected IP:[/bold]  [green]{selected_ip}[/green]"
        )

        return "\n".join(lines)

    # -- Step 6: DNS -------------------------------------------------------

    def _render_dns(self) -> str:
        dns_cfg = self.app.config.dns
        lines = [
            "[bold cyan]Step 6: DNS Configuration[/bold cyan]\n",
        ]

        if not dns_cfg.provider:
            lines.extend([
                "[yellow]DNS provider is not configured.[/yellow]",
                "[dim]To enable DNS management, configure the dns section "
                "in your config.[/dim]\n",
            ])
        else:
            lines.append(
                f"[bold]DNS Provider:[/bold]  [green]{dns_cfg.provider}[/green]"
            )
            if dns_cfg.server:
                lines.append(
                    f"[bold]DNS Server:[/bold]   [green]{dns_cfg.server}:{dns_cfg.port}[/green]"
                )
            lines.append(
                f"[bold]Domain:[/bold]        [green]{dns_cfg.domain}[/green]\n"
            )

        hostname_display = self.spec.dns_name or "(not set)"
        domain_display = self.spec.dns_domain or "(not set)"
        lines.extend([
            f"[bold]Hostname:[/bold]      [green]{hostname_display}[/green]",
            f"[bold]Domain:[/bold]        [green]{domain_display}[/green]",
        ])

        if self.spec.dns_name and self.spec.dns_domain:
            fqdn = f"{self.spec.dns_name}.{self.spec.dns_domain}"
            lines.append(f"[bold]FQDN:[/bold]          [green]{fqdn}[/green]")

            # Show DNS record check result if available
            if hasattr(self, "_dns_check_result"):
                result = self._dns_check_result
                if result is None:
                    lines.append("\n[dim]Checking DNS records...[/dim]")
                elif result:
                    lines.append(
                        f"\n[yellow]Record exists:[/yellow] {fqdn} -> "
                        f"[yellow]{', '.join(result)}[/yellow]"
                    )
                    lines.append(
                        "[dim]The existing record will be updated with the new IP.[/dim]"
                    )
                else:
                    lines.append(
                        f"\n[green]No existing record for {fqdn}[/green] — "
                        "will be created."
                    )

            # Trigger a DNS check in background if BIND9 is configured
            if (
                dns_cfg.provider == "bind9"
                and dns_cfg.server
                and not hasattr(self, "_dns_check_triggered")
            ):
                self._dns_check_triggered = True
                self._dns_check_result = None
                self._check_dns_record()

        lines.extend([
            "\n[bold]Record Type:[/bold]   [green]A[/green]  [dim](A, CNAME)[/dim]",
            "[bold]TTL:[/bold]            [green]3600[/green]  [dim](seconds)[/dim]",
            "\n[dim]DNS records will be created automatically after VM "
            "provisioning.[/dim]",
        ])

        return "\n".join(lines)

    @work(thread=True)
    def _check_dns_record(self):
        """Check if a DNS record already exists for the chosen hostname."""
        try:
            from infraforge.dns_client import DNSClient
            client = DNSClient(self.app.config)
            existing = client.lookup_record(self.spec.dns_name, "A")
            self._dns_check_result = existing
            self.app.call_from_thread(self._render_step)
        except Exception:
            self._dns_check_result = []
            self.app.call_from_thread(self._render_step)

    # -- Step 7: Provisioning ----------------------------------------------

    def _render_provision(self) -> str:
        tf_cfg = self.app.config.terraform
        ans_cfg = self.app.config.ansible
        playbook_display = self.spec.ansible_playbook or "(none -- skip)"
        ssh_display = self.spec.ssh_keys or "(none)"
        tags_display = self.spec.tags or "(none)"
        desc_display = self.spec.description or "(none)"

        lines = [
            "[bold cyan]Step 7: Provisioning[/bold cyan]\n",
            "[bold]Infrastructure:[/bold]  Terraform",
            f"  [dim]Workspace: {tf_cfg.workspace}[/dim]",
            f"  [dim]Backend: {tf_cfg.state_backend}[/dim]\n",
            "[bold]Configuration Management:[/bold]  Ansible",
            f"  [dim]Playbook dir: {ans_cfg.playbook_dir}[/dim]\n",
            f"[bold]Ansible Playbook:[/bold]  [green]{playbook_display}[/green]",
            "  [dim]Select a playbook to run after VM creation[/dim]\n",
            f"[bold]SSH Keys:[/bold]  [green]{ssh_display}[/green]",
            "  [dim]Public SSH key(s) to inject via cloud-init[/dim]\n",
            "[bold]Cloud-Init:[/bold]",
            "  [dim]Cloud-init will be configured based on template type[/dim]",
            "  [dim]Custom cloud-init configs can be added later[/dim]\n",
            f"[bold]Tags:[/bold]  [green]{tags_display}[/green]",
            f"[bold]Description:[/bold]  [green]{desc_display}[/green]",
        ]
        return "\n".join(lines)

    # -- Step 8: Review & Confirm ------------------------------------------

    def _render_review(self) -> str:
        node_display = self.spec.node or (
            self._nodes[0] if self._nodes else "[red](required)[/red]"
        )
        name_display = self.spec.name or "[red](required)[/red]"
        vmid_display = str(self.spec.vmid) if self.spec.vmid else "Auto-assign"
        template_display = self.spec.template or "(none)"
        mem_gb = self.spec.memory_mb / 1024
        ip_display = self.spec.ip_address or "DHCP"
        gw_display = self.spec.gateway or "Auto"
        hostname_display = self.spec.dns_name or "(none)"
        domain_display = self.spec.dns_domain or "(none)"
        playbook_display = self.spec.ansible_playbook or "(skip)"
        ssh_display = "Yes" if self.spec.ssh_keys else "No"
        start_display = "Yes" if self.spec.start_after_create else "No"
        tags_display = self.spec.tags or "(none)"

        lines = [
            "[bold cyan]Step 8: Review & Confirm[/bold cyan]\n",
            "[bold]=== VM Specification ===[/bold]\n",
            f"  [bold]Name:[/bold]           {name_display}",
            f"  [bold]VMID:[/bold]           {vmid_display}",
            f"  [bold]Type:[/bold]           {self.spec.vm_type.value.upper()}",
            f"  [bold]Node:[/bold]           {node_display}",
            f"  [bold]Template:[/bold]       {template_display}",
            "",
            "[bold]--- Resources ---[/bold]",
            f"  [bold]CPU:[/bold]            {self.spec.cpu_cores} cores",
            f"  [bold]Memory:[/bold]         {self.spec.memory_mb} MB ({mem_gb:.1f} GB)",
            f"  [bold]Disk:[/bold]           {self.spec.disk_gb} GB",
            f"  [bold]Storage:[/bold]        {self.spec.storage}",
            "",
            "[bold]--- Network ---[/bold]",
            f"  [bold]Bridge:[/bold]         {self.spec.network_bridge}",
            f"  [bold]IP Address:[/bold]     {ip_display}",
            f"  [bold]Gateway:[/bold]        {gw_display}",
            "",
            "[bold]--- DNS ---[/bold]",
            f"  [bold]Hostname:[/bold]       {hostname_display}",
            f"  [bold]Domain:[/bold]         {domain_display}",
        ]

        if self.spec.dns_name and self.spec.dns_domain:
            fqdn = f"{self.spec.dns_name}.{self.spec.dns_domain}"
            lines.append(f"  [bold]FQDN:[/bold]           {fqdn}")

        lines.extend([
            "",
            "[bold]--- Provisioning ---[/bold]",
            f"  [bold]Playbook:[/bold]       {playbook_display}",
            f"  [bold]SSH Keys:[/bold]       {ssh_display}",
            f"  [bold]Auto-start:[/bold]     {start_display}",
            f"  [bold]Tags:[/bold]           {tags_display}",
            "",
            "[bold yellow]VM creation is not yet implemented.[/bold yellow]",
            "[dim]This wizard will use Terraform to provision the VM and[/dim]",
            "[dim]Ansible for post-creation configuration in a future update.[/dim]",
            "",
            "[dim]Press 'Create VM' to see what would be executed.[/dim]",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Button / navigation handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-cancel":
            self.action_cancel()
        elif event.button.id == "btn-back":
            self.action_prev_step()
        elif event.button.id == "btn-next":
            self._next_step()

    def _next_step(self):
        if self._step < len(WIZARD_STEPS) - 1:
            self._step += 1
            self._render_step()
        else:
            # Final step -- trigger the creation preview
            self._execute_create()

    def _execute_create(self):
        """Placeholder for VM creation execution -- shows an execution plan preview."""
        content = self.query_one("#wizard-step-content", Static)

        node = self.spec.node or (self._nodes[0] if self._nodes else "node1")
        vm_name = self.spec.name or "new-vm"
        template_name = self.spec.template or "base-template"
        ip_config = self.spec.ip_address or "dhcp"
        gw_config = self.spec.gateway or "auto"
        playbook_display = self.spec.ansible_playbook or "(none)"
        ip_target = self.spec.ip_address or "(pending)"
        dns_name = self.spec.dns_name or "(none)"
        dns_domain = self.spec.dns_domain or ""

        plan = (
            "[bold green]=== Execution Plan (Preview) ===[/bold green]\n"
            "\n"
            "[bold]Terraform would execute:[/bold]\n"
            "\n"
            f'  resource "proxmox_vm_qemu" "{vm_name}" {{\n'
            f'    name        = "{vm_name}"\n'
            f'    target_node = "{node}"\n'
            f'    clone       = "{template_name}"\n'
            f"    cores       = {self.spec.cpu_cores}\n"
            f"    memory      = {self.spec.memory_mb}\n"
            "\n"
            "    disk {\n"
            f'      size    = "{self.spec.disk_gb}G"\n'
            f'      storage = "{self.spec.storage}"\n'
            "    }\n"
            "\n"
            "    network {\n"
            f'      bridge = "{self.spec.network_bridge}"\n'
            "    }\n"
            "\n"
            f'    ipconfig0 = "ip={ip_config}/24,gw={gw_config}"\n'
            "  }\n"
            "\n"
            "[bold]Ansible would run:[/bold]\n"
            f"  Playbook: {playbook_display}\n"
            f"  Target: {ip_target}\n"
            "\n"
            "[bold]DNS would create:[/bold]\n"
            f"  {dns_name}.{dns_domain} -> {ip_target}\n"
            "\n"
            "[bold yellow]This is a preview only. "
            "Actual execution coming in a future release.[/bold yellow]\n"
            "\n"
            "[dim]Press Escape or Back to return to the wizard.[/dim]"
        )

        content.update(plan)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self):
        self.app.pop_screen()

    def action_prev_step(self):
        if self._step > 0:
            self._step -= 1
            self._render_step()
        else:
            self.app.pop_screen()
