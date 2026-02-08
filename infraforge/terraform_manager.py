"""Terraform integration for InfraForge VM provisioning."""

import json
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from infraforge.config import Config
from infraforge.models import NewVMSpec, VMType


class TerraformError(Exception):
    """Terraform operation error."""
    pass


# Known Terraform/Proxmox error patterns → (regex, title, guidance template)
_ERROR_PATTERNS = [
    (
        r"permissions.*not sufficient.*missing:?\s*\[([^\]]+)\]",
        "Proxmox Permission Error",
        "The Terraform provider requires permissions: [{0}]\n"
        "  Fix: InfraForge can auto-create a dedicated API token.\n"
        "  Or add the permissions manually:\n"
        "  Proxmox → Datacenter → Permissions → Add → User Permission",
    ),
    (
        r"already exists",
        "Resource Already Exists",
        "A container/VM with this name or VMID already exists.\n"
        "  Fix: Choose a different name or remove the existing resource.",
    ),
    (
        r"storage '([^']+)' (?:does not exist|not found)",
        "Storage Not Found",
        "Storage pool '{0}' not found on the target node.\n"
        "  Fix: Check available storage in Proxmox → Node → Storage.",
    ),
    (
        r"node '([^']+)'.*(?:not exist|not found)",
        "Node Not Found",
        "Proxmox node '{0}' not found in the cluster.\n"
        "  Fix: Check Proxmox → Datacenter → Nodes.",
    ),
    (
        r"(?:connection refused|timeout|unreachable|no route)",
        "Connection Error",
        "Cannot reach the Proxmox API.\n"
        "  Fix: Verify Proxmox is running and firewall allows :8006.",
    ),
    (
        r"(?:authentication fail|401|invalid credentials|login failed)",
        "Authentication Failed",
        "Proxmox API credentials are invalid or expired.\n"
        "  Fix: Check credentials in ~/.config/infraforge/config.yaml\n"
        "  If using an API token, verify it hasn't been revoked.",
    ),
    (
        r"(?:could not retrieve the complete|failed to query available)",
        "Provider Registry Error",
        "Terraform can't reach the provider registry.\n"
        "  Fix: Check internet connectivity, or re-run to use cached plugins.",
    ),
]

_TF_TOKEN_NAME = "infraforge-terraform"


