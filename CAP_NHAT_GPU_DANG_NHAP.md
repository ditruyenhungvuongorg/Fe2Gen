# Cập nhật 17/10/2026: GPU theo nhu cầu và tài khoản bác sĩ

## Thay đổi
- **Model 1 không còn chiếm GPU thường trực.** Tiến trình web không nạp torch. Model 1 (bundle `model1_v7_prod`, khoảng 12,4 GB VRAM) chạy trong một tiến trình con:
  - khởi động khi bác sĩ **đăng nhập**, mở tab **Nhập đoạn văn** hoặc bấm **Tách dấu hiệu**;
  - tự tắt sau **60 phút** không có bác sĩ thao tác, hoặc ngay khi bác sĩ cuối cùng **đăng xuất**;
  - tắt tiến trình nên driver lấy lại **toàn bộ** VRAM, không còn sót bộ nhớ như cách xóa cache;
  - không khởi động nếu GPU còn trống dưới 13.000 MiB (train hoặc LM Studio đang dùng). Bác sĩ thấy thông báo, tra cứu và đối chiếu vẫn chạy bình thường.
- **Tài khoản riêng cho từng bác sĩ** thay cho mã truy cập dùng chung:
  - mật khẩu lưu dạng băm scrypt, trong file `~/prenatal_pubcasefinder/.deployment/users.json` (quyền 600, không lên GitHub);
  - sai mật khẩu 5 lần thì khóa tài khoản 5 phút;
  - phiên đăng nhập kéo dài tối đa 12 giờ, tự hết sau 4 giờ không dùng;
  - đăng xuất sẽ xóa ca đang mở trong tab, vì máy tính bệnh viện có thể dùng chung.
- **Góc trên trang** hiện trạng thái model (*nghỉ / đang khởi động / sẵn sàng / tạm dừng / lỗi*) và tên bác sĩ đang đăng nhập.
- **`gpu.sh`** dùng khi cần GPU cho việc khác: `pause` giải phóng và giữ tắt Model 1 (train, AI cục bộ), `resume` cho dùng lại, `status` xem tình trạng.

## Triển khai trên Ubuntu
Mỗi bước chạy trong terminal của máy Ubuntu. Bản đang chạy được giữ nguyên và sao lưu, nên có thể quay lại bất cứ lúc nào.

### 0. Sao lưu
```bash
systemctl --user cat prenatal-web | grep -E 'WorkingDirectory|ExecStart'
OLD=/duong/dan/WorkingDirectory/o/tren        # thư mục đang chạy (có serve_web.py và data/)
STAMP=$(date +%Y%m%d-%H%M); echo "Mã sao lưu: $STAMP"   # ghi lại mã này
cp -a "$OLD" "$OLD.backup-$STAMP"
cp -a ~/prenatal_pubcasefinder/.deployment/web.env ~/prenatal_pubcasefinder/.deployment/web.env.backup-$STAMP
cp -a ~/.config/systemd/user/prenatal-web.service ~/prenatal_pubcasefinder/.deployment/prenatal-web.service.backup-$STAMP
```

### 1. Lấy code mới vào thư mục riêng
```bash
NEW=~/prenatal_pubcasefinder/releases/web-20261017
git clone --branch claude/sharp-hamilton-v5c3k1 https://github.com/ditruyenhungvuongorg/Fe2Gen.git "$NEW"
cp -a "$OLD/data" "$NEW/backend/data"     # dữ liệu riêng: hp.obo, phenotype.hpoa, genes_to_disease.txt, ...
ls "$NEW/backend/data"
```

### 2. Sửa cấu hình
```bash
ENV=~/prenatal_pubcasefinder/.deployment/web.env
sed -i 's/^MODEL1_PRELOAD=1/MODEL1_PRELOAD=0/' "$ENV"
printf 'MODEL1_IDLE_MINUTES=60\nMODEL1_MIN_FREE_MIB=13000\n' >> "$ENV"
grep -E '^MODEL1_' "$ENV"
```

