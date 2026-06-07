Matrix Synapse homeserver for OpenHost. Runs as a single Docker container:

- Synapse latest (Matrix homeserver for end-to-end encrypted communication)
- Federation disabled by default (personal/team server use case)
- Open registration enabled by default (no email verification required)
- SQLite database (no external database required)
- Persistent data in OpenHost's app_data directory
- Admin UI at `/_openhost/admin` for managing federation and registration

## How it works

On first boot, the container:
1. Generates a `homeserver.yaml` config with the server name derived from OpenHost environment variables (`<app_name>.<zone_domain>`, e.g. `synapse.andrew.host.imbue.com`)
2. Generates signing keys
3. Creates `openhost_settings.json` with default settings (federation disabled, open registration enabled)
4. Applies settings from `openhost_settings.json` to `homeserver.yaml`
5. Appends relaxed rate limits suitable for a small personal server
6. Generates a Caddyfile with a `.well-known/matrix/client` response for client auto-discovery
7. Starts Caddy (serves well-known, routes admin UI, rewrites Host from X-Forwarded-Host, proxies to Synapse on port 8008), the admin UI, and Synapse

On subsequent boots, `start.sh` patches `public_baseurl` and `media_store_path` and re-applies all settings from `openhost_settings.json`.

## Admin UI

Settings for federation and registration are managed via the admin UI at `/_openhost/admin` (e.g. `https://synapse.andrew.host.imbue.com/_openhost/admin`). This page is only accessible to authenticated OpenHost users (zone auth gates it).

The UI has two toggles:

- **Open Registration** — allow anyone to create an account without an invitation
- **Federation** — allow this server to communicate with other Matrix homeservers

On save, the admin UI updates `openhost_settings.json` and patches `homeserver.yaml`. A restart of the app is required for the changes to take effect (Synapse's SIGHUP only reloads log config, not registration or federation settings).

Settings are stored in `$OPENHOST_APP_DATA_DIR/openhost_settings.json`:
```json
{
  "federation_enabled": false,
  "open_registration": true
}
```

## Federation

Federation is **disabled** by default. The start script does two things to ensure this:

1. Removes `federation` from the Synapse listener names (only the `client` API is served on port 8008)
2. Appends `federation_domain_whitelist: []` to `homeserver.yaml`, which blocks all server-to-server communication

The Caddyfile only serves `/.well-known/matrix/client` for client auto-discovery. There is no `/.well-known/matrix/server` endpoint. Port 8448 (the standard Matrix federation port) is not exposed.

To enable federation, use the admin UI at `/_openhost/admin`.

## Registration

Open registration is enabled by default. Anyone who can reach the Matrix client API can create an account without email verification.

To disable registration, use the admin UI at `/_openhost/admin`.

The first user to register can be promoted to admin via the Synapse admin API or by editing the database directly.

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
- `openhost_settings.json` -- Admin UI settings (source of truth for federation and registration)
- `*.signing.key` -- Signing keys (back these up; losing them breaks room continuity)
- `homeserver.db` -- SQLite database (users, rooms, messages, etc.)
- `media_store/` -- Uploaded media and thumbnails

## Resources

The `openhost.toml` requests 2048 MB RAM and 2 CPU cores. Synapse's memory usage grows with the number of joined rooms and active users. For a small personal server (a few users, limited rooms), the actual usage will be well under these limits.

## Public paths

The following paths are publicly accessible (no OpenHost zone auth required):
- `/_matrix/` -- Matrix client-server API
- `/_synapse/client/` -- Synapse-specific client endpoints (registration, password reset)
- `/.well-known/matrix/` -- Matrix client discovery

All other paths (including `/_openhost/admin`) require OpenHost authentication.

## Configuration

`start.sh` auto-configures Synapse at runtime. Settings applied on first boot:
- `server_name` derived from `OPENHOST_ZONE_DOMAIN` and `OPENHOST_APP_NAME`
- `public_baseurl` set for correct URL generation
- SQLite database at `$OPENHOST_APP_DATA_DIR/homeserver.db`
- Relaxed rate limits (`rc_login` set to 10/s with burst of 50)

Settings applied on every boot from `openhost_settings.json`:
- `public_baseurl` (updated to match the current domain)
- `media_store_path` (pointed at persistent storage)
- Federation listener and `federation_domain_whitelist` (from `federation_enabled` setting)
- `enable_registration` and `enable_registration_without_verification` (from `open_registration` setting)
- Rate limits (appended if not present)

To change federation or registration settings, use the admin UI. Direct edits to `homeserver.yaml` for these values will be overwritten on next boot.

## Files

- `Dockerfile` -- extends the official Synapse image with Caddy and Flask for the admin UI
- `start.sh` -- generates config on first boot, applies settings from `openhost_settings.json` on every boot, starts Caddy, admin UI, and Synapse
- `Caddyfile.template` -- Caddy config template; routes `/_openhost/admin` to the admin UI and proxies everything else to Synapse
- `admin.py` -- Flask app serving the admin UI on port 8009
- `openhost.toml` -- OpenHost app manifest (2048 MB RAM, 2 CPU cores, app_data storage)
