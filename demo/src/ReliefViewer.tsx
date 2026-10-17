import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowLeft, ArrowRight, ArrowUp, ArrowDown, ArrowsCounterClockwise } from '@phosphor-icons/react';
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { boundedTilt, MAX_RELIEF_TILT, pressedTilt } from './relief-angle';
import './relief-viewer.css';

function disposeModel(root: THREE.Object3D) {
  const textures = new Set<THREE.Texture>();
  root.traverse(object => {
    if (!(object instanceof THREE.Mesh)) return;
    object.geometry.dispose();
    const materials: THREE.Material[] = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) {
      for (const value of Object.values(material)) if (value instanceof THREE.Texture) textures.add(value);
      material.dispose();
    }
  });
  for (const texture of textures) {
    texture.dispose();
    if (typeof ImageBitmap !== 'undefined' && texture.image instanceof ImageBitmap) texture.image.close();
  }
}

export default function ReliefViewer({ modelUrl, title, imageUrl }: { modelUrl: string; title: string; imageUrl?: string }) {
  const container = useRef<HTMLDivElement>(null);
  const rotate = useRef<(x: number, y: number) => void>(() => {});
  const angles = useRef<[number, number]>([0, 0]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [attempt, setAttempt] = useState(0);

  const setAngle = (x: number, y: number) => {
    angles.current = boundedTilt(x, y);
    rotate.current(...angles.current);
  };
  const step = (x: number, y: number) => setAngle(angles.current[0] + x, angles.current[1] + y);
  const keyboard = (event: KeyboardEvent) => {
    const directions: Record<string, [number, number]> = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] };
    if (directions[event.key]) { event.preventDefault(); step(...directions[event.key]); }
    if (event.key === 'Home') { event.preventDefault(); setAngle(0, 0); }
  };

  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); }
    catch { setStatus('failed'); return; }
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const controller = new AbortController();
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 20);
    camera.position.set(0, 0, 4);
    camera.lookAt(0, 0, 0);
    scene.add(new THREE.AmbientLight(0xffffff, 2.5));
    const light = new THREE.DirectionalLight(0xffffff, 0.65);
    light.position.set(-3, 4, 6);
    scene.add(light);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0xffffff, 0);
    renderer.domElement.setAttribute('aria-hidden', 'true');
    host.appendChild(renderer.domElement);
    let model: THREE.Group | null = null;
    let width = 2, height = 2, frame = 0, disposed = false;
    const target = new THREE.Quaternion();
    const render = () => {
      frame = 0;
      if (disposed || !model) return;
      if (reducedMotion.matches) model.quaternion.copy(target);
      else model.quaternion.slerp(target, 0.24);
      renderer.render(scene, camera);
      if (model.quaternion.angleTo(target) > 0.0005) frame = requestAnimationFrame(render);
      else { model.quaternion.copy(target); renderer.render(scene, camera); }
    };
    const schedule = () => { if (!frame && !disposed) frame = requestAnimationFrame(render); };
    rotate.current = (x, y) => {
      const [a, b] = boundedTilt(x, y), distance = Math.hypot(a, b);
      target.copy(pressedTilt(a, b));
      host.dataset.tiltDegrees = distance.toFixed(2);
      schedule();
    };
    const resize = () => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      const aspect = w / h;
      const halfHeight = Math.max(height / 2, width / 2 / aspect) * 1.12 + 0.08;
      camera.left = -halfHeight * aspect; camera.right = halfHeight * aspect;
      camera.top = halfHeight; camera.bottom = -halfHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
      schedule();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    setStatus('loading');
    angles.current = [0, 0];
    host.dataset.tiltDegrees = '0';
    fetch(modelUrl, { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error('模型文件暂时无法读取');
      return response.arrayBuffer();
    }).then(buffer => new GLTFLoader().parseAsync(buffer, new URL('.', modelUrl).href)).then(gltf => {
      if (disposed) { disposeModel(gltf.scene); return; }
      model = gltf.scene;
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      model.position.sub(center);
      width = size.x; height = size.y;
      scene.add(model);
      resize();
      setStatus('ready');
    }).catch(error => {
      if (!disposed && error.name !== 'AbortError') setStatus('failed');
    });
    return () => {
      disposed = true;
      controller.abort();
      observer.disconnect();
      cancelAnimationFrame(frame);
      rotate.current = () => {};
      if (model) disposeModel(model);
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [modelUrl, attempt]);

  return <section className="relief-viewer" aria-label={`${title}的浮雕`}>
    <div className="relief-viewport" ref={container} role="img" aria-label={`${title}，立体浮雕预览`} tabIndex={0} onKeyDown={keyboard}
      onPointerMove={event => {
        if (status !== 'ready' || event.pointerType !== 'mouse' || event.buttons || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        const rect = event.currentTarget.getBoundingClientRect();
        setAngle((event.clientX - rect.left - rect.width / 2) / rect.width * MAX_RELIEF_TILT * 2, (event.clientY - rect.top - rect.height / 2) / rect.height * MAX_RELIEF_TILT * 2);
      }} onPointerLeave={() => { if (status === 'ready') setAngle(0, 0); }}>
      {status !== 'ready' && imageUrl && <img className="relief-fallback" src={imageUrl} alt={title} draggable={false}/>}
    </div>
    {status === 'loading' && <p className="relief-message" role="status">正在打开浮雕…</p>}
    {status === 'failed' ? <div className="relief-message" role="alert"><p>浮雕暂时无法显示，原图已保留。</p><button className="secondary-button" onClick={() => setAttempt(a => a + 1)}>重新加载浮雕</button></div>
      : <div className="relief-controls" role="group" aria-label="浮雕视角">
        <button aria-label="浮雕向左" disabled={status !== 'ready'} onClick={() => step(-5, 0)}><ArrowLeft size={19}/></button>
        <button aria-label="浮雕向上" disabled={status !== 'ready'} onClick={() => step(0, -5)}><ArrowUp size={19}/></button>
        <button aria-label="浮雕回到正面" disabled={status !== 'ready'} onClick={() => setAngle(0, 0)}><ArrowsCounterClockwise size={19}/></button>
        <button aria-label="浮雕向下" disabled={status !== 'ready'} onClick={() => step(0, 5)}><ArrowDown size={19}/></button>
        <button aria-label="浮雕向右" disabled={status !== 'ready'} onClick={() => step(5, 0)}><ArrowRight size={19}/></button>
      </div>}
    <p className="relief-caption">{status === 'ready' ? '轻转视角，看看照片里的层次' : '图片与浮雕都保存在本机'}</p>
  </section>;
}
