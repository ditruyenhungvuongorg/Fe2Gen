'use strict';
// Cases sent to the genetics lab (26/10/2026). A doctor sends the reviewed case (ultrasound text, HPO, chosen
// diseases, WES genes) and follows its status; lab accounts and admins see every case, set the status and a note.
// The server keeps a case until someone deletes it. Uses $, api, escape, notice, user, result, labIds, selection,
// hasCase, resetCase, renderSelected, updateActions and groupTitle from app.js.
(() => {
  const PILL = {new:'amber', received:'blue', testing:'gray', result:'green'};
  let cases = [], statuses = {}, openId = null;
  const answers = () => ['lab', 'admin'].includes(user?.role);
  const pill = s => '<span class="pill ' + (PILL[s] || 'gray') + '">' + escape(statuses[s] || s) + '</span>';
  const table = (heads, body) => '<table class="lab-table"><thead><tr>' + heads.map(h => '<th>' + escape(h) + '</th>').join('') +
    '</tr></thead><tbody>' + body.map(r => '<tr>' + r.map(v => '<td>' + escape(v) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
  const diseaseRows = c => c.diseases.map(d => [(d.group ? groupTitle(d.group) + ' #' : '') + (d.rank ?? '—'),
    d.disease_name + ' · ' + d.disease_id, d.match_percentage != null ? d.match_percentage + '%' : '—', d.causative_genes.join(', ')]);

  async function refresh() {
    const panel = $('lab-cases-panel');
    if (!user) {
      panel.hidden = true; cases = []; openId = null; $('lab-cases').replaceChildren();
      if ($('lab-case').open) $('lab-case').close();
      return;
    }
    panel.hidden = false;
    $('lab-cases-title').textContent = answers() ? 'Ca gửi Lab' : 'Ca tôi đã gửi Lab';
    try {
      const data = await api('/api/lab/cases', null, 20000);
      cases = data.cases; statuses = data.statuses; render();
      if (openId && $('lab-case').open) { const c = cases.find(x => x.id === openId); if (c) show(c); else $('lab-case').close(); }
    } catch (e) { $('lab-cases').innerHTML = '<p class="hint">' + escape(e.message) + '</p>'; }
  }

  function render() {
    const box = $('lab-cases');
    if (!cases.length) {
      box.innerHTML = '<p class="hint">' + (answers() ? 'Chưa có ca nào được gửi sang Lab.'
        : 'Bạn chưa gửi ca nào. Sau khi đối chiếu và chọn bệnh, bấm "Gửi sang Lab" ở cuối trang.') + '</p>';
      return;
    }
    box.innerHTML = cases.map(c => '<button type="button" class="search-item lab-case-row" data-id="' + escape(c.id) + '"><span><strong>' +
      escape(c.case_id || '(chưa ghi mã ca)') + '</strong><small>' +
      escape([c.gestation, answers() ? 'gửi bởi ' + c.sender_name : '', c.created].filter(Boolean).join(' · ')) +
      (c.note ? '<br>Ghi chú Lab: ' + escape(c.note.slice(0, 140)) : '') + '</small></span>' + pill(c.status) + '</button>').join('');
    for (const b of box.querySelectorAll('.lab-case-row')) b.onclick = () => show(cases.find(c => c.id === b.dataset.id));
  }

  function show(c) {
    openId = c.id;
    const mine = c.sender === user?.username;
    $('lab-case-title').textContent = 'Ca ' + (c.case_id || '(chưa ghi mã ca)');
    $('lab-case-body').innerHTML =
      '<p class="hint">Tuổi thai: ' + escape(c.gestation || '—') + ' · Bác sĩ phụ trách: ' + escape(c.doctor || '—') +
      ' · Gửi bởi ' + escape(c.sender_name) + ' lúc ' + escape(c.created) + '</p>' +
      '<p>Trạng thái: ' + pill(c.status) + (c.status !== 'new' ? ' <small class="hint">' + escape(c.updated_by + ', ' + c.updated) + '</small>' : '') + '</p>' +
      (c.note ? '<p class="lab-note"><strong>Ghi chú của Lab:</strong> ' + escape(c.note) + '</p>' : '') +
      '<h3 class="account-sub">Mô tả siêu âm</h3><p class="lab-text">' + escape(c.text || '(nhập bằng tra cứu HPO, không có đoạn văn)') + '</p>' +
      '<h3 class="account-sub">Kiểu hình đã duyệt (' + c.hpos.length + ')</h3>' +
      table(['Mã HPO', 'Tiếng Việt', 'Tiếng Anh', 'Trạng thái'], c.hpos.map(h => [h.id, h.vi, h.en, h.status])) +
      '<h3 class="account-sub">Bệnh bác sĩ chọn (' + c.diseases.length + ')</h3>' +
      (c.diseases.length ? table(['Hạng', 'Bệnh / mã', 'Tương đồng', 'Gen'], diseaseRows(c)) : '<p class="hint">Không chọn bệnh nào.</p>') +
      (c.wes_genes ? '<h3 class="account-sub">Gene ứng viên từ WES</h3><p class="lab-text">' + escape(c.wes_genes) + '</p>' : '') +
      (answers() ? '<h3 class="account-sub">Cập nhật cho bác sĩ</h3><label>Trạng thái<select id="lab-case-status">' +
        Object.entries(statuses).map(([k, v]) => '<option value="' + escape(k) + '"' + (k === c.status ? ' selected' : '') + '>' + escape(v) + '</option>').join('') +
        '</select></label><label>Ghi chú cho bác sĩ<textarea id="lab-case-note" rows="3" maxlength="2000">' + escape(c.note) + '</textarea></label>' : '') +
      '<p id="lab-case-error" class="settings-error" role="alert" hidden></p><div class="account-actions">' +
      (answers() ? '<button type="button" id="lab-case-save">Lưu cập nhật</button>' : '') +
      '<button type="button" id="lab-case-load" class="quiet">Mở ca để đối chiếu lại</button>' +
      '<button type="button" id="lab-case-sheet" class="quiet">Xem phiếu Lab</button>' +
      (answers() || mine ? '<button type="button" id="lab-case-delete" class="quiet danger">Xoá ca</button>' : '') + '</div>';
    const fail = e => { $('lab-case-error').textContent = e.message; $('lab-case-error').hidden = false; };
    if (answers()) $('lab-case-save').onclick = async () => {
      try {
        await api('/api/lab/cases/update', {id:c.id, status:$('lab-case-status').value, note:$('lab-case-note').value}, 20000);
        notice('Đã cập nhật ca ' + (c.case_id || '') + '; bác sĩ gửi ca sẽ thấy trạng thái và ghi chú này.');
        refresh();
      } catch (e) { fail(e); }
    };
    $('lab-case-load').onclick = () => load(c);
    $('lab-case-sheet').onclick = () => sheet(c);
    const del = $('lab-case-delete');
    if (del) del.onclick = async () => {
      if (!confirm('Xoá vĩnh viễn ca ' + (c.case_id || '') + ' khỏi máy chủ? Không khôi phục được.')) return;
      try { await api('/api/lab/cases/delete', {id:c.id}, 20000); openId = null; $('lab-case').close(); notice('Đã xoá ca khỏi máy chủ.'); refresh(); }
      catch (e) { fail(e); }
    };
    if (!$('lab-case').open) $('lab-case').showModal();
  }

  // Put the case into this tab so the lab (or the doctor) can review it and run the matching again.
  function load(c) {
    if (hasCase() && !confirm('Thay ca đang mở trong tab này bằng ca ' + (c.case_id || '') + '?')) return;
    resetCase();
    $('case-id').value = c.case_id; $('gestation').value = c.gestation; $('doctor').value = c.doctor;
    $('clinical-text').value = c.text; $('wes-genes').value = c.wes_genes;
    for (const h of c.hpos) selection.add({id:h.id, vi:h.vi, en:h.en}, h.status, {kind:'manual'});
    renderSelected(); openId = null; $('lab-case').close();
    notice('Đã mở ca ' + (c.case_id || '') + ' với ' + c.hpos.length + ' HPO. Duyệt lại rồi bấm "Đối chiếu bệnh" để xem kết quả.');
    $('selected').scrollIntoView({behavior:'smooth', block:'center'});
  }

  function sheet(c) {
    $('sheet').innerHTML = '<p style="text-align:center">HỘI CHẨN DI TRUYỀN TIỀN SẢN</p><h1>PHIẾU GỬI LAB DI TRUYỀN</h1><p>Mã ca: ' +
      escape(c.case_id || '……………………') + ' · Tuổi thai: ' + escape(c.gestation || '……………………') + '\nBác sĩ: ' + escape(c.doctor || c.sender_name) +
      '\nNgày gửi: ' + escape(c.created) + ' · Trạng thái: ' + escape(statuses[c.status] || c.status) + '</p>' +
      (c.wes_genes ? '<h2>Gene ứng viên từ WES</h2><p>' + escape(c.wes_genes) + '</p>' : '') +
      '<h2>1. Mô tả siêu âm</h2><p>' + escape(c.text || 'Nhập dấu hiệu bằng tra cứu HPO.') + '</p><h2>2. Kiểu hình đã duyệt</h2>' +
      table(['Mã HPO', 'Tiếng Việt', 'Tiếng Anh', 'Trạng thái'], c.hpos.map(h => [h.id, h.vi, h.en, h.status])) +
      '<h2>3. Bệnh được bác sĩ chọn để hội chẩn</h2>' + table(['Hạng', 'Bệnh / mã', 'Tương đồng', 'Gen'], diseaseRows(c)) +
      '<p>Phần trăm là độ tương đồng kiểu hình, không phải xác suất mắc bệnh.</p>' +
      c.diseases.filter(d => d.recommended_tests).map(d => '<p><strong>' + escape(d.disease_name) + '</strong>\n' + escape(d.recommended_tests) + '</p>').join('') +
      (c.note ? '<h2>Ghi chú của Lab</h2><p>' + escape(c.note) + '</p>' : '') +
      '<div class="signature"><div>Bác sĩ chỉ định<br>(Ký, ghi rõ họ tên)</div><div>Phòng Lab tiếp nhận<br>(Ký, ghi rõ họ tên)</div></div>';
    openId = null; $('lab-case').close(); $('lab').showModal();
  }

  $('send-lab').onclick = async () => {
    if (!result || !labIds.size) return;
    if (!confirm('Gửi ca này sang Phòng Lab? Máy chủ lưu mô tả siêu âm, HPO, bệnh đã chọn và gene WES cho tới khi có người xoá ca.')) return;
    const rows = result.candidates.filter(c => labIds.has(c.disease_id));
    $('send-lab').disabled = true;
    try {
      const data = await api('/api/lab/cases/create', {
        case_id:$('case-id').value, gestation:$('gestation').value, doctor:$('doctor').value, text:result.text || '', wes_genes:result.wesText || '',
        hpos:result.hpos.map(h => ({id:h.id, vi:h.vi, en:h.en, status:h.status})),
        diseases:rows.map(c => ({disease_id:c.disease_id, disease_name:c.disease_name, group:c.group, rank:c.rank, match_percentage:c.match_percentage,
                                 inheritance_modes:c.inheritance_modes, causative_genes:c.causative_genes, recommended_tests:c.model3_recommended_tests}))}, 30000);
      notice('Đã gửi ca ' + (data.case.case_id || '') + ' sang Phòng Lab. Theo dõi trạng thái ở mục "Ca tôi đã gửi Lab" đầu trang.');
      refresh();
    } catch (e) { notice(e.message); }
    finally { updateActions(); }
  };
  $('lab-cases-refresh').onclick = () => refresh();
  $('lab-case-close').onclick = () => { openId = null; $('lab-case').close(); };
  window.labCases = {onUser:refresh};
})();
