import { ReliefComparison } from './lab_viewer.js';
import { STALE_TASK_READ, TaskEpoch, totalElapsedSeconds } from './lab_state.js';

const byId = (id) => document.getElementById(id);
const lifecycle = new AbortController();
const taskEpoch = new TaskEpoch();
const parameterLoadEpoch = new TaskEpoch();
const compactViewport = window.matchMedia('(max-width: 980px)');
const ACTIVE_STATUSES = new Set(['running', 'cancelling']);
const STATUS_LABELS = { running: '生成中', cancelling: '正在取消', cancelled: '已取消', ready: '已完成', failed: '失败', interrupted: '已中断' };
const state = {
  token: null, config: null, parameters: {}, images: [], references: [], runs: [],
  fields: new Map(), controls: new Map(), details: new Map(), detailRequests: new Map(), entries: new Map(),
  imageId: null, maskId: null, boxes: [], imageReady: false, drag: null,
  activeId: null, activeJob: null, watched: new Set(), handled: new Set(),
  selected: { left: null, right: null }, pendingSelection: { left: null, right: null }, selectionSequence: { left: 0, right: 0 },
  downloadBlobs: { left: null, right: null }, historySequence: 0,
  uploading: 0, submitting: false, cancellingRequest: false, polling: false, pollingEpoch: null, pollTimer: null, disposed: false,
};

const clone = (value) => structuredClone(value);
const on = (element, event, handler) => element.addEventListener(event, handler, { signal: lifecycle.signal });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const finite = (value) => typeof value === 'number' && Number.isFinite(value);

function localUrl(value) {
  if (typeof value !== 'string' || !value) throw new Error('文件地址为空。');
  const url = new URL(value, window.location.href);
  if (url.origin !== window.location.origin) throw new Error('实验台只读取当前本地服务的文件。');
  return url.href;
}

async function api(path, { method = 'GET', json, body } = {}) {
  const headers = {};
  if (method !== 'GET') {
    if (!state.token) throw new Error('尚未连接到实验台，请刷新状态。');
    headers['X-Lab-Token'] = state.token;
  }
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }
  const response = await fetch(localUrl(path), { method, headers, body, credentials: 'same-origin', signal: lifecycle.signal });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    let message = typeof payload?.detail === 'string' ? payload.detail
      : typeof payload?.error === 'string' ? payload.error : `本地请求失败（HTTP ${response.status}）。`;
    if (response.status === 403) message = '本地实验台令牌已失效，请刷新页面重新连接。';
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showError(error) {
  if (state.disposed || error?.name === 'AbortError') return;
  byId('app-error-text').textContent = error?.message || String(error);
  byId('app-error').hidden = false;
}

function clearError() { byId('app-error').hidden = true; }

function notice(message) {
  byId('app-notice').textContent = message;
  byId('app-notice').hidden = !message;
}

function action(handler) {
  return (...args) => Promise.resolve().then(() => handler(...args)).catch(showError);
}

