#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

CONFIG_DIR="$HOME/.config/infraforge"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/docker"

# Read a value from existing config.yaml (simple grep-based YAML reader)
# Usage: cfg_get "proxmox" "host"  →  prints the value or empty string
cfg_get() {
    local section="$1"
    local key="$2"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo ""
        return
    fi
    # Find lines under the section, grab the key's value
    $PYTHON_CMD -c "
import yaml, sys
try:
    with open('$CONFIG_FILE') as f:
        data = yaml.safe_load(f) or {}
    val = data.get('$section', {}).get('$key', '')
    print(val if val is not None else '')
except Exception:
    print('')
" 2>/dev/null || echo ""
}

banner() {
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║          InfraForge Setup Wizard          ║"
    echo "  ║    Proxmox VM Management TUI              ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; }

prompt_input() {
    local prompt="$1"
    local default="${2:-}"
    local var_name="$3"

    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${BOLD}$prompt${NC} [${default}]: ")" value
        value="${value:-$default}"
    else
        read -rp "$(echo -e "${BOLD}$prompt${NC}: ")" value
    fi
    eval "$var_name='$value'"
}

prompt_secret() {
    local prompt="$1"
    local var_name="$2"

    read -srp "$(echo -e "${BOLD}$prompt${NC}: ")" value
    echo
    eval "$var_name='$value'"
}

prompt_yesno() {
    local prompt="$1"
    local default="${2:-y}"

    if [[ "$default" == "y" ]]; then
        read -rp "$(echo -e "${BOLD}$prompt${NC} [Y/n]: ")" answer
        answer="${answer:-y}"
    else
        read -rp "$(echo -e "${BOLD}$prompt${NC} [y/N]: ")" answer
        answer="${answer:-n}"
    fi

    [[ "$answer" =~ ^[Yy] ]]
}

check_python() {
    info "Checking Python version..."

    if command -v python3 &>/dev/null; then
        PYTHON_CMD="python3"
    elif command -v python &>/dev/null; then
        PYTHON_CMD="python"
    else
        error "Python 3.10+ is required but not found."
        error "Install Python and try again."
        exit 1
    fi

    PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
    PYTHON_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')

    if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
        error "Python 3.10+ is required. Found: Python $PYTHON_VERSION"
        exit 1
    fi

    success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
}

setup_venv() {
    info "Setting up Python environment..."

    VENV_DIR="$SCRIPT_DIR/.venv"
    USE_VENV=true

    # Check if venv module actually works (--help can pass even without ensurepip)
    VENV_TEST_DIR=$(mktemp -d)
    if ! $PYTHON_CMD -m venv "$VENV_TEST_DIR/test_venv" &>/dev/null 2>&1; then
        rm -rf "$VENV_TEST_DIR"
        warn "python3-venv is not installed."

        # Build the correct package name from detected Python version
        local venv_pkg="python${PYTHON_VERSION}-venv"

        if command -v apt-get &>/dev/null; then
            if prompt_yesno "Install ${venv_pkg} now?" "y"; then
                info "Installing ${venv_pkg}..."
                if [[ $EUID -eq 0 ]]; then
                    apt-get install -y "$venv_pkg"
                else
                    warn "Root privileges required — you may be prompted for your password."
                    sudo apt-get install -y "$venv_pkg"
                fi

                # Retry venv creation after install
                VENV_TEST_DIR=$(mktemp -d)
                if $PYTHON_CMD -m venv "$VENV_TEST_DIR/test_venv" &>/dev/null 2>&1; then
                    rm -rf "$VENV_TEST_DIR"
                    success "${venv_pkg} installed"
                else
                    rm -rf "$VENV_TEST_DIR"
                    error "venv still not working after installing ${venv_pkg}."
                    echo
                    if prompt_yesno "Continue with user-level pip install instead?" "y"; then
                        USE_VENV=false
                    else
                        error "Cannot proceed without venv or user-level install. Exiting."
                        exit 1
                    fi
                fi
            else
                echo
                if prompt_yesno "Continue with user-level pip install instead?" "y"; then
                    USE_VENV=false
                else
                    error "Cannot proceed without venv or user-level install. Exiting."
                    exit 1
                fi
            fi
        else
            warn "apt not found — install ${venv_pkg} (or equivalent for your distro) manually."
            echo
            if prompt_yesno "Continue with user-level pip install instead?" "y"; then
                USE_VENV=false
            else
                error "Cannot proceed without venv or user-level install. Exiting."
                exit 1
            fi
        fi
    else
        rm -rf "$VENV_TEST_DIR"
    fi

    if $USE_VENV; then
        if [[ -d "$VENV_DIR" ]]; then
            if prompt_yesno "Virtual environment already exists. Recreate it?" "n"; then
                rm -rf "$VENV_DIR"
                $PYTHON_CMD -m venv "$VENV_DIR"
                success "Virtual environment recreated at $VENV_DIR"
            else
                success "Using existing virtual environment"
            fi
        else
            $PYTHON_CMD -m venv "$VENV_DIR"
            success "Virtual environment created at $VENV_DIR"
        fi

        source "$VENV_DIR/bin/activate"

        info "Installing dependencies..."
        pip install --upgrade pip -q
        pip install -r "$SCRIPT_DIR/requirements.txt" -q
        pip install -e "$SCRIPT_DIR" -q
        success "Dependencies installed"

        # Create system-wide symlink so 'infraforge' works from anywhere
        VENV_BIN="$VENV_DIR/bin/infraforge"
        GLOBAL_LINK="/usr/local/bin/infraforge"
        if [[ -f "$VENV_BIN" ]]; then
            if [[ $EUID -eq 0 ]]; then
                ln -sf "$VENV_BIN" "$GLOBAL_LINK"
            else
                sudo ln -sf "$VENV_BIN" "$GLOBAL_LINK" 2>/dev/null || true
            fi
            if [[ -L "$GLOBAL_LINK" ]]; then
                success "infraforge command installed to $GLOBAL_LINK"
            fi
        fi
    else
        info "Installing dependencies with pip (user-level)..."
        $PYTHON_CMD -m pip install --user --upgrade pip -q 2>/dev/null || true
        $PYTHON_CMD -m pip install --user -r "$SCRIPT_DIR/requirements.txt" -q
        $PYTHON_CMD -m pip install --user -e "$SCRIPT_DIR" -q
        success "Dependencies installed (user-level)"

        # Ensure user site-packages bin is on PATH
        USER_BIN="$($PYTHON_CMD -m site --user-base)/bin"
        if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
            export PATH="$USER_BIN:$PATH"
            warn "Added $USER_BIN to PATH for this session"
            warn "Add this to your shell profile for persistence:"
            echo -e "    ${BOLD}export PATH=\"$USER_BIN:\$PATH\"${NC}"
        fi
    fi
}

load_existing_config() {
    # Load previous values from existing config (if any) for use as defaults
    PREV_PVE_HOST=""
    PREV_PVE_PORT=""
    PREV_PVE_USER=""
    PREV_PVE_AUTH=""
    PREV_PVE_TOKEN_NAME=""
    PREV_PVE_TOKEN_VALUE=""
    PREV_PVE_PASSWORD=""
    PREV_PVE_VERIFY_SSL=""
    PREV_DNS_PROVIDER=""
    PREV_DNS_SERVER=""
    PREV_DNS_PORT=""
    PREV_DNS_ZONES=""
    PREV_DNS_DOMAIN=""
    PREV_DNS_TSIG_KEY_NAME=""
    PREV_DNS_TSIG_KEY_SECRET=""
    PREV_DNS_TSIG_ALGORITHM=""
    PREV_DNS_API_KEY=""
    PREV_IPAM_URL=""

    if [[ ! -f "$CONFIG_FILE" ]]; then
        return
    fi

    info "Found existing configuration — loading previous values as defaults."

    PREV_PVE_HOST=$(cfg_get proxmox host)
    PREV_PVE_PORT=$(cfg_get proxmox port)
    PREV_PVE_USER=$(cfg_get proxmox user)
    PREV_PVE_AUTH=$(cfg_get proxmox auth_method)
    PREV_PVE_TOKEN_NAME=$(cfg_get proxmox token_name)
    PREV_PVE_TOKEN_VALUE=$(cfg_get proxmox token_value)
    PREV_PVE_PASSWORD=$(cfg_get proxmox password)
    PREV_PVE_VERIFY_SSL=$(cfg_get proxmox verify_ssl)
    PREV_DNS_PROVIDER=$(cfg_get dns provider)
    PREV_DNS_SERVER=$(cfg_get dns server)
    PREV_DNS_PORT=$(cfg_get dns port)
    # Load zones as space-separated list; fall back to old single "zone" key
    PREV_DNS_ZONES=$($PYTHON_CMD -c "
import yaml
try:
    with open('$CONFIG_FILE') as f:
        data = yaml.safe_load(f) or {}
    dns = data.get('dns', {})
    zones = dns.get('zones', [])
    if not zones and dns.get('zone'):
        zones = [str(dns.get('zone'))]
    print(' '.join(str(z) for z in zones))
except Exception:
    print('')
" 2>/dev/null || echo "")
    PREV_DNS_DOMAIN=$(cfg_get dns domain)
    PREV_DNS_TSIG_KEY_NAME=$(cfg_get dns tsig_key_name)
    PREV_DNS_TSIG_KEY_SECRET=$(cfg_get dns tsig_key_secret)
    PREV_DNS_TSIG_ALGORITHM=$(cfg_get dns tsig_algorithm)
    PREV_DNS_API_KEY=$(cfg_get dns api_key)
    PREV_IPAM_URL=$(cfg_get ipam url)
}

configure_proxmox() {
    echo
    echo -e "${CYAN}${BOLD}── Proxmox Connection Setup ──${NC}"
    echo

    prompt_input "Proxmox host (IP or hostname)" "${PREV_PVE_HOST}" PVE_HOST
    prompt_input "Proxmox API port" "${PREV_PVE_PORT:-8006}" PVE_PORT
    prompt_input "Proxmox user" "${PREV_PVE_USER:-root@pam}" PVE_USER

    local default_auth="1"
    [[ "$PREV_PVE_AUTH" == "password" ]] && default_auth="2"
    echo
    echo -e "${BOLD}Authentication method:${NC}"
    echo "  1) API Token (recommended)"
    echo "  2) Password"
    read -rp "$(echo -e "${BOLD}Select [${default_auth}]:${NC} ")" auth_choice
    auth_choice="${auth_choice:-$default_auth}"

    if [[ "$auth_choice" == "1" ]]; then
        PVE_AUTH_METHOD="token"
        echo
        info "API tokens can be created in Proxmox: Datacenter > Permissions > API Tokens"
        info "The token needs appropriate permissions (PVEAuditor role at minimum for read access)"
        echo
        prompt_input "API Token Name" "${PREV_PVE_TOKEN_NAME:-infraforge}" PVE_TOKEN_NAME

        if [[ -n "$PREV_PVE_TOKEN_VALUE" ]]; then
            local masked="${PREV_PVE_TOKEN_VALUE:0:4}...${PREV_PVE_TOKEN_VALUE: -4}"
            echo -e "  ${DIM}Current token: ${masked}${NC}"
            if prompt_yesno "  Keep existing token value?" "y"; then
                PVE_TOKEN_VALUE="$PREV_PVE_TOKEN_VALUE"
            else
                prompt_secret "API Token Value (secret)" PVE_TOKEN_VALUE
            fi
        else
            prompt_secret "API Token Value (secret)" PVE_TOKEN_VALUE
        fi
        PVE_PASSWORD=""
    else
        PVE_AUTH_METHOD="password"
        if [[ -n "$PREV_PVE_PASSWORD" ]]; then
            echo -e "  ${DIM}Password is already set.${NC}"
            if prompt_yesno "  Keep existing password?" "y"; then
                PVE_PASSWORD="$PREV_PVE_PASSWORD"
            else
                prompt_secret "Password" PVE_PASSWORD
            fi
        else
            prompt_secret "Password" PVE_PASSWORD
        fi
        PVE_TOKEN_NAME=""
        PVE_TOKEN_VALUE=""
    fi

    local ssl_default="n"
    [[ "$PREV_PVE_VERIFY_SSL" == "true" || "$PREV_PVE_VERIFY_SSL" == "True" ]] && ssl_default="y"
    if prompt_yesno "Verify SSL certificate?" "$ssl_default"; then
        PVE_VERIFY_SSL="true"
    else
        PVE_VERIFY_SSL="false"
    fi
}

configure_dns() {
    echo
    echo -e "${CYAN}${BOLD}── DNS Configuration (Optional) ──${NC}"
    echo

    DNS_SERVER="${PREV_DNS_SERVER}"
    DNS_PORT="${PREV_DNS_PORT:-53}"
    DNS_TSIG_KEY_NAME="${PREV_DNS_TSIG_KEY_NAME}"
    DNS_TSIG_KEY_SECRET="${PREV_DNS_TSIG_KEY_SECRET}"
    DNS_TSIG_ALGORITHM="${PREV_DNS_TSIG_ALGORITHM:-hmac-sha256}"
    DNS_ZONES=""

    local has_dns="n"
    [[ -n "$PREV_DNS_PROVIDER" ]] && has_dns="y"
    [[ -n "$PREV_DNS_PROVIDER" ]] && echo -e "  ${DIM}Current provider: ${PREV_DNS_PROVIDER}${NC}"

    # Skip the "do you want to configure?" prompt if we're in missing-only mode
    # and this section was selected for configuration
    local do_configure=false
    if [[ "${SKIP_DNS_CONFIRM:-}" == "true" ]]; then
        do_configure=true
    elif prompt_yesno "Configure DNS provider for automated record management?" "$has_dns"; then
        do_configure=true
    fi

    if $do_configure; then
        # Map existing provider to default selection
        local default_dns="1"
        case "$PREV_DNS_PROVIDER" in
            bind9)      default_dns="1" ;;
            cloudflare) default_dns="2" ;;
            route53)    default_dns="3" ;;
            custom)     default_dns="4" ;;
        esac

        echo -e "${BOLD}DNS Provider:${NC}"
        echo "  1) BIND9 (recommended for self-hosted)"
        echo "  2) Cloudflare"
        echo "  3) Route53 (AWS)"
        echo "  4) Custom / Other"
        echo "  5) Skip for now"
        read -rp "$(echo -e "${BOLD}Select [${default_dns}]:${NC} ")" dns_choice
        dns_choice="${dns_choice:-$default_dns}"

        case "$dns_choice" in
            1)
                DNS_PROVIDER="bind9"
                DNS_API_KEY=""
                echo
                echo -e "${DIM}BIND9 uses TSIG keys for authenticated dynamic updates (RFC 2136)."
                echo -e "Generate a key with: tsig-keygen infraforge-key${NC}"
                echo
                prompt_input "BIND9 server IP/hostname" "${PREV_DNS_SERVER}" DNS_SERVER
                prompt_input "DNS port" "${PREV_DNS_PORT:-53}" DNS_PORT
                prompt_input "TSIG key name" "${PREV_DNS_TSIG_KEY_NAME:-infraforge-key}" DNS_TSIG_KEY_NAME

                if [[ -n "$PREV_DNS_TSIG_KEY_SECRET" ]]; then
                    local masked="${PREV_DNS_TSIG_KEY_SECRET:0:4}...${PREV_DNS_TSIG_KEY_SECRET: -4}"
                    echo -e "  ${DIM}Current TSIG secret: ${masked}${NC}"
                    if prompt_yesno "  Keep existing TSIG secret?" "y"; then
                        DNS_TSIG_KEY_SECRET="$PREV_DNS_TSIG_KEY_SECRET"
                    else
                        prompt_secret "TSIG key secret (base64)" DNS_TSIG_KEY_SECRET
                    fi
                else
                    prompt_secret "TSIG key secret (base64)" DNS_TSIG_KEY_SECRET
                fi

                prompt_input "TSIG algorithm" "${PREV_DNS_TSIG_ALGORITHM:-hmac-sha256}" DNS_TSIG_ALGORITHM

                # Collect DNS zones (multi-zone support)
                echo
                echo -e "${BOLD}Add DNS zones to manage${NC} ${DIM}(you can also add zones later in the TUI)${NC}"
                if [[ -n "$PREV_DNS_ZONES" ]]; then
                    echo -e "  ${DIM}Previous zones: ${PREV_DNS_ZONES}${NC}"
                fi

                DNS_ZONES=""
                while true; do
                    # Suggest the next previous zone as default
                    local zone_default=""
                    local zone_count
                    zone_count=$(echo "$DNS_ZONES" | wc -w)
                    local prev_arr=($PREV_DNS_ZONES)
                    if [[ $zone_count -lt ${#prev_arr[@]} ]]; then
                        zone_default="${prev_arr[$zone_count]}"
                    fi

                    prompt_input "Zone name (blank to finish)" "${zone_default}" zone_input
                    [[ -z "$zone_input" ]] && break

                    # Check for duplicates
                    local is_dup=false
                    for existing_zone in $DNS_ZONES; do
                        if [[ "$existing_zone" == "$zone_input" ]]; then
                            is_dup=true
                            break
                        fi
                    done
                    if $is_dup; then
                        warn "Zone '${zone_input}' already added."
                        continue
                    fi

                    DNS_ZONES="${DNS_ZONES:+$DNS_ZONES }${zone_input}"
                    success "Added zone: ${zone_input}"
                done

                # Domain defaults to first zone
                local first_zone=""
                for z in $DNS_ZONES; do
                    first_zone="$z"
                    break
                done
                prompt_input "Domain for FQDNs" "${PREV_DNS_DOMAIN:-$first_zone}" DNS_DOMAIN
                ;;
            2)
                DNS_PROVIDER="cloudflare"
                if [[ -n "$PREV_DNS_API_KEY" ]]; then
                    echo -e "  ${DIM}Current API key: ${PREV_DNS_API_KEY:0:4}...${NC}"
                    if prompt_yesno "  Keep existing API key?" "y"; then
                        DNS_API_KEY="$PREV_DNS_API_KEY"
                    else
                        prompt_secret "Cloudflare API Key" DNS_API_KEY
                    fi
                else
                    prompt_secret "Cloudflare API Key" DNS_API_KEY
                fi
                local prev_cf_zone=""
                for z in $PREV_DNS_ZONES; do
                    prev_cf_zone="$z"
                    break
                done
                prompt_input "DNS Zone (e.g., example.com)" "${prev_cf_zone}" DNS_ZONE_INPUT
                DNS_ZONES="$DNS_ZONE_INPUT"
                DNS_DOMAIN="$DNS_ZONE_INPUT"
                ;;
            3)
                DNS_PROVIDER="route53"
                prompt_input "AWS Access Key ID" "${PREV_DNS_API_KEY}" DNS_API_KEY
                local prev_r53_zone=""
                for z in $PREV_DNS_ZONES; do
                    prev_r53_zone="$z"
                    break
                done
                prompt_input "DNS Zone ID" "${prev_r53_zone}" DNS_ZONE_INPUT
                DNS_ZONES="$DNS_ZONE_INPUT"
                prompt_input "Domain" "${PREV_DNS_DOMAIN}" DNS_DOMAIN
                ;;
            4)
                DNS_PROVIDER="custom"
                DNS_API_KEY=""
                prompt_input "Domain" "${PREV_DNS_DOMAIN}" DNS_DOMAIN
                DNS_ZONES=""
                ;;
            *)
                DNS_PROVIDER=""
                DNS_API_KEY=""
                DNS_ZONES=""
                DNS_DOMAIN=""
                ;;
        esac
    else
        DNS_PROVIDER=""
        DNS_API_KEY=""
        DNS_ZONES=""
        DNS_DOMAIN=""
    fi
}

