#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.deploy}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

HOST="${DEPLOY_HOST:?DEPLOY_HOST is required}"
USER="${DEPLOY_USER:-root}"
WEB_ROOT="${WEB_ROOT:-/var/www/garvynlabs}"
ARCHIVE="/tmp/garvynlabs-web.tar.gz"
API_ARCHIVE="/tmp/garvynlabs-api.tar.gz"

tar -C "$ROOT_DIR/apps/web/public" -czf "$ARCHIVE" .
tar -C "$ROOT_DIR/apps/api" -czf "$API_ARCHIVE" .
scp "$ARCHIVE" "$USER@$HOST:/tmp/garvynlabs-web.tar.gz"
scp "$API_ARCHIVE" "$USER@$HOST:/tmp/garvynlabs-api.tar.gz"
scp "$ROOT_DIR/deploy/nginx/garvynlabs.conf" "$USER@$HOST:/tmp/garvynlabs.conf"

ssh "$USER@$HOST" "set -e
mkdir -p '$WEB_ROOT'
tar xzf /tmp/garvynlabs-web.tar.gz -C '$WEB_ROOT'
mkdir -p /opt/GarvynLabs/api
tar xzf /tmp/garvynlabs-api.tar.gz -C /opt/GarvynLabs/api
if [ ! -f /etc/garvynlabs-admin.env ]; then
  ADMIN_PASSWORD=\$(openssl rand -base64 18)
  cat > /etc/garvynlabs-admin.env <<EOF
GARVYNLABS_ADMIN_USER=garvyn
GARVYNLABS_ADMIN_PASSWORD=\$ADMIN_PASSWORD
GARVYNLABS_SITE_ROOT=$WEB_ROOT
GARVYNLABS_ADMIN_HOST=127.0.0.1
GARVYNLABS_ADMIN_PORT=8090
EOF
  chmod 600 /etc/garvynlabs-admin.env
  echo \"Created admin credentials in /etc/garvynlabs-admin.env\"
fi
cat > /etc/systemd/system/garvynlabs-admin.service <<EOF
[Unit]
Description=Garvyn Labs Admin API
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/opt/GarvynLabs/api
EnvironmentFile=/etc/garvynlabs-admin.env
ExecStart=/usr/bin/python3 /opt/GarvynLabs/api/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now garvynlabs-admin
install -m 0644 /tmp/garvynlabs.conf /etc/nginx/sites-available/garvynlabs
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/garvynlabs /etc/nginx/sites-enabled/garvynlabs
nginx -t
systemctl reload nginx
"

echo "Deployed Garvyn Labs static site to $USER@$HOST:$WEB_ROOT"
