"""Interactive setup wizard for InfraForge (in-app version)."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
import yaml


DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
CONFIG_DIR = Path.home() / ".config" / "infraforge"


def _load_existing_config() -> dict:
    """Load existing config.yaml as a raw dict, or return empty dict."""
    config_path = CONFIG_DIR / "config.yaml"
    if not config_path.exists():
        # Also check fallback paths
        for alt in [CONFIG_DIR / "config.yml", Path("config") / "config.yaml", Path("config") / "config.yml"]:
            if alt.exists():
                config_path = alt
                break
        else:
            return {}

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _detect_missing(existing: dict) -> list[str]:
    """Return a list of section names that are not yet configured."""
    missing = []
    pve = existing.get("proxmox", {})
    if not pve.get("host"):
        missing.append("proxmox")
    dns = existing.get("dns", {})
    if not dns.get("provider"):
        missing.append("dns")
    ipam = existing.get("ipam", {})
    if not ipam.get("url"):
        missing.append("ipam")
    return missing


def run_setup_wizard():
    """Run the interactive setup wizard."""
    console = Console()

    console.print()
    console.print(Panel.fit(
        "[bold cyan]InfraForge Setup Wizard[/bold cyan]\n"
        "[dim]Configure your Proxmox connection, phpIPAM, and preferences[/dim]",
        border_style="cyan",
    ))
    console.print()

    # Load existing config so we can pre-populate defaults
    existing = _load_existing_config()

    # ── Setup mode selection ──────────────────────────────────────────
    missing_only = False
    if existing:
        missing = _detect_missing(existing)
        configured = []
        if "proxmox" not in missing:
            configured.append(f"Proxmox ([green]{existing['proxmox'].get('host', '?')}[/green])")
        if "dns" not in missing:
            configured.append(f"DNS ([green]{existing['dns'].get('provider', '?')}[/green])")
        if "ipam" not in missing:
            configured.append(f"IPAM ([green]{existing['ipam'].get('url', '?')}[/green])")

        if configured:
            console.print("[bold]Already configured:[/bold]")
            for c in configured:
                console.print(f"  [green]✓[/green] {c}", markup=True)
        if missing:
            console.print(f"[bold]Not configured:[/bold] {', '.join(missing)}")
        console.print()

        console.print("[bold]Setup mode:[/bold]")
        console.print("  1) Configure only missing settings [dim](recommended)[/dim]")
        console.print("  2) Reconfigure all settings")
        mode_choice = Prompt.ask("Select", choices=["1", "2"], default="1")
        missing_only = mode_choice == "1"
        console.print()

    config: dict = {}

    # ── Proxmox ──────────────────────────────────────────────────────
    if missing_only and "proxmox" not in missing:
        console.print("[dim]Proxmox: already configured — skipping.[/dim]")
        config["proxmox"] = existing.get("proxmox", {})
    else:
        config["proxmox"] = _configure_proxmox(console, existing.get("proxmox", {}))

    # ── DNS ───────────────────────────────────────────────────────────
    if missing_only and "dns" not in missing:
        console.print("[dim]DNS: already configured — skipping.[/dim]")
        config["dns"] = existing.get("dns", {})
    else:
        # If we're in missing-only mode and DNS is missing, skip the
        # "do you want to configure?" prompt — user already said yes.
        skip_confirm = missing_only and "dns" in missing
        config["dns"] = _configure_dns(console, existing.get("dns", {}), skip_confirm=skip_confirm)

    # ── phpIPAM ───────────────────────────────────────────────────────
    if missing_only and "ipam" not in missing:
        console.print("[dim]IPAM: already configured — skipping.[/dim]")
        config["ipam"] = existing.get("ipam", {})
    else:
        skip_confirm = missing_only and "ipam" in missing
        config["ipam"] = _configure_ipam(console, existing.get("ipam", {}), skip_confirm=skip_confirm)

    # ── Defaults ──────────────────────────────────────────────────────
    ex_tf = existing.get("terraform", {})
    ex_ans = existing.get("ansible", {})
    ex_def = existing.get("defaults", {})

    config["terraform"] = {
        "workspace": ex_tf.get("workspace", "./terraform"),
        "state_backend": ex_tf.get("state_backend", "local"),
    }
    config["ansible"] = {
        "playbook_dir": ex_ans.get("playbook_dir", "./ansible/playbooks"),
        "inventory_dir": ex_ans.get("inventory_dir", "./ansible/inventory"),
    }
    config["defaults"] = {
        "cpu_cores": ex_def.get("cpu_cores", 2),
        "memory_mb": ex_def.get("memory_mb", 2048),
        "disk_gb": ex_def.get("disk_gb", 20),
        "storage": ex_def.get("storage", "local-lvm"),
        "network_bridge": ex_def.get("network_bridge", "vmbr0"),
        "os_type": ex_def.get("os_type", "l26"),
        "start_on_create": ex_def.get("start_on_create", True),
    }

    # ── Write config ──────────────────────────────────────────────────
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CONFIG_DIR / "config.yaml"

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    config_path.chmod(0o600)

    console.print(f"\n[green]✓[/green] Configuration saved to [bold]{config_path}[/bold]")

    # ── Test Proxmox connection ───────────────────────────────────────
    if not (missing_only and "proxmox" not in missing):
        if Confirm.ask("\nTest Proxmox connection?", default=True):
            _test_proxmox_connection(console, config_path)

    # ── Configure subnets if phpIPAM is configured ────────────────────
    if config["ipam"].get("url"):
        _configure_subnets(console, config_path)

    console.print("\n[bold green]Setup complete![/bold green] Run [bold]infraforge[/bold] to start.\n")


# =====================================================================
# Proxmox Configuration
# =====================================================================

def _configure_proxmox(console: Console, prev: dict | None = None) -> dict:
    prev = prev or {}
    console.print("[bold cyan]─── Proxmox Connection ───[/bold cyan]\n")

    pve_host = Prompt.ask("Proxmox host (IP or hostname)", default=prev.get("host") or None)
    pve_port = Prompt.ask("API port", default=str(prev.get("port", 8006)))
    pve_user = Prompt.ask("User", default=prev.get("user", "root@pam"))

    prev_auth = prev.get("auth_method", "token")
    default_auth = "1" if prev_auth == "token" else "2"
    console.print("\n[bold]Authentication method:[/bold]")
    console.print("  1) API Token [dim](recommended)[/dim]")
    console.print("  2) Password")
    auth_choice = Prompt.ask("Select", choices=["1", "2"], default=default_auth)

    if auth_choice == "1":
        auth_method = "token"
        console.print(
            "\n[dim]Create an API token in Proxmox: "
            "Datacenter > Permissions > API Tokens[/dim]"
        )
        token_name = Prompt.ask("Token name", default=prev.get("token_name", "infraforge"))
        prev_token = prev.get("token_value", "")
        if prev_token:
            masked = prev_token[:4] + "..." + prev_token[-4:] if len(prev_token) > 8 else "****"
            console.print(f"  [dim]Current token: {masked}[/dim]")
            if Confirm.ask("  Keep existing token value?", default=True):
                token_value = prev_token
            else:
                token_value = Prompt.ask("Token value (secret)")
        else:
            token_value = Prompt.ask("Token value (secret)")
        password = ""
    else:
        auth_method = "password"
        prev_pw = prev.get("password", "")
        if prev_pw:
            console.print("  [dim]Password is already set.[/dim]")
            if Confirm.ask("  Keep existing password?", default=True):
                password = prev_pw
            else:
                password = Prompt.ask("Password", password=True)
        else:
            password = Prompt.ask("Password", password=True)
        token_name = ""
        token_value = ""

    verify_ssl = Confirm.ask("Verify SSL certificate?", default=prev.get("verify_ssl", False))

    return {
        "host": pve_host,
        "port": int(pve_port),
        "user": pve_user,
        "auth_method": auth_method,
        "token_name": token_name,
        "token_value": token_value,
        "password": password,
        "verify_ssl": verify_ssl,
    }


# =====================================================================
# DNS Configuration
# =====================================================================

def _configure_dns(console: Console, prev: dict | None = None, skip_confirm: bool = False) -> dict:
    prev = prev or {}
    console.print("\n[bold cyan]─── DNS Configuration ───[/bold cyan]\n")

    has_existing = bool(prev.get("provider"))
    if has_existing:
        console.print(f"  [dim]Current provider: {prev['provider']}[/dim]")

    if not skip_confirm and not Confirm.ask("Configure DNS provider?", default=has_existing or skip_confirm):
        return {
            "provider": "", "server": "", "port": 53, "zones": [], "domain": "",
            "tsig_key_name": "", "tsig_key_secret": "", "tsig_algorithm": "hmac-sha256",
            "api_key": "",
        }

    # Map existing provider to default choice
    provider_defaults = {"bind9": "1", "cloudflare": "2", "route53": "3", "custom": "4"}
    default_choice = provider_defaults.get(prev.get("provider", ""), "1")

    console.print("  1) BIND9 [dim](recommended for self-hosted)[/dim]")
    console.print("  2) Cloudflare")
    console.print("  3) Route53")
    console.print("  4) Other")
    dns_choice = Prompt.ask("Select", choices=["1", "2", "3", "4"], default=default_choice)

    if dns_choice == "1":
        console.print(
            "\n[dim]BIND9 uses TSIG keys for authenticated dynamic updates (RFC 2136).\n"
            "Generate a key with: tsig-keygen infraforge-key[/dim]\n"
        )
        dns_server = Prompt.ask("BIND9 server IP/hostname", default=prev.get("server") or None)
        dns_port = Prompt.ask("DNS port", default=str(prev.get("port", 53)))
        tsig_name = Prompt.ask("TSIG key name", default=prev.get("tsig_key_name", "infraforge-key"))

        prev_secret = prev.get("tsig_key_secret", "")
        if prev_secret:
            masked = prev_secret[:4] + "..." + prev_secret[-4:] if len(prev_secret) > 8 else "****"
            console.print(f"  [dim]Current TSIG secret: {masked}[/dim]")
            if Confirm.ask("  Keep existing TSIG secret?", default=True):
                tsig_secret = prev_secret
            else:
                tsig_secret = Prompt.ask("TSIG key secret (base64)", password=True)
        else:
            tsig_secret = Prompt.ask("TSIG key secret (base64)", password=True)

        tsig_algo = Prompt.ask("TSIG algorithm", default=prev.get("tsig_algorithm", "hmac-sha256"))

        # Collect DNS zones (multi-zone support)
        console.print("\n[bold]Add DNS zones to manage?[/bold] [dim](you can also add zones later in the TUI)[/dim]")
        # Seed with previous zones (handle both old "zone" and new "zones" keys)
        prev_zones = prev.get("zones", [])
        if not prev_zones and prev.get("zone"):
            prev_zones = [prev["zone"]]
        dns_zones: list[str] = []
        for pz in prev_zones:
            console.print(f"  [dim]Previous zone: {pz}[/dim]")

        while True:
            default_hint = prev_zones[len(dns_zones)] if len(dns_zones) < len(prev_zones) else None
            zone_input = Prompt.ask(
                "Zone name (blank to finish)",
                default=default_hint or "",
            )
            if not zone_input:
                break
            if zone_input in dns_zones:
                console.print(f"  [yellow]Zone '{zone_input}' already added.[/yellow]")
                continue
            dns_zones.append(zone_input)
            console.print(f"  [green]+[/green] Added zone: {zone_input}")

        # Domain defaults to first zone if not set
        prev_domain = prev.get("domain", "")
        default_domain = prev_domain or (dns_zones[0] if dns_zones else "")
        dns_domain = Prompt.ask("Domain for FQDNs", default=default_domain or None)

        result = {
            "provider": "bind9",
            "server": dns_server,
            "port": int(dns_port),
            "zones": dns_zones,
            "domain": dns_domain,
            "tsig_key_name": tsig_name,
            "tsig_key_secret": tsig_secret,
            "tsig_algorithm": tsig_algo,
            "api_key": "",
        }

        # If no zones were added, try to auto-discover from the domain
        if not dns_zones and dns_domain:
            console.print(f"\n[dim]No zones added — testing if [bold]{dns_domain}[/bold] is a valid zone...[/dim]")
            try:
                from infraforge.dns_client import DNSClient
                client = DNSClient(dns_server, int(dns_port), tsig_name, tsig_secret, tsig_algo)
                soa = client.check_zone(dns_domain)
                if soa:
                    console.print(f"  [green]✓[/green] Found zone: {dns_domain}  (serial: {soa.get('serial', '?')})")
                    if Confirm.ask(f"  Add [bold]{dns_domain}[/bold] as a managed zone?", default=True):
                        dns_zones.append(dns_domain)
                        result["zones"] = dns_zones
                else:
                    console.print(f"  [yellow]Domain {dns_domain} is not a zone on this server.[/yellow]")
                    console.print("  [dim]You can add zones later in the DNS Management screen (press z).[/dim]")
            except Exception:
                console.print("  [dim]Could not test — you can add zones later in the DNS Management screen.[/dim]")

        # Test DNS connectivity
        if Confirm.ask("\nTest DNS connection?", default=True):
            _test_dns_connection(console, result)

        return result

    provider_map = {"2": "cloudflare", "3": "route53", "4": "custom"}
    dns_provider = provider_map[dns_choice]

    prev_api_key = prev.get("api_key", "")
    if prev_api_key:
        masked = prev_api_key[:4] + "..." if len(prev_api_key) > 4 else "****"
        console.print(f"  [dim]Current API key: {masked}[/dim]")
        if Confirm.ask("  Keep existing API key?", default=True):
            dns_api_key = prev_api_key
        else:
            dns_api_key = Prompt.ask("API Key", password=True, default="")
    else:
        dns_api_key = Prompt.ask("API Key", password=True, default="")

    dns_zone = Prompt.ask("DNS Zone / Domain", default=prev.get("domain", "") or (prev.get("zones", [None])[0] if prev.get("zones") else prev.get("zone", "")))

    return {
        "provider": dns_provider,
        "server": "",
        "port": 53,
        "zones": [dns_zone] if dns_zone else [],
        "domain": dns_zone,
        "tsig_key_name": "",
        "tsig_key_secret": "",
        "tsig_algorithm": "hmac-sha256",
        "api_key": dns_api_key,
    }


def _test_dns_connection(console: Console, dns_config: dict) -> None:
    """Test BIND9 DNS connectivity."""
    console.print("[dim]Connecting to DNS server...[/dim]")
    try:
        from infraforge.dns_client import DNSClient, DNSError

        client = DNSClient(
            dns_config["server"],
            dns_config.get("port", 53),
            dns_config.get("tsig_key_name", ""),
            dns_config.get("tsig_key_secret", ""),
            dns_config.get("tsig_algorithm", "hmac-sha256"),
        )
        if client.check_health():
            console.print(f"[green]✓[/green] Connected to DNS server at {dns_config['server']}")
            # Test SOA for each configured zone
            zones = dns_config.get("zones", [])
            for zone in zones:
                try:
                    soa = client.get_zone_soa(zone)
                    if soa:
                        console.print(
                            f"  Zone: {soa.get('zone', zone)}  "
                            f"Serial: {soa.get('serial', '?')}  "
                            f"Primary: {soa.get('mname', '?')}"
                        )
                    else:
                        console.print(f"  [yellow]Zone {zone}: no SOA record found[/yellow]")
                except DNSError:
                    console.print(f"  [yellow]Zone {zone}: SOA query failed[/yellow]")
        else:
            console.print(f"[red]✗[/red] Cannot reach DNS server at {dns_config['server']}")
            console.print("[yellow]Check the server IP and ensure port 53 is accessible.[/yellow]")
    except ImportError:
        console.print("[yellow]dnspython not installed — skipping DNS test.[/yellow]")
        console.print("[dim]Install with: pip install dnspython[/dim]")
    except Exception as e:
        console.print(f"[red]✗[/red] DNS test failed: {e}")


# =====================================================================
# phpIPAM Configuration — Docker deployment
# =====================================================================

def _configure_ipam(console: Console, prev: dict | None = None, skip_confirm: bool = False) -> dict:
    prev = prev or {}
    console.print("\n[bold cyan]─── phpIPAM Configuration ───[/bold cyan]\n")

    # If phpIPAM is already configured, offer to keep it
    prev_url = prev.get("url", "")
    if prev_url:
        console.print(f"  [dim]Existing phpIPAM: {prev_url}[/dim]")
        if Confirm.ask("Keep existing phpIPAM configuration?", default=True):
            return {
                "provider": prev.get("provider", "phpipam"),
                "url": prev_url,
                "app_id": prev.get("app_id", "infraforge"),
                "token": prev.get("token", ""),
                "username": prev.get("username", ""),
                "password": prev.get("password", ""),
                "verify_ssl": prev.get("verify_ssl", False),
            }
        console.print()

    if not skip_confirm and not Confirm.ask("Configure phpIPAM for IP address management?", default=True):
        return _empty_ipam_config()

    console.print()
    console.print("[bold]phpIPAM setup method:[/bold]")
    console.print("  1) Connect to existing phpIPAM server [dim](recommended)[/dim]")
    console.print("  2) Deploy new phpIPAM with Docker")
    console.print("  3) Skip for now")
    ipam_choice = Prompt.ask("Select", choices=["1", "2", "3"], default="1")

    if ipam_choice == "3":
        return _empty_ipam_config()

    if ipam_choice == "1":
        return _configure_ipam_existing(console, prev)

    return _configure_ipam_docker(console, prev)


def _configure_ipam_existing(console: Console, prev: dict) -> dict:
    """Configure connection to an existing phpIPAM server."""
    console.print()
    console.print(
        "[dim]Enter your phpIPAM server details.\n"
        "You'll need an API app configured in phpIPAM:\n"
        "  Administration > API > Create API app\n"
        "  Set app_id, permissions (Read/Write), and security method.[/dim]\n"
    )

    ipam_url = Prompt.ask(
        "phpIPAM URL (e.g. https://ipam.example.com)",
        default=prev.get("url") or None,
    )
    # Strip trailing slash
    ipam_url = ipam_url.rstrip("/")

    app_id = Prompt.ask("API app ID", default=prev.get("app_id", "infraforge"))

    # Auth method
    prev_has_token = bool(prev.get("token"))
    prev_has_user = bool(prev.get("username"))
    default_auth = "1" if prev_has_token else "2" if prev_has_user else "1"

    console.print()
    console.print("[bold]Authentication method:[/bold]")
    console.print("  1) API Token [dim](app security = 'none' or 'ssl')[/dim]")
    console.print("  2) Username / Password [dim](app security = 'user')[/dim]")
    auth_choice = Prompt.ask("Select", choices=["1", "2"], default=default_auth)

    token = ""
    username = ""
    password = ""

    if auth_choice == "1":
        prev_token = prev.get("token", "")
        if prev_token:
            masked = prev_token[:4] + "..." + prev_token[-4:] if len(prev_token) > 8 else "****"
            console.print(f"  [dim]Current token: {masked}[/dim]")
            if Confirm.ask("  Keep existing token?", default=True):
                token = prev_token
            else:
                token = Prompt.ask("API Token", password=True)
        else:
            token = Prompt.ask(
                "API Token [dim](leave blank if app security is 'none')[/dim]",
                default="",
            )
    else:
        username = Prompt.ask("Username", default=prev.get("username", "admin"))
        prev_pw = prev.get("password", "")
        if prev_pw:
            console.print("  [dim]Password is already set.[/dim]")
            if Confirm.ask("  Keep existing password?", default=True):
                password = prev_pw
            else:
                password = Prompt.ask("Password", password=True)
        else:
            password = Prompt.ask("Password", password=True)

    verify_ssl = Confirm.ask("Verify SSL certificate?", default=prev.get("verify_ssl", False))

    result = {
        "provider": "phpipam",
        "url": ipam_url,
        "app_id": app_id,
        "token": token,
        "username": username,
        "password": password,
        "verify_ssl": verify_ssl,
    }

    # Test connection
    if Confirm.ask("\nTest phpIPAM connection?", default=True):
        _test_ipam_connection(console, result)

    return result


def _test_ipam_connection(console: Console, ipam_config: dict) -> None:
    """Test phpIPAM API connectivity."""
    console.print("[dim]Connecting to phpIPAM...[/dim]")
    try:
        from infraforge.ipam_client import IPAMClient, IPAMError

        # Build a minimal Config-like object for the client
        from infraforge.config import Config, IPAMConfig
        cfg = Config()
        cfg.ipam = IPAMConfig(
            provider=ipam_config.get("provider", "phpipam"),
            url=ipam_config.get("url", ""),
            app_id=ipam_config.get("app_id", ""),
            token=ipam_config.get("token", ""),
            username=ipam_config.get("username", ""),
            password=ipam_config.get("password", ""),
            verify_ssl=ipam_config.get("verify_ssl", False),
        )

        client = IPAMClient(cfg)
        if client.check_health():
            console.print(f"[green]✓[/green] Connected to phpIPAM at {ipam_config['url']}")
            # Show summary
            try:
                sections = client.get_sections()
                vlans = client.get_vlans()
                console.print(f"  Sections: {len(sections)}  |  VLANs: {len(vlans)}")
            except IPAMError:
                pass
        else:
            console.print(f"[red]✗[/red] Cannot reach phpIPAM at {ipam_config['url']}")
            console.print("[yellow]Check the URL, app ID, and credentials.[/yellow]")
    except Exception as e:
        console.print(f"[red]✗[/red] phpIPAM test failed: {e}")


def _configure_ipam_docker(console: Console, prev: dict) -> dict:
    """Deploy a new phpIPAM instance with Docker."""
    console.print()
    console.print(
        "[dim]A local Docker instance will be deployed automatically.[/dim]\n"
    )

    # ── Check prerequisites ──
    if not _check_docker(console):
        console.print()
        if Confirm.ask("Connect to an existing phpIPAM server instead?", default=True):
            return _configure_ipam_existing(console, prev)
        return _empty_ipam_config()

    # ── Port ──
    prev_port = "8443"
    env_path = DOCKER_DIR / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("IPAM_PORT="):
                    prev_port = line.split("=", 1)[1].strip()
        except Exception:
            pass
    ipam_port = Prompt.ask("phpIPAM HTTPS port", default=prev_port)

    # ── Credentials ──
    admin_password = Prompt.ask("phpIPAM admin password", default="admin", password=True)
    db_pass = secrets.token_urlsafe(16)

    # ── Generate SSL certs ──
    console.print("\n[dim]Generating self-signed SSL certificate...[/dim]")
    ssl_script = DOCKER_DIR / "phpipam" / "generate-ssl.sh"
    subprocess.run(["bash", str(ssl_script)], check=True, capture_output=True)
    console.print("[green]✓[/green] SSL certificate generated")

    # ── Write .env ──
    env_path = DOCKER_DIR / ".env"
    env_path.write_text(
        f"IPAM_DB_ROOT_PASS={secrets.token_urlsafe(16)}\n"
        f"IPAM_DB_PASS={db_pass}\n"
        f"IPAM_PORT={ipam_port}\n"
        f"SCAN_INTERVAL=15m\n"
    )

    # ── Launch containers ──
    console.print("\n[bold]Launching phpIPAM containers...[/bold]")
    try:
        compose_cmd = _get_compose_cmd()
        subprocess.run(
            [*compose_cmd, "up", "-d"],
            cwd=str(DOCKER_DIR),
            check=True,
            capture_output=True,
        )
        console.print("[green]✓[/green] Containers started")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]✗[/red] Failed to start containers: {e.stderr.decode() if e.stderr else e}")
        return _empty_ipam_config()

    # ── Wait for phpIPAM to be ready ──
    ipam_url = f"https://localhost:{ipam_port}"
    console.print(f"\n[dim]Waiting for phpIPAM at {ipam_url}...[/dim]")

    if not _wait_for_phpipam(ipam_url, timeout=120):
        console.print("[red]✗[/red] phpIPAM did not become ready in time")
        console.print("[dim]Check: docker logs infraforge-ipam-web[/dim]")
        return _empty_ipam_config()

    console.print("[green]✓[/green] phpIPAM is running")

    # ── Bootstrap: create API app, set admin password, enable scanning ──
    console.print("[dim]Bootstrapping phpIPAM (API app, scanning, admin password)...[/dim]")
    _bootstrap_phpipam(console, admin_password, db_pass)

    console.print(f"\n[green]✓[/green] phpIPAM deployed at [bold]{ipam_url}[/bold]")
    console.print(f"  [dim]Web UI: {ipam_url}  (admin / {admin_password})[/dim]")

    return {
        "provider": "phpipam",
        "url": ipam_url,
        "app_id": "infraforge",
        "token": "",
        "username": "admin",
        "password": admin_password,
        "verify_ssl": False,
    }


def _empty_ipam_config() -> dict:
    return {
        "provider": "", "url": "", "app_id": "", "token": "",
        "username": "", "password": "", "verify_ssl": False,
    }


def _check_docker(console: Console) -> bool:
    """Verify Docker and docker compose are available."""
    if not shutil.which("docker"):
        console.print("[red]✗[/red] Docker not found. Please install Docker first.")
        return False

    # Check if Docker daemon is running and accessible
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").lower()
        if "permission denied" in stderr:
            console.print("[red]✗[/red] Permission denied accessing Docker.")
            console.print("[dim]Fix with: sudo usermod -aG docker $USER && newgrp docker[/dim]")
        else:
            console.print(f"[red]✗[/red] Docker daemon is not running or not accessible.")
            if e.stderr:
                console.print(f"[dim]{e.stderr.strip()[:200]}[/dim]")
        return False
    except subprocess.TimeoutExpired:
        console.print("[red]✗[/red] Docker daemon timed out.")
        return False

    # Check for compose — strongly prefer v2 plugin over legacy v1
    try:
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=True,
        )
        console.print("[green]✓[/green] Docker and docker compose v2 found")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if shutil.which("docker-compose"):
        # Legacy docker-compose v1 (Python) is often broken with newer requests/urllib3
        console.print("[yellow]![/yellow] Only legacy docker-compose v1 found (may be broken).")
        console.print(
            "[dim]Install docker compose v2 plugin for reliability:\n"
            "  sudo mkdir -p /usr/local/lib/docker/cli-plugins\n"
            '  sudo curl -SL "https://github.com/docker/compose/releases/latest/'
            'download/docker-compose-linux-x86_64" \\\n'
            "    -o /usr/local/lib/docker/cli-plugins/docker-compose\n"
            "  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose[/dim]"
        )
        if Confirm.ask("Try with legacy docker-compose anyway?", default=False):
            return True
        return False

    console.print("[red]✗[/red] docker compose not found.")
    console.print(
        "[dim]Install the docker compose plugin:\n"
        "  sudo mkdir -p /usr/local/lib/docker/cli-plugins\n"
        '  sudo curl -SL "https://github.com/docker/compose/releases/latest/'
        'download/docker-compose-linux-x86_64" \\\n'
        "    -o /usr/local/lib/docker/cli-plugins/docker-compose\n"
        "  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose[/dim]"
    )
    return False


def _get_compose_cmd() -> list[str]:
    """Return the compose command as a list. Prefers v2 plugin."""
    try:
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=True,
        )
        return ["docker", "compose"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        if shutil.which("docker-compose"):
            return ["docker-compose"]
        return ["docker", "compose"]  # Fall back; will fail with clear error


def _wait_for_phpipam(url: str, timeout: int = 120) -> bool:
    """Wait until phpIPAM web responds (even with self-signed cert)."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    import requests as req

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = req.get(url, verify=False, timeout=5)
            if resp.status_code in (200, 302, 301):
                # Give DB schema a few more seconds to fully initialize
                time.sleep(5)
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _bootstrap_phpipam(console: Console, admin_password: str, db_pass: str) -> None:
    """Bootstrap phpIPAM via direct DB access in the MariaDB container."""
    try:
        # Wait a bit for schema to be fully created by phpIPAM on first access
        time.sleep(3)

        db_container = "infraforge-ipam-db"

        def db_exec(sql: str) -> str:
            result = subprocess.run(
                ["docker", "exec", db_container, "mysql", "-u", "phpipam",
                 f"-p{db_pass}", "phpipam", "-sN", "-e", sql],
                capture_output=True, text=True, timeout=15,
            )
            return result.stdout.strip()

        # Wait for settings table (phpIPAM auto-creates schema on first web access)
        for attempt in range(30):
            try:
                count = db_exec("SELECT COUNT(*) FROM settings;")
                if count and int(count) > 0:
                    break
            except Exception:
                pass
            time.sleep(3)
        else:
            console.print("[yellow]Warning: DB schema may not be fully ready[/yellow]")

        # Enable API globally
        db_exec("UPDATE settings SET api=1, scanPingType='fping', scanMaxThreads=32;")

        # Create API app
        existing = db_exec("SELECT COUNT(*) FROM api WHERE app_id='infraforge';")
        if existing and int(existing) > 0:
            db_exec("UPDATE api SET app_permissions=2, app_security='none' WHERE app_id='infraforge';")
        else:
            db_exec(
                "INSERT INTO api (app_id, app_code, app_permissions, app_security, app_lock_expire) "
                "VALUES ('infraforge', 'infraforge_auto', 2, 'none', 0);"
            )

        # Set admin password via PHP inside the web container
        escaped_pw = admin_password.replace("'", "\\'")
        hash_result = subprocess.run(
            ["docker", "exec", "infraforge-ipam-web",
             "php", "-r", f"echo password_hash('{escaped_pw}', PASSWORD_DEFAULT);"],
            capture_output=True, text=True, timeout=10,
        )
        if hash_result.returncode == 0 and hash_result.stdout:
            pw_hash = hash_result.stdout.strip().replace("'", "\\'")
            db_exec(f"UPDATE users SET password='{pw_hash}' WHERE username='admin';")

        # Ensure default scan agent exists
        agent_count = db_exec("SELECT COUNT(*) FROM scanAgents WHERE id=1;")
        if agent_count and int(agent_count) > 0:
            db_exec("UPDATE scanAgents SET type='mysql' WHERE id=1;")
        else:
            db_exec(
                "INSERT INTO scanAgents (id, name, description, type) "
                "VALUES (1, 'cron', 'Default cron agent', 'mysql');"
            )

        console.print("[green]✓[/green] phpIPAM bootstrapped (API enabled, scanning on)")

    except Exception as e:
        console.print(f"[yellow]Warning: Bootstrap partially failed: {e}[/yellow]")
        console.print("[dim]You may need to configure the API app manually in phpIPAM web UI.[/dim]")


