import * as THREE from '../../demo/node_modules/three/build/three.module.js';
import { OrbitControls } from '../../demo/node_modules/three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from '../../demo/node_modules/three/examples/jsm/loaders/GLTFLoader.js';

const byId = (id) => document.getElementById(id);
const baseUrl = new URL('./', window.location.href);
const lifecycle = new AbortController();
const viewers = [];
const frontDirection = new THREE.Vector3(0, 0, 1);
const maxAngle = THREE.MathUtils.degToRad(40);
const pageStatus = byId('page-status');
let disposed = false;
let syncing = false;
let renderFrame = null;
let cameraDistance = 3;
let sceneWidth = 2;
let sceneHeight = 1.5;
let sceneDepth = 0.3;

function localUrl(value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`结果缺少 ${field} 文件路径。`);
  }
  const url = new URL(value, baseUrl);
  if (url.origin !== baseUrl.origin || !url.pathname.startsWith(baseUrl.pathname)) {
    throw new Error(`${field} 必须指向当前结果目录内的文件。`);
  }
  return url;
}

async function getFile(url, signal = lifecycle.signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`文件读取失败（HTTP ${response.status}）。`);
  return response;
}

function showStatus(element, message, error = false) {
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
}

function requestRender() {
  if (disposed || renderFrame !== null) return;
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null;
    if (disposed) return;
    for (const viewer of viewers) viewer.renderer.render(viewer.scene, viewer.camera);
  });
}

function clampDirection(camera, target) {
  const offset = camera.position.clone().sub(target);
  const distance = offset.length();
  if (distance === 0) return;
  const direction = offset.divideScalar(distance);
  if (direction.angleTo(frontDirection) > maxAngle) {
    const planar = new THREE.Vector2(direction.x, direction.y);
    if (planar.lengthSq() === 0) planar.x = 1;
    planar.normalize().multiplyScalar(Math.sin(maxAngle));
    direction.set(planar.x, planar.y, Math.cos(maxAngle));
    camera.position.copy(target).addScaledVector(direction, distance);
    camera.lookAt(target);
  }
}

function syncFrom(source, customView = true) {
  if (disposed || syncing) return;
  syncing = true;
  clampDirection(source.camera, source.controls.target);
  source.controls.update();
  for (const viewer of viewers) {
    if (viewer === source) continue;
    viewer.camera.position.copy(source.camera.position);
    viewer.camera.quaternion.copy(source.camera.quaternion);
    viewer.camera.zoom = source.camera.zoom;
    viewer.controls.target.copy(source.controls.target);
    viewer.camera.updateProjectionMatrix();
    viewer.controls.update();
  }
  syncing = false;
  if (customView) {
    byId('front-view').setAttribute('aria-pressed', 'false');
    byId('angled-view').setAttribute('aria-pressed', 'false');
  }
  requestRender();
}

function setView(angled) {
  if (!viewers.length || disposed) return;
  const camera = viewers[0].camera;
  const direction = angled
    ? new THREE.Vector3(0.56, 0.20, 0.80).normalize()
    : frontDirection;
  viewers[0].controls.target.set(0, 0, 0);
  camera.position.copy(direction).multiplyScalar(cameraDistance);
  camera.zoom = 1;
  camera.updateProjectionMatrix();
  syncFrom(viewers[0], false);
  byId('front-view').setAttribute('aria-pressed', String(!angled));
  byId('angled-view').setAttribute('aria-pressed', String(angled));
}

function resize() {
  if (disposed || !viewers.length) return;
  cameraDistance = Math.max(sceneWidth, sceneHeight, sceneDepth * 3, 0.01) * 3;
  for (const viewer of viewers) {
    const width = Math.max(1, viewer.container.clientWidth);
    const height = Math.max(1, viewer.container.clientHeight);
    const aspect = width / height;
    const halfHeight = Math.max(sceneHeight / 2, sceneWidth / (2 * aspect)) * 1.15;
    viewer.renderer.setSize(width, height, false);
    viewer.camera.left = -halfHeight * aspect;
    viewer.camera.right = halfHeight * aspect;
    viewer.camera.top = halfHeight;
    viewer.camera.bottom = -halfHeight;
    // Distance only keeps the complete relief inside the clipping planes.
    viewer.camera.near = cameraDistance / 1000;
    viewer.camera.far = cameraDistance * 4;
    viewer.camera.position.setLength(cameraDistance);
    viewer.camera.updateProjectionMatrix();
  }
  syncFrom(viewers[0], false);
}

