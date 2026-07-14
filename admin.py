#!/usr/bin/env python3
"""
OpenHost Synapse Admin UI

Serves a simple web interface at /_openhost/admin for managing:
  - Federation (enable/disable)
  - Open registration (enable/disable)
  - The chat account password (used only for third-party Matrix clients; the
    built-in web client signs the owner in automatically via SSO)

Settings are persisted to openhost_settings.json in the Synapse data dir.
On change, homeserver.yaml is patched and the app restarts itself automatically
(no manual restart): we write a restart sentinel and stop Synapse, and the
supervisor in start.sh exits so podman's --restart=unless-stopped policy
relaunches the container with the new config.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, redirect, render_template_string, request

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "openhost_settings.json"
HOMESERVER_YAML = DATA_DIR / "homeserver.yaml"
# Path of the restart sentinel that start.sh watches. When we want the app to
# restart (to apply settings), we touch this file and then stop Synapse; the
# supervisor exits and podman relaunches the container. Kept in sync with
# start.sh's RESTART_SENTINEL via this env var.
RESTART_SENTINEL = Path(
    os.environ.get("OPENHOST_RESTART_SENTINEL", str(DATA_DIR / ".openhost_restart_requested"))
)

# Synapse listens on localhost:8008 inside the container. The admin/SSO code
# talks to it directly here, bypassing the OpenHost router + zone_auth (which
# only gates the public-facing subdomain, not intra-container localhost calls).
SYNAPSE_BASE = os.environ.get("SYNAPSE_LOCAL_URL", "http://localhost:8008")
# Where we persist the SSO service account's admin access token.
SSO_STATE_FILE = DATA_DIR / "openhost_sso.json"
# Synapse reserves the leading "_" localpart for appservices, so it can't start
# with an underscore. Keep it distinctive to avoid clashing with real users.
SSO_ADMIN_USER = "openhost-sso-admin"

# The canonical OpenHost community *space*. This is the space the "join the
# community" flow joins over federation. A Matrix space is itself a room whose
# child rooms are declared via m.space.child state; joining it gives the client
# the space and lets the user browse/enter its rooms. It lives on the OpenHost
# community hub homeserver and is referenced only by this alias string — the hub
# is separate infrastructure. Overridable per instance via the
# OPENHOST_COMMUNITY_ROOM_ALIAS env var (seeded on first boot).
DEFAULT_COMMUNITY_ROOM_ALIAS = "#openhost-community:matrix.openhost.imbue.com"

DEFAULTS = {
    # Federation is enabled by default so the community space (and other Matrix
    # servers) are reachable out of the box.
    "federation_enabled": True,
    "open_registration": True,
    "community_onboarded": False,
    "community_joined": False,
    # Set True by the onboarding POST when the owner opts into the community
    # space; cleared once the join lands (or if the owner opted out). The
    # onboarding page blocks its spinner on this being cleared.
    "community_join_pending": False,
    # Transient, per-boot flag: set True when this boot's join attempts all
    # failed (so the onboarding page can stop spinning and forward instead of
    # hanging forever). Reset at the start of every join run.
    "community_join_failed": False,
    # Transient, per-boot flag: set True by the boot worker once Synapse's
    # client API answers on this boot. The onboarding status endpoint reads this
    # instead of calling Synapse itself, so status polls stay cheap and never
    # stall on a slow/restarting Synapse. Reset to False at the start of boot.
    "community_synapse_ready": False,
    # The alias of the space the "join the community" flow joins. Defaults to the
    # canonical OpenHost community space; can be overridden.
    "community_room_alias": DEFAULT_COMMUNITY_ROOM_ALIAS,
}

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            # Fill in any missing keys with defaults
            return {**DEFAULTS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")


# ---------------------------------------------------------------------------
# homeserver.yaml patching
# ---------------------------------------------------------------------------

def _set_yaml_bool(content: str, key: str, value: bool) -> str:
    """Set a top-level boolean key in homeserver.yaml content."""
    yaml_value = "true" if value else "false"
    pattern = rf"^{re.escape(key)}:.*$"
    replacement = f"{key}: {yaml_value}"
    if re.search(pattern, content, flags=re.MULTILINE):
        return re.sub(pattern, replacement, content, flags=re.MULTILINE)
    # Key not present — append it
    return content.rstrip() + f"\n{replacement}\n"


def _patch_federation_listener(content: str, enabled: bool) -> str:
    """
    Add or remove 'federation' from the Synapse listener names list.

    Synapse's generated config uses a multi-line block-list format:

        names:
        - client
        - federation

    but an inline list (``names: [client]``) is also valid YAML. Handle both so
    the listener genuinely reflects the federation setting (leaving 'federation'
    in the listener when disabled is inconsistent even though the domain
    whitelist still blocks it).
    """
    # --- Inline list: names: [client] / [client, federation] ------------------
    inline = r"((?:-\s+)?names:\s*\[)\s*client\s*(?:,\s*federation\s*)?\]"
    if re.search(inline, content):
        repl = (lambda m: m.group(1) + ("client, federation]" if enabled else "client]"))
        return re.sub(inline, repl, content)

    # --- Multi-line block list ------------------------------------------------
    # Find a `names:` key followed by `- <name>` items and rewrite that block.
    def replace_block(m: re.Match) -> str:
        header = m.group("header")  # the `names:` line (with indentation)
        indent = m.group("indent")  # indentation of the `names:` key
        items_text = m.group("items")
        # Preserve the item indentation from the first item line.
        first = re.search(r"^(\s*)-\s*", items_text, flags=re.MULTILINE)
        item_indent = first.group(1) if first else indent + "  "
        names = re.findall(r"^\s*-\s*(\S+)\s*$", items_text, flags=re.MULTILINE)
        names = [n for n in names if n not in ("client", "federation")]
        rebuilt = ["client"] + (["federation"] if enabled else []) + names
        # de-dupe while preserving order
        seen: list[str] = []
        for n in rebuilt:
            if n not in seen:
                seen.append(n)
        lines = "".join(f"{item_indent}- {n}\n" for n in seen)
        return f"{header}\n{lines}".rstrip("\n") + "\n"

    block = re.compile(
        r"(?P<header>(?P<indent>[ \t]*)names:)[ \t]*\n"
        r"(?P<items>(?:[ \t]*-[ \t]*\S+[ \t]*\n)+)",
    )
    new = block.sub(replace_block, content, count=1)
    return new


def _set_federation_domain_whitelist(content: str, enabled: bool) -> str:
    """
    When federation is disabled, ensure federation_domain_whitelist: []
    When enabled, remove the whitelist restriction entirely.
    """
    pattern_simple = r"^federation_domain_whitelist:.*$"

    # Remove existing whitelist line and its preceding comment
    content = re.sub(r"\n# Federation disabled[^\n]*\n", "\n", content)
    content = re.sub(pattern_simple, "", content, flags=re.MULTILINE)
    # Collapse excess blank lines
    content = re.sub(r"\n{3,}", "\n\n", content)

    if not enabled:
        content = content.rstrip() + (
            "\n\n# Federation disabled — personal server.\n"
            "federation_domain_whitelist: []\n"
        )
    return content


def _patch_federation(content: str, enabled: bool) -> str:
    """Patch both the listener list and the domain whitelist."""
    content = _patch_federation_listener(content, enabled)
    content = _set_federation_domain_whitelist(content, enabled)
    return content


def apply_settings_to_yaml(settings: dict) -> None:
    try:
        content = HOMESERVER_YAML.read_text()
    except OSError as exc:
        app.logger.error("apply_settings_to_yaml: could not read homeserver.yaml: %s", exc)
        raise

    # Registration
    content = _set_yaml_bool(content, "enable_registration", settings["open_registration"])
    content = _set_yaml_bool(
        content,
        "enable_registration_without_verification",
        settings["open_registration"],
    )

    # Federation
    content = _patch_federation(content, settings["federation_enabled"])

    try:
        HOMESERVER_YAML.write_text(content)
    except OSError as exc:
        app.logger.error("apply_settings_to_yaml: could not write homeserver.yaml: %s", exc)
        raise


def _find_synapse_pids() -> list[int]:
    """Find running Synapse process IDs by scanning /proc without external tools."""
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
                if "synapse" in cmdline and "python" in cmdline:
                    pids.append(int(entry))
            except (OSError, ValueError):
                continue
    except OSError as exc:
        app.logger.error("_find_synapse_pids: could not scan /proc: %s", exc)
    return pids


def request_app_restart() -> bool:
    """Restart the whole app so config changes take effect — no manual step.

    Synapse only reads registration/federation settings at startup (SIGHUP only
    reloads log config), so applying these reliably requires a real restart.

    Mechanism: write the restart sentinel that start.sh watches, then stop
    Synapse (SIGTERM). start.sh runs Synapse as a supervised child; when it
    exits, start.sh (PID 1) exits too, and podman's --restart=unless-stopped
    policy relaunches the container, which re-renders config from the saved
    settings on boot.

    Returns True if the restart was successfully initiated. We stop Synapse in a
    background thread after a short delay so the HTTP response reaches the
    browser first (the client polls / auto-reloads afterwards).
    """
    try:
        RESTART_SENTINEL.write_text("restart requested by admin UI\n")
    except OSError as exc:
        app.logger.error("request_app_restart: could not write sentinel: %s", exc)
        return False

    pids = _find_synapse_pids()
    if not pids:
        # No Synapse yet (e.g. still starting). The sentinel is set; leave it —
        # but we can't force a restart, so report failure so the UI can advise.
        app.logger.warning("request_app_restart: no Synapse processes found")
        return False

    def _do_restart() -> None:
        import time

        # Give the HTTP response time to fully flush to the browser before we
        # kill Synapse (which tears the whole container down). The client also
        # guards against a cut-off response by aborting + polling, but a
        # comfortable delay here avoids that path in the common case.
        time.sleep(3)
        for pid in _find_synapse_pids():
            try:
                os.kill(pid, signal.SIGTERM)
                app.logger.info("request_app_restart: sent SIGTERM to pid %s", pid)
            except (ProcessLookupError, PermissionError) as exc:
                app.logger.error("request_app_restart: kill pid %s failed: %s", pid, exc)

    threading.Thread(target=_do_restart, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synapse Admin</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      margin: 0;
      padding: 2rem;
      min-height: 100vh;
    }
    .container {
      max-width: 560px;
      margin: 0 auto;
    }
    h1 {
      font-size: 1.5rem;
      font-weight: 600;
      margin-bottom: 0.25rem;
      color: #f8fafc;
    }
    .subtitle {
      color: #94a3b8;
      font-size: 0.875rem;
      margin-bottom: 2rem;
    }
    .card {
      background: #1e2130;
      border: 1px solid #2d3348;
      border-radius: 0.75rem;
      padding: 1.5rem;
      margin-bottom: 1rem;
    }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }
    .setting-info h2 {
      font-size: 1rem;
      font-weight: 500;
      margin: 0 0 0.25rem;
      color: #f1f5f9;
    }
    .setting-info p {
      font-size: 0.8125rem;
      color: #64748b;
      margin: 0;
      line-height: 1.4;
    }
    /* Toggle switch */
    .toggle-label {
      position: relative;
      display: inline-block;
      width: 52px;
      height: 28px;
      flex-shrink: 0;
    }
    .toggle-label input {
      opacity: 0;
      width: 0;
      height: 0;
    }
    .slider {
      position: absolute;
      inset: 0;
      background: #374151;
      border-radius: 28px;
      cursor: pointer;
      transition: background 0.2s;
    }
    .slider::before {
      content: "";
      position: absolute;
      width: 20px;
      height: 20px;
      left: 4px;
      top: 4px;
      background: #fff;
      border-radius: 50%;
      transition: transform 0.2s;
    }
    input:checked + .slider { background: #6366f1; }
    input:checked + .slider::before { transform: translateX(24px); }
    .save-btn {
      display: block;
      width: 100%;
      padding: 0.75rem;
      background: #6366f1;
      color: #fff;
      border: none;
      border-radius: 0.5rem;
      font-size: 0.9375rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s;
      margin-top: 1.5rem;
    }
    .save-btn:hover { background: #4f46e5; }
    .alert {
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      font-size: 0.875rem;
      margin-bottom: 1.25rem;
    }
    .alert-success {
      background: #052e16;
      border: 1px solid #166534;
      color: #4ade80;
    }
    .alert-warning {
      background: #1c1003;
      border: 1px solid #92400e;
      color: #fbbf24;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Synapse Admin</h1>
    <p class="subtitle">Manage the chat account, federation and registration for this Matrix server.</p>

    {% if message %}
      <div class="alert alert-success">{{ message }}</div>
    {% endif %}
    {% if warning %}
      <div class="alert alert-warning">{{ warning }}</div>
    {% endif %}

    <div class="card">
      <div class="setting-info" style="margin-bottom:1rem">
        <h2>Chat account</h2>
        <p>Your account is signed in automatically in the built-in web client,
           so you do not need a password for it. Set a password here only if you
           want to sign in from a third-party Matrix client (Element,
           FluffyChat, etc.).</p>
      </div>
      {% if owner_username %}
      <div style="padding:.5rem .75rem;background:#0d1117;border:1px solid #2d3348;border-radius:.4rem;margin-bottom:1rem;font-family:monospace;color:#a5b4fc">
        @{{ owner_username }}:{{ server_name }}
      </div>
      <form method="POST" action="/_openhost/admin/accounts/password"
            style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:flex-end">
        <div style="flex:1;min-width:180px">
          <label for="new_password" style="display:block;font-size:.8rem;color:#94a3b8;margin-bottom:.3rem">Set password for third-party clients</label>
          <input type="password" id="new_password" name="password" required
            minlength="8" autocomplete="new-password"
            placeholder="at least 8 characters"
            style="width:100%;padding:.5rem;border-radius:.4rem;border:1px solid #2d3348;background:#0d1117;color:#e2e8f0">
        </div>
        <button type="submit" class="save-btn" style="margin-top:0;width:auto;padding:.55rem 1.25rem">Update</button>
      </form>
      <p style="font-size:.75rem;color:#64748b;margin-top:.75rem">
        Setting a password does not restart the app or sign you out of the
        built-in web client.</p>
      {% else %}
      <p style="font-size:.8rem;color:#64748b">No account set up yet. Open the app to finish setup.</p>
      {% endif %}
    </div>

    <form method="POST" action="/_openhost/admin/save">
      <div class="card">
        <div class="setting-row">
          <div class="setting-info">
            <h2>Open Registration</h2>
            <p>Allow anyone to create an account on this server without an invitation.</p>
          </div>
          <label class="toggle-label">
            <input type="checkbox" name="open_registration" value="1"
              {% if settings.open_registration %}checked{% endif %}>
            <span class="slider"></span>
          </label>
        </div>
      </div>

      <div class="card">
        <div class="setting-row">
          <div class="setting-info">
            <h2>Federation</h2>
            <p>Allow this server to communicate with other Matrix servers across the network.</p>
          </div>
          <label class="toggle-label">
            <input type="checkbox" name="federation_enabled" value="1"
              {% if settings.federation_enabled %}checked{% endif %}>
            <span class="slider"></span>
          </label>
        </div>
      </div>

      <button type="submit" class="save-btn">Save &amp; Apply</button>
      <p style="font-size:.75rem;color:#64748b;margin-top:.75rem;text-align:center">
        Saving federation or registration changes restarts the app automatically
        to apply them.</p>
    </form>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Matrix SSO — mint a Matrix session for the OpenHost owner so the bundled
# web client starts logged in. All Synapse calls go to localhost:8008 (inside
# the container), so they are not subject to the router's zone_auth.
# ---------------------------------------------------------------------------


class SSOError(Exception):
    pass


def _synapse_request(
    method: str, path: str, token: str | None = None, body: dict | None = None, timeout: int = 15
) -> dict:
    url = f"{SYNAPSE_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SSOError(f"Synapse {method} {path} -> {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SSOError(f"Synapse {method} {path} unreachable: {exc}") from exc


def _read_registration_shared_secret() -> str:
    """Read registration_shared_secret from homeserver.yaml (Synapse generates one)."""
    try:
        content = HOMESERVER_YAML.read_text()
    except OSError as exc:
        raise SSOError(f"could not read homeserver.yaml: {exc}") from exc
    m = re.search(r'^registration_shared_secret:\s*"?([^"\n]+)"?\s*$', content, flags=re.MULTILINE)
    if not m:
        raise SSOError("registration_shared_secret not found in homeserver.yaml")
    return m.group(1).strip()


def _synapse_registration_mac(key: bytes, payload: bytes) -> str:
    """Compute Synapse's shared-secret registration MAC.

    This is a keyed HMAC used purely for API authentication against Synapse's
    /_synapse/admin/v1/register endpoint. The HMAC-SHA1 construction is dictated
    by the Synapse protocol itself: the server independently recomputes this
    exact MAC over the same field-separated payload and rejects the request if it
    does not match. It is NOT password hashing and the digest is never stored or
    used to verify a password — SHA1 here is a fixed wire-format detail, not a
    security choice, and swapping the algorithm would simply break registration.

    The inputs are opaque, already-encoded ``bytes`` (a MAC key and a pre-joined
    payload); this helper has no knowledge of what they contain.
    """
    # HMAC-SHA1 is mandated by the Synapse shared-secret registration wire
    # protocol (server recomputes and compares this exact MAC); it is a keyed
    # authentication MAC, not password hashing, and the digest is never stored.
    # The algorithm cannot be changed without breaking registration, so the
    # weak-hash finding here is a false positive.
    return hmac.new(  # lgtm[py/weak-sensitive-data-hashing]
        key, payload, digestmod=hashlib.sha1  # noqa: S324
    ).hexdigest()  # codeql[py/weak-sensitive-data-hashing]


def _shared_secret_register(username: str, password: str, admin: bool) -> dict:
    """Register a user via the shared-secret admin API (nonce + HMAC)."""
    nonce = _synapse_request("GET", "/_synapse/admin/v1/register")["nonce"]
    secret = _read_registration_shared_secret()
    # Build the exact field-separated payload Synapse expects
    # (nonce\0user\0password\0(admin|notadmin)) as opaque bytes, then hand it to
    # the MAC helper. See _synapse_registration_mac: this is protocol-mandated
    # API authentication, not password hashing.
    mac_payload = b"\x00".join(
        [
            nonce.encode(),
            username.encode(),
            password.encode(),
            b"admin" if admin else b"notadmin",
        ]
    )
    body = {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": admin,
        "mac": _synapse_registration_mac(secret.encode(), mac_payload),
    }
    return _synapse_request("POST", "/_synapse/admin/v1/register", body=body)


def _load_sso_state() -> dict:
    if SSO_STATE_FILE.exists():
        try:
            return json.loads(SSO_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sso_state(state: dict) -> None:
    SSO_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    try:
        os.chmod(SSO_STATE_FILE, 0o600)  # admin token — restrict to owner
    except OSError:
        pass


def _generate_password() -> str:
    return secrets.token_urlsafe(32)


def _bootstrap_admin_token() -> tuple[str, str]:
    """Register a fresh admin service account via the shared secret and return
    (access_token, user_id).

    We register a *uniquely-named* account each time we need to bootstrap. This
    avoids ever storing a password: the shared-secret register returns an access
    token directly, so only the (non-reversible) token is persisted. Access tokens
    don't expire by default, so this bootstrap runs essentially once; a unique name
    means a re-bootstrap (e.g. lost state file) can't collide with M_USER_IN_USE.
    """
    unique = secrets.token_hex(6)
    localpart = f"{SSO_ADMIN_USER}-{unique}"
    result = _shared_secret_register(localpart, _generate_password(), admin=True)
    return result["access_token"], result.get("user_id", "")


def _get_admin_token() -> str:
    """Return an admin access token, bootstrapping a service account on first use.

    Only the resulting access token (not any password) is persisted, at 0600.
    Synapse access tokens don't expire by default, so this is created once; if it
    is ever missing/invalid we bootstrap a fresh, uniquely-named service account.
    """
    state = _load_sso_state()
    token = state.get("admin_token")
    if token:
        try:
            _synapse_request("GET", "/_matrix/client/v3/account/whoami", token=token)
            return token
        except SSOError:
            pass  # missing/invalid — bootstrap a fresh account below

    token, user_id = _bootstrap_admin_token()
    _save_sso_state({"admin_token": token, "admin_user_id": user_id})
    return token


def _server_name() -> str:
    return os.environ.get("SYNAPSE_SERVER_NAME") or _read_server_name_from_yaml()


def _read_server_name_from_yaml() -> str:
    try:
        content = HOMESERVER_YAML.read_text()
        m = re.search(r'^server_name:\s*"?([^"\n]+)"?\s*$', content, flags=re.MULTILINE)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    raise SSOError("could not determine server_name")


def list_user_localparts() -> list[str]:
    """Return the localparts of human accounts on this homeserver.

    Excludes the SSO service account(s) we create for owner auto-login and any
    deactivated users. Uses Synapse's admin user-list API (paginated).
    """
    token = _get_admin_token()
    server = _server_name()
    localparts: list[str] = []
    next_token: str | None = None
    for _ in range(100):  # hard cap on pages to avoid an unbounded loop
        path = "/_synapse/admin/v2/users?deactivated=false&limit=100"
        if next_token is not None:
            path += f"&from={urllib.parse.quote(str(next_token))}"
        resp = _synapse_request("GET", path, token=token)
        for u in resp.get("users", []):
            name = u.get("name", "")  # full @user:server
            if u.get("deactivated"):
                continue
            m = re.match(r"^@([^:]+):(.+)$", name)
            if not m or m.group(2) != server:
                continue
            localpart = m.group(1)
            # Hide the SSO service accounts (named openhost-sso-admin[-xxxx]).
            if localpart == SSO_ADMIN_USER or localpart.startswith(SSO_ADMIN_USER + "-"):
                continue
            localparts.append(localpart)
        next_token = resp.get("next_token")
        if next_token is None:
            break
    return sorted(localparts)


# Serialize SSO logins: the flow sets a fresh ephemeral password then logs in
# with it, so two concurrent logins for the same owner could otherwise race
# (one overwrites the other's password before it logs in). Logins are rare and
# fast, so a process-wide lock is the simplest correct fix.
_sso_lock = threading.Lock()


def _admin_set_password(username: str, password: str, *, logout_devices: bool) -> None:
    """Set a user's password via Synapse's admin API (PUT /_synapse/admin/v2/users).

    Shared by the SSO-login backward-compat fallback, the admin password-change
    page, and onboarding's existing-account takeover so the URL-quoting and the
    (deliberately per-caller) logout_devices flag stay consistent in one place.

    logout_devices controls whether existing sessions/access tokens are
    invalidated: pass True to fully reclaim an account (onboarding takeover),
    False to preserve the owner's live sessions (routine password change / SSO
    fallback).
    """
    admin_token = _get_admin_token()
    user_id = f"@{username}:{_server_name()}"
    _synapse_request(
        "PUT",
        f"/_synapse/admin/v2/users/{urllib.parse.quote(user_id, safe='')}",
        token=admin_token,
        body={"password": password, "logout_devices": logout_devices},
    )


def _store_owner_password(username: str, password: str) -> None:
    """Persist the owner account's current password in the 0600 SSO state file
    so SSO can log in as them WITHOUT rotating (overwriting) it on every load.

    The stored password is the app-generated random one set at onboarding
    (the owner never sees it), or — once the owner sets a password on the admin
    settings page for third-party Matrix clients — that chosen password. In both
    cases SSO reuses whatever is stored here for auto-login rather than replacing
    it with a fresh random value each time; storing the admin-chosen password is
    what lets the same username/password work from other clients (e.g. a phone).
    """
    state = _load_sso_state()
    owners = state.get("owner_passwords") or {}
    owners[username] = password
    state["owner_passwords"] = owners
    _save_sso_state(state)


def _get_stored_owner_password(username: str) -> str | None:
    state = _load_sso_state()
    return (state.get("owner_passwords") or {}).get(username)


def sso_login_for_owner(username: str) -> dict:
    """Ensure the owner's Matrix user exists and mint a fresh session for it.

    We perform a *normal* client login (m.login.password), which — unlike the
    admin login endpoint — creates a real device and returns a device_id, which
    the web client (matrix-js-sdk) requires to initialise a session; a session
    with an empty device_id is rejected and the client bounces to its own login
    screen.

    Preferred path: log in with the owner's stored password, so it is never
    rotated. The stored password is the app-generated random one from onboarding,
    or the password the owner later set on the admin settings page (which then
    also works from third-party Matrix clients). Only if no stored password
    exists (older instances that predate this behaviour) do we fall back to
    setting a fresh ephemeral password via the admin API — and we persist that so
    subsequent logins stop rotating too.

    Serialized under _sso_lock so concurrent logins can't race on the password.

    Returns {user_id, access_token, device_id, home_server}.
    """
    with _sso_lock:
        return _sso_login_for_owner_locked(username)


def _sso_login_for_owner_locked(username: str) -> dict:
    server = _server_name()
    user_id = f"@{username}:{server}"

    password = _get_stored_owner_password(username)
    if not password:
        # Backward-compat: no stored password (e.g. instance onboarded before this
        # change, or SSO state lost). Set one via the admin API and persist it so
        # future logins reuse it instead of rotating. logout_devices=False so we
        # don't kill the owner's existing sessions.
        password = _generate_password()
        _admin_set_password(username, password, logout_devices=False)
        _store_owner_password(username, password)

    # Normal client login -> real device_id + access_token. No password change.
    login = _synapse_request(
        "POST",
        "/_matrix/client/v3/login",
        body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
            "initial_device_display_name": "OpenHost Community (SSO)",
        },
    )
    return {
        "user_id": login.get("user_id", user_id),
        "access_token": login["access_token"],
        "device_id": login.get("device_id", ""),
        "home_server": server,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _render_index(message=None, warning=None):
    settings = load_settings()
    server = ""
    try:
        server = _server_name()
    except SSOError:
        pass
    # Only show the account card once onboarding has set the owner account;
    # community_username is empty until then.
    return render_template_string(
        TEMPLATE,
        settings=settings,
        server_name=server,
        owner_username=settings.get("community_username") or "",
        message=message,
        warning=warning,
    )


@app.route("/_openhost/admin")
def index():
    return _render_index()


@app.route("/_openhost/admin/accounts/password", methods=["POST"])
def accounts_password():
    """Change the owner account's password. Updates it in Synapse and in the
    stored SSO password so auto-login keeps working. Does not restart the app."""
    password = request.form.get("password") or ""
    if len(password) < _MIN_PASSWORD_LEN:
        return _render_index(warning=f"Password must be at least {_MIN_PASSWORD_LEN} characters.")
    username = _owner_matrix_username()
    try:
        # logout_devices=False so changing the password doesn't kill existing
        # chat sessions (e.g. a mobile client).
        _admin_set_password(username, password, logout_devices=False)
    except SSOError as exc:
        app.logger.error("accounts_password: could not set password: %s", exc)
        return _render_index(warning="Could not update password. Check the app logs.")
    _store_owner_password(username, password)
    return _render_index(message=f"Password updated for @{username}.")


@app.route("/_openhost/admin/save", methods=["POST"])
def save():
    # Merge onto existing settings so flags not shown on this form (e.g.
    # community_onboarded, set by the onboarding flow) are preserved.
    settings = load_settings()
    prev = load_settings()
    settings["federation_enabled"] = request.form.get("federation_enabled") == "1"
    settings["open_registration"] = request.form.get("open_registration") == "1"
    # community_room_alias is no longer editable from the admin UI; it is seeded
    # on first boot (default or OPENHOST_COMMUNITY_ROOM_ALIAS) and preserved via
    # the load_settings() merge above.
    save_settings(settings)

    yaml_error = None
    try:
        if not HOMESERVER_YAML.exists():
            yaml_error = f"homeserver.yaml not found at {HOMESERVER_YAML}"
        else:
            apply_settings_to_yaml(settings)
    except OSError as exc:
        yaml_error = str(exc)

    # Only restart when a setting that requires a Synapse restart actually
    # changed (federation or registration). A no-op save shouldn't bounce the app.
    needs_restart = (
        settings["federation_enabled"] != prev["federation_enabled"]
        or settings["open_registration"] != prev["open_registration"]
    )

    restarted = False
    if not yaml_error and needs_restart:
        restarted = request_app_restart()

    warning = None
    message = None
    if yaml_error:
        warning = f"Settings saved, but could not update homeserver.yaml: {yaml_error}"
    elif not needs_restart:
        message = "Settings saved."
    elif restarted:
        message = "Settings saved. The app is restarting to apply changes; this page will be available again in a moment."
    else:
        warning = (
            "Settings saved, but the app could not be restarted automatically. "
            "Restart the app from your OpenHost dashboard to apply changes."
        )

    return _render_index(message=message, warning=warning)


def _join_room_with_token(token: str, room_alias: str, timeout: int = 60) -> str:
    """Join a (federated) room/space by alias using an existing access token.

    Returns the room_id. Works for a space alias too (a space is just a room);
    joining a space gives the client the space so the user can browse its rooms.
    Federation must already be enabled and active for a remote alias to resolve.

    Takes the token as an argument so callers that retry (federation warmup) can
    mint one session up front and reuse it, instead of re-logging-in — and
    minting a new device — on every attempt.

    ``timeout`` defaults to 60s: a *cold* federated join (first contact with the
    remote server, alias resolution, key fetch, partial-state sync kickoff) can
    legitimately take well over the 15s default, and cutting it short makes the
    join look like it failed even though Synapse goes on to complete it.
    """
    # POST /join/{roomIdOrAlias} resolves the alias (over federation if remote)
    # and joins. Idempotent: joining an already-joined room returns the room_id.
    quoted = urllib.parse.quote(room_alias, safe="")
    resp = _synapse_request("POST", f"/_matrix/client/v3/join/{quoted}", token=token, body={}, timeout=timeout)
    return resp.get("room_id", "")


def _is_joined_to_space(token: str, room_alias: str) -> bool:
    """True if the token's account is already a member of the space behind
    ``room_alias``. Used to confirm a join actually landed even when the join
    POST itself timed out on our side (Synapse can finish the federated join
    after our HTTP client gives up).

    Resolves the alias to a room_id, then checks the account's joined rooms.
    Any failure (alias not resolvable yet, request error) returns False so the
    caller keeps retrying.
    """
    try:
        quoted = urllib.parse.quote(room_alias, safe="")
        resolved = _synapse_request(
            "GET", f"/_matrix/client/v3/directory/room/{quoted}", token=token, timeout=30
        )
        room_id = resolved.get("room_id")
        if not room_id:
            return False
        joined = _synapse_request("GET", "/_matrix/client/v3/joined_rooms", token=token, timeout=30)
        return room_id in (joined.get("joined_rooms") or [])
    except SSOError:
        return False


def _default_account_username() -> str:
    """Suggested default username for the first account, derived from the
    OpenHost owner's username (OPENHOST_OWNER_USERNAME) so onboarding pre-fills
    the operator's own name instead of a generic "owner".

    Matrix localparts are restricted (lowercase letters, digits, and . _ = -),
    so we lowercase and strip anything else. If the result is empty (or the env
    var is unset), fall back to the stable "owner" default.
    """
    raw = os.environ.get("OPENHOST_OWNER_USERNAME", "") or ""
    sanitized = re.sub(r"[^a-z0-9._=-]", "", raw.strip().lower())
    return sanitized or "owner"


def _owner_matrix_username() -> str:
    """The Matrix localpart for the OpenHost owner. Configurable via onboarding;
    falls back to the OpenHost owner username (or a stable default)."""
    settings = load_settings()
    return settings.get("community_username") or _default_account_username()


SSO_BOOTSTRAP_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Signing in...</title></head>
<body style="background:#0f1117;color:#e2e8f0;font-family:sans-serif;text-align:center;padding-top:20vh">
<p id="msg">Signing you in to community chat...</p>
<script>
(function () {
  var token = {{ access_token|tojson }};
  var deviceId = {{ device_id|tojson }};
  var userId = {{ user_id|tojson }};
  var hsBase = {{ hs_base_url|tojson }};

  try {
    localStorage.setItem("cinny_access_token", token);
    localStorage.setItem("cinny_device_id", deviceId);
    localStorage.setItem("cinny_user_id", userId);
    localStorage.setItem("cinny_hs_base_url", hsBase);
  } catch (e) {}

  // The access token/device was just minted server-side. Loading the client
  // immediately can race that write becoming usable (the client would then show
  // a transient "can't sign in yet" screen). Confirm the session is live by
  // polling /whoami with the token, and only then hand off to the client. Retry
  // for a few seconds; fall back to loading anyway so we never get stuck here.
  var attempts = 0;
  var maxAttempts = 20;   // ~10s at 500ms
  function go() { window.location.replace("/"); }
  function check() {
    attempts++;
    fetch(hsBase + "/_matrix/client/v3/account/whoami", {
      headers: { Authorization: "Bearer " + token },
      cache: "no-store"
    }).then(function (r) {
      if (r.ok) { go(); }
      else if (attempts < maxAttempts) { setTimeout(check, 500); }
      else { go(); }
    }).catch(function () {
      if (attempts < maxAttempts) { setTimeout(check, 500); }
      else { go(); }
    });
  }
  check();
})();
</script>
</body></html>
"""


ONBOARDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Set up chat</title>
<style>
  *,*::before,*::after{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:2rem;min-height:100vh}
  .container{max-width:620px;margin:0 auto}
  h1{font-size:1.6rem;color:#f8fafc;margin-bottom:.25rem}
  .subtitle{color:#94a3b8;margin-bottom:1.5rem}
  .card{background:#1e2130;border:1px solid #2d3348;border-radius:.75rem;padding:1.5rem;margin-bottom:1rem}
  .card h2{font-size:1.05rem;color:#f1f5f9;margin:0 0 .5rem}
  .card p,li{color:#cbd5e1;font-size:.9rem;line-height:1.5}
  label{display:block;font-size:.9rem;color:#f1f5f9;margin-bottom:.35rem}
  input[type=text],input[type=password]{width:100%;padding:.6rem;border-radius:.5rem;border:1px solid #2d3348;background:#0d1117;color:#e2e8f0;font-size:.95rem}
  .hint{color:#64748b;font-size:.8rem;margin-top:.35rem}
  .consent{display:flex;gap:.6rem;align-items:flex-start;margin-top:1rem}
  .consent input{margin-top:.2rem}
  .btn{display:block;width:100%;padding:.75rem;background:#6366f1;color:#fff;border:none;border-radius:.5rem;font-size:.95rem;font-weight:500;cursor:pointer;margin-top:1.25rem}
  .btn:hover{background:#4f46e5}
  .btn:disabled{opacity:.7;cursor:default}
  .err{color:#f87171;font-size:.85rem;margin-top:.5rem}
  .row{display:flex;gap:.75rem;flex-wrap:wrap}
  .row>div{flex:1;min-width:150px}
  .saving{display:flex;align-items:center;gap:.65rem;color:#cbd5e1;font-size:.9rem;margin-top:1.25rem;justify-content:center}
  .spinner{width:18px;height:18px;border:2px solid #374151;border-top-color:#6366f1;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
</style></head>
<body><div class="container">
  <h1>Set up chat</h1>
  <p class="subtitle">Choose a username, then open chat.
     <a href="/_openhost/community/help" style="color:#a5b4fc">Learn more</a>.</p>

  {% if error %}<div class="err">{{ error }}</div>{% endif %}

  <form id="onboarding-form" method="POST" action="/_openhost/community/onboarding">
    <div class="card">
      <div class="row">
        <div>
          <label for="username">Username</label>
          <input type="text" id="username" name="username" value="{{ suggested }}"
                 pattern="[a-z0-9._=-]+" required autocomplete="off" placeholder="alice">
        </div>
      </div>
      <p class="hint">You'll be signed in to the built-in chat automatically, so
         no password is needed here. Address:
         <code>@&lt;username&gt;:{{ server_name }}</code>. To sign in from a
         third-party Matrix client (Element, FluffyChat, etc.), set a password
         later on the admin settings page (<code>/_openhost/admin</code>).</p>

      {% if community_room_alias %}
      <label class="consent">
        <input type="checkbox" name="join_community" value="1" checked>
        <span>Join the OpenHost community space.</span>
      </label>
      {% endif %}
      <p class="hint">Federation is on by default so other Matrix servers are
         reachable; you can turn it off later in the admin console.
         <a href="/_openhost/community/help" style="color:#a5b4fc">Details</a>.</p>
    </div>

    <button id="submit-btn" type="submit" class="btn"{% if saving %} style="display:none"{% endif %}>Finish &amp; open chat</button>
    <div id="saving" class="saving"{% if not saving %} style="display:none"{% endif %}>
      <span class="spinner"></span>
      <span id="saving-msg">Setting up chat and starting the server. This opens automatically...</span>
    </div>
  </form>

  <script>
  (function () {
    var form = document.getElementById("onboarding-form");
    var btn = document.getElementById("submit-btn");
    var saving = document.getElementById("saving");
    if (!form) { return; }
    var alreadySaving = {{ 'true' if saving else 'false' }};

    // Submit onboarding via fetch so we stay on this screen with a spinner,
    // then poll until the app finishes its automatic restart (to activate
    // federation) AND the community-space join has landed, so the shared space
    // is already there when the chat client opens. This replaces the old
    // transitional "Almost there" page.
    var LOGIN_URL = "/_openhost/community/login";
    var STATUS_URL = "/_openhost/community/onboarding/status";

    function setSavingMsg(text) {
      var el = document.getElementById("saving-msg");
      if (el) { el.textContent = text; }
    }

    function pollUntilUp() {
      // The app restarts to apply federation; the admin server (which served
      // this page) goes away and comes back. Poll the onboarding status
      // endpoint and only forward once it reports done: Synapse is back up and
      // no community-space join is still outstanding. While the join is in
      // flight the spinner keeps spinning ("Joining the community space...").
      var attempts = 0;
      var maxAttempts = 200;  // ~6.5 min at 2s
      function ping() {
        attempts++;
        fetch(STATUS_URL, { cache: "no-store", headers: { "X-Requested-With": "fetch" } })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) {
            if (data) {
              if (data.phase === "joining") {
                setSavingMsg("Joining the community space...");
              } else if (data.phase === "starting") {
                setSavingMsg("Starting the server...");
              }
              // done => Synapse up and the join has landed (or none was wanted,
              // or this boot's join attempts gave up). Forward to the client.
              if (data.done) { window.location.replace(LOGIN_URL); return; }
            }
            if (attempts < maxAttempts) { setTimeout(ping, 2000); }
            else { window.location.replace(LOGIN_URL); }
          })
          .catch(function () {
            // The server is mid-restart (endpoint unreachable). Keep polling.
            if (attempts < maxAttempts) { setTimeout(ping, 2000); }
            else { window.location.replace(LOGIN_URL); }
          });
      }
      // Give the server a moment to begin restarting before polling so we don't
      // immediately see the still-up (about-to-die) instance and forward early.
      setTimeout(ping, 2000);
    }

    // Non-JS fallback path: the server already accepted onboarding and rendered
    // this page in the saving state (the app is restarting). Just poll.
    if (alreadySaving) { pollUntilUp(); return; }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      btn.disabled = true;
      btn.style.display = "none";
      saving.style.display = "flex";

      // The POST triggers an app restart (to activate federation), which can
      // cut off this very response mid-flight: the server may drop the
      // connection while (or just after) sending it, leaving fetch hanging with
      // headers received but body never completing. So bound the request with an
      // AbortController: if it doesn't complete quickly we assume the restart
      // began and switch to polling, rather than stranding the user on a spinner
      // forever. pollStarted guards against double-starting the poll loop.
      var pollStarted = false;
      function startPolling() {
        if (pollStarted) { return; }
        pollStarted = true;
        pollUntilUp();
      }

      var controller = new AbortController();
      var abortTimer = setTimeout(function () { controller.abort(); }, 8000);

      // Use getAttribute("action"): a form control named "action" would
      // otherwise clobber form.action (it returns that element, not the URL),
      // sending the POST to a bogus path.
      fetch(form.getAttribute("action"), {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        signal: controller.signal
      }).then(function (r) {
        return r.json().catch(function () { return {}; });
      }).then(function (data) {
        clearTimeout(abortTimer);
        if (data && data.error) {
          // Validation / setup error: show it and re-enable the form.
          saving.style.display = "none";
          btn.style.display = "block";
          btn.disabled = false;
          var err = document.createElement("div");
          err.className = "err";
          err.textContent = data.error;
          form.parentNode.insertBefore(err, form);
          return;
        }
        // Onboarding succeeded and the app is restarting; poll then forward.
        startPolling();
      }).catch(function () {
        // Aborted (restart cut the response) or a network hiccup: assume the
        // restart began and poll for the app to come back rather than stranding
        // the user. The status endpoint's join_pending gate stops us forwarding
        // before the join lands.
        clearTimeout(abortTimer);
        startPolling();
      });
    });
  })();
  </script>
</div></body></html>
"""


HELP_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Community Chat: help</title>
<style>
  *,*::before,*::after{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:2rem;min-height:100vh}
  .container{max-width:640px;margin:0 auto}
  h1{font-size:1.5rem;color:#f8fafc;margin-bottom:.25rem}
  h2{font-size:1.05rem;color:#f1f5f9;margin:1.25rem 0 .4rem}
  p,li{color:#cbd5e1;font-size:.92rem;line-height:1.55}
  ul{margin:.4rem 0 0 1.1rem}
  code{color:#a5b4fc}
  a.back{display:inline-block;margin-top:1.5rem;color:#a5b4fc}
</style></head>
<body><div class="container">
  <h1>Community Chat</h1>
  <p>This runs a private Matrix homeserver on your instance with a built-in web
     chat client. You sign in to it automatically as your account.</p>

  <h2>Your account</h2>
  <p>You choose one username. The built-in web client signs you in
     automatically, so no password is needed to chat here. Usernames use
     lowercase letters, numbers, and <code>. _ = -</code> only, giving a Matrix
     address like <code>@name:{{ server_name }}</code>. To sign in from a
     third-party Matrix client (Element, FluffyChat, etc.), set a password on
     the admin console (<code>/_openhost/admin</code>) after setup.</p>

  <h2>Federation</h2>
  <ul>
    <li>Federation lets your server talk to other Matrix servers, including the
        OpenHost community. It is on by default and can be toggled anytime in the
        admin console.</li>
    <li>Running a federated server may carry responsibilities (content, data, and
        legal considerations) that vary by jurisdiction.</li>
    <li>Federation needs a publicly reachable instance; it will not work on
        setups without public inbound HTTPS (e.g. some tunnel-only configs).</li>
  </ul>

  <h2>OpenHost community space</h2>
  <p>Optionally join the shared OpenHost community space to chat with other
     OpenHost users. A space is a collection of rooms: joining it gives you the
     space so you can browse and enter its rooms. You can leave it off to keep
     your server fully private.</p>

  <a class="back" href="/_openhost/community/onboarding">&larr; Back to setup</a>
</div></body></html>
"""

# Matrix localpart grammar technically allows a wider set, but Synapse's default
# user_id validation is stricter and '/' in particular breaks account creation and
# URL handling. Restrict to the safe, portable subset.
_USERNAME_RE = re.compile(r"^[a-z0-9._=-]+$")

# Minimum password length for accounts created via the admin UI / onboarding.
_MIN_PASSWORD_LEN = 8

_INVALID_USERNAME_MSG = (
    "Invalid username. Use lowercase letters, numbers, and . _ = - "
    "(not starting with _)."
)


def _validate_username(username: str) -> str | None:
    """Return None if the (already normalized) username is valid, else a
    user-facing error string. Shared by create_account and onboarding so the
    rule and message live in one place."""
    if not username or not _USERNAME_RE.match(username) or username.startswith("_"):
        return _INVALID_USERNAME_MSG
    return None


def create_account(username: str, password: str, admin: bool = False) -> str | None:
    """Validate inputs and register a Matrix account via the shared-secret API.

    Returns None on success, or a user-facing error string on failure. Used by
    onboarding to create the single owner account.
    """
    username = (username or "").strip().lower()
    password = password or ""
    err = _validate_username(username)
    if err:
        return err
    if len(password) < _MIN_PASSWORD_LEN:
        return f"Password must be at least {_MIN_PASSWORD_LEN} characters."
    try:
        _shared_secret_register(username, password, admin=admin)
    except SSOError as exc:
        detail = str(exc)
        if "M_USER_IN_USE" in detail or "409" in detail:
            # Deliberately does not echo the submitted username back into the
            # message: the value is already visible in the form's username field,
            # and keeping user-provided input out of the rendered error text keeps
            # this off any reflected-XSS taint path.
            return "That username is already taken. Choose another."
        app.logger.error("create_account: registration failed: %s", exc)
        return "Could not create account. Check the app logs."
    return None


def _render_onboarding(server, room_alias, *, error=None, suggested=None, saving=False):
    if suggested is None:
        suggested = _default_account_username()
    return render_template_string(
        ONBOARDING_TEMPLATE,
        error=error,
        suggested=suggested,
        server_name=server,
        community_room_alias=room_alias,
        saving=saving,
    )


def _wants_json() -> bool:
    """True when onboarding was submitted via the page's fetch() (JS enabled).

    The client sets X-Requested-With: fetch. When JS is enabled we return JSON
    so the page can stay put, show a spinner, and poll for the restart; when it
    is not, we fall back to rendering full HTML pages.
    """
    return request.headers.get("X-Requested-With") == "fetch"


def _onboarding_error(server, room_alias, *, suggested, error):
    """Return an onboarding error as JSON (fetch path) or re-rendered HTML."""
    if _wants_json():
        return {"error": error}, 400
    return _render_onboarding(server, room_alias, suggested=suggested, error=error)


@app.route("/_openhost/community/onboarding", methods=["GET", "POST"])
def community_onboarding():
    """First-run flow: set up the single owner account (username only), then
    finish. No password is entered here: the built-in web client signs the owner
    in via SSO, and a password for third-party clients can be set later on the
    admin settings page. Owner-only (zone_auth gated)."""
    settings = load_settings()
    # Onboarding is one-time: once finished, send already-onboarded owners
    # straight to login instead.
    if settings.get("community_onboarded", False):
        return redirect("/_openhost/community/login", code=302)
    server = ""
    try:
        server = _server_name()
    except SSOError:
        pass

    room_alias = settings.get("community_room_alias", "")

    if request.method != "POST":
        return _render_onboarding(server, room_alias)

    # --- Finish onboarding: create the single owner account -------------------
    # Onboarding only asks for a username. Most users chat via the built-in web
    # client (auto-logged-in via SSO), so no password is entered here: we
    # generate a strong random one for SSO to use. A user who wants to sign in
    # from a third-party Matrix client sets a password later on the admin
    # settings page (/_openhost/admin), which updates Synapse and the stored SSO
    # password together.
    owner_username = (request.form.get("username") or "").strip().lower()
    owner_password = _generate_password()
    join_community = request.form.get("join_community") == "1"
    # Federation is enabled by default (no onboarding checkbox); it can still be
    # turned off later from the admin console.
    enable_federation = True

    # Validate the username up front (create_account also validates, but we may
    # take the "already exists" branch which skips it).
    username_error = _validate_username(owner_username)
    if username_error:
        return _onboarding_error(
            server, room_alias, suggested=owner_username, error=username_error,
        )

    # Create the owner's Matrix account (single account). If the name is already
    # taken, reset its password to the freshly generated one via the admin API so
    # SSO auto-login works. There is no password prompt to match against anymore.
    #
    # SECURITY: open registration is on by default and /_matrix registration is
    # public, so a third party could pre-register the owner's (predictable)
    # username before the owner onboards. This first-run takeover therefore uses
    # logout_devices=True to invalidate ANY pre-existing sessions/access tokens on
    # that account (e.g. an attacker's), so onboarding fully reclaims it. This is
    # the opposite of the admin password-change page, where preserving the owner's
    # own live sessions (logout_devices=False) is the correct behaviour.
    existing = []
    try:
        existing = list_user_localparts()
    except SSOError:
        existing = []
    if owner_username in existing:
        try:
            # Resolve the server name properly (the page-level `server` may be
            # empty if it wasn't readable at render time). list_user_localparts
            # above already succeeded, so Synapse is reachable here.
            _admin_set_password(owner_username, owner_password, logout_devices=True)
        except SSOError as exc:
            app.logger.error("onboarding: could not reset existing account password: %s", exc)
            return _onboarding_error(
                server, room_alias, suggested=owner_username,
                error="Could not set up that account. Check the app logs.",
            )
    else:
        error = create_account(owner_username, owner_password, admin=True)
        if error:
            return _onboarding_error(server, room_alias, suggested=owner_username, error=error)

    # Persist the generated password (0600) so SSO auto-login reuses it instead
    # of rotating it on every load.
    _store_owner_password(owner_username, owner_password)

    settings = load_settings()
    settings["community_username"] = owner_username
    settings["community_onboarded"] = True
    # A community join is pending only if the owner opted in AND a space/room
    # alias is configured. Joining a remote (federated) alias requires federation
    # active in the running Synapse, which only takes effect after a restart, so
    # the join is completed on the next boot by the boot worker (_on_boot_worker).
    want_join = bool(join_community and room_alias)
    settings["federation_enabled"] = enable_federation
    settings["community_join_pending"] = want_join
    # Fresh onboarding run: clear any stale per-boot transient flags so the
    # page's status poll starts clean.
    settings["community_join_failed"] = False
    settings["community_synapse_ready"] = False
    save_settings(settings)

    # Federation is applied by patching homeserver.yaml + an automatic app
    # restart. It's on by default, so onboarding always restarts once here to
    # activate it; any pending community join then completes in the background.
    try:
        apply_settings_to_yaml(settings)
    except OSError as exc:
        app.logger.error("could not apply federation setting: %s", exc)
        return _onboarding_error(
            server, room_alias, suggested=owner_username,
            error="Set up chat, but could not turn on federation. You can "
            "retry from the admin console.",
        )
    request_app_restart()
    # Success. The app is restarting to activate federation. The onboarding page
    # stays put (spinner) and polls until it comes back, then forwards to login.
    # JS path: return JSON so the page's fetch handler starts polling. Non-JS
    # fallback: re-render this same screen in the saving state (self-polls).
    if _wants_json():
        return {"ok": True}
    return _render_onboarding(server, room_alias, suggested=owner_username, saving=True)


@app.route("/_openhost/community/help")
def community_help():
    """Detailed help for the onboarding flow. Owner-only (zone_auth gated)."""
    server = ""
    try:
        server = _server_name()
    except SSOError:
        pass
    return render_template_string(HELP_TEMPLATE, server_name=server)


@app.route("/_openhost/community/login")
def community_login():
    """Owner SSO entrypoint: mint a Matrix session and hand it to the web client.

    Only reachable by the OpenHost owner (zone_auth gates this subdomain path).
    Redirects to onboarding on first run until setup is complete.
    """
    settings = load_settings()
    if not settings.get("community_onboarded", False):
        return redirect("/_openhost/community/onboarding", code=302)
    username = _owner_matrix_username()
    try:
        session = sso_login_for_owner(username)
    except SSOError as exc:
        # Log the detail server-side; return a generic message so internal
        # errors (which may include upstream response bodies) aren't exposed.
        app.logger.error("community_login: SSO failed: %s", exc)
        return "Could not sign in to chat. Please try again or check the app logs.", 502
    # Cinny expects the homeserver base URL it will talk to (same origin).
    hs_base_url = f"https://{session['home_server']}"
    return render_template_string(
        SSO_BOOTSTRAP_TEMPLATE,
        access_token=session["access_token"],
        device_id=session["device_id"],
        user_id=session["user_id"],
        hs_base_url=hs_base_url,
    )


@app.route("/_openhost/community/onboarding/status")
def community_onboarding_status():
    """JSON status the onboarding page polls after the post-onboarding restart.

    The page keeps its spinner until the community-space join has actually
    landed (``done`` True), so the shared space is already visible the moment
    the user reaches the chat client, instead of appearing a bit later. When no
    join was requested, or this boot's join attempts gave up, ``done`` is True
    as well so the page forwards instead of hanging.

    This handler is intentionally cheap: it only reads the settings file and
    does NOT call Synapse. The background join worker records Synapse readiness
    (``community_synapse_ready``) and join progress into settings, so a slow or
    still-restarting Synapse can never stall these frequent status polls. While
    the app is mid-restart the whole endpoint is simply unreachable (the client
    keeps polling), which is the real "server not back yet" signal.
    """
    settings = load_settings()
    synapse_up = bool(settings.get("community_synapse_ready"))
    want_join = bool(settings.get("community_room_alias")) and (
        settings.get("community_join_pending") or settings.get("community_joined")
    )
    join_pending = bool(settings.get("community_join_pending"))
    joined = bool(settings.get("community_joined"))
    join_failed = bool(settings.get("community_join_failed"))

    # "done" means the page can forward to the chat client:
    #   * Synapse is back up, AND
    #   * either the join isn't pending (joined or opted out), OR this boot's
    #     join attempts gave up (join_failed) so we forward rather than hang
    #     forever. A failed join stays pending so the next boot retries in the
    #     background; the user just sees the space appear a little later.
    done = synapse_up and (not join_pending or join_failed)

    if not synapse_up:
        phase = "starting"
    elif join_failed and not joined:
        phase = "join_failed"
    elif join_pending:
        phase = "joining"
    else:
        phase = "ready"

    return {
        "synapse_up": synapse_up,
        "want_join": want_join,
        "join_pending": join_pending,
        "joined": joined,
        "join_failed": join_failed,
        "phase": phase,
        "done": done,
    }


def _update_settings(**changes: object) -> dict:
    """Read-modify-write helper so the boot worker's flag updates don't clobber
    concurrent writes (e.g. an admin save) between load and save."""
    settings = load_settings()
    settings.update(changes)
    save_settings(settings)
    return settings


def _wait_for_synapse(attempts: int = 120, interval: float = 1.0) -> bool:
    """Poll Synapse's client API until it answers. ~2 min at 1s by default."""
    import time

    for _ in range(attempts):
        try:
            _synapse_request("GET", "/_matrix/client/versions", timeout=10)
            return True
        except SSOError:
            time.sleep(interval)
    return False


def _join_community_with_retries(username: str, room_alias: str) -> bool:
    """Join the owner to the community space, retrying through federation warmup.

    Returns True once the account is confirmed a member of the space. After each
    join attempt (and even if the attempt raised, e.g. our HTTP timeout fired
    while Synapse kept going) we verify actual membership via joined_rooms, so a
    slow-but-successful federated join is recognised instead of being retried
    forever or falsely marked failed.
    """
    import time

    try:
        token = sso_login_for_owner(username)["access_token"]
    except SSOError as exc:
        app.logger.error("community join: owner SSO login failed; retry next boot: %s", exc)
        return False

    # ~3 min of attempts: a cold federated join can take a while on first
    # contact. Each POST /join gets a generous 60s timeout; between attempts we
    # verify membership (in case a timed-out POST actually landed) before
    # sleeping a short backoff.
    last_exc = None
    for attempt in range(30):
        try:
            room_id = _join_room_with_token(token, room_alias, timeout=60)
            app.logger.info("joined community room %s (%s)", room_alias, room_id)
            return True
        except SSOError as exc:
            last_exc = exc
            app.logger.warning("community join attempt %d failed: %s", attempt + 1, exc)
        # Whether the POST succeeded, timed out, or errored, confirm membership:
        # Synapse may have completed the federated join after our client gave up.
        if _is_joined_to_space(token, room_alias):
            app.logger.info("community join confirmed via membership check")
            return True
        time.sleep(2)
    app.logger.error("community join failed after retries (will retry next boot): %s", last_exc)
    return False


def _on_boot_worker() -> None:
    """Per-boot background worker.

    1. Marks Synapse readiness in settings once its client API answers, so the
       onboarding status endpoint (which only reads settings) can report the
       server is back up without calling Synapse itself.
    2. If a community-space join is pending (federation was just enabled during
       onboarding), completes it over federation and clears the pending flag.

    Runs once per boot. Idempotent: joining an already-joined space is a no-op.
    """
    # Reset per-boot transient flags at the very start so a fresh boot never
    # shows a stale "ready"/"failed" to the onboarding page.
    _update_settings(community_synapse_ready=False, community_join_failed=False)

    if not _wait_for_synapse():
        app.logger.error("boot worker: Synapse never became reachable this boot")
        return
    _update_settings(community_synapse_ready=True)

    settings = load_settings()
    if not settings.get("community_join_pending"):
        return
    room_alias = settings.get("community_room_alias", "")
    username = settings.get("community_username") or "owner"
    if not room_alias:
        _update_settings(community_join_pending=False)
        return

    if _join_community_with_retries(username, room_alias):
        _update_settings(community_joined=True, community_join_pending=False, community_join_failed=False)
    else:
        # Mark this boot as failed so the onboarding page stops spinning and
        # forwards; leave pending set so the next boot retries in the background.
        _update_settings(community_join_failed=True)


def _cli_apply_settings() -> int:
    """Patch homeserver.yaml (registration + federation) from openhost_settings.json.

    Invoked by start.sh on every boot ("python3 admin.py apply-settings") so the
    boot-time config patching and the admin-UI-time patching share ONE
    implementation (apply_settings_to_yaml) — no duplicated, drifting regex.
    """
    settings = load_settings()
    if not HOMESERVER_YAML.exists():
        sys.stderr.write(f"apply-settings: {HOMESERVER_YAML} not found\n")
        return 0  # nothing to patch yet; not fatal
    try:
        apply_settings_to_yaml(settings)
    except OSError as exc:
        sys.stderr.write(f"apply-settings: {exc}\n")
        return 1
    sys.stderr.write(
        "apply-settings: federation_enabled=%s open_registration=%s\n"
        % (settings["federation_enabled"], settings["open_registration"])
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "apply-settings":
        raise SystemExit(_cli_apply_settings())

    threading.Thread(target=_on_boot_worker, daemon=True).start()
    port = int(os.environ.get("ADMIN_PORT", "8009"))
    app.run(host="127.0.0.1", port=port, debug=False)
