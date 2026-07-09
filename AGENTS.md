# Design notes for openhost-synapse

Durable, project-level decisions. Keep these in mind for future changes.

- The bundled Cinny web client is always served at the app root (no enable/disable
  toggle). A bare-homeserver fallback exists only if the client isn't bundled.
- Users must never have to manually restart the app. Settings that need a Synapse
  restart (federation, registration) are applied by the app restarting itself
  (start.sh supervises Synapse; podman's restart policy relaunches the container).
- Single account model: onboarding sets exactly one owner account (username
  only, no password prompt). It is SSO'd into the web client automatically.
  Onboarding generates a random password for SSO's internal use; a user-chosen
  password for third-party Matrix clients is set separately on the admin
  settings page (/_openhost/admin), which updates Synapse and the stored SSO
  password together. No multi-account creation/management.
- The onboarding default username comes from the OpenHost owner username
  (OPENHOST_OWNER_USERNAME), sanitized to a valid Matrix localpart.
- Onboarding is minimal (little reading). Detailed explanations live on a separate
  help page (/_openhost/community/help).
- Federation is enabled by default (no onboarding checkbox); it can be turned off
  later from the admin console.
- The one onboarding checkbox is "Join the OpenHost community space", default-checked.
- The community-join target is a Matrix space (browse its rooms), not a single
  room. Do NOT auto-create local rooms/spaces on the instance; only the public
  community-join is wanted.
- No em-dashes in user-facing text. Use plain punctuation.
</content>
</invoke>
