#!/usr/bin/env python3
"""
OpenHost Synapse admin + first-run onboarding

Serves:
  - /                    first-run redirect: onboarding (until done)
  - /_openhost/onboarding  create the owner's Matrix account (username +
                           password) and optionally join the OpenHost community
  - /_openhost/admin       manage federation, registration, community room alias

Settings are persisted to openhost_settings.json in the Synapse data dir.
On change, homeserver.yaml is patched and Synapse is sent SIGHUP to reload.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import threading
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, redirect, render_template_string, request

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "openhost_settings.json"
HOMESERVER_YAML = DATA_DIR / "homeserver.yaml"

# Synapse listens on localhost:8008 inside the container. The admin/onboarding
# code talks to it directly here, bypassing the OpenHost router + zone_auth
# (which only gates the public-facing subdomain, not intra-container localhost
# calls) — in particular to create the owner's account via the shared secret.
SYNAPSE_BASE = os.environ.get("SYNAPSE_LOCAL_URL", "http://localhost:8008")
# Where we persist the admin service-account access token used to join the
# community room over federation on the owner's behalf.
ADMIN_STATE_FILE = DATA_DIR / "openhost_sso.json"
# Synapse reserves the leading "_" localpart for appservices, so it can't start
# with an underscore. Keep it distinctive to avoid clashing with real users.
ADMIN_SERVICE_USER = "openhost-admin-svc"

# The canonical OpenHost community room. This is the room the "join the
# community" flow joins over federation. It lives on the OpenHost community hub
# homeserver (a plain federating Synapse) and is deliberately referenced only by
# this alias string — the hub itself is separate infrastructure. Overridable per
# instance via the admin console or the OPENHOST_COMMUNITY_ROOM_ALIAS env var.
DEFAULT_COMMUNITY_ROOM_ALIAS = "#openhost-community-general:matrix.openhost.imbue.com"

DEFAULTS = {
    "federation_enabled": False,
    "open_registration": True,
    # First-run onboarding: on first load of the app root, the owner creates
    # their Matrix account (username + password) and optionally joins the
    # OpenHost community. Once done, the root proxies to the bare homeserver.
    "onboarded": False,
    "community_joined": False,
    # The single string defining which room the "join the community" flow joins.
    # Defaults to the canonical OpenHost community room; can be overridden.
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
    Handles the inline-list format: names: [client] / names: [client, federation]
    """
    def replace_names(m: re.Match) -> str:
        prefix = m.group(1)
        return prefix + "[client, federation]" if enabled else prefix + "[client]"

    # Match: (optional dash + spaces) names: [(client)(, federation)?]
    pattern = r"((?:-\s+)?names:\s*\[)client(?:,\s*federation)?\]"
    new = re.sub(pattern, replace_names, content)
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
        app.logger.error("reload_synapse: could not scan /proc: %s", exc)
    return pids


