#!/usr/bin/env bash
# Run in Sprintbox VNC/console if SSH password login stopped working.
set -euo pipefail

echo "[recovery] Fixing SSH access..."

chattr -i /root/.ssh/authorized_keys 2>/dev/null || true
chattr -i /root/.ssh 2>/dev/null || true
mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

if [[ -f /root/.ssh/github_actions_deploy.pub ]]; then
  grep -qF "$(cat /root/.ssh/github_actions_deploy.pub)" /root/.ssh/authorized_keys \
    || cat /root/.ssh/github_actions_deploy.pub >> /root/.ssh/authorized_keys
  echo "[recovery] Deploy key added to authorized_keys"
else
  ssh-keygen -t ed25519 -f /root/.ssh/github_actions_deploy -N "" -C "github-actions-deploy"
  cat /root/.ssh/github_actions_deploy.pub >> /root/.ssh/authorized_keys
  echo "[recovery] New deploy key generated — update GitHub SERVER_SSH_KEY secret:"
  cat /root/.ssh/github_actions_deploy
fi

mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-aisearchjob-hardening.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication yes
PubkeyAuthentication yes
MaxAuthTries 5
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
systemctl reload ssh
echo "[recovery] SSH reloaded. Test key login, then run: bash /root/server-hardening.sh"