# =====================================================================
# phpIPAM Docker Deployment
# =====================================================================

check_docker() {
    info "Checking Docker..."

    if ! command -v docker &>/dev/null; then
        error "Docker not found. Please install Docker first."
        DOCKER_AVAILABLE=false
        return 1
    fi

    local docker_err
    docker_err=$(docker info 2>&1)
    if [[ $? -ne 0 ]]; then
        if echo "$docker_err" | grep -qi "permission denied"; then
            error "Permission denied accessing Docker."
            echo -e "  ${DIM}Fix with: sudo usermod -aG docker \$USER && newgrp docker${NC}"
        else
            error "Docker daemon is not running or not accessible."
        fi
        DOCKER_AVAILABLE=false
        return 1
    fi

    # Check for compose — strongly prefer v2 plugin over legacy v1
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
        success "Docker and docker compose v2 found"
        DOCKER_AVAILABLE=true
        return 0
    elif command -v docker-compose &>/dev/null; then
        # Legacy docker-compose v1 (Python) is often broken with newer requests/urllib3
        warn "Only legacy docker-compose v1 found (may be broken with newer Python packages)."
        echo -e "  ${DIM}Install docker compose v2 plugin for reliability:${NC}"
        echo -e "  ${DIM}  sudo mkdir -p /usr/local/lib/docker/cli-plugins${NC}"
        echo -e "  ${DIM}  sudo curl -SL \"https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64\" \\\\${NC}"
        echo -e "  ${DIM}    -o /usr/local/lib/docker/cli-plugins/docker-compose${NC}"
        echo -e "  ${DIM}  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose${NC}"
        if prompt_yesno "Try with legacy docker-compose anyway?" "n"; then
            COMPOSE_CMD="docker-compose"
            DOCKER_AVAILABLE=true
            return 0
        fi
        DOCKER_AVAILABLE=false
        return 1
    fi

    error "docker compose not found."
    DOCKER_AVAILABLE=false
    return 1
}

