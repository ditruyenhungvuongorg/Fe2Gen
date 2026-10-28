# Gắn tên miền riêng cho Fe2Gen (chuẩn bị, chưa kích hoạt)

Tên miền dưới đây là **tên tạm**: `fe2gen.vn`. Khi mua tên miền thật, thay toàn bộ `fe2gen.vn` trong thư mục này
bằng tên đã mua (lệnh ở mục 5). Chưa có file nào trong đây làm web hiện tại thay đổi; web vẫn chạy ở
`https://ditruyenhungvuongorg.github.io/Fe2Gen/` (giao diện) và `https://adminpc-system-product-name.taila6ff46.ts.net` (API).

Hai phần của web nằm ở hai nơi, nên tên miền cũng chia hai:

| Phần | Hiện tại | Sau khi gắn tên miền | Việc cần làm |
| --- | --- | --- | --- |
| Giao diện (GitHub Pages, thư mục `docs/`) | `ditruyenhungvuongorg.github.io/Fe2Gen` | `https://fe2gen.vn` | Bước 1–3, chỉ cần DNS + 1 file |
| API (máy Ubuntu, qua Tailscale Funnel) | `adminpc-system-product-name.taila6ff46.ts.net` | `https://api.fe2gen.vn` (tùy chọn) | Bước 4, cần thêm Cloudflare Tunnel hoặc máy trung gian |

Khuyến nghị: làm bước 1–3 trước (bác sĩ gõ `fe2gen.vn` là vào được), API cứ để trên `ts.net`. Bước 4 làm sau nếu
muốn giấu hẳn địa chỉ Tailscale.

## 1. DNS tại nơi mua tên miền

Thêm các bản ghi sau (GitHub Pages yêu cầu đúng 4 địa chỉ A này, xem docs.github.com "Managing a custom domain"):

```
fe2gen.vn      A      185.199.108.153
fe2gen.vn      A      185.199.109.153
fe2gen.vn      A      185.199.110.153
fe2gen.vn      A      185.199.111.153
www.fe2gen.vn  CNAME  ditruyenhungvuongorg.github.io.
```

Kiểm tra sau 5–30 phút: `nslookup fe2gen.vn` trả về 4 địa chỉ trên.

## 2. GitHub Pages

1. Repo `ditruyenhungvuongorg/Fe2Gen` → Settings → Pages → Custom domain: gõ `fe2gen.vn` → Save. GitHub tự tạo file
   `docs/CNAME` (nội dung một dòng `fe2gen.vn`) và commit vào `main`. Không tự tạo file này trước khi DNS xong,
   vì Pages sẽ báo lỗi và trang github.io có thể ngừng phục vụ.
   Bản mẫu: `backend/ops/ten_mien/CNAME.example` (chép thành `docs/CNAME` nếu muốn commit tay).
