#!/usr/bin/env bash
# Finish security setup + print intrusion attempt report.
set -euo pipefail

echo "=== FINISHING SECURITY SETUP ==="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "${SCRIPT_DIR}/server-hardening.sh" ]]; then
  bash "${SCRIPT_DIR}/server-hardening.sh"
elif [[ -f /root/server-hardening.sh ]]; then
  bash /root/server-hardening.sh
fi

echo ""
echo "=== SERVICE STATUS ==="
for svc in nginx fail2ban auditd security-watch.timer superjobsearch ufw; do
  printf '%-22s %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
done
ufw status numbered 2>/dev/null | head -15 || true

echo ""
echo "=== INTRUSION CHECK: SSH BRUTE-FORCE (last 50 failures) ==="
if [[ -f /var/log/auth.log ]]; then
  grep -E 'Failed password|Invalid user|authentication failure' /var/log/auth.log 2>/dev/null | tail -50 || echo "(none)"
elif journalctl -u ssh -u sshd --no-pager -n 50 2>/dev/null | grep -E 'Failed|Invalid'; then
  true
else
  echo "(no auth log found)"
fi

echo ""
echo "=== FAIL2BAN BANNED IPs ==="
fail2ban-client status sshd 2>/dev/null || echo "fail2ban not running"

echo ""
echo "=== SECURITY-WATCH ALERTS ==="
if [[ -f /var/log/security-watch/events.log ]]; then
  tail -30 /var/log/security-watch/events.log
else
  /usr/local/lib/security-watch/security-watch.sh 2>/dev/null || true
  tail -30 /var/log/security-watch/events.log 2>/dev/null || echo "(no alerts yet)"
fi

echo ""
echo "=== SUSPICIOUS PROCESSES ==="
ps aux --sort=-%cpu | head -10
ps aux | grep -E 'ando|/m |masscan|brute|perl.*acpid' | grep -v grep || echo "(none)"

echo ""
echo "=== OUTBOUND SSH (should be empty) ==="
ss -H -t state syn-sent 2>/dev/null | grep ':22 ' || echo "(none — good)"
ss -H -t state established 2>/dev/null | grep ':22 ' | grep -v '127.0.0.1' || echo "(none — good)"

echo ""
echo "=== MALWARE PATHS ==="
find /etc/ando /tmp /var/tmp -maxdepth 2 -name 'm' -type f 2>/dev/null || echo "(none — good)"

echo ""
echo "=== RECENT AUDIT EVENTS ==="
ausearch -ts today -k root_ssh_keys,identity,malware_ando,priv_esc 2>/dev/null | tail -20 || echo "(none or auditd starting)"

echo ""
echo "=== DONE ==="
