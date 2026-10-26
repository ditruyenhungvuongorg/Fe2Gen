# Chạy web dưới user riêng `fe2gen` (B19)

Hiện `prenatal-web` chạy dưới user đăng nhập chính (có sudo, có khóa SSH, có dữ liệu train). Một lỗi trong dịch vụ
web sẽ có toàn quyền của user đó. Sau khi chuyển, dịch vụ chạy dưới `fe2gen`: không mật khẩu, không sudo, chỉ thêm
nhóm `video` và `render` để dùng GPU, chỉ ghi được thư mục `.deployment` và `cache` của chính nó.

Có hai cách, làm phần chung (bước 1–4) trước:
- **A. user unit dưới `fe2gen` + linger**: giống hiện nay, chỉ đổi user. `deploy_ubuntu.sh` dùng nguyên được.
- **B. system unit** (`backend/ops/prenatal-web.system.service.example`): `DeviceAllow`, `IPAddressDeny`,
  `CapabilityBoundingSet` mới có hiệu lực (ở user unit systemd bỏ qua chúng). Không cần linger.

Chọn giờ vắng (dịch vụ dừng vài phút, mọi phiên bị đăng xuất). Báo phiên Model 1 trước khi làm.
Mọi lệnh chạy trong phiên SSH của user chính (gọi là `$MAIN`), có `sudo`.

## 0. Sao lưu
```bash
MAIN=$USER
bash ~/prenatal_pubcasefinder/releases/<bản-đang-chạy>/backend/ops/backup_deployment.sh
STAMP=$(date +%Y%m%d-%H%M%S); echo "Mã sao lưu: $STAMP"
cp -p ~/.config/systemd/user/prenatal-web.service ~/prenatal_pubcasefinder/.deployment/prenatal-web.service.backup-$STAMP
systemctl --user cat prenatal-web | grep -E 'WorkingDirectory|ExecStart'   # ghi lại REL = thư mục release đang chạy
grep -E '^MODEL1_' ~/prenatal_pubcasefinder/.deployment/web.env              # các đường dẫn model (không có bí mật)
```

## 1. Tạo user
```bash
sudo adduser --disabled-password --gecos "Fe2Gen web" fe2gen      # không mật khẩu: không đăng nhập bằng mật khẩu được
sudo usermod -aG video,render fe2gen
id fe2gen                                                          # không có nhóm sudo/adm/lxd/docker
sudo -l -U fe2gen                                                  # "is not allowed to run sudo"
ls -l /dev/nvidia*                                                 # thường crw-rw-rw-; nếu là crw-rw---- root video thì cần nhóm video
```

## 2. Chép code, venv, trạng thái
```bash
REL=web-2026XXXX                       # tên thư mục release đang chạy (bước 0)
sudo install -d -o fe2gen -g fe2gen -m 700 /home/fe2gen/prenatal_pubcasefinder /home/fe2gen/prenatal_pubcasefinder/.deployment \
  /home/fe2gen/prenatal_pubcasefinder/releases /home/fe2gen/venvs
sudo cp -a ~/prenatal_pubcasefinder/releases/$REL /home/fe2gen/prenatal_pubcasefinder/releases/   # gồm backend/data
sudo cp -a ~/venvs/nckh-model1-span-v1 /home/fe2gen/venvs/
readlink -f ~/venvs/nckh-model1-span-v1/bin/python     # phải là /usr/bin/python3.x; nếu nằm trong /home/$MAIN (pyenv, uv)
                                                       # thì fe2gen không đọc được: tạo lại venv dưới fe2gen
cd ~/prenatal_pubcasefinder/.deployment
sudo cp -p users.json web.env login_failures.json model1_state.json /home/fe2gen/prenatal_pubcasefinder/.deployment/ 2>/dev/null
sudo cp -p audit.jsonl* access.log* /home/fe2gen/prenatal_pubcasefinder/.deployment/ 2>/dev/null
sudo chown -R fe2gen:fe2gen /home/fe2gen/prenatal_pubcasefinder /home/fe2gen/venvs
sudo chmod 600 /home/fe2gen/prenatal_pubcasefinder/.deployment/*
```
Không chép `gpu_pause`.

## 3. Model 1: cho `fe2gen` đọc, không chép
Bundle Model 1 và base model (khoảng 10 GB) giữ nguyên chỗ cũ; `manifest.json` của bundle chứa đường dẫn tuyệt đối
(`base_model_dir`), nên đổi chỗ sẽ phải sửa bundle. Cấp quyền chỉ đọc bằng ACL cho đúng các thư mục đó:
```bash
sudo apt install -y acl
BUNDLE=$(sed -n 's/^MODEL1_V6_BUNDLE=//p' ~/prenatal_pubcasefinder/.deployment/web.env)
BASE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['base_model_dir'])" "$BUNDLE/manifest.json")
for d in "$BUNDLE" "$BASE"; do
  sudo setfacl -R -m u:fe2gen:rX "$d"
  p="$d"; while [ "$p" != / ]; do p=$(dirname "$p"); sudo setfacl -m u:fe2gen:x "$p"; done   # chỉ "đi qua", không liệt kê
done
sudo -u fe2gen test -r "$BUNDLE/manifest.json" && echo "fe2gen đọc được bundle"
```
Nếu `web.env` dùng `MODEL1_ADAPTER`/`MODEL1_WORKER_DIR` (bản v3.8) thì làm tương tự cho hai thư mục đó, và cho
`/home/$MAIN/.cache/huggingface` (base model `unsloth/Qwen3.5-4B` lấy từ cache HF). Base model nằm trong cache HF
của `$MAIN` thì `x` trên `/home/$MAIN` là đủ, không lộ các file khác (không có `r`).
Kiểm `web.env` của `fe2gen` còn đường dẫn nào trỏ vào `/home/$MAIN` mà chưa cấp quyền:
```bash
sudo grep -n "/home/$MAIN" /home/fe2gen/prenatal_pubcasefinder/.deployment/web.env | cut -d= -f1
```