function duration(seconds) {
  if (!finite(seconds)) return '未记录';
  const value = Math.max(0, seconds);
  if (value < 60) return `${value.toFixed(1)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.floor(value % 60)} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
}

function elapsed(job) {
  return totalElapsedSeconds(job);
}

function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
}

function snapshot(job) {
  return {
    image_id: job.image_id ?? null,
    mask_image_id: job.mask_image_id ?? null,
    parameters: clone(job.parameters ?? {}),
    boxes: clone(job.boxes ?? []),
  };
}

function draftSnapshot() {
  return { image_id: state.imageId, mask_image_id: maskIsActive() && state.parameters.mask_mode === 'provided' ? state.maskId : null, parameters: clone(state.parameters), boxes: clone(state.boxes) };
}

function matchingSnapshot(a, b) {
  if (!a || !b) return false;
  const keys = state.config?.fields.map((field) => field.key) ?? [];
  return a.image_id === b.image_id && a.mask_image_id === b.mask_image_id && same(a.boxes, b.boxes)
    && keys.every((key) => same(a.parameters[key], b.parameters[key]));
}

function maskIsActive() { return Number(state.parameters.background_flatten) > 0; }
function currentImage() { return state.images.find((image) => image.id === state.imageId); }
function currentMask() { return state.images.find((image) => image.id === state.maskId); }

function formHasErrors() {
  return [...byId('generation-form').querySelectorAll('input, select')].some((input) => !input.disabled && !input.validity.valid);
}

function updateDraft() {
  if (!state.config) return;
  const draft = draftSnapshot();
  const matchesJob = state.activeId && state.activeJob && matchingSnapshot(draft, snapshot(state.activeJob));
  const matchesResult = state.selected.right?.snapshot && matchingSnapshot(draft, state.selected.right.snapshot);
  const invalid = formHasErrors();
  byId('draft-state').textContent = invalid ? '参数待检查 · 尚未生成'
    : matchesJob ? '当前参数已提交'
      : matchesResult ? '与右侧结果参数一致' : '当前参数尚未生成';
  byId('draft-state').classList.toggle('dirty', invalid || (!matchesJob && !matchesResult));
  const retained = byId('retained-result-note');
  if (state.activeId) {
    retained.textContent = state.selected.right
      ? `生成期间保留右侧“${state.selected.right.label}”；新任务完成后再替换。`
      : '任务正在生成，完成后新结果会载入右侧。';
    retained.hidden = false;
  } else if (state.selected.right && !matchesResult) {
    retained.textContent = '右侧是已生成结果。修改照片、保留框或生成参数不会自动改变这个模型。';
    retained.hidden = false;
  } else {
    retained.hidden = true;
  }
  byId('generate-button').disabled = !state.imageId || Boolean(state.activeId) || state.submitting || state.uploading > 0;
  byId('generate-button').textContent = state.submitting ? '正在提交…' : '生成模型';
  byId('image-file').disabled = state.uploading > 0;
  byId('mask-file').disabled = state.uploading > 0;
}

function formatParameter(field, value) {
  if (value === undefined || value === null) return '未记录';
  if (field?.kind === 'boolean') return value ? '开启' : '关闭';
  if (field?.kind === 'choice') return field.options.find((option) => option.value === value)?.label || String(value);
  if (Array.isArray(value)) return value.join(', ');
  if (finite(value)) {
    const display = Number((value * (field?.display_scale || 1)).toFixed(6));
    return `${display}${field?.unit ? ` ${field.unit}` : ''}`;
  }
  return String(value);
}

function readControl(field, input) {
  input.setCustomValidity('');
  if (field.kind === 'boolean') return input.checked;
  if (field.kind === 'choice') return field.options.find((option) => String(option.value) === input.value)?.value;
  if (field.kind === 'scales') {
    const values = input.value.trim().split(/[,，\s]+/).filter(Boolean).map(Number);
    const valid = values.length >= 1 && values.length <= field.max_items
      && values.every((value) => Number.isFinite(value) && value >= field.min && value <= field.max)
      && values.includes(field.required_value) && new Set(values).size === values.length;
    if (!valid) {
      input.setCustomValidity(`请输入 1–${field.max_items} 个不重复数值，范围 ${field.min}–${field.max}，并包含 ${field.required_value}。`);
      return undefined;
    }
    return values.sort((a, b) => b - a);
  }
  if (input.value.trim() === '') return undefined;
  const value = Number(input.value) / (field.display_scale || 1);
  if (!Number.isFinite(value)) return undefined;
  if (field.kind === 'integer' && !Number.isInteger(value)) {
    input.setCustomValidity('请输入整数。');
    return undefined;
  }
  return value;
}

function writeControls() {
  for (const [key, input] of state.controls) {
    const field = state.fields.get(key);
    const value = state.parameters[key];
    input.setCustomValidity('');
    if (field.kind === 'boolean') input.checked = Boolean(value);
    else if (field.kind === 'scales') input.value = value.join(', ');
    else if (field.kind === 'number' || field.kind === 'integer') input.value = String(Number((value * (field.display_scale || 1)).toFixed(8)));
    else input.value = String(value);
  }
  updateParameterState();
}

function updateParameterState() {
  const invalid = formHasErrors();
  const preset = !invalid && state.config.presets.find((candidate) => state.config.fields.every((field) => same(candidate.parameters[field.key], state.parameters[field.key])));
  byId('preset-status').textContent = preset ? `当前：${preset.label}` : `当前：自定义${invalid ? '（待校验）' : ''}`;
  for (const button of byId('preset-buttons').children) button.setAttribute('aria-pressed', String(button.dataset.presetId === preset?.id));
  const scales = state.parameters.scales || [];
  const calls = scales.length * (state.parameters.mirror ? 2 : 1);
  const cacheHint = state.parameters.use_cache === false ? '已关闭缓存，本次重新计算。' : '缓存命中时会复用已有深度。';
  byId('expected-passes').textContent = `预计最多 ${calls} 次深度推理（${scales.length} 个尺度${state.parameters.mirror ? ' × 原图与镜像' : ''}）。重复像素输入会去重。${cacheHint}`;
  const active = maskIsActive();
  const provided = state.parameters.mask_mode === 'provided';
  const maskKeys = new Set(['mask_mode', 'min_area_fraction', 'mask_refine', 'mask_refine_iterations', 'mask_refine_radius', 'mask_side', 'edge_transition_fraction']);
  const autoKeys = new Set(['min_area_fraction', 'mask_refine', 'mask_refine_iterations', 'mask_refine_radius']);
  for (const [key, input] of state.controls) {
    const disabled = (!active && maskKeys.has(key)) || (active && provided && autoKeys.has(key))
      || (active && !provided && !state.parameters.mask_refine && ['mask_refine_iterations', 'mask_refine_radius'].includes(key));
    input.disabled = disabled;
    input.closest('.param-field').classList.toggle('disabled', disabled);
  }
  byId('mask-upload').hidden = !(active && provided);
  byId('mask-mode-note').textContent = !active ? '背景压平为 0%：保留整图原始起伏，跳过分割。保留框和蒙版设置会留存，但本次不使用。'
    : provided ? '使用上传蒙版；自动面积过滤和自动图割选项暂停使用，原设置保留。' : '自动前景与手工保留框合并使用；色块只表示保留区域，不代表不同高度。';
  byId('image-stage').classList.toggle('inactive', !active || !state.imageReady);
  byId('add-box').disabled = !active || !state.imageId || state.boxes.length >= 8;
  byId('clear-boxes').disabled = !active || !state.boxes.length;
  for (const input of byId('box-list').querySelectorAll('input, button')) input.disabled = !active;
  updateMaskPreview();
  updateDraft();
}

function createParameterControls() {
  const groups = byId('parameter-groups');
  const impactLabels = { depth: '影响深度计算预算', inference: '影响推理次数', fusion: '影响融合，可复用原始深度', mask: '影响保留区域', shape: '影响几何形状，可复用深度', advanced: '影响边界、范围或计算资源' };
  for (const group of state.config.groups) {
    const details = document.createElement('details');
    details.className = 'parameter-group';
    details.open = group.id === state.config.groups[0].id;
    const summary = document.createElement('summary');
    summary.textContent = group.label;
    const description = document.createElement('p');
    description.className = 'group-description';
    description.textContent = group.description;
    details.append(summary, description);
    for (const field of state.config.fields.filter((candidate) => candidate.group === group.id)) {
      state.fields.set(field.key, field);
      const wrapper = document.createElement('div');
      wrapper.className = `param-field${field.kind === 'boolean' ? ' is-boolean' : ''}`;
      const label = document.createElement('label');
      label.htmlFor = `parameter-${field.key}`;
      label.textContent = `${field.label}${field.unit ? `（${field.unit}）` : ''}`;
      const input = document.createElement(field.kind === 'choice' ? 'select' : 'input');
      input.id = label.htmlFor;
      input.name = field.key;
      input.setAttribute('aria-describedby', `${input.id}-help ${input.id}-impact`);
      if (field.kind === 'choice') {
        for (const option of field.options) {
          const node = document.createElement('option');
          node.value = String(option.value);
          node.textContent = option.label;
          input.append(node);
        }
      } else if (field.kind === 'boolean') {
        input.type = 'checkbox';
      } else if (field.kind === 'scales') {
        input.type = 'text';
        input.inputMode = 'decimal';
        input.required = true;
      } else {
        const scale = field.display_scale || 1;
        input.type = 'number';
        input.required = true;
        input.min = String(field.min * scale);
        input.max = String(field.max * scale);
        input.step = String(Number((field.step * scale).toFixed(8)));
        input.inputMode = field.kind === 'integer' ? 'numeric' : 'decimal';
      }
      const help = document.createElement('p');
      help.id = `${input.id}-help`;
      help.className = 'field-help';
      help.textContent = field.help;
      const impact = document.createElement('p');
      impact.id = `${input.id}-impact`;
      impact.className = 'field-impact';
      impact.textContent = impactLabels[field.impact] || field.impact || '';
      if (field.kind === 'boolean') { label.append(input); wrapper.append(label, help, impact); }
      else wrapper.append(label, input, help, impact);
      on(input, field.kind === 'boolean' || field.kind === 'choice' ? 'change' : 'input', () => {
        const value = readControl(field, input);
        if (value !== undefined && input.validity.valid) state.parameters[field.key] = value;
        updateParameterState();
      });
      state.controls.set(field.key, input);
      details.append(wrapper);
    }
    groups.append(details);
  }
  for (const preset of state.config.presets) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button subtle';
    button.textContent = preset.label;
    button.dataset.presetId = preset.id;
    on(button, 'click', () => {
      state.parameters = clone(preset.parameters);
      writeControls();
      notice(`已填入“${preset.label}”参数；需要点击生成，已有模型保持不变。`);
    });
    byId('preset-buttons').append(button);
  }
  writeControls();
}

function renderImageChoices() {
  for (const [id, kind, selected, placeholder] of [
    ['image-select', 'image', state.imageId, '请选择照片'],
    ['mask-select', 'mask', state.maskId, '选择已上传蒙版'],
  ]) {
    const select = byId(id);
    select.replaceChildren();
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = placeholder;
    select.append(empty);
    for (const image of state.images.filter((item) => item.kind === kind)) {
      const option = document.createElement('option');
      option.value = image.id;
      option.textContent = image.name;
      select.append(option);
    }
    select.value = selected || '';
    select.disabled = !state.config;
  }
}

function setSourceImage(id, { boxes, maskId = null } = {}) {
  state.imageId = id || null;
  state.maskId = maskId;
  state.drag = null;
  state.imageReady = false;
  const image = currentImage();
  state.boxes = clone(boxes ?? image?.initial_boxes ?? []).slice(0, 8);
  const element = byId('source-image');
  element.hidden = true;
  byId('image-placeholder').hidden = false;
  byId('image-placeholder').textContent = image ? '正在载入照片…' : '选择或上传照片后开始实验';
  byId('image-size').textContent = image ? `${image.width} × ${image.height}` : '';
  if (image) {
    byId('image-stage').style.aspectRatio = `${image.width} / ${image.height}`;
    try { element.src = localUrl(image.preview_url); } catch (error) { showError(error); }
  } else element.removeAttribute('src');
  renderImageChoices();
  renderBoxes();
  updateParameterState();
}

function updateMaskPreview() {
  const mask = currentMask();
  const preview = byId('mask-preview');
  preview.hidden = !mask;
  byId('mask-select').value = state.maskId || '';
  byId('mask-note').classList.remove('inline-error');
  if (mask) {
    const image = currentImage();
    const compatible = !image || mask.width * image.height === image.width * mask.height;
    byId('mask-note').textContent = compatible
      ? `蒙版 ${mask.width} × ${mask.height}；白色保留起伏，黑色压平。`
      : `蒙版 ${mask.width} × ${mask.height} 与照片宽高比不同，请上传匹配的蒙版。`;
    byId('mask-note').classList.toggle('inline-error', !compatible);
    try {
      const url = localUrl(mask.preview_url);
      if (preview.src !== url) preview.src = url;
    } catch (error) { showError(error); }
  } else {
    preview.removeAttribute('src');
    byId('mask-note').textContent = '白色保留起伏，黑色压平；须与照片方向和宽高比一致。';
  }
}

function drawBoxes() {
  const svg = byId('boxes-overlay');
  svg.replaceChildren();
  const boxes = state.drag ? [...state.boxes, state.drag.box] : state.boxes;
  boxes.forEach((box, index) => {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', String(box[0] * 1000));
    rect.setAttribute('y', String(box[1] * 1000));
    rect.setAttribute('width', String((box[2] - box[0]) * 1000));
    rect.setAttribute('height', String((box[3] - box[1]) * 1000));
    rect.setAttribute('fill', '#40a66b20');
    rect.setAttribute('stroke', '#f9fff8');
    rect.setAttribute('stroke-width', '2');
    rect.setAttribute('vector-effect', 'non-scaling-stroke');
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', String(box[0] * 1000 + 12));
    label.setAttribute('y', String(box[1] * 1000 + 36));
    label.setAttribute('fill', 'white');
    label.setAttribute('stroke', '#244730');
    label.setAttribute('stroke-width', '4');
    label.setAttribute('paint-order', 'stroke');
    label.setAttribute('font-size', '32');
    label.setAttribute('font-weight', '700');
    label.textContent = String(index + 1);
    svg.append(rect, label);
  });
}

function renderBoxes() {
  drawBoxes();
  byId('box-count').textContent = `${state.boxes.length} / 8`;
  const list = byId('box-list');
  list.replaceChildren();
  state.boxes.forEach((box, index) => {
    const row = document.createElement('div');
    row.className = 'box-row';
    const heading = document.createElement('div');
    heading.className = 'box-row-heading';
    const title = document.createElement('span');
    title.textContent = `保留框 ${index + 1}`;
    const remove = document.createElement('button');
    remove.className = 'text-button';
    remove.type = 'button';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除保留框 ${index + 1}`);
    on(remove, 'click', () => { state.boxes.splice(index, 1); renderBoxes(); updateParameterState(); });
    heading.append(title, remove);
    const coordinates = document.createElement('div');
    coordinates.className = 'box-coordinates';
    const inputs = [];
    ['左 %', '上 %', '右 %', '下 %'].forEach((name, coordinate) => {
      const label = document.createElement('label');
      label.textContent = name;
      const input = document.createElement('input');
      input.type = 'number'; input.min = '0'; input.max = '100'; input.step = '0.01'; input.required = true;
      input.value = String(Number((box[coordinate] * 100).toFixed(2)));
      input.setAttribute('aria-label', `保留框 ${index + 1} ${name}`);
      on(input, 'input', () => {
        for (const candidate of inputs) candidate.setCustomValidity('');
        const next = inputs.map((candidate) => Number(candidate.value) / 100);
        if (inputs.some((candidate) => candidate.value === '' || !candidate.validity.valid)
          || !(next[0] < next[2] && next[1] < next[3])) {
          input.setCustomValidity('右边须大于左边，下边须大于上边，坐标范围为 0–100%。');
        } else {
          state.boxes[index] = next;
          drawBoxes();
        }
        updateDraft();
      });
      inputs.push(input);
      label.append(input);
      coordinates.append(label);
    });
    row.append(heading, coordinates);
    list.append(row);
  });
  byId('boxes-details').hidden = !state.boxes.length;
}

