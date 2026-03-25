#!/bin/sh
set -e

# OpenHost mounts persistent storage at OPENHOST_APP_DATA_DIR.
# Synapse expects all data under /data (config, media, SQLite, signing keys).
# We symlink /data into the persistent directory so data survives container restarts.
PERSIST="${OPENHOST_APP_DATA_DIR:-/data}"

# If OpenHost provides a different data dir, symlink /data to it
if [ "$PERSIST" != "/data" ]; then
    # Move any existing /data contents into the persistent dir
    if [ -d /data ] && [ "$(ls -A /data 2>/dev/null)" ]; then
        cp -a /data/* "$PERSIST/" 2>/dev/null || true
    fi
    rm -rf /data
    ln -sf "$PERSIST" /data
else
    mkdir -p /data
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

# Generate config on first boot if homeserver.yaml doesn't exist
if [ ! -f /data/homeserver.yaml ]; then
    echo "First boot: generating Synapse config for server name: $SERVER_NAME"
    /start.py generate
    
    # Patch the generated config with OpenHost-friendly defaults
    # - Set public_baseurl so links/redirects work correctly
    # - Enable registration (first user to register becomes admin-capable)
    # - Listen on all interfaces
    cat >> /data/homeserver.yaml <<EOF

# OpenHost overrides
public_baseurl: "$PUBLIC_BASEURL"
enable_registration: true
enable_registration_without_verification: true
suppress_key_server_warning: true
EOF
else
    # Update public_baseurl on restart (domain may change between dev/prod)
    if grep -q "^public_baseurl:" /data/homeserver.yaml; then
        sed -i "s|^public_baseurl:.*|public_baseurl: \"$PUBLIC_BASEURL\"|" /data/homeserver.yaml
    fi
fi

# Fix ownership for the synapse user (UID 991)
chown -R 991:991 /data 2>/dev/null || true

# Start Caddy in background — it rewrites Host from X-Forwarded-Host on
# port 3000, then proxies to Synapse on port 8008.
caddy run --config /app/Caddyfile &

# Hand off to the official Synapse entrypoint
exec /start.py