check_and_install_terraform() {
    echo
    echo -e "${CYAN}${BOLD}── Terraform ──${NC}"
    if command -v terraform &>/dev/null; then
        local tf_ver
        tf_ver=$(terraform version 2>/dev/null | head -1)
        success "Terraform already installed ($tf_ver)"
        return 0
    fi

    warn "Terraform is not installed."
    if prompt_yesno "Install Terraform now?" "y"; then
        info "Adding HashiCorp GPG key..."
        wget -qO- https://apt.releases.hashicorp.com/gpg \
            | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/hashicorp.gpg 2>/dev/null
        if [[ $? -ne 0 ]]; then
            error "Failed to add GPG key"
            return 1
        fi

        info "Adding HashiCorp APT repository..."
        echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
            | sudo tee /etc/apt/sources.list.d/hashicorp.list >/dev/null

        info "Installing terraform..."
        sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq terraform
        if [[ $? -ne 0 ]]; then
            error "Failed to install terraform"
            return 1
        fi

        local tf_ver
        tf_ver=$(terraform version 2>/dev/null | head -1)
        success "Terraform installed ($tf_ver)"
    else
        echo -e "${DIM}Skipping Terraform installation — you can install it later.${NC}"
    fi
}

check_and_install_ansible() {
    echo
    echo -e "${CYAN}${BOLD}── Ansible ──${NC}"
    if command -v ansible &>/dev/null; then
        local ans_ver
        ans_ver=$(ansible --version 2>/dev/null | head -1)
        success "Ansible already installed ($ans_ver)"
        return 0
    fi

    warn "Ansible is not installed."
    if prompt_yesno "Install Ansible now?" "y"; then
        info "Installing ansible..."
        sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible
        if [[ $? -ne 0 ]]; then
            error "Failed to install ansible"
            return 1
        fi

        local ans_ver
        ans_ver=$(ansible --version 2>/dev/null | head -1)
        success "Ansible installed ($ans_ver)"
    else
        echo -e "${DIM}Skipping Ansible installation — you can install it later.${NC}"
    fi
}

