import { Component, lazy, Suspense, type ReactNode } from 'react';
import { Art } from '../../art';

const ReliefViewer = lazy(() => import('../../ReliefViewer'));

class PreviewBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export default function ReliefPreview({ modelUrl, title, imageUrl, art }: {
  modelUrl: string;
  title: string;
  imageUrl?: string;
  art: string;
}) {
  const original = <Art name={art} src={imageUrl} alt={title} />;
  return <PreviewBoundary key={modelUrl} fallback={<>
    {original}
    <div className="relief-message" role="alert">
      <p>浮雕查看器未能加载，原图与收藏仍可查看。</p>
      {/* A new document clears both React.lazy's rejected promise and the module map. */}
      <button className="secondary-button" onClick={() => window.location.reload()}>重新加载页面</button>
    </div>
  </>}>
    <Suspense fallback={<>{original}<p role="status">正在打开浮雕…</p></>}>
      <ReliefViewer modelUrl={modelUrl} title={title} imageUrl={imageUrl} />
    </Suspense>
  </PreviewBoundary>;
}