### 3. Chạy kiểm thử (không dùng GPU)
```bash
cd "$NEW/backend"
~/venvs/nckh-model1-span-v1/bin/python test_access.py
~/venvs/nckh-model1-span-v1/bin/python test_web.py
```

### 4. Tạo tài khoản
```bash
cd "$NEW/backend"
python3 manage_users.py add admin --name "Quản trị"
python3 manage_users.py add bs.an --name "BS. Nguyễn Văn An"   # mỗi bác sĩ một tài khoản
python3 manage_users.py list
```
Từ lúc có tài khoản đầu tiên, mã truy cập chung (`WEB_ACCESS_TOKEN`) không còn dùng được. Các lệnh `passwd`, `disable`, `enable`, `remove` có hiệu lực ngay, không cần khởi động lại dịch vụ.

### 5. Chuyển dịch vụ sang bản mới
```bash
bash "$NEW/backend/deploy_ubuntu.sh"     # ghi lại unit với thư mục mới; không đổi web.env đã có
systemctl --user restart prenatal-web
```

### 6. Kiểm tra
```bash
nvidia-smi                                  # không còn tiến trình ...nckh-model1-span-v1/bin/python
bash "$NEW/backend/gpu.sh" status           # Model 1: stopped
```
1. Mở `https://adminpc-system-product-name.taila6ff46.ts.net/` (máy chủ tự phục vụ giao diện mới) và đăng nhập. Góc trên sẽ chuyển từ *Model: đang khởi động…* sang *Model: sẵn sàng*.
2. Xem thời gian nạp của từng bước:
   ```bash
   journalctl --user -u prenatal-web -n 60 --no-pager | grep 'Model 1'
   ```
   Gửi lại các dòng này để xem có kịp trong 30 giây không.
3. Bấm **Đăng xuất**. Sau vài giây, `nvidia-smi` không còn tiến trình model.

> Trang GitHub Pages (`ditruyenhungvuongorg.github.io/Fe2Gen`) vẫn là giao diện cũ cho đến khi nhánh này được merge vào `main`. Giao diện cũ chỉ có ô mã truy cập, nên sẽ không đăng nhập được vào máy chủ đã bật tài khoản. Trong lúc thử, hãy dùng địa chỉ `ts.net` ở trên.

## Khi train hoặc chạy AI cục bộ
```bash
bash ~/prenatal_pubcasefinder/releases/web-20261017/backend/gpu.sh pause    # Model 1 rời GPU trong ~10 giây
# ... train / LM Studio ...
bash ~/prenatal_pubcasefinder/releases/web-20261017/backend/gpu.sh resume
```
Không cần dừng `prenatal-web`. Trong lúc tạm dừng, bác sĩ vẫn tra cứu HPO và đối chiếu bệnh được, chỉ phần tách đoạn văn báo "GPU đang tạm dành cho việc khác".

## Quay lại bản cũ
```bash
STAMP=<mã sao lưu ở bước 0>
systemctl --user stop prenatal-web
cp ~/prenatal_pubcasefinder/.deployment/web.env.backup-$STAMP ~/prenatal_pubcasefinder/.deployment/web.env
cp ~/prenatal_pubcasefinder/.deployment/prenatal-web.service.backup-$STAMP ~/.config/systemd/user/prenatal-web.service
systemctl --user daemon-reload && systemctl --user start prenatal-web
```
Thư mục đang chạy cũ (`$OLD`) không bị sửa. Bản sao `$OLD.backup-$STAMP` là lớp dự phòng thứ hai.

## Giới hạn hiện tại
- Code mới được kiểm thử bằng một model giả, trên máy không có GPU. Thời gian nạp thật và lượng VRAM thật cần đo ở bước 6.
- Quản lý tài khoản chỉ qua lệnh `manage_users.py`, chưa có trang quản trị trên web.
- Khởi động lại dịch vụ hoặc tải lại trang thì phải đăng nhập lại, vì phiên chỉ giữ trong bộ nhớ.
