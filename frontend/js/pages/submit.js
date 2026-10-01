/* 作业提交 Submit — 模板（新建/复制/编辑/删除/套用）+ 字段级校验 */
Components.init('submit');
const C = Components;

let SAMPLES = [];
let TEMPLATES = [];
let CURRENT_TPL_ID = null;   // 正在编辑的模板；套用后手动改表单不影响该模板

const FIELD_IDS = ['name', 'mapper', 'reducer', 'num_map_tasks', 'num_reduce_tasks',
  'input_rows', 'params.pattern'];

async function init() {
  const funcs = await API.get('/api/functions');
  SAMPLES = await API.get('/api/samples');

  fillSelect('mapper', funcs.mappers);
  fillSelect('reducer', funcs.reducers);

  const preset = document.getElementById('preset');
  preset.innerHTML = SAMPLES.map(s => `<option value="${s.name}">${C.esc(s.name)}</option>`).join('');
  preset.addEventListener('change', () => {
    const s = SAMPLES.find(x => x.name === preset.value);
    if (s) { fillFromSample(s); clearIssues(); }
  });

  document.getElementById('form').addEventListener('submit', onSubmit);
  document.getElementById('btn-validate').addEventListener('click', () => validate(false));

  document.getElementById('tpl-apply').addEventListener('click', onApply);
  document.getElementById('tpl-save-new').addEventListener('click', onSaveAsNew);
  document.getElementById('tpl-save').addEventListener('click', onSave);
  document.getElementById('tpl-copy').addEventListener('click', onCopy);
  document.getElementById('tpl-delete').addEventListener('click', onDelete);
  document.getElementById('tpl-select').addEventListener('change', onSelectTemplate);

  document.getElementById('func-list').innerHTML =
    '<h3>Map</h3>' + funcs.mappers.map(f =>
      `<div class="small" style="padding:2px 0"><span class="mono">${C.esc(f.name)}</span> — ${C.esc(f.description)}</div>`).join('') +
    '<h3 class="mt">Reduce</h3>' + funcs.reducers.map(f =>
      `<div class="small" style="padding:2px 0"><span class="mono">${C.esc(f.name)}</span> — ${C.esc(f.description)}</div>`).join('');

  await loadTemplates();
  loadRecent();
}

function fillSelect(id, items) {
  document.getElementById(id).innerHTML = items
    .map(f => `<option value="${f.name}">${C.esc(f.name)}</option>`).join('');
}

// ---------------------------------------------------------------------------
// Form <-> spec
// ---------------------------------------------------------------------------
function fillFromSample(s) {
  fillForm({
    name: s.name, mapper: s.mapper, reducer: s.reducer,
    num_map_tasks: s.num_map_tasks, num_reduce_tasks: s.num_reduce_tasks,
    input_rows: s.input_rows, params: s.params || {},
  });
}

function fillForm(spec) {
  document.getElementById('name').value = spec.name ?? '';
  document.getElementById('mapper').value = spec.mapper ?? '';
  document.getElementById('reducer').value = spec.reducer ?? '';
  document.getElementById('num_map_tasks').value = spec.num_map_tasks ?? '';
  document.getElementById('num_reduce_tasks').value = spec.num_reduce_tasks ?? '';
  document.getElementById('input_rows').value = spec.input_rows ?? '';
  document.getElementById('param_pattern').value = (spec.params && spec.params.pattern) ?? '';
  document.getElementById('simulate_failure').checked =
    !!(spec.params && spec.params.simulate_failure);
}

function readForm() {
  const params = {};
  const pattern = document.getElementById('param_pattern').value.trim();
  if (pattern) params.pattern = pattern;
  if (document.getElementById('simulate_failure').checked) params.simulate_failure = true;
  return {
    name: document.getElementById('name').value.trim(),
    mapper: document.getElementById('mapper').value,
    reducer: document.getElementById('reducer').value,
    num_map_tasks: parseInt(document.getElementById('num_map_tasks').value, 10),
    num_reduce_tasks: parseInt(document.getElementById('num_reduce_tasks').value, 10),
    input_rows: parseInt(document.getElementById('input_rows').value, 10),
    params,
  };
}

