# Triển khai bản bảo mật lên máy Ubuntu

Tài liệu cho người vận hành. Mọi lệnh chạy trong terminal của máy Ubuntu, dưới user đang chạy dịch vụ
`prenatal-web` (trừ bước 8 cần `sudo`). Không agent nào tự chạy các bước này. Làm theo thứ tự; bước nào kiểm tra
không đạt thì quay lui bước đó rồi dừng lại.

Bản này thay đổi hành vi so với bản đang chạy:

- Không còn rơi về mã truy cập chung hay "không cần đăng nhập": nếu `users.json` thiếu, hỏng hoặc rỗng, mọi
  `/api/*` trả 503 (`auth_mode: locked`).
- Tài khoản quản trị phải bật xác thực 2 lớp (TOTP) ngay lần đăng nhập đầu sau khi cập nhật.
- Model 1 chỉ nạp khi `manifest.json` của bundle có bảng SHA-256 khớp từng file (`MODEL1_REQUIRE_HASH=1`).
- `/api/status` công khai chỉ còn `status` và `auth_mode`; trạng thái model xem bằng `bash gpu.sh status`.
- `deploy_ubuntu.sh` giờ tự restart dịch vụ và tự lùi khi dịch vụ không trả lời.

Trong các lệnh dưới đây:

```bash
DEP=~/prenatal_pubcasefinder/.deployment      # web.env, users.json, audit, khóa đăng nhập
OLD=~/prenatal_pubcasefinder/releases/<bản-đang-chạy>     # WorkingDirectory hiện tại là $OLD/backend
NEW=~/prenatal_pubcasefinder/releases/web-<ngày>-sec      # bản mới, thư mục riêng
PORT=$(sed -n 's/^PORT=//p' "$DEP/web.env" | tail -n 1); PORT=${PORT:-8000}
```

Xem bản đang chạy: `systemctl --user cat prenatal-web | grep WorkingDirectory`.

---

