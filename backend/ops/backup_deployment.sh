#!/usr/bin/env bash
# Encrypted backup of the web's private state (B24): accounts, audit/access logs, login locks, web.env,
# lab_cases.json (cases sent to the Lab: patient data, kept mode 600 and only ever written encrypted here),
# corrections.json (doctors' HPO corrections: clinical wording, 600), disease_feedback.json (case snapshots with the
# correct disease, Model 2 training data, 600) and the bug_reports/ folder (reports with screenshots that may show
# a case; folder 700, files 600).
#   bash backup_deployment.sh                      one backup now (what the daily timer runs)
#   bash backup_deployment.sh --setup              key/passphrase config + daily systemd user timer
#   bash backup_deployment.sh --list               backups present
#   bash backup_deployment.sh --check FILE         decrypt in memory and list the archive (needs the key)
#   bash backup_deployment.sh --restore FILE [--identity AGE_KEY_FILE]
#                                                  decrypt into a NEW folder; live files are never overwritten
# Encryption: age to the public key(s) in ~/.config/fe2gen-backup/age_recipients.txt when age is installed and
# that file lists a key; otherwise gpg --symmetric with ~/.config/fe2gen-backup/passphrase (mode 600).
# Never writes an unencrypted archive. Keeps the newest 30 backups in ~/prenatal_pubcasefinder/backups/.
# The backups sit on the same disk: copy them elsewhere now and then (they are encrypted), and keep the age
# private key / the passphrase OUTSIDE this server (password manager), or a dead disk takes both.
set -euo pipefail
umask 077
DEPLOY_HOME="${PRENATAL_HOME:-$HOME/prenatal_pubcasefinder}"
SRC="$DEPLOY_HOME/.deployment"
DEST="${PRENATAL_BACKUP_DIR:-$DEPLOY_HOME/backups}"
CONF_DIR="$HOME/.config/fe2gen-backup"
RECIPIENTS="$CONF_DIR/age_recipients.txt"
PASSFILE="$CONF_DIR/passphrase"
KEEP="${PRENATAL_BACKUP_KEEP:-30}"
PREFIX=fe2gen-deployment
INSTALLED="$HOME/.local/bin/fe2gen-backup"   # stable copy for the timer (release folders change per deploy)
SNAP="" PART=""

die() { echo "backup: $*" >&2; exit 1; }
mode_of() { stat -c %a "$1" 2>/dev/null || echo "?"; }
use_age() { command -v age >/dev/null && [ -s "$RECIPIENTS" ] && grep -q '^age1' "$RECIPIENTS"; }
use_gpg() { command -v gpg >/dev/null && [ -s "$PASSFILE" ]; }

encrypt() {  # $1 = age | gpg; stdin -> stdout
  if [ "$1" = age ]; then
    age -R "$RECIPIENTS"
  else
    gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASSFILE" \
        --symmetric --cipher-algo AES256 --output -
  fi
}

decrypt() {  # $1 = file, $2 = age identity (optional); stdout = tar.gz
  case "$1" in
    *.age) command -v age >/dev/null || die "age is not installed"
           [ -n "${2:-}" ] || die "an .age backup needs --identity <age private key file>"
           age -d -i "$2" "$1" ;;
    *.gpg) [ -s "$PASSFILE" ] || die "missing $PASSFILE (the passphrase from your password manager, mode 600)"
           gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASSFILE" --decrypt "$1" ;;
    *) die "unknown backup type: $1" ;;
  esac
}

