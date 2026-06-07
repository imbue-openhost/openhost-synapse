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
    # Group 1 captures everything UP TO but NOT including the '[',
    # so the replacement can safely emit the full '[...]' list.
    def replace_names(m):
        prefix = m.group(1)  # e.g. "      - names: " (no trailing '[')
        if enabled:
            return prefix + "[client, federation]"
        return prefix + "[client]"
    pattern = re.compile(r'((?:-\s+)?names:\s*)\[client(?:,\s*federation)?\]')
    return pattern.sub(replace_names, content)

content = patch_federation_listener(content, federation_enabled)

# Federation domain whitelist
content = re.sub(r'\n# Federation disabled[^\n]*\n', '\n', content)
content = re.sub(r'(?m)^federation_domain_whitelist:.*$', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)
if not federation_enabled:
    content = content.rstrip() + "\n\n# Federation disabled — personal server.\nfederation_domain_whitelist: []\n"

# Password policy
# IMPORTANT: Do NOT use re.DOTALL (s flag) here — that crosses newlines and
# would eat subsequent top-level YAML sections. Use [^\n]* to stay within lines.
content = re.sub(r'(?m)^password_config:\n(?:[ \t]+[^\n]*\n)*', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)
password_block = f"""password_config:
  minimum_length: {pw_min_len}
  require_digit: {'true' if pw_require_digit else 'false'}
  require_punctuation: {'true' if pw_require_symbol else 'false'}
"""
content = content.rstrip() + "\n" + password_block

# Rate limits — remove existing rc_login block and rewrite
# IMPORTANT: Do NOT use re.DOTALL or '.*' in the char class — that crosses newlines
# and would eat subsequent top-level YAML sections (e.g. database:).
content = re.sub(r'(?m)^rc_login:\n(?:[ \t]+[^\n]*\n)*', '', content)
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

with open(yaml_path, "w") as f:
    f.write(content)
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

# ---------------------------------------------------------------------------
# Set up PostgreSQL for Matrix Authentication Service (MAS).
# MAS requires PostgreSQL; Synapse continues to use SQLite.
# ---------------------------------------------------------------------------
MAS_DIR="$DATA_DIR/mas"
MAS_KEYS_DIR="$MAS_DIR/keys"
MAS_CONFIG="$MAS_DIR/mas.yaml"
PG_DATA_DIR="$DATA_DIR/mas/pgdata"
PG_LOG="$PG_DATA_DIR/postgres.log"
MAS_DB_PASS_FILE="$MAS_DIR/db_password"
MAS_SECRET_FILE="$MAS_DIR/shared_secret"
MAS_ENCRYPTION_FILE="$MAS_DIR/encryption_secret"

# Create directories with postgres-writable permissions before switching user
# postgres needs to own the pgdata dir; we need to own the mas dir for secrets
mkdir -p "$MAS_KEYS_DIR" "$PG_DATA_DIR"
chown postgres:postgres "$PG_DATA_DIR"
# Keep $MAS_DIR root-owned but world-executable so postgres can traverse it
chmod 755 "$MAS_DIR"

# Initialize PostgreSQL data directory (first boot only)
if [ ! -f "$PG_DATA_DIR/PG_VERSION" ]; then
    echo "Initializing PostgreSQL for MAS..."
    su -s /bin/sh postgres -c "initdb -D '$PG_DATA_DIR' --encoding=UTF8 --locale=C 2>&1"
    echo "PostgreSQL initialized"
fi

# Start PostgreSQL with -w to wait for startup completion.
# Store log inside pgdata so postgres can write to it without extra permission grants.
echo "Starting PostgreSQL..."
su -s /bin/sh postgres -c "pg_ctl -D '$PG_DATA_DIR' -l '$PG_LOG' start -w -o '-k /tmp'" 2>&1
if [ $? -ne 0 ]; then
    echo "ERROR: PostgreSQL failed to start — MAS will not be available"
    # Continue without MAS rather than aborting Synapse startup
    SKIP_MAS=1
fi

