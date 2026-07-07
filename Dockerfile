FROM matrixdotorg/synapse:latest

# Pin the bundled web client (Cinny) version so builds are reproducible.
ARG CINNY_VERSION=v4.12.3

# The Synapse image is Debian-based, so apt works fine.
# Install Caddy for Host header rewriting (the OpenHost router strips Host
# and sets X-Forwarded-Host; Synapse needs them to match for correct URLs).
# Install Flask for the admin UI. curl is needed to fetch the web client.
RUN apt-get update && \
    apt-get install -y --no-install-recommends caddy curl ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir flask && \
    groupadd -g 1000 host && \
    useradd -u 1000 -g 1000 -m host

# Bundle the Cinny web client as static files served by Caddy when the
# community chat feature is enabled. Downloaded at build time so the image is
# self-contained (no network needed at container start).
RUN mkdir -p /app/webclient && \
    curl -fsSL "https://github.com/cinnyapp/cinny/releases/download/${CINNY_VERSION}/cinny-${CINNY_VERSION}.tar.gz" \
        -o /tmp/cinny.tar.gz && \
    tar xzf /tmp/cinny.tar.gz -C /tmp && \
    cp -a /tmp/dist/. /app/webclient/ && \
    rm -rf /tmp/cinny.tar.gz /tmp/dist && \
    # Keep the pristine, unconfigured config.json as a template; start.sh renders
    # the live config.json (with this zone's homeserver pinned) into the served dir.
    mv /app/webclient/config.json /app/webclient-config.default.json

# Copy our startup wrapper, Caddyfile template, admin UI, and web client config template
COPY start.sh /app/start.sh
COPY Caddyfile.template /app/Caddyfile.template
COPY admin.py /app/admin.py
COPY webclient-config.template.json /app/webclient-config.template.json
RUN chmod +x /app/start.sh

EXPOSE 3000

ENTRYPOINT []
CMD ["/app/start.sh"]
