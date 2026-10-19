import { ReliefComparison } from './lab_viewer.js';
import { STALE_TASK_READ, TaskEpoch, flagValue, snapshotChanges, totalElapsedSeconds } from './lab_state.js';

const byId = (id) => document.getElementById(id);
const lifecycle = new AbortController();
const taskEpoch = new TaskEpoch();
const parameterLoadEpoch = new TaskEpoch();
const compactViewport = window.matchMedia('(max-width: 980px)');
const ACTIVE_STATUSES = new Set(['running', 'cancelling']);
const STATUS_LABELS = { running: '生成中', cancelling: '正在取消', cancelled: '已取消', ready: '已完成', failed: '失败', interrupted: '已中断' };
const BASELINE_STORAGE_KEY = 'memoverse-relief-lab:baseline-run';
const INPUT_CHANGE_LABELS = { image: '输入照片', mask: '上传蒙版', boxes: '保留框' };
const state = {
  token: null, config: null, parameters: {}, images: [], runs: [], baselineId: null,
  fields: new Map(), controls: new Map(), groupChanges: new Map(), details: new Map(), detailRequests: new Map(), entries: new Map(),
  imageId: null, maskId: null, boxes: [], imageReady: false, drag: null,
  activeId: null, activeJob: null, watched: new Set(), handled: new Set(),
  selected: { left: null, right: null }, selectionSequence: { left: 0, right: 0 }, baselineChoice: null,
  downloadBlobs: { left: null, right: null },
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
    image_name: job.image_name ?? null,
    mask_image_id: job.mask_image_id ?? null,
    parameters: clone(job.parameters ?? {}),
    boxes: clone(job.boxes ?? []),
  };
}

function draftSnapshot() {
  return { image_id: state.imageId, mask_image_id: maskIsActive() && state.parameters.mask_mode === 'provided' ? state.maskId : null, parameters: clone(state.parameters), boxes: clone(state.boxes) };
}

const viewable = (job) => job?.status === 'ready' && Boolean(job.files?.model);
const mainPreset = () => state.config?.presets.find((preset) => preset.id === 'main') || null;

function chronological(runs) {
  return [...runs].sort((a, b) => (Date.parse(a.created_at || '') || 0) - (Date.parse(b.created_at || '') || 0)
    || String(a.id).localeCompare(String(b.id)));
}

function runNumbers() {
  return new Map(chronological(state.runs).map((job, index) => [job.id, index + 1]));
}

function runLabel(job, numbers = runNumbers()) {
  const number = numbers.get(job.id);
  return [number ? `#${number}` : null, job.title || job.image_name || '生成结果'].filter(Boolean).join(' · ');
}

function storedBaseline() {
  try { return window.localStorage.getItem(BASELINE_STORAGE_KEY); } catch { return null; }
}

function resolveBaselineId() {
  const stored = state.baselineChoice || storedBaseline();
  if (stored && viewable(state.runs.find((job) => job.id === stored))) return stored;
  const main = mainPreset();
  if (!main) return null;
  return chronological(state.runs).find((job) => viewable(job)
    && state.config.fields.every((field) => same(job.parameters?.[field.key], main.parameters[field.key])))?.id ?? null;
}

// Without a baseline run, parameters are still compared with the main-branch preset; inputs are not.
function baselineSnapshot() {
  const run = state.runs.find((job) => job.id === state.baselineId);
  if (run) return snapshot(run);
  const main = mainPreset();
  return main ? { parameters: clone(main.parameters) } : null;
}

function syncBaseline() {
  const next = resolveBaselineId();
  if (next === state.baselineId) return;
  state.baselineId = next;
  renderDifferences();
  if (next) void selectResult('left', `run:${next}`);
}

async function setBaseline(id) {
  state.baselineChoice = id;
  try { window.localStorage.setItem(BASELINE_STORAGE_KEY, id); } catch { notice('浏览器未能保存基准选择；本次页面内仍然生效。'); }
  state.baselineId = id;
  renderHistory();
  renderDifferences();
  updateDraft();
  await selectResult('left', `run:${id}`);
}

