#!/usr/bin/env bash
# Install the web service as the current user; never modifies training runs.
#   bash deploy_ubuntu.sh                           unit with full hardening (default)
#   PRENATAL_HARDENING=basic bash deploy_ubuntu.sh  only the directives that need no mount namespace
#   PRENATAL_HARDENING=off bash deploy_ubuntu.sh    the unit as it was before the hardening (26/10/2026)
#   PRENATAL_MEMORY_MAX=20G                         memory ceiling of the service (default: 75% of RAM)
#   PRENATAL_NO_RESTART=1                           write the unit only; no restart, no health check
# An existing web.env is never rewritten. The previous unit is kept in .deployment/prenatal-web.service.prev.
# The script restarts the service (open sessions end) and, if /api/status does not answer, falls back
# full -> basic -> previous unit. Which directives a --user unit honours and which need a system service:
# header of backend/ops/prenatal-web.system.service.example.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
DEPLOY_HOME="$HOME/prenatal_pubcasefinder"
STATE_DIR="$DEPLOY_HOME/.deployment"
CACHE_DIR="$DEPLOY_HOME/cache"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/prenatal-web.service"
PREV_UNIT="$STATE_DIR/prenatal-web.service.prev"
HARDENING="${PRENATAL_HARDENING:-full}"
case "$HARDENING" in full|basic|off) ;; *) echo "PRENATAL_HARDENING must be full, basic or off" >&2; exit 2 ;; esac
mkdir -p "$STATE_DIR" "$UNIT_DIR" "$CACHE_DIR/triton" "$CACHE_DIR/torchinductor" "$CACHE_DIR/unsloth_compiled_cache" "$CACHE_DIR/nv"
chmod 700 "$STATE_DIR" "$CACHE_DIR"
ENV_FILE="$STATE_DIR/web.env"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  # No shared access code: with WEB_REQUIRE_ACCOUNTS=1 the API stays locked (503) until
  # users.json lists an account (python manage_users.py add <admin> --role admin).
  printf '%s\n' \
    "MODEL1_ADAPTER=$HOME/Downloads/model1_extract_ubuntu_v3_8/runs/v38_20260918T175546Z_293892/adapter" \
    "MODEL1_WORKER_DIR=$HOME/Downloads/model1_extract_ubuntu_v3_8" \
    'MODEL1_PRELOAD=0' 'MODEL1_IDLE_MINUTES=60' 'MODEL1_MIN_FREE_MIB=13000' 'CUDA_VISIBLE_DEVICES=0' 'PORT=8000' \
    'WEB_REQUIRE_ACCOUNTS=1' \
    'WEB_HSTS=1' \
    'WEB_ALLOWED_ORIGINS=https://ditruyenhungvuongorg.github.io' \
    "MODEL1_STATE_FILE=$STATE_DIR/model1_state.json" \
    'PYTHONUNBUFFERED=1' \
    '# Security settings at their defaults; uncomment to change (backend/ops/TRIEN_KHAI_BAO_MAT.md).' \
    '# WEB_ADMIN_2FA_REQUIRED=1' \
    '# WEB_IDLE_MINUTES=60' \
    '# WEB_SESSION_HOURS=12' \
    '# WEB_TEMP_PASSWORD_HOURS=72' \
    '# WEB_TRUST_PROXY=0' \
    '# WEB_QUEUE_MAX=5' \
    '# WEB_MAX_CONNECTIONS=64' \
    '# WEB_RATE_HPO_SEARCH=60' \
    '# WEB_RATE_CORRECTIONS=20' \
    '# WEB_RATE_BUG_REPORT=5' \
    '# WEB_RATE_DISEASE_SEARCH=60' \
    '# WEB_RATE_DISEASE_FEEDBACK=20' \
    '# WEB_MAX_UPLOAD_BYTES=8000000' \
    "# WEB_CORRECTIONS_FILE=$STATE_DIR/corrections.json" \
    "# WEB_BUG_REPORTS_DIR=$STATE_DIR/bug_reports" \
    "# WEB_DISEASE_FEEDBACK_FILE=$STATE_DIR/disease_feedback.json" \
    '# WEB_DISEASE_FEEDBACK_PER_USER=50' \
    '# WEB_DISEASE_FEEDBACK_MAX_MB=50' \
    '# MODEL1_REQUIRE_HASH=1' > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
