#!/usr/bin/env python3
"""Deploy GarvynLabs static site + admin API to VPS via paramiko."""

import io
import os
import sys
import tarfile
from pathlib import Path

import paramiko

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPT_DIR.parent.parent.parent   # OpenClaw-test/
GL_ROOT     = SCRIPT_DIR.parent.parent           # GarvynLabs/
WEB_PUBLIC  = GL_ROOT / "apps" / "web" / "public"
API_DIR     = GL_ROOT / "apps" / "api"
NGINX_CONF  = GL_ROOT / "deploy" / "nginx" / "garvynlabs.conf"

# ── target ───────────────────────────────────────────────────────────────────
HOST     = "172.245.147.100"
USER     = "root"
PASSWORD = "20260422GY"
WEB_ROOT = "/var/www/garvynlabs"


def make_tar_bytes(source_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(source_dir)
                tar.add(str(path), arcname=str(arcname))
    return buf.getvalue()


def upload_bytes(sftp, data: bytes, remote_path: str) -> None:
    with sftp.open(remote_path, "wb") as f:
        f.write(data)


def run(ssh, cmd: str) -> str:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}):\n{cmd}\n{err}")
    return out


def main() -> None:
    print(f"Connecting to {USER}@{HOST} ...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    sftp = ssh.open_sftp()

    # ── pack and upload ───────────────────────────────────────────────────
    print("Packing web/public ...")
    web_tar = make_tar_bytes(WEB_PUBLIC)
    print(f"  {len(web_tar)//1024} KB -> /tmp/garvynlabs-web.tar.gz")
    upload_bytes(sftp, web_tar, "/tmp/garvynlabs-web.tar.gz")

    print("Packing api ...")
    api_tar = make_tar_bytes(API_DIR)
    print(f"  {len(api_tar)//1024} KB -> /tmp/garvynlabs-api.tar.gz")
    upload_bytes(sftp, api_tar, "/tmp/garvynlabs-api.tar.gz")

    print("Uploading nginx config …")
    sftp.put(str(NGINX_CONF), "/tmp/garvynlabs.conf")
    sftp.close()

    # ── remote commands ───────────────────────────────────────────────────
    print("Deploying on server …")

    run(ssh, f"mkdir -p {WEB_ROOT}")
    run(ssh, f"tar xzf /tmp/garvynlabs-web.tar.gz -C {WEB_ROOT}")
    print("  [ok] web files deployed")

    run(ssh, "mkdir -p /opt/GarvynLabs/api")
    run(ssh, "tar xzf /tmp/garvynlabs-api.tar.gz -C /opt/GarvynLabs/api")
    print("  [ok] api files deployed")

    # create admin env only on first deploy
    check = run(ssh, "test -f /etc/garvynlabs-admin.env && echo exists || echo missing")
    if "missing" in check:
        run(ssh, r"""
PASS=$(openssl rand -base64 18)
cat > /etc/garvynlabs-admin.env <<EOF
GARVYNLABS_ADMIN_USER=garvyn
GARVYNLABS_ADMIN_PASSWORD=$PASS
GARVYNLABS_SITE_ROOT=/var/www/garvynlabs
GARVYNLABS_ADMIN_HOST=127.0.0.1
GARVYNLABS_ADMIN_PORT=8090
EOF
chmod 600 /etc/garvynlabs-admin.env
""")
        print("  [ok] admin credentials created")
    else:
        print("  [ok] admin credentials already exist (skipped)")

    run(ssh, r"""cat > /etc/systemd/system/garvynlabs-admin.service <<'EOF'
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
""")
    run(ssh, "systemctl daemon-reload")
    run(ssh, "systemctl enable garvynlabs-admin && systemctl restart garvynlabs-admin")
    print("  [ok] garvynlabs-admin service restarted")

    run(ssh, "install -m 0644 /tmp/garvynlabs.conf /etc/nginx/sites-available/garvynlabs")
    run(ssh, "rm -f /etc/nginx/sites-enabled/default")
    run(ssh, "ln -sf /etc/nginx/sites-available/garvynlabs /etc/nginx/sites-enabled/garvynlabs")
    run(ssh, "nginx -t")
    run(ssh, "systemctl reload nginx")
    print("  [ok] nginx reloaded")

    # print admin password so user can log in
    pw_out = run(ssh, "grep ADMIN_PASSWORD /etc/garvynlabs-admin.env")
    ssh.close()

    print("\nDone!")
    print(f"  Site:  http://{HOST}/")
    print(f"  Admin: http://{HOST}/admin/")
    print(f"  {pw_out.strip()}")


if __name__ == "__main__":
    main()
