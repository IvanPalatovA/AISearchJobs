#!/usr/bin/env bash
# Run once on the server (console or SSH) to allow GitHub Actions deploy.
set -euo pipefail

DEPLOY_KEY="/root/.ssh/github_actions_deploy"
AUTH_KEYS="/root/.ssh/authorized_keys"

mkdir -p /root/.ssh
chmod 700 /root/.ssh
chattr -i "$AUTH_KEYS" 2>/dev/null || true
chattr -i /root/.ssh 2>/dev/null || true

if [[ ! -f "${DEPLOY_KEY}" ]]; then
  ssh-keygen -t ed25519 -f "${DEPLOY_KEY}" -N "" -C "github-actions-deploy"
  echo ""
  echo "Created new deploy key. Update GitHub secret SERVER_SSH_KEY with:"
  cat "${DEPLOY_KEY}"
  echo ""
fi

touch "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

if grep -qF "$(cat "${DEPLOY_KEY}.pub")" "$AUTH_KEYS"; then
  echo "Deploy public key already authorized."
else
  cat "${DEPLOY_KEY}.pub" >> "$AUTH_KEYS"
  echo "Deploy public key added to ${AUTH_KEYS}"
fi

echo "Fingerprint (must match GitHub Actions log):"
ssh-keygen -lf "${DEPLOY_KEY}.pub"

echo "authorized_keys lines:"
wc -l "$AUTH_KEYS"
