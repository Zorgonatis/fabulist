# syntax=docker/dockerfile:1
#
# Fabulist in ONE container: the FastAPI engine serves the built SvelteKit UI at
# the same origin (src/fabulist/api/app.py mounts frontend/build at "/"), so there
# is no second server, no reverse proxy and no CORS setup in the deploy path.
#
#   docker compose up            # the supported path (see docker-compose.yml)
#   docker build -t fabulist .   # the image on its own
#
# First run needs NOTHING: with no configs/connections.json the engine boots and
# prints the create-door hint; connections are added in the UI or over the API.

# --- stage 1: build the UI (adapter-static SPA -> frontend/build) -------------
FROM node:22-slim AS ui

WORKDIR /ui

# Locked install first, so a source edit does not re-run npm ci.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --- stage 2: the engine + the UI artifact it serves -------------------------
FROM python:3.12-slim AS engine

# uv pinned by tag: the build must resolve the committed uv.lock, whatever
# uv version a builder machine happens to have.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# Bytecode is compiled into the image (no first-request cost) and the venv is
# copied rather than hardlinked (the image layers are read-only).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# ffmpeg is a RUNTIME dependency, not a build tool: attachment preparation and
# the voice path shell out to it. Ship the two STATIC binaries instead of the
# Debian package — the package drags ~600 MB of codec dependencies, while these
# are self-contained (~80 MB total) and behave the same for our use (probe +
# transcode). Pinned tag: same reasoning as the uv image above.
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:7.1 /ffprobe /usr/local/bin/ffprobe

# uid/gid 1000 (the usual first desktop user) so the bind-mounted ./data and
# ./configs stay writable without chown gymnastics on the host. Everything below
# runs as that user, which keeps the venv in one layer instead of copying it
# again just to change ownership.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app \
    && chown app:app /app

USER app
WORKDIR /app

# The project itself is installed editable, so src/ must be present for the
# sync; pyproject.toml + uv.lock are the only other inputs.
COPY --chown=app:app pyproject.toml uv.lock ./
COPY --chown=app:app src ./src

# Voice input is OFF by default: the stt extra (faster-whisper + numpy) is
# heavy, and its absence is a readable 503 on /api/stt, not a broken image.
# Enable it with `--build-arg INSTALL_STT=1` (docker-compose.yml shows the line).
ARG INSTALL_STT=0
RUN if [ "$INSTALL_STT" = "1" ]; then \
        uv sync --frozen --no-dev --extra stt; \
    else \
        uv sync --frozen --no-dev; \
    fi

# Runtime tree, laid out exactly as scripts/serve.py and the API expect: it runs
# from /app, so data/, configs/, prompts/ and frontend/build resolve relatively.
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app configs/connections.example.json ./configs/connections.example.json
COPY --chown=app:app --from=ui /ui/build ./frontend/build

# The venv is used directly: `uv run` would re-resolve (and may want the network)
# on every container start.
ENV PATH="/app/.venv/bin:$PATH"
# A key pasted in the Connections drawer is written to the dotenv file. In a
# container that path must live on the MOUNTED configs volume, or the key would
# vanish when the container is recreated (/app itself is image, not volume).
ENV FABULIST_ENV_FILE=/app/configs/.env

EXPOSE 2727

# /api/sessions answers 200 {"sessions": []} on a virgin log, so this is a real
# engine check, not just a port probe. stdlib urllib: no curl in a slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2727/api/sessions', timeout=3).status == 200 else 1)"]

# --host 0.0.0.0: serve.py defaults to 127.0.0.1, which the port mapping cannot
# reach from outside the container.
CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "2727"]
