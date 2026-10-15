#!/bin/bash
# Deploy this checkout's code to /opt/signal-tv-classic and the iptv command to /usr/local/bin.
# System config (tv.env, systemd units, nginx snippets) is installed separately - see README.
# Then: sudo systemctl restart signal-tv-classic
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
DST=/opt/signal-tv-classic

if [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then
  echo "note: deploying uncommitted changes from $SRC" >&2
fi
# venv/ and demo/media/ are excluded, so --delete leaves them alone
sudo rsync -a --delete --chown=root:root \
  --exclude .git --exclude venv --exclude demo/media --exclude __pycache__ --exclude tests --exclude '*.pyc' \
  "$SRC"/ "$DST"/
[ -x "$DST/venv/bin/python" ] || sudo python3 -m venv "$DST/venv"
sudo "$DST/venv/bin/pip" install --quiet --disable-pip-version-check -r "$DST/requirements.txt"
sudo install -m 0755 "$SRC/deploy/iptv" /usr/local/bin/iptv
# anything else to do after a deploy (e.g. refresh a published copy of the code): the executables in
# post-deploy.d, each given this checkout's path; one that fails is reported, the deploy still counts
for hook in /usr/local/etc/signal-tv-classic/post-deploy.d/*; do
  if [ -x "$hook" ]; then "$hook" "$SRC" || echo "post-deploy hook $hook failed" >&2; fi
done
echo "Deployed $(git -C "$SRC" describe --always --dirty 2>/dev/null || echo '?') to $DST. Restart: sudo systemctl restart signal-tv-classic"
