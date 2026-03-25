FROM matrixdotorg/synapse:latest

# The Synapse image is Debian-based, so apt works fine.
# Install Caddy for Host header rewriting (the OpenHost router strips Host
# and sets X-Forwarded-Host; Synapse needs them to match for correct URLs).
RUN apt-get update && \
    apt-get install -y --no-install-recommends caddy && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy our startup wrapper and Caddyfile
COPY start.sh /app/start.sh
COPY Caddyfile /app/Caddyfile
RUN chmod +x /app/start.sh

EXPOSE 3000

ENTRYPOINT []
CMD ["/app/start.sh"]
