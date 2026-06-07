Matrix Synapse homeserver for OpenHost with matrix-authentication-service (MAS). Runs as a single Docker container:

- Synapse latest (Matrix homeserver for end-to-end encrypted communication)
- matrix-authentication-service v1.18.0 (OIDC/OAuth2 authentication provider)
- PostgreSQL (for MAS; Synapse continues using SQLite)
- Federation disabled by default (personal/team server use case)
- Admin UI at `/` and `/_openhost/admin` for managing all server settings

## How it works

On first boot, the container:
1. Generates a `homeserver.yaml` config with the server name derived from OpenHost environment variables (`<app_name>.<zone_domain>`, e.g. `synapse.andrew.host.imbue.com`)
2. Generates signing keys
3. Creates `openhost_settings.json` with default settings
4. Applies settings from `openhost_settings.json` to `homeserver.yaml`
5. Generates a Caddyfile routing `/` and `/_openhost/*` to the Go admin server, and Matrix APIs to Synapse
6. Starts Caddy, the Go admin server, and Synapse in parallel
7. Once Synapse is ready, provisions an `openhost-admin` user and stores its access token for the admin UI

On subsequent boots, `start.sh` patches `public_baseurl` and `media_store_path` and re-applies all settings from `openhost_settings.json`.

## Admin UI

The admin UI is available at the root URL of the application (e.g. `https://synapse.andrew.host.imbue.com/`). It redirects to `/_openhost/admin`. All admin paths require OpenHost zone auth (owner-only).

The admin UI provides:
- **Dashboard** — live stats (users, rooms, server version, uptime)
- **Users** — list, search, create, deactivate, promote/demote admin, reset password, delete media
- **Rooms** — list, search, delete (with purge)
- **Registration Tokens** — create one-time or limited-use invite tokens (with expiry)
- **Settings** — configure federation, registration, rate limits, password policy, upload size

Changes are applied immediately via SIGHUP — no app restart required.

Settings are stored in `$OPENHOST_APP_DATA_DIR/openhost_settings.json`:
```json
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
```

## Federation

Federation is **disabled** by default. The start script does two things to ensure this:

1. Removes `federation` from the Synapse listener names (only the `client` API is served on port 8008)
2. Appends `federation_domain_whitelist: []` to `homeserver.yaml`, which blocks all server-to-server communication

The Caddyfile serves `/.well-known/matrix/client` for client auto-discovery and `/.well-known/matrix/server` for federation discovery (needed when federation is enabled).

To enable federation, use the admin UI Settings page.

## Registration

Open registration is enabled by default. Anyone who can reach the Matrix client API can create an account without email verification.

To disable registration or create invite tokens, use the admin UI.

An `openhost-admin` user is automatically created on first boot and used internally by the admin panel to call the Synapse Admin API.

## Deploying

Deploy via the OpenHost dashboard or CLI:

```bash
oh app deploy https://github.com/imbue-openhost/openhost-synapse --wait
```

The app will be available at `synapse.<zone_domain>` (e.g. `synapse.andrew.host.imbue.com`).

Connect with any Matrix client (Element, FluffyChat, etc.) using your server URL as the homeserver. User IDs will look like `@user:synapse.<zone_domain>`.

## Data

All persistent data lives in `$OPENHOST_APP_DATA_DIR/`:
- `homeserver.yaml` -- Synapse configuration (regenerated on first boot, patched on subsequent boots)
- `openhost_settings.json` -- Admin UI settings (source of truth for all settings)
- `admin_token` -- Access token for the openhost-admin user (used by the admin panel)
- `admin_password` -- Password for the openhost-admin user (used for token refresh on restart)
- `*.signing.key` -- Signing keys (back these up; losing them breaks room continuity)
- `homeserver.db` -- SQLite database (users, rooms, messages, etc.)
- `media_store/` -- Uploaded media and thumbnails

## Matrix Authentication Service (MAS)

MAS provides OIDC/OAuth2 authentication for Matrix clients that support the new auth spec. It runs alongside Synapse and handles login, logout, token refresh, and registration.

On first boot, MAS is provisioned automatically:
- PostgreSQL database initialized at `$OPENHOST_APP_DATA_DIR/mas/pgdata`
- RSA signing key generated at `$OPENHOST_APP_DATA_DIR/mas/keys/rsa.pem`
- Secrets generated at `$OPENHOST_APP_DATA_DIR/mas/shared_secret` and `encryption_secret`
- MAS config written to `$OPENHOST_APP_DATA_DIR/mas/mas.yaml`
- Synapse patched with `matrix_authentication_service:` config block

MAS admin interface is accessible at `/admin/` (owner-only via zone_auth).

## Resources

The `openhost.toml` requests 3072 MB RAM and 2 CPU cores (increased for PostgreSQL + MAS + Synapse).

## Public paths

The following paths are publicly accessible (no OpenHost zone auth required):
- `/_matrix/` -- Matrix client-server API
- `/_synapse/client/` -- Synapse-specific client endpoints (registration, password reset)
- `/.well-known/matrix/` -- Matrix client discovery

All other paths (including `/`, `/_openhost/admin`) require OpenHost authentication.

## Files

- `Dockerfile` -- Multi-stage build: Go admin server + Synapse + Caddy (no Python Flask dependency)
- `start.sh` -- Generates config on first boot, applies settings on every boot, starts Caddy, admin server, and Synapse
- `Caddyfile.template` -- Routes `/` and `/_openhost/*` to the Go admin server; proxies Matrix APIs to Synapse
- `admin/` -- Go HTTP server serving the admin UI on port 8009
- `openhost.toml` -- OpenHost app manifest (3072 MB RAM, 2 CPU cores, app_data storage)
- `mas/mas.yaml.template` -- MAS configuration template (populated by start.sh)
