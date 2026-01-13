#!/bin/bash
echo "Build and Push jasonacox/pypowerwall-server to Docker Hub"
echo "Usage: $0 [beta_number]"
echo "  If beta_number is not provided, auto-increments from last beta version"
echo ""

last_path=$(basename $PWD)
if [ "$last_path" == "pypowerwall-server" ]; then
  # Determine version
  SERVER_VERSION=`grep "SERVER_VERSION = " app/config.py | cut -d\" -f2`
  
  # Handle beta numbering
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

  # Check with user before proceeding
  echo "Build and push jasonacox/pypowerwall-server:${VER} to Docker Hub?"
  echo "Beta version: ${BETA_NUM} (stored in ${BETA_FILE})"
  read -p "Press [Enter] to continue or Ctrl-C to cancel..."
  
  # Build jasonacox/pypowerwall-server:x.y.z
  echo "* BUILD jasonacox/pypowerwall-server:${VER}"
  docker buildx build -f Dockerfile --no-cache --platform linux/amd64,linux/arm64,linux/arm/v7 --push -t jasonacox/pypowerwall-server:${VER} .
  echo ""

  # Verify
  echo "* VERIFY jasonacox/pypowerwall-server:${VER}"
  docker buildx imagetools inspect jasonacox/pypowerwall-server:${VER} | grep Platform
  echo ""
  echo "* VERIFY jasonacox/pypowerwall-server:latest"
  docker buildx imagetools inspect jasonacox/pypowerwall-server | grep Platform
  echo ""

else
  # Exit script if last_path is not "server"
  echo "Current directory is not 'server'."
  exit 0
fi
