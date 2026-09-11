#!/usr/bin/env bash
# ============================================================================
# content-extractor installer
#
# One command on a fresh Debian/Ubuntu server:
#   sudo bash install.sh --n8n-url https://n8n.example.com/webhook --api-key KEY
#
# Installs system packages, a dedicated user, a virtualenv, the config file and
# a systemd service, then verifies the whole thing actually works.
# Safe to re-run: existing config values are kept unless you pass a new one.
# ============================================================================
set -Eeuo pipefail

APP_NAME="content-extractor"
SERVICE_NAME="content-extractor"
SERVICE_USER="extractor"
INSTALL_DIR="/opt/content-extractor"
CONFIG_DIR="/etc/content-extractor"
CONFIG_FILE="${CONFIG_DIR}/config.env"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"

# --- options ---------------------------------------------------------------
OPT_N8N_URL=""
OPT_API_KEY=""
OPT_NODE_NAME=""
OPT_THREADS=""
OPT_BATCH=""
OPT_PANEL_HOST=""
OPT_PANEL_PORT=""
OPT_PANEL_PASSWORD=""
OPT_PROXY=""
OPT_WITH_BROWSER=0
OPT_ASSUME_YES=0
OPT_NO_START=0

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

STEP=0
step()  { STEP=$((STEP + 1)); printf '\n%s[%d/9]%s %s%s%s\n' "$BLUE" "$STEP" "$RESET" "$BOLD" "$1" "$RESET"; }
info()  { printf '      %s\n' "$1"; }
ok()    { printf '      %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '      %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()   { printf '\n%sERROR:%s %s\n\n' "$RED" "$RESET" "$1" >&2; exit 1; }

on_error() {
  local line=$1
  printf '\n%sInstall failed at line %s.%s\n' "$RED" "$line" "$RESET" >&2
  printf 'The step above did not finish. Nothing else was changed.\n' >&2
  printf 'If it is not obvious why, re-run with:  bash -x %s ...\n\n' "${BASH_SOURCE[0]}" >&2
}
trap 'on_error $LINENO' ERR

usage() {
  cat <<EOF
${BOLD}content-extractor installer${RESET}

  sudo bash install.sh [options]

${BOLD}Connection${RESET}
  --n8n-url URL         n8n webhook base, e.g. https://n8n.example.com/webhook
  --api-key KEY         the X-API-Key set in the n8n Auth nodes
  --node-name NAME      unique worker name (default: this machine's hostname)

${BOLD}Tuning${RESET}
  --threads N           parallel downloads (default 24)
  --batch-size N        URLs claimed per cycle (default 60)
  --proxy URL           outbound HTTP proxy for page downloads

${BOLD}Panel${RESET}
  --panel-port PORT     default 8787
  --panel-password PW   default: generated and printed at the end
  --panel-public        listen on 0.0.0.0 instead of 127.0.0.1
  --panel-host HOST     explicit bind address

${BOLD}Extras${RESET}
  --with-browser        also install Chromium for the JavaScript-page fallback
  --yes                 never prompt (fails if --api-key is missing and unset)
  --no-start            install but do not start the service
  -h, --help            this text

Re-running keeps every existing setting you do not pass again.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --n8n-url)        OPT_N8N_URL="${2:-}"; shift 2 ;;
    --api-key)        OPT_API_KEY="${2:-}"; shift 2 ;;
    --node-name)      OPT_NODE_NAME="${2:-}"; shift 2 ;;
    --threads)        OPT_THREADS="${2:-}"; shift 2 ;;
    --batch-size)     OPT_BATCH="${2:-}"; shift 2 ;;
    --proxy)          OPT_PROXY="${2:-}"; shift 2 ;;
    --panel-port)     OPT_PANEL_PORT="${2:-}"; shift 2 ;;
    --panel-password) OPT_PANEL_PASSWORD="${2:-}"; shift 2 ;;
    --panel-host)     OPT_PANEL_HOST="${2:-}"; shift 2 ;;
    --panel-public)   OPT_PANEL_HOST="0.0.0.0"; shift ;;
    --with-browser)   OPT_WITH_BROWSER=1; shift ;;
    --yes|-y)         OPT_ASSUME_YES=1; shift ;;
    --no-start)       OPT_NO_START=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage; die "Unknown option: $1" ;;
  esac
done

printf '\n%s%s installer%s\n' "$BOLD" "$APP_NAME" "$RESET"

# ===========================================================================
step "Checking the server"

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo bash install.sh ..."

if ! command -v apt-get >/dev/null 2>&1; then
  die "This installer targets Debian/Ubuntu (apt-get not found).
       On other distros, install python3 + python3-venv yourself and see
       the 'Manual install' section of README.md."
