#!/usr/bin/env bash
# Full server hardening + intrusion monitoring for AISearchJob (Debian 13).
# Run as root: bash deploy/server-hardening.sh
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

APP_USER="${APP_USER:-app}"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/AISearchJob"
DOMAIN="${DOMAIN:-superjobsearch.ru}"

log() { printf '[hardening] %s\n' "$*"; }

log "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
  ufw fail2ban auditd audispd-plugins \
  unattended-upgrades apt-listchanges \
  nginx certbot python3-certbot-nginx \
  python3 python3-venv python3-pip \
  rsync git curl wget ca-certificates \
  lsof net-tools procps \
  rkhunter chkrootkit aide \
  sudo

log "Creating application user: ${APP_USER}..."
if ! id "${APP_USER}" &>/dev/null; then
  useradd -m -s /bin/bash "${APP_USER}"
fi
mkdir -p "${APP_DIR}/AI_Vacancy_Match_Agent"/{data/collected,data/criteria,data/filters,output}
mkdir -p "${APP_HOME}/.ssh"
chmod 700 "${APP_HOME}/.ssh"
chattr -i "${APP_HOME}/.ssh/authorized_keys" 2>/dev/null || true
chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}" "${APP_DIR}" 2>/dev/null || true
chmod 700 "${APP_HOME}" 2>/dev/null || true

log "Configuring SSH hardening..."
SSHD_CFG="/etc/ssh/sshd_config.d/99-aisearchjob-hardening.conf"
mkdir -p /etc/ssh/sshd_config.d
chattr -i /root/.ssh/authorized_keys 2>/dev/null || true
chattr -i /root/.ssh 2>/dev/null || true
mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

log "Setting up deploy SSH key for GitHub Actions..."
DEPLOY_KEY="/root/.ssh/github_actions_deploy"
if [[ ! -f "${DEPLOY_KEY}" ]]; then
  ssh-keygen -t ed25519 -f "${DEPLOY_KEY}" -N "" -C "github-actions-deploy"
fi
grep -qF "$(cat "${DEPLOY_KEY}.pub")" /root/.ssh/authorized_keys 2>/dev/null \
  || cat "${DEPLOY_KEY}.pub" >> /root/.ssh/authorized_keys

if grep -qF "$(cat "${DEPLOY_KEY}.pub")" /root/.ssh/authorized_keys; then
  PASSWORD_AUTH="no"
  log "Deploy key present — disabling password SSH login."
else
  PASSWORD_AUTH="yes"
  log "WARN: Deploy key not in authorized_keys — keeping password login enabled."
fi

cat > "${SSHD_CFG}" <<EOF
PermitRootLogin prohibit-password
PasswordAuthentication ${PASSWORD_AUTH}
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
systemctl reload ssh || systemctl reload sshd

log "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
# Block outbound SSH — prevents botnet SSH scanning (what got the box blocked).
ufw deny out 22/tcp comment 'Block outbound SSH scan/flood'
ufw allow 22/tcp comment 'SSH inbound'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable

log "Configuring fail2ban for SSH..."
cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 3
backend  = systemd

[sshd]
enabled  = true
port     = ssh
filter   = sshd
maxretry = 3
bantime  = 24h
EOF
systemctl enable --now fail2ban

log "Configuring unattended security upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

log "Configuring auditd (step-by-step audit trail)..."
cat > /etc/audit/rules.d/aisearchjob-security.rules <<'EOF'
-D
-b 8192
-f 1

# Identity changes
-w /etc/passwd -p wa -k identity
-w /etc/group -p wa -k identity
-w /etc/shadow -p wa -k identity
-w /etc/gshadow -p wa -k identity
-w /etc/sudoers -p wa -k identity
-w /etc/sudoers.d/ -p wa -k identity

# SSH and auth
-w /etc/ssh/ -p wa -k sshd_config
-w /root/.ssh/ -p wa -k root_ssh_keys
-w /home/app/.ssh/ -p wa -k app_ssh_keys

# Cron and systemd persistence
-w /etc/cron.d/ -p wa -k cron_persist
-w /etc/cron.daily/ -p wa -k cron_persist
-w /var/spool/cron/ -p wa -k cron_persist
-w /etc/systemd/system/ -p wa -k systemd_persist

# Suspicious paths from prior incident
-w /etc/ando/ -p rwxa -k malware_ando
-w /tmp/ -p wa -k tmp_exec
-w /var/tmp/ -p wa -k tmp_exec
-w /dev/shm/ -p wa -k tmp_exec

# App directory integrity
-w /home/app/AISearchJob/ -p wa -k app_files

