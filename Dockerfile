# OZON's antibot blocks headless Chromium, so the session bootstrap runs a real
# Chromium under Xvfb; thereafter requests go out over curl_cffi. Xvfb is started
# by entrypoint.sh before the server.
FROM python:3.13.18-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-browsers \
    DEBIAN_FRONTEND=noninteractive \
    OZON_STATE_PATH=/data/state.json

WORKDIR /app
# README.md comes along because pyproject declares it as the package readme —
# without it `uv sync` fails while reading project metadata.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && uv run playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb fluxbox \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY entrypoint.sh ./entrypoint.sh
RUN uv sync --frozen --no-dev && chmod +x entrypoint.sh

# /data holds the authenticated session (state.json) and the price history, so it
# must be bind-mounted to survive container recreation — the session rotates and
# is written back on every call.
VOLUME ["/data"]

# HTTP/SSE require OZON_AUTH_TOKEN and OZON_HOST=0.0.0.0 inside the container.
# Publish to loopback with -p 127.0.0.1:8084:8084; /metrics also requires auth.
EXPOSE 8084

ENTRYPOINT ["./entrypoint.sh"]