fi

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "${PRETTY_NAME:-unknown Linux} on $(uname -m)"
fi

HAS_SYSTEMD=1
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  HAS_SYSTEMD=0
  warn "systemd not available - the service will not be installed"
fi

# ===========================================================================
step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
APT_PACKAGES="python3 python3-venv python3-pip ca-certificates curl tzdata"

info "apt-get update"
apt-get update -qq || die "apt-get update failed - check the network and apt sources"

info "installing: ${APT_PACKAGES}"
# shellcheck disable=SC2086
apt-get install -y -qq --no-install-recommends $APT_PACKAGES \
  || die "apt-get install failed - see the output above"

PY_VERSION="$($PYTHON -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PY_MINOR="$($PYTHON -c 'import sys; print(sys.version_info.minor)')"
$PYTHON -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python ${PY_VERSION} is too old - 3.9 or newer is required"
ok "python ${PY_VERSION}"

# ===========================================================================
step "Creating the service user and directories"

if id "$SERVICE_USER" >/dev/null 2>&1; then
  ok "user ${SERVICE_USER} already exists"
else
  useradd --system --create-home --home-dir "$INSTALL_DIR" \
          --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "created system user ${SERVICE_USER}"
fi

mkdir -p "$INSTALL_DIR/app" "$INSTALL_DIR/browsers" "$CONFIG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
ok "${INSTALL_DIR} ready"

# ===========================================================================
step "Copying the application"

[ -d "$SRC_DIR/extractor" ] || die "Cannot find the 'extractor' package next to this script.
       Run the installer from inside the content-extractor-app directory."

rm -rf "$INSTALL_DIR/app/extractor"
cp -r "$SRC_DIR/extractor" "$INSTALL_DIR/app/extractor"
cp "$SRC_DIR/requirements.txt" "$INSTALL_DIR/app/requirements.txt"
find "$INSTALL_DIR/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/app"
ok "$(find "$INSTALL_DIR/app/extractor" -name '*.py' | wc -l) python files installed"

# ===========================================================================
step "Building the virtualenv"

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  if ! $PYTHON -m venv "$INSTALL_DIR/venv" 2>/dev/null; then
    warn "python3 -m venv failed - installing python3.${PY_MINOR}-venv"
    apt-get install -y -qq "python3.${PY_MINOR}-venv" \
      || die "Could not install python3.${PY_MINOR}-venv"
    $PYTHON -m venv "$INSTALL_DIR/venv" || die "Could not create the virtualenv"
  fi
  ok "virtualenv created"
else
  ok "virtualenv already present"
fi

VENV_PY="$INSTALL_DIR/venv/bin/python"
info "installing python dependencies (this takes a minute)"
"$VENV_PY" -m pip install --quiet --upgrade pip wheel \
  || die "pip could not upgrade itself - check outbound HTTPS to pypi.org"
"$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/app/requirements.txt" \
  || die "Dependency install failed - see the output above"
ok "dependencies installed"

if [ "$OPT_WITH_BROWSER" -eq 1 ]; then
  info "installing playwright + chromium (a few hundred MB)"
  "$VENV_PY" -m pip install --quiet playwright || die "Could not install playwright"
  PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/browsers" \
    "$INSTALL_DIR/venv/bin/playwright" install --with-deps chromium \
    || die "Chromium install failed - see the output above"
  ok "chromium installed"
fi

# Make `python -m extractor` resolve from any directory, not just app/.
SITE_PACKAGES="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
if [ -d "$SITE_PACKAGES" ]; then
  printf '%s\n' "$INSTALL_DIR/app" > "$SITE_PACKAGES/content-extractor.pth"
  ok "import path registered (python -m extractor works from anywhere)"
else
  warn "site-packages not found - run the CLI from ${INSTALL_DIR}/app"
fi

# Convenience wrapper so `content-extractor doctor` works from anywhere.
if [ -f "$SRC_DIR/bin/content-extractor" ]; then
  install -m 755 "$SRC_DIR/bin/content-extractor" /usr/local/bin/content-extractor
  ok "/usr/local/bin/content-extractor installed"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ===========================================================================
step "Writing the configuration"

set_config() {
  "$PYTHON" - "$CONFIG_FILE" "$1" "$2" <<'PYEOF'
import pathlib, sys

path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
config = pathlib.Path(path)
lines = config.read_text(encoding="utf-8").splitlines() if config.exists() else []
out, done = [], False
for line in lines:
    bare = line.lstrip("# ").strip()
    if not done and bare.startswith(key + "="):
        out.append(f"{key}={value}")
        done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={value}")
config.write_text("\n".join(out) + "\n", encoding="utf-8")
PYEOF
}