generate_password() {
    # Generate a random password using available tools
    if command -v openssl &>/dev/null; then
        openssl rand -base64 18 | tr -d '/+=' | head -c 20
    else
        head -c 20 /dev/urandom | base64 | tr -d '/+=' | head -c 20
    fi
}

deploy_phpipam() {
    echo
    echo -e "${CYAN}${BOLD}── phpIPAM Configuration ──${NC}"
    echo

    # If phpIPAM is already configured, offer to keep existing setup
    if [[ -n "$PREV_IPAM_URL" ]]; then
        echo -e "  ${DIM}Existing phpIPAM: ${PREV_IPAM_URL}${NC}"
        if prompt_yesno "Keep existing phpIPAM configuration?" "y"; then
            IPAM_URL="$PREV_IPAM_URL"
            IPAM_APP_ID=$(cfg_get ipam app_id)
            IPAM_TOKEN=$(cfg_get ipam token)
            IPAM_USERNAME=$(cfg_get ipam username)
            IPAM_PASSWORD=$(cfg_get ipam password)
            IPAM_VERIFY_SSL=$(cfg_get ipam verify_ssl)
            [[ -z "$IPAM_VERIFY_SSL" ]] && IPAM_VERIFY_SSL="false"
            success "Keeping existing phpIPAM at ${IPAM_URL}"
            return
        fi
        echo
    fi

    if [[ "${SKIP_IPAM_CONFIRM:-}" != "true" ]]; then
        if ! prompt_yesno "Configure phpIPAM for IP address management?" "y"; then
            IPAM_URL=""
            IPAM_APP_ID=""
            IPAM_TOKEN=""
            IPAM_USERNAME=""
            IPAM_PASSWORD=""
            IPAM_VERIFY_SSL="false"
            return
        fi
    fi

    echo
    echo -e "${BOLD}phpIPAM setup method:${NC}"
    echo "  1) Connect to existing phpIPAM server (recommended)"
    echo "  2) Deploy new phpIPAM with Docker"
    echo "  3) Skip for now"
    read -rp "$(echo -e "${BOLD}Select [1]:${NC} ")" ipam_choice
    ipam_choice="${ipam_choice:-1}"

    case "$ipam_choice" in
        1)
            deploy_phpipam_existing
            ;;
        2)
            deploy_phpipam_docker
            ;;
        *)
            IPAM_URL=""
            IPAM_APP_ID=""
            IPAM_TOKEN=""
            IPAM_USERNAME=""
            IPAM_PASSWORD=""
            IPAM_VERIFY_SSL="false"
            ;;
    esac
}

