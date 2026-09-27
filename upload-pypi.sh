#!/bin/bash
#
# Build package and upload to PyPI
#
# Builds the sdist and wheel from a clean checkout of the committed HEAD - never
# the working tree, whose untracked files (the pypowerwall library symlink,
# .venv, local data) must not reach a release - then checks both with
# `twine check --strict`, confirms the wheel ships only the app package, and
# uploads both after confirmation. PyPI never accepts a re-upload of a version,
# so every check runs before anything is sent.
#
# Usage:  ./upload-pypi.sh [--dry-run] [-y]
#           --dry-run  build and check, but don't upload
#           -y         skip the confirmation prompt
# Needs:  python with `build` and `twine` (set PYTHON to override python3),
#         and PyPI credentials for twine (~/.pypirc [pypi])
#
set -euo pipefail

PYTHON="${PYTHON:-python3}"
DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -y) ASSUME_YES=1 ;;
        *) echo "Usage: $0 [--dry-run] [-y]" >&2; exit 2 ;;
    esac
done

die() { echo "ERROR: $*" >&2; exit 1; }

# Resolve PYTHON to an absolute path now: the build runs inside a temporary
# worktree, where a relative override like .venv/bin/python would not resolve.
case "$PYTHON" in
    /*) ;;
    */*) PYTHON="$PWD/$PYTHON" ;;
    *) PYTHON="$(command -v "$PYTHON")" || die "python not found: ${PYTHON}" ;;
esac
[ -x "$PYTHON" ] || die "not an executable python: $PYTHON"

cd "$(git rev-parse --show-toplevel)"

# --- Preconditions ---------------------------------------------------------
"$PYTHON" -m build --version >/dev/null 2>&1 || die "$PYTHON has no 'build' module (pip install build)"
"$PYTHON" -m twine --version >/dev/null 2>&1 || die "$PYTHON has no 'twine' module (pip install twine)"
git diff --quiet HEAD -- || die "uncommitted changes to tracked files - commit or stash them (only committed HEAD is published)"

VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml)
SERVER_VERSION=$(sed -n 's/^SERVER_VERSION = "\([^"]*\)".*/\1/p' app/config.py)
[ -n "$VERSION" ] || die "could not read version from pyproject.toml"
[ "$VERSION" = "$SERVER_VERSION" ] || die "pyproject.toml version ($VERSION) != app/config.py SERVER_VERSION ($SERVER_VERSION)"
if curl -sf -o /dev/null "https://pypi.org/pypi/pypowerwall-server/$VERSION/json"; then
    die "pypowerwall-server $VERSION is already on PyPI - bump the version (pyproject.toml + app/config.py) first"
fi

# --- Build from a clean checkout of HEAD -----------------------------------
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pypowerwall-server-build.XXXXXX")"
cleanup() { git worktree remove --force "$BUILD_DIR" >/dev/null 2>&1 || true; rm -rf "$BUILD_DIR"; git worktree prune; }
trap cleanup EXIT
git worktree add --quiet --detach "$BUILD_DIR" HEAD

echo "Building pypowerwall-server $VERSION from $(git rev-parse --short HEAD) (clean checkout)..."
(cd "$BUILD_DIR" && "$PYTHON" -m build --outdir dist . > build.log 2>&1) || { tail -30 "$BUILD_DIR/build.log"; die "build failed"; }

# --- Check before anything is uploaded -------------------------------------
echo "Checking distributions..."
"$PYTHON" -m twine check --strict "$BUILD_DIR"/dist/*
"$PYTHON" - "$BUILD_DIR/dist" "$VERSION" <<'EOF'
import sys, zipfile
from pathlib import Path

dist, version = Path(sys.argv[1]), sys.argv[2]
wheels = list(dist.glob("*.whl"))
sdists = list(dist.glob("*.tar.gz"))
assert len(wheels) == 1 and len(sdists) == 1, f"expected one wheel and one sdist, got {sorted(p.name for p in dist.iterdir())}"
for path in wheels + sdists:
    assert f"-{version}" in path.name, f"{path.name} is not version {version}"
top = {name.split("/")[0] for name in zipfile.ZipFile(wheels[0]).namelist()}
extra = {t for t in top if t != "app" and not t.endswith(".dist-info")}
assert not extra, f"wheel ships unexpected top-level entries: {sorted(extra)}"
print(f"  OK: {wheels[0].name} ships only app/ ; {sdists[0].name}")
EOF

# Keep a copy in ./dist (gitignored) for reference
rm -rf dist && cp -R "$BUILD_DIR/dist" dist

PYPI_VERSION=$(curl -fsS https://pypi.org/pypi/pypowerwall-server/json 2>/dev/null \
    | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || echo "(unknown)")
echo "Version on PyPI now:     $PYPI_VERSION"
echo "Version ready to upload: $VERSION"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run - built and checked, nothing uploaded:"
    ls -1 dist
    exit 0
fi

# --- Upload ----------------------------------------------------------------
echo "Ready to upload to PyPI (a version can never be re-uploaded):"
ls -1 dist
if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Upload pypowerwall-server $VERSION? [y/N] " answer || answer=""
    case "$answer" in y|Y) ;; *) die "aborted - nothing uploaded" ;; esac
fi
echo "Uploading..."
"$PYTHON" -m twine upload dist/*
echo ""
echo "Uploaded. Install with: pip install pypowerwall-server==$VERSION"
