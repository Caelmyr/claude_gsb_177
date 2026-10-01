/* 作业提交 Submit — job form, named templates and field-level validation. */
Components.init('submit');
const C = Components;

let SAMPLES = [];
let TEMPLATES = [];
let activeTemplateId = null;   // template currently applied to the form (null = unsaved)

const NUM_FIELDS = ['num_map_tasks', 'num_reduce_tasks', 'input_rows'];

async function init() {
  const funcs = await API.get('/api/functions');
  SAMPLES = await API.get('/api/samples');

  fillSelect('mapper', funcs.mappers);
  fillSelect('reducer', funcs.reducers);

  const preset = document.getElementById('preset');
  preset.innerHTML = SAMPLES.map(s => `<option value="${s.name}">${C.esc(s.name)}</option>`).join('');
  preset.addEventListener('change', () => {
    const s = SAMPLES.find(x => x.name === preset.value);
    if (s) { fillFromSample(s); clearValidation(); validateFormSoon(); }
  });

  document.getElementById('mapper').addEventListener('change', () => {
    syncPatternVisibility();
    clearValidation();
    validateFormSoon();
  });
  document.getElementById('form').addEventListener('submit', onSubmit);

  document.getElementById('func-list').innerHTML =
    '<h3>Map</h3>' + funcs.mappers.map(f =>
      `<div class="small" style="padding:2px 0"><span class="mono">${C.esc(f.name)}</span> — ${C.esc(f.description)}</div>`).join('') +
    '<h3 class="mt">Reduce</h3>' + funcs.reducers.map(f =>
      `<div class="small" style="padding:2px 0"><span class="mono">${C.esc(f.name)}</span> — ${C.esc(f.description)}</div>`).join('');

  bindTemplateControls();
  document.querySelectorAll('#form input, #form select').forEach(el => {
    el.addEventListener('input', () => validateFormSoon());
  });
  syncPatternVisibility();
  await loadTemplates();
  loadRecent();
}

function fillSelect(id, items) {
  document.getElementById(id).innerHTML = items
    .map(f => `<option value="${f.name}">${C.esc(f.name)}</option>`).join('');
}

// ---------------------------------------------------------------------------
// Form <-> payload
// ---------------------------------------------------------------------------
function readForm() {
  const body = {
    name: document.getElementById('name').value.trim(),
    mapper: document.getElementById('mapper').value,
    reducer: document.getElementById('reducer').value,
    num_map_tasks: parseInt(document.getElementById('num_map_tasks').value, 10),
    num_reduce_tasks: parseInt(document.getElementById('num_reduce_tasks').value, 10),
    input_rows: parseInt(document.getElementById('input_rows').value, 10),
    params: {},
  };
  if (document.getElementById('simulate_failure').checked) body.params.simulate_failure = true;
  if (body.mapper === 'grep_mapper') {
    const pattern = document.getElementById('pattern').value.trim();
    if (pattern) body.params.pattern = pattern;
  }
  return body;
}

function writeForm(p) {
  document.getElementById('name').value = p.name || '';
  document.getElementById('mapper').value = p.mapper || '';
  document.getElementById('reducer').value = p.reducer || '';
  document.getElementById('num_map_tasks').value = p.num_map_tasks;
  document.getElementById('num_reduce_tasks').value = p.num_reduce_tasks;
  document.getElementById('input_rows').value = p.input_rows;
  document.getElementById('simulate_failure').checked = !!(p.params && p.params.simulate_failure);
  if (p.params && p.params.pattern !== undefined) document.getElementById('pattern').value = p.params.pattern;
  syncPatternVisibility();
}

function fillFromSample(s) {
  writeForm({
    name: s.name, mapper: s.mapper, reducer: s.reducer,
    num_map_tasks: s.num_map_tasks, num_reduce_tasks: s.num_reduce_tasks,
    input_rows: s.input_rows, params: s.params || {},
  });
  activeTemplateId = null;
  renderTemplateMeta();
}

function syncPatternVisibility() {
  document.getElementById('pattern-row').style.display =
    document.getElementById('mapper').value === 'grep_mapper' ? 'block' : 'none';
}

