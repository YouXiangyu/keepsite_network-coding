import { useState } from 'react';

export default function TicketImage({ url, title, retry = true }: { url: string; title: string; retry?: boolean }) {
  const [failedUrl, setFailedUrl] = useState('');
  return <div className="ticket-image">{failedUrl === url
    ? <div className="image-unavailable" role="status"><p>图片暂时无法读取</p>{retry && <button className="secondary-button" onClick={() => setFailedUrl('')}>重试图片</button>}</div>
    : <img src={url} alt={title || '电影票原图'} loading="lazy" onError={() => setFailedUrl(url)} />}</div>;
}
