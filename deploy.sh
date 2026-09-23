#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# --- CONFIGURATION ---
IMAGE_NAME="roonalbumdisplay:latest"
TAR_FILE="roonalbumdisplay-linux-amd64.tar"
REMOTE_USER="koehntopp"
REMOTE_HOST="192.168.1.97"
REMOTE_DIR="/opt/stacks/roonalbumdisplay"  # Dockge stack directory (holds compose.yaml + tar during load)
# ---------------------

for f in roon_client/roon_core_id.txt roon_client/roon_token.txt; do
  if [[ ! -f "$f" ]]; then
    echo "Missing $f - run 'uv run roon_client/pair.py' locally first." >&2
    exit 1
  fi
done

echo "🚀 1. Building Docker image for Linux architecture..."
# buildx + --platform is crucial if your Mac is Apple Silicon but the
# server is Intel/AMD.
docker buildx build --platform linux/amd64 -t $IMAGE_NAME --load roon_client

echo "📦 2. Saving image to a tarball archive..."
docker save -o $TAR_FILE $IMAGE_NAME

echo "📁 3. Ensuring remote stack directory exists..."
ssh ${REMOTE_USER}@${REMOTE_HOST} "mkdir -p ${REMOTE_DIR}"

echo "🚀 4. Transferring compose file, Roon credentials, and image via SCP..."
scp compose.yaml ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/compose.yaml
scp roon_client/roon_core_id.txt roon_client/roon_token.txt ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/
scp $TAR_FILE ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/${TAR_FILE}

echo "🧹 5. Cleaning up local tarball on Mac..."
rm $TAR_FILE

echo "🔄 6. Loading image and (re)starting the stack on the Linux box..."
ssh ${REMOTE_USER}@${REMOTE_HOST} << EOF
  set -e
  cd ${REMOTE_DIR}

  echo "📥 Loading image into remote Docker..."
  docker load -i ${TAR_FILE}

  echo "🧹 Removing remote tarball to save space..."
  rm ${TAR_FILE}

  echo "🔄 Recreating containers with Docker Compose..."
  docker compose up -d --remove-orphans
EOF

echo "✅ Deployment complete!"
