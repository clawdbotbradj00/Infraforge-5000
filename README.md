<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![GitHub Release](https://img.shields.io/badge/Release-v0.6.0-brightgreen)](https://github.com/clawdbotbradj00/InfraForge/releases/latest)
[![GitHub Last Commit](https://img.shields.io/badge/Last_Commit-2026--02--08-blue)](https://github.com/clawdbotbradj00/InfraForge/commits/main)
[![GitHub Issues](https://img.shields.io/badge/Issues-Open-orange)](https://github.com/clawdbotbradj00/InfraForge/issues)

[![Built with Textual](https://img.shields.io/badge/Built_with-Textual-4EAA25?logo=python&logoColor=white)](https://github.com/Textualize/textual)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-VE_7.x%2F8.x-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![Docker](https://img.shields.io/badge/Docker-phpIPAM_Stack-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![BIND9 DNS](https://img.shields.io/badge/BIND9-DNS_Automation-4A8CC2?logo=internetexplorer&logoColor=white)](https://www.isc.org/bind/)
[![Ansible](https://img.shields.io/badge/Ansible-Playbook_Runner-EE0000?logo=ansible&logoColor=white)](https://www.ansible.com/)
[![Claude AI](https://img.shields.io/badge/Claude-AI_Copilot-D4A574?logo=anthropic&logoColor=white)](https://claude.ai/)

# InfraForge

**A powerful terminal UI for managing Proxmox VE infrastructure**

Integrated phpIPAM for IP address management, BIND9 for DNS automation, Ansible playbook execution, and an AI copilot powered by Claude.

<img src="assets/logo.png" alt="InfraForge Logo" width="100%">

</div>

---

## Features

- **Dashboard** — Cluster overview with VM counts, node health bars, numbered hotkeys for quick navigation
- **VM Management** — List all VMs/containers with sort, filter, and group controls; drill into details
- **Template Browser** — Hierarchical tree of VM templates, container templates, and ISO images
- **Node Info** — Per-node CPU, memory, disk, and storage pool stats
- **DNS Management** — Multi-zone BIND9 with hierarchical tree view, full record CRUD, AXFR zone transfers, SOA display
- **IPAM Management** — phpIPAM integration with tree-based subnet/VLAN/address browsing, section management, ping scan control
- **Ansible Playbooks** — Auto-discovers `.yml` playbooks from a configured directory; run against dynamically targeted hosts with ping-sweep validation and live-streamed output
- **AI Copilot** — Claude-powered assistant (`/` to open) that can query VMs, manage DNS records, create IPAM subnets, navigate screens, and more — with streaming responses and persistent chat history
- **New VM Wizard** — 8-step guided creation (basics, template, resources, network, IPAM, DNS, provisioning, review)
- **phpIPAM Docker Stack** — Turnkey 3-container deployment (web + MariaDB + cron scanner) with automated bootstrap
- **Persistent Preferences** — Sort/filter/group settings survive restarts
- **Parallelized API Calls** — ThreadPoolExecutor for fast data loading across nodes
- **Auto-Update Checker** — Checks GitHub releases on startup, notifies when updates are available

## Requirements

- Python 3.10+
- Proxmox VE 7.x or 8.x with API access
- Docker + Docker Compose (for phpIPAM)
- BIND9 server with TSIG key (for DNS management, optional)

## Quick Start

### Automated Setup

```bash
git clone https://github.com/clawdbotbradj00/InfraForge.git
cd InfraForge
bash setup.sh
```

The setup wizard will:
1. Create a Python virtual environment and install dependencies
2. Configure your Proxmox connection (API token or password)
3. Configure BIND9 DNS (optional)
4. Deploy phpIPAM via Docker with automated bootstrap
5. Walk you through adding subnets for IP scanning

### Manual Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Copy and edit the config:

```bash
mkdir -p ~/.config/infraforge
cp config/config.example.yaml ~/.config/infraforge/config.yaml
# Edit with your Proxmox host, credentials, etc.
```

### Run

```bash
# If using venv:
source .venv/bin/activate
infraforge

# Or directly:
.venv/bin/infraforge

# Re-run setup anytime:
infraforge setup
```

## Configuration

Config lives at `~/.config/infraforge/config.yaml`. The setup wizard pre-populates previous values as defaults when re-run.

### Proxmox

```yaml
proxmox:
  host: "10.0.0.50"
  port: 8006
  user: "root@pam"
  auth_method: "token"       # "token" or "password"
  token_name: "infraforge"
  token_value: "your-token-secret"
  verify_ssl: false
```

Create an API token in Proxmox: **Datacenter > Permissions > API Tokens**. The token needs at least `PVEAuditor` role for read access.

### BIND9 DNS (Optional)

```yaml
dns:
  provider: "bind9"
  server: "10.0.0.1"
  port: 53
  zones:
    - "lab.local"
    - "dev.local"
  domain: "lab.local"
  tsig_key_name: "infraforge-key"
  tsig_key_secret: "base64-encoded-secret"
  tsig_algorithm: "hmac-sha256"
```

Zones can also be added/removed from within the DNS Management screen in InfraForge.

Generate a TSIG key on your BIND9 server:

```bash
tsig-keygen infraforge-key
```

Add the key to your BIND9 config and set `allow-update` and `allow-transfer` for the key on your zone.

### phpIPAM

```yaml
ipam:
  provider: "phpipam"
  url: "https://localhost:8443"
  app_id: "infraforge"
  username: "admin"
  password: "your-admin-password"
  verify_ssl: false
```

phpIPAM is deployed automatically by the setup wizard as a Docker stack (web + MariaDB + cron scanner). The bootstrap creates an API app with read/write access and enables fping-based subnet scanning.

## Keybindings

### Global
| Key | Action |
|-----|--------|
| `q` | Quit |
| `d` | Go to Dashboard |
| `?` | Help screen |
| `/` | Open AI Copilot |
| `Escape` | Go back |

### Dashboard
| Key | Action |
|-----|--------|
| `1` | Virtual Machines |
| `2` | Templates |
| `3` | Node Info |
| `4` | Create New VM |
| `5` | DNS Management |
| `6` | IPAM Management |
| `7` | Ansible Playbooks |
| `r` | Refresh |

### VM List / Templates
| Key | Action |
|-----|--------|
| `s` | Cycle sort field |
| `f` | Cycle filter |
| `g` | Cycle grouping |
| `r` | Refresh |
| `Enter` | View details |

### DNS Management
| Key | Action |
|-----|--------|
| `Tab` / `Shift+Tab` | Switch zones |
| `1`-`9` | Jump to zone by number |
| `z` | Add a zone |
| `Z` | Remove current zone |
| `a` | Add DNS record |
| `e` | Edit selected record |
| `d` | Delete selected record |
| `s` | Cycle sort field |
| `f` | Cycle filter by record type |
| `r` | Refresh records |

### IPAM Management
| Key | Action |
|-----|--------|
| `Tab` | Switch view (Subnets/Addresses/VLANs) |
| `Enter` | View subnet addresses |
| `s` | Cycle sort |
| `f` | Cycle filter |
| `r` | Refresh |
| `Escape` | Go back |

### Ansible Playbooks
| Key | Action |
|-----|--------|
| `x` / `Enter` | Run selected playbook |
| `l` | View last run log |
| `s` | Cycle sort field |
| `r` | Refresh / rescan directory |

### AI Copilot
| Key | Action |
|-----|--------|
| `/` | Open AI chat (from any screen) |
| `Escape` | Cancel generation / close chat |
| `Ctrl+N` | Start new conversation |

## Project Structure

```
InfraForge/
├── infraforge/
│   ├── app.py              # Main Textual App
│   ├── config.py           # Config dataclasses + YAML loader
│   ├── models.py           # VM, Template, Node data models
│   ├── proxmox_client.py   # Proxmox API wrapper (parallelized)
│   ├── ipam_client.py      # phpIPAM REST client (full CRUD)
│   ├── dns_client.py       # BIND9 client (dnspython, TSIG, RFC 2136)
│   ├── ansible_runner.py   # Playbook discovery, ping sweep, execution
│   ├── ai_client.py        # Claude CLI integration (streaming)
│   ├── ai_context.py       # Live infrastructure context for AI
│   ├── preferences.py      # Persistent sort/filter/group prefs
│   ├── updater.py          # GitHub release auto-update checker
│   ├── setup_wizard.py     # Rich-based interactive setup
│   └── screens/
│       ├── dashboard.py        # Main dashboard with hotkeys
│       ├── vm_list.py          # VM list with sort/filter/group
│       ├── vm_detail.py        # VM detail view
│       ├── template_list.py    # Hierarchical template browser
│       ├── template_detail.py
│       ├── node_info.py        # Cluster node details
│       ├── dns_screen.py       # Multi-zone DNS tree + CRUD
│       ├── ipam_screen.py      # IPAM tree (subnets/VLANs/addresses)
│       ├── ansible_screen.py   # Playbook discovery + management
│       ├── ansible_run_modal.py # Playbook execution modal
│       ├── ai_chat_modal.py    # AI copilot chat overlay
│       ├── ai_settings_screen.py
│       ├── new_vm.py           # 8-step VM creation wizard
│       └── help_screen.py      # Keybinding reference
├── ansible/
│   └── playbooks/          # Drop .yml playbooks here
├── styles/
│   └── app.tcss            # Textual CSS
├── docker/
│   ├── docker-compose.yml  # phpIPAM stack (web + db + cron)
│   └── phpipam/            # phpIPAM config + bootstrap scripts
├── config/
│   └── config.example.yaml
├── setup.sh                # Bash setup wizard
└── pyproject.toml
```

## phpIPAM Docker Stack

The setup wizard deploys three containers:

| Container | Purpose | Port |
|-----------|---------|------|
| `infraforge-ipam-web` | phpIPAM web UI + API | 8443 (HTTPS) |
| `infraforge-ipam-db` | MariaDB database | internal |
| `infraforge-ipam-cron` | fping subnet scanner | internal |

Manage with:

```bash
cd docker/

# View logs
docker compose logs -f

# Stop
docker compose down

# Restart
docker compose restart
```

## License

MIT