deploy_phpipam_existing() {
    echo
    echo -e "${DIM}Enter your phpIPAM server details."
    echo -e "You'll need an API app configured in phpIPAM:"
    echo -e "  Administration > API > Create API app"
    echo -e "  Set app_id, permissions (Read/Write), and security method.${NC}"
    echo

    prompt_input "phpIPAM URL (e.g. https://ipam.example.com)" "${PREV_IPAM_URL}" IPAM_URL
    # Strip trailing slash
    IPAM_URL="${IPAM_URL%/}"

    prompt_input "API app ID" "$(cfg_get ipam app_id || echo 'infraforge')" IPAM_APP_ID

    echo
    echo -e "${BOLD}Authentication method:${NC}"
    echo "  1) API Token (app security = 'none' or 'ssl')"
    echo "  2) Username / Password (app security = 'user')"
    read -rp "$(echo -e "${BOLD}Select [1]:${NC} ")" auth_choice
    auth_choice="${auth_choice:-1}"

    IPAM_TOKEN=""
    IPAM_USERNAME=""
    IPAM_PASSWORD=""

    if [[ "$auth_choice" == "1" ]]; then
        local prev_token
        prev_token=$(cfg_get ipam token)
        if [[ -n "$prev_token" ]]; then
            local masked="${prev_token:0:4}...${prev_token: -4}"
            echo -e "  ${DIM}Current token: ${masked}${NC}"
            if prompt_yesno "  Keep existing token?" "y"; then
                IPAM_TOKEN="$prev_token"
            else
                prompt_secret "API Token" IPAM_TOKEN
            fi
        else
            echo -e "${DIM}Leave blank if app security is 'none'${NC}"
            prompt_input "API Token" "" IPAM_TOKEN
        fi
    else
        prompt_input "Username" "$(cfg_get ipam username || echo 'admin')" IPAM_USERNAME
        local prev_pw
        prev_pw=$(cfg_get ipam password)
        if [[ -n "$prev_pw" ]]; then
            echo -e "  ${DIM}Password is already set.${NC}"
            if prompt_yesno "  Keep existing password?" "y"; then
                IPAM_PASSWORD="$prev_pw"
            else
                prompt_secret "Password" IPAM_PASSWORD
            fi
        else
            prompt_secret "Password" IPAM_PASSWORD
        fi
    fi

    if prompt_yesno "Verify SSL certificate?" "n"; then
        IPAM_VERIFY_SSL="true"
    else
        IPAM_VERIFY_SSL="false"
    fi

    # Test connection
    if prompt_yesno "Test phpIPAM connection?" "y"; then
        info "Connecting to phpIPAM..."
        if $PYTHON_CMD -c "
from infraforge.config import Config, IPAMConfig
from infraforge.ipam_client import IPAMClient

cfg = Config()
cfg.ipam = IPAMConfig(
    provider='phpipam',
    url='${IPAM_URL}',
    app_id='${IPAM_APP_ID}',
    token='${IPAM_TOKEN}',
    username='${IPAM_USERNAME}',
    password='${IPAM_PASSWORD}',
    verify_ssl=${IPAM_VERIFY_SSL^},
)
client = IPAMClient(cfg)
if client.check_health():
    sections = client.get_sections()
    vlans = client.get_vlans()
    print(f'OK|Sections: {len(sections)} | VLANs: {len(vlans)}')
else:
    print('FAIL|Cannot reach phpIPAM API')
" 2>/dev/null; then
            local result
            result=$($PYTHON_CMD -c "
from infraforge.config import Config, IPAMConfig
from infraforge.ipam_client import IPAMClient
cfg = Config()
cfg.ipam = IPAMConfig(provider='phpipam', url='${IPAM_URL}', app_id='${IPAM_APP_ID}', token='${IPAM_TOKEN}', username='${IPAM_USERNAME}', password='${IPAM_PASSWORD}', verify_ssl=${IPAM_VERIFY_SSL^})
client = IPAMClient(cfg)
if client.check_health():
    sections = client.get_sections()
    vlans = client.get_vlans()
    print(f'OK|Sections: {len(sections)} | VLANs: {len(vlans)}')
else:
    print('FAIL|Cannot reach phpIPAM API')
" 2>/dev/null || echo "FAIL|Connection error")
            if [[ "$result" == OK* ]]; then
                local details="${result#OK|}"
                success "Connected to phpIPAM at ${IPAM_URL}"
                echo -e "  ${details}"
            else
                local details="${result#FAIL|}"
                error "${details}"
                warn "Check the URL, app ID, and credentials."
            fi
        else
            error "Connection test failed"
            warn "Check the URL, app ID, and credentials."
        fi
    fi
}

deploy_phpipam_docker() {
    echo
    echo -e "${DIM}A local Docker instance will be deployed automatically."
    echo -e "The database schema and API app are configured on first boot.${NC}"
    echo

    if ! $DOCKER_AVAILABLE; then
        echo
        if prompt_yesno "Connect to an existing phpIPAM server instead?" "y"; then
            deploy_phpipam_existing
            return
        fi
        IPAM_URL=""
        IPAM_APP_ID=""
        IPAM_TOKEN=""
        IPAM_USERNAME=""
        IPAM_PASSWORD=""
        IPAM_VERIFY_SSL="false"
        return
    fi

    # Detect existing broken deployment
    if detect_broken_phpipam; then
        warn "An existing phpIPAM deployment was detected but its database is not properly initialized."
        if prompt_yesno "Wipe and redeploy from scratch?" "y"; then
            info "Stopping containers and removing data volume..."
            $COMPOSE_CMD -f "$DOCKER_DIR/docker-compose.yml" down -v 2>/dev/null || true
            success "Old deployment removed"
        else
            if prompt_yesno "Connect to an existing phpIPAM server instead?" "n"; then
                deploy_phpipam_existing
                return
            fi
            IPAM_URL=""
            IPAM_APP_ID=""
            IPAM_TOKEN=""
            IPAM_USERNAME=""
            IPAM_PASSWORD=""
            IPAM_VERIFY_SSL="false"
            return
        fi
    fi

    # Detect existing port from docker .env
    local prev_port="8443"
    if [[ -f "$DOCKER_DIR/.env" ]]; then
        prev_port=$(grep -oP 'IPAM_PORT=\K.*' "$DOCKER_DIR/.env" 2>/dev/null || echo "8443")
    fi
    prompt_input "phpIPAM HTTPS port" "$prev_port" IPAM_PORT
    prompt_secret "phpIPAM admin password (default: admin)" IPAM_ADMIN_PASS
    IPAM_ADMIN_PASS="${IPAM_ADMIN_PASS:-admin}"

    DB_PASS="$(generate_password)"
    DB_ROOT_PASS="$(generate_password)"

    # Generate admin password hash
    info "Generating admin password hash..."
    ADMIN_HASH=$(generate_php_password_hash "$IPAM_ADMIN_PASS")

    # Generate SSL certs
    info "Generating self-signed SSL certificate..."
    bash "$DOCKER_DIR/phpipam/generate-ssl.sh"
    success "SSL certificate generated"

    # Write .env (includes admin hash for MariaDB init script)
    cat > "$DOCKER_DIR/.env" << EOF
IPAM_DB_ROOT_PASS=${DB_ROOT_PASS}
IPAM_DB_PASS=${DB_PASS}
IPAM_PORT=${IPAM_PORT}
SCAN_INTERVAL=15m
EOF
    if [[ -n "$ADMIN_HASH" ]]; then
        # Escape $ as $$ for docker compose .env variable interpolation
        local escaped_hash="${ADMIN_HASH//\$/\$\$}"
        echo "IPAM_ADMIN_HASH=${escaped_hash}" >> "$DOCKER_DIR/.env"
    fi

    # Launch containers
    echo
    info "Launching phpIPAM containers..."
    echo -e "${DIM}MariaDB will auto-initialize the schema on first boot.${NC}"
    if $COMPOSE_CMD -f "$DOCKER_DIR/docker-compose.yml" up -d 2>/dev/null; then
        success "Containers started"
    else
        error "Failed to start containers. Check: docker logs infraforge-ipam-web"
        IPAM_URL=""
        IPAM_APP_ID=""
        IPAM_TOKEN=""
        IPAM_USERNAME=""
        IPAM_PASSWORD=""
        IPAM_VERIFY_SSL="false"
        return
    fi

    IPAM_URL="https://localhost:${IPAM_PORT}"

    # Wait for phpIPAM to be ready (schema init may take 30-60s)
    info "Waiting for phpIPAM at ${IPAM_URL} (schema init may take 30-60s)..."
    READY=false
    for i in $(seq 1 60); do
        if curl -sk -o /dev/null -w "%{http_code}" "${IPAM_URL}" 2>/dev/null | grep -qE "^(200|301|302)$"; then
            sleep 5
            READY=true
            break
        fi
        sleep 3
    done

    if ! $READY; then
        error "phpIPAM did not become ready in time."
        warn "Check: docker logs infraforge-ipam-web"
        warn "Check: docker logs infraforge-ipam-db"
        IPAM_URL=""
        IPAM_APP_ID=""
        IPAM_TOKEN=""
        IPAM_USERNAME=""
        IPAM_PASSWORD=""
        IPAM_VERIFY_SSL="false"
        return
    fi
    success "phpIPAM is running"

    IPAM_APP_ID="infraforge"
    IPAM_TOKEN=""
    IPAM_USERNAME="Admin"
    IPAM_PASSWORD="$IPAM_ADMIN_PASS"
    IPAM_VERIFY_SSL="false"

    # Verify API connectivity
    info "Verifying API connectivity..."
    verify_ipam_api

    echo
    success "phpIPAM deployed at ${IPAM_URL}"
    echo -e "  ${DIM}Web UI: ${IPAM_URL}  (Admin / ${IPAM_ADMIN_PASS})${NC}"
}

generate_php_password_hash() {
    local password="$1"
    local escaped_pw
    escaped_pw=$(echo "$password" | sed "s/'/\\\\'/g")
    local php_code="echo password_hash('${escaped_pw}', PASSWORD_DEFAULT);"
    local hash=""

    # Try 1: Use the running phpipam-web container if available
    hash=$(docker exec infraforge-ipam-web php -r "$php_code" 2>/dev/null || echo "")
    if [[ "$hash" == \$2* ]]; then
        echo "$hash"
        return
    fi

    # Try 2: Use a throwaway PHP container
    hash=$(docker run --rm php:cli php -r "$php_code" 2>/dev/null || echo "")
    if [[ "$hash" == \$2* ]]; then
        echo "$hash"
        return
    fi

    # Try 3: Use the phpipam image
    hash=$(docker run --rm phpipam/phpipam-www:latest php -r "$php_code" 2>/dev/null || echo "")
    if [[ "$hash" == \$2* ]]; then
        echo "$hash"
        return
    fi

    warn "Could not generate password hash — admin password must be set via web UI"
    echo ""
}

detect_broken_phpipam() {
    # Check if DB container is running but schema is missing or API app not created
    local running
    running=$(docker inspect --format '{{.State.Running}}' infraforge-ipam-db 2>/dev/null || echo "false")
    if [[ "$running" != "true" ]]; then
        return 1
    fi

    # Read root password from .env
    local root_pass
    root_pass=$(grep -oP 'IPAM_DB_ROOT_PASS=\K.*' "$DOCKER_DIR/.env" 2>/dev/null || echo "infraforge_root_pw")

    # Check if settings table exists
    if ! docker exec infraforge-ipam-db mysql -u root -p"${root_pass}" phpipam -sN -e "SELECT COUNT(*) FROM settings;" 2>/dev/null | grep -q "[0-9]"; then
        return 0  # broken — no schema
    fi

    # Check if API app exists
    local api_count
    api_count=$(docker exec infraforge-ipam-db mysql -u root -p"${root_pass}" phpipam -sN -e "SELECT COUNT(*) FROM api WHERE app_id='infraforge';" 2>/dev/null || echo "0")
    if [[ "${api_count:-0}" -eq 0 ]]; then
        return 0  # broken — no API app
    fi

    return 1  # not broken
}

verify_ipam_api() {
    # Retry a few times — init scripts may still be running
    for attempt in $(seq 1 5); do
        local result
        result=$($PYTHON_CMD -c "
from infraforge.config import Config, IPAMConfig
from infraforge.ipam_client import IPAMClient
cfg = Config()
cfg.ipam = IPAMConfig(provider='phpipam', url='${IPAM_URL}', app_id='${IPAM_APP_ID}', token='', username='${IPAM_USERNAME}', password='${IPAM_PASSWORD}', verify_ssl=False)
client = IPAMClient(cfg)
if client.check_health():
    sections = client.get_sections()
    print(f'OK|Sections: {len(sections)}')
else:
    print('FAIL')
" 2>/dev/null || echo "FAIL")
        if [[ "$result" == OK* ]]; then
            local details="${result#OK|}"
            success "phpIPAM API is functional"
            echo -e "  ${DIM}${details}${NC}"
            return
        fi
        sleep 3
    done
    warn "API not responding yet — it may need a moment"
    echo -e "  ${DIM}You can test later with: infraforge setup${NC}"
}

write_config() {
    info "Writing configuration..."

    mkdir -p "$CONFIG_DIR"

    cat > "$CONFIG_FILE" << YAML
# InfraForge Configuration
# Generated by setup wizard on $(date -Iseconds)

proxmox:
  host: "${PVE_HOST}"
  port: ${PVE_PORT}
  user: "${PVE_USER}"
  auth_method: "${PVE_AUTH_METHOD}"
  token_name: "${PVE_TOKEN_NAME}"
  token_value: "${PVE_TOKEN_VALUE}"
  password: "${PVE_PASSWORD}"
  verify_ssl: ${PVE_VERIFY_SSL}

dns:
  provider: "${DNS_PROVIDER}"
  server: "${DNS_SERVER}"
  port: ${DNS_PORT:-53}
$(if [[ -z "$DNS_ZONES" ]]; then echo "  zones: []"; else echo "  zones:"; for z in $DNS_ZONES; do echo "    - \"$z\""; done; fi)
  domain: "${DNS_DOMAIN}"
  tsig_key_name: "${DNS_TSIG_KEY_NAME}"
  tsig_key_secret: "${DNS_TSIG_KEY_SECRET}"
  tsig_algorithm: "${DNS_TSIG_ALGORITHM:-hmac-sha256}"
  api_key: "${DNS_API_KEY}"

ipam:
  provider: "phpipam"
  url: "${IPAM_URL}"
  app_id: "${IPAM_APP_ID}"
  token: "${IPAM_TOKEN}"
  username: "${IPAM_USERNAME}"
  password: "${IPAM_PASSWORD}"
  verify_ssl: ${IPAM_VERIFY_SSL}

terraform:
  workspace: "./terraform"
  state_backend: "local"

ansible:
  playbook_dir: "./ansible/playbooks"

defaults:
  cpu_cores: 2
  memory_mb: 2048
  disk_gb: 20
  storage: "local-lvm"
  network_bridge: "vmbr0"
  os_type: "l26"
  start_on_create: true
YAML

    chmod 600 "$CONFIG_FILE"
    success "Configuration written to $CONFIG_FILE"
}

test_connection() {
    echo
    info "Testing Proxmox connection..."

    if $PYTHON_CMD -c "
from infraforge.config import Config
from infraforge.proxmox_client import ProxmoxClient
config = Config.load()
client = ProxmoxClient(config)
client.connect()
nodes = client.get_nodes()
print(f'Connected! Found {len(nodes)} node(s):')
for n in nodes:
    print(f'  - {n[\"node\"]} ({n.get(\"status\", \"unknown\")})')
" 2>/dev/null; then
        success "Connection test passed!"
    else
        warn "Connection test failed. Please verify your credentials and network."
        warn "You can edit the config at: $CONFIG_FILE"
    fi
}

configure_subnets() {
    # Only run if phpIPAM was deployed
    if [[ -z "${IPAM_URL:-}" ]]; then
        return
    fi

    echo
    echo -e "${CYAN}${BOLD}── Subnet Configuration ──${NC}"
    echo
    echo -e "${DIM}Add your IP subnets so InfraForge can allocate static IPs for new VMs."
    echo -e "You can add more subnets later via the phpIPAM web UI.${NC}"
    echo

    if ! prompt_yesno "Configure subnets now?" "y"; then
        return
    fi

    # Use Python/InfraForge client for subnet creation
    $PYTHON_CMD << 'PYEOF'
import sys
from infraforge.config import Config
from infraforge.ipam_client import IPAMClient

try:
    cfg = Config.load()
    client = IPAMClient(cfg)
    if not client.check_health():
        print("Cannot connect to phpIPAM API")
        sys.exit(1)
except Exception as e:
    print(f"Failed to connect to phpIPAM: {e}")
    sys.exit(1)

# Create or find InfraForge section
section = client.find_section_by_name("InfraForge")
if not section:
    try:
        result = client.create_section("InfraForge", "Managed by InfraForge")
        section_id = result if isinstance(result, (int, str)) else result.get("id", 1)
    except Exception:
        section_id = 1
else:
    section_id = section["id"]

print(f"Using IPAM section: InfraForge (id={section_id})")

import ipaddress

while True:
    cidr = input("\nSubnet CIDR (e.g. 10.0.7.0/24, blank to finish): ").strip()
    if not cidr:
        break

    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        print("Invalid CIDR notation. Try again.")
        continue

    desc = input(f"Description [{cidr}]: ").strip() or cidr
    vlan_input = input("VLAN ID (blank for none): ").strip()

    vlan_id = None
    if vlan_input:
        try:
            vlan_num = int(vlan_input)
            vlan = client.find_vlan_by_number(vlan_num)
            if vlan:
                vlan_id = vlan["vlanId"]
            else:
                vlan_name = input(f"VLAN name [VLAN {vlan_num}]: ").strip() or f"VLAN {vlan_num}"
                try:
                    result = client.create_vlan(vlan_num, vlan_name)
                    vlan_id = result if isinstance(result, (int, str)) else result.get("id")
                    print(f"  VLAN {vlan_num} created")
                except Exception as e:
                    print(f"  Warning: Could not create VLAN: {e}")
        except ValueError:
            print("Invalid VLAN ID, skipping.")

    try:
        client.create_subnet(
            subnet=str(net.network_address),
            mask=net.prefixlen,
            section_id=section_id,
            description=desc,
            vlan_id=vlan_id,
            ping_subnet=True,
            discover_subnet=True,
        )
        print(f"  Subnet {net.network_address}/{net.prefixlen} created with ping scanning enabled")
    except Exception as e:
        print(f"  Failed to create subnet: {e}")

    again = input("Add another subnet? [Y/n]: ").strip().lower()
    if again and not again.startswith("y"):
        break

print("\nSubnet configuration complete")
print("The phpIPAM cron container will begin scanning within 15 minutes.")
PYEOF
}

print_summary() {
    echo
    echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}  InfraForge setup complete!${NC}"
    echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
    echo
    echo -e "  Config:  ${BOLD}$CONFIG_FILE${NC}"

    if $USE_VENV; then
        echo -e "  Venv:    ${BOLD}$SCRIPT_DIR/.venv${NC}"
        echo
        echo -e "  ${BOLD}To run InfraForge:${NC}"
        echo -e "    source $SCRIPT_DIR/.venv/bin/activate"
        echo -e "    infraforge"
        echo
        echo -e "  ${BOLD}Or directly:${NC}"
        echo -e "    $SCRIPT_DIR/.venv/bin/infraforge"
    else
        echo
        echo -e "  ${BOLD}To run InfraForge:${NC}"
        echo -e "    infraforge"
        echo
        echo -e "  ${BOLD}Or:${NC}"
        echo -e "    python3 -m infraforge"
    fi

    if [[ -n "${IPAM_URL:-}" ]]; then
        echo
        echo -e "  ${BOLD}phpIPAM:${NC}"
        echo -e "    Web UI:  ${IPAM_URL}"
        echo -e "    Manage:  cd $DOCKER_DIR && $COMPOSE_CMD logs -f"
        echo -e "    Stop:    cd $DOCKER_DIR && $COMPOSE_CMD down"
        echo -e "    Restart: cd $DOCKER_DIR && $COMPOSE_CMD restart"
    fi

    echo
    echo -e "  ${BOLD}To reconfigure:${NC}"
    echo -e "    infraforge setup"
    echo -e "    or edit $CONFIG_FILE"
    echo
}

select_setup_mode() {
    SETUP_MODE="all"
    MISSING_SECTIONS=""

    if [[ ! -f "$CONFIG_FILE" ]]; then
        return
    fi

    # Detect which sections are configured
    local has_proxmox=false has_dns=false has_ipam=false
    local pve_host dns_provider ipam_url

    pve_host=$(cfg_get proxmox host)
    dns_provider=$(cfg_get dns provider)
    ipam_url=$(cfg_get ipam url)

    [[ -n "$pve_host" ]] && has_proxmox=true
    [[ -n "$dns_provider" ]] && has_dns=true
    [[ -n "$ipam_url" ]] && has_ipam=true

    echo
    echo -e "${BOLD}Current configuration:${NC}"
    if $has_proxmox; then
        echo -e "  ${GREEN}✓${NC} Proxmox (${pve_host})"
    else
        echo -e "  ${RED}✗${NC} Proxmox — not configured"
        MISSING_SECTIONS="${MISSING_SECTIONS} proxmox"
    fi
    if $has_dns; then
        echo -e "  ${GREEN}✓${NC} DNS (${dns_provider})"
    else
        echo -e "  ${RED}✗${NC} DNS — not configured"
        MISSING_SECTIONS="${MISSING_SECTIONS} dns"
    fi
    if $has_ipam; then
        echo -e "  ${GREEN}✓${NC} IPAM (${ipam_url})"
    else
        echo -e "  ${RED}✗${NC} IPAM — not configured"
        MISSING_SECTIONS="${MISSING_SECTIONS} ipam"
    fi

    echo
    echo -e "${BOLD}Setup mode:${NC}"
    echo "  1) Configure only missing settings (recommended)"
    echo "  2) Reconfigure all settings"
    read -rp "$(echo -e "${BOLD}Select [1]:${NC} ")" mode_choice
    mode_choice="${mode_choice:-1}"

    if [[ "$mode_choice" == "1" ]]; then
        SETUP_MODE="missing"
    fi
}

should_configure() {
    local section="$1"
    if [[ "$SETUP_MODE" == "all" ]]; then
        return 0
    fi
    # In "missing" mode, only configure sections that are missing
    echo "$MISSING_SECTIONS" | grep -qw "$section"
}

# ── Main ──

banner
check_python
setup_venv

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Launching InfraForge Setup Wizard...${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo ""

# Ensure textual is available (should be installed via requirements)
$PYTHON_CMD -m pip install textual -q 2>/dev/null || true

# Launch the TUI-based setup wizard
infraforge setup

echo ""
echo -e "${GREEN}${BOLD}Setup wizard complete!${NC}"
echo -e "Run ${BOLD}infraforge${NC} to start."