# Privilege escalation
-a always,exit -F arch=b64 -S execve -F euid=0 -F auid>=1000 -k priv_esc
-a always,exit -F arch=b32 -S execve -F euid=0 -F auid>=1000 -k priv_esc

# Network tools often abused by malware
-w /usr/bin/nc -p x -k net_tools
-w /usr/bin/ncat -p x -k net_tools
-w /usr/bin/curl -p x -k net_tools
-w /usr/bin/wget -p x -k net_tools
EOF
augenrules --load 2>/dev/null || service auditd restart
systemctl enable --now auditd

log "Installing intrusion watch script + timer..."
install -d -m 755 /usr/local/lib/security-watch
WATCH_SRC=""
for candidate in "$(dirname "$0")/security-watch.sh" "/root/security-watch.sh" "${APP_DIR}/deploy/security-watch.sh"; do
  if [[ -f "${candidate}" ]]; then
    WATCH_SRC="${candidate}"
    break
  fi
done
if [[ -z "${WATCH_SRC}" ]]; then
  echo "security-watch.sh not found" >&2
  exit 1
fi
install -m 755 "${WATCH_SRC}" /usr/local/lib/security-watch/security-watch.sh

cat > /etc/systemd/system/security-watch.service <<'EOF'
[Unit]
Description=Security intrusion watch for AISearchJob server
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/lib/security-watch/security-watch.sh
EOF

cat > /etc/systemd/system/security-watch.timer <<'EOF'
[Unit]
Description=Run security watch every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now security-watch.timer

log "Initializing AIDE file integrity baseline..."
if [[ ! -f /var/lib/aide/aide.db ]]; then
  aideinit -y -f 2>/dev/null || true
  if [[ -f /var/lib/aide/aide.db.new ]]; then
    mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
  fi
fi
cat > /etc/cron.daily/aide-check <<'EOF'
#!/bin/sh
/usr/bin/aide --check >> /var/log/aide-check.log 2>&1 || true
EOF
chmod 755 /etc/cron.daily/aide-check

log "Configuring nginx reverse proxy..."
NGINX_SRC=""
for candidate in "$(dirname "$0")/nginx-superjobsearch.conf" "${APP_DIR}/deploy/nginx-superjobsearch.conf" "/root/nginx-superjobsearch.conf"; do
  if [[ -f "${candidate}" ]]; then
    NGINX_SRC="${candidate}"
    break
  fi
done
if [[ -n "${NGINX_SRC}" ]] && [[ -f /etc/letsencrypt/live/${DOMAIN}/fullchain.pem ]]; then
  install -m 0644 "${NGINX_SRC}" /etc/nginx/sites-available/superjobsearch
elif [[ -n "${NGINX_SRC}" ]]; then
  install -m 0644 "${NGINX_SRC}" /etc/nginx/sites-available/superjobsearch
else
cat > /etc/nginx/sites-available/superjobsearch <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${DOMAIN} www.${DOMAIN} _;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_connect_timeout 60s;
    }
}
EOF
fi
ln -sf /etc/nginx/sites-available/superjobsearch /etc/nginx/sites-enabled/superjobsearch
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/superjobsearch.ru
nginx -t
systemctl enable --now nginx

log "Installing superjobsearch systemd unit (placeholder until deploy)..."
for unit in "${APP_DIR}/deploy/superjobsearch.service" "$(dirname "$0")/superjobsearch.service" "/root/superjobsearch.service"; do
  if [[ -f "${unit}" ]]; then
    install -m 0644 "${unit}" /etc/systemd/system/superjobsearch.service
    break
  fi
done
systemctl daemon-reload
systemctl enable superjobsearch 2>/dev/null || true

log "Creating security log directory..."
install -d -m 750 /var/log/security-watch
touch /var/log/security-watch/events.log /var/log/security-watch/daily-report.log
chmod 640 /var/log/security-watch/*.log

log "Done. Summary:"
echo "  App user:     ${APP_USER}"
echo "  App dir:      ${APP_DIR}"
echo "  Deploy key:   ${DEPLOY_KEY}.pub (add private key to GitHub SERVER_SSH_KEY secret)"
echo "  SSH:          key-only (password login disabled)"
echo "  UFW:          inbound 22/80/443; outbound SSH blocked"
echo "  fail2ban:     active"
echo "  auditd:       active — ausearch -k malware_ando / aureport"
echo "  security-watch: every 5 min — /var/log/security-watch/events.log"
echo "  AIDE:         daily integrity check"
echo ""
echo "NEXT: Update GitHub secret SERVER_SSH_KEY with:"
echo "  cat ${DEPLOY_KEY}"
echo ""
echo "Then push to main to deploy the app."