if ! grep -q '^WEB_REQUIRE_ACCOUNTS=' "$ENV_FILE"; then
  echo "NOTE: $ENV_FILE has no WEB_REQUIRE_ACCOUNTS; the service now defaults to 1 (fail closed)."
fi
if ! grep -q '^WEB_HSTS=' "$ENV_FILE"; then
  echo "NOTE: $ENV_FILE has no WEB_HSTS; add WEB_HSTS=1 (HTTPS through Tailscale Funnel) to make it explicit."
fi
if grep -q '^WEB_ACCESS_TOKEN=' "$ENV_FILE"; then
  echo "NOTE: $ENV_FILE still has WEB_ACCESS_TOKEN; once accounts exist, remove it (backend/ops/rotate_token.md)."
fi

# MemoryMax: the web process needs about 0.4 GB, the Model 1 child a few GB of RAM while it loads (the weights
# go to the GPU). 75% of RAM keeps sshd/tailscaled alive if something runs away without starving a model load.
# Tune it after a real load: systemctl --user show prenatal-web -p MemoryPeak (or MemoryCurrent while loading),
# then set PRENATAL_MEMORY_MAX to about 1.5x that peak, never below 6G. Too low -> the kernel kills the model
# child (journal: oom-kill) and Model 1 reports failed; search and matching keep working.
MEMORY_MAX="${PRENATAL_MEMORY_MAX:-}"
if [ -z "$MEMORY_MAX" ]; then
  mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  if [ "${mem_kib:-0}" -gt 0 ]; then MEMORY_MAX="$(( mem_kib * 75 / 100 / 1024 ))M"; else MEMORY_MAX=infinity; fi
fi

# NVIDIA device nodes for DeviceAllow (a /dev path there takes no glob): every /dev/nvidiaN present now plus the
# fixed nodes. /dev/nvidia-uvm* appear on the first CUDA use; systemd skips a missing node with a warning.
DEVICES=()
for dev in /dev/nvidia[0-9]*; do [ -c "$dev" ] && DEVICES+=("$dev"); done
[ "${#DEVICES[@]}" -gt 0 ] || DEVICES=(/dev/nvidia0)
DEVICES+=(/dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset)

write_unit() {  # $1 = full | basic | off
  local mode="$1" dev
  cat <<EOF
[Unit]
Description=Prenatal phenotype web service
After=network-online.target
[Service]
WorkingDirectory=$ROOT
EOF
  if [ "$mode" = full ]; then
    # Code and home are read-only in "full": writable caches for the Model 1 child (unsloth compiles into the
    # working directory by default; triton, inductor and the CUDA JIT cache write under ~ or /tmp).
    # Values in web.env (EnvironmentFile) take precedence over these.
    cat <<EOF
Environment=UNSLOTH_COMPILE_LOCATION=$CACHE_DIR/unsloth_compiled_cache
Environment=TRITON_CACHE_DIR=$CACHE_DIR/triton
Environment=TORCHINDUCTOR_CACHE_DIR=$CACHE_DIR/torchinductor
Environment=CUDA_CACHE_PATH=$CACHE_DIR/nv
Environment=HF_HUB_OFFLINE=1
Environment=HF_HUB_DISABLE_TELEMETRY=1
EOF
  fi
  cat <<EOF
EnvironmentFile=$ENV_FILE
ExecStart=$HOME/venvs/nckh-model1-span-v1/bin/python $ROOT/serve_web.py
Restart=on-failure
RestartSec=15
TimeoutStopSec=60
UMask=0077
EOF
  if [ "$mode" != off ]; then
    # B18, part 1: seccomp, no_new_privs, rlimits. These work in a --user unit (no namespace needed).
    # SystemCallErrorNumber=EPERM: a filtered call fails with EPERM instead of killing CUDA/torch with SIGSYS.
    # MemoryMax needs the memory controller delegated to user@.service (default on Ubuntu 24.04).
    cat <<EOF
NoNewPrivileges=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictNamespaces=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
LimitNOFILE=4096
MemoryMax=$MEMORY_MAX
EOF
  fi
  if [ "$mode" = full ]; then
    # B18, part 2: mount namespace. In a --user unit systemd implies PrivateUsers=yes for these, which needs
    # unprivileged user namespaces; Ubuntu 24.04 (kernel.apparmor_restrict_unprivileged_userns=1) may refuse
    # them -> status 226/NAMESPACE, and this script falls back to "basic".
    # Not used: MemoryDenyWriteExecute (CUDA/triton JIT), PrivateDevices (would hide /dev/nvidia*).
    # DevicePolicy/DeviceAllow need a cgroup BPF program that a user manager cannot attach: systemd ignores them
    # here; they take effect in the system unit (backend/ops/prenatal-web.system.service.example).
    cat <<EOF
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/prenatal_pubcasefinder/.deployment %h/prenatal_pubcasefinder/cache
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
DevicePolicy=closed
EOF
    for dev in "${DEVICES[@]}"; do echo "DeviceAllow=$dev rw"; done
  fi
  cat <<EOF
[Install]
WantedBy=default.target
EOF
}