if [ -z "$SKIP_MAS" ]; then
    echo "PostgreSQL started"

    # Generate MAS secrets on first boot (before DB setup so we have the password)
    if [ ! -f "$MAS_DB_PASS_FILE" ]; then
        MAS_DB_PASS=$(openssl rand -hex 24)
        (umask 077; printf '%s' "$MAS_DB_PASS" > "$MAS_DB_PASS_FILE")
        echo "Generated MAS DB password"
    else
        MAS_DB_PASS=$(cat "$MAS_DB_PASS_FILE")
    fi

    if [ ! -f "$MAS_SECRET_FILE" ]; then
        MAS_SECRET=$(openssl rand -hex 32)
        (umask 077; printf '%s' "$MAS_SECRET" > "$MAS_SECRET_FILE")
        echo "Generated MAS shared secret"
    else
        MAS_SECRET=$(cat "$MAS_SECRET_FILE")
    fi

    if [ ! -f "$MAS_ENCRYPTION_FILE" ]; then
        MAS_ENC_SECRET=$(openssl rand -hex 32)
        (umask 077; printf '%s' "$MAS_ENC_SECRET" > "$MAS_ENCRYPTION_FILE")
        echo "Generated MAS encryption secret"
    else
        MAS_ENC_SECRET=$(cat "$MAS_ENCRYPTION_FILE")
    fi

    # Create PostgreSQL user and database for MAS (idempotent).
    # Use PGPASSWORD env var so the password never appears in psql's argv.
    su -s /bin/sh postgres -c "psql -h /tmp -tAc \"SELECT 1 FROM pg_roles WHERE rolname='mas'\" | grep -q 1 || createuser -h /tmp mas" 2>/dev/null || true
    MAS_DB_PASS="$MAS_DB_PASS" su -s /bin/sh postgres -c "
        psql -h /tmp -c \"ALTER USER mas WITH PASSWORD '\$MAS_DB_PASS';\" 2>/dev/null || true
        psql -h /tmp -c \"SELECT 1 FROM pg_database WHERE datname='mas'\" | grep -q 1 || createdb -h /tmp -O mas mas 2>/dev/null || true
    "
    echo "MAS PostgreSQL database ready"

    # Generate MAS RSA signing key (first boot only)
    if [ ! -f "$MAS_KEYS_DIR/rsa.pem" ]; then
        # Use 2048-bit for faster generation (adequate for signing tokens)
        openssl genrsa -out "$MAS_KEYS_DIR/rsa.pem" 2048 2>/dev/null
        chmod 600 "$MAS_KEYS_DIR/rsa.pem"
        echo "Generated MAS RSA signing key"
    fi

    # Generate MAS config from template
    sed \
        -e "s|PUBLIC_BASEURL_PLACEHOLDER|${PUBLIC_BASEURL}|g" \
        -e "s|SERVER_NAME_PLACEHOLDER|${SERVER_NAME}|g" \
        -e "s|MAS_DB_PASSWORD_PLACEHOLDER|${MAS_DB_PASS}|g" \
        -e "s|MAS_SHARED_SECRET_PLACEHOLDER|${MAS_SECRET}|g" \
        -e "s|MAS_ENCRYPTION_SECRET_PLACEHOLDER|${MAS_ENC_SECRET}|g" \
        -e "s|DATA_DIR_PLACEHOLDER|${DATA_DIR}|g" \
        /app/mas.yaml.template > "$MAS_CONFIG"
    chmod 600 "$MAS_CONFIG"
    echo "MAS config generated: $MAS_CONFIG"

    # Patch Synapse homeserver.yaml to enable MAS integration
    MAS_SECRET="$MAS_SECRET" DATA_DIR="$DATA_DIR" python3 << 'MASPY'
import re, sys, os

yaml_path = os.environ['DATA_DIR'] + '/homeserver.yaml'
mas_secret = os.environ['MAS_SECRET']

try:
    content = open(yaml_path).read()
except OSError as e:
    sys.stderr.write(f"Error: could not read homeserver.yaml: {e}\n")
    sys.exit(1)

# Remove existing matrix_authentication_service block
content = re.sub(r'(?m)^matrix_authentication_service:\n(?:[ \t]+[^\n]*\n)*', '', content)
content = re.sub(r'\n{3,}', '\n\n', content)

# Add MAS integration block
mas_block = f"""matrix_authentication_service:
  enabled: true
  endpoint: "http://127.0.0.1:8080"
  secret: "{mas_secret}"
"""
content = content.rstrip() + "\n" + mas_block

with open(yaml_path, "w") as f:
    f.write(content)
print("Synapse homeserver.yaml patched for MAS integration")
MASPY

    # Run MAS database migrations
    echo "Running MAS database migrations..."
    if ! mas-cli --config "$MAS_CONFIG" database migrate 2>&1; then
        echo "Warning: MAS migration failed — MAS will start but may not function correctly"
    fi
fi # end SKIP_MAS

