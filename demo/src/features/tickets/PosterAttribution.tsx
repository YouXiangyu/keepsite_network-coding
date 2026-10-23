import type { PosterSource } from '../../data';

export default function PosterAttribution({ source }: { source: PosterSource }) {
  return <aside className="poster-attribution" aria-label="海报出处与许可">
    <strong>海报出处与许可</strong>
    <p>作者：{source.author || '作者信息未提供'}</p>
    <p><a href={source.sourcePage} target="_blank" rel="noopener noreferrer">Wikimedia Commons 来源页</a><span aria-hidden="true"> · </span><a href={source.licenseUrl} target="_blank" rel="noopener noreferrer">{source.licenseName}</a></p>
  </aside>;
}
