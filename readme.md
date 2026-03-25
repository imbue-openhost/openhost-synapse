Matrix Synapse homeserver for OpenHost. Runs as a single Docker container:

- Synapse latest (Matrix homeserver for decentralized, end-to-end encrypted communication)
- SQLite database (no external database required)
- Persistent data in OpenHost's app_data directory

## How it works

On first boot, the container:
1. Generates a `homeserver.yaml` config with the correct server name derived from OpenHost environment variables
2. Generates signing keys for federation
3. Enables registration (first user to register can be made admin)
4. Starts Synapse with sensible defaults

## Deploying

Deploy via the OpenHost router dashboard -- point it at this repo. The app will be available at `{app_name}.{zone_domain}` via subdomain routing (e.g. `synapse.zack.host.imbue.com`).

Connect with any Matrix client (Element, etc.) using your server URL as the homeserver.

## Data

All persistent data lives in `$OPENHOST_APP_DATA_DIR/`:
- `homeserver.yaml` -- Synapse configuration
- `*.signing.key` -- Federation signing keys (back these up!)
- `homeserver.db` -- SQLite database (users, rooms, messages, etc.)
- `media_store/` -- Uploaded media and thumbnails

## Resources

Needs ~512MB RAM and 0.5 CPU cores. The container image is ~300MB.

## Public paths

The following paths are publicly accessible (required for Matrix federation and client connections):
- `/_matrix/` -- Matrix client-server and server-server APIs
- `/_synapse/client/` -- Synapse-specific client endpoints
- `/.well-known/matrix/` -- Matrix server discovery

All other paths require OpenHost authentication.

## Configuration

`start.sh` auto-configures Synapse at runtime. Key settings:
- `server_name` derived from `OPENHOST_ZONE_DOMAIN` and `OPENHOST_APP_NAME`
- `public_baseurl` set for correct URL generation
- SQLite database (no external DB needed)
- Registration enabled without email verification
- Federation signing keys auto-generated

To customize settings, edit `homeserver.yaml` in the app's data directory.

## Files

- `Dockerfile` -- extends the official Synapse image, adds Caddy
- `start.sh` -- generates config on first boot, then launches Synapse
- `Caddyfile` -- rewrites Host header from X-Forwarded-Host for correct URL handling
- `openhost.toml` -- OpenHost app manifest
