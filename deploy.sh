#!/usr/bin/env bash
# Builds the roon-display image locally, copies it (and the compose file
# and Roon credentials) to the Linux box, and (re)starts it there.
#
# Usage:
#   ./deploy.sh                       # uses defaults below
#   REMOTE_USER=pi ./deploy.sh        # override the SSH user
#   REMOTE_HOST=192.168.1.50 ./deploy.sh
#
# First-time setup on the remote host: Docker (with the `docker compose`
# plugin) must already be installed there. This script doesn't install it.
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-192.168.1.97}"
REMOTE_USER="${REMOTE_USER:-$(whoami)}"
REMOTE_DIR="${REMOTE_DIR:-~/roonalbumdisplay}"
IMAGE_NAME="roonalbumdisplay"
IMAGE_TAG="latest"

cd "$(dirname "$0")"

for f in roon_client/roon_core_id.txt roon_client/roon_token.txt; do
	if [[ ! -f "$f" ]]; then
		echo "Missing $f - run 'uv run roon_client/pair.py' locally first." >&2
		exit 1
	fi
done

echo "==> Building ${IMAGE_NAME}:${IMAGE_TAG} for linux/amd64..."
# Building for a specific platform matters if you're building on Apple
# Silicon and the Linux box is x86_64 - adjust if it's arm64 (e.g. a
# Raspberry Pi).
docker build --platform linux/amd64 -t "${IMAGE_NAME}:${IMAGE_TAG}" roon_client

TARBALL="/tmp/${IMAGE_NAME}.tar.gz"
echo "==> Saving image to ${TARBALL}..."
docker save "${IMAGE_NAME}:${IMAGE_TAG}" | gzip > "$TARBALL"

echo "==> Copying image, compose file, and roon_client/ (incl. credentials) to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}..."
# roon_client/ is copied whole (not just the two credential files) because
# docker-compose.yml's build context points at it - compose expects that
# path to exist even when it won't actually rebuild (the image is already
# loaded below).
ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"
scp "$TARBALL" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
scp docker-compose.yml "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
scp -r roon_client "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

echo "==> Loading image and starting the service on ${REMOTE_HOST}..."
# shellcheck disable=SC2029
ssh "${REMOTE_USER}@${REMOTE_HOST}" "
	set -e
	cd ${REMOTE_DIR}
	gunzip -c $(basename "$TARBALL") | docker load
	docker compose up -d
"

rm -f "$TARBALL"
echo "==> Done. Check status with:"
echo "    ssh ${REMOTE_USER}@${REMOTE_HOST} 'cd ${REMOTE_DIR} && docker compose logs -f'"