## 0. Sao lưu

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$DEP/before-sec-$STAMP"
cp -p ~/.config/systemd/user/prenatal-web.service "$DEP/web.env" "$DEP/users.json" "$DEP/before-sec-$STAMP"/
cp -p "$DEP"/audit.jsonl* "$DEP/before-sec-$STAMP"/ 2>/dev/null || true
chmod 600 "$DEP/before-sec-$STAMP"/*
echo "Mã sao lưu: $STAMP"
```

Kiểm tra: `ls -l "$DEP/before-sec-$STAMP"` có `prenatal-web.service`, `web.env`, `users.json`, mỗi file `-rw-------`.
Ghi lại `$STAMP`; các bước quay lui dùng nó.

Quay lui: không có gì để quay lui (chưa thay đổi).

## 1. Lấy bản mới vào thư mục riêng

Sau khi PR đã merge vào `main`:

```bash
git clone --depth 1 https://github.com/ditruyenhungvuongorg/Fe2Gen.git "$NEW"
git -C "$NEW" log -1 --format='%h %s'            # đúng commit merge của PR
cp -a "$OLD/backend/data" "$NEW/backend/data"    # dữ liệu HPO có giấy phép, không có trong repo
```

Kiểm tra:

```bash
ls "$NEW/backend/data/hp.obo" "$NEW/backend/data/phenotype.hpoa"
ls "$NEW/backend/ops/"           # harden_host.sh, backup_deployment.sh, TRIEN_KHAI_BAO_MAT.md ...
```

Quay lui: `rm -rf "$NEW"` (bản đang chạy chưa bị đụng tới).

## 2. Thêm SHA-256 cho bundle Model 1 (bắt buộc)

`MODEL1_REQUIRE_HASH=1` là mặc định: bundle chưa có khóa `sha256` trong `manifest.json` sẽ không nạp
("Bundle không khớp manifest"), tra cứu và đối chiếu vẫn chạy nhưng tách đoạn văn báo lỗi.

```bash
BUNDLE=$(sed -n 's/^MODEL1_V6_BUNDLE=//p' "$DEP/web.env" | tail -n 1); echo "$BUNDLE"
cp -p "$BUNDLE/manifest.json" "$DEP/before-sec-$STAMP/manifest.json.bundle"
cd "$NEW/backend"
~/venvs/nckh-model1-span-v1/bin/python -m model1_v6.build_bundle --rehash "$BUNDLE"
```

Lệnh in `{"bundle": ..., "files": N, "MODEL1_MANIFEST_SHA256": "..."}`. Lệnh chỉ đọc các file trong bundle và
ghi lại `manifest.json`; không đổi trọng số. Chạy `--rehash` khi chắc bundle trên đĩa là bản đúng (bản đang phục vụ
bác sĩ): từ đây mọi thay đổi file trong bundle đều bị phát hiện.

Tùy chọn, chặt hơn: thêm vào `web.env` dòng `MODEL1_MANIFEST_SHA256=<giá trị vừa in>` để phát hiện cả việc sửa
`manifest.json`. Mỗi lần `--rehash` lại thì phải cập nhật dòng này.

Kiểm tra:

```bash
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(len(m.get("sha256", {})), "file có hash")' "$BUNDLE/manifest.json"
```

Số file phải lớn hơn 0. Lưu ý: chỉ các file trong thư mục bundle được kiểm; base model nằm ngoài bundle
(`base_model_dir`) không được kiểm.

Quay lui: `cp -p "$DEP/before-sec-$STAMP/manifest.json.bundle" "$BUNDLE/manifest.json"`.
Cách tạm nếu chưa rehash được: thêm `MODEL1_REQUIRE_HASH=0` vào `web.env` (chỉ cảnh báo trên journal, vẫn nạp),
rồi rehash và bỏ dòng đó sớm nhất có thể.

## 3. Sửa web.env

`deploy_ubuntu.sh` không bao giờ viết lại `web.env` đã có, nên thêm biến bằng tay:

```bash
ENV="$DEP/web.env"
cp -p "$ENV" "$DEP/before-sec-$STAMP/web.env.step3"
grep -q '^WEB_REQUIRE_ACCOUNTS=' "$ENV" || echo 'WEB_REQUIRE_ACCOUNTS=1' >> "$ENV"
grep -q '^WEB_HSTS=' "$ENV" || echo 'WEB_HSTS=1' >> "$ENV"
grep -q '^MODEL1_STATE_FILE=' "$ENV" || echo "MODEL1_STATE_FILE=$DEP/model1_state.json" >> "$ENV"
chmod 600 "$ENV"
```

Giữ `WEB_ACCESS_TOKEN` tạm thời đến bước 7: nếu phải quay lui về bản cũ, bản cũ vẫn cần nó. Với bản mới và
`WEB_REQUIRE_ACCOUNTS=1`, mã chung không còn mở được API khi đã có tài khoản.

`WEB_TRUST_PROXY` để `0` (mặc định, không cần ghi). Qua Tailscale Funnel mọi kết nối tới dịch vụ đều đến từ
`127.0.0.1`, và chưa xác nhận được Funnel có gửi `X-Forwarded-For` đáng tin hay không. Hệ quả: giới hạn số lần
đăng nhập sai "theo nguồn" (`WEB_RATE_LOGIN`, 10/phút) là **chung cho mọi người** dùng Funnel, không theo từng máy.
Khóa theo tên đăng nhập vẫn riêng từng tài khoản. Chỉ đặt `WEB_TRUST_PROXY=1` khi đã đặt một proxy cục bộ (Caddy,
nginx) trước dịch vụ và proxy đó tự ghi `X-Forwarded-For`.

Kiểm tra: `grep -E '^(WEB_REQUIRE_ACCOUNTS|WEB_HSTS|MODEL1_STATE_FILE)=' "$ENV"` in đủ 3 dòng;
`stat -c %a "$ENV"` là `600`. Bảng biến môi trường đầy đủ ở cuối tài liệu.

Quay lui: `cp -p "$DEP/before-sec-$STAMP/web.env.step3" "$ENV"`.

## 4. Chạy test trên máy Ubuntu

```bash
cd "$NEW/backend"
PY=~/venvs/nckh-model1-span-v1/bin/python
$PY test_access.py && $PY test_model_integrity.py && $PY test_web.py
```

Kết thúc bằng `OK` cho cả ba (test không dùng GPU, không đọc `web.env`, không đụng `.deployment`).
`test_web.py` nạp dữ liệu HPO, mất vài phút.

Quay lui: không có gì (test chỉ ghi vào thư mục tạm). Test lỗi thì dừng, không deploy.

## 5. Chạy deploy_ubuntu.sh

```bash
cd "$NEW/backend"
bash deploy_ubuntu.sh
```

Khác bản cũ:

- Script **tự restart** dịch vụ (mọi phiên đang mở bị đăng xuất). Báo cho bác sĩ trước, chọn lúc không có ca.
- Script viết unit với hardening mức `PRENATAL_HARDENING=full` (mặc định), chờ `/api/status` trả lời tối đa khoảng
  3 phút. Không trả lời thì tự lùi: `full` → `basic` (không dùng mount namespace) → unit cũ (đã chép sang
  `$DEP/prenatal-web.service.prev`) và thoát với mã 1.
- `PRENATAL_HARDENING=basic` hoặc `off` để chọn mức thấp hơn ngay từ đầu; `PRENATAL_MEMORY_MAX=20G` để đặt trần RAM
  (mặc định 75% RAM); `PRENATAL_NO_RESTART=1` chỉ viết unit, không restart.

Kiểm tra:

```bash
systemctl --user status prenatal-web --no-pager | head -5
systemctl --user cat prenatal-web | grep -E 'WorkingDirectory|ProtectSystem|NoNewPrivileges'
curl -s "http://127.0.0.1:$PORT/api/status"         # {"status": "online", "auth_mode": "accounts"}
journalctl --user -u prenatal-web -n 30 --no-pager
```

Dòng cuối của script cho biết mức hardening thực tế (`hardening=full` hoặc `basic`). Nếu lùi về `basic`, journal
ở trên ghi nguyên nhân (thường là `226/NAMESPACE` do Ubuntu 24.04 hạn chế user namespace). `auth_mode: locked`
nghĩa là dịch vụ không đọc được `users.json`: xem journal, sửa đường dẫn/quyền, không xóa `WEB_REQUIRE_ACCOUNTS`.

Sau đó một bác sĩ (hoặc admin mở Nhập đoạn văn) đăng nhập để Model 1 nạp, rồi:

```bash
bash "$NEW/backend/gpu.sh" status                  # Model 1: ready ...
journalctl --user -u prenatal-web -n 80 --no-pager | grep -Ei 'read-only|permission|EPERM|namespace|manifest'
```

Model 1 lỗi nhưng tra cứu chạy được: chạy lại `PRENATAL_HARDENING=basic bash deploy_ubuntu.sh` và giữ dòng
journal để báo lại.

Quay lui về bản cũ hoàn toàn:

```bash
cp -p "$DEP/before-sec-$STAMP/prenatal-web.service" ~/.config/systemd/user/prenatal-web.service
systemctl --user daemon-reload && systemctl --user restart prenatal-web
curl -s "http://127.0.0.1:$PORT/api/status"
```

(`web.env` đã thêm biến ở bước 3 vẫn dùng được cho bản cũ; bản cũ bỏ qua biến lạ. `WEB_ACCESS_TOKEN` còn đó.)

## 6. Ngay sau deploy: admin bật xác thực 2 lớp

Mỗi tài khoản quản trị, ngay sau khi deploy:

1. Đăng nhập trên trang web bằng mật khẩu. Trang tự mở hộp "Bật xác thực 2 lớp"; trước khi bật, admin chỉ dùng được
   hộp này, đổi mật khẩu và đăng xuất (`WEB_ADMIN_2FA_REQUIRED=1`).
2. Bấm "Bật xác thực 2 lớp", nhập khóa (hoặc liên kết `otpauth://`) vào ứng dụng xác thực trên điện thoại
   (Google Authenticator, Microsoft Authenticator, Aegis), nhập mã 6 số để xác nhận.
3. Trang hiện **8 mã dự phòng, chỉ một lần**. Chép ra giấy hoặc trình quản lý mật khẩu cá nhân, cất riêng; không lưu
   trên máy dùng chung, không gửi qua chat.
4. Đăng xuất, đăng nhập lại: sau mật khẩu, trang hỏi mã 6 số.

Bác sĩ và Phòng Lab không bắt buộc; họ tự bật trong hộp Tài khoản nếu muốn.

Kiểm tra:

```bash
cd "$NEW/backend"; python3 manage_users.py list
grep '"event": "totp_enabled"' "$DEP/audit.jsonl" | tail -3     # một dòng cho mỗi admin vừa bật
```

Trên trang quản trị, dòng của admin có "2FA bật".

Mất điện thoại:

- Đăng nhập bằng một mã dự phòng (nút "Không có điện thoại? Dùng mã dự phòng"), rồi nhờ admin khác bấm "Đặt lại 2FA".
- Không còn admin nào đăng nhập được: trên máy Ubuntu
  `cd "$NEW/backend" && python3 manage_users.py totp-reset <tên-admin>`, rồi đăng nhập bằng mật khẩu và bật lại 2FA.

Quay lui (chỉ khi cần, tạm thời): thêm `WEB_ADMIN_2FA_REQUIRED=0` vào `web.env` và restart. Ghi lý do; bật lại sớm.

## 7. Bỏ WEB_ACCESS_TOKEN

Khi bước 5 và 6 đã ổn (đăng nhập được bằng admin có 2FA và bằng một bác sĩ):

```bash
ENV="$DEP/web.env"
cp -p "$ENV" "$DEP/before-sec-$STAMP/web.env.step7"
sed -i '/^WEB_ACCESS_TOKEN=/d' "$ENV"
grep -c '^WEB_ACCESS_TOKEN=' "$ENV"                # 0
systemctl --user restart prenatal-web
curl -s "http://127.0.0.1:$PORT/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["auth_mode"])'
```

Kết quả phải là `accounts`. Chi tiết (kiểm mã cũ bị từ chối mà không in mã, xóa bản sao chứa mã cũ):
`backend/ops/rotate_token.md`. Các file `$DEP/before-sec-$STAMP/web.env*` vẫn chứa mã cũ: sau một ngày chạy ổn,
`shred -u "$DEP/before-sec-$STAMP"/web.env*`.

Quay lui: `cp -p "$DEP/before-sec-$STAMP/web.env.step7" "$ENV" && systemctl --user restart prenatal-web`.

## 8. Hardening máy chủ (sudo)

Đọc đầu file `backend/ops/harden_host.sh` trước. Mở sẵn một phiên SSH thứ hai bằng khóa (hoặc Tailscale SSH),
giữ nó đến khi xong.

```bash
sudo bash "$NEW/backend/ops/harden_host.sh" --dry-run           # in từng bước, không đổi gì
sudo bash "$NEW/backend/ops/harden_host.sh" --ssh-keys-tested   # chạy thật
```

Script làm: ufw (chặn vào, chỉ mở trên `tailscale0`, SSH chỉ từ tailnet và `--lan-ssh` nếu có), sshd (không mật
khẩu, không root), unattended-upgrades (kernel/NVIDIA giữ cập nhật tay), fail2ban cho sshd. Mỗi bước kiểm trạng
thái trước nên chạy lại được, và tự in lệnh quay lui của bước đó.

Kiểm tra: `sudo ufw status verbose`; `sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin'`;
mở phiên SSH thứ ba bằng khóa; trang web vẫn đăng nhập được qua Funnel.

Quay lui: theo lệnh script in ở từng bước (ví dụ `sudo ufw disable`; khôi phục `sshd_config` từ bản sao script
tạo).

## 9. Sao lưu mã hóa hằng ngày

```bash
bash "$NEW/backend/ops/backup_deployment.sh" --setup     # khóa age hoặc passphrase gpg + timer user hằng ngày
bash "$NEW/backend/ops/backup_deployment.sh"             # một bản ngay
bash "$NEW/backend/ops/backup_deployment.sh" --list
```

Sao lưu gồm `users.json`, `audit.jsonl*`, `login_failures.json`, `access.log*`, `web.env` và `lab_cases.json`.
`lab_cases.json` (ca gửi sang Phòng Lab, khi chức năng đó được triển khai) chứa nội dung ca bệnh: nó thuộc phạm vi
sao lưu mã hóa này và phải giữ quyền 600 như `users.json`. Khóa riêng age / passphrase cất NGOÀI máy chủ.

Kiểm tra: `systemctl --user list-timers fe2gen-backup.timer`;
`bash "$NEW/backend/ops/backup_deployment.sh" --check <file mới nhất>` liệt kê được các file (cần khóa).

Quay lui: `systemctl --user disable --now fe2gen-backup.timer`; xóa thư mục `~/prenatal_pubcasefinder/backups`
nếu không giữ.

## 10. Kiểm tra cuối

```bash
bash "$NEW/backend/gpu.sh" status                        # cờ tạm dừng, Web: online, Model 1 từ file trạng thái
systemd-analyze security --user prenatal-web | tail -1   # điểm phơi nhiễm, càng thấp càng tốt
journalctl --user -u prenatal-web -n 50 --no-pager | grep -E '\[audit\]|Model 1|Error' | tail -20
cd "$NEW/backend" && python3 audit.py verify "$DEP/audit.jsonl"     # chuỗi băm của nhật ký
ls -l "$DEP"                                             # users.json, web.env, login_failures.json, access.log: 600
```

Lưu ý `systemd-analyze security --user` chấm cả các mục mà unit user không áp dụng được (DeviceAllow, một số
Protect*); điểm vẫn cao hơn unit hệ thống. Unit hệ thống mẫu: `backend/ops/prenatal-web.system.service.example`.

## 11. GitHub Pages

Trang `docs/` mới chỉ chạy đúng với backend mới: bước nhập mã 2 lớp, `/api/session` (trạng thái model, thời gian
tự đăng xuất), `/api/status` rút gọn. Merge PR làm GitHub Pages đăng trang mới sau vài phút. Vì vậy **merge PR và
deploy backend (bước 1–5) trong cùng một buổi**, báo bác sĩ trước; giữa hai việc, trang có thể báo lỗi đăng nhập.
Trang mới cũng khóa máy chủ API theo `docs/config.js` và CSP chỉ cho kết nối tới đúng host đó: đổi host Funnel thì
phải sửa cả `docs/config.js` và CSP trong `docs/index.html`.

Kiểm tra: mở trang GitHub Pages ở cửa sổ ẩn danh, đăng nhập bác sĩ, tìm HPO, đối chiếu một ca thử (không dùng ca thật).

Quay lui: revert commit merge trên `main` (Pages đăng lại trang cũ) cùng lúc với quay lui bước 5.

## 12. Lockfile phụ thuộc (commit riêng, sau)

```bash
bash "$NEW/backend/ops/freeze_lock.sh"                   # ghi backend/requirements.lock.txt từ venv đang chạy
```

Chép file đó về máy phát triển và commit trong một PR riêng; CI (`pip-audit`) sẽ kiểm nó. Không sửa venv đang chạy
ở bước này.

---

## Biến môi trường mới hoặc đổi nghĩa

Tên và mặc định lấy từ code (`grep getenv|env_flag|env_int|_env_float` trong `backend/` và các script).

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WEB_REQUIRE_ACCOUNTS` | `1` | `users.json` thiếu/hỏng/rỗng → mọi `/api/*` 503 (`auth_mode: locked`). `0` chỉ cho máy local: rơi về `WEB_ACCESS_TOKEN` rồi không đăng nhập. |
| `WEB_ADMIN_2FA_REQUIRED` | `1` | Admin chưa bật TOTP chỉ dùng được `/api/session`, `/api/totp/setup|enable`, `/api/change_password`, `/api/logout`. |
| `WEB_HSTS` | `1` | Gửi `Strict-Transport-Security`; `0` tắt (chạy http thuần). |
| `WEB_ACCESS_LOG` | `~/prenatal_pubcasefinder/.deployment/access.log` | Access log JSON (không query, body, token), xoay ở 20 MB, giữ 3 file. Rỗng, `0` hoặc `off` = tắt. |
| `WEB_AUDIT_FILE` | `~/prenatal_pubcasefinder/.deployment/audit.jsonl` | Nhật ký audit có chuỗi băm (`python audit.py verify`). |
| `WEB_AUDIT_STDERR` | `1` | Chép từng dòng audit ra stderr (journald giữ bản thứ hai). `0` tắt. |
| `WEB_RATE_HPO_SEARCH` | `60` | Số yêu cầu/phút mỗi phiên cho `/api/hpo_search`; vượt → 429 + `Retry-After`. `0` tắt. |
| `WEB_RATE_MATCH` | `10` | Như trên, `/api/match_diseases`. |
| `WEB_RATE_EXTRACT` | `10` | Như trên, `/api/extract_hpo`. |
| `WEB_RATE_LOGIN` | `10` | Lần đăng nhập SAI/phút theo nguồn kết nối và, riêng, theo tên đăng nhập (đăng nhập đúng không tính). |
| `WEB_RATE_ADMIN` | `60` | Yêu cầu/phút mỗi phiên cho `/api/admin/*`. |
| `WEB_QUEUE_MAX` | `5` | Số yêu cầu tối đa chờ hàng đợi GPU/CPU (1 chạy); người tiếp theo nhận 503 ngay. |
| `WEB_MAX_CONNECTIONS` | `64` | Số kết nối đồng thời; quá → 503 ngắn và đóng. |
| `WEB_HEADER_TIMEOUT` | `15` | Giây tối đa để nhận dòng yêu cầu + header (chống slowloris). |
| `WEB_TRUST_PROXY` | `0` | `1`: lấy nguồn từ mục cuối `X-Forwarded-For` (chỉ khi có proxy cục bộ tự ghi header đó). Xem bước 3. |
| `WEB_LOCK_MAX_SECONDS` | `300` | Thời gian khóa tối đa của một tài khoản sau nhiều lần sai mật khẩu. |
| `WEB_IDLE_MINUTES` | `60` | Phiên hết hạn sau từng ấy phút không yêu cầu; trang tự đăng xuất và xóa ca ở cùng ngưỡng (cảnh báo 60 giây trước). |
| `WEB_SESSION_HOURS` | `12` | Thời gian sống tối đa của một phiên kể từ khi đăng nhập. |
| `WEB_TEMP_PASSWORD_HOURS` | `72` | Mật khẩu tạm do admin cấp hết hạn sau từng ấy giờ nếu chưa đổi. |
| `WEB_LOGIN_FAILURES_FILE` | `login_failures.json` cạnh `users.json` | Khóa đăng nhập giữ qua restart (600). |
| `MODEL1_STATE_FILE` | `~/prenatal_pubcasefinder/.deployment/model1_state.json` | Trạng thái Model 1 cho `gpu.sh status` (600). Đặt rỗng để tắt. |
| `MODEL1_REQUIRE_HASH` | `1` | `1`: thiếu bảng `sha256`/file thiếu/hash lệch → không nạp. `strict`: thêm cả file lạ trong bundle. `0`: chỉ cảnh báo. |
| `MODEL1_MANIFEST_SHA256` | (rỗng) | Hash mong đợi của chính `manifest.json` (in ra bởi `--rehash`). |
| `PRENATAL_HARDENING` | `full` | Biến của `deploy_ubuntu.sh`: `full`, `basic` (không mount namespace), `off` (unit như trước 26/10/2026). |
| `PRENATAL_MEMORY_MAX` | 75% RAM | `MemoryMax` của unit (ví dụ `20G`; không dưới `6G`). |
| `PRENATAL_NO_RESTART` | `0` | `1`: `deploy_ubuntu.sh` chỉ viết unit, không restart, không kiểm tra. |

Giữ nguyên từ trước: `WEB_USERS_FILE`, `WEB_ALLOWED_ORIGINS`, `PORT`, `MODEL1_V6_BUNDLE`, `MODEL1_ADAPTER`,
`MODEL1_PRELOAD`, `MODEL1_IDLE_MINUTES`, `MODEL1_MIN_FREE_MIB`, `MODEL1_PAUSE_FILE`, `MODEL1_LOAD_WAIT_SECONDS`.
`WEB_ACCESS_TOKEN` chỉ còn ý nghĩa khi `WEB_REQUIRE_ACCOUNTS=0`; bỏ khỏi `web.env` ở bước 7.

## Giới hạn đã biết

- Bí mật TOTP lưu dạng rõ trong `users.json` (quyền 600, thư mục 700); ai đọc được file này trên máy chủ thì tạo
  được mã. Mã dự phòng chỉ lưu SHA-256.
- Chưa có mã QR trên trang: người dùng nhập khóa hoặc liên kết `otpauth://` bằng tay.
- Unit `--user` có thể phải chạy mức `basic` trên Ubuntu 24.04; muốn đủ hardening (DeviceAllow, Protect*) thì chuyển
  sang unit hệ thống với user riêng (`backend/ops/separate_user.md`).
- Qua Funnel, giới hạn đăng nhập theo nguồn là chung cho mọi người (bước 3).
