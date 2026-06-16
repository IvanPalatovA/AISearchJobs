#!/usr/bin/env bash
# Fix GitHub Actions deploy SSH access. Run as root on the server console.
set -euo pipefail

DEPLOY_KEY="/root/.ssh/github_actions_deploy"
AUTH_KEYS="/root/.ssh/authorized_keys"
BACKUP="/root/.ssh/authorized_keys.bak.$(date +%Y%m%d_%H%M%S)"

echo "=== SSH deploy fix ==="

mkdir -p /root/.ssh
chmod 700 /root/.ssh
chattr -i "$AUTH_KEYS" 2>/dev/null || true
chattr -i /root/.ssh 2>/dev/null || true

if [[ ! -f "${DEPLOY_KEY}" ]]; then
  ssh-keygen -t ed25519 -f "${DEPLOY_KEY}" -N "" -C "github-actions-deploy"
  echo ""
  echo "NEW key created — update GitHub secret SERVER_SSH_KEY:"
  cat "${DEPLOY_KEY}"
  echo ""
fi

[[ -f "$AUTH_KEYS" ]] && cp "$AUTH_KEYS" "$BACKUP" && echo "Backup: $BACKUP"

# Clean authorized_keys: one key per line, deploy key guaranteed present.
PUB="$(cat "${DEPLOY_KEY}.pub")"
{
  if [[ -f "$AUTH_KEYS" ]]; then
    grep -v '^[[:space:]]*$' "$AUTH_KEYS" | grep -v '^#' || true
  fi
  echo "$PUB"
} | awk '!seen[$0]++' > "${AUTH_KEYS}.new"

mv "${AUTH_KEYS}.new" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"
chown root:root "$AUTH_KEYS" /root/.ssh

echo ""
echo "=== Fingerprint (must match GitHub Actions log) ==="
ssh-keygen -lf "${DEPLOY_KEY}.pub"

echo ""
echo "=== authorized_keys ==="
cat "$AUTH_KEYS"

echo ""
echo "=== sshd effective settings ==="
sshd -T 2>/dev/null | grep -E 'permitrootlogin|pubkeyauthentication|passwordauthentication|authorizedkeysfile' || true

echo ""
echo "=== local SSH test ==="
ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i "${DEPLOY_KEY}" root@127.0.0.1 "echo LOCAL_SSH_OK" || {
  echo "LOCAL TEST FAILED — check sshd logs: journalctl -u ssh -n 20 --no-pager"
  exit 1
}

echo ""
echo "=== DONE ==="
echo "If LOCAL_SSH_OK above, re-run GitHub Actions deploy."
echo "GitHub secrets: SERVER_USER=root, SERVER_HOST=$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