// ---------------------------------------------------------------------------
// Field-level validation rendering
// ---------------------------------------------------------------------------
function clearValidation() {
  document.querySelectorAll('.field-error,.field-warn').forEach(el => {
    el.textContent = '';
    el.classList.remove('show');
  });
  document.querySelectorAll('.invalid,.warn-field').forEach(el => {
    el.classList.remove('invalid', 'warn-field');
  });
  document.getElementById('error-banner').classList.remove('show');
  document.getElementById('warn-banner').classList.remove('show');
}

function showIssues(issues, kind) {
  const cls = kind === 'error' ? '.field-error' : '.field-warn';
  for (const issue of issues) {
    const nodes = document.querySelectorAll(`${cls}[data-field="${CSS.escape(issue.field)}"]`);
    nodes.forEach(n => { n.textContent = issue.message; n.classList.add('show'); });
    // Highlight the concrete control; params.* maps onto the named leaf input.
    const leaf = issue.field.split('.').pop();
    const input = document.getElementById(leaf) ||
      document.querySelector(`#form [name="${CSS.escape(leaf)}"]`);
    if (input) input.classList.add(kind === 'error' ? 'invalid' : 'warn-field');
  }
}

function renderBanners(validation) {
  const errBanner = document.getElementById('error-banner');
  const warnBanner = document.getElementById('warn-banner');
  if (validation.errors.length) {
    errBanner.innerHTML = '以下字段不合规，请修正后再提交：' +
      '<ul>' + validation.errors.map(e => `<li><b>${C.esc(e.field)}</b> — ${C.esc(e.message)}</li>`).join('') + '</ul>';
    errBanner.classList.add('show');
  }
  if (validation.warnings.length) {
    warnBanner.innerHTML = '环境 / 输入提示（不阻止提交）：' +
      '<ul>' + validation.warnings.map(w => `<li><b>${C.esc(w.field)}</b> — ${C.esc(w.message)}</li>`).join('') + '</ul>';
    warnBanner.classList.add('show');
  }
}

let validateTimer = null;
function validateFormSoon() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateForm, 250);
}

async function validateForm() {
  let validation;
  try {
    validation = await API.post('/api/validate-job', readForm());
  } catch (e) {
    return; // transient: keep the previous state
  }
  clearValidation();
  showIssues(validation.errors, 'error');
  showIssues(validation.warnings, 'warn');
  renderBanners(validation);
  return validation;
}

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------
async function onSubmit(ev) {
  ev.preventDefault();
  const btn = ev.target.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    const job = await API.post('/api/jobs', readForm());
    C.toast('作业已提交 Job submitted: ' + job.job_id, 'ok');
    if (job.warnings && job.warnings.length) {
      C.toast('有 ' + job.warnings.length + ' 条环境提示，请留意横幅', 'warn');
    }
    setTimeout(() => location.href = 'monitor.html', 600);
  } catch (e) {
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
  const sel = document.getElementById('template-select');
  sel.innerHTML = '<option value="">选择模板 Select a template…</option>' +
    TEMPLATES.map(t => `<option value="${t.template_id}">${C.esc(t.name)}</option>`).join('');
  if (activeTemplateId && TEMPLATES.some(t => t.template_id === activeTemplateId)) {
    sel.value = activeTemplateId;
  } else {
    activeTemplateId = null;
  }
  renderTemplateMeta();
}

function selectedTemplate() {
  const id = document.getElementById('template-select').value;
  return TEMPLATES.find(t => t.template_id === id) || null;
}

function renderTemplateMeta() {
  const meta = document.getElementById('tpl-meta');
  const tpl = TEMPLATES.find(t => t.template_id === activeTemplateId);
  if (!tpl) { meta.classList.remove('show'); meta.textContent = ''; return; }
  meta.innerHTML = `已套用模板 <b>${C.esc(tpl.name)}</b> · ${C.esc(tpl.mapper)} / ${C.esc(tpl.reducer)}` +
    (tpl.source_template_id ? ' · 由其他模板复制 duplicated' : '') +
    ' — 当前表单的修改不会写回模板，直到你点击“保存修改”。Form edits stay local until you press Update.';
  meta.classList.add('show');
}

function bindTemplateControls() {
  document.getElementById('template-select').addEventListener('change', () => {
    activeTemplateId = document.getElementById('template-select').value || null;
    renderTemplateMeta();
  });

  document.getElementById('tpl-apply').addEventListener('click', applyTemplate);
  document.getElementById('tpl-save').addEventListener('click', saveAsTemplate);
  document.getElementById('tpl-copy').addEventListener('click', duplicateTemplate);
  document.getElementById('tpl-edit').addEventListener('click', updateTemplate);
  document.getElementById('tpl-delete').addEventListener('click', deleteTemplate);
}

