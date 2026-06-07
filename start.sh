#!/bin/sh
set -e

# OpenHost mounts persistent storage at OPENHOST_APP_DATA_DIR.
# Inside the container this is /data/app_data/<app_name>, NOT /data itself.
# Synapse's start.py defaults to /data for config, keys, and the SQLite DB.
# We tell Synapse to use the persistent directory via SYNAPSE_CONFIG_DIR and
# SYNAPSE_CONFIG_PATH, and redirect any hardcoded /data references via symlinks.
DATA_DIR="${OPENHOST_APP_DATA_DIR:-/data}"

# Point Synapse's config/data dirs at persistent storage so homeserver.yaml,
# signing keys, media_store, and the SQLite DB all land on the volume.
export SYNAPSE_CONFIG_DIR="$DATA_DIR"
export SYNAPSE_CONFIG_PATH="$DATA_DIR/homeserver.yaml"
export SYNAPSE_DATA_DIR="$DATA_DIR"

SETTINGS_FILE="$DATA_DIR/openhost_settings.json"

mkdir -p "$DATA_DIR"

# ---------------------------------------------------------------------------
# openhost_settings.json — source of truth for admin-controlled toggles.
# Written once on first boot; thereafter managed by the admin UI.
# ---------------------------------------------------------------------------
if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<'EOF'
{
  "federation_enabled": false,
  "open_registration": true
}
EOF
    echo "Created default settings: $SETTINGS_FILE"
fi

