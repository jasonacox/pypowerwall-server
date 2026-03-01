#!/bin/bash
echo "Build and Push jasonacox/pypowerwall-server to Docker Hub"
echo "Usage: $0 [beta_number]"
echo "  If beta_number is not provided, auto-increments from last beta version"
echo ""

# Start timing
BUILD_START=$(date +%s)

last_path=$(basename $PWD)
if [ "$last_path" == "pypowerwall-server" ]; then
  # Determine version
  SERVER_VERSION=`grep "SERVER_VERSION = " app/config.py | cut -d\" -f2`
  
  # Ask if this is a beta release
  echo "Release Type:"
  echo "  1) Beta release (adds -betaX suffix)"
  echo "  2) Production release (version ${SERVER_VERSION})"
  echo ""
  read -p "Select release type [1-2]: " RELEASE_TYPE
  
  if [ "$RELEASE_TYPE" == "2" ]; then
    # Production release - just use version number
    VER="${SERVER_VERSION}"
    CONTAINER_NAME="jasonacox/pypowerwall-server:${VER}"
    echo ""
    echo "Production release: ${CONTAINER_NAME}"
  else
    # Beta release - handle beta numbering
    BETA_FILE=".beta_version"
    if [ -n "$1" ]; then
      # Use provided beta number
      BETA_NUM="$1"
      echo "$BETA_NUM" > "$BETA_FILE"
    else
      # Auto-increment beta number
      if [ -f "$BETA_FILE" ]; then
        BETA_NUM=$(cat "$BETA_FILE")
        BETA_NUM=$((BETA_NUM + 1))
      else
        BETA_NUM=1
      fi
      echo "$BETA_NUM" > "$BETA_FILE"
    fi
    
    VER="${SERVER_VERSION}-beta${BETA_NUM}"
    CONTAINER_NAME="jasonacox/pypowerwall-server:${VER}"
    echo ""
    echo "Beta release: ${CONTAINER_NAME}"
    echo "Beta version: ${BETA_NUM} (stored in ${BETA_FILE})"
  fi

  # Check with user before proceeding
  echo ""
  read -p "Build and push to Docker Hub? Press [Enter] to continue or Ctrl-C to cancel..."
  
  # Ask if no-cache build is desired
  echo ""
  read -p "Use --no-cache for build? [y/N]: " NO_CACHE_RESPONSE
  NO_CACHE_FLAG=""
  if [ "$NO_CACHE_RESPONSE" == "y" ] || [ "$NO_CACHE_RESPONSE" == "Y" ]; then
    NO_CACHE_FLAG="--no-cache"
    echo "Building with --no-cache"
  else
    echo "Building with cache"
  fi
  
  # Select Dockerfile and determine whether to tag :latest
  if [ "$RELEASE_TYPE" == "2" ]; then
    DOCKERFILE="Dockerfile"
    LATEST_TAG="-t jasonacox/pypowerwall-server:latest"
    PYPW_DEREFFED=false
  else
    DOCKERFILE="Dockerfile.beta"
    LATEST_TAG=""  # Beta builds do not overwrite :latest

    # Docker BuildKit does not follow symlinks that point outside the build context.
    # If pypowerwall/ is a symlink (e.g. to ../pypowerwall/pypowerwall), dereference it
    # into a real directory so the COPY step in Dockerfile.beta works correctly.
    PYPW_DEREFFED=false
    if [ -L "pypowerwall" ]; then
      echo "* Dereferencing pypowerwall symlink for Docker build context..."
      cp -rL pypowerwall pypowerwall_real
      mv pypowerwall pypowerwall_symlink
      mv pypowerwall_real pypowerwall
      PYPW_DEREFFED=true
      # Ensure symlink is always restored even if the build fails
      trap 'if [ "$PYPW_DEREFFED" = true ] && [ -d pypowerwall_symlink ]; then rm -rf pypowerwall; mv pypowerwall_symlink pypowerwall; fi' EXIT
    fi
  fi

  # Build jasonacox/pypowerwall-server:x.y.z
  echo "* BUILD ${CONTAINER_NAME} (using ${DOCKERFILE})"
  docker buildx build -f ${DOCKERFILE} ${NO_CACHE_FLAG} --platform linux/amd64,linux/arm64,linux/arm/v7 --push -t ${CONTAINER_NAME} ${LATEST_TAG} .
  echo ""

  # Verify
  echo "* VERIFY ${CONTAINER_NAME}"
  docker buildx imagetools inspect ${CONTAINER_NAME} | grep Platform
  echo ""
  if [ "$RELEASE_TYPE" == "2" ]; then
    echo "* VERIFY jasonacox/pypowerwall-server:latest"
    docker buildx imagetools inspect jasonacox/pypowerwall-server:latest | grep Platform
    echo ""
  fi

  # Restore pypowerwall symlink if it was dereferenced for the beta build
  if [ "$PYPW_DEREFFED" = true ]; then
    echo "* Restoring pypowerwall symlink..."
    rm -rf pypowerwall
    mv pypowerwall_symlink pypowerwall
  fi

  # Calculate build time
  BUILD_END=$(date +%s)
  BUILD_TIME=$((BUILD_END - BUILD_START))
  BUILD_MIN=$((BUILD_TIME / 60))
  BUILD_SEC=$((BUILD_TIME % 60))

  # Get container sizes for each architecture
  echo "* Fetching container sizes..."
  
  # Get the digest for each platform
  AMD64_DIGEST=$(docker buildx imagetools inspect ${CONTAINER_NAME} --raw 2>/dev/null | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest' 2>/dev/null)
  ARM64_DIGEST=$(docker buildx imagetools inspect ${CONTAINER_NAME} --raw 2>/dev/null | jq -r '.manifests[] | select(.platform.architecture=="arm64") | .digest' 2>/dev/null)
  ARM_V7_DIGEST=$(docker buildx imagetools inspect ${CONTAINER_NAME} --raw 2>/dev/null | jq -r '.manifests[] | select(.platform.architecture=="arm" and .platform.variant=="v7") | .digest' 2>/dev/null)
  
  # Get actual image sizes by inspecting each platform's manifest
  if [ -n "$AMD64_DIGEST" ]; then
    SIZE_AMD64=$(docker buildx imagetools inspect ${CONTAINER_NAME}@${AMD64_DIGEST} --raw 2>/dev/null | jq '[.layers[].size] | add' 2>/dev/null)
    SIZE_AMD64_MB=$((SIZE_AMD64 / 1024 / 1024))
  else
    SIZE_AMD64_MB="N/A"
  fi
  
  if [ -n "$ARM64_DIGEST" ]; then
    SIZE_ARM64=$(docker buildx imagetools inspect ${CONTAINER_NAME}@${ARM64_DIGEST} --raw 2>/dev/null | jq '[.layers[].size] | add' 2>/dev/null)
    SIZE_ARM64_MB=$((SIZE_ARM64 / 1024 / 1024))
  else
    SIZE_ARM64_MB="N/A"
  fi
  
  if [ -n "$ARM_V7_DIGEST" ]; then
    SIZE_ARM_V7=$(docker buildx imagetools inspect ${CONTAINER_NAME}@${ARM_V7_DIGEST} --raw 2>/dev/null | jq '[.layers[].size] | add' 2>/dev/null)
    SIZE_ARM_V7_MB=$((SIZE_ARM_V7 / 1024 / 1024))
  else
    SIZE_ARM_V7_MB="N/A"
  fi

  # Print summary
  echo "=========================================="
  echo "          BUILD SUMMARY"
  echo "=========================================="
  echo "Build Time:      ${BUILD_MIN}m ${BUILD_SEC}s"
  echo "Container Sizes:"
  echo "  - amd64:       ${SIZE_AMD64_MB} MB"
  echo "  - arm64:       ${SIZE_ARM64_MB} MB"
  echo "  - arm/v7:      ${SIZE_ARM_V7_MB} MB"
  echo "Container Name:  ${CONTAINER_NAME}"
  echo "Docker Hub:      https://hub.docker.com/r/jasonacox/pypowerwall-server"
  echo "=========================================="

else
  # Exit script if last_path is not "server"
  echo "Current directory is not 'server'."
  exit 0
fi
