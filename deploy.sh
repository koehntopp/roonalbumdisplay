#!/bin/bash
#
# Builds the image, ships it to the Docker host, and recreates the
# Dockge-managed "roonalbumdisplay" stack there.
#
#   ./deploy.sh           # code change: build, ship image, recreate container
#   ./deploy.sh --creds   # also push re-paired Roon files to the NAS and
#                         # force-recreate (only needed after a new pair.py run)
#
# What this script deliberately does NOT do: write a compose file or the
# Roon credentials into a stack folder on the Docker host. The live stack's
# compose.yml is managed in Dockge (on the NAS), and a "./" path in it
# resolves on the *host's* local disk, not in Dockge's stacks folder - which
# is how stale local copies of the credentials ended up on the host before.
# See AGENTS.md ("Docker deployment").

# Exit immediately if a command exits with a non-zero status
set -e

# --- CONFIGURATION ---
IMAGE_NAME="roonalbumdisplay:latest"
TAR_FILE="roonalbumdisplay-linux-amd64.tar"
REMOTE_USER="koehntopp"
REMOTE_HOST="192.168.1.97"   # Docker host
REMOTE_TMP="/tmp"            # where the image tarball lands (not a stack dir)
DOCKGE_CONTAINER="dockge-dockge-agent-1"
STACK="roonalbumdisplay"     # folder name under Dockge's /opt/stacks
NAS_HOST="192.168.1.98"
# Same path as the compose file's NFS volume `device:`. Over SSH this is
# /volume1/docker/...; Synology's SFTP (and so scp) shows it as /docker/...
# instead, which is why files are written through the SSH shell below.
NAS_CREDS_DIR="/volume1/docker/discogs/roonalbumdisplay"
# ---------------------

PUSH_CREDS=0
for arg in "$@"; do
  case "$arg" in
    --creds) PUSH_CREDS=1 ;;
    *) echo "Unknown option: $arg (only --creds is supported)" >&2; exit 1 ;;
  esac
done

if [[ $PUSH_CREDS -eq 1 ]]; then
  for f in roon_client/roon_core_id.txt roon_client/roon_token.txt; do
    if [[ ! -f "$f" ]]; then
      echo "Missing $f - run 'uv run roon_client/pair.py' locally first." >&2
      exit 1
    fi
  done
fi

echo "🚀 1. Building Docker image for Linux architecture..."
# buildx + --platform is crucial if your Mac is Apple Silicon but the
# server is Intel/AMD.
docker buildx build --platform linux/amd64 -t $IMAGE_NAME --load roon_client

echo "📦 2. Saving image to a tarball archive..."
docker save -o $TAR_FILE $IMAGE_NAME

echo "🚀 3. Transferring image via SCP..."
scp $TAR_FILE ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_TMP}/${TAR_FILE}

echo "🧹 4. Cleaning up local tarball on Mac..."
rm $TAR_FILE

if [[ $PUSH_CREDS -eq 1 ]]; then
  echo "🔑 5. Pushing Roon pairing files to the NAS (${NAS_HOST}:${NAS_CREDS_DIR})..."
  for f in roon_core_id.txt roon_token.txt; do
    ssh ${REMOTE_USER}@${NAS_HOST} "cat > ${NAS_CREDS_DIR}/${f}" < roon_client/${f}
  done
else
  echo "🔑 5. Leaving Roon pairing files on the NAS untouched (use --creds after re-pairing)."
fi

echo "🔄 6. Loading image and recreating the stack via Dockge's container on the Linux box..."
# Runs compose the way Dockge itself does: inside its container, against the
# stack folder it manages - not from a directory on the host.
if [[ $PUSH_CREDS -eq 1 ]]; then
  UP_FLAGS="--remove-orphans --force-recreate"
else
  UP_FLAGS="--remove-orphans"
fi
ssh ${REMOTE_USER}@${REMOTE_HOST} << EOF
  set -e

  echo "📥 Loading image into remote Docker..."
  docker load -i ${REMOTE_TMP}/${TAR_FILE}

  echo "🧹 Removing remote tarball to save space..."
  rm ${REMOTE_TMP}/${TAR_FILE}

  echo "🔄 Recreating containers with Docker Compose (via ${DOCKGE_CONTAINER})..."
  docker exec ${DOCKGE_CONTAINER} sh -c "cd /opt/stacks/${STACK} && docker compose up -d ${UP_FLAGS}"

  echo "📋 Status:"
  docker exec ${DOCKGE_CONTAINER} sh -c "cd /opt/stacks/${STACK} && docker compose ps"
EOF

echo "✅ Deployment complete!"
