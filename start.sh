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
# The canonical OpenHost community room, hardcoded as the default target for the
# "join the community" flow. Lives on the OpenHost community hub homeserver and
# is joined over federation. Overridable per instance via
# OPENHOST_COMMUNITY_ROOM_ALIAS or the admin console.
DEFAULT_COMMUNITY_ROOM_ALIAS="#openhost-community-general:matrix.openhost.imbue.com"

# The alias to seed on first boot: an operator-provided env override wins,
# otherwise the hardcoded canonical default. Used both when creating the initial
# settings file below and when backfilling older settings files that lack the
# key. After first boot the value in the settings file is authoritative (the
# admin console can change or clear it), so this only ever seeds an absent key.
COMMUNITY_ROOM_ALIAS_SEED="${OPENHOST_COMMUNITY_ROOM_ALIAS:-$DEFAULT_COMMUNITY_ROOM_ALIAS}"

if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<EOF
{
  "federation_enabled": false,
  "open_registration": true,
  "onboarded": false,
  "community_joined": false,
  "community_room_alias": "$COMMUNITY_ROOM_ALIAS_SEED"
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

# Backfill the community room alias ONLY when the settings file predates the
# hardcoded default and has no alias key at all (older instances created before
# this default existed). We must NOT touch a key that is present-but-empty: the
# admin console lets an operator deliberately blank the alias to disable the
# community-join opt-in ("Leave blank to disable"), and re-populating it would
# silently re-enable a feature they turned off. We also must NOT overwrite a
# value already present, so an admin-chosen alias is never clobbered on reboot.
#
# OPENHOST_COMMUNITY_ROOM_ALIAS, if set, seeds the alias only when the key is
# absent (a provisioning-time default), for the same reason — it is not a
# per-reboot enforcer that would override later admin choices. (On a fresh
# instance the key is already written above with this same seed value, so this
# block only fires for older settings files that predate the key.)
python3 - "$SETTINGS_FILE" "$COMMUNITY_ROOM_ALIAS_SEED" <<'PYEOF'
import json, sys
path, seed = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception:
    d = {}
# Only seed when the key is entirely absent. A present value (including an
# intentionally-empty string) is authoritative and left untouched.
if "community_room_alias" not in d:
    d["community_room_alias"] = seed
    json.dump(d, open(path, "w"), indent=2)
    print(f"Seeded community_room_alias={seed}")
PYEOF

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
# Use Python to reliably patch the YAML listener list and whitelist —
# sed is fragile against Synapse's varied whitespace/quoting.
# ---------------------------------------------------------------------------
python3 << PYEOF
import re, sys

path = "$DATA_DIR/homeserver.yaml"
federation_enabled = "$FEDERATION_ENABLED" == "true"

try:
    content = open(path).read()
except OSError as e:
    sys.stderr.write(f"Warning: could not read homeserver.yaml: {e}\n")
    sys.exit(0)

# ---- Listener names ----
# Synapse generates listeners with a "names" list. We need to add or remove
# "federation" from that list. Handle both inline-list and multi-line formats.
def set_federation_listener(content, enabled):
    # Match "- names: [client]" or "- names: [client, federation]" (with optional spaces)
    # Also handle "names: [client]" without leading dash (inside a list item)
    def replace_names(m):
        prefix = m.group(1)  # everything before the list
        names_str = m.group(2)
        # Parse the names
        names = [n.strip().strip("'\"") for n in names_str.split(",")]
        names = [n for n in names if n and n not in ("client", "federation")]
        names = ["client"]
        if enabled:
            names = ["client", "federation"]
        return prefix + "[" + ", ".join(names) + "]"

    # Match inline list format: names: [client] or names: [client, federation]
    pattern = r'((?:- )?names:\s*\[)(client(?:,\s*federation)?)\]'
    new_content = re.sub(pattern, replace_names, content)
    if new_content != content:
        return new_content

    # If no match found and federation_enabled, try to find listener block and ensure federation
    return content

content = set_federation_listener(content, federation_enabled)

# ---- federation_domain_whitelist ----
# Remove any existing whitelist lines (and their comments)
content = re.sub(r'\n# Federation disabled[^\n]*\n', '\n', content)
content = re.sub(r'^federation_domain_whitelist:.*$', '', content, flags=re.MULTILINE)
content = re.sub(r'\n{3,}', '\n\n', content)  # collapse excess blank lines

if not federation_enabled:
    content = content.rstrip() + "\n\n# Federation disabled — personal server.\nfederation_domain_whitelist: []\n"

open(path, "w").write(content)
if federation_enabled:
    print("Federation listener enabled, whitelist restriction removed")
else:
    print("Federation listener client-only, whitelist blocks all federation")
PYEOF

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

# ---------------------------------------------------------------------------
# Root routing:
#   - The exact root path "/" goes to the admin app (Flask, :8009), which shows
#     first-run onboarding (create account) until onboarding is complete, then
#     redirects to Synapse / the admin console. Onboarded state can change at
#     runtime, so the decision must be made per-request by the app — not baked
#     into the Caddyfile at boot.
#   - Everything else (the Matrix client/federation API, .well-known, media,
#     the /_openhost admin + onboarding routes) proxies to Synapse or the admin
#     app as appropriate. See Caddyfile.template.
# ---------------------------------------------------------------------------
echo "Root path -> onboarding/admin app; homeserver API -> Synapse"

# Generate Caddyfile from template, filling in the server name + public base URL
# for .well-known discovery. Routing is static (root -> onboarding/admin app,
# everything else -> Synapse), so no dynamic root handler is needed.
python3 - "$SERVER_NAME" "$PUBLIC_BASEURL" <<'PYEOF'
import sys
server_name, public_baseurl = sys.argv[1], sys.argv[2]
tpl = open("/app/Caddyfile.template").read()
tpl = tpl.replace("SERVER_NAME_PLACEHOLDER", server_name)
tpl = tpl.replace("PUBLIC_BASEURL_PLACEHOLDER", public_baseurl)
open("/app/Caddyfile", "w").write(tpl)
PYEOF
echo "well-known: client_base=${PUBLIC_BASEURL}"

# Fix ownership for the host user (UID 1000)
chown -R 1000:1000 "$DATA_DIR" 2>/dev/null || true

# Start Caddy in background — it serves .well-known, rewrites Host from
# X-Forwarded-Host, and proxies to Synapse on port 8008.
caddy run --config /app/Caddyfile &
CADDY_PID=$!
echo "Caddy started (PID $CADDY_PID)"

# Start the admin UI in background
OPENHOST_APP_DATA_DIR="$DATA_DIR" SYNAPSE_SERVER_NAME="$SERVER_NAME" python3 /app/admin.py &
ADMIN_PID=$!
echo "Admin UI started (PID $ADMIN_PID)"

# Hand off to the official Synapse entrypoint
echo "Starting Synapse..."
exec /start.py
