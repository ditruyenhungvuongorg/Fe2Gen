'use strict';
// Admin page: accounts, open sessions, audit log, GPU. Uses $, api, user and ROLE_NAMES from app.js.
(() => {
  const STATE = {stopped:'Nghỉ (không chiếm GPU)', loading:'Đang khởi động', ready:'Sẵn sàng (đang chiếm GPU)', paused:'Tạm dừng', failed:'Chưa chạy được'};
  const DETAIL = {target:'tài khoản', role:'vai', changes:'thay đổi', chars:'số ký tự', findings:'số cụm', seconds:'giây',
                  hpo_count:'số HPO', wes:'có gene WES', action:'thao tác', count:'số phiên',
                  correction_id:'góp ý', report_id:'báo lỗi', images:'số ảnh', status:'trạng thái', feedback_id:'ghi nhận'};
  // The same key means something else in some events: corrections_exported counts rows, not sessions.
  const EVENT_DETAIL = {corrections_exported:{count:'số dòng'}, disease_feedback_exported:{count:'số ca'}};
  const ACTIONS = {pause:'tạm dừng', resume:'cho chạy lại', release:'giải phóng ngay'};
  const CORR_LABELS = {pending:'Chờ duyệt', approved:'Đã duyệt', rejected:'Đã từ chối'};
  const CORR_PILL = {pending:'pending', approved:'active', rejected:'locked'};
  const CORR_SOURCE = {text:'từ đoạn văn', search:'từ tìm HPO', selected:'từ HPO của ca'};
  const BUG_STATUS = {new:'Mới', closed:'Đã xử lý'};
  const DFB_STATUS = {new:'Chờ xem', reviewed:'Đã xem'};
  const HPO_STATUS_PILL = {'CÓ':'green', 'NGHI NGỜ':'amber', 'KHÔNG':'gray'};
  const IMAGE_EXT = {'image/png':'png', 'image/jpeg':'jpg', 'image/webp':'webp'};
  const termLine = t => t && t.id ? (t.vi || t.en || '') + ' · ' + t.id : '';
  const kb = bytes => Math.max(1, Math.round((Number(bytes) || 0) / 1024)) + ' kB';
  const readDataUrl = blob => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || '')); r.onerror = () => reject(Error('Không đọc được ảnh.'));
    r.readAsDataURL(blob);
  });
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
            u.totp_enabled === false || self ? null : el('button', {type:'button', class:'quiet', onclick:() => {  // the server refuses resetting one's own 2FA
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
        ((EVENT_DETAIL[e.event] || {})[k] || DETAIL[k] || k) + ': ' + (k === 'role' ? ROLE_NAMES[v] || v : k === 'action' ? ACTIONS[v] || v : k === 'status' ? (e.event.startsWith('disease_feedback') ? DFB_STATUS[v] || v : BUG_STATUS[v] || CORR_LABELS[v] || v) : typeof v === 'object' ? JSON.stringify(v) : v === true ? 'có' : v === false ? 'không' : v)).join(' · ');
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

    // "Sửa sai": doctors' corrections (phrase → right HPO). Approving makes the phrase map to that code for everyone at once.
    async corrections() {
      const f = VIEWS.correctionsFilter || (VIEWS.correctionsFilter = {status:'pending', user:''});
      const data = await api('/api/admin/corrections?status=' + encodeURIComponent(f.status) + '&user=' + encodeURIComponent(f.user), null, 20000);
      const labels = {...CORR_LABELS, ...(data.labels || {})}, counts = data.counts || {}, items = Array.isArray(data.items) ? data.items : [];
      const statusSelect = el('select', {'aria-label':'Lọc theo trạng thái'},
        [['pending', 'Chờ duyệt'], ['approved', 'Đã duyệt'], ['rejected', 'Đã từ chối'], ['all', 'Tất cả']].map(([k, v]) => el('option', {value:k, selected:k === f.status}, v)));
      const who = el('input', {value:f.user, placeholder:'Tên đăng nhập', 'aria-label':'Lọc theo tài khoản', autocapitalize:'none', spellcheck:'false'});
      const form = el('form', {class:'admin-filter'}, statusSelect, who, el('button', {class:'quiet'}, 'Lọc'));
      form.onsubmit = e => { e.preventDefault(); f.status = statusSelect.value; f.user = who.value.trim().toLowerCase(); show('corrections'); };
      const head = el('div', {class:'admin-head'},
        el('p', {class:'hint'}, 'Chờ duyệt ' + (counts.pending || 0) + ' · Đã duyệt ' + (counts.approved || 0) + ' · Đã từ chối ' + (counts.rejected || 0)),
        el('button', {type:'button', class:'quiet', title:'Tệp JSON các góp ý đã duyệt: dòng memory và cụm từ bác sĩ cho lần huấn luyện sau',
          onclick:() => act(() => window.apiDownload('/api/admin/corrections/export', 'fe2gen_gop_y.json'), () => 'Đã tải tệp góp ý đã duyệt.')}, 'Tải dữ liệu huấn luyện'));
      const decide = (it, decision) => {
        const question = decision === 'approved'
          ? 'Duyệt góp ý này? Từ giờ cụm “' + it.phrase + '” sẽ ra mã ' + termLine(it.right) + ' cho mọi bác sĩ.'
          : 'Từ chối góp ý này? Cụm “' + it.phrase + '” giữ cách tách hiện tại.';
        if (!confirm(question)) return;
        act(() => api('/api/admin/corrections/decide', {id:it.id, decision}, 20000), () => decision === 'approved' ? 'Đã duyệt góp ý.' : 'Đã từ chối góp ý.');
      };
      const rows = items.map(it => el('div', {class:'corr-row'},
        el('div', {class:'admin-who'}, el('strong', {}, it.name || it.user), el('small', {}, it.user + ' · ' + it.time + (CORR_SOURCE[it.source] ? ' · ' + CORR_SOURCE[it.source] : ''))),
        el('div', {class:'corr-body'},
          el('blockquote', {class:'corr-phrase'}, '“' + it.phrase + '”'),
          el('p', {class:'corr-map'},
            el('span', {class:'corr-wrong'}, Array.isArray(it.wrong) && it.wrong.length ? it.wrong.map(termLine).join(', ') : 'Model chưa đề xuất mã'),
            ' → ', el('strong', {}, termLine(it.right))),
          it.context ? el('small', {}, 'Ngữ cảnh: …' + it.context + '…') : null,
          it.note ? el('small', {}, 'Ghi chú: ' + it.note) : null,
          it.decided_by ? el('small', {}, (labels[it.status] || it.status) + ' bởi ' + it.decided_by + (it.decided_at ? ' · ' + it.decided_at : '') + (it.decision_note ? ' · ' + it.decision_note : '')) : null),
        el('span', {class:'state-pill ' + (CORR_PILL[it.status] || 'pending')}, labels[it.status] || it.status),
        el('div', {class:'admin-buttons'},
          it.status === 'approved' ? null : el('button', {type:'button', onclick:() => decide(it, 'approved')}, 'Duyệt'),
          it.status === 'rejected' ? null : el('button', {type:'button', class:'quiet danger', onclick:() => decide(it, 'rejected')}, 'Từ chối'))));
      return el('div', {}, form, head,
        el('p', {class:'hint'}, 'Bác sĩ báo mã sai cho một cụm và nêu mã đúng. Duyệt xong, cụm đó ra mã đúng ngay cho mọi người, không cần huấn luyện lại. ' + items.length + ' góp ý đang hiện.'),
        rows.length ? rows : el('p', {class:'hint'}, 'Không có góp ý nào trong bộ lọc này.'));
    },

    // "Báo lỗi": reports with screenshots, stored on the server. Images are fetched with the bearer header.
    async bugs() {
      const f = VIEWS.bugsFilter || (VIEWS.bugsFilter = {status:'new'});
      const data = await api('/api/admin/bug_reports?status=' + encodeURIComponent(f.status), null, 20000);
      const counts = data.counts || {}, items = Array.isArray(data.items) ? data.items : [];
      const statusSelect = el('select', {'aria-label':'Lọc theo trạng thái'},
        [['new', 'Mới'], ['closed', 'Đã xử lý'], ['all', 'Tất cả']].map(([k, v]) => el('option', {value:k, selected:k === f.status}, v)));
      const form = el('form', {class:'admin-filter bug-filter'}, statusSelect, el('button', {class:'quiet'}, 'Lọc'));
      form.onsubmit = e => { e.preventDefault(); f.status = statusSelect.value; show('bugs'); };
      const head = el('p', {class:'hint'}, 'Mới ' + (counts.new || 0) + ' · Đã xử lý ' + (counts.closed || 0) + ' · ' + items.length + ' báo lỗi đang hiện.');
      const setStatus = (r, status) => act(() => api('/api/admin/bug_reports/status', {id:r.id, status}, 20000), () => status === 'closed' ? 'Đã đánh dấu báo lỗi là đã xử lý.' : 'Đã mở lại báo lỗi.');
      const rows = items.map(r => {
        const ci = r.client_info && typeof r.client_info === 'object' ? r.client_info : {};
        const info = [ci.model_version ? 'model ' + ci.model_version : '', ci.app_version ? 'trang ' + ci.app_version : '', ci.viewport ? 'màn hình ' + ci.viewport : '', ci.user_agent || ''].filter(Boolean).join(' · ');
        const preview = el('div', {class:'bug-preview'});
        const imageButtons = (Array.isArray(r.images) ? r.images : []).flatMap((im, i) => {
          const n = Number.isInteger(im.n) ? im.n : i, path = '/api/admin/bug_reports/image?id=' + encodeURIComponent(r.id) + '&n=' + n;
          const holder = el('div', {class:'bug-preview-item'}); preview.append(holder);
          const view = el('button', {type:'button', class:'quiet', onclick:async () => {
            if (holder.children.length) { holder.replaceChildren(); view.textContent = 'Xem ảnh ' + (n + 1) + ' (' + kb(im.bytes) + ')'; return; }
            view.disabled = true; status('');
            try {
              const url = await readDataUrl(await window.apiBlob(path));
              holder.replaceChildren(el('img', {class:'bug-thumb', src:url, alt:'Ảnh ' + (n + 1) + ' của báo lỗi'}));
              view.textContent = 'Ẩn ảnh ' + (n + 1);
            } catch (e) { status(e.message, true); }
            finally { view.disabled = false; }
          }}, 'Xem ảnh ' + (n + 1) + ' (' + kb(im.bytes) + ')');
          const download = el('button', {type:'button', class:'quiet', onclick:() => {
            status('');
            window.apiDownload(path, r.id + '_' + n + '.' + (IMAGE_EXT[im.type] || 'bin')).catch(e => status(e.message, true));
          }}, 'Tải ảnh');
          return [view, download];
        });
        return el('div', {class:'bug-row' + (r.status === 'new' ? ' is-new' : '')},
          el('div', {class:'bug-head'}, el('span', {class:'audit-time'}, r.time), el('strong', {}, r.name || r.user),
            el('small', {}, r.user + ' · ' + (ROLE_NAMES[r.role] || r.role || '') + (r.page ? ' · ' + r.page : '')),
            el('span', {class:'state-pill ' + (r.status === 'new' ? 'pending' : 'active')}, BUG_STATUS[r.status] || r.status),
            r.closed_by ? el('small', {}, 'xử lý bởi ' + r.closed_by + (r.closed_at ? ' · ' + r.closed_at : '')) : null),
          el('pre', {class:'bug-text'}, r.text || ''),
          info ? el('small', {class:'bug-client'}, info) : null,
          imageButtons.length ? el('div', {class:'bug-images'}, imageButtons) : el('small', {class:'bug-client'}, 'Không có ảnh đính kèm.'),
          el('div', {class:'admin-buttons'},
            r.status === 'closed' ? el('button', {type:'button', class:'quiet', onclick:() => setStatus(r, 'new')}, 'Mở lại')
                                  : el('button', {type:'button', onclick:() => setStatus(r, 'closed')}, 'Đã xử lý'),
            el('button', {type:'button', class:'quiet danger', onclick:() => {
              if (!confirm('Xóa báo lỗi này và các ảnh đính kèm?')) return;
              act(() => api('/api/admin/bug_reports/delete', {id:r.id}, 20000), () => 'Đã xóa báo lỗi.');
            }}, 'Xóa')),
          preview);
      });
      return el('div', {}, form, head, rows.length ? rows : el('p', {class:'hint'}, 'Không có báo lỗi nào trong bộ lọc này.'));
    },

    // "Bệnh đúng": the doctor's correct disease for a case next to Model 2's wrong suggestions, with the case's HPO
    // and genes. No approval: the admin reads, marks "Đã xem" and exports the whole set for retraining Model 2.
    async diseases() {
      const f = VIEWS.diseasesFilter || (VIEWS.diseasesFilter = {status:'new', user:''});
      const data = await api('/api/admin/disease_feedback?status=' + encodeURIComponent(f.status) + '&user=' + encodeURIComponent(f.user), null, 20000);
      const labels = {...DFB_STATUS, ...(data.labels || {})}, counts = data.counts || {}, items = Array.isArray(data.items) ? data.items : [];
      const statusSelect = el('select', {'aria-label':'Lọc theo trạng thái'},
        [['new', 'Chờ xem'], ['reviewed', 'Đã xem'], ['all', 'Tất cả']].map(([k, v]) => el('option', {value:k, selected:k === f.status}, v)));
      const who = el('input', {value:f.user, placeholder:'Tên đăng nhập', 'aria-label':'Lọc theo tài khoản', autocapitalize:'none', spellcheck:'false'});
      const form = el('form', {class:'admin-filter'}, statusSelect, who, el('button', {class:'quiet'}, 'Lọc'));
      form.onsubmit = e => { e.preventDefault(); f.status = statusSelect.value; f.user = who.value.trim().toLowerCase(); show('diseases'); };
      const head = el('div', {class:'admin-head'},
        el('p', {class:'hint'}, 'Chờ xem ' + (counts.new || 0) + ' · Đã xem ' + (counts.reviewed || 0) + ' · ' + items.length + ' ghi nhận đang hiện.'),
        el('button', {type:'button', class:'quiet', title:'Tệp JSON mọi ghi nhận (kể cả đã xem): HPO của ca, gene, gợi ý của AI và bệnh đúng, cho lần huấn luyện lại Model 2',
          onclick:() => act(() => window.apiDownload('/api/admin/disease_feedback/export', 'fe2gen_benh_dung.json'), () => 'Đã tải dữ liệu huấn luyện Model 2.')}, 'Tải dữ liệu huấn luyện Model 2'));
      const setStatus = (it, status) => act(() => api('/api/admin/disease_feedback/status', {id:it.id, status}, 20000), () => status === 'reviewed' ? 'Đã đánh dấu ghi nhận là đã xem.' : 'Đã mở lại ghi nhận.');
      const diseaseLine = d => d && d.disease_id ? (d.disease_name || '') + ' · ' + d.disease_id : (d && d.disease_name ? d.disease_name + ' (ngoài danh mục)' : '—');
      const rows = items.map(it => {
        const hpos = Array.isArray(it.hpos) ? it.hpos : [], genes = Array.isArray(it.genes) ? it.genes : [];
        const sugg = (Array.isArray(it.suggestions) ? it.suggestions : []).slice().sort((a, b) => (a.overall_rank ?? a.rank ?? 999) - (b.overall_rank ?? b.rank ?? 999));
        const wrongIds = Array.isArray(it.wrong_ids) ? it.wrong_ids : [];
        const nameOf = id => { const s = sugg.find(x => x.disease_id === id); return s && s.disease_name ? s.disease_name + ' (' + id + ')' : id; };
        return el('div', {class:'dfb-row' + (it.status === 'new' ? ' is-new' : '')},
          el('div', {class:'bug-head'}, el('span', {class:'audit-time'}, it.time), el('strong', {}, it.name || it.user),
            el('small', {}, it.user + (it.case_code ? ' · mã ca ' + it.case_code : '')),
            el('span', {class:'state-pill ' + (it.status === 'new' ? 'pending' : 'active')}, labels[it.status] || it.status),
            it.reviewed_by ? el('small', {}, 'xem bởi ' + it.reviewed_by + (it.reviewed_at ? ' · ' + it.reviewed_at : '')) : null),
          el('div', {class:'dfb-body'},
            el('details', {class:'dfb-hpos'}, el('summary', {}, 'HPO của ca: ' + hpos.length + (genes.length ? ' · gene WES: ' + genes.length : '')),
              el('div', {}, hpos.map(h => el('span', {class:'pill ' + (HPO_STATUS_PILL[h.status] || 'gray')}, (h.vi || h.en || '') + ' · ' + h.id + ' · ' + (h.status || '')))),
              genes.length ? el('p', {class:'dfb-genes'}, 'Gene WES: ' + genes.join(', ')) : null),
            el('p', {}, el('span', {class:'dfb-label'}, 'AI gợi ý: '), sugg.length ? sugg.slice(0, 3).map((s, i) => (i ? ' · ' : '') + (i + 1) + '. ' + (s.disease_name || '') + ' (' + s.disease_id + ')').join('') + (sugg.length > 3 ? ' · …' : '') : '—'),
            el('p', {}, el('span', {class:'dfb-label'}, 'Báo sai: '), wrongIds.length ? el('span', {class:'corr-wrong'}, wrongIds.map(nameOf).join(', ')) : 'không đánh dấu'),
            el('p', {}, el('span', {class:'dfb-label'}, 'Bệnh đúng: '), el('strong', {}, diseaseLine(it.correct))),
            it.note ? el('small', {}, 'Ghi chú: ' + it.note) : null),
          el('div', {class:'admin-buttons'},
            it.status === 'reviewed' ? el('button', {type:'button', class:'quiet', onclick:() => setStatus(it, 'new')}, 'Mở lại')
                                     : el('button', {type:'button', onclick:() => setStatus(it, 'reviewed')}, 'Đã xem'),
            el('button', {type:'button', class:'quiet danger', onclick:() => {
              if (!confirm('Xóa ghi nhận này? Bệnh đúng của ca sẽ không còn trong dữ liệu huấn luyện.')) return;
              act(() => api('/api/admin/disease_feedback/delete', {id:it.id}, 20000), () => 'Đã xóa ghi nhận.');
            }}, 'Xóa')));
      });
      return el('div', {}, form, head,
        el('p', {class:'hint'}, 'Bác sĩ đánh dấu bệnh AI đề xuất sai và ghi bệnh đúng của ca; không thay đổi kết quả đối chiếu hiện tại. Tệp tải về gồm mọi ghi nhận, kể cả đã xem.'),
        rows.length ? rows : el('p', {class:'hint'}, 'Không có ghi nhận nào trong bộ lọc này.'));
    },
  };

  window.openAdmin = () => { if (!$('admin').open) $('admin').showModal(); show(tab); };
  $('admin-close').onclick = () => $('admin').close();
  for (const b of document.querySelectorAll('#admin [data-tab]')) b.onclick = () => show(b.dataset.tab);
})();