class TerraformManager:
    """Manages Terraform deployments for VM provisioning."""

    def __init__(self, config: Config):
        self.config = config
        self.workspace = Path(config.terraform.workspace).resolve()
        self.deployments_dir = self.workspace / "deployments"
        self.templates_dir = self.workspace / "templates"
        self.plugin_mirror_dir = self.workspace / "plugins"

    def ensure_dirs(self):
        """Create workspace directories if they don't exist."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.deployments_dir.mkdir(exist_ok=True)
        self.templates_dir.mkdir(exist_ok=True)
        self.plugin_mirror_dir.mkdir(exist_ok=True)
        gitignore = self.workspace / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# Terraform state and provider cache\n"
                ".terraform/\n"
                "*.tfstate\n"
                "*.tfstate.backup\n"
                "*.tfvars\n"
                ".terraform.lock.hcl\n"
                "deployments/\n"
                "plugins/\n"
                ".tf-token.json\n"
            )

    def check_terraform_installed(self) -> tuple[bool, str]:
        """Check if terraform CLI is available."""
        try:
            result = subprocess.run(
                ["terraform", "version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip().split("\n")[0]
                return True, version
            return False, "terraform command failed"
        except FileNotFoundError:
            return False, "terraform not found in PATH"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # Pre-flight validation
    # ------------------------------------------------------------------

    def validate_pre_deploy(
        self, spec: NewVMSpec, log_fn=None,
    ) -> list[tuple[str, bool, str]]:
        """Pre-flight checks using Proxmox API (via proxmoxer).

        Returns list of (check_name, passed, detail).
        If *log_fn* is provided, progress messages are streamed live
        (used during deployment to show auto-download progress etc.).
        """
        checks: list[tuple[str, bool, str]] = []

        def _log(msg: str):
            if log_fn:
                log_fn(msg)

        try:
            from infraforge.proxmox_client import ProxmoxClient
            client = ProxmoxClient(self.config)
            client.connect()
            checks.append((
                "Proxmox API",
                True,
                f"Connected to {self.config.proxmox.host}",
            ))
        except Exception as e:
            checks.append((
                "Proxmox API",
                False,
                f"Cannot connect: {e}\n"
                "  Fix: Check host/port/credentials in "
                "~/.config/infraforge/config.yaml",
            ))
            return checks  # Can't continue without API

        # Target node
        online: list[str] = []
        try:
            nodes_raw = client.api.nodes.get()
            node_names = [n["node"] for n in nodes_raw]
            online = [
                n["node"] for n in nodes_raw
                if n.get("status") == "online"
            ]
            if spec.node in online:
                checks.append(("Target node", True, f"'{spec.node}' online"))
            elif spec.node in node_names:
                checks.append((
                    "Target node", False,
                    f"'{spec.node}' exists but is OFFLINE\n"
                    "  Fix: Power on the node in Proxmox",
                ))
            else:
                checks.append((
                    "Target node", False,
                    f"'{spec.node}' not found — available: "
                    f"{', '.join(node_names)}\n"
                    "  Fix: Select a valid node in the wizard",
                ))
        except Exception as e:
            checks.append(("Target node", False, f"Check failed: {e}"))

        # Template exists on node — use same approach as proxmox_client
        if spec.template_volid and spec.node in online:
            found = False
            try:
                templates = client.get_downloaded_templates(spec.node)
                found = any(t.volid == spec.template_volid for t in templates)
            except Exception:
                pass

            if found:
                checks.append(("Template", True, spec.template_volid))
            else:
                # Auto-download: try to fetch via pveam
                tpl_file = (
                    spec.template_volid.split("/")[-1]
                    if "/" in spec.template_volid
                    else spec.template_volid
                )
                stor_name = spec.template_volid.split(":")[0]
                ok, msg = self._download_template(
                    client, spec.node, stor_name, tpl_file, _log,
                )
                if ok:
                    checks.append((
                        "Template", True,
                        f"Downloaded {tpl_file} to {stor_name}",
                    ))
                else:
                    checks.append((
                        "Template", False,
                        f"'{spec.template_volid}' not found and "
                        f"auto-download failed: {msg}",
                    ))

        # Storage pool exists with enough space
        if spec.node in online:
            try:
                storages = client.api.nodes(spec.node).storage.get()
                stor_names = [s["storage"] for s in storages]
                if spec.storage in stor_names:
                    info = next(
                        s for s in storages if s["storage"] == spec.storage
                    )
                    avail_gb = info.get("avail", 0) / (1024 ** 3)
                    if avail_gb >= spec.disk_gb:
                        checks.append((
                            "Storage", True,
                            f"'{spec.storage}' — {avail_gb:.1f} GB free",
                        ))
                    else:
                        checks.append((
                            "Storage", False,
                            f"'{spec.storage}' only {avail_gb:.1f} GB free, "
                            f"need {spec.disk_gb} GB\n"
                            "  Fix: Free space or choose another pool",
                        ))
                else:
                    checks.append((
                        "Storage", False,
                        f"'{spec.storage}' not on '{spec.node}'\n"
                        f"  Available: {', '.join(stor_names)}",
                    ))
            except Exception as e:
                checks.append(("Storage", False, f"Check failed: {e}"))

        return checks

    @staticmethod
    def _download_template(client, node, storage, template_name, log_fn):
        """Download a CT template via pveam (Proxmox Appliance Manager).

        Returns (success: bool, message: str).
        """
        import time

        log_fn(f"[dim]    Template not cached — downloading "
               f"{template_name}...[/dim]")

        # Find the template in the appliance catalog
        try:
            catalog = client.api.nodes(node).aplinfo.get()
        except Exception as e:
            return False, f"Cannot fetch appliance catalog: {e}"

        match = None
        for entry in catalog:
            if entry.get("template", "") == template_name:
                match = entry
                break

        if not match:
            return False, (
                f"'{template_name}' not in pveam catalog.\n"
                "    Fix: Manually upload the template to Proxmox storage."
            )

        # Start the download task
        try:
            upid = client.api.nodes(node).aplinfo.post(
                storage=storage,
                template=template_name,
            )
        except Exception as e:
            return False, f"Download request failed: {e}"

        # Poll task until completion (max 120s)
        for tick in range(60):
            time.sleep(2)
            try:
                status = client.api.nodes(node).tasks(upid).status.get()
                state = status.get("status", "")
                if state == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status == "OK":
                        log_fn(f"[green]    Downloaded {template_name}"
                               f"[/green]")
                        return True, "OK"
                    return False, f"Download task failed: {exit_status}"
                if tick % 5 == 4:
                    log_fn(f"[dim]    Still downloading... "
                           f"({(tick + 1) * 2}s)[/dim]")
            except Exception:
                pass

        return False, "Download timed out after 120s"

    # ------------------------------------------------------------------
    # API token management
    # ------------------------------------------------------------------

    def ensure_terraform_token(self) -> tuple[Optional[str], Optional[str], str]:
        """Create/retrieve a dedicated API token for Terraform.

        The Telmate provider handles token auth more reliably than
        password auth (avoids spurious permission-check failures).

        Returns (token_id, token_secret, status_message).
        """
        self.ensure_dirs()
        token_file = self.workspace / ".tf-token.json"

        # Re-use stored token if available
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text())
                tid = data["token_id"]
                tsec = data["token_secret"]
                if tid and tsec:
                    return tid, tsec, "Using cached Terraform API token"
            except Exception:
                pass

        # Create a new token via Proxmox API
        try:
            from infraforge.proxmox_client import ProxmoxClient
            client = ProxmoxClient(self.config)
            client.connect()

            user = self.config.proxmox.user

            # Remove stale token if it exists (we lost the secret)
            try:
                existing = client.api.access.users(user).token.get()
                for t in existing:
                    if t.get("tokenid") == _TF_TOKEN_NAME:
                        client.api.access.users(user).token(
                            _TF_TOKEN_NAME
                        ).delete()
                        break
            except Exception:
                pass

            # Create with privsep=0 → inherits user's full permissions
            result = client.api.access.users(user).token(
                _TF_TOKEN_NAME
            ).post(privsep=0, comment="InfraForge Terraform provisioning")

            # Extract secret (only returned once)
            if isinstance(result, dict):
                token_secret = result.get("value", "")
            else:
                token_secret = str(result)

            if not token_secret:
                return None, None, "Token created but no secret returned"

            token_id = f"{user}!{_TF_TOKEN_NAME}"

            # Persist for future deployments
            token_file.write_text(json.dumps({
                "token_id": token_id,
                "token_secret": token_secret,
            }, indent=2))

            return token_id, token_secret, f"Created API token: {token_id}"

        except Exception as e:
            return None, None, f"Token creation failed: {e}"

    # ------------------------------------------------------------------
    # Error parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_terraform_error(output: str) -> tuple[str, str]:
        """Parse terraform output for known errors.

        Returns (title, guidance) or ("", "") if unrecognised.
        """
        lower = output.lower()
        for pattern, title, guidance in _ERROR_PATTERNS:
            m = re.search(pattern, lower)
            if m:
                try:
                    formatted = guidance.format(*m.groups())
                except (IndexError, KeyError):
                    formatted = guidance
                return title, formatted
        return "", ""

    # ------------------------------------------------------------------
    # HCL generation
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_os_type(template_name: str) -> str:
        """Detect Proxmox OS type from template name for correct UI icon."""
        name = template_name.lower()
        for os_type in (
            "ubuntu", "debian", "centos", "fedora", "opensuse",
            "archlinux", "alpine", "gentoo", "nixos",
        ):
            if os_type in name:
                return os_type
        return "unmanaged"

    def generate_provider_block(
        self,
        token_id: str = "",
        token_secret: str = "",
    ) -> str:
        """Generate Terraform provider configuration (bpg/proxmox)."""
        pve = self.config.proxmox
        endpoint = f"https://{pve.host}:{pve.port}/"
        insecure = "true" if not pve.verify_ssl else "false"

        lines = [
            'terraform {',
            '  required_providers {',
            '    proxmox = {',
            '      source  = "bpg/proxmox"',
            '      version = ">= 0.66.0"',
            '    }',
            '  }',
            '}',
            '',
            'provider "proxmox" {',
            f'  endpoint = "{endpoint}"',
        ]

        if token_id and token_secret:
            lines.append(f'  api_token = "{token_id}={token_secret}"')
        elif pve.auth_method == "token":
            tid = f"{pve.user}!{pve.token_name}"
            lines.append(f'  api_token = "{tid}={pve.token_value}"')
        else:
            lines.append(f'  username = "{pve.user}"')
            lines.append(f'  password = "{pve.password}"')

        lines.extend([
            f'  insecure = {insecure}',
            '}',
        ])

        return '\n'.join(lines)

    def generate_lxc_resource(self, spec: NewVMSpec) -> str:
        """Generate proxmox_virtual_environment_container resource block."""
        safe_name = spec.name.replace("-", "_").replace(".", "_")

        lines = [
            f'resource "proxmox_virtual_environment_container" "{safe_name}" {{',
            f'  node_name    = "{spec.node}"',
            f'  unprivileged = {str(spec.unprivileged).lower()}',
            f'  started      = {str(spec.start_after_create).lower()}',
        ]

        if spec.description:
            escaped = spec.description.replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'  description = "{escaped}"')

        os_type = self._detect_os_type(spec.template_volid or spec.template)

        lines.extend([
            '',
            '  operating_system {',
            f'    template_file_id = "{spec.template_volid}"',
            f'    type             = "{os_type}"',
            '  }',
            '',
            '  cpu {',
            f'    cores = {spec.cpu_cores}',
            '  }',
            '',
            '  memory {',
            f'    dedicated = {spec.memory_mb}',
            '  }',
            '',
            '  disk {',
            f'    datastore_id = "{spec.storage}"',
            f'    size         = {spec.disk_gb}',
            '  }',
            '',
            '  network_interface {',
            '    name   = "eth0"',
            f'    bridge = "{spec.network_bridge}"',
        ])

        if spec.vlan_tag:
            lines.append(f'    vlan_id = {spec.vlan_tag}')

        lines.extend([
            '  }',
            '',
            '  initialization {',
            f'    hostname = "{spec.name}"',
            '',
            '    ip_config {',
            '      ipv4 {',
        ])

        if spec.ip_address:
            lines.append(f'        address = "{spec.ip_address}/{spec.subnet_mask}"')
        else:
            lines.append('        address = "dhcp"')

        if spec.gateway:
            lines.append(f'        gateway = "{spec.gateway}"')

        lines.extend([
            '      }',
            '    }',
        ])

        if spec.ssh_keys:
            lines.extend([
                '',
                '    user_account {',
                '      keys = [',
                f'        "{spec.ssh_keys}",',
                '      ]',
                '    }',
            ])

        lines.extend([
            '  }',
            '}',
        ])

        return '\n'.join(lines)

    def generate_qemu_resource(self, spec: NewVMSpec) -> str:
        """Generate proxmox_virtual_environment_vm resource block."""
        safe_name = spec.name.replace("-", "_").replace(".", "_")

        lines = [
            f'resource "proxmox_virtual_environment_vm" "{safe_name}" {{',
            f'  name      = "{spec.name}"',
            f'  node_name = "{spec.node}"',
            f'  started   = {str(spec.start_after_create).lower()}',
        ]

        if spec.description:
            escaped = spec.description.replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'  description = "{escaped}"')

        # Clone from template VM
        if spec.template_vmid:
            lines.extend([
                '',
                '  clone {',
                f'    vm_id = {spec.template_vmid}',
                '    full  = true',
                '  }',
            ])

        lines.extend([
            '',
            '  cpu {',
            f'    cores = {spec.cpu_cores}',
            '  }',
            '',
            '  memory {',
            f'    dedicated = {spec.memory_mb}',
            '  }',
            '',
            '  disk {',
            f'    datastore_id = "{spec.storage}"',
            '    interface    = "scsi0"',
            f'    size         = {spec.disk_gb}',
            '  }',
            '',
            '  network_device {',
            f'    bridge = "{spec.network_bridge}"',
        ])

        if spec.vlan_tag:
            lines.append(f'    vlan_id = {spec.vlan_tag}')

        lines.extend([
            '  }',
            '',
            '  initialization {',
            '    ip_config {',
            '      ipv4 {',
        ])

        if spec.ip_address:
            lines.append(f'        address = "{spec.ip_address}/{spec.subnet_mask}"')
        else:
            lines.append('        address = "dhcp"')

        if spec.gateway:
            lines.append(f'        gateway = "{spec.gateway}"')

        lines.extend([
            '      }',
            '    }',
        ])

        if spec.ssh_keys:
            lines.extend([
                '',
                '    user_account {',
                '      keys = [',
                f'        "{spec.ssh_keys}",',
                '      ]',
                '    }',
            ])

        lines.extend([
            '  }',
            '}',
        ])

        return '\n'.join(lines)

    def get_deployment_tf(
        self,
        spec: NewVMSpec,
        token_id: str = "",
        token_secret: str = "",
    ) -> str:
        """Get the full terraform config as a string (for preview)."""
        provider = self.generate_provider_block(token_id, token_secret)
        if spec.vm_type == VMType.LXC:
            resource = self.generate_lxc_resource(spec)
        else:
            resource = self.generate_qemu_resource(spec)
        return f"{provider}\n\n{resource}\n"

    # ------------------------------------------------------------------
    # Deployment management
    # ------------------------------------------------------------------

    def create_deployment(
        self,
        spec: NewVMSpec,
        token_id: str = "",
        token_secret: str = "",
    ) -> Path:
        """Create a deployment directory with Terraform config files."""
        self.ensure_dirs()
        deploy_dir = self.deployments_dir / spec.name
        deploy_dir.mkdir(parents=True, exist_ok=True)
        tf_content = self.get_deployment_tf(spec, token_id, token_secret)
        (deploy_dir / "main.tf").write_text(tf_content)
        return deploy_dir

    def ensure_provider_mirror(self, deploy_dir: Path) -> tuple[bool, str]:
        """Mirror providers locally so init doesn't depend on registry resolution."""
        marker = self.plugin_mirror_dir / ".mirrored"
        if marker.exists():
            return True, "provider mirror already cached"
        try:
            result = subprocess.run(
                ["terraform", "providers", "mirror", "-no-color",
                 str(self.plugin_mirror_dir)],
                cwd=str(deploy_dir),
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0:
                marker.write_text("ok")
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "provider mirror timed out after 120s"
        except Exception as e:
            return False, str(e)

    def terraform_init(self, deploy_dir: Path) -> tuple[bool, str]:
        """Run terraform init in the deployment directory."""
        try:
            # Use local plugin mirror to avoid registry resolution issues
            cmd = ["terraform", "init", "-no-color"]
            if self.plugin_mirror_dir.exists() and any(self.plugin_mirror_dir.iterdir()):
                cmd.extend(["-plugin-dir", str(self.plugin_mirror_dir)])
            result = subprocess.run(
                cmd,
                cwd=str(deploy_dir),
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "terraform init timed out after 120s"
        except Exception as e:
            return False, str(e)

    def terraform_plan(self, deploy_dir: Path) -> tuple[bool, str]:
        """Run terraform plan in the deployment directory."""
        try:
            result = subprocess.run(
                ["terraform", "plan", "-no-color"],
                cwd=str(deploy_dir),
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "terraform plan timed out after 120s"
        except Exception as e:
            return False, str(e)

    def terraform_apply(self, deploy_dir: Path) -> tuple[bool, str]:
        """Run terraform apply -auto-approve in the deployment directory."""
        try:
            result = subprocess.run(
                ["terraform", "apply", "-auto-approve", "-no-color"],
                cwd=str(deploy_dir),
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "terraform apply timed out after 300s"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # Reusable template management
    # ------------------------------------------------------------------

    def save_template(self, name: str, spec: NewVMSpec) -> Path:
        """Save a NewVMSpec as a reusable template."""
        self.ensure_dirs()
        template_path = self.templates_dir / f"{name}.json"
        data = asdict(spec)
        data["vm_type"] = spec.vm_type.value
        template_path.write_text(json.dumps(data, indent=2))
        return template_path

    def load_template(self, name: str) -> Optional[NewVMSpec]:
        """Load a saved template by name."""
        template_path = self.templates_dir / f"{name}.json"
        if not template_path.exists():
            return None
        try:
            data = json.loads(template_path.read_text())
            vm_type_str = data.pop("vm_type", "lxc")
            valid_fields = set(NewVMSpec.__dataclass_fields__.keys())
            filtered = {k: v for k, v in data.items() if k in valid_fields}
            spec = NewVMSpec(**filtered)
            spec.vm_type = VMType(vm_type_str)
            return spec
        except Exception:
            return None

    def list_templates(self) -> list[dict]:
        """List all saved templates with summary info."""
        self.ensure_dirs()
        templates = []
        for f in sorted(self.templates_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                templates.append({
                    "name": f.stem,
                    "vm_type": data.get("vm_type", "lxc"),
                    "template": data.get("template", ""),
                    "cpu_cores": data.get("cpu_cores", 2),
                    "memory_mb": data.get("memory_mb", 2048),
                    "disk_gb": data.get("disk_gb", 10),
                })
            except Exception:
                pass
        return templates

    def delete_template(self, name: str) -> bool:
        """Delete a saved template."""
        template_path = self.templates_dir / f"{name}.json"
        if template_path.exists():
            template_path.unlink()
            return True
        return False
