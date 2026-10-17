# Cập nhật 17/10/2026: GPU theo nhu cầu, tài khoản và trang quản trị

## Thay đổi
- **Model 1 không còn chiếm GPU thường trực.** Tiến trình web không nạp torch. Model 1 (bundle `model1_v7_prod`, khoảng 12,4 GB VRAM) chạy trong một tiến trình con:
  - khởi động khi bác sĩ **đăng nhập**, hoặc khi bác sĩ/quản trị mở tab **Nhập đoạn văn** hay bấm **Tách dấu hiệu**;
  - tự tắt sau **60 phút** không có bác sĩ thao tác, hoặc ngay khi bác sĩ cuối cùng **đăng xuất**;
  - tắt tiến trình nên driver lấy lại **toàn bộ** VRAM, không còn sót bộ nhớ như cách xóa cache;
  - không khởi động nếu GPU còn trống dưới 13.000 MiB (train hoặc LM Studio đang dùng). Bác sĩ thấy thông báo, tra cứu và đối chiếu vẫn chạy bình thường.
- **Tài khoản riêng, ba vai trò** thay cho mã truy cập dùng chung:
  - **Quản trị**: mọi chức năng, cộng thêm trang **Quản trị**;
  - **Bác sĩ**: tra cứu HPO, tách đoạn văn (GPU), đối chiếu bệnh, phiếu Lab;
  - **Phòng Lab**: tra cứu HPO, đối chiếu bệnh (kể cả gene WES), phiếu Lab. Không có tab Nhập đoạn văn, nên Lab đăng nhập **không làm GPU khởi động**.
- **Trang Quản trị** (nút *Quản trị* góc trên; trên điện thoại nằm trong nút tên tài khoản), gồm bốn thẻ:
  - **Tài khoản**: tạo tài khoản (tên, vai trò), khóa/mở, đổi vai, đặt lại mật khẩu, xóa. Mỗi tài khoản mới hoặc vừa đặt lại nhận **mật khẩu tạm hiện đúng một lần**; người dùng phải đặt mật khẩu mới ở lần đăng nhập đầu, Esc không bỏ qua được;
  - **Đang đăng nhập**: ai đang mở phiên, nút buộc đăng xuất;
  - **Nhật ký**: ai làm gì lúc nào, lọc theo tài khoản hoặc hành động;
  - **GPU**: trạng thái Model 1, VRAM còn trống, nút giải phóng ngay, tạm dừng hoặc cho chạy lại.
  - Không thể xóa, khóa hay hạ quyền quản trị cuối cùng, cũng không thể tự khóa hoặc tự xóa chính mình.
- **Bảo mật**:
  - mật khẩu tối thiểu 10 ký tự, chặn mật khẩu phổ biến và mật khẩu chứa tên đăng nhập; lưu dạng băm scrypt trong `~/prenatal_pubcasefinder/.deployment/users.json` (quyền 600, không lên GitHub);
  - sai 5 lần liên tiếp thì khóa tài khoản 1 phút, sau đó 2, 4… tối đa 15 phút; quá 30 lần sai trong 1 phút (trên mọi tài khoản) thì tạm ngừng mọi lần đăng nhập trong 1 phút;
  - phiên đăng nhập kéo dài tối đa 12 giờ, tự hết sau 4 giờ không dùng; đổi mật khẩu sẽ đăng xuất các thiết bị khác;
  - thêm Content-Security-Policy (trang chỉ chạy script của chính nó) và chặn nhúng trang vào khung;
  - đăng xuất sẽ xóa ca đang mở trong tab, vì máy tính bệnh viện có thể dùng chung.
- **Nhật ký kiểm toán** `~/prenatal_pubcasefinder/.deployment/audit.jsonl` (quyền 600) ghi đăng nhập (đúng/sai/bị khóa), đăng xuất, đổi mật khẩu, thao tác quản trị, điều khiển GPU, và mỗi lần tách đoạn văn hoặc đối chiếu. Mỗi dòng chỉ có người, thời gian và các con số (số ký tự, số cụm, số HPO), **không có nội dung ca**.
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

### 4. Tạo tài khoản quản trị đầu tiên
```bash
cd "$NEW/backend"
python3 manage_users.py add quan.tri --name "Quản trị" --role admin   # nhập mật khẩu mạnh (≥ 10 ký tự)
python3 manage_users.py list
```
Các tài khoản bác sĩ và Lab sau đó tạo trên trang **Quản trị** của web.
- Từ lúc có tài khoản đầu tiên, mã truy cập chung (`WEB_ACCESS_TOKEN`) không còn dùng được.
- Lệnh dự phòng khi quên mật khẩu quản trị: `python3 manage_users.py passwd quan.tri`. Các lệnh khác: `role`, `disable`, `enable`, `remove`. Lệnh có hiệu lực ngay, không cần khởi động lại dịch vụ.
- Trang quản trị mở được từ mọi nơi, nên mật khẩu quản trị cần thật mạnh và không dùng lại ở chỗ khác.

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
1. Mở `https://adminpc-system-product-name.taila6ff46.ts.net/` (máy chủ tự phục vụ giao diện mới) và đăng nhập bằng tài khoản quản trị. Vào **Quản trị → Tài khoản**, tạo một tài khoản bác sĩ thử.
2. Mở cửa sổ ẩn danh, đăng nhập bằng tài khoản bác sĩ đó và mật khẩu tạm, rồi đặt mật khẩu mới. Góc trên sẽ chuyển từ *Model: đang khởi động…* sang *Model: sẵn sàng*.
3. Xem thời gian nạp của từng bước:
   ```bash
   journalctl --user -u prenatal-web -n 60 --no-pager | grep 'Model 1'
   ```
   Gửi lại các dòng này để xem có kịp trong 30 giây không.
4. Đăng xuất cả hai tài khoản. Sau vài giây, `nvidia-smi` không còn tiến trình model. Thẻ **Quản trị → Nhật ký** có đủ các dòng đăng nhập, đổi mật khẩu và tách đoạn văn.

> Trang GitHub Pages (`ditruyenhungvuongorg.github.io/Fe2Gen`) vẫn là giao diện cũ cho đến khi nhánh này được merge vào `main`. Giao diện cũ chỉ có ô mã truy cập, nên sẽ không đăng nhập được vào máy chủ đã bật tài khoản. Trong lúc thử, hãy dùng địa chỉ `ts.net` ở trên.

## Khi train hoặc chạy AI cục bộ
```bash
bash ~/prenatal_pubcasefinder/releases/web-20261017/backend/gpu.sh pause    # Model 1 rời GPU trong ~10 giây
# ... train / LM Studio ...
bash ~/prenatal_pubcasefinder/releases/web-20261017/backend/gpu.sh resume
```
Có thể bấm thẳng trong **Quản trị → GPU**. Không cần dừng `prenatal-web`. Trong lúc tạm dừng, bác sĩ vẫn tra cứu HPO và đối chiếu bệnh được, chỉ phần tách đoạn văn báo "GPU đang tạm dành cho việc khác".

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
- Chưa có xác thực 2 lớp (đã thống nhất để sau).
- Máy chủ web vẫn là `http.server` của Python, đứng sau Tailscale.
- Khởi động lại dịch vụ hoặc tải lại trang thì phải đăng nhập lại, vì phiên chỉ giữ trong bộ nhớ.
