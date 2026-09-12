# Content Extractor — container image
#
# Lightweight news-article extraction worker (httpx + trafilatura + FastAPI).
# The headless-browser (Chromium) fallback is intentionally NOT installed: it
# would add a few hundred MB of RAM and image size. Leaving it out keeps the
# image small and lets the worker run comfortably on a tiny box — e.g. a
# MikroTik RouterOS container with ~700 MB RAM. See MIKROTIK.md for deployment.
#
# Build for the architecture of the host that will run it. A MikroTik CHR or
# x86 router is amd64:
#     docker build --platform linux/amd64 -t content-extractor:latest .

FROM python:3.12-slim

# PYTHONUNBUFFERED   - logs reach `docker logs` / RouterOS `/log` without delay
# PANEL_HOST=0.0.0.0 - inside a container the app's loopback-only default would
#                      make the panel unreachable; container isolation is what
#                      replaces that safety, so bind all interfaces here.
# RENDER_FALLBACK=false - no browser is installed in this image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8787 \
    RENDER_FALLBACK=false

# ca-certificates: TLS to the n8n API and to the news sites being fetched.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not re-install them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code (only the package is needed at runtime).
COPY extractor ./extractor

# Run unprivileged.
RUN useradd --system --create-home --uid 10001 extractor
USER extractor

EXPOSE 8787

# Liveness: is the control panel accepting TCP connections? A 401 (auth) still
# means the server is up, so a plain port check is the right signal. RouterOS
# ignores HEALTHCHECK, but this is useful when smoke-testing under plain Docker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('127.0.0.1', int(os.environ.get('PANEL_PORT','8787')))))"

# Worker + control panel. All configuration comes from environment variables
# (N8N_BASE_URL, N8N_API_KEY, NODE_NAME, THREADS, ...); see config.example.env.
# Use `run --no-panel` to run the worker alone.
CMD ["python", "-m", "extractor", "run"]
