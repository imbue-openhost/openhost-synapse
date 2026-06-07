FROM matrixdotorg/synapse:latest AS synapse-base

# ---- Build the Go admin server ----
FROM golang:1.22-bookworm AS go-builder
WORKDIR /build
# Copy all Go source files at once (no external deps, so no go.sum needed)
COPY admin/ ./
RUN go mod download && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /admin-server ./cmd/admin

# ---- Final image ----
FROM synapse-base

# Install Caddy for reverse-proxy / Host header rewriting.
RUN apt-get update && \
    apt-get install -y --no-install-recommends caddy && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 host 2>/dev/null || true && \
    useradd -u 1000 -g 1000 -m host 2>/dev/null || true

# Copy admin server binary
COPY --from=go-builder /admin-server /app/admin-server

# Copy our startup wrapper and Caddyfile template
COPY start.sh /app/start.sh
COPY Caddyfile.template /app/Caddyfile.template
RUN chmod +x /app/start.sh

EXPOSE 3000

ENTRYPOINT []
CMD ["/app/start.sh"]
