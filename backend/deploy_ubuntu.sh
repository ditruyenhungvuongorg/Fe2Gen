#!/usr/bin/env bash
# Install the web service as the current user; never modifies training runs.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
DEPLOY_HOME="$HOME/prenatal_pubcasefinder"
mkdir -p "$DEPLOY_HOME/.deployment" "$HOME/.config/systemd/user"
ENV_FILE="$DEPLOY_HOME/.deployment/web.env"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  # No shared access code: with WEB_REQUIRE_ACCOUNTS=1 the API stays locked (503) until
  # users.json lists an account (python manage_users.py add <admin> --role admin).
  printf '%s\n' \
    "MODEL1_ADAPTER=$HOME/Downloads/model1_extract_ubuntu_v3_8/runs/v38_20260918T175546Z_293892/adapter" \
    "MODEL1_WORKER_DIR=$HOME/Downloads/model1_extract_ubuntu_v3_8" \
    'MODEL1_PRELOAD=0' 'MODEL1_IDLE_MINUTES=60' 'MODEL1_MIN_FREE_MIB=13000' 'CUDA_VISIBLE_DEVICES=0' 'PORT=8000' \
    'WEB_REQUIRE_ACCOUNTS=1' \
    'WEB_ALLOWED_ORIGINS=https://ditruyenhungvuongorg.github.io' \
    'PYTHONUNBUFFERED=1' > "$ENV_FILE"
fi
if ! grep -q '^WEB_REQUIRE_ACCOUNTS=' "$ENV_FILE"; then
  echo "NOTE: $ENV_FILE has no WEB_REQUIRE_ACCOUNTS; the service now defaults to 1 (fail closed)."
fi
if grep -q '^WEB_ACCESS_TOKEN=' "$ENV_FILE"; then
  echo "NOTE: $ENV_FILE still has WEB_ACCESS_TOKEN; once accounts exist, remove it (backend/ops/rotate_token.md)."
fi
cat > "$HOME/.config/systemd/user/prenatal-web.service" <<EOF
[Unit]
Description=Prenatal phenotype web service
After=network-online.target
[Service]
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
ExecStart=$HOME/venvs/nckh-model1-span-v1/bin/python $ROOT/serve_web.py
Restart=on-failure
RestartSec=15
TimeoutStopSec=60
UMask=0077
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now prenatal-web.service
echo 'Installed prenatal-web user service. Check: systemctl --user status prenatal-web'
echo 'For boot without login, an administrator must enable linger for this user.'
echo 'First admin: python manage_users.py add <username> --name "<display name>" --role admin; other accounts on the admin page'
echo 'GPU for training/local AI: bash gpu.sh pause | resume | status'