## 4. Hàm tiện dụng chạy lệnh dưới `fe2gen`
```bash
sudo loginctl enable-linger fe2gen                       # chỉ cần cho cách A
loginctl show-user fe2gen -p Linger                      # Linger=yes
FUID=$(id -u fe2gen)
asweb() { sudo -u fe2gen -H env XDG_RUNTIME_DIR=/run/user/$FUID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$FUID/bus "$@"; }
asweb systemctl --user status --no-pager | head -3       # user manager của fe2gen đang chạy
```

## 5A. Chuyển dịch vụ (user unit)
```bash
systemctl --user disable --now prenatal-web              # dừng bản dưới $MAIN (giữ unit để quay lui); cổng 8000 trống
asweb bash /home/fe2gen/prenatal_pubcasefinder/releases/$REL/backend/deploy_ubuntu.sh
asweb systemd-analyze security --user prenatal-web | tail -1
```

## 5B. Chuyển dịch vụ (system unit), thay cho 5A
```bash
sudo -u fe2gen ln -sfn /home/fe2gen/prenatal_pubcasefinder/releases/$REL /home/fe2gen/prenatal_pubcasefinder/current
sudo -u fe2gen mkdir -p /home/fe2gen/prenatal_pubcasefinder/cache/{triton,torchinductor,unsloth_compiled_cache,nv}
sudo cp /home/fe2gen/prenatal_pubcasefinder/current/backend/ops/prenatal-web.system.service.example /etc/systemd/system/prenatal-web.service
sudo nano /etc/systemd/system/prenatal-web.service       # sửa MemoryMax (75% RAM) và DeviceAllow theo `ls /dev/nvidia*`
ls /dev/nvidia-uvm || sudo nvidia-modprobe -u -c=0       # nút uvm phải có trước khi dịch vụ chạy
systemctl --user disable --now prenatal-web
sudo systemctl daemon-reload && sudo systemctl enable --now prenatal-web
sudo systemd-analyze security prenatal-web | tail -1
sudo loginctl disable-linger fe2gen                      # cách B không cần
```
Đổi release sau này: chép release mới, đổi symlink `current`, `sudo systemctl restart prenatal-web`.

## 6. Kiểm tra
```bash
curl -s http://127.0.0.1:8000/api/status                 # auth_mode: accounts
pgrep -a -u fe2gen -f serve_web.py                 # có dòng = đang chạy dưới fe2gen
tailscale funnel status                                  # vẫn trỏ 127.0.0.1:8000, không cần đổi
```
Đăng nhập web bằng tài khoản bác sĩ: model chuyển *sẵn sàng*; `nvidia-smi` thấy tiến trình con của `fe2gen`.
Không được thì xem log:
- cách A: `asweb journalctl --user -u prenatal-web -n 80 --no-pager`
- cách B: `sudo journalctl -u prenatal-web -n 80 --no-pager`

## 7. Việc thường ngày sau khi chuyển
- Quản lý tài khoản: `asweb /home/fe2gen/venvs/nckh-model1-span-v1/bin/python manage_users.py list` (chạy trong thư mục backend của `fe2gen`).
- GPU cho train: `asweb bash /home/fe2gen/prenatal_pubcasefinder/releases/$REL/backend/gpu.sh pause` (hoặc thẻ Quản trị → GPU).
  `$MAIN` không còn ghi được cờ tạm dừng trong `.deployment` của `fe2gen`.
- Sao lưu: `asweb bash .../backend/ops/backup_deployment.sh --setup`; tắt timer cũ: `systemctl --user disable --now fe2gen-backup.timer`.
- Sau khoảng một tuần chạy ổn: `shred -u` các `users.json`, `web.env`, `login_failures.json` còn trong
  `/home/$MAIN/prenatal_pubcasefinder/.deployment` (chúng còn hash mật khẩu và cấu hình cũ).

## Quay lui
Tài khoản, mật khẩu và nhật ký thay đổi từ lúc chuyển nằm bên `fe2gen`; chép về trước khi bật lại bản cũ.
```bash
asweb systemctl --user disable --now prenatal-web         # cách A
sudo systemctl disable --now prenatal-web                 # cách B
sudo cp -p /home/fe2gen/prenatal_pubcasefinder/.deployment/{users.json,login_failures.json} ~/prenatal_pubcasefinder/.deployment/ 2>/dev/null
sudo cp -p /home/fe2gen/prenatal_pubcasefinder/.deployment/audit.jsonl* ~/prenatal_pubcasefinder/.deployment/
sudo chown $MAIN: ~/prenatal_pubcasefinder/.deployment/*; chmod 600 ~/prenatal_pubcasefinder/.deployment/*
cp -p ~/prenatal_pubcasefinder/.deployment/prenatal-web.service.backup-$STAMP ~/.config/systemd/user/prenatal-web.service
systemctl --user daemon-reload && systemctl --user enable --now prenatal-web
curl -s http://127.0.0.1:8000/api/status
```
User `fe2gen` và các ACL có thể để nguyên; gỡ hẳn: `sudo setfacl -R -x u:fe2gen <thư mục>`, `sudo deluser --remove-home fe2gen`
(xóa vĩnh viễn thư mục `/home/fe2gen`, chỉ làm khi đã chép lại hết).
