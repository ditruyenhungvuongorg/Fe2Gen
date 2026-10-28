'use strict';
// "Báo lỗi" (28/10/2026): any signed-in person describes a problem, optionally with up to 3 screenshots, and the
// report is stored on the server for the admin page (no email). Images are downscaled in the browser (max 1600 px,
// about 1.5 MB each) before they are sent as base64. Uses $, api, sessionInfo, extraction, idle, markActivity
// and accountLocked from app.js. Dialog #bug-report lives in index.html.
(() => {
  const APP_VERSION = '20261028a';
  const MAX_IMAGES = 3, MAX_SIDE = 1600, SOFT_BYTES = 1.5e6;
  const TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
  const EXT = {'image/png':'.png', 'image/jpeg':'.jpg', 'image/webp':'.webp'};
  let images = [];   // [{name, type, data (base64 without prefix), dataUrl, bytes}]
  let sending = false, adding = false, closeTimer;

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v; else if (k.startsWith('on')) node[k] = v; else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function say(error = '', ok = '') {
    $('bug-error').textContent = error; $('bug-error').hidden = !error;
    $('bug-ok').textContent = ok; $('bug-ok').hidden = !ok;
  }
  const base64Bytes = b64 => Math.floor(b64.length * 3 / 4) - (b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0);
  const readDataUrl = file => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || '')); r.onerror = () => reject(Error('Không đọc được tệp ảnh.'));
    r.readAsDataURL(file);
  });
  const loadImage = url => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img); img.onerror = () => reject(Error('Ảnh không hợp lệ hoặc không phải PNG/JPEG/WEBP.'));
    img.src = url;
  });
  function typeOf(file) {
    if (TYPES.has(file.type)) return file.type;
    const m = /\.(png|jpe?g|webp)$/i.exec(file.name || '');
    return m ? {png:'image/png', jpg:'image/jpeg', jpeg:'image/jpeg', webp:'image/webp'}[m[1].toLowerCase()] : '';
  }
  // Downscale to MAX_SIDE on the long side and re-encode: PNG stays PNG unless it is still large, then JPEG 0.85;
  // JPEG and WEBP become JPEG 0.85. Keeps every image under about SOFT_BYTES.
  async function shrink(file) {
    const inType = typeOf(file);
    if (!inType) throw Error('Ảnh không hợp lệ hoặc không phải PNG/JPEG/WEBP.');
    const img = await loadImage(await readDataUrl(file));
    const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth || 1, img.naturalHeight || 1));
    const w = Math.max(1, Math.round((img.naturalWidth || 1) * scale)), h = Math.max(1, Math.round((img.naturalHeight || 1) * scale));
    const canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h;
    const g = canvas.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);  // a transparent PNG turned JPEG gets a white, not black, background
    g.drawImage(img, 0, 0, w, h);
    let type = inType === 'image/png' ? 'image/png' : 'image/jpeg';
    let out = type === 'image/png' ? canvas.toDataURL('image/png') : canvas.toDataURL('image/jpeg', 0.85);
    let data = out.split(',')[1] || '';
    if (type === 'image/png' && base64Bytes(data) > SOFT_BYTES) { type = 'image/jpeg'; out = canvas.toDataURL('image/jpeg', 0.85); data = out.split(',')[1] || ''; }
    for (let q = 0.7; base64Bytes(data) > SOFT_BYTES && q >= 0.4; q -= 0.15) { out = canvas.toDataURL('image/jpeg', q); data = out.split(',')[1] || ''; }
    if (!out.startsWith('data:' + type + ';base64,')) { type = 'image/png'; }  // a browser without JPEG encoding answers PNG
    const stem = String(file.name || 'anh').replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]+/g, '_').trim().slice(0, 100) || 'anh';
    return {name:stem + EXT[type], type, data, dataUrl:out, bytes:base64Bytes(data)};
  }

  function renderThumbs() {
    const box = $('bug-thumbs');
    box.replaceChildren();
    images.forEach((im, i) => box.append(el('div', {class:'bug-thumb-box'},
      el('img', {src:im.dataUrl, alt:'Ảnh ' + (i + 1)}),
      el('small', {title:im.name}, im.name + ' · ' + Math.max(1, Math.round(im.bytes / 1024)) + ' kB'),
      el('button', {type:'button', class:'quiet', 'aria-label':'Bỏ ảnh ' + (i + 1), disabled:sending, onclick:() => { images.splice(i, 1); renderThumbs(); }}, 'Bỏ ảnh'))));
    $('bug-images').disabled = sending || adding || images.length >= MAX_IMAGES;
  }

  $('bug-images').onchange = async () => {
    const input = $('bug-images'), files = [...(input.files || [])];
    input.value = '';
    if (!files.length || sending) return;
    say(''); adding = true; renderThumbs();
    try {
      for (const file of files) {
        if (images.length >= MAX_IMAGES) { say('Tối đa 3 ảnh mỗi báo lỗi.'); break; }
        images.push(await shrink(file));
        renderThumbs();
      }
    } catch (e) { say(e.message); }
    finally { adding = false; renderThumbs(); }
  };

  function clientInfo() {
    let model = '';
    try { model = (typeof extraction !== 'undefined' && extraction && extraction.model_version) || (sessionInfo && sessionInfo.model_version) || ''; } catch {}
    return {model_version:String(model).slice(0, 60), user_agent:String(navigator.userAgent || '').slice(0, 300),
            viewport:(innerWidth + 'x' + innerHeight).slice(0, 40), app_version:APP_VERSION};
  }

  async function submit() {
    if (sending || adding) return;
    const text = $('bug-text').value.trim();
    if (!text) { say('Mô tả lỗi không được để trống.'); $('bug-text').focus(); return; }
    if (images.length > MAX_IMAGES) return say('Tối đa 3 ảnh mỗi báo lỗi.');
    say(''); sending = true; $('bug-submit').disabled = true; $('bug-submit').textContent = 'Đang gửi…'; renderThumbs(); idle.busy++;
    try {
      const body = {text:text.slice(0, 4000), page:(location.pathname + location.hash).slice(0, 200), client_info:clientInfo(),
                    images:images.map(im => ({name:im.name, type:im.type, data:im.data}))};
      await api('/api/bug_reports', body, 180000);
      $('bug-text').value = ''; images = [];
      say('', 'Đã gửi báo lỗi. Cảm ơn bác sĩ.');
      closeTimer = setTimeout(() => { if ($('bug-report').open) $('bug-report').close(); }, 2000);
    } catch (e) { say(e.message); }
    finally { sending = false; $('bug-submit').disabled = false; $('bug-submit').textContent = 'Gửi báo lỗi'; renderThumbs(); idle.busy--; markActivity(); }
  }
  $('bug-submit').onclick = submit;
  $('bug-report-close').onclick = () => $('bug-report').close();
  $('bug-report').onclose = () => { clearTimeout(closeTimer); say(''); };

  // No sign-in guard: in token/none auth modes `user` stays null while the button is visible; the server authorizes
  // and api() surfaces its error in the dialog. Every role, lab included, may report.
  window.openBugReport = () => {
    clearTimeout(closeTimer); say(''); renderThumbs();
    if (!$('bug-report').open) $('bug-report').showModal();
    $('bug-text').focus();
  };
  // Sign-out: the next person at the computer must not see this draft or the screenshots.
  window.resetBugReport = () => {
    clearTimeout(closeTimer); images = []; $('bug-text').value = ''; $('bug-images').value = ''; say(''); renderThumbs();
    if ($('bug-report').open) $('bug-report').close();
  };
  $('bug-report-open').onclick = () => window.openBugReport();
  $('account-report').onclick = () => { if (!accountLocked()) $('account').close(); window.openBugReport(); };
})();