PORT_VALUE="$(sed -n 's/^PORT=//p' "$ENV_FILE" | tail -n 1)"
PORT_VALUE="${PORT_VALUE:-8000}"
healthy() {  # /api/status answers within ~3 minutes (HPO data loading; longer with MODEL1_PRELOAD=1)
  local i state sub
  for i in $(seq 1 60); do
    if curl -fsS -m 5 "http://127.0.0.1:$PORT_VALUE/api/status" >/dev/null 2>&1; then return 0; fi
    state="$(systemctl --user show -p ActiveState --value prenatal-web.service 2>/dev/null || true)"
    sub="$(systemctl --user show -p SubState --value prenatal-web.service 2>/dev/null || true)"
    if [ "$state" = failed ] || [ "$sub" = auto-restart ]; then return 1; fi
    sleep 3
  done
  return 1
}
apply_unit() {  # $1 = mode: write the unit, reload, and unless PRENATAL_NO_RESTART=1 restart and check
  write_unit "$1" > "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable prenatal-web.service >/dev/null
  if [ "${PRENATAL_NO_RESTART:-0}" = 1 ]; then return 0; fi
  systemctl --user reset-failed prenatal-web.service 2>/dev/null || true
  systemctl --user restart prenatal-web.service
  healthy
}

if [ -f "$UNIT" ]; then cp -p "$UNIT" "$PREV_UNIT"; fi
APPLIED="$HARDENING"
if ! apply_unit "$HARDENING"; then
  echo "WARNING: prenatal-web did not answer with hardening=$HARDENING. Last journal lines:" >&2
  journalctl --user -u prenatal-web -n 20 --no-pager >&2 || true
  if [ "$HARDENING" = full ] && apply_unit basic; then
    APPLIED=basic
    echo "Fell back to PRENATAL_HARDENING=basic (no mount namespace); the cause is in the journal above." >&2
  elif [ -f "$PREV_UNIT" ]; then
    cp -p "$PREV_UNIT" "$UNIT"
    systemctl --user daemon-reload
    systemctl --user restart prenatal-web.service || true
    echo "Restored the previous unit ($PREV_UNIT). Not only the hardening is at fault; check the journal." >&2
    exit 1
  else
    echo "Service not healthy and no previous unit to restore. Try: PRENATAL_HARDENING=off bash $0" >&2
    exit 1
  fi
fi
echo "Installed prenatal-web user service (hardening=$APPLIED, MemoryMax=$MEMORY_MAX). Check: systemctl --user status prenatal-web"
if [ "${PRENATAL_NO_RESTART:-0}" = 1 ]; then echo 'Unit written, not restarted: systemctl --user restart prenatal-web'; fi
echo "Previous unit: $PREV_UNIT (rollback: cp -p it to $UNIT; systemctl --user daemon-reload; systemctl --user restart prenatal-web)"
echo 'For boot without login, an administrator must enable linger for this user.'
echo 'First admin: python manage_users.py add <username> --name "<display name>" --role admin; other accounts on the admin page'
echo 'GPU for training/local AI: bash gpu.sh pause | resume | status'
echo 'Model 1 runs in the sandbox only after a doctor logs in: log in once, check the model is ready, then'
echo '  journalctl --user -u prenatal-web -n 80 --no-pager | grep -Ei "read-only|permission|EPERM|namespace"'
echo '  (Model 1 fails but search works -> rerun with PRENATAL_HARDENING=basic and report the journal lines)'
echo 'Exposure score of the sandbox (lower is better):'
echo '  systemd-analyze security --user prenatal-web'
