# Server security setup

Scripts for hardening the AISearchJob VDS after reinstall.

## Quick setup (from your laptop)

```bash
scp deploy/{server-hardening,security-watch,superjobsearch.service}.sh root@109.205.58.239:/root/
ssh root@109.205.58.239 'bash /root/server-hardening.sh'
```

## If SSH password stopped working

Use **Sprintbox web console** (VNC), login as root, then:

```bash
bash /root/console-recovery.sh
# or paste contents of deploy/console-recovery.sh
bash /root/server-hardening.sh
```

## What gets installed

| Component | Purpose |
|---|---|
| **UFW** | Firewall: inbound 22/80/443; **blocks outbound SSH** (anti botnet) |
| **fail2ban** | Bans IPs after 3 failed SSH attempts (24h) |
| **auditd** | Step-by-step audit log: file changes, SSH keys, privilege escalation |
| **security-watch** | Every 5 min checks for Ando-style malware patterns |
| **AIDE** | Daily file integrity baseline |
| **unattended-upgrades** | Automatic security patches |
| **nginx** | Reverse proxy → `127.0.0.1:8000` (web_app.py) |

## Monitoring commands

```bash
# Real-time alerts (fake acpid, /etc/ando, outbound SSH scan)
tail -f /var/log/security-watch/events.log

# Audit trail — who changed SSH keys, /etc, sudo
ausearch -k root_ssh_keys -ts today
aureport -k --summary

# fail2ban
fail2ban-client status sshd

# AIDE integrity
tail /var/log/aide-check.log
```

## GitHub Actions deploy key

After hardening, copy private key to GitHub secret `SERVER_SSH_KEY`:

```bash
cat /root/.ssh/github_actions_deploy
```

Set secrets: `SERVER_HOST=109.205.58.239`, `SERVER_USER=root`.

`SERVER_SSH_KEY` must be the **private** key matching a line in `/root/.ssh/authorized_keys`:

```bash
# on server — show public key that must be authorized
cat /root/.ssh/github_actions_deploy.pub

# on server — private key goes to GitHub secret (full PEM, including BEGIN/END lines)
cat /root/.ssh/github_actions_deploy
```

Verify fingerprint on server and in GitHub Actions log (Configure SSH step):

```bash
ssh-keygen -lf /root/.ssh/github_actions_deploy.pub
```

## App is not affected

- Outbound **HTTPS** (hh.ru, Telegram, Resend, LLM API) — allowed
- Outbound **SSH port 22** — blocked (this is what caused the Sprintbox ban)
- `web_app.py` listens on localhost only; nginx serves public HTTP/HTTPS
