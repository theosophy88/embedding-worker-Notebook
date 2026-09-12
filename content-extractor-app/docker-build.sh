#!/usr/bin/env bash
# Build the content-extractor image for a MikroTik router, then get it onto the
# router by one of the two paths in MIKROTIK.md.
#
# RouterOS CANNOT build images. Run this on any machine that has Docker:
#
#   ./docker-build.sh save                                  # -> content-extractor.tar  (offline import, Option B)
#   ./docker-build.sh save /path/to/ce.tar                  # custom output path
#   ./docker-build.sh push docker.io/youruser/content-extractor:latest   # registry pull, Option A
#
# A MikroTik CHR / x86 router is amd64. If you build on an arm host (Apple
# silicon), Docker buildx + qemu handles the --platform cross-build.
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-content-extractor:latest}"   # local tag used by `save`
PLATFORM="${PLATFORM:-linux/amd64}"          # CHR / x86 RouterOS
mode="${1:-save}"

case "$mode" in
  save)
    out="${2:-content-extractor.tar}"
    echo ">> building $IMAGE for $PLATFORM"
    docker build --platform "$PLATFORM" -t "$IMAGE" .
    echo ">> saving $IMAGE -> $out"
    docker save "$IMAGE" -o "$out"
    echo
    echo "Done: $out ($(du -h "$out" | cut -f1))."
    echo "Upload it to the router (Files), then follow MIKROTIK.md > Option B (offline .tar)."
    ;;
  push)
    target="${2:?usage: ./docker-build.sh push <registry>/<user>/<image>:<tag>}"
    echo ">> building $target for $PLATFORM"
    docker build --platform "$PLATFORM" -t "$target" .
    echo ">> pushing $target (make sure you are logged in: docker login <registry>)"
    docker push "$target"
    echo
    echo "Done. Use \"$target\" as remote-image in MIKROTIK.md > Option A (registry pull)."
    ;;
  *)
    echo "usage: $0 {save [output.tar] | push <registry-image>}" >&2
    exit 2
    ;;
esac