# =====================================================================
# Subnet Configuration (post-deploy)
# =====================================================================

def _configure_subnets(console: Console, config_path: Path) -> None:
    """Interactively configure subnets in phpIPAM."""
    console.print("\n[bold cyan]─── Subnet Configuration ───[/bold cyan]\n")
    console.print(
        "[dim]Add your IP subnets so InfraForge can allocate static IPs for new VMs.\n"
        "You can add more subnets later via the phpIPAM web UI.[/dim]\n"
    )

    if not Confirm.ask("Configure subnets now?", default=True):
        return

    # Connect to phpIPAM via our client
    try:
        from infraforge.config import Config
        from infraforge.ipam_client import IPAMClient

        cfg = Config.load(config_path)
        client = IPAMClient(cfg)

        if not client.check_health():
            console.print("[red]✗[/red] Cannot connect to phpIPAM API")
            return
    except Exception as e:
        console.print(f"[red]✗[/red] Failed to connect to phpIPAM: {e}")
        return

    # Create or find the InfraForge section
    section = client.find_section_by_name("InfraForge")
    if not section:
        try:
            result = client.create_section("InfraForge", "Managed by InfraForge")
            section_id = result if isinstance(result, (int, str)) else result.get("id", 1)
        except Exception:
            # Fall back to default section (id=1)
            section_id = 1
    else:
        section_id = section["id"]

    console.print(f"[green]✓[/green] Using IPAM section: InfraForge (id={section_id})\n")

    # Add subnets interactively
    while True:
        console.print("[bold]Add a subnet:[/bold]")
        subnet_cidr = Prompt.ask(
            "Subnet CIDR (e.g. 10.0.7.0/24)",
            default="",
        )
        if not subnet_cidr:
            break

        # Parse CIDR
        try:
            import ipaddress
            net = ipaddress.ip_network(subnet_cidr, strict=False)
            subnet_addr = str(net.network_address)
            mask = net.prefixlen
        except ValueError:
            console.print("[red]Invalid CIDR notation. Try again.[/red]")
            continue

        description = Prompt.ask("Description", default=f"{subnet_cidr}")
        vlan_input = Prompt.ask("VLAN ID (leave blank for none)", default="")

        vlan_id = None
        if vlan_input.strip():
            try:
                vlan_num = int(vlan_input.strip())
                # Find or create the VLAN
                vlan = client.find_vlan_by_number(vlan_num)
                if vlan:
                    vlan_id = vlan["vlanId"]
                else:
                    vlan_name = Prompt.ask("VLAN name", default=f"VLAN {vlan_num}")
                    try:
                        result = client.create_vlan(vlan_num, vlan_name)
                        vlan_id = result if isinstance(result, (int, str)) else result.get("id")
                        console.print(f"  [green]✓[/green] VLAN {vlan_num} created")
                    except Exception as e:
                        console.print(f"  [yellow]Warning: Could not create VLAN: {e}[/yellow]")
            except ValueError:
                console.print("[yellow]Invalid VLAN ID, skipping.[/yellow]")

        # Create subnet with scanning enabled
        try:
            client.create_subnet(
                subnet=subnet_addr,
                mask=mask,
                section_id=section_id,
                description=description,
                vlan_id=vlan_id,
                ping_subnet=True,
                discover_subnet=True,
            )
            console.print(f"  [green]✓[/green] Subnet {subnet_addr}/{mask} created with ping scanning enabled")
        except Exception as e:
            console.print(f"  [red]✗[/red] Failed to create subnet: {e}")

        if not Confirm.ask("\nAdd another subnet?", default=True):
            break

    console.print("\n[green]✓[/green] Subnet configuration complete")
    console.print("[dim]The phpIPAM cron container will begin scanning within 15 minutes.[/dim]")


# =====================================================================
# Connection Tests
# =====================================================================

def _test_proxmox_connection(console: Console, config_path: Path) -> None:
    console.print("[dim]Connecting...[/dim]")
    try:
        from infraforge.config import Config
        from infraforge.proxmox_client import ProxmoxClient

        cfg = Config.load(config_path)
        client = ProxmoxClient(cfg)
        client.connect()
        nodes = client.get_node_info()

        console.print(f"[green]✓[/green] Connected! Found {len(nodes)} node(s):")
        for n in nodes:
            console.print(
                f"  [bold]{n.node}[/bold] - {n.status}"
                f" | CPU: {n.cpu_percent:.1f}% | Uptime: {n.uptime_str}"
            )
    except Exception as e:
        console.print(f"[red]✗[/red] Connection failed: {e}")
        console.print("[yellow]You can edit the config later and try again.[/yellow]")
