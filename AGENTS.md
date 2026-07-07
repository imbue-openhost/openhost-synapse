# Design notes for openhost-synapse

Durable, project-level decisions. Keep these in mind for future changes.

- The bundled Cinny web client is always served at the app root (no enable/disable
  toggle). A bare-homeserver fallback exists only if the client isn't bundled.
- Users must never have to manually restart the app. Settings that need a Synapse
  restart (federation, registration) are applied by the app restarting itself
  (start.sh supervises Synapse; podman's restart policy relaunches the container).
- Single account model: onboarding sets exactly one owner account (username +
  password). It is SSO'd into the web client and the same password works from
  third-party Matrix clients. No multi-account creation/management.
- The onboarding default username comes from the OpenHost owner username
  (OPENHOST_OWNER_USERNAME), sanitized to a valid Matrix localpart.
- Onboarding is minimal (little reading). Detailed explanations live on a separate
  help page (/_openhost/community/help).
- The only onboarding checkboxes are "Enable federation" and "Join the OpenHost
  community space" — separate checkboxes, both default-checked.
- The community-join target is a Matrix space (browse its rooms), not a single
  room. Onboarding also creates a local space with a few starter rooms on the
  instance's own homeserver.
- No em-dashes in user-facing text. Use plain punctuation.
</content>
</invoke>
