#!/usr/bin/env bash
# Removes the service. Config and the app stay unless you pass --purge.
#   sudo bash uninstall.sh [--purge] [--yes]
set -Eeuo pipefail

SERVICE_NAME="content-extractor"
SERVICE_USER="extractor"
INSTALL_DIR="/opt/content-extractor"
CONFIG_DIR="/etc/content-extractor"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

PURGE=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help)
      printf 'Usage: sudo bash uninstall.sh [--purge] [--yes]\n'
      printf '  --purge  also delete %s, %s and the %s user\n' \
             "$INSTALL_DIR" "$CONFIG_DIR" "$SERVICE_USER"
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { printf 'Run as root: sudo bash uninstall.sh\n' >&2; exit 1; }

if [ "$PURGE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
  printf 'This deletes %s, %s (including your API key) and the %s user.\n' \
         "$INSTALL_DIR" "$CONFIG_DIR" "$SERVICE_USER"
  read -r -p 'Type "purge" to confirm: ' answer
  [ "$answer" = "purge" ] || { printf 'Cancelled.\n'; exit 1; }
fi

rm -f /usr/local/bin/content-extractor

if command -v systemctl >/dev/null 2>&1; then
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  printf 'Service stopped, disabled and removed.\n'
fi

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  printf 'Purged %s, %s and the %s user.\n' "$INSTALL_DIR" "$CONFIG_DIR" "$SERVICE_USER"
else
  printf 'Kept %s and %s - re-run install.sh to bring the service back.\n' \
         "$INSTALL_DIR" "$CONFIG_DIR"
  printf 'Add --purge to delete them.\n'
fi
