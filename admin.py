#!/usr/bin/env python3
"""
OpenHost Synapse Admin UI

Serves a simple web interface at /_openhost/admin for managing:
  - Federation (enable/disable)
  - Open registration (enable/disable)

Settings are persisted to openhost_settings.json in the Synapse data dir.
On change, homeserver.yaml is patched and Synapse is sent SIGHUP to reload.
"""

import hashlib
import hmac
import json
import os
import re
import signal
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, redirect, render_template_string, request

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "openhost_settings.json"
HOMESERVER_YAML = DATA_DIR / "homeserver.yaml"

# Synapse listens on localhost:8008 inside the container. The admin/SSO code
# talks to it directly here, bypassing the OpenHost router + zone_auth (which
# only gates the public-facing subdomain, not intra-container localhost calls).
SYNAPSE_BASE = os.environ.get("SYNAPSE_LOCAL_URL", "http://localhost:8008")
# Where we persist the SSO service account's admin access token.
SSO_STATE_FILE = DATA_DIR / "openhost_sso.json"
# Synapse reserves the leading "_" localpart for appservices, so it can't start
# with an underscore. Keep it distinctive to avoid clashing with real users.
SSO_ADMIN_USER = "openhost-sso-admin"

DEFAULTS = {
    "federation_enabled": False,
    "open_registration": True,
    "community_enabled": False,
    "community_onboarded": False,
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
        <div class="setting-row">
          <div class="setting-info">
            <h2>Community Chat</h2>
            <p>Serve the built-in web chat client at this app's URL and enable the
               OpenHost community first-run flow. Requires an app restart to apply.</p>
          </div>
          <label class="toggle-label">
            <input type="checkbox" name="community_enabled" value="1"
              {% if settings.community_enabled %}checked{% endif %}>
            <span class="slider"></span>
          </label>
        </div>
      </div>

      <button type="submit" class="save-btn">Save &amp; Apply</button>
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


def _shared_secret_register(username: str, password: str, admin: bool) -> dict:
    """Register a user via the shared-secret admin API (nonce + HMAC)."""
    nonce = _synapse_request("GET", "/_synapse/admin/v1/register")["nonce"]
    secret = _read_registration_shared_secret()
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
    mac.update(nonce.encode())
    mac.update(b"\x00")
    mac.update(username.encode())
    mac.update(b"\x00")
    mac.update(password.encode())
    mac.update(b"\x00")
    mac.update(b"admin" if admin else b"notadmin")
    body = {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": admin,
        "mac": mac.hexdigest(),
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
    import secrets
    return secrets.token_urlsafe(32)


def _get_admin_token() -> str:
    """Return an admin access token, creating the SSO service account on first use."""
    state = _load_sso_state()
    token = state.get("admin_token")
    if token:
        # Verify it still works; whoami requires a valid token.
        try:
            _synapse_request("GET", "/_matrix/client/v3/account/whoami", token=token)
            return token
        except SSOError:
            pass  # stale — re-register below
    result = _shared_secret_register(SSO_ADMIN_USER, _generate_password(), admin=True)
    token = result["access_token"]
    _save_sso_state({"admin_token": token, "admin_user_id": result.get("user_id")})
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


def sso_login_for_owner(username: str) -> dict:
    """Ensure the owner's Matrix user exists and mint a fresh session for it.

    We set the owner's password via the admin API, then perform a *normal*
    client login (m.login.password). Unlike the admin login endpoint, this
    creates a real device and returns a device_id — which the web client
    (matrix-js-sdk) requires to initialise a session; a session with an empty
    device_id is rejected and the client bounces to its own login screen.

    Returns {user_id, access_token, device_id, home_server}.
    """
    admin_token = _get_admin_token()
    server = _server_name()
    user_id = f"@{username}:{server}"

    # Set a fresh, ephemeral password for this login. Idempotent create-or-update.
    password = _generate_password()
    _synapse_request(
        "PUT",
        f"/_synapse/admin/v2/users/{user_id}",
        token=admin_token,
        # logout_devices=False so re-running SSO doesn't kill the owner's other
        # existing chat sessions (e.g. a mobile client) via the password change.
        body={"password": password, "logout_devices": False},
    )
    # Normal client login -> real device_id + access_token.
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

@app.route("/_openhost/admin")
def index():
    settings = load_settings()
    return render_template_string(TEMPLATE, settings=settings, message=None, warning=None)


@app.route("/_openhost/admin/save", methods=["POST"])
def save():
    # Merge onto existing settings so flags not shown on this form (e.g.
    # community_onboarded, set by the onboarding flow) are preserved.
    settings = load_settings()
    settings["federation_enabled"] = request.form.get("federation_enabled") == "1"
    settings["open_registration"] = request.form.get("open_registration") == "1"
    settings["community_enabled"] = request.form.get("community_enabled") == "1"
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


def _owner_matrix_username() -> str:
    """The Matrix localpart for the OpenHost owner. Configurable via onboarding;
    falls back to a stable default."""
    settings = load_settings()
    return settings.get("community_username") or "owner"


SSO_BOOTSTRAP_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Signing in…</title></head>
<body style="background:#0f1117;color:#e2e8f0;font-family:sans-serif;text-align:center;padding-top:20vh">
<p>Signing you in to community chat…</p>
<script>
try {
  localStorage.setItem("cinny_access_token", {{ access_token|tojson }});
  localStorage.setItem("cinny_device_id", {{ device_id|tojson }});
  localStorage.setItem("cinny_user_id", {{ user_id|tojson }});
  localStorage.setItem("cinny_hs_base_url", {{ hs_base_url|tojson }});
} catch (e) {}
window.location.replace("/");
</script>
</body></html>
"""


ONBOARDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Set up Community Chat</title>
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
  input[type=text]{width:100%;padding:.6rem;border-radius:.5rem;border:1px solid #2d3348;background:#0d1117;color:#e2e8f0;font-size:.95rem}
  .hint{color:#64748b;font-size:.8rem;margin-top:.35rem}
  .consent{display:flex;gap:.6rem;align-items:flex-start;margin-top:1rem}
  .consent input{margin-top:.2rem}
  .btn{display:block;width:100%;padding:.75rem;background:#6366f1;color:#fff;border:none;border-radius:.5rem;font-size:.95rem;font-weight:500;cursor:pointer;margin-top:1.25rem}
  .btn:hover{background:#4f46e5}
  .warn{background:#1c1003;border:1px solid #92400e;color:#fbbf24;border-radius:.5rem;padding:.75rem 1rem;font-size:.85rem;margin-bottom:1rem}
  .err{color:#f87171;font-size:.85rem;margin-top:.5rem}
</style></head>
<body><div class="container">
  <h1>Welcome to Community Chat</h1>
  <p class="subtitle">A Matrix-based chat client, built in to your OpenHost instance.</p>

  <div class="card">
    <h2>What this does</h2>
    <p>This runs a private Matrix homeserver on your instance and gives you a web
       chat client, signed in automatically as the instance owner. You can chat
       privately, invite others, and — if you opt in — join the wider OpenHost
       community room.</p>
  </div>

  <div class="card">
    <h2>Before you continue</h2>
    <ul>
      <li><strong>Federation is optional and off by default.</strong> Your server
          only talks to other Matrix servers (including the OpenHost community) if
          you enable federation later in the admin console.</li>
      <li><strong>Running a federated server may carry responsibilities</strong>
          (content, data, and legal considerations) that vary by jurisdiction.
          Review these before enabling federation.</li>
      <li><strong>Federation needs a publicly reachable instance.</strong> It will
          not work on setups without public inbound HTTPS (e.g. some tunnel-only
          configurations).</li>
    </ul>
  </div>

  {% if error %}<div class="err">{{ error }}</div>{% endif %}

  <form method="POST" action="/_openhost/community/onboarding">
    <div class="card">
      <label for="username">Choose your chat username</label>
      <input type="text" id="username" name="username" value="{{ suggested }}"
             pattern="[a-z0-9._=/-]+" required autocomplete="off">
      <p class="hint">Lowercase letters, numbers, and . _ = / - only. Your Matrix
         address will be <code>@&lt;username&gt;:{{ server_name }}</code>.</p>
      <label class="consent">
        <input type="checkbox" name="consent" value="1" required>
        <span>I understand what community chat does and the considerations above,
              and I want to enable it.</span>
      </label>
    </div>
    <button type="submit" class="btn">Enable community chat</button>
  </form>
</div></body></html>
"""

_USERNAME_RE = re.compile(r"^[a-z0-9._=/-]+$")


@app.route("/_openhost/community/onboarding", methods=["GET", "POST"])
def community_onboarding():
    """First-run consent + username flow. Owner-only (zone_auth gated)."""
    if not load_settings().get("community_enabled", False):
        return "Community chat is not enabled.", 404
    server = ""
    try:
        server = _server_name()
    except SSOError:
        pass

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        consent = request.form.get("consent") == "1"
        error = None
        if not consent:
            error = "Please confirm you understand before enabling."
        elif not username or not _USERNAME_RE.match(username) or username.startswith("_"):
            error = "Invalid username. Use lowercase letters, numbers, and . _ = / - (not starting with _)."
        if error:
            return render_template_string(
                ONBOARDING_TEMPLATE, error=error, suggested=username, server_name=server
            )
        settings = load_settings()
        settings["community_username"] = username
        settings["community_onboarded"] = True
        save_settings(settings)
        return redirect("/_openhost/community/login", code=302)

    return render_template_string(
        ONBOARDING_TEMPLATE, error=None, suggested="owner", server_name=server
    )


@app.route("/_openhost/community/login")
def community_login():
    """Owner SSO entrypoint: mint a Matrix session and hand it to the web client.

    Only reachable by the OpenHost owner (zone_auth gates this subdomain path).
    Redirects to onboarding on first run until consent is recorded.
    """
    settings = load_settings()
    if not settings.get("community_enabled", False):
        return "Community chat is not enabled.", 404
    if not settings.get("community_onboarded", False):
        return redirect("/_openhost/community/onboarding", code=302)
    username = _owner_matrix_username()
    try:
        session = sso_login_for_owner(username)
    except SSOError as exc:
        app.logger.error("community_login: SSO failed: %s", exc)
        return f"Could not sign in to chat: {exc}", 502
    # Cinny expects the homeserver base URL it will talk to (same origin).
    hs_base_url = f"https://{session['home_server']}"
    return render_template_string(
        SSO_BOOTSTRAP_TEMPLATE,
        access_token=session["access_token"],
        device_id=session["device_id"],
        user_id=session["user_id"],
        hs_base_url=hs_base_url,
    )


if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_PORT", "8009"))
    app.run(host="127.0.0.1", port=port, debug=False)