2. Chờ dòng "DNS check successful", tick **Enforce HTTPS** (GitHub cấp chứng chỉ Let's Encrypt, mất đến 1 giờ).
3. Vì tên miền riêng phục vụ ở gốc `/` (không còn `/Fe2Gen/`), mọi đường dẫn trong `docs/` đều là tương đối
   (`styles.css`, `app.js`, `./`) nên không cần sửa gì.

## 3. Cho API chấp nhận giao diện mới (máy Ubuntu)

Giao diện ở `fe2gen.vn` gọi API ở `ts.net` là gọi **khác nguồn**; API chỉ trả lời nguồn có trong `WEB_ALLOWED_ORIGINS`.

```bash
DEP=~/prenatal_pubcasefinder/.deployment
cp -p "$DEP/web.env" "$DEP/web.env.backup-tenmien-$(date +%Y%m%d-%H%M)"
sed -i 's#^WEB_ALLOWED_ORIGINS=.*#WEB_ALLOWED_ORIGINS=https://ditruyenhungvuongorg.github.io,https://fe2gen.vn,https://www.fe2gen.vn#' "$DEP/web.env"
grep ^WEB_ALLOWED_ORIGINS "$DEP/web.env"
systemctl --user restart prenatal-web && sleep 3 && curl -s http://127.0.0.1:8000/api/status
```

Giữ nguyên `github.io` trong danh sách cho tới khi chắc chắn mọi bác sĩ đã chuyển sang tên mới. Kiểm tra CORS:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: https://fe2gen.vn' http://127.0.0.1:8000/api/status   # 200
```

Phía giao diện: `docs/index.html` có `connect-src 'self' https://adminpc-system-product-name.taila6ff46.ts.net`,
tức trang đã được phép gọi API ở `ts.net` dù trang mở từ `fe2gen.vn`. Không cần sửa `docs/config.js`.

## 4. (Tùy chọn) API ở `api.fe2gen.vn`

Tailscale Funnel chỉ cấp chứng chỉ cho tên `*.ts.net`, nên trỏ CNAME `api.fe2gen.vn → ...ts.net` sẽ **lỗi chứng chỉ**.
Hai cách làm được:

- **Cloudflare Tunnel** (miễn phí, không mở cổng): đưa `fe2gen.vn` vào Cloudflare (đổi nameserver), trên Ubuntu cài
  `cloudflared`, tạo tunnel trỏ `api.fe2gen.vn → http://127.0.0.1:8000`. Cloudflare cấp HTTPS; Funnel có thể tắt.
  Rủi ro cần cân nhắc: Cloudflare giải mã TLS ở biên (họ đọc được dữ liệu đi qua), xem `backend/ops/ADR-funnel-vs-tailnet.md`.
- **Máy trung gian** (VPS có IP tĩnh): nginx + Let's Encrypt cho `api.fe2gen.vn`, `proxy_pass` về máy Ubuntu qua
  Tailscale (tailnet riêng, không qua Funnel). Mẫu nginx: `backend/ops/ten_mien/nginx-api.fe2gen.vn.conf.example`.

Sau khi API đổi địa chỉ, sửa đúng 3 chỗ trong repo rồi deploy giao diện (push `main`):

| File | Dòng | Thay |
| --- | --- | --- |
| `docs/config.js` | `apiBase: 'https://adminpc-system-product-name.taila6ff46.ts.net'` | `https://api.fe2gen.vn` |
| `docs/index.html` | `connect-src 'self' https://adminpc-system-product-name.taila6ff46.ts.net` | `https://api.fe2gen.vn` (giữ thêm host cũ trong lúc chuyển đổi) |
| `backend/serve_web.py` | hằng `CSP` (connect-src 'self') | không đổi, trang do backend tự phục vụ gọi cùng nguồn |

Và trong `web.env`: `WEB_ALLOWED_ORIGINS` thêm `https://api.fe2gen.vn` **không** cần (origin là trang, không phải API).

## 5. Khi có tên miền thật

```bash
cd ~/Fe2Gen            # hoặc C:\Fe2Gen
grep -rl 'fe2gen\.vn' backend/ops/ten_mien | xargs sed -i 's/fe2gen\.vn/TEN-MOI.vn/g'
```

Rồi làm lại từ bước 1 với tên mới. Các tài liệu nhắc địa chỉ cũ (`DEPLOYMENT_REPORT.md`, `CAP_NHAT_GPU_DANG_NHAP.md`)
là báo cáo lịch sử, không cần sửa.

## Kiểm tra cuối

- Mở `https://fe2gen.vn` trên điện thoại ngoài mạng bệnh viện: trang hiện, góc trên báo "Đã kết nối".
- Đăng nhập bác sĩ, tách một đoạn văn, đối chiếu: chạy như trên địa chỉ cũ.
- `https://ditruyenhungvuongorg.github.io/Fe2Gen/` tự chuyển hướng sang `fe2gen.vn` (GitHub làm sẵn).
