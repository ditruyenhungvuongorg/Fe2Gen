#!/usr/bin/env bash
# Host hardening of the Fe2Gen Ubuntu server (B20). Run by hand, never from a timer or CI:
#   sudo bash harden_host.sh --dry-run                       print every step and command, change nothing
#   sudo bash harden_host.sh --ssh-keys-tested               apply all steps
#   options: --lan-ssh 192.168.1.0/24   also keep SSH open from that LAN range
#            --skip-ufw --skip-ssh --skip-upgrades --skip-fail2ban
# Every step checks the current state first, so the script can be run again.
#
# BEFORE the SSH step: open a SECOND terminal and log in with your SSH key (or Tailscale SSH) WITHOUT a password.
# Keep that session open until the script ends and a third login works. Without --ssh-keys-tested the sshd step
# is skipped. The firewall only lets SSH in through Tailscale (and --lan-ssh); a LAN-only SSH session is cut.
# Not changed here: Tailscale itself, the web service, NVIDIA packages. Rollback commands are printed per step.
set -euo pipefail

DRY=0 SSH_TESTED=0 LAN_SSH="" SKIP_UFW=0 SKIP_SSH=0 SKIP_UPGRADES=0 SKIP_F2B=0
TAILNET4=100.64.0.0/10
TAILNET6=fd7a:115c:a1e0::/48
STAMP="$(date +%Y%m%d-%H%M%S)"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --ssh-keys-tested) SSH_TESTED=1 ;;
    --lan-ssh) LAN_SSH="${2:?--lan-ssh needs a CIDR, e.g. 192.168.1.0/24}"; shift ;;
    --skip-ufw) SKIP_UFW=1 ;;
    --skip-ssh) SKIP_SSH=1 ;;
    --skip-upgrades) SKIP_UPGRADES=1 ;;
    --skip-fail2ban) SKIP_F2B=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then echo "Run with sudo (or add --dry-run)." >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive

step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
run() {  # print, then run unless --dry-run
  printf '   + %s\n' "$*"
  if [ "$DRY" = 0 ]; then "$@"; fi
}
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }
ensure_pkg() {
  if installed "$1"; then note "$1: already installed"; else run apt-get install -y "$1"; fi
}
# put_file PATH CONTENT: write only when different; an existing different file is kept as PATH.bak-fe2gen-STAMP
put_file() {
  local path="$1" content="$2"
  if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then note "$path: already up to date"; return 1; fi
  printf '   + write %s:\n' "$path"
  printf '%s\n' "$content" | sed 's/^/       | /'
  if [ "$DRY" = 0 ]; then
    if [ -f "$path" ]; then cp -p "$path" "$path.bak-fe2gen-$STAMP"; fi
    printf '%s\n' "$content" > "$path"
    chmod 644 "$path"
  fi
  return 0
}
in_tailnet() {  # $1 = IP address
  python3 - "$1" "$TAILNET4" "$TAILNET6" <<'PY'
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1].strip('[]').split('%')[0])
ip = getattr(ip, 'ipv4_mapped', None) or ip
ok = ip.is_loopback or any(ip in ipaddress.ip_network(n) for n in sys.argv[2:] if ipaddress.ip_network(n).version == ip.version)
sys.exit(0 if ok else 1)
PY
}

[ "$DRY" = 1 ] && echo "DRY RUN: nothing is changed."
[ "$DRY" = 0 ] && ! command -v apt-get >/dev/null && { echo "apt-get not found; this script is for Ubuntu." >&2; exit 1; }

# ---------------------------------------------------------------- 1. firewall
if [ "$SKIP_UFW" = 0 ]; then
  step "1. ufw: deny incoming; allow everything on tailscale0; SSH only from the tailnet"
  if ! ip link show tailscale0 >/dev/null 2>&1; then
    echo "   tailscale0 does not exist: Tailscale is down, enabling the firewall now could cut you off. Step skipped." >&2
  else
    LAN_PEERS=""
    while read -r peer; do
      [ -n "$peer" ] || continue
      addr="${peer%:*}"
      if ! in_tailnet "$addr"; then LAN_PEERS="$LAN_PEERS $addr"; fi
    done < <(ss -Htn state established '( sport = :22 )' 2>/dev/null | awk '{print $NF}')
    if [ -n "$LAN_PEERS" ] && [ -z "$LAN_SSH" ]; then
      echo "   Open SSH sessions from outside the tailnet:$LAN_PEERS" >&2
      echo "   They would be cut. Reconnect through Tailscale, or rerun with --lan-ssh <CIDR>. Step skipped." >&2
    else
      ensure_pkg ufw
      run ufw default deny incoming
      run ufw default allow outgoing
      run ufw allow in on tailscale0 comment 'tailnet (Funnel, serve, Tailscale SSH)'
      # The rule above already covers SSH from the tailnet; this one keeps it explicit and tied to tailscale0,
      # so a spoofed 100.64.x.x source on the LAN interface does not match it.
      run ufw allow in on tailscale0 from "$TAILNET4" to any port 22 proto tcp comment 'SSH from tailnet'
      # WireGuard port of tailscaled: direct connections instead of DERP relays; unauthenticated packets are dropped.
      run ufw allow 41641/udp comment 'tailscale wireguard'
      if [ -n "$LAN_SSH" ]; then run ufw allow from "$LAN_SSH" to any port 22 proto tcp comment 'SSH from LAN'; fi
      if ufw status 2>/dev/null | grep -q '^Status: active'; then note "ufw already active (rules added above if missing)"
      else run ufw --force enable; fi
      [ "$DRY" = 0 ] && ufw status verbose | sed 's/^/   /'
      note "Note: services listening on the LAN (llama-server, LM Studio...) are now reachable only through Tailscale."
      note "Rollback: sudo ufw disable   (rules stay; sudo ufw reset removes them)"
    fi
  fi
