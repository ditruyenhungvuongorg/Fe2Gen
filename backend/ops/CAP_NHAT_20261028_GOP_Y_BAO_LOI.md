# Cập nhật 28/10/2026: Góp ý sửa mã và Báo lỗi

Tài liệu cho người vận hành máy chủ Ubuntu. Hai chức năng mới chạy trên bản web đang có (Model 1 v7.1), không cần
train lại, không cần GPU. Mỗi bước có lệnh kiểm tra và cách quay lui.

## Thay đổi

### Góp ý sửa mã (bác sĩ gửi, quản trị duyệt, có hiệu lực ngay)
- Khi model gán sai mã cho một cụm từ (ví dụ "thai to" ra *thai nhỏ so với tuổi thai*), bác sĩ chọn mã đúng và bấm
  **Gửi góp ý**. Góp ý ghi lại cụm từ nguyên văn, mã sai, mã đúng, một đoạn ngữ cảnh ngắn và ghi chú (nếu có).
- Quản trị xem ở **Quản trị → Góp ý sửa mã**, bấm **Duyệt** hoặc **Từ chối**; có thể đổi quyết định sau. Trạng
  thái: *Chờ duyệt / Đã duyệt / Đã từ chối*. Bác sĩ chỉ thấy góp ý của chính mình.
- **Hiệu lực ngay sau khi duyệt, không cần khởi động lại dịch vụ:** máy chủ dựng lại bảng tra `cụm từ → mã` trong
  bộ nhớ. Từ lúc đó:
  - **Tách đoạn văn**: cụm từ đúng nguyên văn (không phân biệt hoa thường, khoảng trắng; giữ dấu) được xếp mã đã duyệt
    lên **đầu** danh sách gợi ý, mapping `doctor_correction`, giải thích ghi "mã đầu tiên theo góp ý bác sĩ đã được
    quản trị duyệt". Góp ý đã duyệt đứng trên cả từ điển cụm bác sĩ từ 234 ca gold (`doctor_clinical_phrases.json`).
    Bác sĩ vẫn phải xác nhận từng mã như trước.
  - **Tra cứu HPO**: gõ đúng cụm từ đó (không phân biệt dấu) thì mã đã duyệt đứng đầu kết quả.
  - Góp ý bị từ chối hoặc thu hồi không còn tác dụng ngay lúc đó.
- Góp ý chỉ ảnh hưởng đến thứ tự gợi ý; model không bị sửa. Để model học hẳn, xem mục *Xuất góp ý cho lần train sau*.

### Báo lỗi (mọi vai trò, lưu trên máy chủ, không gửi email)
- Nút **Báo lỗi** trên trang: mô tả lỗi (tối đa 4.000 ký tự) và tối đa **3 ảnh chụp màn hình** (PNG/JPEG/WEBP, mỗi ảnh
  tối đa 4 MB). Trang tự kèm phiên bản model, kích thước cửa sổ, trình duyệt và vị trí đang mở.
- Quản trị xem ở **Quản trị → Báo lỗi**: đọc mô tả, tải ảnh, đánh dấu **Đã xử lý** (hoặc mở lại), **Xóa**.
  Ảnh chỉ tải được bằng phiên quản trị; không có đường dẫn công khai.
- Ảnh được kiểm tra chữ ký file (magic bytes) phải khớp loại khai báo; tên file của người gửi chỉ lưu làm ghi chú,
  không dùng làm đường dẫn.
- Giới hạn kho: mỗi tài khoản tối đa 20 báo lỗi *chưa xử lý* (`WEB_BUG_REPORTS_PER_USER`) và cả thư mục tối đa
  500 MB ảnh (`WEB_BUG_REPORTS_MAX_MB`); vượt thì trang báo cho người gửi, quản trị đánh dấu đã xử lý hoặc xóa bớt
  là gửi lại được. Trang quản trị hiển thị tối đa 200 báo lỗi mới nhất, số đếm vẫn đầy đủ.

### Nhật ký kiểm toán
Thêm các sự kiện: *Gửi góp ý sửa mã*, *Duyệt góp ý sửa mã*, *Từ chối góp ý sửa mã*, *Tải góp ý đã duyệt*, *Gửi báo
lỗi*, *Cập nhật báo lỗi*, *Xóa báo lỗi*. Mỗi dòng chỉ có người, thời gian, mã định danh (12 ký tự hex) và số lượng;
**không có cụm từ, mã HPO, ghi chú hay nội dung báo lỗi**.