backup_now() {
  [ -d "$SRC" ] || die "$SRC does not exist"
  mkdir -p "$DEST"; chmod 700 "$DEST"
  local stamp files=() f ext out
  if use_age; then ext=age
  elif use_gpg; then
    [ "$(mode_of "$PASSFILE")" = 600 ] || die "$PASSFILE must be mode 600 (chmod 600 $PASSFILE)"
    ext=gpg
  else die "no key: list an age public key in $RECIPIENTS or create $PASSFILE (bash $0 --setup)"; fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  shopt -s nullglob
  for f in "$SRC"/users.json "$SRC"/audit.jsonl* "$SRC"/login_failures.json "$SRC"/access.log* "$SRC"/web.env "$SRC"/lab_cases.json \
           "$SRC"/corrections.json "$SRC"/disease_feedback.json; do
    [ -f "$f" ] && files+=("$(basename "$f")")
  done
  shopt -u nullglob
  local dirs=()
  [ -d "$SRC/bug_reports" ] && dirs+=(bug_reports)   # index.json + screenshots, copied as a whole folder (cp -a)
  [ "${#files[@]}" -gt 0 ] || [ "${#dirs[@]}" -gt 0 ] || die "nothing to back up in $SRC"
  # Snapshot first (the service appends to the logs while tar would read them), in a 700 folder, removed on exit.
  out="$DEST/$PREFIX-$stamp.tar.gz"
  SNAP="$(mktemp -d "$DEST/.snapshot.XXXXXX")"   # globals: the EXIT trap runs after this function returned
  PART="$out.part"
  trap 'rm -rf -- "$SNAP" "$PART"' EXIT
  for f in "${files[@]}"; do cp -p "$SRC/$f" "$SNAP/$f"; done
  for f in "${dirs[@]}"; do cp -a "$SRC/$f" "$SNAP/$f"; done
  tar -C "$SNAP" -czf - -- "${files[@]}" "${dirs[@]}" | encrypt "$ext" > "$PART"
  mv "$out.part" "$out.$ext"
  chmod 600 "$out.$ext"
  echo "backup: $out.$ext (${files[*]} ${dirs[*]})"
  # Keep the newest $KEEP (names sort by time).
  find "$DEST" -maxdepth 1 -type f -name "$PREFIX-*.tar.gz.*" ! -name '*.part' -printf '%f\n' | sort -r \
    | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f -- "$DEST/$old"; echo "backup: removed old $old"; done
  find "$DEST" -maxdepth 1 -type f -name "$PREFIX-*.part" -mmin +60 -delete 2>/dev/null || true
  find "$DEST" -maxdepth 1 -type d -name '.snapshot.*' -mmin +60 -exec rm -rf -- {} + 2>/dev/null || true
}

