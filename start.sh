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
# Sentinel written by the admin UI to request an app restart. If present on
# boot we clear it; the admin UI creates it and then terminates Synapse (PID of
# the child of this script), which makes this supervisor exit so podman's
# --restart=unless-stopped policy relaunches the container with fresh config.
RESTART_SENTINEL="$DATA_DIR/.openhost_restart_requested"

mkdir -p "$DATA_DIR"

# Clear any stale restart sentinel from a previous boot. The admin UI writes
# this immediately before asking Synapse to stop; on the fresh boot it has
# served its purpose, so remove it.
rm -f "$RESTART_SENTINEL" 2>/dev/null || true

# ---------------------------------------------------------------------------
# openhost_settings.json — source of truth for admin-controlled toggles.
# Written once on first boot; thereafter managed by the admin UI.
# ---------------------------------------------------------------------------
# The canonical OpenHost community space, hardcoded as the default target for the
# "join the community" flow. A space is a room whose child rooms are declared via
# m.space.child; joining it lets the client browse its rooms. Lives on the
# OpenHost community hub homeserver and is joined over federation. Overridable per
# instance via OPENHOST_COMMUNITY_ROOM_ALIAS (seeded on first boot).
DEFAULT_COMMUNITY_ROOM_ALIAS="#openhost-community:matrix.openhost.imbue.com"

# The alias to seed on first boot: an operator-provided env override wins,
# otherwise the hardcoded canonical default. Used both when creating the initial
# settings file below and when backfilling older settings files that lack the
# key. After first boot the value in the settings file is authoritative, so this
# only ever seeds an absent key.
COMMUNITY_ROOM_ALIAS_SEED="${OPENHOST_COMMUNITY_ROOM_ALIAS:-$DEFAULT_COMMUNITY_ROOM_ALIAS}"