# Read current settings (use python3 which is available in the Synapse image)
FEDERATION_ENABLED=$(python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE') as f:
        d = json.load(f)
    print('true' if d.get('federation_enabled', False) else 'false')
except Exception as e:
    sys.stderr.write('Warning: could not read settings file: ' + str(e) + '\n')
    print('false')
")
OPEN_REGISTRATION=$(python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE') as f:
        d = json.load(f)
    print('true' if d.get('open_registration', True) else 'false')
except Exception as e:
    sys.stderr.write('Warning: could not read settings file: ' + str(e) + '\n')
    print('true')
")

echo "Settings: federation_enabled=$FEDERATION_ENABLED open_registration=$OPEN_REGISTRATION"

# Synapse's start.py hardcodes a few paths under /data (secret key files,
# appservices glob).  If persistent storage is elsewhere, symlink individual
# items so those hardcoded reads/writes hit the persistent directory.
if [ "$DATA_DIR" != "/data" ]; then
    # Migrate any existing data that landed on the ephemeral /data to
    # persistent storage (one-time fix for prior broken deployments).
    for f in /data/homeserver.yaml /data/*.signing.key /data/*.key /data/*.log.config; do
        [ -e "$f" ] || continue
        base="$(basename "$f")"
        if [ ! -e "$DATA_DIR/$base" ]; then
            echo "Migrating $f -> $DATA_DIR/$base"
            cp -a "$f" "$DATA_DIR/$base"
        fi
    done
    for d in /data/media_store /data/uploads; do
        [ -d "$d" ] || continue
        base="$(basename "$d")"
        if [ ! -e "$DATA_DIR/$base" ]; then
            echo "Migrating $d -> $DATA_DIR/$base"
            cp -a "$d" "$DATA_DIR/$base"
        fi
    done

    # Symlink /data items -> persistent dir so hardcoded /data/<file> paths
    # resolve correctly.  We can't replace /data itself (it contains the
    # bind-mount at /data/app_data), so we link individual entries.
    for f in "$DATA_DIR"/*; do
        [ -e "$f" ] || continue
        base="$(basename "$f")"
        target="/data/$base"
        # Don't clobber the app_data mount point
        [ "$base" = "app_data" ] && continue
        if [ ! -L "$target" ]; then
            rm -rf "$target" 2>/dev/null || true
            ln -sfn "$f" "$target"
        fi
    done
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

# Tell upstream start.py to run Synapse as UID/GID 1000 (host user) instead
# of the default 991, so persistent data ownership stays consistent.
export UID=1000
export GID=1000

echo "Synapse starting: server_name=$SERVER_NAME public_baseurl=$PUBLIC_BASEURL data_dir=$DATA_DIR"

# Generate config on first boot if homeserver.yaml doesn't exist
if [ ! -f "$DATA_DIR/homeserver.yaml" ]; then
    echo "First boot: generating Synapse config for server name: $SERVER_NAME"

    # Ensure ownership before generate (it runs as uid 1000 via gosu)
    chown -R 1000:1000 "$DATA_DIR" 2>/dev/null || true

    /start.py generate

    # The generate command may write keys to /data/ (hardcoded).
    # Move them to the persistent dir if they landed there.
    if [ "$DATA_DIR" != "/data" ]; then
        for f in /data/*.key /data/*.log.config /data/homeserver.yaml; do
            [ -e "$f" ] || continue
            base="$(basename "$f")"
            if [ ! -e "$DATA_DIR/$base" ]; then
                mv "$f" "$DATA_DIR/$base"
            fi
            # Symlink so /data/<file> still resolves
            [ -L "/data/$base" ] || ln -sfn "$DATA_DIR/$base" "/data/$base"
        done
    fi

    # Patch the generated config with OpenHost-friendly defaults.
    # Also override paths so Synapse reads/writes the persistent dir.
    cat >> "$DATA_DIR/homeserver.yaml" <<EOF

# OpenHost overrides
public_baseurl: "$PUBLIC_BASEURL"
suppress_key_server_warning: true
media_store_path: "$DATA_DIR/media_store"
EOF

    echo "Config generated successfully"
else
    echo "Existing config found, updating public_baseurl and media_store_path"
    # Update public_baseurl on restart (domain may change between dev/prod)
    if grep -q "^public_baseurl:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^public_baseurl:.*|public_baseurl: \"$PUBLIC_BASEURL\"|" "$DATA_DIR/homeserver.yaml"
    fi
    # Ensure media_store_path points to persistent storage
    if grep -q "^media_store_path:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^media_store_path:.*|media_store_path: \"$DATA_DIR/media_store\"|" "$DATA_DIR/homeserver.yaml"
    elif ! grep -q "media_store_path:" "$DATA_DIR/homeserver.yaml"; then
        echo "media_store_path: \"$DATA_DIR/media_store\"" >> "$DATA_DIR/homeserver.yaml"
    fi
fi

# ---------------------------------------------------------------------------
# Apply federation setting from openhost_settings.json
# ---------------------------------------------------------------------------
if [ "$FEDERATION_ENABLED" = "true" ]; then
    # Re-enable federation listener if it was disabled
    if grep -q "- names: \[client\]" "$DATA_DIR/homeserver.yaml"; then
        echo "Enabling federation listener"
        sed -i 's/- names: \[client\]/- names: [client, federation]/' "$DATA_DIR/homeserver.yaml"
    fi
    # Remove federation_domain_whitelist restriction
    sed -i '/^# Federation disabled/d' "$DATA_DIR/homeserver.yaml"
    sed -i '/^federation_domain_whitelist: \[\]/d' "$DATA_DIR/homeserver.yaml"
else
    # Disable federation listener
    if grep -q "client, federation" "$DATA_DIR/homeserver.yaml"; then
        echo "Disabling federation listener"
        sed -i 's/\- names: \[client, federation\]/- names: [client]/' "$DATA_DIR/homeserver.yaml"
    fi
    # Block all federation with an empty domain whitelist
    if ! grep -q "^federation_domain_whitelist:" "$DATA_DIR/homeserver.yaml"; then
        cat >> "$DATA_DIR/homeserver.yaml" <<EOF

# Federation disabled — personal server, no need for cross-server communication.
federation_domain_whitelist: []
EOF
    fi
fi

# ---------------------------------------------------------------------------
# Apply registration setting from openhost_settings.json
# ---------------------------------------------------------------------------
if [ "$OPEN_REGISTRATION" = "true" ]; then
    if grep -q "^enable_registration:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^enable_registration:.*|enable_registration: true|" "$DATA_DIR/homeserver.yaml"
    else
        echo "enable_registration: true" >> "$DATA_DIR/homeserver.yaml"
    fi
    if grep -q "^enable_registration_without_verification:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^enable_registration_without_verification:.*|enable_registration_without_verification: true|" "$DATA_DIR/homeserver.yaml"
    else
        echo "enable_registration_without_verification: true" >> "$DATA_DIR/homeserver.yaml"
    fi
else
    if grep -q "^enable_registration:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^enable_registration:.*|enable_registration: false|" "$DATA_DIR/homeserver.yaml"
    else
        echo "enable_registration: false" >> "$DATA_DIR/homeserver.yaml"
    fi
    if grep -q "^enable_registration_without_verification:" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|^enable_registration_without_verification:.*|enable_registration_without_verification: false|" "$DATA_DIR/homeserver.yaml"
    else
        echo "enable_registration_without_verification: false" >> "$DATA_DIR/homeserver.yaml"
    fi
fi

# Always ensure relaxed rate limits (small personal server)
if ! grep -q "^rc_login:" "$DATA_DIR/homeserver.yaml"; then
    cat >> "$DATA_DIR/homeserver.yaml" <<EOF

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

# Ensure the SQLite database path points to persistent storage.
# Synapse defaults to /data/homeserver.db — redirect it.
if grep -q "^database:" "$DATA_DIR/homeserver.yaml"; then
    if grep -q "/data/homeserver.db" "$DATA_DIR/homeserver.yaml"; then
        sed -i "s|/data/homeserver.db|$DATA_DIR/homeserver.db|g" "$DATA_DIR/homeserver.yaml"
    fi
fi

# Generate Caddyfile from template with .well-known client discovery.
sed -e "s|SERVER_NAME_PLACEHOLDER|${SERVER_NAME}|g" \
    -e "s|PUBLIC_BASEURL_PLACEHOLDER|${PUBLIC_BASEURL}|g" \
    /app/Caddyfile.template > /app/Caddyfile
echo "well-known: client_base=${PUBLIC_BASEURL}"

# Fix ownership for the host user (UID 1000)
chown -R 1000:1000 "$DATA_DIR" 2>/dev/null || true

# Start Caddy in background — it serves .well-known, rewrites Host from
# X-Forwarded-Host, and proxies to Synapse on port 8008.
caddy run --config /app/Caddyfile &
CADDY_PID=$!
echo "Caddy started (PID $CADDY_PID)"

# Start the admin UI in background
OPENHOST_APP_DATA_DIR="$DATA_DIR" python3 /app/admin.py &
ADMIN_PID=$!
echo "Admin UI started (PID $ADMIN_PID)"

# Hand off to the official Synapse entrypoint
echo "Starting Synapse..."
exec /start.py