// ---------------------------------------------------------------------------
// Validation rendering (issues locate the exact field)
// ---------------------------------------------------------------------------
function clearIssues() {
  FIELD_IDS.forEach(f => {
    const el = document.getElementById('err-' + f);
    if (el) el.textContent = '';
  });
  document.querySelectorAll('.invalid').forEach(el => el.classList.remove('invalid'));
  document.getElementById('issues').innerHTML = '';
}

function renderIssues(errors, warnings) {
  clearIssues();
  errors = errors || [];
  warnings = warnings || [];

  for (const issue of errors) {
    const host = document.getElementById('err-' + issue.field);
    if (host) {
      host.textContent = issue.message;
      const input = document.getElementById(issue.field);
      if (input) input.classList.add('invalid');
    }
  }

  const box = document.getElementById('issues');
  const block = (title, list, cls) => list.length
    ? `<div class="issue-block ${cls}"><div class="issue-title">${title}（${list.length}）</div>` +
      list.map(i => `<div class="issue-item"><span class="mono">${C.esc(i.field || 'job')}</span> — ${C.esc(i.message)}</div>`).join('') +
      '</div>'
    : '';
  box.innerHTML = block('错误 Errors', errors, 'issue-error') +
                  block('警告 Warnings（可继续提交）', warnings, 'issue-warn');
  return errors.length === 0;
}

async function validate(showOkToast) {
  try {
    const d = await API.post('/api/jobs/validate', readForm());
    const ok = renderIssues(d.errors, d.warnings);
    if (ok && showOkToast) C.toast('校验通过 Validation passed', 'ok');
    return ok;
  } catch (e) {
    C.toast('校验请求失败 ' + e.message, 'error');
    return false;
  }
}

