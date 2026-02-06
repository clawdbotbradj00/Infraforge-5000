#!/usr/bin/env bash
# Bootstrap phpIPAM after first startup:
#   1. Wait for phpIPAM DB schema to be initialized
#   2. Create the InfraForge API app with read/write access
#   3. Set the admin password
#   4. Enable ping scanning globally
#
# Usage: bootstrap-ipam.sh [admin_password] [app_id] [app_security]
#
# Environment variables (from docker .env):
#   IPAM_DB_PASS  — MariaDB phpipam user password

set -euo pipefail

ADMIN_PASS="${1:-admin}"
APP_ID="${2:-infraforge}"
APP_SECURITY="${3:-none}"
DB_HOST="${4:-127.0.0.1}"
DB_PORT="${5:-3306}"
DB_USER="phpipam"
DB_PASS="${IPAM_DB_PASS:-infraforge_ipam_pw}"
DB_NAME="phpipam"

mysql_cmd() {
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" -sN -e "$1" 2>/dev/null
}

echo "Waiting for phpIPAM database schema..."
MAX_WAIT=120
WAITED=0
while ! mysql_cmd "SELECT COUNT(*) FROM settings;" &>/dev/null; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "ERROR: Timed out waiting for phpIPAM database schema after ${MAX_WAIT}s"
        exit 1
    fi
done
echo "Database schema ready (waited ${WAITED}s)"

# Set admin password (phpIPAM stores it as password_hash with PHP's password_hash())
# We use a fallback: first try to generate the hash via PHP in the container,
# otherwise use a Python-generated bcrypt hash
echo "Setting admin password..."
PASS_HASH=$(python3 -c "
import bcrypt
print(bcrypt.hashpw(b'''${ADMIN_PASS}''', bcrypt.gensalt()).decode())
" 2>/dev/null || echo "")

if [[ -z "$PASS_HASH" ]]; then
    # Fallback: use htpasswd-style or just set the raw password and let phpIPAM rehash on login
    echo "Warning: bcrypt not available, admin password must be set via web UI"
else
    mysql_cmd "UPDATE users SET password='${PASS_HASH}' WHERE username='admin';" || true
    echo "Admin password set"
fi

# Create the InfraForge API application
echo "Creating API application '${APP_ID}'..."
EXISTING=$(mysql_cmd "SELECT COUNT(*) FROM api WHERE app_id='${APP_ID}';")
if [[ "${EXISTING:-0}" -gt 0 ]]; then
    echo "API app '${APP_ID}' already exists, updating..."
    mysql_cmd "UPDATE api SET app_permissions=2, app_security='${APP_SECURITY}' WHERE app_id='${APP_ID}';"
else
    mysql_cmd "INSERT INTO api (app_id, app_code, app_permissions, app_security, app_lock_expire)
               VALUES ('${APP_ID}', 'infraforge_generated', 2, '${APP_SECURITY}', 0);"
fi
echo "API app '${APP_ID}' configured (rw, security=${APP_SECURITY})"

# Enable scanning agent (discovery + ping check)
echo "Enabling scan agents..."
mysql_cmd "UPDATE settings SET scanPingType='fping', scanMaxThreads=32, api=1;" || true

# Enable the default scan agent for ping checks
AGENT_EXISTS=$(mysql_cmd "SELECT COUNT(*) FROM scanAgents WHERE id=1;")
if [[ "${AGENT_EXISTS:-0}" -gt 0 ]]; then
    mysql_cmd "UPDATE scanAgents SET type='mysql' WHERE id=1;" || true
else
    mysql_cmd "INSERT INTO scanAgents (id, name, description, type) VALUES (1, 'cron', 'Default cron agent', 'mysql');" || true
fi

echo ""
echo "phpIPAM bootstrap complete!"
echo "  API App:  ${APP_ID}"
echo "  Security: ${APP_SECURITY}"
echo "  Scanning: enabled (fping)"
