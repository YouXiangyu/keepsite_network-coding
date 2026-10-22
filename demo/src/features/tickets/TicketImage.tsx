import { useEffect, useRef, useState } from 'react';
import type { MediaRef } from '../../data';
import { useMedia } from '../../MediaProvider';

export default function TicketImage({ media, title, retry = true }: { media: MediaRef; title: string; retry?: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const image = useMedia(media, visible);
  useEffect(() => {
    if (!container.current) return;
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)), { rootMargin: '200px' });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  return <div className="ticket-media"><div ref={container} className="ticket-image">{image.url
    ? <img src={image.url} alt={title || '电影票原图'} />
    : <div className="image-unavailable" role="status"><p>{image.status === 'error' ? image.message : '正在准备图片…'}</p>{retry && image.status === 'error' && <button className="secondary-button" onClick={image.retry}>重试图片</button>}</div>}</div>
    <p className="media-availability" role="status">{image.message}</p></div>;
}
