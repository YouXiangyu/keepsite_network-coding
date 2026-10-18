import { useState, type ReactNode } from 'react';
import { Camera, Check } from '@phosphor-icons/react';
import { Art } from '../../art';
import Link from '../../app/Link';
import type { RunMutation } from '../../app/useAccountWorkspace';
import type { CollectionItem } from '../../data';
import { Empty, PageHeading } from '../../shared/ui';
import { categories } from './presentation';
import { useCollectionDraft } from './useCollectionDraft';

export default function CollectionEditor({ item, editing, busy, runMutation, onSaved, back }: {
  item?: CollectionItem;
  editing: boolean;
  busy: boolean;
  runMutation: RunMutation;
  onSaved: (id: string, editing: boolean) => void;
  back: ReactNode;
}) {
  const [missingOnOpen] = useState(editing && !item);
  const { draft, setDraft, isEditing, formError, versionConflict, fileInput, readFile, submit, resolveConflict } =
    useCollectionDraft({ item, editing, busy, runMutation, onSaved });

  if (missingOnOpen) return <>{back}<Empty title="这件收藏已不存在" /></>;

  return <div className="form-page">
    {back}
    <PageHeading title={isEditing ? '编辑收藏' : '留下一件美好'} subtitle="一张照片，把这一刻留在身边。" />
    <form className="collection-form" onSubmit={submit}>
      <fieldset disabled={busy} className="mutation-fields">
        <div className="upload-column">
          <button type="button" className="upload-card" onClick={() => fileInput.current?.click()}>
            {draft.customImage ? <Art name={draft.art} src={draft.customImage} alt="收藏图片预览" ratio={1.2} /> : <div className="upload-placeholder">
              <Camera size={38} weight="light" />
              <strong>选择一张照片</strong>
              <span>JPG、PNG 或 WebP，最大 12 MB</span>
            </div>}
            <span className="upload-label"><Camera size={18} />{draft.customImage ? '更换图片' : '选择图片'}</span>
          </button>
          <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" className="visually-hidden" tabIndex={-1} aria-label="选择收藏图片"
            onChange={event => { readFile(event.target.files?.[0]); event.target.value = ''; }} />
          <p className="field-hint">图片保存后会自动准备立体效果，你可以先继续浏览。</p>
        </div>
        <div className="form-column">
          <label className="field-label">名称
            <input value={draft.title} onChange={event => setDraft(previous => ({ ...previous, title: event.target.value }))} placeholder="给这件收藏起个名字" maxLength={70} required />
          </label>
          <fieldset className="category-picker">
            <legend>分类</legend>
            {categories.map(category => <button key={category.id} type="button" className={draft.category === category.id ? 'selected' : ''}
              aria-pressed={draft.category === category.id} onClick={() => setDraft(previous => ({ ...previous, category: category.id }))}>
              {category.label}
            </button>)}
          </fieldset>
          <label className="field-label">日期
            <input type="date" value={draft.date} required onChange={event => setDraft(previous => ({ ...previous, date: event.target.value }))} />
          </label>
          <label className="field-label">标签 <span>选填，用顿号分隔</span>
            <input value={draft.tags} onChange={event => setDraft(previous => ({ ...previous, tags: event.target.value }))} placeholder="例如：果茶、周末" maxLength={100} />
          </label>
          <label className="field-label">想记住的话 <span>选填</span>
            <textarea value={draft.note} onChange={event => setDraft(previous => ({ ...previous, note: event.target.value }))} placeholder="那天的天气，或想推荐给朋友的理由…" maxLength={500} rows={4} />
          </label>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          {versionConflict && <button type="button" className="secondary-button" onClick={resolveConflict}>读取最新版本，保留草稿</button>}
          <button className="primary-button save-button" type="submit"><Check size={19} />{busy ? '正在保存…' : '保存收藏'}</button>
          <Link to={isEditing ? `/collection/${encodeURIComponent(draft.id)}` : '/collection'} className="text-button" disabled={busy}>取消编辑</Link>
        </div>
      </fieldset>
    </form>
  </div>;
}
