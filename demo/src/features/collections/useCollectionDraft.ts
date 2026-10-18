import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, assertSessionEpoch, errorMessage, getState, mutate, sessionEpoch, uploadMedia } from '../../api';
import type { RunMutation } from '../../app/useAccountWorkspace';
import type { CollectionItem } from '../../data';

function initialDraft(item?: CollectionItem) {
  const now = new Date();
  const date = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  return {
    id: item?.id ?? crypto.randomUUID(), version: item?.version ?? 1, date: item?.date ?? date,
    title: item?.title ?? '', category: item?.category ?? 'daily', tags: item?.tags.join('、') ?? '',
    note: item?.note ?? '', customImage: item?.customImage ?? '', art: item?.art ?? 'cafe',
  };
}

export function useCollectionDraft({ item, editing, busy, runMutation, onSaved }: {
  item?: CollectionItem;
  editing: boolean;
  busy: boolean;
  runMutation: RunMutation;
  onSaved: (id: string, editing: boolean) => void;
}) {
  // The route keys this editor. Snapshot/polling and CSRF refreshes never reset a draft.
  const [draft, setDraft] = useState(() => initialDraft(item));
  const [isEditing, setIsEditing] = useState(editing);
  const [formError, setFormError] = useState('');
  const [versionConflict, setVersionConflict] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const selectedFile = useRef<File | null>(null);
  const uploaded = useRef<{ file: File; assetId: string } | null>(null);
  const previewUrl = useRef('');
  const alive = useRef(true);

  const releasePreview = () => {
    if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
    previewUrl.current = '';
    selectedFile.current = null;
    uploaded.current = null;
  };
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; releasePreview(); };
  }, []);

  const readFile = (file?: File) => {
    if (!file || busy) return;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { setFormError('请选择 JPG、PNG 或 WebP 图片。'); return; }
    if (file.size > 12 * 1024 * 1024) { setFormError('图片请小于 12 MB。'); return; }
    releasePreview();
    selectedFile.current = file;
    previewUrl.current = URL.createObjectURL(file);
    setDraft(previous => ({ ...previous, customImage: previewUrl.current }));
    setFormError('');
  };
  const failed = (error: unknown) => {
    if (!alive.current) return;
    setFormError(errorMessage(error));
    if (error instanceof ApiError && error.status === 409) setVersionConflict(true);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!draft.title.trim()) { setFormError('给这件收藏起个名字吧。'); return; }
    const file = selectedFile.current;
    if (!isEditing && !file) { setFormError('请先选择一张图片。'); return; }
    const payload = {
      title: draft.title.trim(), category: draft.category,
      tags: draft.tags.trim() ? draft.tags.split(/[、,，]/).map(value => value.trim()).filter(Boolean) : isEditing ? [] : null,
      note: draft.note.trim(), date: draft.date, art: draft.art,
    };
    const { id, version } = draft;
    const scope = sessionEpoch();
    void runMutation(async () => {
      let assetId: string | undefined;
      if (file) {
        if (uploaded.current?.file !== file) {
          const asset = await uploadMedia(file);
          assertSessionEpoch(scope);
          if (!alive.current) throw new DOMException('编辑页面已关闭。', 'AbortError');
          uploaded.current = { file, assetId: asset.assetId };
        }
        assetId = uploaded.current!.assetId;
      }
      assertSessionEpoch(scope);
      return isEditing ? mutate(`/api/collections/${encodeURIComponent(id)}`, 'PATCH', { ...payload, version, ...(assetId ? { assetId } : {}) })
        : mutate('/api/collections', 'POST', { ...payload, id, assetId });
    }, {
      onStart: () => setFormError(''),
      onError: failed,
      onSuccess: next => {
        if (!alive.current) return;
        if (!next.collections.some(item => item.id === id)) throw new Error('服务未返回保存后的收藏，请刷新确认。');
        releasePreview();
        onSaved(id, isEditing);
      },
    });
  };
  const resolveConflict = () => {
    void runMutation(() => getState(), {
      onStart: () => setFormError(''),
      onError: failed,
      onSuccess: next => {
        if (!alive.current) return;
        const current = next.collections.find(item => item.id === draft.id);
        if (!current) throw new Error('收藏已被删除，当前草稿仍保留。');
        setDraft(previous => ({ ...previous, version: current.version }));
        setIsEditing(true);
        setVersionConflict(false);
        setFormError(isEditing ? '已读取最新版本。再次保存会使用当前草稿更新内容。'
          : '已找到先前创建的收藏。草稿仍保留，再次保存会更新这件收藏，不会重复创建。');
      },
    });
  };

  return { draft, setDraft, isEditing, formError, versionConflict, fileInput, readFile, submit, resolveConflict };
}
