# Bỏ (hoặc xoay) mã truy cập chung `WEB_ACCESS_TOKEN`

Từ khi `users.json` có tài khoản, máy chủ không còn nhận mã chung, nhưng mã vẫn nằm trong `web.env`. Bản cũ của
`auth.py` sẽ quay về chế độ mã chung nếu `users.json` đọc lỗi, nên mã này phải bỏ đi. Mã nào từng hiện ra ngoài
máy chủ (terminal, tài liệu, chat) thì coi như đã lộ.

Chạy trong terminal của máy Ubuntu, dưới user đang chạy dịch vụ. Không gõ giá trị mã vào dòng lệnh `ssh host '...'`,
vì tailscaled ghi nguyên dòng lệnh SSH vào journal. Các lệnh dưới đây không chứa giá trị mã, nên cả journal lẫn
lịch sử shell không giữ mã.

## Điều kiện trước
```bash
cd ~/prenatal_pubcasefinder/releases/<bản-đang-chạy>/backend     # thư mục WorkingDirectory của unit
python3 manage_users.py list            # phải có ít nhất 1 admin đang hoạt động
bash ops/backup_deployment.sh           # sao lưu mã hóa (đã chạy --setup trước đó)
```
Đăng nhập thử trên web bằng tài khoản admin đó. Nếu đăng nhập không được, dừng lại ở đây.

## Bỏ mã chung
```bash
ENV=~/prenatal_pubcasefinder/.deployment/web.env
STAMP=$(date +%Y%m%d-%H%M%S); echo "Mã sao lưu: $STAMP"
cp -p "$ENV" "$ENV.before-token-removal-$STAMP"
grep -c '^WEB_ACCESS_TOKEN=' "$ENV"            # 1 = còn mã (lệnh không in giá trị)
sed -i '/^WEB_ACCESS_TOKEN=/d' "$ENV"
grep -q '^WEB_REQUIRE_ACCOUNTS=' "$ENV" || echo 'WEB_REQUIRE_ACCOUNTS=1' >> "$ENV"
grep -q '^WEB_HSTS=' "$ENV" || echo 'WEB_HSTS=1' >> "$ENV"
chmod 600 "$ENV"
grep -c '^WEB_ACCESS_TOKEN=' "$ENV"            # 0
systemctl --user restart prenatal-web          # các phiên đang mở bị đăng xuất
```

## Kiểm tra
```bash
PORT=$(sed -n 's/^PORT=//p' "$ENV"); PORT=${PORT:-8000}
curl -s "http://127.0.0.1:$PORT/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("auth_mode"))'
```
- `accounts`: đúng.
- `locked`: dịch vụ không đọc được `users.json` (sai đường dẫn, quyền, JSON hỏng). API trả 503 cho mọi người, không mở cửa. Xem `journalctl --user -u prenatal-web -n 50 --no-pager`.
- `token` hoặc `none`: bản đang chạy chưa có `WEB_REQUIRE_ACCOUNTS` và không thấy tài khoản. Quay lui ngay (mục dưới), rồi kiểm tra `WEB_USERS_FILE`.

Mã cũ phải bị từ chối (đọc mã từ bản sao lưu vào biến, không in ra):
```bash
OLD=$(sed -n 's/^WEB_ACCESS_TOKEN=//p' "$ENV.before-token-removal-$STAMP")
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $OLD" "http://127.0.0.1:$PORT/api/session"   # 401
unset OLD
```
Đăng nhập lại trên web bằng tài khoản admin và một tài khoản bác sĩ.

Khi mọi thứ chạy ổn (sau một ngày làm việc), xóa bản sao còn chứa mã cũ:
```bash
shred -u "$ENV.before-token-removal-$STAMP"
```
Các bản sao lưu mã hóa cũ trong `~/prenatal_pubcasefinder/backups/` vẫn chứa mã cũ, nhưng mã đó đã vô hiệu.

## Quay lui
```bash
cp -p "$ENV.before-token-removal-$STAMP" "$ENV"
systemctl --user restart prenatal-web
curl -s "http://127.0.0.1:$PORT/api/status"
```

## Xoay mã thay vì bỏ (chỉ khi máy chủ vẫn chạy chế độ mã chung, chưa có tài khoản)
```bash
cp -p "$ENV" "$ENV.before-token-rotation-$(date +%Y%m%d-%H%M%S)"
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
sed -i "s|^WEB_ACCESS_TOKEN=.*|WEB_ACCESS_TOKEN=$NEW|" "$ENV"; unset NEW
systemctl --user restart prenatal-web
```
Gửi mã mới cho người dùng qua kênh riêng (gặp trực tiếp hoặc trình quản lý mật khẩu), không qua chat nhóm hay email.
Cách tốt hơn: tạo tài khoản admin (`python3 manage_users.py add <tên> --role admin`) rồi làm mục "Bỏ mã chung".