function changeFlag(change) {
  if (change.key === 'mask') return `--mask ${change.after ? '已上传' : 'none'}`;
  if (change.key === 'boxes') return `--boxes ${change.after}`;
  return `--${change.key} ${flagValue(change.field, change.after)}`;
}

function changeValues(change) {
  if (change.field) return [formatParameter(change.field, change.before), formatParameter(change.field, change.after)];
  if (change.key === 'mask') return [change.before ? '已上传蒙版' : '未使用', change.after ? '已上传蒙版' : '未使用'];
  if (change.key === 'boxes') return [`${change.before} 个`, `${change.after} 个（坐标不同）`];
  return [String(change.before), String(change.after)];
}

function flagElements(changes) {
  return changes.map((change) => {
    const flag = document.createElement('code');
    flag.className = 'flag';
    flag.textContent = changeFlag(change);
    const [before, after] = changeValues(change);
    flag.title = `${change.field?.label || INPUT_CHANGE_LABELS[change.key]}：${before} → ${after}`;
    return flag;
  });
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
  renderDraftChanges(draft, invalid);
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

function renderDraftChanges(draft, invalid) {
  const base = baselineSnapshot();
  byId('draft-diff-label').textContent = state.baselineId ? `相对基准 #${runNumbers().get(state.baselineId)}` : '相对主分支默认';
  const changes = base && !invalid ? snapshotChanges(base, { ...draft, image_name: currentImage()?.name }, state.config.fields) : [];
  const target = byId('draft-diff');
  if (!base) target.textContent = '—';
  else if (invalid) target.textContent = '参数待检查';
  else if (!changes.length) target.textContent = '无改动';
  else target.replaceChildren(...flagElements(changes));
  const changed = new Set(changes.map((change) => change.key));
  const counts = new Map();
  for (const [key, input] of state.controls) {
    const isChanged = changed.has(key);
    input.closest('.param-field').classList.toggle('changed', isChanged);
    if (isChanged) counts.set(state.fields.get(key).group, (counts.get(state.fields.get(key).group) || 0) + 1);
  }
  for (const [group, element] of state.groupChanges) {
    element.textContent = `改动 ${counts.get(group) || 0}`;
    element.hidden = !counts.get(group);
  }
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
  byId('region-state').textContent = !active ? '未启用（背景压平 0%）' : provided ? '使用上传蒙版' : '自动识别 + 保留框';
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
  for (const group of state.config.groups) {
    const details = document.createElement('details');
    details.className = 'parameter-group';
    details.open = group.id === state.config.groups[0].id || group.id === 'shape';
    const summary = document.createElement('summary');
    const title = document.createElement('span');
    title.textContent = group.label;
    const changes = document.createElement('span');
    changes.className = 'group-changes';
    changes.hidden = true;
    state.groupChanges.set(group.id, changes);
    summary.append(title, changes);
    const description = document.createElement('p');
    description.className = 'group-description';
    description.textContent = group.description;
    details.append(summary, description);
    for (const field of state.config.fields.filter((candidate) => candidate.group === group.id)) {
      state.fields.set(field.key, field);
      const wrapper = document.createElement('div');
      wrapper.className = `param-field${field.kind === 'boolean' ? ' is-boolean' : ''}${['choice', 'scales'].includes(field.kind) ? ' stacked' : ''}`;
      const row = document.createElement('div');
      row.className = 'param-row';
      const label = document.createElement('label');
      label.htmlFor = `parameter-${field.key}`;
      label.textContent = `${field.label}${field.unit ? `（${field.unit}）` : ''}`;
      const input = document.createElement(field.kind === 'choice' ? 'select' : 'input');
      input.id = label.htmlFor;
      input.name = field.key;
      input.setAttribute('aria-describedby', `${input.id}-effect ${input.id}-help`);
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
      const note = document.createElement('details');
      note.className = 'param-note';
      const effect = document.createElement('summary');
      effect.id = `${input.id}-effect`;
      effect.textContent = `效果：${field.effect}`;
      const help = document.createElement('p');
      help.id = `${input.id}-help`;
      help.className = 'param-meaning';
      help.textContent = `含义：${field.help}`;
      note.append(effect, help);
      row.append(label, input);
      wrapper.append(row, note);
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
  const thumb = byId('source-thumb');
  thumb.hidden = !image;
  if (image) {
    byId('image-stage').style.aspectRatio = `${image.width} / ${image.height}`;
    try {
      element.src = localUrl(image.preview_url);
      thumb.src = element.src;
    } catch (error) { showError(error); }
  } else {
    element.removeAttribute('src');
    thumb.removeAttribute('src');
  }
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
    key: `run:${job.id}`, id: job.id, label: runLabel(job),
    model: job.files?.model, parameters: clone(job.parameters || {}), snapshot: snapshot(job), job,
  };
}

function refreshEntries() {
  state.entries = new Map(state.runs.filter(viewable).map((job) => [`run:${job.id}`, runEntry(job)]));
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
  byId(`${slot}-summary`).textContent = [entry.job.image_name, timeLabel(entry.job.created_at),
    `生成耗时 ${duration(elapsed(entry.job))}`, cacheSummary(entry.job.report)].filter(Boolean).join(' · ');
  setDownload(byId(`${slot}-model-download`), entry.model, `${entry.id}.glb`);
  if (state.downloadBlobs[slot]) {
    URL.revokeObjectURL(state.downloadBlobs[slot]);
    state.downloadBlobs[slot] = null;
  }
  const parameterLink = byId(`${slot}-parameters-download`);
  if (entry.job.files?.parameters) setDownload(parameterLink, entry.job.files.parameters, `${entry.id}-parameters.json`);
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
  const base = state.baselineId ? baselineSnapshot() : null;
  const right = state.selected.right;
  const empty = byId('diff-empty');
  const list = byId('diff-list');
  list.replaceChildren();
  list.hidden = true;
  empty.hidden = false;
  if (!base || !right) { empty.textContent = '基准和这一轮都有模型后，这里列出不同的生成参数。'; return; }
  if (state.baselineId === right.id) { empty.textContent = '两侧是同一轮结果。生成新的一轮，或在下方历史中选择其他结果放到右侧。'; return; }
  const changes = snapshotChanges(base, right.snapshot, state.config.fields);
  if (!changes.length) { empty.textContent = '两侧记录的生成参数与输入相同；差异只可能来自重新推理的微小数值波动。'; return; }
  const flags = flagElements(changes);
  for (const [index, change] of changes.entries()) {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = change.field?.label || INPUT_CHANGE_LABELS[change.key];
    const values = document.createElement('span');
    values.className = 'diff-values';
    const [before, after] = changeValues(change);
    const removed = document.createElement('del');
    removed.textContent = before;
    const added = document.createElement('ins');
    added.textContent = after;
    values.append(removed, ' → ', added);
    item.append(flags[index], name, values);
    if (change.field?.effect) {
      const effect = document.createElement('span');
      effect.className = 'muted small';
      effect.textContent = change.field.effect;
      item.append(effect);
    }
    list.append(item);
  }
  empty.hidden = true;
  list.hidden = false;
}

async function selectResult(slot, key) {
  if (!key || !state.entries.has(key)) return;
  const sequence = ++state.selectionSequence[slot];
  let entry = state.entries.get(key);
  comparison.cancelPending(slot);
  renderModelStatus(slot, { loading: true, label: entry.label, retainedLabel: state.selected[slot]?.label });
  try {
    const job = await getRun(entry.id);
    if (state.disposed || sequence !== state.selectionSequence[slot]) return;
    entry = runEntry(job);
    state.entries.set(key, entry);
    const ready = await comparison.setModel(slot, entry.model, entry.label);
    if (state.disposed || sequence !== state.selectionSequence[slot]) return;
    if (ready === true || !comparison.views.has(slot)) {
      state.selected[slot] = entry;
      renderEntry(slot, entry);
      renderDifferences();
      renderHistory();
      updateDraft();
      return true;
    }
    return ready === null ? undefined : false;
  } catch (error) {
    if (state.disposed || sequence !== state.selectionSequence[slot]) return;
    renderModelStatus(slot, { error: error.message, retainedLabel: state.selected[slot]?.label });
    return false;
  }
}

function historyCell(row, ...content) {
  const cell = document.createElement('td');
  cell.append(...content);
  row.append(cell);
  return cell;
}

function historyButton(kind, text, label, disabled) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'text-button';
  button.dataset.action = kind;
  button.textContent = text;
  button.setAttribute('aria-label', label);
  button.disabled = disabled;
  return button;
}

function renderHistory() {
  const numbers = runNumbers();
  const base = baselineSnapshot();
  const rightId = state.selected.right?.id;
  const signature = JSON.stringify([state.baselineId, rightId, base,
    state.runs.map((job) => [job.id, job.status, job.title, job.error, ACTIVE_STATUSES.has(job.status) ? null : job.elapsed_seconds, Boolean(job.files?.model)])]);
  const rows = byId('history-rows');
  if (rows.dataset.signature === signature) return;
  rows.dataset.signature = signature;
  const focused = document.activeElement?.closest?.('#history-rows button[data-action]');
  const focus = focused ? [focused.closest('tr').dataset.runId, focused.dataset.action] : null;
  rows.replaceChildren();
  for (const job of state.runs) {
    const number = numbers.get(job.id);
    const isBaseline = job.id === state.baselineId;
    const row = document.createElement('tr');
    row.dataset.runId = job.id;
    row.classList.toggle('is-baseline', isBaseline);
    row.classList.toggle('is-right', job.id === rightId);
    const numberText = document.createElement('span');
    numberText.className = 'run-number';
    numberText.textContent = `#${number}`;
    const first = historyCell(row, numberText);
    for (const [shown, text, className] of [[isBaseline, '基准', 'row-tag baseline'], [job.id === rightId, '右侧', 'row-tag']]) {
      if (!shown) continue;
      const tag = document.createElement('span');
      tag.className = className;
      tag.textContent = text;
      first.append(tag);
    }
    if (job.title) {
      const title = document.createElement('span');
      title.className = 'row-title';
      title.textContent = job.title;
      first.append(title);
    }
    historyCell(row, timeLabel(job.created_at));
    historyCell(row, STATUS_LABELS[job.status] || job.status).className = `status-${job.status}`;
    historyCell(row, ACTIVE_STATUSES.has(job.status) ? '—' : duration(elapsed(job)));
    const changes = isBaseline ? null : snapshotChanges(base, snapshot(job), state.config.fields);
    const changeCell = historyCell(row);
    if (isBaseline) changeCell.textContent = '基准本身';
    else if (changes.length) changeCell.append(...flagElements(changes));
    else {
      const sameText = document.createElement('span');
      sameText.className = 'flag-same';
      sameText.textContent = state.baselineId ? '与基准相同' : '与主分支默认相同';
      changeCell.append(sameText);
    }
    if (job.error) {
      const error = document.createElement('p');
      error.className = 'row-error';
      error.textContent = job.error;
      changeCell.append(error);
    }
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.append(
      historyButton('baseline', '设为基准', `把 #${number} 设为基准`, !viewable(job) || isBaseline),
      historyButton('view', '右侧查看', `在右侧查看 #${number}`, !viewable(job) || job.id === rightId),
      historyButton('load', '载入参数', `把 #${number} 的参数载入左侧`, !job.parameters),
    );
    historyCell(row, actions);
    rows.append(row);
  }
  if (focus) rows.querySelector(`tr[data-run-id="${CSS.escape(focus[0])}"] button[data-action="${focus[1]}"]`)?.focus();
  byId('history-empty').hidden = state.runs.length > 0;
  byId('history-table-wrap').hidden = !state.runs.length;
  byId('history-count').textContent = state.runs.length ? `共 ${state.runs.length} 轮` : '';
}

async function loadHistoryParameters(id) {
  const epoch = parameterLoadEpoch.invalidate();
  const job = await parameterLoadEpoch.read(epoch, getRun(id));
  if (job === STALE_TASK_READ || state.disposed) return;
  if (!state.images.some((image) => image.id === job.image_id)) throw new Error('该记录使用的照片已不可用，无法载入参数。');
  state.parameters = clone(job.parameters);
  writeControls();
  setSourceImage(job.image_id, { boxes: job.boxes || [], maskId: job.mask_image_id || null });
  byId('run-title').value = '';
  setMobileView('parameters');
  notice(`已载入 #${runNumbers().get(id)} 的照片、保留框和参数；已有模型不变，点击生成可开始新的一轮。`);
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
  if (viewable(job)) {
    refreshEntries();
    syncBaseline();
    const label = runLabel(job);
    if (job.id === state.baselineId) notice(`${label} 已生成，并自动设为基准；之后每一轮都和它对比。`);
    else {
      void selectResult('right', `run:${job.id}`).then((shown) => {
        if (shown === true) notice(`${label} 已生成，显示在右侧。${cacheSummary(job.report)}`);
        else if (shown === false) notice(`${label} 已生成，但右侧模型未能载入；可在下方历史中点“右侧查看”重试。`);
      });
    }
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
    syncBaseline();
    renderHistory();
    renderJob();
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
    if (!mask) { setMobileView('parameters'); byId('region-details').open = true; byId('mask-select').focus(); throw new Error('请上传或选择黑白保留蒙版。'); }
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
    renderHistory();
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
on(byId('source-thumb'), 'error', () => { byId('source-thumb').hidden = true; });
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
on(byId('history-rows'), 'click', action((event) => {
  const button = event.target.closest('button[data-action]');
  if (!button || button.disabled) return;
  const id = button.closest('tr').dataset.runId;
  if (button.dataset.action === 'load') return loadHistoryParameters(id);
  setMobileView('results');
  if (button.dataset.action === 'baseline') return setBaseline(id);
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
const runBarObserver = new ResizeObserver(() => {
  document.documentElement.style.setProperty('--run-bar-height', `${Math.ceil(byId('run-bar').getBoundingClientRect().height)}px`);
});
runBarObserver.observe(byId('run-bar'));
on(window, 'pagehide', () => {
  state.disposed = true;
  runBarObserver.disconnect();
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
  state.runs = payload.runs || [];
  for (const job of state.runs) state.details.set(job.id, job);
  state.activeId = payload.active_run_id || null;
  state.controls.clear();
  state.fields.clear();
  state.groupChanges.clear();
  byId('parameter-groups').replaceChildren();
  byId('preset-buttons').replaceChildren();
  createParameterControls();
  state.baselineId = resolveBaselineId();
  let imageId = payload.default_image_id || state.images.find((image) => image.kind === 'image')?.id || null;
  if (restored) {
    state.watched.add(state.activeId);
    state.activeJob = upsertRun(restored);
    if (!ACTIVE_STATUSES.has(restored.status)) state.activeId = null;
    state.parameters = clone(restored.parameters);
    writeControls();
    imageId = restored.image_id;
  } else if (!state.baselineId && mainPreset()) {
    state.parameters = clone(mainPreset().parameters);
    writeControls();
    notice('还没有基准：已填入“主分支默认”参数，点击生成后这一轮会自动成为基准。');
  }
  setSourceImage(imageId, restored ? { boxes: restored.boxes, maskId: restored.mask_image_id } : {});
  refreshEntries();
  renderHistory();
  renderJob();
  setMobileView(state.activeId ? 'results' : 'parameters');
  const latest = state.runs.find((job) => viewable(job) && job.id !== state.baselineId);
  schedulePoll();
  const selections = [];
  if (state.baselineId) selections.push(selectResult('left', `run:${state.baselineId}`));
  if (latest) selections.push(selectResult('right', `run:${latest.id}`));
  await Promise.allSettled(selections);
}

bootstrap().catch((error) => {
  showError(error);
  byId('run-state').textContent = '连接失败';
  byId('run-state').classList.add('error');
  byId('job-message').textContent = '无法读取本地实验台；可点击“刷新状态”重新连接。';
});
