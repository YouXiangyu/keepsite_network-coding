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

async function getFile(url) {
  const response = await fetch(url, { signal: lifecycle.signal });
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

function disposeModel(root, extraMaterials = []) {
  const geometries = new Set();
  const materials = new Set(extraMaterials);
  const textures = new Set();
  root.traverse((object) => {
    if (object.geometry) geometries.add(object.geometry);
    for (const material of [].concat(object.material ?? [])) materials.add(material);
    for (const material of [].concat(object.userData.originalMaterial ?? [])) materials.add(material);
  });
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

function createViewer(containerId, statusId, label) {
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
  const viewer = { container, status, renderer, scene, camera, controls, neutral, model: null };
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

async function loadModel(viewer, url) {
  const response = await getFile(url);
  const data = await response.arrayBuffer();
  if (disposed) return;
  const gltf = await new GLTFLoader().parseAsync(data, new URL('./', url).href);
  if (disposed) {
    disposeModel(gltf.scene);
    return;
  }
  const model = gltf.scene;
  const bounds = new THREE.Box3().setFromObject(model);
  if (bounds.isEmpty()) {
    disposeModel(model);
    throw new Error('模型没有可显示的几何表面。');
  }
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  model.position.x -= center.x;
  model.position.y -= center.y;
  model.traverse((object) => {
    if (!object.isMesh) return;
    object.userData.originalMaterial = object.material;
    object.material = viewer.neutral;
  });
  viewer.model = model;
  viewer.size = size;
  viewer.scene.add(model);
  viewer.status.hidden = true;
  requestRender();
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
      : subject.source === 'manual-mask' ? '人工蒙版' : '自动分割';
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
    const specifications = [
      ['baseline-viewport', 'baseline-status', '原流程模型', report.baseline_model, 'baseline_model'],
      ['subject-viewport', 'subject-status', '主体分层模型', report.model, 'model'],
    ];
    const results = await Promise.allSettled(specifications.map(async ([container, status, label, path, field]) => {
      try {
        const viewer = createViewer(container, status, label);
        await loadModel(viewer, localUrl(path, field));
      } catch (error) {
        if (!disposed) showStatus(byId(status), error.message || '模型加载失败。', true);
        throw error;
      }
    }));
    if (disposed) return;
    const ready = viewers.filter((viewer) => viewer.model);
    if (ready.length) {
      sceneWidth = Math.max(...ready.map((viewer) => viewer.size.x));
      sceneHeight = Math.max(...ready.map((viewer) => viewer.size.y));
      sceneDepth = Math.max(...ready.map((viewer) => viewer.size.z));
      resize();
      setView(true);
      for (const viewer of ready) viewer.controls.enabled = true;
      for (const id of ['front-view', 'angled-view', 'texture-toggle', 'height-scale']) byId(id).disabled = false;
    }
    const failed = results.filter((result) => result.status === 'rejected').length;
    showStatus(pageStatus, failed ? `已载入 ${ready.length} / 2 个模型；请查看对应面板中的错误。` : '两个模型已载入 · 两侧使用相同视角与高度倍率。', failed > 0);
  } catch (error) {
    if (disposed) return;
    showStatus(pageStatus, `无法读取实验结果：${error.message} 请通过本地 HTTP 服务打开结果目录。`, true);
    showStatus(byId('baseline-status'), '等待有效的 report.json。', true);
    showStatus(byId('subject-status'), '等待有效的 report.json。', true);
  }
}

start();