async function onSubmit(ev) {
  ev.preventDefault();
  const valid = await validate(false);
  if (!valid) {
    C.toast('存在校验错误，请修正标红字段 Fix the highlighted fields', 'error');
    return;
  }
  const btn = ev.target.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    const job = await API.post('/api/jobs', readForm());
    C.toast('作业已提交 Job submitted: ' + job.job_id, 'ok');
    setTimeout(() => location.href = 'monitor.html', 600);
  } catch (e) {
    // 服务器端字段级错误（如提交瞬间环境变化）
    if (e.payload && Array.isArray(e.payload.errors)) {
      renderIssues(e.payload.errors, e.payload.warnings || []);
    }
    C.toast('提交失败 ' + e.message, 'error');
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------
async function loadTemplates(selectId) {
  const d = await API.get('/api/templates');
  TEMPLATES = d.templates || [];
  const sel = document.getElementById('tpl-select');
  sel.innerHTML = '<option value="">— 不使用模板 None —</option>' + TEMPLATES.map(t =>
    `<option value="${t.template_id}">${C.esc(t.name)}${t.builtin ? ' ★' : ''}</option>`).join('');
  if (selectId && TEMPLATES.some(t => t.template_id === selectId)) {
    sel.value = selectId;
  } else {
    CURRENT_TPL_ID = null;
    updateTemplateMeta();
  }
  onSelectTemplate();
}

function selectedTemplate() {
  const id = document.getElementById('tpl-select').value;
  return TEMPLATES.find(t => t.template_id === id) || null;
}

function onSelectTemplate() {
  const tpl = selectedTemplate();
  CURRENT_TPL_ID = tpl ? tpl.template_id : null;
  updateTemplateMeta();
}

function updateTemplateMeta() {
  const tpl = selectedTemplate();
  const meta = document.getElementById('tpl-meta');
  const saveBtn = document.getElementById('tpl-save');
  const delBtn = document.getElementById('tpl-delete');
  const copyBtn = document.getElementById('tpl-copy');
  const applyBtn = document.getElementById('tpl-apply');
  const has = !!tpl;
  applyBtn.disabled = !has;
  copyBtn.disabled = !has;
  saveBtn.disabled = !has;
  delBtn.disabled = !has;
  meta.textContent = tpl
    ? `${tpl.builtin ? '内置模板 built-in · ' : ''}更新于 updated ${C.fmtTime(tpl.updated_ms)}${tpl.description ? ' · ' + tpl.description : ''}`
    : '未选择模板；可调整下方表单后「存为新模板」。No template selected — tweak the form and use “Save as new”.';
}

async function onApply() {
  const tpl = selectedTemplate();
  if (!tpl) return;
  try {
    const d = await API.post(`/api/templates/${tpl.template_id}/apply`);
    // 仅填充一个独立快照；后续手动改动不会回写到模板
    fillForm(d.spec);
    CURRENT_TPL_ID = tpl.template_id;
    renderIssues(d.errors, d.warnings);
    C.toast(`已套用模板 ${tpl.name}（可自由修改，模板不会被改动）Applied — edits stay local`, 'ok');
  } catch (e) {
    C.toast('套用失败 ' + e.message, 'error');
  }
}

async function onSaveAsNew() {
  const tpl = selectedTemplate();
  const defaultName = tpl ? tpl.name : document.getElementById('name').value;
  const name = prompt('新模板名称 Template name:', defaultName);
  if (name === null) return;
  const body = readForm();
  body.template_name = name.trim();
  try {
    const tpl = await API.post('/api/templates', body);
    C.toast('模板已保存 Template saved', 'ok');
    await loadTemplates(tpl.template_id);
  } catch (e) {
    if (e.payload && e.payload.errors) renderIssues(e.payload.errors, []);
    C.toast('保存失败 ' + e.message, 'error');
  }
}

async function onSave() {
  const tpl = selectedTemplate();
  if (!tpl) return;
  const body = readForm();
  body.template_name = tpl.name;
  try {
    await API.put(`/api/templates/${tpl.template_id}`, body);
    C.toast('模板已更新 Template updated', 'ok');
    await loadTemplates(tpl.template_id);
  } catch (e) {
    if (e.payload && e.payload.errors) renderIssues(e.payload.errors, []);
    C.toast('保存失败 ' + e.message, 'error');
  }
}

async function onCopy() {
  const tpl = selectedTemplate();
  if (!tpl) return;
  const name = prompt('新模板名称 Template name:', tpl.name + ' (copy)');
  if (name === null) return;
  try {
    const copy = await API.post(`/api/templates/${tpl.template_id}/copy`,
      { template_name: name.trim() });
    C.toast('已复制为新模板 Copied', 'ok');
    await loadTemplates(copy.template_id);
  } catch (e) {
    C.toast('复制失败 ' + e.message, 'error');
  }
}

async function onDelete() {
  const tpl = selectedTemplate();
  if (!tpl) return;
  if (!confirm(`确认删除模板「${tpl.name}」？该操作不可恢复。Delete this template?`)) return;
  try {
    await API.del(`/api/templates/${tpl.template_id}`);
    C.toast('模板已删除 Template deleted', 'ok');
    await loadTemplates();
  } catch (e) {
    C.toast('删除失败 ' + e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
async function loadRecent() {
  const d = await API.get('/api/jobs');
  const jobs = d.jobs || [];
  document.getElementById('recent').innerHTML = jobs.length
    ? C.table([
        { key: 'name', label: '作业 Job' },
        { key: 'status', label: '状态 Status', render: r => C.stateBadge(r.status, true) },
        { key: 'mapper', label: 'Mapper' },
        { key: 'reducer', label: 'Reducer' },
        { key: 'created_ms', label: '时间 Time', render: r => C.fmtTime(r.created_ms) },
        { key: 'link', label: '', render: () => `<a href="monitor.html">监控→</a>` },
      ], jobs)
    : C.empty();
}

init();
C.poll(loadRecent, 4000).start();
