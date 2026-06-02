Matrix Synapse homeserver for OpenHost. Runs as a single Docker container:

- Synapse latest (Matrix homeserver for end-to-end encrypted communication)
- Federation disabled by default (personal/team server use case)
- Open registration enabled by default (no email verification required)
- SQLite database (no external database required)
- Persistent data in OpenHost's app_data directory

## How it works

On first boot, the container:
1. Generates a `homeserver.yaml` config with the server name derived from OpenHost environment variables (`<app_name>.<zone_domain>`, e.g. `synapse.andrew.host.imbue.com`)
2. Generates signing keys
3. Enables open registration without email verification
4. Disables the federation listener (removes `federation` from the listener names list) and blocks federation via an empty `federation_domain_whitelist`
5. Appends relaxed rate limits suitable for a small personal server
6. Generates a Caddyfile with a `.well-known/matrix/client` response for client auto-discovery
7. Starts Caddy (serves well-known, rewrites Host from X-Forwarded-Host, proxies to Synapse on port 8008) and Synapse

On subsequent boots, `start.sh` patches `public_baseurl` and `media_store_path` in the existing config but does not regenerate from scratch.

## Federation

Federation is **disabled** by default. The start script does two things to ensure this:

1. Removes `federation` from the Synapse listener names (only the `client` API is served on port 8008)
2. Appends `federation_domain_whitelist: []` to `homeserver.yaml`, which blocks all server-to-server communication

The Caddyfile only serves `/.well-known/matrix/client` for client auto-discovery. There is no `/.well-known/matrix/server` endpoint. Port 8448 (the standard Matrix federation port) is not exposed.

To enable federation, you would need to edit `homeserver.yaml` in the app data directory **and** modify `start.sh` to stop overwriting the federation settings on each boot. This is not a supported configuration today.

## Registration

Open registration is enabled on first boot (`enable_registration: true` and `enable_registration_without_verification: true` in `homeserver.yaml`). Anyone who can reach the Matrix client API can create an account without email verification.

To disable registration, edit `homeserver.yaml` in the app data directory and set `enable_registration: false`. This change persists across restarts since `start.sh` only sets this value on first boot.

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

All other paths require OpenHost authentication.

## Configuration

`start.sh` auto-configures Synapse at runtime. Settings applied on first boot:
- `server_name` derived from `OPENHOST_ZONE_DOMAIN` and `OPENHOST_APP_NAME`
- `public_baseurl` set for correct URL generation
- Client-only listener on port 8008 (federation listener removed)
- `federation_domain_whitelist: []` (federation blocked)
- `enable_registration: true` and `enable_registration_without_verification: true`
- SQLite database at `$OPENHOST_APP_DATA_DIR/homeserver.db`
- Relaxed rate limits (`rc_login` set to 10/s with burst of 50)

Settings patched on every boot (even if `homeserver.yaml` already exists):
- `public_baseurl` (updated to match the current domain)
- `media_store_path` (pointed at persistent storage)
- Federation listener removal (re-applied if the config still has `client, federation`)
- `federation_domain_whitelist: []` (appended if not present)
- Rate limits (appended if not present)

To customize settings, edit `homeserver.yaml` in the app's data directory. Be aware that the settings listed above as "patched on every boot" will be reapplied by `start.sh` on restart, so changes to those specific values will be overwritten.

## Files

- `Dockerfile` -- extends the official Synapse image with Caddy for Host header rewriting and .well-known serving
- `start.sh` -- generates config on first boot, patches config on subsequent boots, starts Caddy and Synapse
- `Caddyfile.template` -- Caddy config template; start.sh substitutes server name and base URL at runtime. Serves `.well-known/matrix/client` and reverse-proxies to Synapse on port 8008
- `openhost.toml` -- OpenHost app manifest (2048 MB RAM, 2 CPU cores, app_data storage)