if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<EOF
{
  "federation_enabled": true,
  "open_registration": true,
  "community_onboarded": false,
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
    print('true' if d.get('federation_enabled', True) else 'false')
except Exception as e:
    sys.stderr.write('Warning: could not read settings file: ' + str(e) + '\n')
    print('true')
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
# this default existed). We must NOT touch a key that is present-but-empty: an
# empty alias intentionally disables the community-join opt-in, and re-populating
# it would silently re-enable a feature that was turned off. We also must NOT
# overwrite a value already present, so a configured alias is never clobbered on
# reboot.
#
# OPENHOST_COMMUNITY_ROOM_ALIAS, if set, seeds the alias only when the key is
# absent (a provisioning-time default), for the same reason. (On a fresh instance
# the key is already written above with this same seed value, so this block only
# fires for older settings files that predate the key.)
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
    APP_SUBDOMAIN="${OPENHOST_APP_NAME:-community-chat}"
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
# Apply federation + registration settings from openhost_settings.json.
# Delegate to admin.py's apply_settings_to_yaml (single source of truth) so the
# boot-time patching and the admin-UI-time patching can never drift. This
# robustly rewrites both the (possibly multi-line) listener `names:` list and
# federation_domain_whitelist, and the enable_registration* flags.
# ---------------------------------------------------------------------------
OPENHOST_APP_DATA_DIR="$DATA_DIR" python3 /app/admin.py apply-settings || \
    echo "Warning: apply-settings failed; homeserver.yaml may be stale"

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
# Web client (Cinny) — always served at the app root. The bundled client is
# pinned to this homeserver. The OpenHost owner is auto-logged-in via SSO
# whenever the client has no session (a first-run guard bounces a session-less
# client to the SSO/onboarding endpoint, from any app path). Matrix APIs stay
# under /_matrix and /_synapse.
# ---------------------------------------------------------------------------
WEBROOT="/app/webclient"
if [ -d "$WEBROOT" ]; then
    echo "Serving bundled web client (Cinny) from $WEBROOT"
    # Render Cinny's config.json with this zone's homeserver pinned, and wire the
    # community space + hub server into featuredCommunities so the client surfaces
    # the shared space and lets the owner browse/join its public rooms (Cinny's
    # "explore" uses these). The space is featured by its alias; the hub server
    # (the domain part of the alias) is featured so its public room directory is
    # browsable. Values are JSON string literals (quoted).
    COMMUNITY_ALIAS_CFG=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS_FILE'))
    print(d.get('community_room_alias') or '')
except Exception:
    print('')
")
    if [ -n "$COMMUNITY_ALIAS_CFG" ]; then
        # Hub server = the part after the first ':' in '#name:server'.
        COMMUNITY_SERVER_CFG="${COMMUNITY_ALIAS_CFG#*:}"
        SPACE_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$COMMUNITY_ALIAS_CFG")
        SERVER_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$COMMUNITY_SERVER_CFG")
    else
        SPACE_JSON=""
        SERVER_JSON=""
    fi
    if [ -f /app/webclient-config.template.json ]; then
        # Use python for the substitution so JSON values with special chars are
        # inserted safely (sed would choke on # and : in aliases).
        python3 - "$WEBROOT/config.json" "$SERVER_NAME" "$SPACE_JSON" "$SERVER_JSON" <<'PYEOF'
import sys
out_path, server_name, space_json, server_json = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
tpl = open("/app/webclient-config.template.json").read()
tpl = tpl.replace("SERVER_NAME_PLACEHOLDER", server_name)
tpl = tpl.replace("COMMUNITY_SPACE_PLACEHOLDER", space_json)
tpl = tpl.replace("COMMUNITY_ROOM_PLACEHOLDER", "")
tpl = tpl.replace("COMMUNITY_SERVER_PLACEHOLDER", server_json)
open(out_path, "w").write(tpl)
PYEOF
        echo "Rendered web client config.json (homeserver=$SERVER_NAME, space=${COMMUNITY_ALIAS_CFG:-none})"
    fi
    # Inject a first-run guard into index.html that runs before Cinny boots. Two
    # behaviours, combined:
    #   * No Matrix session (ANY path) -> bounce to the OpenHost SSO/onboarding
    #     endpoint. This runs on every path, not just "/": Cinny is a single-page
    #     app served with an index.html fallback, so a deep link or refresh on a
    #     sub-path (e.g. /inbox, a room URL) also boots session-less and would
    #     otherwise land on Cinny's own (dead-end, custom-homeservers-disabled)
    #     login screen.
    #   * Has a session AND on "/" -> ask the app where to land and, if that's the
    #     community space (not "/"), redirect there so opening the app (e.g. via
    #     the OpenHost dashboard link) lands onto the space lobby instead of
    #     Cinny's empty Home view. This fires on every full-page load of "/", not
    #     just the first: the guard script only runs on a real document load, and
    #     Cinny's in-app navigation to Home is a client-side history push that
    #     does NOT re-execute this script. So there is no "user deliberately went
    #     Home" case to protect here -- a fresh load of "/" always means the app
    #     was (re)opened, which is exactly when we want to land on the space.
    # The guard only ever runs inside this served index.html; the SSO endpoint
    # (/_openhost/*) and the Matrix APIs (/_matrix, /_synapse) are handled by
    # Caddy before the SPA, so redirecting to /_openhost/community/login cannot
    # loop back through here. Idempotent (only injects once).
    #
    # "Has a session" means ALL of the keys Cinny needs to initialise a session
    # are present: the access token, the device id, and the homeserver base URL.
    # A partial set (e.g. a token left behind but no device/hs after a partial
    # clear or a Cinny storage-schema change) can't boot Cinny and would dead-end
    # on its own login screen, so we treat that as "no session" and route through
    # SSO, which repopulates all of them.
    if [ -f "$WEBROOT/index.html" ] && ! grep -q "openhost-firstrun-guard" "$WEBROOT/index.html"; then
        GUARD='<script id="openhost-firstrun-guard">(function(){var ls=window.localStorage;if(!ls.getItem("cinny_access_token")||!ls.getItem("cinny_device_id")||!ls.getItem("cinny_hs_base_url")){location.replace("/_openhost/community/login");return;}if(location.pathname!=="/")return;var x=new XMLHttpRequest();x.open("GET","/_openhost/community/landing",false);try{x.send(null);if(x.status===200){var p=JSON.parse(x.responseText).path;if(p&&p!=="/"){location.replace(p);}}}catch(e){}})();</script>'
        # Insert right after <head> so it runs before Cinny boots.
        python3 - "$WEBROOT/index.html" "$GUARD" <<'PYEOF'
import sys
path, guard = sys.argv[1], sys.argv[2]
html = open(path).read()
if "openhost-firstrun-guard" not in html:
    html = html.replace("<head>", "<head>" + guard, 1)
    open(path, "w").write(html)
PYEOF
        echo "Injected first-run guard into web client index.html"
    fi
    # file_server for the SPA; unmatched paths fall back to index.html so
    # Cinny's client-side router works on deep links / refresh.
    ROOT_HANDLER="root * ${WEBROOT}
		try_files {path} /index.html
		file_server"
else
    echo "Web client not bundled — root proxies to Synapse (bare homeserver)"
    ROOT_HANDLER="reverse_proxy localhost:8008 {
			header_up Host {header.X-Forwarded-Host}
		}"
