#!/bin/bash
# Startup script for the careplix-openemr VM (Debian 12): Docker Engine + Compose from Docker's
# apt repository, plus what scripts/openemr_setup.py needs. Runs on every boot; exits early once
# installed. The OpenEMR containers restart on their own (restart: unless-stopped).
set -euo pipefail
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  exit 0
fi
apt-get update
apt-get install -y ca-certificates curl python3-httpx
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