fi

# ---------------------------------------------------------------- 2. sshd
if [ "$SKIP_SSH" = 0 ]; then
  step "2. sshd: keys only, no root login"
  SSHD_DROPIN=/etc/ssh/sshd_config.d/00-fe2gen-hardening.conf   # first value wins: 00- beats 50-cloud-init.conf
  if [ ! -x /usr/sbin/sshd ]; then
    note "openssh-server is not installed (Tailscale SSH only?): nothing to do."
  elif [ "$SSH_TESTED" = 0 ]; then
    echo "   WARNING: skipped. First check, in a second terminal, that you log in with a key and NO password:" >&2
    echo "     ssh -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no ${SUDO_USER:-$USER}@<tailscale-name>" >&2
    echo "   then rerun with --ssh-keys-tested, keeping that session open." >&2
  else
    keys="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/.ssh/authorized_keys"
    if [ ! -s "$keys" ]; then
      note "WARNING: $keys is empty or missing; after this step only Tailscale SSH (or the console) can log in."
    fi
    CONTENT='# Fe2Gen hardening (backend/ops/harden_host.sh). Rollback: delete this file, then sshd -t && systemctl reload ssh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes'
    if put_file "$SSHD_DROPIN" "$CONTENT"; then
      if [ "$DRY" = 0 ] && ! /usr/sbin/sshd -t; then
        echo "   sshd -t failed: removing $SSHD_DROPIN, sshd left as it was." >&2
        rm -f "$SSHD_DROPIN"; exit 1
      fi
      note "+ /usr/sbin/sshd -t   (configuration valid)"
      if systemctl is-active --quiet ssh.service 2>/dev/null; then run systemctl reload ssh.service
      elif systemctl is-active --quiet sshd.service 2>/dev/null; then run systemctl reload sshd.service
      else note "sshd not running now (socket activation): the next connection reads the new file."; fi
    fi
    [ "$DRY" = 0 ] && /usr/sbin/sshd -T 2>/dev/null | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) ' | sed 's/^/   effective: /'
    note "Now open a NEW terminal and log in again before closing the old one."
    note "Rollback: sudo rm $SSHD_DROPIN && sudo sshd -t && sudo systemctl reload ssh"
  fi
fi

# ---------------------------------------------------------------- 3. unattended-upgrades
if [ "$SKIP_UPGRADES" = 0 ]; then
  step "3. unattended-upgrades: daily security updates, no automatic reboot, kernel/NVIDIA held for manual updates"
  ensure_pkg unattended-upgrades
  put_file /etc/apt/apt.conf.d/20auto-upgrades 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' || true
  # Kernel and NVIDIA stay manual: an unattended NVIDIA library update breaks CUDA until reboot ("driver/library
  # version mismatch"), and a kernel without its matching NVIDIA module took the web down for 6 days in 10/2026.
  # Update them by hand in a maintenance window: sudo apt full-upgrade; reboot; nvidia-smi; bash gpu.sh status.
  put_file /etc/apt/apt.conf.d/52fe2gen-unattended '// Fe2Gen (backend/ops/harden_host.sh): kernel and NVIDIA packages are upgraded by hand.
Unattended-Upgrade::Package-Blacklist { "linux-"; "nvidia-"; "libnvidia-"; };
Unattended-Upgrade::Automatic-Reboot "false";' || true
  run systemctl enable --now unattended-upgrades.service
  note "Check: sudo unattended-upgrade --dry-run --debug 2>&1 | tail -20"
  note "Rollback: sudo rm /etc/apt/apt.conf.d/52fe2gen-unattended; restore 20auto-upgrades from its .bak-fe2gen-* copy"
fi

# ---------------------------------------------------------------- 4. fail2ban
if [ "$SKIP_F2B" = 0 ]; then
  step "4. fail2ban: sshd jail"
  if [ ! -x /usr/sbin/sshd ]; then
    note "openssh-server is not installed: no sshd jail needed."
  else
    ensure_pkg fail2ban
    if put_file /etc/fail2ban/jail.d/fe2gen-sshd.local '# Fe2Gen (backend/ops/harden_host.sh)
[sshd]
enabled  = true
backend  = systemd
port     = ssh
maxretry = 5
findtime = 10m
bantime  = 1h
ignoreip = 127.0.0.1/8 ::1'; then
      run systemctl enable fail2ban.service
      run systemctl restart fail2ban.service
    else
      run systemctl enable --now fail2ban.service
    fi
    [ "$DRY" = 0 ] && { sleep 2; fail2ban-client status sshd 2>&1 | sed 's/^/   /' || true; }
    note "Unban an address: sudo fail2ban-client set sshd unbanip <IP>"
    note "Rollback: sudo rm /etc/fail2ban/jail.d/fe2gen-sshd.local && sudo systemctl restart fail2ban"
  fi
fi

step "Done$([ "$DRY" = 1 ] && echo ' (dry run, nothing changed)')."
note "Verify from another device: the web through Funnel still answers, and SSH through Tailscale still works."
