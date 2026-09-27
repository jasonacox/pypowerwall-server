#!/bin/bash
# Build and push the jasonacox/pypowerwall-server image to Docker Hub.
#
# Usage (from anywhere in the repo):
#   ./upload.sh                 # asks: beta or production
#   ./upload.sh --prod [-y]     # production: :<version> and :latest
#   ./upload.sh --beta [N] [-y] # beta: :<version>-betaN (auto-increments N)
#   --no-cache                  # build without the layer cache
#   -y                          # don't prompt (for a scripted release)
#
# Production images are built from a clean checkout of the committed HEAD
# (never the working tree, whose .venv, dist/ and pypowerwall symlink must not
# reach an image), refuse to overwrite a version tag already on Docker Hub, wait
# until the pinned pypowerwall is installable from PyPI, and are smoke-tested
# after the push. Beta images build the working tree with the local
# pypowerwall/ (Dockerfile.beta). Any failure exits non-zero.
set -u

IMAGE="jasonacox/pypowerwall-server"
PLATFORMS="linux/amd64,linux/arm64,linux/arm/v7,linux/arm/v8"

die() { echo "ERROR: $*" >&2; exit 1; }

RELEASE_TYPE=""
BETA_NUM=""
ASSUME_YES=0
NO_CACHE_FLAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prod) RELEASE_TYPE=2 ;;
    --beta)
      RELEASE_TYPE=1
      if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then BETA_NUM="$2"; shift; fi ;;
    [0-9]*) RELEASE_TYPE=1; BETA_NUM="$1" ;;  # legacy: ./upload.sh <beta_number>
    -y) ASSUME_YES=1 ;;
    --no-cache) NO_CACHE_FLAG="--no-cache" ;;
    *) echo "Usage: $0 [--prod | --beta [N]] [--no-cache] [-y]" >&2; exit 2 ;;
  esac
  shift
done

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  read -r -p "$1 [Enter] to continue or Ctrl-C to cancel..." || die "no input - cancelled"
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside the pypowerwall-server git repo"
cd "$ROOT" || exit 1
grep -q '^name = "pypowerwall-server"' pyproject.toml 2>/dev/null || die "$ROOT is not the pypowerwall-server repo"

echo "Build and Push ${IMAGE} to Docker Hub"
BUILD_START=$(date +%s)

SERVER_VERSION=$(sed -n 's/^SERVER_VERSION = "\([^"]*\)".*/\1/p' app/config.py)
[ -n "$SERVER_VERSION" ] || die "could not read SERVER_VERSION from app/config.py"

if [ -z "$RELEASE_TYPE" ]; then
  echo "Release Type:"
  echo "  1) Beta release (adds -betaX suffix)"
  echo "  2) Production release (version ${SERVER_VERSION})"
  read -r -p "Select release type [1-2]: " RELEASE_TYPE || die "no input - cancelled"
fi
case "$RELEASE_TYPE" in 1|2) ;; *) die "release type must be 1 (beta) or 2 (production)" ;; esac

if [ "$RELEASE_TYPE" == "2" ]; then
  # --- Production ----------------------------------------------------------
  VER="${SERVER_VERSION}"
  PROJECT_VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml)
  [ "$PROJECT_VERSION" = "$VER" ] || die "pyproject.toml version ($PROJECT_VERSION) != app/config.py SERVER_VERSION ($VER)"
  git diff --quiet HEAD -- || die "uncommitted changes to tracked files - commit them (production builds use the committed HEAD)"
  if docker buildx imagetools inspect "${IMAGE}:${VER}" >/dev/null 2>&1; then
    die "${IMAGE}:${VER} is already on Docker Hub - bump the version instead of overwriting a release"
  fi

  # The image runs 'pip install -r requirements.txt' from PyPI. Right after a
  # pypowerwall upload PyPI's simple index can lag the JSON API, so wait (up to
  # 10 minutes) until the pinned version is listed in both simple-index forms.
  PYPOWERWALL=$(sed -n 's/^pypowerwall==\(.*\)$/\1/p' requirements.txt)
  [ -n "$PYPOWERWALL" ] || die "could not read the pypowerwall pin from requirements.txt"
  simple_has_version() {
    curl -fsS https://pypi.org/simple/pypowerwall/ | grep -q "pypowerwall-${PYPOWERWALL}-py3-none-any.whl" \
    && curl -fsS -H "Accept: application/vnd.pypi.simple.v1+json" https://pypi.org/simple/pypowerwall/ \
       | python3 -c "import json,sys; sys.exit(0 if '${PYPOWERWALL}' in json.load(sys.stdin).get('versions', []) else 1)"
  }
  echo "Checking pypowerwall==${PYPOWERWALL} is installable from PyPI..."
  for i in $(seq 1 40); do
    simple_has_version && break
    [ "$i" -eq 40 ] && die "pypowerwall==${PYPOWERWALL} is not on PyPI's simple index after 10 minutes"
    echo "  not listed yet (PyPI index lag) - retrying in 15s ($i/40)"
    sleep 15
  done

  CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/pypowerwall-server-image.XXXXXX")"
  trap 'git worktree remove --force "$CONTEXT" >/dev/null 2>&1; rm -rf "$CONTEXT"; git worktree prune' EXIT
  git worktree add --quiet --detach "$CONTEXT" HEAD || die "could not create a clean checkout of HEAD"
  DOCKERFILE="Dockerfile"
  TAGS=(-t "${IMAGE}:${VER}" -t "${IMAGE}:latest")
  echo ""
  echo "Production release: ${IMAGE}:${VER} and :latest from $(git rev-parse --short HEAD) (clean checkout)"
