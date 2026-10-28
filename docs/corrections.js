'use strict';
// "Góp ý sửa mã" (28/10/2026): a doctor reports that Model 1 (or a chosen HPO) is wrong for a phrase and names the
// right code. An admin approves it on the admin page; from then on the phrase maps to the approved code for everyone.
// Uses $, api, user, add, idle, markActivity and sessionInfo from app.js. Dialog #correction lives in index.html.
(() => {
  const LABELS = {pending:'Chờ duyệt', approved:'Đã duyệt', rejected:'Đã từ chối'};
  const PILL = {pending:'pending', approved:'active', rejected:'locked'};
  let ctx = null;        // {phrase, wrong, wrongNames, text, start, status, source}
  let chosenTerm = null; // {id, vi, en}
  let timer, seq = 0, sending = false, closeTimer, controller;

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v; else if (k.startsWith('on')) node[k] = v; else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function say(error = '', ok = '') {
    $('correction-error').textContent = error; $('correction-error').hidden = !error;
    $('correction-ok').textContent = ok; $('correction-ok').hidden = !ok;
  }
  const termLine = t => (t.vi || t.en || '') + ' · ' + t.id;
  // A mention whose status the doctor has not chosen yet (negated, other subject, history) must not land in the case
  // as 'CÓ' by default: the checkbox is then off and disabled, with a hint under it.
  const addHint = el('p', {id:'correction-add-hint', class:'hint', hidden:true}, 'Chọn trạng thái ở cụm này trước khi thêm vào ca.');
  $('correction-add').closest('label').after(addHint);
  function gateAdd(gated) { $('correction-add').disabled = gated; $('correction-add').checked = !gated; addHint.hidden = !gated; }

  function clearFields() {
    clearTimeout(timer); clearTimeout(closeTimer); controller?.abort('closed'); seq++;
    ctx = null; chosenTerm = null; sending = false;
    for (const id of ['correction-query', 'correction-note']) $(id).value = '';
    gateAdd(false);
    $('correction-results').replaceChildren(); $('correction-list').replaceChildren();
    $('correction-search-status').textContent = '';
    $('correction-chosen').hidden = true; $('correction-chosen').replaceChildren();
    $('correction-phrase').textContent = ''; $('correction-wrong').textContent = '';
    $('correction-submit').disabled = true; $('correction-mine').disabled = false;
    say('');
  }

  function showChosen() {
    const box = $('correction-chosen');
    box.replaceChildren();
    if (!chosenTerm) { box.hidden = true; $('correction-submit').disabled = true; return; }
    box.hidden = false;
    box.append(el('span', {class:'pill green'}, 'Mã đúng: ' + termLine(chosenTerm)),
      el('button', {type:'button', class:'quiet', onclick:() => { chosenTerm = null; showChosen(); $('correction-query').focus(); }}, 'Bỏ chọn'));
    $('correction-submit').disabled = sending;
  }

  function renderResults(terms) {
    const box = $('correction-results');
    box.replaceChildren();
    for (const term of terms) {
      if (!term || !/^HP:\d{7}$/.test(term.id)) continue;
      const b = el('button', {type:'button', class:'search-item' + (chosenTerm && chosenTerm.id === term.id ? ' is-selected' : '')},
        el('span', {}, el('strong', {}, term.vi || term.en || ''), el('small', {}, (term.en || '') + ' · ' + term.id)),
        el('span', {class:'selection-action'}, chosenTerm && chosenTerm.id === term.id ? 'Đang chọn' : '+ Chọn mã này'));
      b.onclick = () => { chosenTerm = {id:term.id, vi:term.vi || '', en:term.en || ''}; showChosen(); renderResults(terms); say(''); };
      box.append(b);
    }
  }

  $('correction-query').oninput = () => {
    clearTimeout(timer); controller?.abort('superseded');
    const mine = ++seq, q = $('correction-query').value.trim();
    $('correction-results').replaceChildren();
    $('correction-search-status').textContent = q ? 'Đang tìm thuật ngữ…' : '';
    if (!q) return;
    timer = setTimeout(async () => {
      controller = new AbortController();
      try {
        const terms = await api('/api/hpo_search?q=' + encodeURIComponent(q), null, 20000, controller);
        if (mine !== seq) return;
        renderResults(Array.isArray(terms) ? terms : []);
        $('correction-search-status').textContent = terms.length ? terms.length + ' gợi ý · Chọn mã đúng cho cụm này.' : 'Không tìm thấy. Thử cụm ngắn hơn hoặc mã HPO.';
      } catch (e) { if (mine === seq) $('correction-search-status').textContent = e.message; }
    }, 250);
  };

  function contextOf(c) {
    if (!c || typeof c.text !== 'string' || !c.text || !Number.isInteger(c.start) || c.start < 0) return '';
    const pad = Math.min(120, Math.max(0, Math.floor((400 - c.phrase.length) / 2)));
    return c.text.slice(Math.max(0, c.start - pad), c.start + c.phrase.length + pad).slice(0, 400);
  }

  async function submit() {
    if (!ctx || !chosenTerm || sending) return;
    // Snapshots: closing the dialog (Esc, session end) while the request is in flight nulls ctx via clearFields.
    const c = ctx, term = chosenTerm;
    const addToCase = $('correction-add').checked && !$('correction-add').disabled;
    say(''); sending = true; $('correction-submit').disabled = true; idle.busy++;
    try {
      const body = {phrase:c.phrase, right_id:term.id, wrong_ids:c.wrong.slice(0, 5), context:contextOf(c),
                    note:$('correction-note').value.trim().slice(0, 500), source:c.source};
      const data = await api('/api/corrections', body, 30000);
      if (ctx !== c) return;  // dialog closed or reopened for another phrase meanwhile: the server already stored the item
      if (addToCase) {
        const source = c.text && Number.isInteger(c.start) ? {kind:'text', text:c.text, phrase:c.phrase, start:c.start} : {kind:'manual'};
        try { add(term, c.status || 'CÓ', source); } catch (e) { console.warn(e); }
      }
      say('', data && data.duplicate ? 'Bác sĩ đã gửi góp ý này trước đó; đang chờ quản trị duyệt.'
                                     : 'Đã gửi góp ý. Quản trị duyệt xong thì cụm này sẽ ra đúng mã cho mọi bác sĩ.');
      closeTimer = setTimeout(() => { if ($('correction').open) $('correction').close(); }, 1500);
    } catch (e) {
      if (ctx !== c) return;
      say(e.message); $('correction-submit').disabled = !chosenTerm;  // after success the button stays off
    } finally { if (ctx === c) sending = false; idle.busy--; markActivity(); }
  }
  $('correction-submit').onclick = submit;

  async function loadMine() {
    const list = $('correction-list');
    if (list.children.length) { list.replaceChildren(); return; }  // second press hides the list
    list.replaceChildren(el('p', {class:'hint'}, 'Đang tải…'));
    $('correction-mine').disabled = true;
    try {
      const data = await api('/api/corrections?status=all', null, 20000);
      const labels = data.labels || LABELS, items = Array.isArray(data.items) ? data.items : [];
      list.replaceChildren(el('div', {class:'correction-list'},
        el('p', {class:'hint'}, items.length ? items.length + ' góp ý của bác sĩ, mới nhất trước.' : 'Bác sĩ chưa gửi góp ý nào.'),
        items.map(it => el('div', {class:'correction-item'},
          el('span', {}, '“' + it.phrase + '” → ' + (it.right ? termLine(it.right) : ''), el('small', {}, it.time || '')),
          el('span', {class:'state-pill ' + (PILL[it.status] || 'pending')}, labels[it.status] || it.status)))));
    } catch (e) { list.replaceChildren(); say(e.message); }
    finally { $('correction-mine').disabled = false; }
  }
  $('correction-mine').onclick = loadMine;
  $('correction-close').onclick = () => $('correction').close();
  $('correction').onclose = clearFields;

  // opts: {phrase, wrong: [ids], wrongNames: [vi names], text, start, status, source: 'text' | 'selected'}
  window.openCorrection = opts => {
    if (!opts || typeof opts.phrase !== 'string' || !opts.phrase.trim() || (user && user.role === 'lab')) return;
    clearFields();
    const wrong = (Array.isArray(opts.wrong) ? opts.wrong : []).filter(id => /^HP:\d{7}$/.test(id)).slice(0, 5);
    const names = Array.isArray(opts.wrongNames) ? opts.wrongNames : [];
    ctx = {phrase:opts.phrase.trim().slice(0, 200), wrong, wrongNames:names, text:typeof opts.text === 'string' ? opts.text : '',
           start:Number.isInteger(opts.start) ? opts.start : null, status:['CÓ', 'NGHI NGỜ', 'KHÔNG'].includes(opts.status) ? opts.status : '',
           source:opts.source === 'selected' ? 'selected' : 'text'};
    gateAdd(ctx.source === 'text' && ctx.status === '');
    $('correction-phrase').textContent = '“' + ctx.phrase + '”';
    $('correction-wrong').textContent = wrong.length
      ? 'Mã model đề xuất: ' + wrong.map((id, i) => (names[i] ? names[i] + ' · ' : '') + id).join(', ')
      : 'Model chưa đề xuất mã nào.';
    if (!$('correction').open) $('correction').showModal();
    $('correction-query').focus();
  };
  // A new sign-in or sign-out must not see the previous person's phrase or draft note.
  window.resetCorrection = () => { if ($('correction').open) $('correction').close(); else clearFields(); };
})();