get_config() {
  [ -f "$CONFIG_FILE" ] || return 0
  grep -E "^${1}=" "$CONFIG_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

if [ -f "$CONFIG_FILE" ]; then
  ok "keeping the existing ${CONFIG_FILE}"
else
  cp "$SRC_DIR/config.example.env" "$CONFIG_FILE"
  ok "created ${CONFIG_FILE} from the example"
fi

# systemd's EnvironmentFile keeps everything after "=" - including a trailing
# "# comment" - as part of the value, so "THREADS=24  # parallel" silently
# becomes an invalid number and the default is used instead. Earlier versions
# of config.example.env shipped comments like that; clean them up.
if grep -qE '^[A-Z_]+=[^#]*[[:space:]]{2,}#' "$CONFIG_FILE"; then
  cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
  sed -i -E 's/^([A-Z_]+=[^#]*[^[:space:]#])[[:space:]]{2,}#.*$/\1/' "$CONFIG_FILE"
  ok "removed inline # comments from ${CONFIG_FILE} (backup: ${CONFIG_FILE}.bak)"
fi

# --- n8n URL ---
if [ -z "$OPT_N8N_URL" ] && [ -z "$(get_config N8N_BASE_URL)" ] && [ "$OPT_ASSUME_YES" -eq 0 ]; then
  read -r -p "      n8n webhook base URL (e.g. https://n8n.example.com/webhook): " OPT_N8N_URL
fi
[ -n "$OPT_N8N_URL" ] && set_config N8N_BASE_URL "$OPT_N8N_URL"

# --- API key ---
CURRENT_KEY="$(get_config N8N_API_KEY)"
if [ -z "$OPT_API_KEY" ] && { [ -z "$CURRENT_KEY" ] || [ "$CURRENT_KEY" = "change-me" ]; }; then
  if [ "$OPT_ASSUME_YES" -eq 1 ]; then
    die "No API key. Pass --api-key KEY (the X-API-Key from the n8n Auth nodes)."
  fi
  read -r -s -p "      n8n X-API-Key: " OPT_API_KEY; printf '\n'
  [ -n "$OPT_API_KEY" ] || die "The API key cannot be empty"
fi
[ -n "$OPT_API_KEY" ] && set_config N8N_API_KEY "$OPT_API_KEY"

# --- identity and tuning ---
if [ -n "$OPT_NODE_NAME" ]; then
  set_config NODE_NAME "$OPT_NODE_NAME"
elif [ -z "$(get_config NODE_NAME)" ] || [ "$(get_config NODE_NAME)" = "vps-extractor-1" ]; then
  set_config NODE_NAME "$(hostname -s)-extractor"
fi
[ -n "$OPT_THREADS" ] && set_config THREADS "$OPT_THREADS"
[ -n "$OPT_BATCH" ]   && set_config BATCH_SIZE "$OPT_BATCH"
[ -n "$OPT_PROXY" ]   && set_config PROXY_URL "$OPT_PROXY"
[ "$OPT_WITH_BROWSER" -eq 1 ] && set_config RENDER_FALLBACK "true"

# --- panel ---
[ -n "$OPT_PANEL_PORT" ] && set_config PANEL_PORT "$OPT_PANEL_PORT"
if [ -n "$OPT_PANEL_HOST" ]; then
  set_config PANEL_HOST "$OPT_PANEL_HOST"
elif [ -z "$(get_config PANEL_HOST)" ]; then
  set_config PANEL_HOST "127.0.0.1"
fi

PANEL_PASSWORD="$OPT_PANEL_PASSWORD"
if [ -z "$PANEL_PASSWORD" ]; then
  PANEL_PASSWORD="$(get_config PANEL_PASSWORD)"
fi
if [ -z "$PANEL_PASSWORD" ]; then
  PANEL_PASSWORD="$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(12))')"
  info "generated a panel password"
fi
set_config PANEL_PASSWORD "$PANEL_PASSWORD"

chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 640 "$CONFIG_FILE"
ok "configuration written (root-owned, readable by ${SERVICE_USER} only)"

PANEL_HOST="$(get_config PANEL_HOST)"; PANEL_HOST="${PANEL_HOST:-127.0.0.1}"
PANEL_PORT="$(get_config PANEL_PORT)"; PANEL_PORT="${PANEL_PORT:-8787}"
PANEL_USER="$(get_config PANEL_USER)"; PANEL_USER="${PANEL_USER:-admin}"
NODE_NAME="$(get_config NODE_NAME)"

# ===========================================================================
step "Installing the systemd service"

if [ "$HAS_SYSTEMD" -eq 1 ]; then
  sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
      -e "s|__CONFIG_FILE__|${CONFIG_FILE}|g" \
      -e "s|__USER__|${SERVICE_USER}|g" \
      "$SRC_DIR/systemd/content-extractor.service" > "$UNIT_FILE"
  chmod 644 "$UNIT_FILE"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  ok "${UNIT_FILE} installed and enabled at boot"
else
  warn "skipped - run it yourself with:"
  info "  content-extractor run"
fi

# ===========================================================================
step "Running the pre-flight checks"

# A failing check here is informational, not fatal - so run it as the condition
# of an `if`, which bash exempts from both `set -e` and the ERR trap. Note that
# `set +e` alone would NOT do: with `set -E` the ERR trap still fires, and a
# subshell would fire it twice.
if EXTRACTOR_CONFIG="$CONFIG_FILE" PYTHONPATH="$INSTALL_DIR/app" \
   PYTHONDONTWRITEBYTECODE=1 "$VENV_PY" -m extractor doctor; then
  ok "all pre-flight checks passed"
else
  warn "some checks failed (see above) - the install itself is fine and continues"
  warn "fix them, then: sudo systemctl restart ${SERVICE_NAME}"
fi

# ===========================================================================
step "Starting the service"

if [ "$OPT_NO_START" -eq 1 ]; then
  warn "--no-start given; start it later with: sudo systemctl start ${SERVICE_NAME}"
elif [ "$HAS_SYSTEMD" -eq 1 ]; then
  systemctl restart "$SERVICE_NAME"
  info "waiting for the panel to answer on 127.0.0.1:${PANEL_PORT}"
  HEALTHY=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PANEL_PORT}/healthz" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
      break
    fi
    sleep 1
  done

  if [ "$HEALTHY" -eq 1 ]; then
    ok "service is up and the panel responds"
  else
    printf '\n%sThe service did not come up.%s Last log lines:\n\n' "$RED" "$RESET" >&2
    journalctl -u "$SERVICE_NAME" -n 25 --no-pager >&2 || true
    printf '\nFix the problem, then: sudo systemctl restart %s\n\n' "$SERVICE_NAME" >&2
    exit 1
  fi

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    if [ "$PANEL_HOST" = "0.0.0.0" ]; then
      warn "ufw is active - allow the panel port with: sudo ufw allow ${PANEL_PORT}/tcp"
    fi
  fi
