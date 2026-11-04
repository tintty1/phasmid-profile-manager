# One image, one process: FastAPI serves the REST API and the built web UI on a
# single port, exactly as `camoufox-pm` does outside a container. The UI is built
# in the first stage and copied in, so the runtime image carries no Node.
#
# Browsers launched from the container are real (headed) Camoufox processes, so
# the image carries Firefox's GTK/X11 runtime libraries and TigerVNC: with
# CPM_VNC=1 every launch gets its own VNC display, viewed and driven from the
# web UI.

FROM node:20-slim AS webui
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ENV NEXT_EXPORT=1
RUN npm run build

FROM python:3.12-slim

# Firefox's shared-library dependencies (Debian trixie names) and Xvnc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxtst6 \
        libxcursor1 libxi6 libxcomposite1 libxdamage1 libxrandr2 libpci3 \
        fonts-liberation tigervnc-standalone-server \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (better layer caching).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --no-dev --frozen

# The static export, where the package looks for it when no CPM_WEBUI_DIR is set.
COPY --from=webui /web/out ./src/camoufox_pm/webui

ENV CPM_VNC=1 \
    CPM_HOST=0.0.0.0 \
    CPM_PORT=8000 \
    CPM_DB_PATH=/data/profiles.db

VOLUME ["/data"]
EXPOSE 8000

# The console script, so the container runs the same entry point as a local
# install rather than a second, drifting invocation of uvicorn.
CMD ["uv", "run", "camoufox-pm", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