const resizeObserver = new ResizeObserver(resize);

function disposeModel(root, extraMaterials = [], preservedMaterials = []) {
  const geometries = new Set();
  const materials = new Set(extraMaterials);
  const textures = new Set();
  root.traverse((object) => {
    if (object.geometry) geometries.add(object.geometry);
    for (const material of [].concat(object.material ?? [])) materials.add(material);
    for (const material of [].concat(object.userData.originalMaterial ?? [])) materials.add(material);
  });
  for (const material of preservedMaterials) materials.delete(material);
  for (const material of materials) {
    for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
  }
  const images = new Set();
  for (const texture of textures) {
    if (texture.source?.data) images.add(texture.source.data);
    texture.dispose();
  }
  for (const image of images) if (typeof image.close === 'function') image.close();
  for (const material of materials) material.dispose();
  for (const geometry of geometries) geometry.dispose();
}

function createViewer(prefix, label) {
  const containerId = `${prefix}-viewport`;
  const statusId = `${prefix}-status`;
  const container = byId(containerId);
  const status = byId(statusId);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setClearColor(0xe9ede8, 1);
  const canvas = renderer.domElement;
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', `${label}；方向键旋转，加减键缩放，正面和斜侧按钮重置视角`);
  container.prepend(canvas);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.001, 100);
  camera.position.set(0, 0, cameraDistance);
  const controls = new OrbitControls(camera, canvas);
  controls.enabled = false;
  controls.enableDamping = false;
  controls.enablePan = false;
  controls.rotateSpeed = 0.65;
  controls.zoomSpeed = 0.65;
  controls.minZoom = 0.5;
  controls.maxZoom = 4;
  controls.minPolarAngle = Math.PI / 2 - maxAngle;
  controls.maxPolarAngle = Math.PI / 2 + maxAngle;
  controls.minAzimuthAngle = -maxAngle;
  controls.maxAzimuthAngle = maxAngle;
  scene.add(new THREE.AmbientLight(0xffffff, 0.65));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.5);
  keyLight.position.set(-3, 2, 1.4);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xe4edff, 0.5);
  fillLight.position.set(3, -1, 3);
  scene.add(fillLight);
  const neutral = new THREE.MeshStandardMaterial({ color: 0xd9ddd4, roughness: 0.88, metalness: 0, side: THREE.DoubleSide });
  const viewer = {
    prefix, container, status, renderer, scene, camera, controls, neutral,
    model: null, variant: null, generation: 0, request: null, loading: false, error: false,
  };
  viewers.push(viewer);
  const onChange = () => syncFrom(viewer);
  controls.addEventListener('change', onChange);
  viewer.onChange = onChange;
  canvas.addEventListener('keydown', (event) => {
    if (!viewer.model || event.ctrlKey || event.metaKey || event.altKey) return;
    const offset = camera.position.clone().sub(controls.target);
    const spherical = new THREE.Spherical().setFromVector3(offset);
    const step = THREE.MathUtils.degToRad(5);
    if (event.key === 'ArrowLeft') spherical.theta -= step;
    else if (event.key === 'ArrowRight') spherical.theta += step;
    else if (event.key === 'ArrowUp') spherical.phi -= step;
    else if (event.key === 'ArrowDown') spherical.phi += step;
    else if (event.key === '+' || event.key === '=') camera.zoom *= 1.1;
    else if (event.key === '-' || event.key === '−') camera.zoom /= 1.1;
    else return;
    event.preventDefault();
    camera.zoom = THREE.MathUtils.clamp(camera.zoom, controls.minZoom, controls.maxZoom);
    camera.updateProjectionMatrix();
    camera.position.setFromSpherical(spherical).add(controls.target);
    syncFrom(viewer);
  }, { signal: lifecycle.signal });
  canvas.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    showStatus(status, '三维绘图上下文已丢失，请刷新页面重新载入。', true);
  }, { signal: lifecycle.signal });
  resizeObserver.observe(container);
  return viewer;
}