else
  # --- Beta ----------------------------------------------------------------
  BETA_FILE=".beta_version"
  if [ -z "$BETA_NUM" ]; then
    if [ -f "$BETA_FILE" ]; then BETA_NUM=$(( $(cat "$BETA_FILE") + 1 )); else BETA_NUM=1; fi
  fi
  echo "$BETA_NUM" > "$BETA_FILE"
  VER="${SERVER_VERSION}-beta${BETA_NUM}"
  DOCKERFILE="Dockerfile.beta"
  TAGS=(-t "${IMAGE}:${VER}")  # beta builds never overwrite :latest
  CONTEXT="$ROOT"

  # Dockerfile.beta COPYs pypowerwall/ into the image; fail fast if missing.
  if [ ! -d "pypowerwall" ] && [ ! -L "pypowerwall" ]; then
    die "beta builds need a 'pypowerwall/' directory or symlink in the project root (e.g. ln -s ../pypowerwall/pypowerwall pypowerwall)"
  fi
  # BuildKit does not follow symlinks out of the build context: dereference the
  # pypowerwall symlink for the build and always restore it afterwards.
  if [ -L "pypowerwall" ]; then
    echo "* Dereferencing pypowerwall symlink for Docker build context..."
    cp -rL pypowerwall pypowerwall_real
    mv pypowerwall pypowerwall_symlink
    mv pypowerwall_real pypowerwall
    trap 'if [ -L pypowerwall_symlink ]; then rm -rf pypowerwall; mv pypowerwall_symlink pypowerwall; fi' EXIT
  fi
  echo ""
  echo "Beta release: ${IMAGE}:${VER} (beta number stored in ${BETA_FILE})"
fi

confirm "Build and push to Docker Hub?"

echo "* BUILD ${IMAGE}:${VER} (using ${DOCKERFILE}${NO_CACHE_FLAG:+, --no-cache})"
docker buildx build -f "${CONTEXT}/${DOCKERFILE}" ${NO_CACHE_FLAG} --platform "${PLATFORMS}" --push \
  "${TAGS[@]}" "${CONTEXT}" \
  || die "docker buildx build failed - nothing was pushed (see the build output above)"
echo ""

echo "* VERIFY ${IMAGE}:${VER}"
docker buildx imagetools inspect "${IMAGE}:${VER}" | grep Platform \
  || die "${IMAGE}:${VER} is not on Docker Hub after the push"
if [ "$RELEASE_TYPE" == "2" ]; then
  echo "* VERIFY ${IMAGE}:latest"
  LATEST_DIGEST=$(docker buildx imagetools inspect "${IMAGE}:latest" --format '{{json .Manifest.Digest}}')
  VER_DIGEST=$(docker buildx imagetools inspect "${IMAGE}:${VER}" --format '{{json .Manifest.Digest}}')
  [ -n "$VER_DIGEST" ] && [ "$LATEST_DIGEST" = "$VER_DIGEST" ] \
    || die "${IMAGE}:latest does not point at ${VER}"
  echo "  :latest = :${VER} (${VER_DIGEST})"

  # Smoke test: the published image carries this server and library version
  echo "* SMOKE TEST ${IMAGE}:${VER} (linux/amd64)"
  REPORTED=$(docker run --rm --pull always --platform linux/amd64 --entrypoint python "${IMAGE}:${VER}" \
    -c "import pypowerwall; from app.config import SERVER_VERSION; print(SERVER_VERSION, pypowerwall.__version__)") \
    || die "could not run ${IMAGE}:${VER}"
  [ "$REPORTED" = "${VER} ${PYPOWERWALL}" ] \
    || die "image reports '${REPORTED}', expected '${VER} ${PYPOWERWALL}'"
  echo "  server ${VER}, pypowerwall ${PYPOWERWALL}"
fi
echo ""

# --- Summary -----------------------------------------------------------------
BUILD_TIME=$(( $(date +%s) - BUILD_START ))
platform_size() {
  local digest
  digest=$(docker buildx imagetools inspect "${IMAGE}:${VER}" --raw 2>/dev/null \
    | jq -r ".manifests[] | select($1) | .digest" 2>/dev/null)
  if [ -n "$digest" ]; then
    echo "$(( $(docker buildx imagetools inspect "${IMAGE}:${VER}@${digest}" --raw 2>/dev/null \
      | jq '[.layers[].size] | add' 2>/dev/null) / 1024 / 1024 )) MB"
  else
    echo "N/A"
  fi
}
echo "=========================================="
echo "          BUILD SUMMARY"
echo "=========================================="
echo "Build Time:      $((BUILD_TIME / 60))m $((BUILD_TIME % 60))s"
echo "Container Sizes:"
echo "  - amd64:       $(platform_size '.platform.architecture=="amd64"')"
echo "  - arm64:       $(platform_size '.platform.architecture=="arm64"')"
echo "  - arm/v7:      $(platform_size '.platform.architecture=="arm" and .platform.variant=="v7"')"
echo "  - arm/v8:      $(platform_size '.platform.architecture=="arm" and .platform.variant=="v8"')"
echo "Container Name:  ${IMAGE}:${VER}"
echo "Docker Hub:      https://hub.docker.com/r/${IMAGE}"
echo "=========================================="
