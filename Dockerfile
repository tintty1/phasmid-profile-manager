# One image, one process: FastAPI serves the REST API and the built web UI on a
# single port, exactly as `phasmid-pm` does outside a container. The UI is built
# in the first stage and copied in, so the runtime image carries no Node.
#
# Browsers launched from the container are real Phasmid (Chromium) processes,
# so the image carries Chrome's runtime libraries, the font families Phasmid's
# Linux profiles list, and TigerVNC: with PPM_VNC=1 every launch gets its own VNC
# display, viewed and driven from the web UI. The browser itself is not in the
# image: Settings > Install browser downloads the Phasmid release into /data.
#
# Chrome's sandbox needs user namespaces, which Docker's default seccomp profile
# blocks; run with --security-opt seccomp=unconfined (docker-compose.yml does).

FROM node:20-slim AS webui
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ENV NEXT_EXPORT=1
RUN npm run build

FROM python:3.12-slim-trixie

# The libraries Phasmid's own runtime image installs (docker/Dockerfile in the
# Phasmid repository), its fonts, and Xvnc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        fonts-freefont-ttf \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libatspi2.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libglib2.0-0t64 \
        libgtk-3-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libudev1 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        tigervnc-standalone-server \
    && rm -rf /var/lib/apt/lists/* \
    # Chrome refuses to run as root without --no-sandbox, so the app does not.
    && useradd --create-home --uid 1000 phasmid \
    && mkdir -p /data/cache /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R phasmid:phasmid /data

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (better layer caching).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --no-dev --frozen

# The static export, where the package looks for it when no PPM_WEBUI_DIR is set.
COPY --from=webui /web/out ./src/phasmid_pm/webui

# Everything that persists lives in /data: the database, profile directories,
# the installed Phasmid release, and phasmid-profile-gen's DB-IP cache.
ENV PPM_VNC=1 \
    PPM_HOST=0.0.0.0 \
    PPM_PORT=8000 \
    PPM_DB_PATH=/data/profiles.db \
    XDG_CACHE_HOME=/data/cache

USER phasmid
VOLUME ["/data"]
EXPOSE 8000

# The console script, so the container runs the same entry point as a local
# install rather than a second, drifting invocation of uvicorn.
CMD ["/app/.venv/bin/phasmid-pm", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
