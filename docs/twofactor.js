'use strict';
// Account dialog, "Xác thực 2 lớp" (TOTP): turn on (setup key → confirm code → one-time backup codes) or off
// (password + code). Uses $, api, user, sessionInfo, refreshSession, openAccount, notice and FrontLogic from app.js.
(() => {
  let setup = null;     // {secret, uri} until the first code confirms it
  let backup = null;    // backup codes, shown once right after turning on
  let disabling = false, wasForced = false;

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v; else if (k.startsWith('on')) node[k] = v; else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function say(error = '', ok = '') {
    $('totp-error').textContent = error; $('totp-error').hidden = !error;
    $('totp-ok').textContent = ok; $('totp-ok').hidden = !ok;
  }
  async function run(button, work) {
    say(''); button.disabled = true;
    try { await work(); } catch (e) { say(e.message); } finally { button.disabled = false; }
  }
  // Clipboard on a shared computer: copy, then try to clear it a minute later.
  function copy(text, what) {
    if (!navigator.clipboard) return say('Trình duyệt không cho chép tự động; hãy chọn và chép bằng tay.');
    navigator.clipboard.writeText(text).then(() => {
      say('', 'Đã chép ' + what + '. Dán vào nơi lưu an toàn; bộ nhớ tạm sẽ được xóa sau 1 phút.');
      setTimeout(() => navigator.clipboard.writeText('').catch(() => {}), 60000);
    }, () => say('Không chép được; hãy chọn và chép bằng tay.'));
  }
  const codeInput = (attrs = {}) => el('input', {inputmode:'numeric', autocomplete:'one-time-code', maxlength:'7', spellcheck:'false', ...attrs});

  function setupView() {
    const secret = FrontLogic.formatSecret(setup.secret);
    const code = codeInput({'aria-label':'Mã 6 số để xác nhận'});
    const confirmButton = el('button', {}, 'Xác nhận và bật');
    const form = el('form', {class:'totp-form'}, el('label', {}, '3. Mã 6 số ứng dụng đang hiện', code),
      el('div', {class:'account-actions'}, confirmButton,
        el('button', {type:'button', class:'quiet', onclick:() => { setup = null; render(); }}, 'Hủy')));
    form.onsubmit = e => {
      e.preventDefault();
      const value = FrontLogic.normalizeCode(code.value);
      if (!value) return say('Nhập đủ 6 chữ số đang hiện trong ứng dụng.');
      run(confirmButton, async () => {
        const data = await api('/api/totp/enable', {code:value}, 30000);
        const codes = Array.isArray(data.backup_codes) ? data.backup_codes.filter(c => typeof c === 'string') : [];
        wasForced = !!sessionInfo?.totp_required;
        setup = null; backup = codes;
        sessionInfo = {...(sessionInfo || {}), totp_enabled:true, totp_required:false};
        refreshSession();
        openAccount();  // leave the "required" mode; the backup codes keep the dialog open until saved
      });
    };
    const uri = FrontLogic.isOtpauth(setup.uri) ? setup.uri : '';
    const qr = uri && window.QRCanvas ? window.QRCanvas.draw(uri) : null;
    return el('div', {class:'totp-setup'},
      el('p', {}, '1. Cài một ứng dụng xác thực trên điện thoại: Google Authenticator, Microsoft Authenticator hoặc Aegis.'),
      el('p', {}, '2. Trong ứng dụng chọn thêm tài khoản, ' + (qr ? 'quét mã QR hoặc ' : '') + 'chọn "Nhập khóa thiết lập" và gõ khóa dưới đây (loại: theo thời gian).'),
      qr ? el('div', {class:'totp-qr'}, qr) : null,
      el('p', {class:'totp-key'}, el('code', {class:'totp-secret'}, secret),
        el('button', {type:'button', class:'quiet', onclick:() => copy(setup.secret, 'khóa')}, 'Chép khóa')),
      el('p', {class:'hint'}, 'Tên tài khoản: Fe2Gen:' + (user?.username || '') + '. Không chụp hoặc gửi khóa này cho ai.'),
      uri ? el('details', {class:'totp-uri'}, el('summary', {}, 'Liên kết otpauth (cho ứng dụng nhận liên kết)'), el('code', {}, uri)) : null,
      form);
  }

  function backupView() {
    return el('div', {class:'backup-box'},
      el('p', {}, el('strong', {}, 'Đã bật xác thực 2 lớp. Mã dự phòng chỉ hiện lần này.')),
      el('p', {class:'hint'}, 'Mỗi mã dùng được một lần để đăng nhập khi không có điện thoại. Chép hoặc in ra và cất ở nơi an toàn (két, trình quản lý mật khẩu cá nhân); không lưu trên máy dùng chung, không gửi qua tin nhắn hay nhóm chat.'),
      backup.length ? el('ol', {class:'backup-codes'}, backup.map(c => el('li', {}, el('code', {}, c))))
                    : el('p', {class:'settings-error'}, 'Máy chủ không trả mã dự phòng. Nhờ quản trị viên kiểm tra.'),
      el('div', {class:'account-actions'},
        backup.length ? el('button', {type:'button', class:'quiet', onclick:() => copy(backup.join('\n'), 'mã dự phòng')}, 'Chép') : null,
        el('button', {type:'button', onclick:() => {
          if (backup.length && !confirm('Bạn đã lưu các mã dự phòng ở nơi an toàn? Sau khi đóng sẽ không xem lại được.')) return;
          backup = null; say('');
          if (wasForced) { $('account').close(); notice('Đã bật xác thực 2 lớp. Từ lần sau, đăng nhập cần thêm mã 6 số.'); }
          else openAccount();
        }}, 'Tôi đã lưu các mã')));
  }

  function disableView() {
    const password = el('input', {type:'password', autocomplete:'current-password'});
    const code = el('input', {autocomplete:'one-time-code', maxlength:'40', spellcheck:'false', autocapitalize:'none'});
    const off = el('button', {class:'danger-fill'}, 'Tắt xác thực 2 lớp');
    const form = el('form', {class:'totp-form'},
      el('label', {}, 'Mật khẩu hiện tại', password),
      el('label', {}, 'Mã 6 số trong ứng dụng (hoặc một mã dự phòng)', code),
      el('div', {class:'account-actions'}, off, el('button', {type:'button', class:'quiet', onclick:() => { disabling = false; render(); }}, 'Hủy')));
    form.onsubmit = e => {
      e.preventDefault();
      const value = FrontLogic.normalizeCode(code.value) || FrontLogic.normalizeCode(code.value, true);
      if (!password.value || !value) return say('Nhập mật khẩu và mã.');
      run(off, async () => {
        await api('/api/totp/disable', {password:password.value, code:value}, 30000);
        password.value = ''; disabling = false;
        sessionInfo = {...(sessionInfo || {}), totp_enabled:false};
        await refreshSession();  // an admin is asked to turn it on again at once
        openAccount(); say('', 'Đã tắt xác thực 2 lớp.');
      });
    };
    return form;
  }

  function render() {
    say('');
    const on = !!sessionInfo?.totp_enabled, admin = user?.role === 'admin', body = $('totp-body');
    $('totp-state').textContent = backup ? '' : on
      ? 'Đang bật: mỗi lần đăng nhập cần thêm mã 6 số từ ứng dụng xác thực trên điện thoại.'
      : (admin ? 'Chưa bật. Bắt buộc với tài khoản quản trị.' : 'Chưa bật (tùy chọn).') + ' Khi bật, đăng nhập cần mật khẩu và mã 6 số đổi mỗi 30 giây trên điện thoại, nên lộ mật khẩu chưa đủ để vào.';
    if (backup) return body.replaceChildren(backupView());
    if (setup) return body.replaceChildren(setupView());
    if (on && disabling) return body.replaceChildren(disableView());
    if (on) return body.replaceChildren(el('button', {type:'button', class:'quiet', onclick:() => { disabling = true; render(); }}, 'Tắt xác thực 2 lớp…'));
    const start = el('button', {type:'button'}, 'Bật xác thực 2 lớp');
    start.onclick = () => run(start, async () => {
      const data = await api('/api/totp/setup', {}, 30000);
      if (!data || typeof data.secret !== 'string' || !/^[A-Z2-7=\s]+$/i.test(data.secret)) throw Error('Máy chủ trả khóa không hợp lệ.');
      setup = {secret:data.secret, uri:data.uri}; render();
      $('totp-body').querySelector('input')?.focus();
    });
    body.replaceChildren(start);
  }

  window.renderTwoFactor = render;
  // Backup codes on screen: keep the dialog open until the user confirms they saved them.
  window.twoFactorBlocking = () => !!backup;
  // A new sign-in (or sign-out) must not see the previous user's setup key or codes.
  window.resetTwoFactor = () => { setup = null; backup = null; disabling = false; wasForced = false; };
})();
