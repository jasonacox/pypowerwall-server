#!/bin/bash
#
# Build package and upload to PyPI
#

# Extract version from pyproject.toml
REPO_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')

echo "Cleaning up..."
rm -rf build dist *.egg-info

echo "Building dist..."
python3 -m build

echo ""
echo "Built packages:"
ls -lh dist/

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check current PyPI version
echo "Checking PyPI..."
PYPI_VERSION=$(curl -s https://pypi.org/pypi/pypowerwall-server/json 2>/dev/null | python3 -c "import sys, json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null)

echo ""
if [ -n "$PYPI_VERSION" ]; then
    echo "Version in PyPI:        $PYPI_VERSION"
else
    echo "Version in PyPI:        (not published yet)"
fi
echo "Version ready to upload: $REPO_VERSION"
echo ""

if [ -n "$PYPI_VERSION" ] && [ "$REPO_VERSION" == "$PYPI_VERSION" ]; then
    echo "⚠️  Warning: Versions match! Update version in pyproject.toml first."
    echo ""
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
read -p "Upload to PyPI? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "Uploading to PyPI..."
    python3 -m twine upload dist/*
    echo ""
    echo "✓ Package uploaded successfully!"
    echo "Install with: pip install pypowerwall-server"
else
    echo "Upload cancelled."
fi