fi

# Generate Caddyfile from template with .well-known client discovery.
# Use a Python replacement for the root handler because it may span multiple
# lines (sed with newlines is fragile).
export ROOT_HANDLER
python3 - "$SERVER_NAME" "$PUBLIC_BASEURL" <<'PYEOF'
import os, sys
server_name, public_baseurl = sys.argv[1], sys.argv[2]
tpl = open("/app/Caddyfile.template").read()
tpl = tpl.replace("SERVER_NAME_PLACEHOLDER", server_name)
tpl = tpl.replace("PUBLIC_BASEURL_PLACEHOLDER", public_baseurl)
tpl = tpl.replace("ROOT_HANDLER_PLACEHOLDER", os.environ["ROOT_HANDLER"])
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

# Start the admin UI in background. It is told the path of the restart sentinel
# so it can request an app restart (see the supervision loop below).
OPENHOST_APP_DATA_DIR="$DATA_DIR" \
    SYNAPSE_SERVER_NAME="$SERVER_NAME" \
    OPENHOST_RESTART_SENTINEL="$RESTART_SENTINEL" \
    python3 /app/admin.py &
ADMIN_PID=$!
echo "Admin UI started (PID $ADMIN_PID)"

# ---------------------------------------------------------------------------
# Supervise Synapse.
#
# We deliberately do NOT `exec /start.py`. Instead we run it as a child and
# wait on it. This lets the admin UI apply settings changes with zero manual
# steps: it renders new settings, writes the restart sentinel, and stops
# Synapse. Synapse exiting unblocks the wait below; because the sentinel is
# present we exit this supervisor (PID 1), and podman's
# --restart=unless-stopped policy relaunches the whole container, which re-runs
# this script and re-renders homeserver.yaml/Caddyfile from the new settings.
#
# If Synapse exits WITHOUT the sentinel (i.e. it crashed), we also exit so
# podman restarts us — the behaviour you'd want from a supervisor anyway.
#
# Forward termination signals to Synapse so a normal `podman stop` shuts it
# down cleanly instead of waiting for SIGKILL.
# ---------------------------------------------------------------------------
SYNAPSE_PID=""
term_handler() {
    echo "start.sh: received termination signal, forwarding to Synapse"
    [ -n "$SYNAPSE_PID" ] && kill -TERM "$SYNAPSE_PID" 2>/dev/null || true
}
trap term_handler TERM INT

echo "Starting Synapse..."
/start.py &
SYNAPSE_PID=$!
echo "Synapse started (PID $SYNAPSE_PID)"

# Disable errexit for the supervision/teardown section. Synapse almost always
# exits non-zero on the restart path (SIGTERM -> status 143), and `wait`
# returning a non-zero child status (or >128 when interrupted by a trapped
# signal) must NOT abort the script — otherwise the second `wait`, the sidecar
# cleanup, and the sentinel log below would all be skipped, and on `podman stop`
# Synapse would never get its clean-shutdown window.
set +e

# Wait specifically for Synapse. `wait` returns when it exits (or when a trapped
# signal interrupts it, after which we re-wait for the clean shutdown).
wait "$SYNAPSE_PID"
SYNAPSE_EXIT=$?
# If a signal interrupted the wait, term_handler already forwarded SIGTERM;
# re-wait so Synapse finishes shutting down before we tear everything down.
wait "$SYNAPSE_PID" 2>/dev/null

echo "Synapse exited (status $SYNAPSE_EXIT). Shutting down supervisor so podman restarts the container."

# Stop the sidecars so the container fully exits (and podman's restart policy
# brings everything back up cleanly).
kill "$CADDY_PID" "$ADMIN_PID" 2>/dev/null || true

if [ -f "$RESTART_SENTINEL" ]; then
    echo "Restart was requested via admin UI; exiting for automatic restart."
fi

# Non-zero exit is fine: --restart=unless-stopped restarts regardless. Use the
# child's status when it's a clean/known code, otherwise a generic non-zero.
exit "${SYNAPSE_EXIT:-1}"
