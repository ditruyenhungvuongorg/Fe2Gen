# ADR: Tailscale Funnel (Internet) hay chỉ tailnet cho web Fe2Gen

- Trạng thái: **đề xuất, chờ người dùng chọn** (26/10/2026)
- Liên quan: B5 trong rà soát bảo mật; `backend/ops/harden_host.sh`; nhánh 2FA (TOTP) do phiên khác làm.

## Bối cảnh
Giao diện tĩnh nằm trên GitHub Pages (công khai). Nó gọi API trên máy Ubuntu qua Tailscale Funnel: HTTPS mở ra
Internet, hostname `*.ts.net` có trong `docs/config.js` công khai. Sau Funnel là `serve_web.py` (Python `http.server`)
bind 127.0.0.1. Ai trên Internet cũng chạm được trang đăng nhập, API và trang quản trị; chỉ mật khẩu đứng chắn.
Máy chủ không lưu nội dung ca (audit chỉ ghi con số), nhưng tài khoản admin điều khiển mọi tài khoản và GPU.

## Phương án
### (a) Bỏ Funnel, chỉ phục vụ trong tailnet (`tailscale serve`)
```bash
tailscale funnel --https=443 off
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status
```
Cú pháp theo Tailscale từ 1.52; xem cấu hình hiện tại trước bằng `tailscale funnel status`.
- Mỗi bác sĩ cài Tailscale trên máy/điện thoại và được mời vào tailnet (hoặc được chia sẻ riêng máy này).
- `tailscale serve` thêm header `Tailscale-User-Login` (và `Tailscale-User-Name`) cho request từ người dùng tailnet.
  Backend có thể đòi header này khớp với tài khoản đăng nhập, thành lớp xác thực thứ hai. Chỉ tin header khi
  Funnel đã tắt và request đến từ 127.0.0.1; cần một biến bật riêng và test (chưa có code).
- Được: API, trang quản trị và `http.server` biến mất khỏi Internet. Dò mật khẩu, DoS, lỗ hổng chưa biết của
  `http.server` chỉ còn trong tầm những thiết bị đã vào tailnet.
- Mất: bác sĩ phải cài và đăng nhập Tailscale; máy dùng chung trong bệnh viện có thể không cho cài VPN; quản lý
  thiết bị khi bác sĩ nghỉ hoặc đổi máy; gói miễn phí của Tailscale giới hạn số người dùng (kiểm giá hiện hành).
  GitHub Pages vẫn dùng được nếu thiết bị đang ở trong tailnet, hoặc mở thẳng giao diện do máy chủ phục vụ.

### (b) Giữ Funnel, bắt buộc TOTP 2FA cho admin
- 2FA TOTP (RFC 6238) bắt buộc cho admin, tùy chọn cho bác sĩ; mã dự phòng một lần; `/api/admin/*` đòi phiên đã
  qua 2FA. Đi kèm các lớp đang làm ở nhánh khác: giới hạn tốc độ, chặn số kết nối, fail-closed khi không đọc được
  `users.json`, access log, HSTS.
- Được: bác sĩ không phải cài gì, dùng được từ mọi máy; phần lớn đã có code.
- Còn rủi ro: `http.server` vẫn ra Internet; tài khoản bác sĩ không bắt buộc 2FA nên có thể bị lừa lấy mật khẩu;
  DoS chỉ được giảm, không loại bỏ; hostname công khai nên bị quét thường xuyên.

### (c) Biến thể lai (cần sửa backend, ghi lại để cân nhắc sau)
API cho bác sĩ vẫn qua Funnel; API quản trị chạy ở cổng riêng chỉ có `tailscale serve` (tailnet), cổng Funnel trả 404
cho `/api/admin/*`. Chỉ admin cần cài Tailscale.

## Khuyến nghị
**Chọn (b) ngay bây giờ**, vì nó không đổi cách bác sĩ làm việc và code 2FA đã ở nhánh khác; chạy cùng
`harden_host.sh` (tường lửa chỉ mở tailnet) và hardening systemd. Theo dõi access log trong 4 tuần đầu.
Coi (c) là bước tiếp theo nếu muốn đóng trang quản trị khỏi Internet mà bác sĩ không phải cài gì.

## Khi nào đổi sang (a) (hoặc (c))
Đổi khi có **một** trong các điều sau:
- máy chủ bắt đầu lưu nội dung ca, tên hoặc mã bệnh nhân, hay hội đồng đạo đức / phòng CNTT bệnh viện yêu cầu
  không mở dịch vụ ra Internet (dữ liệu sức khỏe là dữ liệu cá nhân nhạy cảm);
- access log cho thấy dò mật khẩu hoặc quét kéo dài vượt giới hạn tốc độ, hoặc có lỗ hổng trong `http.server` /
  `serve_web.py` chưa vá được ngay;
- số người dùng nhỏ và ổn định (khoảng ≤ 10) và tất cả cài được Tailscale trên máy họ dùng;
- một tài khoản admin hoặc bác sĩ bị lộ mật khẩu.

Giữ (b) chừng nào bác sĩ còn phải dùng máy bệnh viện không cài được VPN và máy chủ vẫn không lưu dữ liệu ca.

## Quay lui
- (a) → (b): `tailscale serve --https=443 off`, rồi `tailscale funnel --bg --https=443 http://127.0.0.1:8000`.
- (b) → (a): các lệnh ở mục (a). Tài khoản và 2FA giữ nguyên trong cả hai cách.
