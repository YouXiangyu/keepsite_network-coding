import { WarningCircle } from '@phosphor-icons/react';
import type { ServerState } from '../../api';
import type { CollectionItem } from '../../data';

export default function GenerationRecoveryNotice({ entries, collections, busy, onRetry }: {
  entries: ServerState['generationRecovery'];
  collections: CollectionItem[];
  busy: boolean;
  onRetry: (collectionId: string) => void;
}) {
  return <>{entries.map(entry => {
    const item = collections.find(item => item.id === entry.collectionId);
    if (!item) return null;
    return <div className="storage-error" role="alert" key={entry.jobId}>
      <WarningCircle size={20} />
      <span>“{item.title}”的生成结果保存失败，原图仍保留。{entry.error}</span>
      <button disabled={busy} aria-label={`重试保存生成结果：${item.title}`} onClick={() => onRetry(entry.collectionId)}>重试保存生成结果</button>
    </div>;
  })}</>;
}