function updatePageStatus() {
  if (disposed) return;
  const ready = viewers.filter((viewer) => viewer.model).length;
  const loading = viewers.some((viewer) => viewer.loading);
  const failed = viewers.some((viewer) => viewer.error) || viewers.length < 2;
  const message = loading ? '正在载入所选模型；已载入的模型仍可查看。'
    : failed ? `当前可查看 ${ready} / 2 个模型；请查看面板中的加载提示。`
      : '两个模型已载入 · 两侧使用相同正交视角与高度倍率。';
  showStatus(pageStatus, message, failed && !loading);
}

function fitModels() {
  const ready = viewers.filter((viewer) => viewer.model);
  if (!ready.length) return;
  sceneWidth = Math.max(...ready.map((viewer) => viewer.size.x));
  sceneHeight = Math.max(...ready.map((viewer) => viewer.size.y));
  sceneDepth = Math.max(...ready.map((viewer) => viewer.size.z));
  resize();
  for (const viewer of ready) viewer.controls.enabled = true;
  for (const id of ['front-view', 'angled-view', 'texture-toggle', 'height-scale']) byId(id).disabled = false;
}

function setModelLabel(viewer, variant) {
  byId(`${viewer.prefix}-title`).textContent = variant.label;
  if (viewer.selector) {
    viewer.selector.value = variant.id;
    byId(`${viewer.prefix}-description`).textContent = typeof variant.title === 'string' ? variant.title
      : variant.id === 'original' ? '原流程整图深度，作为参考。' : '保留整图主体起伏，仅将背景铺平。';
  }
  viewer.renderer.domElement.setAttribute('aria-label', `${variant.label}模型；方向键旋转，加减键缩放，正面和斜侧按钮重置视角`);
}

async function loadModel(viewer, variant) {
  viewer.request?.abort();
  const request = new AbortController();
  const generation = ++viewer.generation;
  viewer.request = request;
  viewer.loading = true;
  viewer.error = false;
  viewer.container.setAttribute('aria-busy', 'true');
  viewer.status.classList.toggle('has-model', Boolean(viewer.model));
  setModelLabel(viewer, variant);
  const retained = viewer.variant ? `；暂时显示“${viewer.variant.label}”` : '';
  showStatus(viewer.status, `正在载入“${variant.label}”${retained}…`);
  updatePageStatus();
  let model = null;
  try {
    const url = localUrl(variant.model, variant.label);
    const response = await getFile(url, request.signal);
    const data = await response.arrayBuffer();
    if (disposed || generation !== viewer.generation) return;
    const gltf = await new GLTFLoader().parseAsync(data, new URL('./', url).href);
    model = gltf.scene;
    if (disposed || generation !== viewer.generation) {
      disposeModel(model);
      return;
    }
    const bounds = new THREE.Box3().setFromObject(model);
    if (bounds.isEmpty()) throw new Error('模型没有可显示的几何表面。');
    const center = bounds.getCenter(new THREE.Vector3());
    const size = bounds.getSize(new THREE.Vector3());
    model.position.x -= center.x;
    model.position.y -= center.y;
    model.scale.z = Number(byId('height-scale').value);
    const textured = byId('texture-toggle').checked;
    model.traverse((object) => {
      if (!object.isMesh) return;
      object.userData.originalMaterial = object.material;
      if (!textured) object.material = viewer.neutral;
    });
    const previousModel = viewer.model;
    viewer.scene.add(model);
    viewer.model = model;
    viewer.variant = variant;
    viewer.size = size;
    if (previousModel) {
      viewer.scene.remove(previousModel);
      disposeModel(previousModel, [], [viewer.neutral]);
    }
    setModelLabel(viewer, variant);
    viewer.status.hidden = true;
    fitModels();
  } catch (error) {
    if (model && model !== viewer.model) disposeModel(model, [], [viewer.neutral]);
    if (disposed || generation !== viewer.generation) return;
    viewer.error = true;
    if (viewer.variant) setModelLabel(viewer, viewer.variant);
    const retainedMessage = viewer.variant ? `仍显示“${viewer.variant.label}”。` : '';
    showStatus(viewer.status, `“${variant.label}”加载失败。${retainedMessage}${error.message || '请检查模型文件。'}`, true);
  } finally {
    if (!disposed && generation === viewer.generation) {
      viewer.request = null;
      viewer.loading = false;
      viewer.container.setAttribute('aria-busy', 'false');
      updatePageStatus();
      requestRender();
    }
  }
}

