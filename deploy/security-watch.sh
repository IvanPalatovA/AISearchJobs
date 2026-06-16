#!/usr/bin/env bash
# Detects suspicious activity patterns (based on prior Ando V2 incident).
# Logs step-by-step events to /var/log/security-watch/events.log
set -uo pipefail

LOG_DIR="/var/log/security-watch"
EVENT_LOG="${LOG_DIR}/events.log"
REPORT_LOG="${LOG_DIR}/daily-report.log"
TIMESTAMP="$(date -Is)"
HOST="$(hostname -s)"

mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG}" "${REPORT_LOG}"

log_event() {
  local severity="$1"
  shift
  printf '%s [%s] %s host=%s %s\n' "${TIMESTAMP}" "${severity}" "${HOST}" "$*" >> "${EVENT_LOG}"
}

ALERTS=0

# 1. Fake acpid / perl masquerading (prior incident: exe -> perl, no real acpid binary)
while read -r line; do
  pid="$(echo "$line" | awk '{print $2}')"
  [[ -z "$pid" || "$pid" == "PID" ]] && continue
  exe="$(readlink "/proc/${pid}/exe" 2>/dev/null || echo unknown)"
  cmd="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || echo unknown)"
  if [[ "$exe" == *perl* ]] || [[ "$cmd" == *acpid* && "$exe" != "/usr/sbin/acpid" ]]; then
    log_event "CRITICAL" "suspicious_process pid=${pid} exe=${exe} cmd=${cmd}"
    ALERTS=$((ALERTS + 1))
  fi
done < <(ps aux 2>/dev/null | grep -E '[a]cpid|[p]erl' || true)

# 2. Known malware paths
if [[ -d /etc/ando ]]; then
  log_event "CRITICAL" "malware_dir_found path=/etc/ando contents=$(ls -la /etc/ando 2>/dev/null | tr '\n' ';')"
  ALERTS=$((ALERTS + 1))
fi

find /etc /tmp /var/tmp /dev/shm -maxdepth 3 -name 'm' -type f 2>/dev/null | while read -r f; do
  if file "$f" 2>/dev/null | grep -q ELF; then
    log_event "CRITICAL" "suspicious_elf name=m path=${f}"
    ALERTS=$((ALERTS + 1))
  fi
done

# 3. Outbound SSH connections (syn-flood indicator)
OUT_SSH="$(ss -H -t state syn-sent 2>/dev/null | grep ':22 ' || true)"
if [[ -n "$OUT_SSH" ]]; then
  log_event "CRITICAL" "outbound_ssh_syn ${OUT_SSH}"
  ALERTS=$((ALERTS + 1))
fi

ESTAB_SSH_OUT="$(ss -H -t state established 2>/dev/null | awk '{print $4,$5}' | grep -v '127.0.0.1:22' | grep ':22 ' || true)"
if [[ -n "$ESTAB_SSH_OUT" ]]; then
  log_event "WARN" "outbound_ssh_established ${ESTAB_SSH_OUT}"
  ALERTS=$((ALERTS + 1))
fi

# 4. High CPU processes (excluding known good)
ps aux --sort=-%cpu 2>/dev/null | awk 'NR>1 && $3+0 > 50 {print}' | while read -r line; do
  cmd="$(echo "$line" | awk '{for(i=11;i<=NF;i++) printf $i" "; print ""}')"
  if ! echo "$cmd" | grep -qE 'python.*web_app|nginx|systemd|kworker|aide|rkhunter'; then
    log_event "WARN" "high_cpu ${line}"
    ALERTS=$((ALERTS + 1))
  fi
done

# 5. New/modified SSH authorized_keys
for keyfile in /root/.ssh/authorized_keys /home/app/.ssh/authorized_keys; do
  if [[ -f "$keyfile" ]]; then
    hash="$(sha256sum "$keyfile" | awk '{print $1}')"
    hashfile="${LOG_DIR}/.hash_$(echo "$keyfile" | tr '/' '_')"
    if [[ -f "$hashfile" ]]; then
      old="$(cat "$hashfile")"
      if [[ "$old" != "$hash" ]]; then
        log_event "CRITICAL" "ssh_keys_changed file=${keyfile} old_sha=${old} new_sha=${hash}"
        ALERTS=$((ALERTS + 1))
      fi
    fi
    echo "$hash" > "$hashfile"
  fi
done

# 6. Unexpected listeners (not ssh/nginx/systemd-resolved)
ss -H -tlnp 2>/dev/null | while read -r line; do
  port="$(echo "$line" | awk '{print $4}' | rev | cut -d: -f1 | rev)"
  case "$port" in
    22|80|443|53|8000|8001|8002|8003|8004|8005) continue ;;
  esac
  if ! echo "$line" | grep -qE 'nginx|sshd|systemd-resolved|python.*web_app'; then
    log_event "WARN" "unexpected_listener port=${port} ${line}"
    ALERTS=$((ALERTS + 1))
  fi
done

# 7. Screen/tmux sessions under root (attackers used screen)
if command -v screen >/dev/null 2>&1; then
  screens="$(screen -ls 2>/dev/null | grep -v 'No Sockets' || true)"
  if [[ -n "$screens" ]]; then
    log_event "INFO" "screen_sessions ${screens}"
  fi
fi

# 8. Recent audit events summary (last 5 min)
if command -v ausearch >/dev/null 2>&1; then
  audit_hits="$(ausearch -ts recent -k malware_ando,identity,root_ssh_keys,priv_esc 2>/dev/null | tail -5 | tr '\n' ' ' || true)"
  if [[ -n "$audit_hits" ]]; then
    log_event "INFO" "recent_audit ${audit_hits}"
  fi
fi

# Daily summary at midnight hour
if [[ "$(date +%H)" == "00" && "$(date +%M)" -lt 6 ]]; then
  {
    echo "=== Daily security report ${TIMESTAMP} ==="
    echo "Alerts in last 24h: $(grep -c CRITICAL "${EVENT_LOG}" 2>/dev/null || echo 0)"
    echo "Recent critical events:"
    grep CRITICAL "${EVENT_LOG}" 2>/dev/null | tail -20 || true
    echo ""
    echo "fail2ban banned IPs:"
    fail2ban-client status sshd 2>/dev/null || true
    echo ""
    echo "UFW status:"
    ufw status numbered 2>/dev/null || true
  } >> "${REPORT_LOG}"
fi

if [[ "$ALERTS" -gt 0 ]]; then
  logger -t security-watch "AISearchJob security-watch: ${ALERTS} alert(s) — see ${EVENT_LOG}"
fi

exit 0
