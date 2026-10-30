#!/usr/bin/env bash
# Security scans, the same locally and in CI (.github/workflows/security.yml, ci.yml).
# Needs only uv and docker; the tool versions are pinned here.
#
#   scripts/scan.sh code          Semgrep: project rules (.semgrep/) + registry packs
#   scripts/scan.sh deps          Trivy: uv.lock vulnerabilities, secrets, Dockerfile misconfig
#   scripts/scan.sh image [REF]   Trivy: an image (default: build the Dockerfile as tools-gateway:scan)
#   scripts/scan.sh [all]         code + deps + image
#
# Exits non-zero on any blocking finding. Settings: trivy.yaml; exceptions: .trivyignore,
# `# nosemgrep: <rule id>`.
set -euo pipefail

SEMGREP_VERSION=1.182.1
TRIVY_VERSION=0.76.1

cd "$(dirname "$0")/.."
TRIVY_CACHE=${TRIVY_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/trivy}

trivy() {
  mkdir -p "$TRIVY_CACHE"
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$PWD:/src:ro" -w /src -v "$TRIVY_CACHE:/cache" \
    "${TRIVY_MOUNTS[@]}" \
    "aquasec/trivy:$TRIVY_VERSION" "$1" --cache-dir /cache --no-progress "${@:2}"
}
TRIVY_MOUNTS=()

scan_code() {
  echo "== code (semgrep $SEMGREP_VERSION)"
  uvx "semgrep==$SEMGREP_VERSION" scan --metrics=off --error \
    --config .semgrep/ --config p/python --config p/owasp-top-ten --config p/github-actions
}

scan_deps() {
  echo "== dependencies, secrets, misconfig (trivy $TRIVY_VERSION)"
  trivy fs --scanners vuln,secret,misconfig .
}

scan_image() {
  local ref=${1:-}
  if [[ -z $ref ]]; then
    ref=tools-gateway:scan
    # No cache: a stale `apt-get upgrade` layer would report packages already fixed upstream.
    docker build --pull --no-cache -t "$ref" .
  elif ! docker image inspect "$ref" >/dev/null 2>&1; then
    docker pull "$ref"
  fi
  echo "== image $ref (trivy $TRIVY_VERSION)"
  # Handed over as a tarball: no docker socket in the scanner container.
  local tmp
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  docker save "$ref" -o "$tmp/image.tar"
  chmod -R a+rX "$tmp"
  TRIVY_MOUNTS=(-v "$tmp:/image:ro")
  trivy image --scanners vuln,secret --input /image/image.tar
}

case ${1:-all} in
  code) scan_code ;;
  deps) scan_deps ;;
  image) scan_image "${2:-}" ;;
  all)
    status=0
    scan_code || status=1
    scan_deps || status=1
    scan_image || status=1
    exit $status ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
