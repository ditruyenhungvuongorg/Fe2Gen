'use strict';
// "Bệnh đúng của ca" (29/10/2026): when Model 2 ranks the wrong disease, the doctor marks the wrong suggestions
// ("Báo sai" on the disease card) and names the right disease next to the results. The whole case snapshot (HPO
// of the case, WES genes, the AI's top suggestions, the flagged ones, the right disease) is stored on the server
// as training data for Model 2; nothing changes in the ranking at runtime. Uses $, api, result, idle, markActivity
// and groupTitle from app.js. Panel #disease-feedback lives in index.html, inside the results area after #pattern.
(() => {
  const MAX_WRONG = 10, MAX_SUGGESTIONS = 10, MAX_HPOS = 100, MAX_GENES = 50;
  const wrong = new Set();   // disease ids the doctor flagged on the cards
  let chosen = null;         // {disease_id: str|null, disease_name}
  let timer, seq = 0, sending = false, controller, lastQuery = '';

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v; else if (k.startsWith('on')) node[k] = v; else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function say(error = '', ok = '') {
    $('dfb-error').textContent = error; $('dfb-error').hidden = !error;
    $('dfb-ok').textContent = ok; $('dfb-ok').hidden = !ok;
  }
  const group = id => id && typeof groupTitle === 'function' ? groupTitle(id) : '';
  const order = c => Number.isInteger(c.overall_rank) ? c.overall_rank : Number.isInteger(c.rank) ? c.rank : 999;
  const candidates = () => (typeof result !== 'undefined' && result && Array.isArray(result.candidates)) ? result.candidates : [];
  const nameOf = id => { const c = candidates().find(x => x.disease_id === id); return c ? c.disease_name : id; };

  // The AI's suggestions as shown: the 10 best by overall rank. A flagged disease outside that top 10 takes the
  // place of the last ones, so every wrong id is among the suggestions the server keeps.
  function suggestions() {
    const sorted = [...candidates()].sort((a, b) => order(a) - order(b));
    const flagged = sorted.filter(c => wrong.has(c.disease_id)).slice(0, MAX_SUGGESTIONS);
    const picked = [...flagged];
    for (const c of sorted) { if (picked.length >= MAX_SUGGESTIONS) break; if (!picked.includes(c)) picked.push(c); }
    return picked.sort((a, b) => order(a) - order(b)).map(c => ({
      disease_id:c.disease_id, disease_name:String(c.disease_name || '').slice(0, 200),
      rank:Number.isInteger(c.rank) ? c.rank : null, overall_rank:Number.isInteger(c.overall_rank) ? c.overall_rank : null,
      group:typeof c.group === 'string' ? c.group : null, match_percentage:Number.isFinite(c.match_percentage) ? c.match_percentage : null}));
  }
  function genes() {
    const text = (typeof result !== 'undefined' && result && typeof result.wesText === 'string') ? result.wesText : '';
    const out = [];
    for (const g of text.split(/[\s,;]+/)) {
      const s = g.trim().toUpperCase();
      if (s && s.length <= 40 && !out.includes(s)) out.push(s);
      if (out.length >= MAX_GENES) break;
    }
    return out;
  }

  // Cards: "Báo sai" buttons (app.js draws them with data-disease-id) follow the wrong set.
  function syncCards() {
    for (const b of document.querySelectorAll('#results .disease-wrong[data-disease-id]')) {
      const on = wrong.has(b.dataset.diseaseId);
      b.textContent = on ? 'Đã báo sai · bỏ' : 'Báo sai';
      b.setAttribute('aria-pressed', String(on));
      b.closest('.disease')?.classList.toggle('is-wrong', on);
    }
  }
  function renderWrong() {
    const line = $('dfb-wrong');
    line.replaceChildren();
    if (!wrong.size) { line.append('Chưa đánh dấu bệnh nào.'); return; }
    line.append('Đã báo sai: ');
    [...wrong].forEach((id, i) => line.append(i ? ', ' : '', el('strong', {}, nameOf(id)), el('small', {}, ' (' + id + ')')));
  }
  window.isWrongDisease = id => wrong.has(id);
  window.toggleWrongDisease = c => {
    if (!c || typeof c.disease_id !== 'string') return;
    say('');
    if (wrong.has(c.disease_id)) wrong.delete(c.disease_id);
    else if (wrong.size >= MAX_WRONG) { say('Tối đa ' + MAX_WRONG + ' bệnh báo sai cho một ca.'); return; }
    else wrong.add(c.disease_id);
    window.renderDiseaseFeedback();  // syncs the cards and shows the panel again when a result is on screen
  };

  function showChosen() {
    const box = $('dfb-chosen');
    box.replaceChildren();
    if (!chosen) { box.hidden = true; $('dfb-submit').disabled = true; return; }
    box.hidden = false;
    box.append(el('span', {class:'pill green'}, 'Bệnh đúng: ' + chosen.disease_name + (chosen.disease_id ? ' · ' + chosen.disease_id : ' (ngoài danh mục)')),
      el('button', {type:'button', class:'quiet', onclick:() => { chosen = null; showChosen(); $('dfb-query').focus(); }}, 'Bỏ chọn'));
    $('dfb-submit').disabled = sending;
  }
  function renderSearch(items) {
    const box = $('dfb-results');
    box.replaceChildren();
    for (const d of items) {
      if (!d || typeof d.disease_id !== 'string' || !d.disease_id) continue;
      const on = chosen && chosen.disease_id === d.disease_id;
      const b = el('button', {type:'button', class:'search-item' + (on ? ' is-selected' : '')},
        el('span', {}, el('strong', {}, d.disease_name || d.disease_id), el('small', {}, d.disease_id + (group(d.group) ? ' · ' + group(d.group) : ''))),
        el('span', {class:'selection-action'}, on ? 'Đang chọn' : '+ Chọn bệnh này'));
      b.onclick = () => { chosen = {disease_id:d.disease_id, disease_name:String(d.disease_name || d.disease_id).slice(0, 200)}; showChosen(); renderSearch(items); say(''); };
      box.append(b);
    }
  }
  function showManual(show) {
    const b = $('dfb-manual');
    b.hidden = !show;
    if (show) b.textContent = 'Ghi tay tên bệnh này: “' + lastQuery + '”';
  }
  $('dfb-manual').onclick = () => {
    const name = $('dfb-query').value.trim().slice(0, 200);
    if (!name) return;
    chosen = {disease_id:null, disease_name:name}; showChosen(); say('');
  };
  $('dfb-query').oninput = () => {
    clearTimeout(timer); controller?.abort('superseded');
    const mine = ++seq, q = $('dfb-query').value.trim();
    lastQuery = q; $('dfb-results').replaceChildren(); showManual(false);
    $('dfb-search-status').textContent = q ? 'Đang tìm bệnh…' : '';
    if (!q) return;
    timer = setTimeout(async () => {
      controller = new AbortController();
      try {
        const items = await api('/api/disease_search?q=' + encodeURIComponent(q.slice(0, 200)), null, 20000, controller);
        if (mine !== seq) return;
        const list = Array.isArray(items) ? items : [];
        renderSearch(list);
        $('dfb-search-status').textContent = list.length ? list.length + ' gợi ý · Chọn bệnh đúng của ca.' : 'Không có trong danh mục. Bạn có thể ghi tay tên bệnh.';
        showManual(!list.length);
      } catch (e) { if (mine === seq) $('dfb-search-status').textContent = e.message; }
    }, 250);
  };

  function clearFields() {
    clearTimeout(timer); controller?.abort('closed'); seq++;
    chosen = null; sending = false; lastQuery = '';
    $('dfb-query').value = ''; $('dfb-note').value = '';
    $('dfb-results').replaceChildren(); $('dfb-search-status').textContent = ''; showManual(false);
    showChosen(); $('dfb-submit').textContent = 'Lưu bệnh đúng';
  }

  async function submit() {
    if (sending || !chosen) return;
    const snapshot = typeof result !== 'undefined' ? result : null;
    if (!snapshot) return say('Hãy đối chiếu bệnh trước khi ghi bệnh đúng.');
    if (!Array.isArray(snapshot.hpos) || !snapshot.hpos.length) return say('Chọn ít nhất một HPO của ca.');
    const hpos = snapshot.hpos.slice(0, MAX_HPOS).map(h => ({id:h.id, vi:String(h.vi || '').slice(0, 200), en:String(h.en || '').slice(0, 200), status:h.status}));
    const sugg = suggestions(), ids = new Set(sugg.map(s => s.disease_id));
    const body = {case_code:$('case-id').value.trim().slice(0, 80), hpos, genes:genes(), suggestions:sugg,
                  wrong_ids:[...wrong].filter(id => ids.has(id)).slice(0, MAX_WRONG),
                  correct:{disease_id:chosen.disease_id, disease_name:chosen.disease_name}, note:$('dfb-note').value.trim().slice(0, 500)};
    say(''); sending = true; $('dfb-submit').disabled = true; $('dfb-submit').textContent = 'Đang lưu…'; idle.busy++;
    try {
      await api('/api/disease_feedback', body, 30000);
      wrong.clear(); clearFields(); syncCards(); renderWrong();
      say('', 'Đã lưu bệnh đúng của ca. Dữ liệu chờ huấn luyện lại Model 2.');
    } catch (e) { say(e.message); }
    finally { sending = false; $('dfb-submit').textContent = 'Lưu bệnh đúng'; $('dfb-submit').disabled = !chosen; idle.busy--; markActivity(); }
  }
  $('dfb-submit').onclick = submit;

  // After renderResults(): the panel shows while a matching result exists.
  window.renderDiseaseFeedback = () => {
    const has = !!candidates().length;
    $('disease-feedback').hidden = !has;
    if (!has) wrong.clear();  // no result on screen: nothing can stay flagged
    else for (const id of [...wrong]) if (!candidates().some(c => c.disease_id === id)) wrong.delete(id);
    syncCards(); renderWrong();
  };
  // New matching, new case, sign-in or sign-out: the flags and the draft belong to the previous result or person.
  // The panel hides; "Báo sai" on a card calls renderDiseaseFeedback(), which shows it again while a result exists.
  window.resetDiseaseFeedback = () => {
    wrong.clear(); clearFields(); say(''); syncCards(); renderWrong();
    $('disease-feedback').hidden = true;
  };
  showChosen(); renderWrong();
})();
