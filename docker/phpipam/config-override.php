<?php
/**
 * InfraForge phpIPAM configuration.
 * Mounted as /phpipam/config.docker.php (config.php symlinks to it).
 *
 * Loads defaults from config.dist.php, applies Docker env var overrides,
 * and adds InfraForge-specific settings.
 */

// Helper: read env var or file-based secret, with fallback default
function file_env($var, $default) {
    $env_filename = getenv($var.'_FILE');
    if ($env_filename === false) {
        return getenv($var) ?: $default;
    } elseif (is_readable($env_filename)) {
        return trim(file_get_contents($env_filename), "\n\r");
    } else {
        error_log("$var:$env_filename can not be read");
        return $default;
    }
}

// Load phpIPAM defaults
require('config.dist.php');

// Override: read BASE path from env
getenv('IPAM_BASE') ? define('BASE', getenv('IPAM_BASE')) : false;

// Override: disable installer (schema loaded by MariaDB init scripts)
$disable_installer = filter_var(file_env('IPAM_DISABLE_INSTALLER', $disable_installer), FILTER_VALIDATE_BOOLEAN);

// Override: database connection from Docker env vars
$db['host']    = file_env('IPAM_DATABASE_HOST',    $db['host']);
$db['user']    = file_env('IPAM_DATABASE_USER',    $db['user']);
$db['pass']    = file_env('IPAM_DATABASE_PASS',    $db['pass']);
$db['name']    = file_env('IPAM_DATABASE_NAME',    $db['name']);
$db['port']    = file_env('IPAM_DATABASE_PORT',    $db['port']);
$db['webhost'] = file_env('IPAM_DATABASE_WEBHOST', $db['webhost']);

// Override: reverse proxy headers
$trust_x_forwarded_headers = filter_var(file_env('IPAM_TRUST_X_FORWARDED', $trust_x_forwarded_headers), FILTER_VALIDATE_BOOLEAN);

// Override: proxy settings
$proxy_enabled  = file_env('PROXY_ENABLED',  $proxy_enabled);
$proxy_server   = file_env('PROXY_SERVER',   $proxy_server);
$proxy_port     = file_env('PROXY_PORT',     $proxy_port);
$proxy_user     = file_env('PROXY_USER',     $proxy_user);
$proxy_pass     = file_env('PROXY_PASS',     $proxy_pass);
$proxy_use_auth = file_env('PROXY_USE_AUTH', $proxy_use_auth);

$offline_mode = filter_var(file_env('OFFLINE_MODE', $offline_mode), FILTER_VALIDATE_BOOLEAN);

// Override: debugging
$debugging = filter_var(file_env('IPAM_DEBUG', $debugging), FILTER_VALIDATE_BOOLEAN);

// Override: cookie settings
$cookie_samesite = file_env('COOKIE_SAMESITE', $cookie_samesite);

// Use database session storage
$session_storage = "database";

// Footer
$config['footer_message'] = file_env('IPAM_FOOTER_MESSAGE', $config['footer_message']);

// ─── InfraForge-specific settings ───────────────────────────────────

// Allow API access over HTTP (non-SSL) connections.
// The InfraForge Docker stack serves phpIPAM over SSL (port 8443) by
// default, but users behind a reverse proxy that terminates TLS may
// appear as plain HTTP to phpIPAM.  With ssl_token security mode the
// API still requires user-token auth regardless of this flag.
$api_allow_unsafe = true;
