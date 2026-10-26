'use strict';
// Admin page: accounts, open sessions, audit log, GPU. Uses $, api, user and ROLE_NAMES from app.js.
(() => {
  const STATE = {stopped:'Nghỉ (không chiếm GPU)', loading:'Đang khởi động', ready:'Sẵn sàng (đang chiếm GPU)', paused:'Tạm dừng', failed:'Chưa chạy được'};
  const DETAIL = {target:'tài khoản', role:'vai', changes:'thay đổi', chars:'số ký tự', findings:'số cụm', seconds:'giây',
                  hpo_count:'số HPO', wes:'có gene WES', action:'thao tác', count:'số phiên'};
  const ACTIONS = {pause:'tạm dừng', resume:'cho chạy lại', release:'giải phóng ngay'};
  let tab = 'users', temp = null;

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v; else if (k.startsWith('on')) node[k] = v; else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function status(message = '', error = false) {
    $('admin-status').textContent = message; $('admin-status').hidden = !message;
    $('admin-status').className = 'admin-status' + (error ? ' settings-error' : '');
  }
  async function act(run, done) {
    status('');
    try { const data = await run(); if (done) status(done(data)); await show(tab, true); }
    catch (e) { status(e.message, true); }
  }
  // locked_until: epoch seconds or ms, or a date string; returns false, true (time unknown) or the local time.
  function lockUntil(value) {
    if (value == null || value === '' || value === false || value === 0) return false;
    const ms = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
    if (!Number.isFinite(ms)) return true;
    return ms > Date.now() ? new Date(ms).toLocaleTimeString('vi-VN', {hour:'2-digit', minute:'2-digit'}) : false;
  }
  async function show(name, keepStatus = false) {
    tab = name;
    for (const b of document.querySelectorAll('#admin [data-tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
    if (!keepStatus) status('');
    const body = $('admin-body');
    if (!body.children.length) body.append(el('p', {class:'hint'}, 'Đang tải…'));
    try { body.replaceChildren(await VIEWS[name]()); }
    catch (e) { body.replaceChildren(); status(e.message, true); }
  }

  const VIEWS = {
    async users() {
      const [data, open] = await Promise.all([api('/api/admin/users', null, 20000), api('/api/admin/sessions', null, 20000)]);
      const online = new Set(open.sessions.map(s => s.username));
      const roleSelect = (value, attrs = {}) => el('select', attrs, Object.entries(data.roles).map(([k, v]) => el('option', {value:k, selected:k === value}, v)));
      const form = el('form', {class:'admin-create'},
        el('label', {}, 'Tên đăng nhập', el('input', {name:'username', required:true, maxlength:32, autocomplete:'off', autocapitalize:'none', spellcheck:'false', placeholder:'vd: bs.an, lab.hv'})),
        el('label', {}, 'Tên hiển thị', el('input', {name:'name', maxlength:80, placeholder:'vd: BS. Nguyễn Văn An'})),
        el('label', {}, 'Vai trò', roleSelect('doctor', {name:'role'})),
        el('button', {}, 'Tạo tài khoản'));
      form.onsubmit = e => {
        e.preventDefault();
        const f = new FormData(form);
        act(async () => { temp = await api('/api/admin/users/create', {username:f.get('username'), name:f.get('name'), role:f.get('role')}, 20000); },
            () => 'Đã tạo tài khoản ' + temp.username + '.');
      };
      const parts = [el('h3', {class:'account-sub'}, 'Tạo tài khoản'), form];
      if (temp) {
        const code = el('code', {class:'temp-password'}, temp.temp_password);
        parts.push(el('div', {class:'temp-box'},
          el('p', {}, el('strong', {}, 'Mật khẩu tạm của ' + temp.username + ': '), code),
          el('p', {class:'hint'}, 'Chỉ hiện lần này. Gửi riêng cho người dùng (không gửi qua nhóm chung); họ phải đặt mật khẩu mới ở lần đăng nhập đầu.'),
          el('button', {type:'button', class:'quiet', onclick:() => { navigator.clipboard?.writeText(temp.temp_password).then(() => status('Đã chép mật khẩu tạm.'), () => {}); }}, 'Chép'),
          el('button', {type:'button', class:'quiet', onclick:() => { temp = null; show('users'); }}, 'Ẩn')));
      }
      parts.push(el('h3', {class:'account-sub'}, data.users.length + ' tài khoản · ' + online.size + ' đang online'));
      for (const u of data.users) {
        const self = u.username === user?.username;
        const state = u.disabled ? ['Đã khóa', 'locked'] : u.must_change ? ['Chờ đặt mật khẩu', 'pending'] : ['Hoạt động', 'active'];
        // Login lockout after wrong passwords: shown when the server reports it; without the field the button stays.
        const lockedUntil = lockUntil(u.locked_until), lockKnown = 'locked_until' in u;
        const totp = u.totp_enabled === true ? ' · 2FA bật' : u.totp_enabled === false ? ' · chưa bật 2FA' : '';
        const role = roleSelect(u.role, {'aria-label':'Vai trò của ' + u.username});
        role.onchange = () => {
          if (!confirm('Đổi vai trò của ' + u.username + ' thành ' + data.roles[role.value] + '?')) { role.value = u.role; return; }
          act(() => api('/api/admin/users/update', {username:u.username, role:role.value}, 20000), () => 'Đã đổi vai trò ' + u.username + '.');
        };
        parts.push(el('div', {class:'admin-row'},
          el('div', {class:'admin-who'}, el('strong', {}, u.name), el('small', {}, u.username + (self ? ' · bạn' : '') + totp + ' · tạo ' + (u.created || '—') + ' · đăng nhập gần nhất ' + (u.last_login || 'chưa')),
            lockedUntil ? el('small', {class:'lock-note'}, 'Tạm khóa đăng nhập do nhập sai nhiều lần' + (lockedUntil === true ? '' : ' · đến ' + lockedUntil)) : null),
          el('span', {class:'state-pill ' + state[1]}, state[0], online.has(u.username) ? ' · online' : null), role,
          el('div', {class:'admin-buttons'},
            el('button', {type:'button', class:'quiet', onclick:() => {
              const name = prompt('Tên hiển thị mới của ' + u.username + ':', u.name);
              if (name == null || !name.trim() || name.trim() === u.name) return;
              act(async () => {
                await api('/api/admin/users/update', {username:u.username, name:name.trim()}, 20000);
                if (self) { user.name = name.trim().slice(0, 80); showUser(); }
              }, () => 'Đã đổi tên ' + u.username + '.');
            }}, 'Đổi tên'),
            el('button', {type:'button', class:'quiet', disabled:self, onclick:() => {
              if (!u.disabled && !confirm('Khóa ' + u.username + '? Người này bị đăng xuất ngay.')) return;
              act(() => api('/api/admin/users/update', {username:u.username, disabled:!u.disabled}, 20000), () => (u.disabled ? 'Đã mở khóa ' : 'Đã khóa ') + u.username + '.');
            }}, u.disabled ? 'Mở khóa' : 'Khóa'),
            el('button', {type:'button', class:'quiet', onclick:() => {
              if (!confirm('Đặt lại mật khẩu của ' + u.username + '? Người này bị đăng xuất và nhận mật khẩu tạm mới.')) return;
              act(async () => { temp = await api('/api/admin/users/reset', {username:u.username}, 20000); }, () => 'Đã đặt lại mật khẩu ' + u.username + '.');
            }}, 'Đặt lại mật khẩu'),
            u.totp_enabled === false ? null : el('button', {type:'button', class:'quiet', onclick:() => {
              if (!confirm('Đặt lại xác thực 2 lớp của ' + u.username + '? Ứng dụng trên điện thoại cũ và các mã dự phòng hết hiệu lực; ' +
                (u.role === 'admin' ? 'tài khoản quản trị phải bật lại 2FA ngay khi đăng nhập.' : 'người này đăng nhập chỉ bằng mật khẩu cho đến khi tự bật lại.'))) return;
              act(async () => {
                await api('/api/admin/users/totp_reset', {username:u.username}, 20000);
                if (self) { await refreshSession(); if (sessionInfo?.totp_required) { $('admin').close(); openAccount(); } }
              }, () => 'Đã đặt lại 2FA của ' + u.username + '.');
            }}, 'Đặt lại 2FA'),
            u.disabled || (lockKnown && !lockedUntil) ? null : el('button', {type:'button', class:'quiet', title:'Xóa đếm đăng nhập sai để người này thử lại ngay', onclick:() =>
              act(() => api('/api/admin/users/unlock', {username:u.username}, 20000), () => 'Đã mở khóa đăng nhập cho ' + u.username + '.')}, 'Mở khóa đăng nhập'),
            el('button', {type:'button', class:'quiet danger', disabled:self, onclick:() => {
              if (prompt('Xóa vĩnh viễn tài khoản này? Gõ "' + u.username + '" để xác nhận.') !== u.username) return;
              act(() => api('/api/admin/users/delete', {username:u.username}, 20000), () => 'Đã xóa ' + u.username + '.');
            }}, 'Xóa'))));
      }
      return el('div', {}, parts);
    },

    async sessions() {
      const data = await api('/api/admin/sessions', null, 20000);
      const rows = data.sessions.map(s => el('div', {class:'admin-row'},
        el('div', {class:'admin-who'}, el('strong', {}, s.name), el('small', {}, s.username + ' · ' + (ROLE_NAMES[s.role] || s.role) + ' · từ ' + s.since + ' · ' + (s.idle_minutes ? 'không thao tác ' + s.idle_minutes + ' phút' : 'vừa thao tác'))),
        el('button', {type:'button', class:'quiet', onclick:() => {
          if (!confirm('Buộc ' + s.username + ' đăng xuất trên mọi thiết bị' + (s.username === user?.username ? ' khác' : '') + '?')) return;
          act(() => api('/api/admin/sessions/end', {username:s.username}, 20000), r => 'Đã đăng xuất ' + r.ended + ' phiên của ' + s.username + '.');
        }}, 'Buộc đăng xuất')));
      return el('div', {}, el('p', {class:'hint'}, data.sessions.length + ' phiên đang mở. Phiên tự hết sau ' + FrontLogic.idleMinutesOf(sessionInfo?.idle_minutes) + ' phút không thao tác' + (sessionInfo?.session_hours ? ' hoặc ' + sessionInfo.session_hours + ' giờ kể từ khi đăng nhập' : '') + '; khởi động lại dịch vụ thì mọi người đăng nhập lại.'), rows);
    },

    async audit() {
      const f = VIEWS.auditFilter || (VIEWS.auditFilter = {user:'', event:''});
      const data = await api('/api/admin/audit?limit=300&user=' + encodeURIComponent(f.user) + '&event=' + encodeURIComponent(f.event), null, 20000);
      const who = el('input', {value:f.user, placeholder:'Tên đăng nhập', 'aria-label':'Lọc theo tài khoản'});
      const what = el('select', {'aria-label':'Lọc theo hành động'}, el('option', {value:''}, 'Mọi hành động'),
        Object.entries(data.events).map(([k, v]) => el('option', {value:k, selected:k === f.event}, v)));
      const form = el('form', {class:'admin-filter'}, who, what, el('button', {class:'quiet'}, 'Lọc'));
      form.onsubmit = e => { e.preventDefault(); f.user = who.value.trim().toLowerCase(); f.event = what.value; show('audit'); };
      const describe = e => Object.entries(e).filter(([k]) => !['time','event','user'].includes(k)).map(([k, v]) =>
        (DETAIL[k] || k) + ': ' + (k === 'role' ? ROLE_NAMES[v] || v : k === 'action' ? ACTIONS[v] || v : typeof v === 'object' ? JSON.stringify(v) : v === true ? 'có' : v === false ? 'không' : v)).join(' · ');
      const rows = data.entries.map(e => el('div', {class:'audit-row' + (/fail|locked|paused|rate_limited|error|expired|totp_reset/.test(e.event) ? ' warn' : '')},
        el('span', {class:'audit-time'}, e.time), el('strong', {}, e.user || '—'), el('span', {}, data.events[e.event] || e.event), el('small', {}, describe(e))));
      return el('div', {}, form, el('p', {class:'hint'}, 'Nhật ký ghi ai làm gì và lúc nào; không ghi nội dung ca (đoạn văn, HPO, gene). ' + data.entries.length + ' dòng mới nhất.'),
        rows.length ? rows : el('p', {class:'hint'}, 'Chưa có dòng nào.'));
    },

    async gpu() {
      const g = await api('/api/admin/gpu', null, 20000);
      const row = (k, v) => el('div', {class:'gpu-row'}, el('span', {}, k), el('strong', {}, v));
      const send = (action, question) => () => { if (!question || confirm(question)) act(() => api('/api/admin/gpu', {action}, 30000), () => 'Đã ' + ACTIONS[action] + '.'); };
      return el('div', {},
        row('Model 1', STATE[g.model_state] || g.model_state),
        g.model_error ? row('Lỗi', g.model_error) : null,
        row('Lần nạp gần nhất', g.model_load_seconds != null ? g.model_load_seconds + ' giây' : '—'),
        row('VRAM còn trống', g.free_mib != null ? g.free_mib.toLocaleString('vi-VN') + ' MiB' : 'không đọc được'),
        row('Bác sĩ thao tác trong 10 phút', String(g.doctors_active)),
        row('Tự giải phóng sau', (g.gpu_idle_minutes ?? g.idle_minutes) + ' phút không dùng'),
        row('Cờ tạm dừng (train, AI cục bộ)', g.paused ? 'ĐANG BẬT' : 'tắt'),
        el('div', {class:'account-actions'},
          el('button', {type:'button', class:'quiet', onclick:() => show('gpu')}, 'Làm mới'),
          el('button', {type:'button', class:'quiet', disabled:!['ready','loading'].includes(g.model_state), onclick:send('release', 'Giải phóng GPU ngay? Bác sĩ đang tách đoạn văn sẽ phải thử lại.')}, 'Giải phóng ngay'),
          g.paused ? el('button', {type:'button', onclick:send('resume')}, 'Cho chạy lại')
                   : el('button', {type:'button', class:'quiet', onclick:send('pause', 'Tạm dừng Model 1 và giữ tắt cho đến khi bấm Cho chạy lại? Tra cứu và đối chiếu vẫn chạy.')}, 'Tạm dừng (giữ tắt)')));
    },
  };

  window.openAdmin = () => { if (!$('admin').open) $('admin').showModal(); show(tab); };
  $('admin-close').onclick = () => $('admin').close();
  for (const b of document.querySelectorAll('#admin [data-tab]')) b.onclick = () => show(b.dataset.tab);
})();
