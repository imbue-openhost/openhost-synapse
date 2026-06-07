FROM matrixdotorg/synapse:latest AS synapse-base

# ---- Pull MAS binary and assets from the official image ----
FROM ghcr.io/element-hq/matrix-authentication-service:v1.18.0 AS mas-source

# ---- Build the Go admin server ----
FROM golang:1.22-bookworm AS go-builder
WORKDIR /build
# Copy all Go source files at once (no external deps, so no go.sum needed)
COPY admin/ ./
RUN go mod download && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /admin-server ./cmd/admin

# ---- Final image ----
FROM synapse-base

# Install Caddy, PostgreSQL (for MAS), and supporting tools.
# PostgreSQL 15 is available from the default Debian repos in the Synapse image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        caddy \
        postgresql \
        postgresql-client \
        curl \
        openssl && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 host 2>/dev/null || true && \
    useradd -u 1000 -g 1000 -m host 2>/dev/null || true

# Copy MAS binary and required share/ assets
COPY --from=mas-source /usr/local/bin/mas-cli /usr/local/bin/mas-cli
COPY --from=mas-source /usr/local/share/mas-cli/ /usr/local/share/mas-cli/

# Copy admin server binary
COPY --from=go-builder /admin-server /app/admin-server

# Copy our startup wrapper, Caddyfile template, and MAS config template
COPY start.sh /app/start.sh
COPY Caddyfile.template /app/Caddyfile.template
COPY mas/mas.yaml.template /app/mas.yaml.template
RUN chmod +x /app/start.sh

EXPOSE 3000

ENTRYPOINT []
CMD ["/app/start.sh"]