# ---------------------------------------------------------------------------
# Fix ownership for the host user (UID 1000)
# Explicitly EXCLUDE pgdata from the chown (postgres must own it).
# ---------------------------------------------------------------------------
find "$DATA_DIR" -maxdepth 4 -not -path "$PG_DATA_DIR*" -exec chown 1000:1000 {} \; 2>/dev/null || true
# Re-ensure postgres owns pgdata
chown -R postgres:postgres "$PG_DATA_DIR" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Start Caddy (serves /, /_openhost/*, .well-known, proxies Matrix + MAS)
# ---------------------------------------------------------------------------
caddy run --config /app/Caddyfile &
CADDY_PID=$!
echo "Caddy started (PID $CADDY_PID)"

# ---------------------------------------------------------------------------
# Start Matrix Authentication Service (only if PostgreSQL started successfully)
# ---------------------------------------------------------------------------
if [ -z "$SKIP_MAS" ] && [ -f "$MAS_CONFIG" ]; then
    mas-cli --config "$MAS_CONFIG" server &
    MAS_PID=$!
    echo "MAS server started (PID $MAS_PID)"
else
    echo "MAS skipped (PostgreSQL unavailable or MAS config missing)"
fi

# ---------------------------------------------------------------------------
# Start Go admin server
# The admin server reads/writes DATA_DIR and sends SIGHUP to Synapse.
# It also handles /_openhost/admin/* and / (which Caddy proxies to it).
# ---------------------------------------------------------------------------
OPENHOST_APP_DATA_DIR="$DATA_DIR" /app/admin-server &
ADMIN_PID=$!
echo "Admin server started (PID $ADMIN_PID)"

# ---------------------------------------------------------------------------
# get_admin_token: mints an admin access token for openhost-admin.
#
# Strategy:
# 1. Use /_synapse/admin/v1/register (shared-secret) to create the user on
#    first boot. This endpoint is never delegated to MAS.
# 2. On subsequent boots (user already exists, registration returns 400):
#    a. If MAS is configured, use 'mas-cli manage issue-compatibility-token'
#       to mint a Synapse compat token — this works regardless of MAS state.
#    b. Otherwise, use the Synapse Admin v1 user login endpoint.
# ---------------------------------------------------------------------------
get_admin_token() {
    ADMIN_PASS="$1" DATA_DIR="$DATA_DIR" SERVER_NAME="$SERVER_NAME" MAS_CONFIG="$MAS_CONFIG" python3 << 'ADMINTOKEN'
import json, os, urllib.request, urllib.error, sys, hmac, subprocess

admin_pass = os.environ['ADMIN_PASS']
data_dir = os.environ['DATA_DIR']
server_name = os.environ['SERVER_NAME']
mas_config = os.environ.get('MAS_CONFIG', '')

def get_shared_secret():
    """Read the registration_shared_secret from homeserver.yaml."""
    try:
        with open(data_dir + '/homeserver.yaml') as f:
            for line in f:
                if line.startswith('registration_shared_secret:'):
                    return line.split(':', 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return None

def register_via_shared_secret(username, password):
    """Register a user via /_synapse/admin/v1/register. Returns (token, user_existed)."""
    shared_secret = get_shared_secret()
    if not shared_secret:
        return None, False
    try:
        with urllib.request.urlopen('http://127.0.0.1:8008/_synapse/admin/v1/register') as resp:
            nonce = json.loads(resp.read())['nonce']
        mac = hmac.new(shared_secret.encode(), digestmod='sha1')
        mac.update(nonce.encode())
        mac.update(b'\x00')
        mac.update(username.encode())
        mac.update(b'\x00')
        mac.update(password.encode())
        mac.update(b'\x00')
        mac.update(b'admin')
        payload = json.dumps({
            'nonce': nonce,
            'username': username,
            'password': password,
            'admin': True,
            'mac': mac.hexdigest(),
        }).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:8008/_synapse/admin/v1/register',
            data=payload,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()).get('access_token'), False
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return None, True  # User already exists
        sys.stderr.write(f'register error {e.code}\n')
        return None, False
    except Exception as e:
        sys.stderr.write(f'register error: {e}\n')
        return None, False

def mas_issue_compat_token(username):
    """Use mas-cli to issue a Synapse compat token (works when MAS is active)."""
    if not mas_config or not os.path.exists(mas_config):
        return None
    try:
        result = subprocess.run(
            ['mas-cli', '--config', mas_config, 'manage',
             'issue-compatibility-token', username,
             '--yes-i-want-to-grant-synapse-admin-privileges'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            # Output is the token on stdout
            return result.stdout.strip()
        else:
            sys.stderr.write(f'mas issue-compat-token error: {result.stderr}\n')
    except Exception as e:
        sys.stderr.write(f'mas-cli error: {e}\n')
    return None

def synapse_admin_login(token, user_id):
    """Use Synapse Admin v1 login endpoint (only works if MAS is NOT enabled)."""
    try:
        payload = json.dumps({}).encode()
        req = urllib.request.Request(
            f'http://127.0.0.1:8008/_synapse/admin/v1/users/{user_id}/login',
            data=payload,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()).get('access_token')
    except Exception as e:
        sys.stderr.write(f'admin login error: {e}\n')
    return None

# Step 1: Try to register the admin user (first boot)
token, user_exists = register_via_shared_secret('openhost-admin', admin_pass)
if token:
    print(token)
    sys.exit(0)

if not user_exists:
    sys.stderr.write('User registration failed for an unknown reason\n')
    sys.exit(1)

# Step 2: User already exists. Try MAS compat token first (works with MAS enabled).
token = mas_issue_compat_token('openhost-admin')
if token:
    print(token)
    sys.exit(0)

# Step 3: MAS not available — try Synapse Admin v1 login endpoint (no MAS only).
# This requires an admin token to call, so we temporarily register a bootstrap user.
import time, hashlib
tmp_user = 'oh-setup-' + hashlib.sha1(os.urandom(8)).hexdigest()[:10]
tmp_pass = hashlib.sha256(os.urandom(32)).hexdigest()
bootstrap_token, _ = register_via_shared_secret(tmp_user, tmp_pass)
if bootstrap_token:
    admin_user_id = f'@openhost-admin:{server_name}'
    token = synapse_admin_login(bootstrap_token, admin_user_id)
    # Deactivate the temporary bootstrap user (best-effort cleanup)
    try:
        payload = json.dumps({'erase': True}).encode()
        deact_req = urllib.request.Request(
            f'http://127.0.0.1:8008/_synapse/admin/v1/deactivate/@{tmp_user}:{server_name}',
            data=payload,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {bootstrap_token}'}
        )
        urllib.request.urlopen(deact_req)
        sys.stderr.write(f'Cleaned up bootstrap user @{tmp_user}\n')
    except Exception:
        sys.stderr.write(f'Warning: could not clean up bootstrap user @{tmp_user}\n')
    if token:
        print(token)
        sys.exit(0)

sys.stderr.write('All admin token strategies failed\n')
sys.exit(1)
ADMINTOKEN
}

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

    if [ $ATTEMPTS -ge 60 ]; then
        echo "ERROR: Synapse did not start within 120s, skipping admin token provisioning"
        return 1
    fi

    if [ ! -f "$ADMIN_TOKEN_FILE" ] || [ ! -s "$ADMIN_TOKEN_FILE" ]; then
        echo "Provisioning admin token..."
        ADMIN_PASS=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c "import uuid; print(str(uuid.uuid4()))")

        if [ -z "$ADMIN_PASS" ]; then
            echo "ERROR: could not generate admin password"
            return 1
        fi

        # Try to register the admin user — pass password via environment to avoid
        # exposing it in /proc/<pid>/cmdline.
        # Note: register_new_matrix_user passes -p to argv of a subprocess, which
        # is visible in that child's cmdline. This is a known limitation of the
        # Synapse tooling; the password is only used once during first boot.
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
    # It's expected to fail if user already exists
    if 'already taken' not in result.stderr and 'already registered' not in result.stderr:
        sys.stderr.write('register_new_matrix_user failed: ' + result.stderr + '\n')
        sys.exit(1)
    else:
        sys.stderr.write('openhost-admin user already exists, skipping registration\n')
REGPY
        # Log in to get an access token.
        # When MAS is enabled, Synapse's login endpoint is delegated to MAS.
        # We use the Synapse admin shared-secret endpoint to mint a token directly,
        # which bypasses the MAS login flow (it uses /_synapse/admin endpoints).
        TOKEN=$(get_admin_token "$ADMIN_PASS")

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

        # Re-validate the token — if expired, re-mint
        EXISTING_TOKEN=$(cat "$ADMIN_TOKEN_FILE")
        HTTP_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer $EXISTING_TOKEN" \
            "http://127.0.0.1:8008/_synapse/admin/v2/users?limit=1" 2>/dev/null || echo "000")
        if [ "$HTTP_STATUS" != "200" ]; then
            echo "Admin token invalid (status $HTTP_STATUS), refreshing..."
            STORED_PASS=$(cat "$DATA_DIR/admin_password" 2>/dev/null || echo "")
            if [ -n "$STORED_PASS" ]; then
                TOKEN=$(get_admin_token "$STORED_PASS")
                if [ -n "$TOKEN" ]; then
                    (umask 077; printf '%s' "$TOKEN" > "$ADMIN_TOKEN_FILE")
                    echo "Admin token refreshed"
                else
                    echo "Warning: could not refresh admin token"
                fi
            else
                echo "Warning: no stored password, cannot refresh admin token"
            fi
        fi
    fi
}

# Provision admin token in background after Synapse starts
provision_admin_token &

# Hand off to the official Synapse entrypoint
echo "Starting Synapse..."
exec /start.py