async function applyTemplate() {
  const tpl = selectedTemplate();
  if (!tpl) { C.toast('请先选择模板 Select a template first', 'error'); return; }
  clearValidation();
  try {
    const d = await API.post(`/api/templates/${tpl.template_id}/apply`, {});
    writeForm(d.payload);
    activeTemplateId = tpl.template_id;
    document.getElementById('template-select').value = tpl.template_id;
    renderTemplateMeta();
    showIssues(d.validation.errors, 'error');
    showIssues(d.validation.warnings, 'warn');
    renderBanners(d.validation);
    C.toast(`已套用模板 ${tpl.name} — 可继续修改，模板不受影响`, 'ok');
  } catch (e) {
    C.toast('套用失败 ' + e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
function promptTemplateName(defaultName) {
  const name = window.prompt('模板名称 Template name:', defaultName);
  return name == null ? null : name.trim();
}

async function saveAsTemplate() {
  const validation = await validateForm();
  if (validation && !validation.ok) {
    C.toast('参数校验未通过，无法保存模板 Validation failed', 'error');
    return;
  }
  const name = promptTemplateName(readForm().name);
  if (!name) return;
  try {
    const tpl = await API.post('/api/templates', { ...readForm(), name });
    await loadTemplates();
    activeTemplateId = tpl.template_id;
    document.getElementById('template-select').value = tpl.template_id;
    renderTemplateMeta();
    C.toast('模板已保存 Template saved: ' + tpl.name, 'ok');
  } catch (e) {
    C.toast('保存失败 ' + e.message, 'error');
  }
}

async function duplicateTemplate() {
  const tpl = selectedTemplate();
  if (!tpl) { C.toast('请先选择要复制的模板 Select a template first', 'error'); return; }
  const name = promptTemplateName(tpl.name + ' (copy)');
  if (!name) return;
  try {
    const copy = await API.post(`/api/templates/${tpl.template_id}/duplicate`, { name });
    await loadTemplates();
    activeTemplateId = copy.template_id;
    document.getElementById('template-select').value = copy.template_id;
    writeForm({
      name: copy.name, mapper: copy.mapper, reducer: copy.reducer,
      num_map_tasks: copy.num_map_tasks, num_reduce_tasks: copy.num_reduce_tasks,
      input_rows: copy.input_rows, params: copy.params || {},
    });
    renderTemplateMeta();
    clearValidation();
    validateFormSoon();
    C.toast('已复制为新模板 Duplicated: ' + copy.name, 'ok');
  } catch (e) {
    C.toast('复制失败 ' + e.message, 'error');
  }
}

async function updateTemplate() {
  const tpl = selectedTemplate();
  if (!tpl) { C.toast('请先选择要保存到的模板 Select a template first', 'error'); return; }
  const validation = await validateForm();
  if (validation && !validation.ok) {
    C.toast('参数校验未通过，无法保存 Validation failed', 'error');
    return;
  }
  if (!window.confirm(`将当前表单保存并覆盖模板 “${tpl.name}”？Overwrite this template?`)) return;
  try {
    const updated = await API.put(`/api/templates/${tpl.template_id}`, { ...readForm(), name: tpl.name });
    await loadTemplates();
    activeTemplateId = updated.template_id;
    renderTemplateMeta();
    C.toast('模板已更新 Template updated', 'ok');
  } catch (e) {
    C.toast('更新失败 ' + e.message, 'error');
  }
}

async function deleteTemplate() {
  const tpl = selectedTemplate();
  if (!tpl) { C.toast('请先选择要删除的模板 Select a template first', 'error'); return; }
  if (!window.confirm(`删除模板 “${tpl.name}”？已提交的作业不受影响。Delete? Submitted jobs are unaffected.`)) return;
  try {
    await API.del(`/api/templates/${tpl.template_id}`);
    activeTemplateId = null;
    await loadTemplates();
    C.toast('模板已删除 Template deleted', 'ok');
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
        { key: 'link', label: '', render: r => `<a href="monitor.html">监控→</a>` },
      ], jobs)
    : C.empty();
}

init();
C.poll(loadRecent, 4000).start();
