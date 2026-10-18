import * as THREE from '../../demo/node_modules/three/build/three.module.js';
import { OrbitControls } from '../../demo/node_modules/three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from '../../demo/node_modules/three/examples/jsm/loaders/GLTFLoader.js';

const FRONT = new THREE.Vector3(0, 0, 1);
const MAX_ANGLE = THREE.MathUtils.degToRad(40);

function disposeObject(root, preservedMaterials = []) {
  const geometries = new Set();
  const materials = new Set();
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

export class ReliefComparison {
  constructor(containers, { onStatus = () => {}, onPoseChange = () => {} } = {}) {
    this.views = new Map();
    this.onStatus = onStatus;
    this.onPoseChange = onPoseChange;
    this.lifecycle = new AbortController();
    this.settings = { texture: false, height: 1, light: 1 };
    this.distance = 6;
    this.frame = null;
    this.disposed = false;
    this.syncing = false;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    for (const [slot, container] of Object.entries(containers)) {
      try {
        this.views.set(slot, this.createView(slot, container));
        this.resizeObserver.observe(container);
      } catch (error) {
        this.onStatus(slot, { error: `三维查看器不可用：${error.message}。仍可生成并下载模型。` });
      }
    }
    this.setView('angle');
  }

  createView(slot, container) {
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0xe8ede5, 1);
    const canvas = renderer.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', `${slot === 'left' ? '左' : '右'}侧模型；方向键旋转，加减键缩放`);
    container.prepend(canvas);
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.001, 100);
    camera.position.set(0, 0, this.distance);
    const controls = new OrbitControls(camera, canvas);
    controls.enabled = false;
    controls.enableDamping = false;
    controls.enablePan = false;
    controls.rotateSpeed = 0.65;
    controls.zoomSpeed = 0.65;
    controls.minZoom = 0.5;
    controls.maxZoom = 4;
    controls.minPolarAngle = Math.PI / 2 - MAX_ANGLE;
    controls.maxPolarAngle = Math.PI / 2 + MAX_ANGLE;
    controls.minAzimuthAngle = -MAX_ANGLE;
    controls.maxAzimuthAngle = MAX_ANGLE;
    const neutral = new THREE.MeshStandardMaterial({ color: 0xd9ddd4, roughness: 0.88, metalness: 0, side: THREE.DoubleSide });
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.5);
    keyLight.position.set(-3, 2, 1.4);
    const fillLight = new THREE.DirectionalLight(0xe4edff, 0.5);
    fillLight.position.set(3, -1, 3);
    scene.add(new THREE.AmbientLight(0xffffff, 0.65), keyLight, fillLight);
    const view = { slot, container, renderer, scene, camera, controls, neutral, keyLight, fillLight, model: null, generation: 0, request: null, label: '', url: null, contextLost: false };
    view.onChange = () => this.syncFrom(view);
    controls.addEventListener('change', view.onChange);
    canvas.addEventListener('keydown', (event) => {
      if (!view.model || event.ctrlKey || event.metaKey || event.altKey) return;
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
      this.syncFrom(view);
    }, { signal: this.lifecycle.signal });
    canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      view.contextLost = true;
      this.onStatus(slot, { error: '三维绘图上下文已丢失，请刷新页面恢复；生成任务仍会继续。', retainedLabel: view.label });
    }, { signal: this.lifecycle.signal });
    return view;
  }

  render() {
    if (this.disposed || this.frame !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      if (this.disposed) return;
      for (const view of this.views.values()) {
        if (!view.contextLost && view.container.clientWidth) view.renderer.render(view.scene, view.camera);
      }
    });
  }

  syncFrom(source, manual = true) {
    if (this.disposed || this.syncing) return;
    this.syncing = true;
    const offset = source.camera.position.clone().sub(source.controls.target);
    const distance = offset.length();
    if (distance > 0) {
      const direction = offset.divideScalar(distance);
      if (direction.angleTo(FRONT) > MAX_ANGLE) {
        const planar = new THREE.Vector2(direction.x, direction.y);
        if (!planar.lengthSq()) planar.x = 1;
        planar.normalize().multiplyScalar(Math.sin(MAX_ANGLE));
        direction.set(planar.x, planar.y, Math.cos(MAX_ANGLE));
        source.camera.position.copy(source.controls.target).addScaledVector(direction, distance);
        source.camera.lookAt(source.controls.target);
      }
    }
    source.controls.update();
    for (const view of this.views.values()) {
      if (view === source) continue;
      view.camera.position.copy(source.camera.position);
      view.camera.quaternion.copy(source.camera.quaternion);
      view.camera.zoom = source.camera.zoom;
      view.controls.target.copy(source.controls.target);
      view.camera.updateProjectionMatrix();
      view.controls.update();
    }
    this.syncing = false;
    if (manual) this.onPoseChange();
    this.render();
  }

  resize() {
    if (this.disposed || !this.views.size) return;
    const ready = [...this.views.values()].filter((view) => view.model);
    const width = ready.length ? Math.max(...ready.map((view) => view.size.x)) : 2;
    const height = ready.length ? Math.max(...ready.map((view) => view.size.y)) : 1.5;
    const depth = ready.length ? Math.max(...ready.map((view) => view.size.z)) : 0.3;
    this.distance = Math.max(width, height, depth * 3, 0.01) * 3;
    for (const view of this.views.values()) {
      const viewportWidth = view.container.clientWidth;
      const viewportHeight = view.container.clientHeight;
      if (viewportWidth && viewportHeight) {
        const aspect = viewportWidth / viewportHeight;
        const halfHeight = Math.max(height / 2, width / (2 * aspect)) * 1.15;
        view.renderer.setSize(viewportWidth, viewportHeight, false);
        view.camera.left = -halfHeight * aspect;
        view.camera.right = halfHeight * aspect;
        view.camera.top = halfHeight;
        view.camera.bottom = -halfHeight;
      }
      view.camera.near = this.distance / 1000;
      view.camera.far = this.distance * 4;
      view.camera.position.setLength(this.distance);
      view.camera.updateProjectionMatrix();
    }
    this.syncFrom(this.views.values().next().value, false);
  }

  setView(angle) {
    const view = this.views.values().next().value;
    if (!view || this.disposed) return;
    const direction = angle === 'front' ? FRONT : new THREE.Vector3(0.56, 0.20, 0.80).normalize();
    view.controls.target.set(0, 0, 0);
    view.camera.position.copy(direction).multiplyScalar(this.distance);
    view.camera.zoom = 1;
    view.camera.updateProjectionMatrix();
    this.syncFrom(view, false);
  }

  setTexture(enabled) {
    this.settings.texture = enabled;
    for (const view of this.views.values()) {
      view.model?.traverse((object) => {
        if (object.isMesh) object.material = enabled ? object.userData.originalMaterial : view.neutral;
      });
    }
    this.render();
  }

  setHeightScale(value) {
    this.settings.height = value;
    for (const view of this.views.values()) if (view.model) view.model.scale.z = value;
    this.render();
  }

  setLightIntensity(value) {
    this.settings.light = value;
    for (const view of this.views.values()) {
      view.keyLight.intensity = 2.5 * value;
      view.fillLight.intensity = 0.5 * value;
    }
    this.render();
  }

  cancelPending(slot) {
    const view = this.views.get(slot);
    if (!view || this.disposed) return;
    view.request?.abort();
    view.request = null;
    view.generation += 1;
    view.container.setAttribute('aria-busy', 'false');
  }

  async setModel(slot, value, label) {
    const view = this.views.get(slot);
    if (!view || this.disposed) return false;
    if (view.contextLost) {
      this.onStatus(slot, { error: '绘图上下文已丢失，请刷新页面恢复三维查看。', retainedLabel: view.label });
      return false;
    }
    view.request?.abort();
    const generation = ++view.generation;
    const request = new AbortController();
    view.request = request;
    view.container.setAttribute('aria-busy', 'true');
    this.onStatus(slot, { loading: true, label, retainedLabel: view.label });
    let model = null;
    try {
      const url = new URL(value, window.location.href);
      if (url.origin !== window.location.origin) throw new Error('模型必须来自当前本地实验台。');
      if (view.url === url.href) {
        view.label = label;
        this.onStatus(slot, { ready: true, label });
        return true;
      }
      const response = await fetch(url, { signal: request.signal });
      if (!response.ok) throw new Error(`模型读取失败（HTTP ${response.status}）。`);
      const buffer = await response.arrayBuffer();
      if (this.disposed || generation !== view.generation) return null;
      const gltf = await new GLTFLoader().parseAsync(buffer, new URL('./', url).href);
      model = gltf.scene;
      if (this.disposed || generation !== view.generation) {
        disposeObject(model);
        return null;
      }
      const bounds = new THREE.Box3().setFromObject(model);
      if (bounds.isEmpty()) throw new Error('模型没有可显示的几何表面。');
      const center = bounds.getCenter(new THREE.Vector3());
      const size = bounds.getSize(new THREE.Vector3());
      model.position.x -= center.x;
      model.position.y -= center.y;
      model.scale.z = this.settings.height;
      model.traverse((object) => {
        if (!object.isMesh) return;
        object.userData.originalMaterial = object.material;
        if (!this.settings.texture) object.material = view.neutral;
      });
      const previous = view.model;
      view.scene.add(model);
      view.model = model;
      view.size = size;
      view.label = label;
      view.url = url.href;
      view.controls.enabled = true;
      view.renderer.domElement.setAttribute('aria-label', `${label}；方向键旋转，加减键缩放`);
      if (previous) {
        view.scene.remove(previous);
        disposeObject(previous, [view.neutral]);
      }
      this.resize();
      this.onStatus(slot, { ready: true, label });
      return true;
    } catch (error) {
      if (model && model !== view.model) disposeObject(model, [view.neutral]);
      if (this.disposed || generation !== view.generation) return null;
      this.onStatus(slot, { error: `“${label}”加载失败：${error.message}`, retainedLabel: view.label });
      return false;
    } finally {
      if (!this.disposed && generation === view.generation) {
        view.request = null;
        view.container.setAttribute('aria-busy', 'false');
        this.render();
      }
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.lifecycle.abort();
    this.resizeObserver.disconnect();
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    for (const view of this.views.values()) {
      view.request?.abort();
      view.generation += 1;
      view.controls.removeEventListener('change', view.onChange);
      view.controls.dispose();
      disposeObject(view.scene, [view.neutral]);
      view.neutral.dispose();
      view.renderer.dispose();
      view.renderer.forceContextLoss();
    }
    this.views.clear();
  }
}