setup() {
  mkdir -p "$CONF_DIR" "$DEST" "$(dirname "$INSTALLED")" "$HOME/.config/systemd/user"
  chmod 700 "$CONF_DIR" "$DEST"
  if use_age; then
    echo "setup: age with $(grep -c '^age1' "$RECIPIENTS") public key(s) from $RECIPIENTS"
  elif [ -s "$PASSFILE" ]; then
    chmod 600 "$PASSFILE"; echo "setup: gpg with the passphrase in $PASSFILE"
  else
    if command -v age >/dev/null; then
      echo "setup: age is installed. Preferred: on ANOTHER computer run  age-keygen -o fe2gen-backup.key"
      echo "       and put its public key line (age1...) in $RECIPIENTS; the private key never comes here."
    fi
    command -v gpg >/dev/null || die "neither a usable age key nor gpg: sudo apt install age (or gnupg)"
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$PASSFILE"
    chmod 600 "$PASSFILE"
    echo "setup: created $PASSFILE (gpg fallback). Copy it into your password manager NOW: cat $PASSFILE"
    echo "       Without it the backups cannot be decrypted."
  fi
  install -m 700 "$0" "$INSTALLED"
  cat > "$HOME/.config/systemd/user/fe2gen-backup.service" <<EOF
[Unit]
Description=Encrypted backup of the Fe2Gen web state
[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALLED
Nice=10
EOF
  cat > "$HOME/.config/systemd/user/fe2gen-backup.timer" <<'EOF'
[Unit]
Description=Daily encrypted backup of the Fe2Gen web state
[Timer]
OnCalendar=*-*-* 02:30
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now fe2gen-backup.timer
  systemctl --user start fe2gen-backup.service && echo "setup: first backup done" \
    || echo "setup: first backup FAILED: journalctl --user -u fe2gen-backup -n 20" >&2
  systemctl --user list-timers fe2gen-backup.timer --no-pager || true
  echo "setup: the timer needs linger to run while nobody is logged in (loginctl show-user $USER -p Linger)."
  echo "Without systemd, the same with cron (crontab -e):"
  echo "  30 2 * * * /bin/bash $INSTALLED >> $DEST/backup.log 2>&1"
  echo "After changing this script in a new release, rerun --setup to refresh $INSTALLED."
}

restore() {  # $1 = backup file, $2 = age identity
  local file="$1" target
  [ -f "$file" ] || die "no such file: $file"
  target="$DEPLOY_HOME/restore-$(date +%Y%m%d-%H%M%S)"
  mkdir -m 700 "$target"
  decrypt "$file" "${2:-}" | tar -C "$target" -xzf -
  chmod 600 "$target"/* 2>/dev/null || true
  if [ -d "$target/bug_reports" ]; then chmod 700 "$target/bug_reports"; chmod 600 "$target"/bug_reports/* 2>/dev/null || true; fi
  echo "restore: decrypted into $target:"; ls -l "$target"
  cat <<EOF

Nothing live was changed. To put the files back (stop first so the service does not write meanwhile):
  systemctl --user stop prenatal-web
  STAMP=\$(date +%Y%m%d-%H%M%S); mkdir -m 700 "$SRC/before-restore-\$STAMP"
  cp -p "$SRC"/users.json "$SRC"/login_failures.json "$SRC"/audit.jsonl* "$SRC"/web.env "$SRC"/lab_cases.json "$SRC"/corrections.json "$SRC"/disease_feedback.json "$SRC/before-restore-\$STAMP"/ 2>/dev/null
  cp -a "$SRC"/bug_reports "$SRC/before-restore-\$STAMP"/ 2>/dev/null
  cp -p "$target"/<file you need> "$SRC"/      # usually users.json; audit/access logs only if the live ones are lost
  cp -a "$target"/bug_reports "$SRC"/          # only if the live bug reports are lost (index.json + screenshots)
  chmod 600 "$SRC"/users.json "$SRC"/web.env "$SRC"/lab_cases.json "$SRC"/corrections.json "$SRC"/disease_feedback.json 2>/dev/null || true
  chmod 700 "$SRC"/bug_reports 2>/dev/null; chmod 600 "$SRC"/bug_reports/* 2>/dev/null || true
  systemctl --user start prenatal-web
Check: python3 manage_users.py list   (from the backend folder);  bash gpu.sh status
       python3 audit.py verify "$SRC/audit.jsonl"   (if this release has audit.py verify)
Undo: stop the service, copy the files back from "$SRC/before-restore-\$STAMP", start it.
Delete $target when done: it holds the decrypted accounts.
EOF
}

case "${1:-}" in
  "") backup_now ;;
  --setup) setup ;;
  --list) ls -l "$DEST"/$PREFIX-*.tar.gz.* 2>/dev/null || echo "no backups in $DEST" ;;
  --check) [ -n "${2:-}" ] || die "--check FILE [--identity KEY]"
           id_file=""; [ "${3:-}" = --identity ] && id_file="${4:?--identity needs a file}"
           decrypt "$2" "$id_file" | tar -tvzf - ;;
  --restore) [ -n "${2:-}" ] || die "--restore FILE [--identity KEY]"
             id_file=""; [ "${3:-}" = --identity ] && id_file="${4:?--identity needs a file}"
             restore "$2" "$id_file" ;;
  -h|--help) sed -n '2,15p' "$0" ;;
  *) die "unknown option $1 (see --help)" ;;
esac
