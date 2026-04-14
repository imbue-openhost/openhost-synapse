Matrix Synapse homeserver for OpenHost with federation support. Runs as a single Docker container:

- Synapse latest (Matrix homeserver for federated, end-to-end encrypted communication)
- Federation enabled out of the box via `.well-known` delegation
- SQLite database (no external database required)
- Persistent data in OpenHost's app_data directory

## How it works

On first boot, the container:
1. Generates a `homeserver.yaml` config with the correct server name derived from OpenHost environment variables
2. Generates signing keys for federation
3. Enables registration (first user to register can be made admin)
4. Generates a Caddyfile with `.well-known/matrix/server` and `.well-known/matrix/client` responses for federation discovery
5. Starts Caddy (serves well-known, proxies to Synapse) and Synapse

## Federation

Federation works automatically. Other Matrix homeservers discover this server via `.well-known` delegation:

- `/.well-known/matrix/server` returns `{"m.server": "synapse.yourdomain.com:443"}`, telling federating servers to connect over standard HTTPS
- `/.well-known/matrix/client` returns the homeserver base URL for client auto-discovery

No special ports (8448) or DNS SRV records are needed. All federation traffic goes through the existing OpenHost HTTPS (:443) routing.

User IDs will look like `@user:synapse.yourdomain.com`.

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
- `/.well-known/matrix/` -- Matrix server discovery (federation + client)

All other paths require OpenHost authentication.

## Configuration

`start.sh` auto-configures Synapse at runtime. Key settings:
- `server_name` derived from `OPENHOST_ZONE_DOMAIN` and `OPENHOST_APP_NAME`
- `public_baseurl` set for correct URL generation
- Federation listener on same port as client API (8008)
- SQLite database (no external DB needed)
- Registration enabled without email verification
- Federation signing keys auto-generated

To customize settings, edit `homeserver.yaml` in the app's data directory.

## Files

- `Dockerfile` -- extends the official Synapse image, adds Caddy
- `start.sh` -- generates config on first boot, then launches Synapse
- `Caddyfile.template` -- template for Caddy config; start.sh fills in server name and base URL at runtime
- `openhost.toml` -- OpenHost app manifest
