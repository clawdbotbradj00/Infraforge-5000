#!/usr/bin/env bash
# InfraForge phpIPAM bootstrap — runs inside MariaDB container on first boot
# via /docker-entrypoint-initdb.d/ mechanism.
#
# This script configures phpIPAM for headless use with InfraForge:
#   1. Enables the REST API
#   2. Creates the "infraforge" API application (rw, no auth)
#   3. Configures fping scanning
#   4. Creates the default cron scan agent
#   5. Sets admin password (if IPAM_ADMIN_HASH env var is provided)

set -euo pipefail

DB="${MYSQL_DATABASE:-phpipam}"
ROOT_PASS="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}"

run_sql() {
    if [[ -n "$ROOT_PASS" ]]; then
        mysql --protocol=socket -uroot -p"$ROOT_PASS" "$DB" -e "$1"
    else
        mysql --protocol=socket -uroot "$DB" -e "$1"
    fi
}

echo "[infraforge] Bootstrapping phpIPAM for InfraForge..."

# 1. Enable REST API + configure scanning
run_sql "UPDATE settings SET api=1, scanPingType='fping', scanMaxThreads=32 WHERE id=1;"
echo "[infraforge] API enabled, scan type set to fping"

# 2. Create InfraForge API application (read/write, no auth required)
run_sql "INSERT INTO api (app_id, app_code, app_permissions, app_security, app_lock)
         VALUES ('infraforge', 'infraforge_auto', 2, 'none', 0);"
echo "[infraforge] API app 'infraforge' created (rw, security=none)"

# 3. Set scan agent to mysql type (for cron-based scanning)
run_sql "UPDATE scanAgents SET type='mysql' WHERE id=1;"
echo "[infraforge] Scan agent configured for cron (mysql type)"

# 4. Set admin password if hash is provided
if [[ -n "${IPAM_ADMIN_HASH:-}" ]]; then
    # Escape single quotes in hash for SQL safety
    ESCAPED_HASH="${IPAM_ADMIN_HASH//\'/\'\'}"
    run_sql "UPDATE users SET password='${ESCAPED_HASH}', passChange='No' WHERE username='Admin';"
    echo "[infraforge] Admin password set"
else
    echo "[infraforge] No IPAM_ADMIN_HASH provided — keeping default admin password (ipamadmin)"
fi

echo "[infraforge] Bootstrap complete!"