function loadImage(imageId, statusId, value, label) {
  const image = byId(imageId);
  const status = byId(statusId);
  try {
    const url = localUrl(value, label);
    image.addEventListener('load', () => { image.hidden = false; status.hidden = true; }, { signal: lifecycle.signal });
    image.addEventListener('error', () => showStatus(status, `${label}读取失败，请检查结果文件。`, true), { signal: lifecycle.signal });
    image.src = url.href;
  } catch (error) {
    showStatus(status, error.message, true);
  }
}

function metric(value, digits = 4) {
  return typeof value === 'number' && Number.isFinite(value) ? String(Number(value.toFixed(digits))) : '—';
}

function configureQuality(report) {
  if (!Array.isArray(report.variants) || !report.variants.length) return null;
  const variants = ['original', 'average', 'fine', 'finer', 'maximal']
    .map((id) => report.variants.find((variant) => variant?.id === id))
    .filter(Boolean);
  if (!variants.length || variants.some((variant) => typeof variant.label !== 'string' || typeof variant.model !== 'string')) {
    throw new Error('档位报告缺少有效的名称或模型路径。');
  }
  byId('experiment-intro').textContent = '保留整图深度推理得到的主体起伏，只将背景铺平。左右可独立选择原流程或四档推理结果，用相同视角对照；关闭照片纹理，可以直接检查表面凹凸。';
  byId('overlay-title').textContent = '保留起伏的区域';
  byId('overlay-description').textContent = '色块已合为保留起伏区域，颜色不代表凹凸；区域内部保留整图深度，外部背景铺平。';
  byId('quality-section').hidden = false;
  byId('legacy-metrics').hidden = true;
  byId('details-title').textContent = '保留区域的来源';
  for (const variant of variants) {
    const row = document.createElement('tr');
    const label = document.createElement('th');
    label.scope = 'row';
    label.textContent = variant.label;
    row.append(label);
    const elapsed = typeof variant.elapsed_seconds === 'number' && Number.isFinite(variant.elapsed_seconds)
      ? `${metric(variant.elapsed_seconds, 1)} 秒` : '—';
    const errorValue = (value) => typeof value === 'number' && Number.isFinite(value)
      ? value === 0 ? '0' : value.toPrecision(3) : '—';
    for (const value of [
      metric(variant.depth_level, 0),
      typeof variant.input_side === 'number' ? `${metric(variant.input_side, 0)} px` : '—',
      metric(variant.inference_passes, 0),
      elapsed,
      errorValue(variant.background_deviation),
      errorValue(variant.foreground_error),
    ]) {
      const cell = document.createElement('td');
      cell.textContent = value;
      row.append(cell);
    }
    byId('quality-rows').append(row);
  }
  return variants;
}

function configureSelector(viewer, variants, initial) {
  viewer.selector = byId(`${viewer.prefix}-model-select`);
  for (const variant of variants) {
    const option = document.createElement('option');
    option.value = variant.id;
    option.textContent = variant.label;
    viewer.selector.append(option);
  }
  viewer.selector.value = initial.id;
  byId(`${viewer.prefix}-choice`).hidden = false;
  byId(`${viewer.prefix}-tag`).textContent = viewer.prefix === 'baseline' ? '左侧' : '右侧';
  viewer.selector.addEventListener('change', () => {
    const selected = variants.find((variant) => variant.id === viewer.selector.value);
    if (selected) void loadModel(viewer, selected);
  }, { signal: lifecycle.signal });
}