fi

# ===========================================================================
LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -n1)"

printf '\n%s────────────────────────────────────────────────────────────%s\n' "$DIM" "$RESET"
printf '%s  %s is installed%s\n' "$BOLD" "$APP_NAME" "$RESET"
printf '%s────────────────────────────────────────────────────────────%s\n\n' "$DIM" "$RESET"
printf '  worker name   %s\n' "${NODE_NAME:-$(hostname -s)}"
printf '  panel         http://%s:%s\n' "$PANEL_HOST" "$PANEL_PORT"
printf '  panel login   %s / %s\n' "$PANEL_USER" "$PANEL_PASSWORD"
printf '  config        %s\n' "$CONFIG_FILE"
printf '  app           %s\n\n' "$INSTALL_DIR"

if [ "$PANEL_HOST" = "127.0.0.1" ]; then
  printf '  The panel is bound to localhost. Reach it from your own machine with:\n'
  printf '    %sssh -N -L %s:127.0.0.1:%s %s@%s%s\n' \
    "$BOLD" "$PANEL_PORT" "$PANEL_PORT" "${SUDO_USER:-root}" "${LAN_IP:-your-server}" "$RESET"
  printf '    then open http://127.0.0.1:%s\n\n' "$PANEL_PORT"
  printf '  To expose it directly instead (plain HTTP - put a TLS proxy in front\n'
  printf '  if it faces the internet):  sudo bash install.sh --panel-public\n\n'
else
  printf '  %sThe panel is reachable from the network over plain HTTP.%s\n' "$YELLOW" "$RESET"
  printf '  Anyone who can reach port %s only needs the password above, so put\n' "$PANEL_PORT"
  printf '  it behind a TLS reverse proxy or a firewall rule.\n\n'
fi

printf '  %sEveryday commands%s\n' "$BOLD" "$RESET"
printf '    systemctl status %s\n' "$SERVICE_NAME"
printf '    journalctl -fu %s          # live log\n' "$SERVICE_NAME"
printf '    systemctl restart %s       # after editing the config\n' "$SERVICE_NAME"
printf '    content-extractor doctor            # re-check everything\n'
printf '    content-extractor test URL          # try one page, writes nothing\n\n'
printf '  Running more workers? Install on another box and give it a different\n'
printf '  NODE_NAME - n8n hands every worker a different slice of the queue.\n\n'