async function uploadFile(kind, input) {
  const file = input.files?.[0];
  if (!file) return;
  clearError();
  state.uploading += 1;
  updateDraft();
  notice(`正在上传${kind === 'image' ? '照片' : '蒙版'}…`);
  try {
    const image = await api(`/api/images?kind=${kind}&name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file });
    state.images = [...state.images.filter((item) => item.id !== image.id), image];
    if (kind === 'image') setSourceImage(image.id);
    else { state.maskId = image.id; renderImageChoices(); updateMaskPreview(); }
    notice(`${kind === 'image' ? '照片' : '蒙版'}已上传；点击生成后才会产生新模型。`);
  } catch (error) {
    notice('');
    throw error;
  } finally {
    state.uploading -= 1;
    input.value = '';
    updateDraft();
  }
}

function cacheSummary(report) {
  if (!report) return '';
  const cache = report.cache;
  const calls = report.actual_inference_calls;
  const parts = [];
  if (cache?.depth_hit === true) parts.push('复用深度 / 重建模型');
  else if (finite(cache?.depth_view_hits) && cache.depth_view_hits > 0) {
    parts.push(`复用 ${cache.depth_view_hits} / ${cache.depth_view_total} 份深度，再重建模型`);
  }
  if (finite(calls)) parts.push(`本次新深度推理 ${calls} 次`);
  if (cache?.semantic_hit === true) parts.push('分割结果已复用');
  return parts.join(' · ');
}

function upsertRun(job) {
  const existing = state.details.get(job.id);
  const merged = { ...existing, ...job };
  state.details.set(job.id, merged);
  const index = state.runs.findIndex((run) => run.id === job.id);
  if (index >= 0) state.runs[index] = merged;
  else state.runs.unshift(merged);
  return merged;
}

async function getRun(id, fresh = false) {
  const epoch = taskEpoch.capture();
  const cached = state.details.get(id);
  if (!fresh && cached && (cached.report || cached.status !== 'ready')) return cached;
  const existing = state.detailRequests.get(id);
  if (existing?.epoch === epoch) return existing.request;
  const request = api(`/api/runs/${encodeURIComponent(id)}`).then((job) => taskEpoch.isCurrent(epoch) && !state.disposed ? upsertRun(job) : job);
  const pending = { epoch, request };
  state.detailRequests.set(id, pending);
  try { return await request; } finally { if (state.detailRequests.get(id) === pending) state.detailRequests.delete(id); }
}

function runEntry(job) {
  return {
    key: `run:${job.id}`, kind: 'run', id: job.id, label: job.title || `${job.image_name || '生成结果'} · ${timeLabel(job.created_at)}`,
    model: job.files?.model, parameters: clone(job.parameters || {}), snapshot: snapshot(job), job,
  };
}

function refreshEntries() {
  const entries = new Map();
  for (const reference of state.references) {
    entries.set(`reference:${reference.id}`, {
      key: `reference:${reference.id}`, kind: 'reference', id: reference.id,
      label: reference.label, model: reference.model_url,
      parameters: reference.parameters ? clone(reference.parameters) : null,
    });
  }
  for (const job of state.runs.filter((run) => run.status === 'ready' && run.files?.model)) {
    const entry = runEntry(job);
    entries.set(entry.key, entry);
  }
  state.entries = entries;
  for (const slot of ['left', 'right']) {
    const select = byId(`${slot}-result`);
    const selected = state.pendingSelection[slot] || state.selected[slot]?.key || select.value;
    const signature = [...entries.values()].map((entry) => [entry.key, entry.label]);
    if (select.dataset.signature !== JSON.stringify(signature)) {
      select.dataset.signature = JSON.stringify(signature);
      select.replaceChildren();
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = entries.size ? '选择对照模型' : '暂无已生成模型';
      empty.disabled = true;
      select.append(empty);
      for (const [kind, label] of [['reference', '已有参考模型'], ['run', '本地运行结果']]) {
        const group = document.createElement('optgroup');
        group.label = label;
        for (const entry of entries.values()) {
          if (entry.kind !== kind) continue;
          const option = document.createElement('option');
          option.value = entry.key;
          option.textContent = entry.label;
          group.append(option);
        }
        if (group.children.length) select.append(group);
      }
    }
    select.value = entries.has(selected) ? selected : '';
    select.disabled = !entries.size;
  }
}

function renderModelStatus(slot, status) {
  const element = byId(`${slot}-model-status`);
  element.classList.toggle('retained', Boolean(status.retainedLabel));
  element.classList.toggle('error', Boolean(status.error));
  if (status.loading) {
    element.textContent = `正在载入“${status.label}”${status.retainedLabel ? `；仍显示“${status.retainedLabel}”` : ''}…`;
    byId(`${slot}-title`).textContent = status.label;
    element.hidden = false;
  } else if (status.error) {
    element.textContent = `${status.error}${status.retainedLabel ? ` 仍保留“${status.retainedLabel}”。` : ''}`;
    if (status.retainedLabel) byId(`${slot}-title`).textContent = status.retainedLabel;
    element.hidden = false;
  } else if (status.ready) {
    element.hidden = true;
    byId(`${slot}-title`).textContent = status.label;
  }
}

const comparison = new ReliefComparison({ left: byId('left-viewport'), right: byId('right-viewport') }, {
  onStatus: renderModelStatus,
  onPoseChange: () => {
    byId('display-front').setAttribute('aria-pressed', 'false');
    byId('display-angle').setAttribute('aria-pressed', 'false');
  },
});

function setDownload(link, url, filename) {
  link.hidden = !url;
  link.removeAttribute('href');
  if (!url) return;
  link.href = localUrl(url);
  link.download = filename;
}

function renderEntry(slot, entry) {
  byId(`${slot}-title`).textContent = entry.label;
  byId(`${slot}-result`).value = entry.key;
  const completeReference = entry.parameters && state.config.fields.every((field) => Object.hasOwn(entry.parameters, field.key));
  const description = entry.kind === 'run'
    ? [entry.job.image_name, `生成耗时 ${duration(elapsed(entry.job))}`, cacheSummary(entry.job.report)].filter(Boolean).join(' · ')
    : completeReference ? '已生成参考模型；生成参数以保存的快照为准。' : '参考模型：未保存完整参数快照，不将当前表单当作它的生成参数。';
  byId(`${slot}-summary`).textContent = description;
  setDownload(byId(`${slot}-model-download`), entry.model, `${entry.id}.glb`);
  if (state.downloadBlobs[slot]) {
    URL.revokeObjectURL(state.downloadBlobs[slot]);
    state.downloadBlobs[slot] = null;
  }
  const parameterLink = byId(`${slot}-parameters-download`);
  if (entry.job?.files?.parameters) setDownload(parameterLink, entry.job.files.parameters, `${entry.id}-parameters.json`);
  else if (entry.parameters) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(entry.parameters, null, 2)], { type: 'application/json' }));
    state.downloadBlobs[slot] = url;
    parameterLink.href = url;
    parameterLink.download = `${entry.id}-parameters.json`;
    parameterLink.hidden = false;
  } else parameterLink.hidden = true;
  if (slot === 'right') {
    const files = entry.job?.files;
    const show = Boolean(files?.original && files?.overlay);
    byId('result-images-details').hidden = !show;
    for (const [id, key] of [['result-original', 'original'], ['result-overlay', 'overlay']]) {
      const image = byId(id);
      if (show) image.src = localUrl(files[key]);
      else image.removeAttribute('src');
    }
  }
}

function renderDifferences() {
  const left = state.selected.left;
  const right = state.selected.right;
  const empty = byId('diff-empty');
  byId('diff-rows').replaceChildren();
  byId('diff-table-wrap').hidden = true;
  empty.hidden = false;
  if (!left || !right) { empty.textContent = '两侧选择模型后，只显示不同的生成参数。'; return; }
  if (!left.parameters || !right.parameters) { empty.textContent = '其中一个参考模型没有保存生成参数，无法逐项比较。'; return; }
  byId('diff-left-label').textContent = left.label;
  byId('diff-right-label').textContent = right.label;
  const keys = Object.keys(left.parameters).filter((key) => Object.hasOwn(right.parameters, key));
  const incomplete = [...state.fields.keys()].some((key) => !Object.hasOwn(left.parameters, key) || !Object.hasOwn(right.parameters, key));
  let count = 0;
  for (const key of keys) {
    if (same(left.parameters[key], right.parameters[key])) continue;
    const field = state.fields.get(key);
    const row = document.createElement('tr');
    const label = document.createElement('th');
    label.scope = 'row';
    label.textContent = field?.label || key;
    const leftCell = document.createElement('td');
    leftCell.textContent = formatParameter(field, left.parameters[key]);
    const rightCell = document.createElement('td');
    rightCell.textContent = formatParameter(field, right.parameters[key]);
    row.append(label, leftCell, rightCell);
    byId('diff-rows').append(row);
    count += 1;
  }
  if (left.snapshot && right.snapshot) {
    const additions = [];
    if (left.snapshot.image_id !== right.snapshot.image_id) additions.push(['输入照片', left.job.image_name || left.snapshot.image_id, right.job.image_name || right.snapshot.image_id]);
    if (left.snapshot.mask_image_id !== right.snapshot.mask_image_id) additions.push(['上传蒙版', left.snapshot.mask_image_id || '未使用', right.snapshot.mask_image_id || '未使用']);
    if (!same(left.snapshot.boxes, right.snapshot.boxes)) additions.push(['保留框', `${left.snapshot.boxes.length} 个（坐标不同）`, `${right.snapshot.boxes.length} 个（坐标不同）`]);
    for (const values of additions) {
      const row = document.createElement('tr');
      values.forEach((value, index) => {
        const cell = document.createElement(index === 0 ? 'th' : 'td');
        if (index === 0) cell.scope = 'row';
        cell.textContent = value;
        row.append(cell);
      });
      byId('diff-rows').append(row);
      count += 1;
    }
  }
  if (count) {
    empty.hidden = !incomplete;
    if (incomplete) empty.textContent = '参考参数快照不完整；这里只比较两侧均有记录的参数，缺失值不当作参数变化。';
    byId('diff-table-wrap').hidden = false;
  } else empty.textContent = incomplete ? '两侧共同记录的参数没有差异；参考参数快照不完整，未记录部分无法比较。'
    : '两侧记录的生成参数相同。此处始终比较模型快照，不读取当前编辑中的表单。';
}

async function selectResult(slot, key) {
  if (!key || !state.entries.has(key)) return;
  const sequence = ++state.selectionSequence[slot];
  state.pendingSelection[slot] = key;
  byId(`${slot}-result`).value = key;
  let entry = state.entries.get(key);
  comparison.cancelPending(slot);
  renderModelStatus(slot, { loading: true, label: entry.label, retainedLabel: state.selected[slot]?.label });
  try {
    if (entry.kind === 'run') {
      const job = await getRun(entry.id);
      if (state.disposed || sequence !== state.selectionSequence[slot]) return;
      entry = runEntry(job);
      state.entries.set(key, entry);
    }
    const ready = await comparison.setModel(slot, entry.model, entry.label);
    if (state.disposed || sequence !== state.selectionSequence[slot]) return;
    if (ready === true || !comparison.views.has(slot)) {
      state.selected[slot] = entry;
      renderEntry(slot, entry);
      renderDifferences();
      updateDraft();
    } else byId(`${slot}-result`).value = state.selected[slot]?.key || '';
  } catch (error) {
    if (state.disposed || sequence !== state.selectionSequence[slot]) return;
    renderModelStatus(slot, { error: error.message, retainedLabel: state.selected[slot]?.label });
    byId(`${slot}-result`).value = state.selected[slot]?.key || '';
  } finally {
    if (sequence === state.selectionSequence[slot]) state.pendingSelection[slot] = null;
  }
}

function renderHistory(preferredId = null) {
  const select = byId('history-select');
  const chosen = preferredId || select.value || state.activeId || state.runs[0]?.id || '';
  const signature = JSON.stringify(state.runs.map((job) => [job.id, job.status, job.title]));
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren();
    if (!state.runs.length) {
      const option = document.createElement('option');
      option.value = ''; option.textContent = '暂无运行记录'; select.append(option);
    }
    for (const job of state.runs) {
      const option = document.createElement('option');
      option.value = job.id;
      option.textContent = `${job.title || job.image_name || job.id} · ${STATUS_LABELS[job.status] || job.status}`;
      select.append(option);
    }
  }
  select.value = state.runs.some((job) => job.id === chosen) ? chosen : state.runs[0]?.id || '';
  select.disabled = !state.runs.length;
  const selected = state.runs.find((job) => job.id === select.value);
  byId('load-parameters').disabled = !selected;
  byId('show-history-result').disabled = selected?.status !== 'ready' || !selected?.files?.model;
}

async function showHistory(id = byId('history-select').value, knownJob = null) {
  if (!id) return;
  const epoch = taskEpoch.capture();
  const sequence = ++state.historySequence;
  const job = knownJob || await getRun(id);
  if (state.disposed || !taskEpoch.isCurrent(epoch) || sequence !== state.historySequence || id !== byId('history-select').value) return;
  byId('history-summary').textContent = [
    STATUS_LABELS[job.status] || job.status,
    job.image_name,
    timeLabel(job.created_at),
    `${ACTIVE_STATUSES.has(job.status) ? '已运行' : '耗时'} ${duration(elapsed(job))}`,
  ].filter(Boolean).join(' · ');
  byId('history-cache').textContent = cacheSummary(job.report);
  byId('history-error').hidden = !job.error;
  byId('history-error').textContent = job.error || '';
  byId('show-history-result').disabled = job.status !== 'ready' || !job.files?.model;
}

async function loadHistoryParameters() {
  const id = byId('history-select').value;
  if (!id) return;
  const epoch = parameterLoadEpoch.invalidate();
  const job = await parameterLoadEpoch.read(epoch, getRun(id));
  if (job === STALE_TASK_READ || state.disposed || id !== byId('history-select').value) return;
  if (!state.images.some((image) => image.id === job.image_id)) throw new Error('该记录使用的照片已不可用，无法载入参数。');
  state.parameters = clone(job.parameters);
  writeControls();
  setSourceImage(job.image_id, { boxes: job.boxes || [], maskId: job.mask_image_id || null });
  byId('run-title').value = '';
  setMobileView('parameters');
  notice('已载入这次运行的照片、保留框和参数；已有模型不变，点击生成可开始新的运行。');
}

function renderJob() {
  const job = state.activeJob;
  const badge = byId('run-state');
  const cancel = byId('cancel-button');
  cancel.hidden = !state.activeId;
  cancel.disabled = !job || job.status === 'cancelling' || state.cancellingRequest;
  cancel.textContent = job?.status === 'cancelling' ? '正在取消…' : '取消生成';
  if (!job) {
    badge.textContent = '就绪';
    badge.classList.remove('error');
    byId('job-message').textContent = '选择照片与参数，然后生成模型。';
    byId('job-detail').textContent = '一次运行一个任务；刷新页面可恢复运行状态。';
    updateDraft();
    return;
  }
  badge.textContent = STATUS_LABELS[job.status] || job.status;
  badge.classList.toggle('error', job.status === 'failed' || job.status === 'interrupted');
  const progress = job.progress || {};
  let message = progress.message || '任务正在运行，等待下一条进度。';
  if (job.status === 'ready') message = '模型生成完成，可在结果对照中查看。';
  else if (job.status === 'cancelled') message = '生成已取消；已完成的历史模型保留。';
  else if (job.status === 'cancelling') message = '已请求取消，等待当前计算退出。';
  else if (job.status === 'failed') message = `生成失败：${job.error || '请检查参数后重试。'}`;
  else if (job.status === 'interrupted') message = '运行已中断；可以载入这次参数后重新生成。';
  byId('job-message').textContent = message;
  const details = [];
  if (ACTIVE_STATUSES.has(job.status)) {
    if (progress.stage) details.push(`阶段：${progress.stage}`);
    if (finite(progress.current) && finite(progress.total) && progress.total > 0) details.push(`进度 ${progress.current} / ${progress.total}`);
    details.push(`${job.started_at ? '已运行' : '已等待'} ${duration(elapsed(job))}`);
    if (progress.cache_hit === true) details.push(progress.stage === 'inference' ? '当前视图复用深度'
      : ['segmentation', 'mask'].includes(progress.stage) ? '复用分割结果' : '当前步骤命中缓存');
  } else {
    details.push(`耗时 ${duration(elapsed(job))}`);
    const cache = cacheSummary(job.report);
    if (cache) details.push(cache);
  }
  byId('job-detail').textContent = details.join(' · ');
  updateDraft();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.disposed || !state.activeId) return;
  state.pollTimer = setTimeout(() => void refreshRuns().catch(showError), document.hidden ? 10000 : 2000);
}

function acceptFinishedRun(job) {
  if (ACTIVE_STATUSES.has(job.status) || !state.watched.has(job.id) || state.handled.has(job.id)) return;
  state.handled.add(job.id);
  if (job.status === 'ready' && job.files?.model) {
    refreshEntries();
    void selectResult('right', `run:${job.id}`);
    notice(`“${job.title || '新模型'}”已生成。${cacheSummary(job.report)}`);
  }
}

async function refreshRuns() {
  if (state.disposed || !state.token) return;
  const epoch = taskEpoch.capture();
  if (state.polling && state.pollingEpoch === epoch) return;
  const previousId = state.activeId;
  state.polling = true;
  state.pollingEpoch = epoch;
  clearTimeout(state.pollTimer);
  try {
    const response = await taskEpoch.read(epoch, api('/api/runs'));
    if (response === STALE_TASK_READ || state.disposed) return;
    const nextActiveId = response.active_run_id || null;
    let finished = null;
    let active = null;
    if (previousId && previousId !== nextActiveId) {
      finished = await taskEpoch.read(epoch, api(`/api/runs/${encodeURIComponent(previousId)}`));
      if (finished === STALE_TASK_READ || state.disposed) return;
    }
    if (nextActiveId) {
      active = await taskEpoch.read(epoch, api(`/api/runs/${encodeURIComponent(nextActiveId)}`));
      if (active === STALE_TASK_READ || state.disposed) return;
    }
    // Publish the list and its details together, after every read passed the epoch check.
    const nextId = active && !ACTIVE_STATUSES.has(active.status) ? null : nextActiveId;
    const nextJob = active || finished || state.activeJob;
    if (state.activeId !== nextId || state.activeJob?.id !== nextJob?.id || state.activeJob?.status !== nextJob?.status) taskEpoch.invalidate();
    const summaries = Array.isArray(response.runs) ? response.runs : [];
    state.runs = summaries.map((job) => ({ ...state.details.get(job.id), ...job }));
    for (const job of state.runs) state.details.set(job.id, job);
    if (finished) upsertRun(finished);
    if (active) upsertRun(active);
    state.activeId = nextId;
    state.activeJob = nextJob;
    if (nextActiveId) state.watched.add(nextActiveId);
    if (finished) acceptFinishedRun(finished);
    if (active) acceptFinishedRun(active);
    state.runs.sort((a, b) => Date.parse(b.created_at || '') - Date.parse(a.created_at || ''));
    refreshEntries();
    renderHistory();
    renderJob();
    const publishedEpoch = taskEpoch.capture();
    await showHistory();
    if (!taskEpoch.isCurrent(publishedEpoch) || state.disposed) return;
  } finally {
    if (state.pollingEpoch === epoch) {
      state.polling = false;
      state.pollingEpoch = null;
      schedulePoll();
    }
  }
}

function revealInvalidControl() {
  const form = byId('generation-form');
  const invalid = [...form.querySelectorAll('input, select')].find((input) => !input.disabled && !input.validity.valid);
  if (!invalid) return false;
  for (let parent = invalid.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
  setMobileView('parameters');
  invalid.focus();
  invalid.reportValidity();
  return true;
}

async function generate(event) {
  event.preventDefault();
  clearError();
  if (state.activeId || state.submitting) return;
  if (revealInvalidControl()) return;
  if (!state.imageId) throw new Error('请先选择或上传照片。');
  if (maskIsActive() && state.parameters.mask_mode === 'provided') {
    const image = currentImage();
    const mask = currentMask();
    if (!mask) { setMobileView('parameters'); byId('mask-select').focus(); throw new Error('请上传或选择黑白保留蒙版。'); }
    if (mask.width * image.height !== image.width * mask.height) throw new Error('蒙版与照片宽高比不同，请重新选择。');
  }
  const submitted = draftSnapshot();
  parameterLoadEpoch.invalidate();
  const title = byId('run-title').value.trim();
  const submittingEpoch = taskEpoch.invalidate();
  state.submitting = true;
  updateDraft();
  try {
    const created = await api('/api/runs', { method: 'POST', json: { ...submitted, ...(title ? { title } : {}) } });
    if (state.disposed) return;
    if (!created?.id) throw new Error('服务器没有返回运行编号，请刷新状态后再试。');
    state.watched.add(created.id);
    if (!taskEpoch.isCurrent(submittingEpoch)) {
      await refreshRuns();
      return;
    }
    const confirmedEpoch = taskEpoch.invalidate();
    state.activeId = created.id;
    state.activeJob = upsertRun(created);
    renderJob();
    renderHistory();
    byId('history-select').value = created.id;
    setMobileView('results');
    notice('任务已提交。生成参数已保存为快照，接下来修改表单不会改变这个任务。');
    schedulePoll();
    const detail = await taskEpoch.read(confirmedEpoch, api(`/api/runs/${encodeURIComponent(created.id)}`));
    if (detail === STALE_TASK_READ || state.disposed) return;
    if (state.activeJob?.status !== detail.status) taskEpoch.invalidate();
    state.activeJob = upsertRun(detail);
    if (!ACTIVE_STATUSES.has(detail.status)) {
      state.activeId = null;
      acceptFinishedRun(detail);
    }
    renderJob();
    renderHistory();
  } catch (error) {
    if (error.status === 409) await refreshRuns();
    throw error;
  } finally {
    state.submitting = false;
    if (!state.disposed) updateDraft();
    schedulePoll();
  }
}

async function cancelRun() {
  if (!state.activeId || state.activeJob?.status === 'cancelling' || state.cancellingRequest) return;
  const id = state.activeId;
  const cancellingEpoch = taskEpoch.invalidate();
  state.cancellingRequest = true;
  byId('cancel-button').disabled = true;
  try {
    const cancelled = await api(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
    if (state.disposed || !taskEpoch.isCurrent(cancellingEpoch)) return;
    taskEpoch.invalidate();
    state.activeJob = upsertRun(cancelled);
    if (!ACTIVE_STATUSES.has(cancelled.status)) {
      state.activeId = null;
      acceptFinishedRun(cancelled);
    }
    renderJob();
    renderHistory(cancelled.id);
    await showHistory(cancelled.id, cancelled);
    await refreshRuns();
  } finally {
    state.cancellingRequest = false;
    if (!state.disposed) renderJob();
    schedulePoll();
  }
}

function setMobileView(view) {
  byId('workspace').dataset.mobileView = view;
  byId('parameters-tab').setAttribute('aria-selected', String(view === 'parameters'));
  byId('results-tab').setAttribute('aria-selected', String(view === 'results'));
  if (compactViewport.matches) {
    byId('parameters-panel').setAttribute('aria-hidden', String(view !== 'parameters'));
    byId('results-panel').setAttribute('aria-hidden', String(view !== 'results'));
  } else {
    byId('parameters-panel').removeAttribute('aria-hidden');
    byId('results-panel').removeAttribute('aria-hidden');
  }
  if (view === 'results' || !compactViewport.matches) comparison.resize();
}

function pointerPosition(event) {
  const rect = byId('image-stage').getBoundingClientRect();
  return [Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))];
}

on(byId('image-stage'), 'pointerdown', (event) => {
  if (event.button !== 0 || !state.imageReady || !maskIsActive() || state.drag) return;
  if (state.boxes.length >= 8) { notice('最多保留 8 个框；可先删除或调整已有框。'); return; }
  parameterLoadEpoch.invalidate();
  event.preventDefault();
  const start = pointerPosition(event);
  state.drag = { pointerId: event.pointerId, start, box: [...start, ...start] };
  byId('image-stage').setPointerCapture(event.pointerId);
  drawBoxes();
});
on(byId('image-stage'), 'pointermove', (event) => {
  if (!state.drag || state.drag.pointerId !== event.pointerId) return;
  const [x, y] = pointerPosition(event);
  const [x0, y0] = state.drag.start;
  state.drag.box = [Math.min(x, x0), Math.min(y, y0), Math.max(x, x0), Math.max(y, y0)];
  drawBoxes();
});
on(byId('image-stage'), 'pointerup', (event) => {
  if (!state.drag || state.drag.pointerId !== event.pointerId) return;
  const box = state.drag.box;
  const rect = byId('image-stage').getBoundingClientRect();
  state.drag = null;
  if (byId('image-stage').hasPointerCapture(event.pointerId)) byId('image-stage').releasePointerCapture(event.pointerId);
  if ((box[2] - box[0]) * rect.width >= 4 && (box[3] - box[1]) * rect.height >= 4) {
    state.boxes.push(box.map((value) => Number(value.toFixed(6))));
  }
  renderBoxes();
  updateParameterState();
});
on(byId('image-stage'), 'pointercancel', () => { state.drag = null; drawBoxes(); });
on(byId('add-box'), 'click', () => {
  if (!state.imageId || !maskIsActive() || state.boxes.length >= 8) return;
  state.boxes.push([0.25, 0.25, 0.75, 0.75]);
  renderBoxes();
  byId('boxes-details').open = true;
  updateParameterState();
});
on(byId('clear-boxes'), 'click', () => { state.boxes = []; renderBoxes(); updateParameterState(); });
on(byId('source-image'), 'load', () => {
  state.imageReady = true;
  byId('source-image').hidden = false;
  byId('image-placeholder').hidden = true;
  byId('image-stage').classList.toggle('inactive', !maskIsActive());
});
on(byId('source-image'), 'error', () => {
  state.imageReady = false;
  byId('image-placeholder').hidden = false;
  byId('image-placeholder').textContent = '照片预览读取失败；可重新选择照片后重试。';
});
on(byId('mask-preview'), 'error', () => { byId('mask-preview').hidden = true; byId('mask-note').textContent = '蒙版预览读取失败，请重新选择或上传。'; });
on(byId('image-select'), 'change', (event) => setSourceImage(event.target.value));
on(byId('mask-select'), 'change', (event) => { state.maskId = event.target.value || null; updateMaskPreview(); updateDraft(); });
on(byId('image-file'), 'change', action(() => uploadFile('image', byId('image-file'))));
on(byId('mask-file'), 'change', action(() => uploadFile('mask', byId('mask-file'))));
// User changes invalidate pending history loads, including invalid input and
// file selection before its asynchronous upload completes. Rendering does not.
for (const event of ['input', 'change']) on(byId('generation-form'), event, () => parameterLoadEpoch.invalidate());
on(byId('generation-form'), 'click', (event) => {
  if (event.target.closest('button')) parameterLoadEpoch.invalidate();
});
on(byId('generation-form'), 'submit', (event) => { event.preventDefault(); void generate(event).catch(showError); });
on(byId('cancel-button'), 'click', action(cancelRun));
on(byId('parameters-tab'), 'click', () => setMobileView('parameters'));
on(byId('results-tab'), 'click', () => setMobileView('results'));
for (const id of ['parameters-tab', 'results-tab']) on(byId(id), 'keydown', (event) => {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  event.preventDefault();
  const view = id === 'parameters-tab' ? 'results' : 'parameters';
  setMobileView(view);
  byId(`${view}-tab`).focus();
});
on(compactViewport, 'change', () => setMobileView(byId('workspace').dataset.mobileView));
on(byId('display-front'), 'click', () => {
  comparison.setView('front');
  byId('display-front').setAttribute('aria-pressed', 'true');
  byId('display-angle').setAttribute('aria-pressed', 'false');
});
on(byId('display-angle'), 'click', () => {
  comparison.setView('angle');
  byId('display-front').setAttribute('aria-pressed', 'false');
  byId('display-angle').setAttribute('aria-pressed', 'true');
});
on(byId('display-texture'), 'change', (event) => comparison.setTexture(event.target.checked));
on(byId('display-height'), 'input', (event) => {
  const value = Number(event.target.value);
  byId('display-height-value').textContent = `${value.toFixed(1)}×`;
  comparison.setHeightScale(value);
});
on(byId('display-light'), 'input', (event) => {
  const value = Number(event.target.value);
  byId('display-light-value').textContent = `${value.toFixed(1)}×`;
  comparison.setLightIntensity(value);
});
for (const slot of ['left', 'right']) on(byId(`${slot}-result`), 'change', () => void selectResult(slot, byId(`${slot}-result`).value));
on(byId('history-select'), 'change', action(() => { parameterLoadEpoch.invalidate(); renderHistory(); return showHistory(); }));
on(byId('load-parameters'), 'click', action(loadHistoryParameters));
on(byId('show-history-result'), 'click', action(() => {
  const id = byId('history-select').value;
  if (!id) return;
  setMobileView('results');
  return selectResult('right', `run:${id}`);
}));
on(byId('history-refresh'), 'click', action(async () => { clearError(); await refreshRuns(); }));
on(byId('refresh-state'), 'click', action(async () => {
  clearError();
  if (state.config && state.token) await refreshRuns();
  else await bootstrap();
}));
on(document, 'visibilitychange', () => {
  if (document.hidden) schedulePoll();
  else { comparison.resize(); void refreshRuns().catch(showError); }
});
on(window, 'pagehide', () => {
  state.disposed = true;
  taskEpoch.invalidate();
  parameterLoadEpoch.invalidate();
  clearTimeout(state.pollTimer);
  lifecycle.abort();
  comparison.dispose();
  for (const url of Object.values(state.downloadBlobs)) if (url) URL.revokeObjectURL(url);
});
window.addEventListener('pageshow', (event) => {
  if (event.persisted && state.disposed) window.location.reload();
});

async function bootstrap() {
  const epoch = taskEpoch.invalidate();
  const payload = await taskEpoch.read(epoch, api('/api/bootstrap'));
  if (payload === STALE_TASK_READ || state.disposed) return;
  let restored = null;
  if (payload.active_run_id) {
    restored = await taskEpoch.read(epoch, api(`/api/runs/${encodeURIComponent(payload.active_run_id)}`));
    if (restored === STALE_TASK_READ || state.disposed) return;
  }
  state.token = payload.token;
  state.config = payload.config;
  state.parameters = clone(payload.config.defaults);
  state.images = payload.images || [];
  state.references = payload.references || [];
  state.runs = payload.runs || [];
  for (const job of state.runs) state.details.set(job.id, job);
  state.activeId = payload.active_run_id || null;
  state.controls.clear();
  state.fields.clear();
  byId('parameter-groups').replaceChildren();
  byId('preset-buttons').replaceChildren();
  createParameterControls();
  let imageId = payload.default_image_id || state.images.find((image) => image.kind === 'image')?.id || null;
  if (restored) {
    state.watched.add(state.activeId);
    state.activeJob = upsertRun(restored);
    if (!ACTIVE_STATUSES.has(restored.status)) state.activeId = null;
    state.parameters = clone(restored.parameters);
    writeControls();
    imageId = restored.image_id;
  }
  setSourceImage(imageId, restored ? { boxes: restored.boxes, maskId: restored.mask_image_id } : {});
  refreshEntries();
  renderHistory();
  renderJob();
  setMobileView(state.activeId ? 'results' : 'parameters');
  const original = state.references.find((reference) => reference.id === 'original') || state.references[0];
  const latest = state.runs.find((job) => job.status === 'ready' && job.files?.model);
  const fine = state.references.find((reference) => reference.id === 'fine') || state.references[1] || original;
  schedulePoll();
  const selections = [];
  if (original) selections.push(selectResult('left', `reference:${original.id}`));
  else if (latest) selections.push(selectResult('left', `run:${latest.id}`));
  if (latest) selections.push(selectResult('right', `run:${latest.id}`));
  else if (fine) selections.push(selectResult('right', `reference:${fine.id}`));
  selections.push(showHistory());
  await Promise.allSettled(selections);
}

bootstrap().catch((error) => {
  showError(error);
  byId('run-state').textContent = '连接失败';
  byId('run-state').classList.add('error');
  byId('job-message').textContent = '无法读取本地实验台；可点击“刷新状态”重新连接。';
});
