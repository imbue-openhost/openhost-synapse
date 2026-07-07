Matrix Synapse homeserver for OpenHost. Runs as a single Docker container:

- Synapse latest (Matrix homeserver for end-to-end encrypted communication)
- Federation disabled by default (personal/team server use case)
- Open registration enabled by default (no email verification required)
- SQLite database (no external database required)
- Persistent data in OpenHost's app_data directory
- Admin UI at `/_openhost/admin` for managing accounts, federation and registration
- Built-in **chat client** (Cinny) served at the app root, with owner
  single-sign-on and a first-run onboarding flow — always on

## Chat client

The bundled [Cinny](https://cinny.in) web client is served at the app's URL,
pinned to this homeserver (custom homeservers disabled). It's always available;
there is no toggle to enable it.

- The OpenHost **owner is auto-logged-in** via SSO: opening the app with no
  existing session runs a first-run **onboarding** flow. Onboarding lets you
  create **as many accounts as you like** (each with its own username and
  password), pick which account signs you in automatically, and — optionally —
  join the wider OpenHost community room. It also explains the
  federation/legal/public-reachability considerations.
- After onboarding, opening the app signs the chosen owner account straight into
  the client.
- The web client and onboarding are gated by OpenHost zone auth, so only the
  owner can reach them. The Matrix APIs (`/_matrix`, `.well-known`) stay public
  as usual, so the accounts you create can sign in from any Matrix client too.

You can create additional accounts at any time from the admin UI
(`/_openhost/admin`), and share the app URL + credentials with the people you
want to invite.

Relevant settings in `openhost_settings.json`:

```json
{
  "community_onboarded": false,
  "community_username": "owner"
}
```

Implementation notes:

- SSO mints a Matrix session for the owner by talking to Synapse over
  `localhost:8008` (bypassing the router/zone-auth): it registers a one-time
  admin service account via Synapse's registration shared secret, ensures the
  owner's user exists, sets a fresh ephemeral password, and performs a normal
  `m.login.password` to obtain a real `device_id` + access token. Only the
  service account's access token (never a password) is persisted, in
  `openhost_sso.json` (mode 0600).
- Accounts created during onboarding / from the admin UI are registered via
  Synapse's shared-secret admin register API with the username and password you
  provide.
- The client is handed the owner session by a small bootstrap page that seeds
  Cinny's localStorage session keys and redirects into the app.
- The Caddy routing and web client config are rendered by `start.sh` on boot.

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

Accounts, federation and registration are managed via the admin UI at `/_openhost/admin` (e.g. `https://synapse.andrew.host.imbue.com/_openhost/admin`). This page is only accessible to authenticated OpenHost users (zone auth gates it).

The UI provides:

- **Chat accounts** — create as many accounts as you like, each with a chosen
  username and password (optionally server admin). Creating an account takes
  effect immediately and does not restart the app.
- **Open Registration** — allow anyone to create an account without an invitation
- **Federation** — allow this server to communicate with other Matrix homeservers

On save of Open Registration or Federation, the admin UI updates `openhost_settings.json`, patches `homeserver.yaml`, and **automatically restarts the app** so the changes take effect — no manual restart needed. See "Automatic restarts" below.

## Automatic restarts

Synapse only reads registration and federation settings at startup (SIGHUP just
reloads log config). Rather than asking the user to manually restart the app,
this app restarts itself:

- `start.sh` runs Synapse as a supervised child process (it does not `exec`
  into it). When the admin UI needs to apply a restart-only setting, it writes a
  restart sentinel and stops Synapse.
- Synapse exiting unblocks the supervisor, which then exits (PID 1). OpenHost
  runs containers with podman's `--restart=unless-stopped` policy, so the
  container is relaunched automatically and `start.sh` re-renders
  `homeserver.yaml` / the Caddyfile from the saved settings on the fresh boot.
- The admin UI and onboarding pages poll for the app coming back up and continue
  automatically, so the user never has to touch the OpenHost dashboard.

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

The Caddyfile serves `/.well-known/matrix/client` for client auto-discovery and `/.well-known/matrix/server` (pointing remote servers at port 443) so federation works over standard HTTPS without exposing port 8448. Federation is still blocked at the Synapse level until enabled.

To enable federation, use the admin UI at `/_openhost/admin`. The app restarts itself automatically to apply the change.

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

- `Dockerfile` -- extends the official Synapse image with Caddy, Flask, and the bundled Cinny web client
- `start.sh` -- generates config on first boot, applies settings from `openhost_settings.json` on every boot, renders the Cinny config, and supervises Caddy, the admin UI, and Synapse (exits to trigger an automatic restart when requested)
- `Caddyfile.template` -- Caddy config template; serves the Cinny client at the root, routes `/_openhost/*` to the admin/onboarding UI, and proxies `/_matrix` + `/_synapse` to Synapse
- `admin.py` -- Flask app serving the admin UI, onboarding, and owner SSO on port 8009
- `webclient-config.template.json` -- Cinny config template (homeserver pinned at boot)
- `openhost.toml` -- OpenHost app manifest (2048 MB RAM, 2 CPU cores, app_data storage)