function showReport(report) {
  if (typeof report.title === 'string') {
    byId('title').textContent = report.title;
    document.title = `${report.title} · 本地对照`;
  }
  const subjects = Array.isArray(report.subjects)
    ? report.subjects.filter((subject) => subject && typeof subject === 'object')
    : [];
  byId('subject-count').textContent = String(subjects.length);
  byId('depth-calls').textContent = metric(report.metrics?.depth_inference_calls, 0);
  byId('background-height').textContent = metric(report.metrics?.background_max_abs_height);
  byId('subject-height').textContent = metric(report.metrics?.minimum_subject_height);
  const subjectNames = new Map([
    ['bed', '床'],
    ['sofa', '沙发'],
    ['cushion', '靠垫'],
    ['table', '桌子'],
    ['blanket', '毯子'],
    ['air-device', '白色设备'],
  ]);
  for (const subject of subjects) {
    const item = document.createElement('li');
    const originalName = typeof subject.name === 'string' && subject.name ? subject.name : '未命名区域';
    const className = originalName.replace(/-\d+$/, '');
    const name = subjectNames.has(className) ? `${subjectNames.get(className)}（${originalName}）` : originalName;
    const source = subject.source === 'manual-grabcut' ? '人工框选'
      : subject.source === 'manual-mask' ? '人工蒙版'
      : subject.source === 'semantic-union-with-manual-correction' ? '自动合并＋已指定设备校正'
      : subject.source === 'semantic-union' ? '自动前景合并' : '自动分割';
    const score = source === '自动分割' && typeof subject.score === 'number' && Number.isFinite(subject.score)
      ? ` · 类别置信度 ${Math.round(subject.score * 100)}%` : '';
    item.textContent = `${name} · ${source}${score}`;
    byId('subjects').append(item);
  }
  if (Array.isArray(report.limitations) && report.limitations.length) {
    byId('limitations-details').hidden = false;
    for (const limitation of report.limitations) {
      const item = document.createElement('li');
      item.textContent = String(limitation);
      byId('limitations').append(item);
    }
  }
  loadImage('original-image', 'original-status', report.original, '原图');
  loadImage('overlay-image', 'overlay-status', report.overlay, '主体掩码');
}

function cleanup() {
  if (disposed) return;
  disposed = true;
  lifecycle.abort();
  resizeObserver.disconnect();
  if (renderFrame !== null) cancelAnimationFrame(renderFrame);
  for (const viewer of viewers) {
    viewer.request?.abort();
    viewer.generation += 1;
    viewer.controls.removeEventListener('change', viewer.onChange);
    viewer.controls.dispose();
    disposeModel(viewer.scene, [viewer.neutral]);
    viewer.renderer.dispose();
    viewer.renderer.forceContextLoss();
  }
}

window.addEventListener('pagehide', cleanup, { once: true });
window.addEventListener('pageshow', (event) => {
  if (event.persisted && disposed) window.location.reload();
});
byId('front-view').addEventListener('click', () => setView(false), { signal: lifecycle.signal });
byId('angled-view').addEventListener('click', () => setView(true), { signal: lifecycle.signal });
byId('texture-toggle').addEventListener('change', (event) => {
  for (const viewer of viewers) {
    viewer.model?.traverse((object) => {
      if (object.isMesh) object.material = event.target.checked ? object.userData.originalMaterial : viewer.neutral;
    });
  }
  requestRender();
}, { signal: lifecycle.signal });
byId('height-scale').addEventListener('input', (event) => {
  const scale = Number(event.target.value);
  byId('height-value').textContent = `${scale.toFixed(1)}×`;
  for (const viewer of viewers) if (viewer.model) viewer.model.scale.z = scale;
  requestRender();
}, { signal: lifecycle.signal });

async function start() {
  try {
    const response = await getFile(new URL('report.json', baseUrl));
    const report = await response.json();
    if (disposed) return;
    showReport(report);
    const variants = configureQuality(report);
    const left = variants ? variants.find((variant) => variant.id === 'original') ?? variants[0]
      : { id: 'baseline', label: '整张图估计深度', model: report.baseline_model };
    const right = variants ? variants.find((variant) => variant.id === 'fine') ?? variants[variants.length - 1]
      : { id: 'subject', label: '主体凸起，背景铺平', model: report.model };
    const specifications = [
      ['baseline', left],
      ['subject', right],
    ];
    const pending = [];
    for (const [prefix, initial] of specifications) {
      try {
        const viewer = createViewer(prefix, initial.label);
        if (variants) configureSelector(viewer, variants, initial);
        pending.push([viewer, initial]);
      } catch (error) {
        showStatus(byId(`${prefix}-status`), error.message || '三维查看器初始化失败。', true);
      }
    }
    setView(true);
    await Promise.allSettled(pending.map(([viewer, initial]) => loadModel(viewer, initial)));
    updatePageStatus();
  } catch (error) {
    if (disposed) return;
    showStatus(pageStatus, `无法读取实验结果：${error.message} 请通过本地 HTTP 服务打开结果目录。`, true);
    showStatus(byId('baseline-status'), '等待有效的 report.json。', true);
    showStatus(byId('subject-status'), '等待有效的 report.json。', true);
  }
}

start();
