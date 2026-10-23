import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Camera, Check } from '@phosphor-icons/react';
import { ApiError, assertSessionEpoch, errorMessage, getOperationStatus, getUploadStatus, mutate, sessionEpoch, uploadMedia, type SyncPosition } from '../../api';
import { emptyFields, fieldNames, type FieldName, type Ticket, type TicketFields } from '../../data';
import type { RunMutation, RunReconciliation } from '../../app/useAccountWorkspace';
import Link from '../../app/Link';
import { useMedia } from '../../MediaProvider';
import { verifyMediaBlob } from '../../mediaCache';
import { sourceLabels } from './processing';

export const fieldLabels: Record<FieldName, string> = { title: '片名', cinema: '影院', hall: '影厅', seat: '座位', showtime: '放映时间' };
export default function TicketEditor({ item, busy, runMutation, runReconciliation, editId, onSaved, processingEnabled, processingUnavailableReason }: {
  item?: Ticket; editId?: string; busy: boolean; runMutation: RunMutation; runReconciliation: RunReconciliation; onSaved: (id: string) => void; processingEnabled: boolean; processingUnavailableReason: string | null;
}) {
  const [id] = useState(() => editId ?? item?.id ?? crypto.randomUUID());
  const [fields, setFields] = useState<TicketFields>(() => item?.fields ?? emptyFields());
  const baseline = useRef(item?.fields ?? emptyFields());
  const baselineSources = useRef<Ticket['fieldSources']>(item?.fieldSources ?? { title: 'empty', cinema: 'empty', hall: 'empty', seat: 'empty', showtime: 'empty' });
  const touched = useRef(new Map<FieldName, number>());
  const editSequence = useRef(0);
  const submissions = useRef(new Map<string, { operationId: string; file: File | null; fieldEdits: Partial<Record<FieldName, number>>; applied: boolean }>());
  const [deleted, setDeleted] = useState(!!editId && !item);
  const [editing, setEditing] = useState(!!editId || !!item);
  const [preview, setPreview] = useState('');
  const [persistedMedia, setPersistedMedia] = useState(item?.media.original);
  const originalImage = useMedia(persistedMedia, !preview);
  const displayPreview = preview || originalImage.url;
  const uploadIntent = useRef<{ file: File; uploadId: string; attempted: boolean } | null>(null);
  const [uploadStatus, setUploadStatus] = useState('');
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const file = useRef<File | null>(null);
  const uploaded = useRef<{ file: File; assetId: string } | null>(null);
  const objectUrl = useRef('');
  const alive = useRef(true);
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const currentFields = item?.fields ?? baseline.current;
  const currentSources = item?.fieldSources ?? baselineSources.current;
  const userChange = (key: FieldName) => touched.current.has(key) && (fields[key] !== currentFields[key] || currentSources[key] !== 'user');
  useEffect(() => {
    if (!item) { if (editId) { setDeleted(true); setError('这张票根已被删除，当前草稿仍保留。不能保存到原记录，可以返回票夹另建。'); } return; }
    setFields(previous => {
      const next = { ...previous };
      for (const key of fieldNames) if (!touched.current.has(key)) next[key] = item.fields[key];
      return fieldNames.some(key => next[key] !== previous[key]) ? next : previous;
    });
    if (!file.current) setPersistedMedia(item.media.original);
  }, [item?.fields, item?.media.original.assetId, editId]);
  const releasePreview = () => { if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); objectUrl.current = ''; };
  useEffect(() => { alive.current = true; return () => { alive.current = false; releasePreview(); }; }, []);
  const select = (next?: File) => {
    if (!next || busy) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(next.type) || next.size > 12 * 1024 * 1024 || next.size === 0) {
      setError('请选择不超过 12 MiB 的 JPG、PNG 或 WebP 图片。'); return;
    }
    releasePreview(); file.current = next; uploaded.current = null;
    uploadIntent.current = { file: next, uploadId: crypto.randomUUID(), attempted: false };
    setUploadStatus('原票尚未上传，当前仅为页面草稿。');
    objectUrl.current = URL.createObjectURL(next); setPreview(objectUrl.current); setError('');
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (deleted) return;
    if (!editing && !file.current) { setError('请先选择一张电影票照片。'); return; }
    const changes = Object.fromEntries(fieldNames.filter(key => !editing ? fields[key] !== '' || touched.current.has(key) : userChange(key)).map(key => [key, fields[key]]));
    const selected = file.current;
    if (editing && !selected && Object.keys(changes).length === 0) { setError('尚未修改电影信息或原票。'); return; }
    const scope = sessionEpoch();
    void runMutation(async () => {
      let assetId: string | undefined;
      if (selected) {
        if (uploaded.current?.file !== selected) {
          const intent = uploadIntent.current;
          if (!intent || intent.file !== selected) throw new Error('上传草稿已改变，请重新选择图片。');
          let media;
          if (intent.attempted) {
            try {
              const status = await getUploadStatus(intent.uploadId);
              if (status.status === 'attached') throw new ApiError('图片已关联票根，请先核对保存结果。', 409);
              media = status.asset ?? undefined;
            } catch (error) { if (!(error instanceof ApiError) || error.status !== 404) throw error; }
          }
          intent.attempted = true;
          setUploadStatus('正在上传或核对原票…');
          if (!media) media = await uploadMedia(selected, intent.uploadId);
          await verifyMediaBlob(selected, media, () => assertSessionEpoch(scope));
          assertSessionEpoch(scope);
          if (!alive.current) throw new DOMException('编辑页面已关闭。', 'AbortError');
          setUploadStatus('图片已上传，票根引用尚待确认；这不代表票根已保存。');
          uploaded.current = { file: selected, assetId: media.assetId };
        }
        assetId = uploaded.current!.assetId;
      }
      const payload = { ...(editing ? {} : { id }), fields: changes, ...(assetId ? { originalAssetId: assetId } : {}) };
      const fingerprint = JSON.stringify({ editing, payload });
      if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
      assertSessionEpoch(scope);
      if (!submissions.current.has(operation.current.id)) submissions.current.set(operation.current.id, {
        operationId: operation.current.id, file: selected, applied: false,
        fieldEdits: Object.fromEntries(Object.keys(changes).map(key => [key, touched.current.get(key as FieldName)])),
      });
      return mutate(editing ? `/api/tickets/${encodeURIComponent(id)}` : '/api/tickets', editing ? 'PATCH' : 'POST', { ...payload, operationId: operation.current.id });
    }, {
      onStart: () => setError(''),
      onError: failure => { if (alive.current) { if (uploadIntent.current?.attempted && !uploaded.current) setUploadStatus('原票上传结果尚未确认，草稿仍保留。'); setError(errorMessage(failure)); setUncertain(!(failure instanceof ApiError) || failure.status === 0 || failure.status === 409 || failure.status >= 500); } },
      onCancel: () => { if (alive.current) { setError('保存请求已取消，结果尚未确认。请核对保存结果，草稿仍保留。'); setUncertain(true); } },
      onSuccess: next => {
        if (!alive.current) return;
        if (!next.tickets.some(ticket => ticket.id === id && ticket.deletedAt === null)) throw new Error('未返回已保存的票根，请核对保存结果。');
        releasePreview(); onSaved(id);
      },
    });
  };
  const reconcile = () => void runReconciliation(async () => {
    let position: SyncPosition | undefined;
    const scope = sessionEpoch();
    // A durable receipt, rather than the current image or record version,
    // proves that a submission applied even if another device changed it later.
    for (const submission of submissions.current.values()) {

      const receipt = await getOperationStatus(submission.operationId);
      assertSessionEpoch(scope);
      submission.applied = receipt.applied;
      if (position && receipt.serverId !== position.serverId) throw new ApiError('核对期间同步服务已变更，请重新核对；草稿仍保留。');
      position = { serverId: receipt.serverId, committedCursor: receipt.committedCursor === null ? position?.committedCursor ?? null : Math.max(position?.committedCursor ?? 0, receipt.committedCursor) };
    }
    if (!position) throw new ApiError('没有需要核对的保存请求。');
    return position;
  }, {
    onStart: () => setError(''),
    onError: failure => { if (alive.current) setError(errorMessage(failure)); },
    onSuccess: next => {
      if (!alive.current) return;
      const current = next.tickets.find(ticket => ticket.id === id);
      const confirmed = [...submissions.current.values()].filter(submission => submission.applied);
      if (current?.deletedAt != null || !current && (editing || confirmed.length > 0)) {
        setDeleted(true);
        setError('已保存的票根已被删除。当前草稿保留，不能重新提交到已删除的记录。');
        return;
      }
      if (submissions.current.size > 0 && confirmed.length === 0) {
        setError('上次提交尚未确认，草稿仍保留。可以重试相同内容或稍后再次核对。');
        return;
      }
      if (current) {
        for (const submission of confirmed) {
          for (const key of fieldNames) {
            const submittedSequence = submission.fieldEdits[key];
            if (submittedSequence !== undefined && (touched.current.get(key) ?? Infinity) <= submittedSequence) touched.current.delete(key);
          }
          if (submission.file && file.current === submission.file) {
            file.current = null;
            uploaded.current = null;
            releasePreview();
          }
        }
        const merged = { ...current.fields };
        for (const key of touched.current.keys()) merged[key] = fields[key];
        baseline.current = current.fields;
        baselineSources.current = current.fieldSources;
        setFields(merged);
        if (!file.current) { setPreview(''); setPersistedMedia(current.media.original); setUploadStatus(''); }
        setEditing(true);
        if (operation.current && submissions.current.get(operation.current.id)?.applied) operation.current = null;
      }
      setUncertain(false);
      setError(current ? '已核对保存结果，草稿中尚未提交的修改仍保留。再次保存会更新同一条记录。' : '尚未找到保存记录，草稿保留，可以重试。');
    },
  });
  return <section><h1>{editing ? '编辑票根' : '上传电影票'}</h1><p className="page-intro">保留原票照片，电影信息可以稍后补充。</p>
    <form className="ticket-editor" onSubmit={submit}><fieldset disabled={busy}>
      <div><button type="button" className="upload-card" onClick={() => fileInput.current?.click()}>
        {displayPreview ? <img src={displayPreview} alt="原票预览" /> : <span><Camera size={36} /><strong>选择电影票照片</strong><small>JPG / PNG / WebP · 最大 12 MiB</small></span>}
      </button><input ref={fileInput} type="file" className="visually-hidden" accept="image/jpeg,image/png,image/webp" aria-label="选择原票照片" onChange={event => { select(event.target.files?.[0]); event.target.value = ''; }} />
        <p className="field-hint">{displayPreview ? '点击图片可以重新选择。' : ''}提交确认前仅为当前页面草稿，清除浏览器数据后无法恢复。</p>
        {uploadStatus && <p className="field-hint" role="status">{uploadStatus}</p>}
        {!preview && persistedMedia && <p className="field-hint" role="status">{originalImage.message}</p>}
        <p className="status-note">{processingEnabled ? '原票保存后会在服务端自动识别。手动填写的字段优先保留，也可暂时留空。' : processingUnavailableReason || '处理配置尚未就绪，当前可以手动填写或留空。'}</p></div>
      <div className="ticket-fields">{fieldNames.map(key => <label key={key}>{fieldLabels[key]} <small>选填</small>
        <input type={key === 'showtime' ? 'datetime-local' : 'text'} maxLength={key === 'title' ? 120 : 200} value={fields[key]} onChange={event => { touched.current.set(key, ++editSequence.current); setFields(previous => ({ ...previous, [key]: event.target.value })); }} />
        {(item || touched.current.has(key)) && <span className="field-source">{userChange(key) ? '手动修改，尚未保存' : sourceLabels[currentSources[key]]}</span>}
      </label>)}
        {error && <p className="form-error" role="alert">{error}</p>}
        {deleted && <Link to="/tickets/new" className="secondary-button">返回上传页另建票根</Link>}
        {uncertain && <button type="button" className="secondary-button" onClick={reconcile}>核对保存结果，保留草稿</button>}
        <div className="action-row"><button type="submit" className="primary-button" disabled={deleted}><Check size={20} />{busy ? '正在保存…' : '保存票根'}</button><Link to={editing ? `/tickets/${encodeURIComponent(id)}` : '/tickets'} className="secondary-button" disabled={busy}>取消</Link></div>
      </div>
    </fieldset></form>
  </section>;
}