## File mới trong `~/prenatal_pubcasefinder/.deployment/`
| File / thư mục | Quyền | Nội dung |
|---|---|---|
| `corrections.json` | 600 | Mọi góp ý (chờ duyệt / đã duyệt / đã từ chối), ghi nguyên tử (file tạm + đổi tên). Cụm từ lâm sàng, không có danh tính bệnh nhân. |
| `bug_reports/` | 700 | `index.json` (600, siêu dữ liệu) và ảnh `<id>_<n>.png|jpg|webp` (600). Ảnh chụp màn hình **có thể lộ ca bệnh**. |

Cả hai được tạo khi có bản ghi đầu tiên; dịch vụ đọc lại file khi nó đổi trên đĩa. Hai mục này đã được thêm vào
`.gitignore`, hook pre-commit và sao lưu mã hóa (xem dưới).

## Biến môi trường (đều có mặc định; chỉ thêm vào `web.env` khi muốn đổi)
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WEB_CORRECTIONS_FILE` | `~/prenatal_pubcasefinder/.deployment/corrections.json` | Nơi lưu góp ý. |
| `WEB_BUG_REPORTS_DIR` | `~/prenatal_pubcasefinder/.deployment/bug_reports` | Thư mục báo lỗi và ảnh. |
| `WEB_BUG_REPORTS_PER_USER` | `20` | Số báo lỗi chưa xử lý tối đa mỗi tài khoản; vượt → 422, gửi lại được sau khi quản trị xử lý/xóa. `0` tắt. |
| `WEB_BUG_REPORTS_MAX_MB` | `500` | Tổng dung lượng ảnh (MB) của thư mục báo lỗi; vượt → 422 "Kho báo lỗi đã đầy…" cho đến khi quản trị xóa bớt. `0` tắt. |
| `WEB_MAX_UPLOAD_BYTES` | `8000000` | Body tối đa riêng cho `/api/bug_reports` (3 ảnh base64). Mọi route khác giữ 100.000 byte. |
| `WEB_RATE_CORRECTIONS` | `20` | Góp ý/phút mỗi phiên; vượt → 429 + `Retry-After`. `0` tắt. |
| `WEB_RATE_BUG_REPORT` | `5` | Báo lỗi/phút mỗi phiên. `0` tắt. |

`deploy_ubuntu.sh` ghi các dòng này ở dạng chú thích vào `web.env` **mới**; `web.env` đang có không bị sửa.
Bảng đầy đủ: `backend/ops/TRIEN_KHAI_BAO_MAT.md`.

## Triển khai
```bash
REL=web-20261028-gopy
NEW=~/prenatal_pubcasefinder/releases/$REL
git clone --branch doctor-feedback-bugreport https://github.com/ditruyenhungvuongorg/Fe2Gen.git "$NEW"
cp -a ~/prenatal_pubcasefinder/releases/<bản đang chạy>/backend/data "$NEW/backend/data"   # hp.obo, phenotype.hpoa, ...
cd "$NEW/backend"
~/venvs/nckh-model1-span-v1/bin/python test_access.py      # không GPU, không chạm users.json thật
~/venvs/nckh-model1-span-v1/bin/python test_web.py         # ~3 phút
~/venvs/nckh-model1-span-v1/bin/python test_model_integrity.py
bash "$NEW/backend/deploy_ubuntu.sh"                        # viết lại unit trỏ vào $NEW; web.env giữ nguyên
systemctl --user restart prenatal-web
journalctl --user -u prenatal-web -n 30 --no-pager | grep -E 'Góp ý|Web ready'   # "[Góp ý] 0 cụm từ ... được áp dụng."
```
Nếu chạy dưới user riêng `fe2gen` (`backend/ops/separate_user.md`): làm các lệnh trên qua `asweb`, và khi chuyển
`.deployment` nhớ chép thêm `corrections.json` và `bug_reports/` (đã thêm vào danh sách trong tài liệu đó).

### Sao lưu: chạy lại `--setup`
`backup_deployment.sh` giờ gói thêm `corrections.json` và cả thư mục `bug_reports/`. Timer dùng bản sao ở
`~/.local/bin/fe2gen-backup`, nên **phải chạy lại** để bản sao được cập nhật:
```bash
bash "$NEW/backend/ops/backup_deployment.sh" --setup
bash "$NEW/backend/ops/backup_deployment.sh" --check "$(ls -t ~/prenatal_pubcasefinder/backups/fe2gen-deployment-* | head -1)"
```
Danh sách phải có `corrections.json` và `bug_reports/` khi hai mục đó đã tồn tại.

### Kiểm tra trên web
1. Đăng nhập bác sĩ, tách một đoạn có cụm bị gán sai, chọn mã đúng, **Gửi góp ý**. Thẻ góp ý của bác sĩ hiện *Chờ duyệt*.
2. Đăng nhập quản trị (cửa sổ ẩn danh), **Quản trị → Góp ý sửa mã → Duyệt**.
3. Bác sĩ tách lại đoạn đó: mã đã duyệt đứng đầu, giải thích có "góp ý bác sĩ đã được quản trị duyệt". Gõ cụm từ vào
   ô tra cứu HPO: mã đó đứng đầu.
4. Gửi một báo lỗi kèm ảnh; quản trị thấy ở **Quản trị → Báo lỗi**, tải được ảnh, đánh dấu **Đã xử lý**.
5. `ls -l ~/prenatal_pubcasefinder/.deployment/` : `corrections.json` 600, `bug_reports` 700.
6. **Quản trị → Nhật ký** có các dòng mới, không dòng nào chứa cụm từ hay mã HPO.

## Xuất góp ý cho lần train sau (không train ở đây)
Hai cách lấy cùng một nội dung (chỉ góp ý **đã duyệt**):
- **Quản trị → Góp ý sửa mã → Tải góp ý đã duyệt**: file `fe2gen_gop_y_<YYYYMMDD>.json` gồm `items` (cụm từ, mã đúng,
  mã sai, ngữ cảnh, ghi chú, người gửi, người duyệt), `memory_rows` và `doctor_phrases`.
- Trên máy chủ, từ `corrections.json`:
  ```bash
  cd "$NEW/backend"
  python3 tools/export_corrections.py --file ~/prenatal_pubcasefinder/.deployment/corrections.json --out ~/gop_y_$(date +%Y%m%d)
  ```
Kết quả trong thư mục `--out`:
- `memory_additions.jsonl`: mỗi dòng `{"query_text", "hpo_id"}` (cụm từ viết thường, NFC, gọn khoảng trắng). Đây là
  các dòng **memory** thêm vào khi chạy `backend/model1_v6/build_bundle.py` cho bundle kế tiếp (nối vào nguồn memory
  hiện có của retriever; bundle v7.1 dùng memory không dấu nên build_bundle sẽ chuẩn hóa theo cách của nó).
- `doctor_phrases_additions.json`: `[{"id", "vi", "en"}]`, cùng cấu trúc với `data/doctor_clinical_phrases.json`.
  Gộp vào file đó (tránh trùng `vi` + `id`) rồi khởi động lại dịch vụ để từ điển cụm bác sĩ có thêm các cụm này
  ngay cả khi `corrections.json` bị xóa.
Hai file này là nội dung lâm sàng (cụm từ mô tả thai): chép bằng kênh nội bộ, không đưa lên GitHub
(`*.jsonl` ngoài `backend/resources/` đã bị hook chặn).

## Quay lui
```bash
systemctl --user stop prenatal-web
bash ~/prenatal_pubcasefinder/releases/<bản trước>/backend/deploy_ubuntu.sh
systemctl --user restart prenatal-web
```
Bản cũ bỏ qua các biến lạ trong `web.env` và không đọc `corrections.json` / `bug_reports/`; hai mục đó cứ để
nguyên (chúng vẫn nằm trong sao lưu mã hóa) cho lần triển khai lại.

## Giới hạn hiện tại
- Góp ý chỉ khớp **nguyên cụm từ**; cụm viết khác đi ("thai lớn" thay "thai to") cần góp ý riêng.
- Góp ý được áp dụng theo thứ tự duyệt mới nhất trước khi hai bác sĩ chọn mã khác nhau cho cùng cụm; quản trị nên
  từ chối một trong hai để danh sách rõ ràng.
- Báo lỗi không gửi thông báo; quản trị cần mở thẻ **Báo lỗi** định kỳ (số báo lỗi mới hiện trên thẻ).
- Chưa có dọn tự động: báo lỗi đã xử lý và ảnh nằm lại cho đến khi quản trị **Xóa**.
