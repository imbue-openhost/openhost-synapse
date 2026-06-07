#!/usr/bin/env python3
"""
OpenHost Synapse Admin UI

Serves a simple web interface at /_openhost/admin for managing:
  - Federation (enable/disable)
  - Open registration (enable/disable)

Settings are persisted to openhost_settings.json in the Synapse data dir.
On change, homeserver.yaml is patched and Synapse is sent SIGHUP to reload.
"""

import json
import os
import re
import signal
import sys
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "openhost_settings.json"
HOMESERVER_YAML = DATA_DIR / "homeserver.yaml"

DEFAULTS = {
    "federation_enabled": False,
    "open_registration": True,
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


def _set_federation_domain_whitelist(content: str, enabled: bool) -> str:
    """
    When federation is disabled, set federation_domain_whitelist: []
    When enabled, remove the whitelist restriction entirely.
    """
    pattern = r"^# Federation disabled.*\nfederation_domain_whitelist:.*$"
    pattern_simple = r"^federation_domain_whitelist:.*$"

    if enabled:
        # Remove any federation_domain_whitelist line (and its comment)
        content = re.sub(
            r"\n# Federation disabled[^\n]*\nfederation_domain_whitelist:[^\n]*",
            "",
            content,
            flags=re.MULTILINE,
        )
        content = re.sub(pattern_simple, "", content, flags=re.MULTILINE)
        # Also re-enable the federation listener if it was removed
        content = re.sub(
            r"- names: \[client\]",
            "- names: [client, federation]",
            content,
        )
    else:
        # Disable federation listener
        content = re.sub(
            r"- names: \[client, federation\]",
            "- names: [client]",
            content,
        )
        # Add/update whitelist
        if re.search(pattern_simple, content, flags=re.MULTILINE):
            content = re.sub(
                pattern_simple,
                "federation_domain_whitelist: []",
                content,
                flags=re.MULTILINE,
            )
        else:
            content = content.rstrip() + (
                "\n\n# Federation disabled — personal server.\n"
                "federation_domain_whitelist: []\n"
            )
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
    content = _set_federation_domain_whitelist(content, settings["federation_enabled"])

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

      <button type="submit" class="save-btn">Save &amp; Apply</button>
    </form>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/_openhost/admin")
def index():
    settings = load_settings()
    return render_template_string(TEMPLATE, settings=settings, message=None, warning=None)


@app.route("/_openhost/admin/save", methods=["POST"])
def save():
    settings = {
        "federation_enabled": request.form.get("federation_enabled") == "1",
        "open_registration": request.form.get("open_registration") == "1",
    }
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


if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_PORT", "8009"))
    app.run(host="127.0.0.1", port=port, debug=False)
