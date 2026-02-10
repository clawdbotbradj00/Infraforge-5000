<div align="center">

<img src="assets/logo.png" alt="InfraForge" width="100%">

### Manage your entire Proxmox homelab from one terminal.

```bash
git clone https://github.com/clawdbotbradj00/Infraforge-5000.git && cd Infraforge-5000 && bash setup.sh
```

The setup wizard connects to Proxmox, deploys phpIPAM via Docker, configures DNS, and drops you into InfraForge — ready to go.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![GitHub Release](https://img.shields.io/badge/Release-v0.8.0-brightgreen)](https://github.com/clawdbotbradj00/Infraforge-5000/releases/latest)

</div>

---

## What is InfraForge?

InfraForge is a terminal UI that puts your Proxmox cluster, DNS, IP address management, Ansible playbooks, and Terraform provisioning behind a single keyboard-driven interface. It runs over SSH, needs no web browser, and talks directly to your existing infrastructure APIs.

**VMs & Containers** — Browse, sort, filter, and group everything running across your nodes. Drill into any VM for live resource stats.

**DNS** — Manage multiple BIND9 zones with full record CRUD, AXFR zone transfers, and RFC 2136 dynamic updates authenticated via TSIG.

**IPAM** — Browse subnets, VLANs, and addresses in a tree view. phpIPAM deploys as a turnkey Docker stack (web + MariaDB + cron scanner) — the setup wizard handles everything.

**Ansible** — Drop `.yml` playbooks into a directory and run them from InfraForge with dynamic host targeting and live-streamed output.

**Terraform** — Provision new VMs through a guided wizard that generates HCL, runs init/plan/apply, registers DNS, and assigns IPs from IPAM.

**AI Copilot** — Press `/` anywhere to open a Claude-powered assistant that can query your VMs, manage DNS records, create subnets, and navigate screens.

**Cloud Images** — Download Ubuntu, Debian, Rocky, Alma, Fedora, and openSUSE cloud images with automatic SHA256 integrity verification.

---

## Requirements

- Python 3.10+
- Proxmox VE 7.x or 8.x with API access
- Docker + Docker Compose v2 (for phpIPAM — installed automatically if missing)
- BIND9 with TSIG key (optional, for DNS management)

## Manual Setup

If you prefer not to use the setup wizard:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
mkdir -p ~/.config/infraforge
cp config/config.example.yaml ~/.config/infraforge/config.yaml
# Edit config.yaml with your Proxmox host, API token, etc.
infraforge
```

Re-run `infraforge setup` anytime to reconfigure.

## Configuration

Config lives at `~/.config/infraforge/config.yaml`. Example:

```yaml
proxmox:
  host: "192.0.2.10"
  port: 8006
  user: "root@pam"
  auth_method: "token"
  token_name: "infraforge"
  token_value: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  verify_ssl: true

dns:
  provider: "bind9"
  server: "198.51.100.1"
  zones: ["example.com", "dev.example.com"]
  domain: "example.com"
  tsig_key_name: "infraforge-key"
  tsig_key_secret: "base64-encoded-secret"

ipam:
  provider: "phpipam"
  url: "https://localhost:8443"
  app_id: "infraforge"
  username: "admin"
  password: "your-password"
  verify_ssl: true

ansible:
  playbook_dir: "./ansible/playbooks"
  host_key_checking: true
```

Create a Proxmox API token at **Datacenter > Permissions > API Tokens** with at least `PVEAuditor` role.

## Keybindings

| Key | Action |
|-----|--------|
| `d` | Dashboard |
| `1`-`7` | Jump to screen (VMs, Templates, Nodes, New VM, DNS, IPAM, Ansible) |
| `/` | AI Copilot |
| `s` / `f` / `g` | Sort / Filter / Group (in list views) |
| `r` | Refresh |
| `?` | Help |
| `q` | Quit |
| `Escape` | Go back |

## phpIPAM Docker Stack

Deployed automatically by the setup wizard as three containers:

| Container | Purpose | Port |
|-----------|---------|------|
| `infraforge-ipam-web` | phpIPAM web UI + API | 8443 (HTTPS) |
| `infraforge-ipam-db` | MariaDB | internal |
| `infraforge-ipam-cron` | fping subnet scanner | internal |

```bash
cd docker && docker compose logs -f    # View logs
cd docker && docker compose restart    # Restart
```

## License

MIT
