#!/bin/sh
set -e

# OpenHost mounts persistent storage at OPENHOST_APP_DATA_DIR.
# Synapse expects all data under /data (config, media, SQLite, signing keys).
PERSIST="${OPENHOST_APP_DATA_DIR:-/data}"

# Ensure /data exists and points to persistent storage.
# Unlike forgejo (which symlinks subdirs), Synapse's start.py hardcodes /data
# as the config/data root, so we need /data itself to be the persistent dir.
if [ "$PERSIST" != "/data" ]; then
    # Symlink /data -> persistent storage so Synapse writes there
    rm -rf /data 2>/dev/null || true
    ln -sfn "$PERSIST" /data
else
    mkdir -p /data
fi

# Verify /data is accessible
if [ ! -d /data ]; then
    echo "ERROR: /data is not a directory (OPENHOST_APP_DATA_DIR=$PERSIST)"
    ls -la /data 2>&1 || true
    exit 1
fi

# Derive server name and public URL from OpenHost environment variables
if [ -n "$OPENHOST_ZONE_DOMAIN" ]; then
    APP_SUBDOMAIN="${OPENHOST_APP_NAME:-synapse}"
    SERVER_NAME="${APP_SUBDOMAIN}.${OPENHOST_ZONE_DOMAIN}"

    case "$OPENHOST_ZONE_DOMAIN" in
        lvh.me|*.lvh.me|localhost|*.localhost)
            # Dev environment — use http with the router's external port
            ROUTER_PORT=""
            if [ -n "$OPENHOST_ROUTER_URL" ]; then
                ROUTER_PORT=$(echo "$OPENHOST_ROUTER_URL" | sed -n 's/.*:\([0-9]*\)$/\1/p')
            fi
            PUBLIC_BASEURL="http://${SERVER_NAME}${ROUTER_PORT:+:$ROUTER_PORT}/"
            ;;
        *)
            # Production — HTTPS on standard port
            PUBLIC_BASEURL="https://${SERVER_NAME}/"
            ;;
    esac
else
    SERVER_NAME="${SYNAPSE_SERVER_NAME:-localhost}"
    PUBLIC_BASEURL="http://localhost:3000/"
fi

export SYNAPSE_SERVER_NAME="$SERVER_NAME"
export SYNAPSE_REPORT_STATS="no"

echo "Synapse starting: server_name=$SERVER_NAME public_baseurl=$PUBLIC_BASEURL data_dir=$PERSIST"

# Generate config on first boot if homeserver.yaml doesn't exist
if [ ! -f /data/homeserver.yaml ]; then
    echo "First boot: generating Synapse config for server name: $SERVER_NAME"

    # Ensure ownership before generate (it runs as uid 991 via gosu)
    chown -R 991:991 /data 2>/dev/null || true

    /start.py generate

    # Patch the generated config with OpenHost-friendly defaults.
    cat >> /data/homeserver.yaml <<EOF

# OpenHost overrides
public_baseurl: "$PUBLIC_BASEURL"
enable_registration: true
enable_registration_without_verification: true
suppress_key_server_warning: true
EOF

    echo "Config generated successfully"
else
    echo "Existing config found, updating public_baseurl"
    # Update public_baseurl on restart (domain may change between dev/prod)
    if grep -q "^public_baseurl:" /data/homeserver.yaml; then
        sed -i "s|^public_baseurl:.*|public_baseurl: \"$PUBLIC_BASEURL\"|" /data/homeserver.yaml
    fi
fi

# Ensure the listener serves federation (not just client) on every boot.
# Synapse's generated config uses "client, federation" by default, but
# verify it's there so federation works even on existing deployments.
if ! grep -q "federation" /data/homeserver.yaml; then
    echo "WARNING: federation not found in listeners config, adding it"
    sed -i 's/\- names: \[client\]/- names: [client, federation]/' /data/homeserver.yaml
fi

# Always ensure relaxed rate limits (small personal server)
if ! grep -q "^rc_login:" /data/homeserver.yaml; then
    cat >> /data/homeserver.yaml <<EOF

# Relaxed rate limits for personal server
rc_login:
  address:
    per_second: 10
    burst_count: 50
  account:
    per_second: 10
    burst_count: 50
  failed_attempts:
    per_second: 10
    burst_count: 50
EOF
fi

# Generate Caddyfile from template with .well-known responses for federation.
# Caddy serves these directly so other homeservers can discover this server.
sed -e "s|SERVER_NAME_PLACEHOLDER|${SERVER_NAME}|g" \
    -e "s|PUBLIC_BASEURL_PLACEHOLDER|${PUBLIC_BASEURL}|g" \
    /app/Caddyfile.template > /app/Caddyfile
echo "well-known: server=${SERVER_NAME}:443 client_base=${PUBLIC_BASEURL}"

# Fix ownership for the synapse user (UID 991)
chown -R 991:991 /data 2>/dev/null || true

# Start Caddy in background — it serves .well-known, rewrites Host from
# X-Forwarded-Host, and proxies to Synapse on port 8008.
caddy run --config /app/Caddyfile &
CADDY_PID=$!
echo "Caddy started (PID $CADDY_PID)"

# Hand off to the official Synapse entrypoint
echo "Starting Synapse..."
exec /start.py
