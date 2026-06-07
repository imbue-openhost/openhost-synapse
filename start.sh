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
ADMIN_TOKEN_FILE="$DATA_DIR/admin_token"

mkdir -p "$DATA_DIR"

# ---------------------------------------------------------------------------
# openhost_settings.json — source of truth for admin-controlled toggles.
# Written once on first boot; thereafter managed by the admin UI.
# ---------------------------------------------------------------------------
if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<'EOF'
{
  "federation_enabled": false,
  "open_registration": true,
  "rc_login_per_second": 10,
  "rc_login_burst": 50,
  "max_upload_size_mb": 50,
  "password_min_length": 8,
  "password_require_digit": false,
  "password_require_symbol": false,
  "allow_public_rooms": true
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

# Symlink /data items -> persistent dir so hardcoded /data/<file> paths resolve correctly.
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
# Apply settings from openhost_settings.json to homeserver.yaml
# ---------------------------------------------------------------------------
python3 << PYEOF
import re, sys, json

settings_path = "$SETTINGS_FILE"
yaml_path = "$DATA_DIR/homeserver.yaml"

# Load settings
try:
    with open(settings_path) as f:
        settings = json.load(f)
except Exception as e:
    sys.stderr.write(f"Warning: could not read settings: {e}\n")
    settings = {}

federation_enabled = settings.get("federation_enabled", False)
open_registration = settings.get("open_registration", True)
allow_public_rooms = settings.get("allow_public_rooms", True)
max_upload_mb = settings.get("max_upload_size_mb", 50)
pw_min_len = settings.get("password_min_length", 8)
pw_require_digit = settings.get("password_require_digit", False)
pw_require_symbol = settings.get("password_require_symbol", False)
rc_per_sec = settings.get("rc_login_per_second", 10)
rc_burst = settings.get("rc_login_burst", 50)

try:
    content = open(yaml_path).read()
except OSError as e:
    sys.stderr.write(f"Error: could not read homeserver.yaml: {e}\n")
    sys.exit(1)

def set_yaml_bool(content, key, value):
    val = "true" if value else "false"
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {val}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    return content.rstrip() + f"\n{replacement}\n"

def set_yaml_value(content, key, value):
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {value}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    return content.rstrip() + f"\n{replacement}\n"

# Registration
content = set_yaml_bool(content, "enable_registration", open_registration)
content = set_yaml_bool(content, "enable_registration_without_verification", open_registration)

# Public rooms
content = set_yaml_bool(content, "allow_public_rooms_without_auth", allow_public_rooms)
content = set_yaml_bool(content, "allow_public_rooms_over_federation", federation_enabled and allow_public_rooms)

# Upload size
content = set_yaml_value(content, "max_upload_size", f"{max_upload_mb}M")

# Federation listener
def patch_federation_listener(content, enabled):
    def replace_names(m):
        prefix = m.group(1)
        if enabled:
            return prefix + "[client, federation]"
        return prefix + "[client]"
    pattern = re.compile(r'((?:-\s+)?names:\s*\[)client(?:,\s*federation)?\]')
    return pattern.sub(replace_names, content)

content = patch_federation_listener(content, federation_enabled)

# Federation domain whitelist
content = re.sub(r'\n# Federation disabled[^\n]*\n', '\n', content)
content = re.sub(r'(?m)^federation_domain_whitelist:.*$', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)
if not federation_enabled:
    content = content.rstrip() + "\n\n# Federation disabled — personal server.\nfederation_domain_whitelist: []\n"

# Password policy
content = re.sub(r'(?ms)^password_config:.*?(?=\n\S|\Z)', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)
password_block = f"""password_config:
  minimum_length: {pw_min_len}
  require_digit: {'true' if pw_require_digit else 'false'}
  require_punctuation: {'true' if pw_require_symbol else 'false'}
"""
content = content.rstrip() + "\n" + password_block

# Rate limits — remove existing rc_login block and rewrite
content = re.sub(r'(?ms)^rc_login:\n(?:[ \t]+.*\n)*', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)
rc_block = f"""rc_login:
  address:
    per_second: {rc_per_sec}
    burst_count: {rc_burst}
  account:
    per_second: {rc_per_sec}
    burst_count: {rc_burst}
  failed_attempts:
    per_second: {rc_per_sec}
    burst_count: {rc_burst}
"""
content = content.rstrip() + "\n" + rc_block

open(yaml_path, "w").write(content)
print(f"Applied settings: federation={federation_enabled} registration={open_registration}")
PYEOF

# Ensure the SQLite database path points to persistent storage.
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

# ---------------------------------------------------------------------------
# Start Caddy (serves /, /_openhost/*, .well-known, proxies Matrix to Synapse)
# ---------------------------------------------------------------------------
caddy run --config /app/Caddyfile &
CADDY_PID=$!
echo "Caddy started (PID $CADDY_PID)"

# ---------------------------------------------------------------------------
# Start Go admin server
# The admin server reads/writes DATA_DIR and sends SIGHUP to Synapse.
# It also handles /_openhost/admin/* and / (which Caddy proxies to it).
# ---------------------------------------------------------------------------
OPENHOST_APP_DATA_DIR="$DATA_DIR" /app/admin-server &
ADMIN_PID=$!
echo "Admin server started (PID $ADMIN_PID)"

# ---------------------------------------------------------------------------
# Wait for Synapse to start, then provision an admin token for the admin UI.
# This is done after Synapse is running so register_new_matrix_user works.
# ---------------------------------------------------------------------------
provision_admin_token() {
    echo "Waiting for Synapse to become ready..."
    ATTEMPTS=0
    while [ $ATTEMPTS -lt 60 ]; do
        if curl -sf http://127.0.0.1:8008/health > /dev/null 2>&1; then
            break
        fi
        sleep 2
        ATTEMPTS=$((ATTEMPTS + 1))
    done

    if [ ! -f "$ADMIN_TOKEN_FILE" ] || [ ! -s "$ADMIN_TOKEN_FILE" ]; then
        echo "Provisioning admin token..."
        ADMIN_PASS=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c "import uuid; print(str(uuid.uuid4()))")

        if [ -z "$ADMIN_PASS" ]; then
            echo "ERROR: could not generate admin password"
            return 1
        fi

        # Try to register the admin user — pass password via environment to avoid
        # exposing it in /proc/<pid>/cmdline.
        ADMIN_PASS="$ADMIN_PASS" DATA_DIR="$DATA_DIR" python3 << 'REGPY'
import os, subprocess, sys
pw = os.environ['ADMIN_PASS']
data_dir = os.environ['DATA_DIR']
result = subprocess.run(
    ['register_new_matrix_user',
     '-c', data_dir + '/homeserver.yaml',
     '-u', 'openhost-admin',
     '-p', pw,
     '--admin',
     'http://127.0.0.1:8008'],
    capture_output=True, text=True
)
print(result.stdout, end='')
if result.returncode != 0:
    sys.stderr.write(result.stderr + '\n')
REGPY
        # Log in to get an access token — pass password via env
        TOKEN=$(ADMIN_PASS="$ADMIN_PASS" python3 << 'LOGINPY'
import json, os, urllib.request, sys
try:
    pw = os.environ['ADMIN_PASS']
    payload = json.dumps({
        'type': 'm.login.password',
        'user': 'openhost-admin',
        'password': pw,
    }).encode()
    req = urllib.request.Request(
        'http://127.0.0.1:8008/_matrix/client/v3/login',
        data=payload,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        print(data.get('access_token', ''))
except Exception as e:
    sys.stderr.write('login error: ' + str(e) + '\n')
LOGINPY
)

        if [ -n "$TOKEN" ]; then
            # Write with restricted permissions atomically to avoid TOCTOU race
            (umask 077; printf '%s' "$TOKEN" > "$ADMIN_TOKEN_FILE")
            (umask 077; printf '%s' "$ADMIN_PASS" > "$DATA_DIR/admin_password")
            echo "Admin token provisioned successfully"
        else
            echo "Warning: could not provision admin token — user management features may be limited"
        fi
    else
        echo "Admin token already exists"

        # Re-validate the token — if expired, re-login using stored password
        EXISTING_TOKEN=$(cat "$ADMIN_TOKEN_FILE")
        HTTP_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer $EXISTING_TOKEN" \
            "http://127.0.0.1:8008/_synapse/admin/v2/users?limit=1" 2>&1 || echo "000")
        if [ "$HTTP_STATUS" != "200" ]; then
            echo "Admin token invalid (status $HTTP_STATUS), re-logging in..."
            STORED_PASS=$(cat "$DATA_DIR/admin_password" 2>/dev/null || echo "")
            if [ -n "$STORED_PASS" ]; then
                TOKEN=$(ADMIN_PASS="$STORED_PASS" python3 << 'LOGINPY'
import json, os, urllib.request, sys
try:
    pw = os.environ['ADMIN_PASS']
    payload = json.dumps({
        'type': 'm.login.password',
        'user': 'openhost-admin',
        'password': pw,
    }).encode()
    req = urllib.request.Request(
        'http://127.0.0.1:8008/_matrix/client/v3/login',
        data=payload,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        print(data.get('access_token', ''))
except Exception as e:
    sys.stderr.write('login error: ' + str(e) + '\n')
LOGINPY
)
                if [ -n "$TOKEN" ]; then
                    (umask 077; printf '%s' "$TOKEN" > "$ADMIN_TOKEN_FILE")
                    echo "Admin token refreshed"
                fi
            fi
        fi
    fi
}

# Provision admin token in background after Synapse starts
provision_admin_token &

# Hand off to the official Synapse entrypoint
echo "Starting Synapse..."
exec /start.py
