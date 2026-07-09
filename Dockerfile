# ---------------------------------------------------------------------------
# Cinny build stage.
#
# We build the bundled Cinny web client from source (instead of downloading the
# prebuilt release tarball) so we can apply a small patch that suppresses the
# transient green "Connecting..." banner. That banner is shown by matrix-js-sdk
# on every normal startup and only clears once the first steady sync completes;
# on a personal homeserver the first /sync long-poll holds open for the full
# poll window, so the banner lingers at the top of the screen for tens of
# seconds on an otherwise healthy connection. The patch keeps the genuinely
# useful "Reconnecting..." and "Connection Lost!" banners. See
# cinny-suppress-connecting-banner.patch.
# ---------------------------------------------------------------------------
FROM node:22-bookworm AS cinny-builder

# Pin the bundled web client (Cinny) version so builds are reproducible. Keep
# this in sync with the patch in cinny-suppress-connecting-banner.patch; the
# build fails loudly if the patch no longer applies (e.g. after a version bump).
ARG CINNY_VERSION=v4.12.3

WORKDIR /build
RUN git clone --depth 1 --branch "${CINNY_VERSION}" https://github.com/cinnyapp/cinny.git .

# Apply the OpenHost patch. `git apply --check` first so a version bump that
# invalidates the patch fails the build here with a clear error instead of
# silently shipping the upstream banner behavior.
COPY cinny-suppress-connecting-banner.patch /build/cinny-suppress-connecting-banner.patch
RUN git apply --check cinny-suppress-connecting-banner.patch && \
    git apply cinny-suppress-connecting-banner.patch

# Build the static SPA. Guard against a future refactor silently regressing the
# patch: the built bundle must NOT contain the "Connecting..." banner string,
# and MUST still contain the error banners we intentionally keep.
RUN npm ci && npm run build && \
    if grep -rq 'Connecting\.\.\.' dist/assets/*.js; then \
        echo "ERROR: 'Connecting...' banner still present in built Cinny; patch did not take effect" >&2; \
        exit 1; \
    fi && \
    if ! grep -rq 'Connection Lost!' dist/assets/*.js; then \
        echo "ERROR: 'Connection Lost!' banner missing from built Cinny; over-patched" >&2; \
        exit 1; \
    fi

# ---------------------------------------------------------------------------
# Final image.
# ---------------------------------------------------------------------------
FROM matrixdotorg/synapse:latest

# The Synapse image is Debian-based, so apt works fine.
# Install Caddy for Host header rewriting (the OpenHost router strips Host
# and sets X-Forwarded-Host; Synapse needs them to match for correct URLs).
# Install Flask for the admin UI.
RUN apt-get update && \
    apt-get install -y --no-install-recommends caddy ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir flask && \
    groupadd -g 1000 host && \
    useradd -u 1000 -g 1000 -m host

# Bundle the (patched) Cinny web client built in the cinny-builder stage. It is
# served by Caddy at the app root; the image stays self-contained (no network
# needed at container start).
RUN mkdir -p /app/webclient
COPY --from=cinny-builder /build/dist/ /app/webclient/
# Keep the pristine, unconfigured config.json as a template; start.sh renders
# the live config.json (with this zone's homeserver pinned) into the served dir.
RUN mv /app/webclient/config.json /app/webclient-config.default.json

# Copy our startup wrapper, Caddyfile template, admin UI, and web client config template
COPY start.sh /app/start.sh
COPY Caddyfile.template /app/Caddyfile.template
COPY admin.py /app/admin.py
COPY webclient-config.template.json /app/webclient-config.template.json
RUN chmod +x /app/start.sh

EXPOSE 3000

ENTRYPOINT []
CMD ["/app/start.sh"]
