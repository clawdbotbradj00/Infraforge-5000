# InfraForge

A terminal UI for managing Proxmox VE clusters, with integrated phpIPAM for IP address management and BIND9 for DNS record automation.

Built with [Textual](https://github.com/Textualize/textual) (Python TUI framework).

## Features

- **Dashboard** — Cluster overview with VM counts, node health bars, and quick navigation
- **VM Management** — List all VMs/containers with sort, filter, and group controls; drill into details
- **Template Browser** — Tabbed view of VM templates, container templates, and ISO images
- **Node Info** — Per-node CPU, memory, disk, and storage pool stats
- **DNS Management** — Multi-zone BIND9 management with full record CRUD (add/edit/delete), zone switching, and AXFR zone transfers
- **New VM Wizard** — 8-step guided creation (basics, template, resources, network, IPAM, DNS, provisioning, review)
- **phpIPAM Integration** — Auto-deployed Docker stack with subnet scanning and IP allocation
- **Persistent Preferences** — Sort/filter/group settings survive restarts
- **Parallelized API Calls** — ThreadPoolExecutor for fast data loading across nodes

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
| `Escape` | Go back |

### Dashboard
| Key | Action |
|-----|--------|
| `v` | Virtual Machines |
| `t` | Templates |
| `n` | Node Info |
| `x` | DNS Management |
| `i` | IPAM Management |
| `c` | Create New VM |
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

## Project Structure

```
InfraForge/
├── infraforge/
│   ├── app.py              # Main Textual App
│   ├── config.py           # Config dataclasses + YAML loader
│   ├── models.py           # VM, Template, Node data models
│   ├── proxmox_client.py   # Proxmox API wrapper (parallelized)
│   ├── ipam_client.py      # phpIPAM REST client
│   ├── dns_client.py       # BIND9 client (dnspython, TSIG, RFC 2136)
│   ├── preferences.py      # Persistent sort/filter/group prefs
│   ├── setup_wizard.py     # Rich-based interactive setup
│   └── screens/
│       ├── dashboard.py    # Main dashboard
│       ├── vm_list.py      # VM list with sort/filter/group
│       ├── vm_detail.py    # VM detail view
│       ├── template_list.py # Tabbed template browser
│       ├── template_detail.py
│       ├── node_info.py    # Cluster node details
│       ├── dns_screen.py   # DNS zone record viewer
│       ├── ipam_screen.py  # IPAM management (phpIPAM)
│       ├── new_vm.py       # 8-step VM creation wizard
│       └── help_screen.py  # Keybinding reference
├── styles/
│   └── app.tcss            # Textual CSS
├── docker/
│   ├── docker-compose.yml  # phpIPAM stack (web + db + cron)
│   ├── .env.example
│   └── phpipam/
│       ├── bootstrap-ipam.sh   # DB seeding script
│       └── generate-ssl.sh     # Self-signed cert generator
├── config/
│   └── config.example.yaml
├── setup.sh                # Bash setup wizard
├── requirements.txt
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