def reload_synapse() -> bool:
    """Send SIGHUP to Synapse so it reloads config. Returns True on success."""
    try:
        pids = _find_synapse_pids()
        if not pids:
            app.logger.warning("reload_synapse: no Synapse processes found")
            return False
        for pid in pids:
            os.kill(pid, signal.SIGHUP)
        app.logger.info("reload_synapse: sent SIGHUP to pids %s", pids)
        return True
    except (ValueError, ProcessLookupError, PermissionError) as exc:
        app.logger.error("reload_synapse: failed to reload Synapse: %s", exc)
        return False


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
    <p class="subtitle">Manage federation and registration settings for this Matrix server.</p>

    {% if message %}
      <div class="alert alert-success">{{ message }}</div>
    {% endif %}
    {% if warning %}
      <div class="alert alert-warning">{{ warning }}</div>
    {% endif %}

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

      <div class="card">
        <div class="setting-info">
          <h2>OpenHost community room</h2>
          <p>The room the first-run "join the community" option connects to over
             federation. Leave blank to hide the community-join option during
             onboarding.</p>
        </div>
        <div style="margin-top:1rem">
          <label for="community_room_alias" style="display:block;font-size:.85rem;color:#94a3b8;margin-bottom:.35rem">
            Community room alias (optional)</label>
          <input type="text" id="community_room_alias" name="community_room_alias"
            value="{{ settings.community_room_alias or '' }}"
            placeholder="#openhost-community-general:matrix.openhost.imbue.com"
            style="width:100%;padding:.5rem;border-radius:.4rem;border:1px solid #2d3348;background:#0d1117;color:#e2e8f0">
        </div>
      </div>

      <button type="submit" class="save-btn">Save &amp; Apply</button>
    </form>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Synapse admin helpers — talk to Synapse over localhost:8008 (inside the
# container, so not subject to the router's zone_auth) to create the owner's
# account during onboarding and, for the community-join opt-in, mint a token
# for the owner to join the federated community room on their behalf.
# ---------------------------------------------------------------------------


class SSOError(Exception):
    pass


def _synapse_request(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    url = f"{SYNAPSE_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
    if ADMIN_STATE_FILE.exists():
        try:
            return json.loads(ADMIN_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sso_state(state: dict) -> None:
    ADMIN_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    try:
        os.chmod(ADMIN_STATE_FILE, 0o600)  # admin token — restrict to owner
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
    localpart = f"{ADMIN_SERVICE_USER}-{unique}"
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


# Serialize owner-account creation so two concurrent onboarding submissions
# can't race. Onboarding is a one-time, single-owner action, so a process-wide
# lock is the simplest correct guard.
_onboard_lock = threading.Lock()


def create_owner_account(username: str, password: str) -> str:
    """Create the owner's Matrix account with the given password.

    Uses Synapse's shared-secret registration API (localhost, admin=True).
    Returns the full Matrix user id (@username:server). Raises SSOError on
    failure (e.g. the username is already taken).
    """
    with _onboard_lock:
        _shared_secret_register(username, password, admin=True)
        return f"@{username}:{_server_name()}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/_openhost/admin")
def index():
    settings = load_settings()
    return render_template_string(TEMPLATE, settings=settings, message=None, warning=None)


@app.route("/_openhost/admin/save", methods=["POST"])
def save():
    # Merge onto existing settings so flags not shown on this form (e.g.
    # onboarded, set by the first-run flow) are preserved.
    settings = load_settings()
    settings["federation_enabled"] = request.form.get("federation_enabled") == "1"
    settings["open_registration"] = request.form.get("open_registration") == "1"
    if "community_room_alias" in request.form:
        settings["community_room_alias"] = request.form.get("community_room_alias", "").strip()
    save_settings(settings)

    yaml_error = None
    try:
        if not HOMESERVER_YAML.exists():
            yaml_error = f"homeserver.yaml not found at {HOMESERVER_YAML}"
        else:
            apply_settings_to_yaml(settings)
    except OSError as exc:
        yaml_error = str(exc)

    reloaded = False if yaml_error else reload_synapse()

    warning = None
    if yaml_error:
        warning = f"Settings saved, but could not update homeserver.yaml: {yaml_error}"
    elif not reloaded:
        warning = (
            "Settings saved, but Synapse could not be reloaded automatically. "
            "Restart the app to apply changes."
        )

    return render_template_string(
        TEMPLATE,
        settings=settings,
        message="Settings saved. Restart the app to apply changes." if reloaded else None,
        warning=warning,
    )


def _join_community_room(username: str, room_alias: str) -> str:
    """Join the owner's account to a (federated) room by alias. Returns room_id.

    The Synapse admin "edit room membership" API only works when the admin is
    already in the target room, so it cannot join a *remote* (federated) room on
    the owner's behalf. Instead we mint a login token for the owner via the admin
    API (no password needed), then perform a normal client-side join with that
    token — a client join resolves the alias over federation and joins. Federation
    must already be enabled and active in the running Synapse for a remote alias
    to resolve.
    """
    admin_token = _get_admin_token()
    server = _server_name()
    user_id = f"@{username}:{server}"
    import urllib.parse

    # Admin API: mint a short-lived login token for the owner without their
    # password, then exchange it for an access token via m.login.token.
    login_token_resp = _synapse_request(
        "POST",
        f"/_synapse/admin/v1/users/{urllib.parse.quote(user_id, safe='')}/login",
        token=admin_token,
        body={},
    )
    owner_token = login_token_resp["access_token"]

    quoted = urllib.parse.quote(room_alias, safe="")
    # Client-side join as the owner: resolves the alias (over federation if
    # remote) and joins. Idempotent — joining an already-joined room is fine.
    resp = _synapse_request(
        "POST",
        f"/_matrix/client/v3/join/{quoted}",
        token=owner_token,
        body={},
    )
    return resp.get("room_id", "")


ONBOARDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Set up your Matrix account</title>
<style>
  *,*::before,*::after{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:2rem;min-height:100vh}
  .container{max-width:620px;margin:0 auto}
  h1{font-size:1.6rem;color:#f8fafc;margin-bottom:.25rem}
  .subtitle{color:#94a3b8;margin-bottom:1.5rem}
  .card{background:#1e2130;border:1px solid #2d3348;border-radius:.75rem;padding:1.5rem;margin-bottom:1rem}
  .card h2{font-size:1.05rem;color:#f1f5f9;margin:0 0 .5rem}
  .card p,li{color:#cbd5e1;font-size:.9rem;line-height:1.5}
  ul{margin:.5rem 0 0 1.1rem}
  label{display:block;font-size:.9rem;color:#f1f5f9;margin-bottom:.35rem}
  input[type=text],input[type=password]{width:100%;padding:.6rem;border-radius:.5rem;border:1px solid #2d3348;background:#0d1117;color:#e2e8f0;font-size:.95rem}
  .hint{color:#64748b;font-size:.8rem;margin-top:.35rem}
  .field{margin-bottom:1rem}
  .consent{display:flex;gap:.6rem;align-items:flex-start;margin-top:1rem}
  .consent input{margin-top:.2rem}
  .btn{display:block;width:100%;padding:.75rem;background:#6366f1;color:#fff;border:none;border-radius:.5rem;font-size:.95rem;font-weight:500;cursor:pointer;margin-top:1.25rem}
  .btn:hover{background:#4f46e5}
  .err{color:#f87171;font-size:.85rem;margin-top:.5rem}
</style></head>
<body><div class="container">
  <h1>Welcome to your Matrix server</h1>
  <p class="subtitle">Create your account to get started. This runs a private
     Matrix (Synapse) homeserver on your OpenHost instance.</p>

  {% if error %}<div class="err">{{ error }}</div>{% endif %}

  <form method="POST" action="/_openhost/onboarding">
    <div class="card">
      <div class="field">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" value="{{ suggested }}"
               pattern="[a-z0-9._=-]+" required autocomplete="off">
        <p class="hint">Lowercase letters, numbers, and . _ = - only. Your Matrix
           address will be <code>@&lt;username&gt;:{{ server_name }}</code>.</p>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required
               autocomplete="new-password" minlength="8">
        <p class="hint">At least 8 characters. You'll use this to sign in from a
           Matrix client such as Element.</p>
      </div>
      <div class="field">
        <label for="confirm_password">Confirm password</label>
        <input type="password" id="confirm_password" name="confirm_password"
               required autocomplete="new-password">
      </div>
    </div>

    {% if community_room_alias %}
    <div class="card">
      <h2>Join the OpenHost community?</h2>
      <p>Optionally join the OpenHost community room
         (<code>{{ community_room_alias }}</code>) to chat with other OpenHost
         users. This <strong>enables federation</strong> so your server can reach
         the community's server. Leave it off to keep your server fully private;
         you can enable federation later from the admin console.</p>
      <label class="consent">
        <input type="checkbox" name="join_community" value="1">
        <span>Yes, enable federation and join the OpenHost community room.</span>
      </label>
    </div>
    {% endif %}

    <button type="submit" class="btn">Create account</button>
  </form>
</div></body></html>
"""

# Shown once onboarding is complete: connection instructions for Element.
CONNECT_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connect with Element</title>
<style>
  *,*::before,*::after{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:2rem;min-height:100vh}
  .container{max-width:640px;margin:0 auto}
  h1{font-size:1.55rem;color:#f8fafc;margin-bottom:.25rem}
  .subtitle{color:#94a3b8;margin-bottom:1.5rem}
  .card{background:#1e2130;border:1px solid #2d3348;border-radius:.75rem;padding:1.5rem;margin-bottom:1rem}
  .card h2{font-size:1.05rem;color:#f1f5f9;margin:0 0 .75rem}
  .card p,li{color:#cbd5e1;font-size:.9rem;line-height:1.55}
  ol{margin:.25rem 0 0 1.2rem}
  li{margin-bottom:.4rem}
  code{color:#a5b4fc;background:#0d1117;padding:.15rem .4rem;border-radius:.3rem;font-size:.85rem}
  .kv{display:flex;justify-content:space-between;gap:1rem;padding:.5rem 0;border-bottom:1px solid #2d3348}
  .kv:last-child{border-bottom:none}
  .kv .k{color:#94a3b8;font-size:.85rem}
  .kv .v{color:#e2e8f0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem;text-align:right;word-break:break-all}
  .ok{background:#052e16;border:1px solid #166534;color:#4ade80;border-radius:.5rem;padding:.6rem .9rem;font-size:.85rem;margin-bottom:1rem}
  a{color:#a5b4fc}
</style></head>
<body><div class="container">
  <h1>Your account is ready</h1>
  <p class="subtitle">Connect to your Matrix server from any Matrix client. Below
     are step-by-step instructions for Element.</p>

  {% if community_note %}<div class="ok">{{ community_note }}</div>{% endif %}

  <div class="card">
    <h2>Your connection details</h2>
    <div class="kv"><span class="k">Homeserver URL</span><span class="v">{{ homeserver_url }}</span></div>
    <div class="kv"><span class="k">Matrix ID</span><span class="v">{{ user_id }}</span></div>
    <div class="kv"><span class="k">Username</span><span class="v">{{ username }}</span></div>
    <div class="kv"><span class="k">Password</span><span class="v">the password you just set</span></div>
  </div>

  <div class="card">
    <h2>Set up Element</h2>
    <ol>
      <li>Get Element: use the web app at <a href="https://app.element.io" target="_blank" rel="noopener">app.element.io</a>,
          or install the desktop/mobile app from
          <a href="https://element.io/download" target="_blank" rel="noopener">element.io/download</a>.</li>
      <li>On the Element welcome screen, click <strong>Sign in</strong>.</li>
      <li>Under the server selector, click <strong>Edit</strong> (next to "Homeserver"),
          choose <strong>Other homeserver</strong>, and enter:
          <br><code>{{ homeserver_url }}</code></li>
      <li>Sign in with your <strong>username</strong> (<code>{{ username }}</code>)
          and the password you just set.</li>
      <li>That's it — you're connected to <code>{{ server_name }}</code>.</li>
    </ol>
  </div>

  {% if community_room_alias %}
  <div class="card">
    <h2>Find the community</h2>
    <p>Once signed in, open a room by alias and enter
       <code>{{ community_room_alias }}</code> to join the OpenHost community
       (requires federation to be enabled).</p>
  </div>
  {% endif %}
</div></body></html>
"""

# Matrix localpart grammar technically allows a wider set, but Synapse's default
# user_id validation is stricter and '/' in particular breaks account creation and
# URL handling. Restrict to the safe, portable subset.
_USERNAME_RE = re.compile(r"^[a-z0-9._=-]+$")


def _public_base_url() -> str:
    """The public HTTPS base URL of this app (no trailing slash)."""
    base = os.environ.get("OPENHOST_APP_PUBLIC_URL") or os.environ.get("PUBLIC_BASEURL")
    if base:
        return base.rstrip("/")
    try:
        return f"https://{_server_name()}"
    except SSOError:
        return ""


@app.route("/")
def root():
    """First-run entrypoint. Caddy routes the exact root path here (:8009).

    Before onboarding, redirect to the onboarding flow. After onboarding, there
    is nothing to show at the root for a plain homeserver, so redirect to the
    admin console. (Users connect to the homeserver from a Matrix client such
    as Element using the instructions shown at the end of onboarding.)
    """
    settings = load_settings()
    if settings.get("onboarded", False):
        return redirect("/_openhost/admin", code=302)
    return redirect("/_openhost/onboarding", code=302)


@app.route("/_openhost/onboarding", methods=["GET", "POST"])
def onboarding():
    """First-run account-creation flow (owner-only; zone_auth gates the subdomain).

    Creates the owner's Matrix account with a username + password and, if opted
    in, enables federation and joins the OpenHost community room. One-time: once
    onboarded, re-visiting redirects to the connection instructions.
    """
    settings = load_settings()
    server = ""
    try:
        server = _server_name()
    except SSOError:
        pass
    room_alias = settings.get("community_room_alias", "")

    # One-time: after onboarding, show the connection instructions instead.
    if settings.get("onboarded", False):
        return render_template_string(
            CONNECT_TEMPLATE,
            homeserver_url=_public_base_url(),
            user_id=f"@{settings.get('owner_username', '')}:{server}",
            username=settings.get("owner_username", ""),
            server_name=server,
            community_room_alias=room_alias,
            community_note=None,
        )

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        join_community = request.form.get("join_community") == "1"

        error = None
        if not username or not _USERNAME_RE.match(username) or username.startswith("_"):
            error = "Invalid username. Use lowercase letters, numbers, and . _ = - (not starting with _)."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        if error:
            return render_template_string(
                ONBOARDING_TEMPLATE, error=error, suggested=username,
                server_name=server, community_room_alias=room_alias,
            )

        # Create the owner's Matrix account with their chosen password.
        try:
            user_id = create_owner_account(username, password)
        except SSOError as exc:
            app.logger.error("onboarding: account creation failed: %s", exc)
            msg = "Could not create the account. The username may already be taken."
            return render_template_string(
                ONBOARDING_TEMPLATE, error=msg, suggested=username,
                server_name=server, community_room_alias=room_alias,
            )

        settings = load_settings()
        settings["owner_username"] = username
        settings["onboarded"] = True

        community_note = None
        # Opt-in community join: only if ticked AND a room alias is configured.
        # A remote (federated) alias requires federation to be active in the
        # running Synapse, which only takes effect after an app restart. So we
        # record the intent + turn federation on; the join completes on the next
        # boot via _complete_pending_community_join.
        if join_community and room_alias:
            settings["federation_enabled"] = True
            settings["community_join_pending"] = True
            save_settings(settings)
            try:
                apply_settings_to_yaml(settings)
                community_note = (
                    "Federation enabled to join the OpenHost community room. "
                    "Restart this app from your OpenHost dashboard to activate it; "
                    "your account will join the community room automatically."
                )
            except OSError as exc:
                app.logger.error("could not apply federation setting: %s", exc)
                community_note = (
                    "Account created, but federation could not be enabled to join "
                    "the community. You can retry from the admin console."
                )
        else:
            save_settings(settings)

        return render_template_string(
            CONNECT_TEMPLATE,
            homeserver_url=_public_base_url(),
            user_id=user_id,
            username=username,
            server_name=server,
            community_room_alias=room_alias,
            community_note=community_note,
        )

    return render_template_string(
        ONBOARDING_TEMPLATE, error=None, suggested="",
        server_name=server, community_room_alias=room_alias,
    )


def _complete_pending_community_join() -> None:
    """Background worker: if a community join is pending (federation was just
    enabled during onboarding), wait for Synapse to come up, join the configured
    room over federation, and clear the pending flag. Runs once per boot."""
    import time

    settings = load_settings()
    if not settings.get("community_join_pending"):
        return
    room_alias = settings.get("community_room_alias", "")
    username = settings.get("owner_username") or ""
    if not room_alias or not username:
        return

    # Wait for Synapse's client API to be reachable (up to ~2 min).
    reachable = False
    for _ in range(60):
        try:
            _synapse_request("GET", "/_matrix/client/versions")
            reachable = True
            break
        except SSOError:
            time.sleep(2)
    if not reachable:
        app.logger.error("pending community join: Synapse never became reachable; retry next boot")
        return

    # Retry the join a handful of times within this boot: a remote (federated)
    # alias may not resolve immediately after Synapse starts (federation/DNS
    # warmup). If all attempts fail, leave the pending flag set so the next boot
    # retries.
    last_exc = None
    for attempt in range(10):
        try:
            room_id = _join_community_room(username, room_alias)
            settings = load_settings()
            settings["community_joined"] = True
            settings["community_join_pending"] = False
            save_settings(settings)
            app.logger.info("joined community room %s (%s)", room_alias, room_id)
            return
        except SSOError as exc:
            last_exc = exc
            app.logger.warning("community join attempt %d failed: %s", attempt + 1, exc)
            time.sleep(6)
    app.logger.error("pending community join failed after retries (will retry next boot): %s", last_exc)


if __name__ == "__main__":
    import threading

    threading.Thread(target=_complete_pending_community_join, daemon=True).start()
    port = int(os.environ.get("ADMIN_PORT", "8009"))
    app.run(host="127.0.0.1", port=port, debug=False)
