import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Plus, Bell, MagnifyingGlass, CaretRight, CaretDown, ArrowLeft, BookmarkSimple, Cloud, GearSix, Camera, PencilSimple, Trash, Check, Heart, Export, WarningCircle, SignOut } from '@phosphor-icons/react';
import { Art } from './art';
import { Brand } from './AuthGate';
import WebDialog from './WebDialog';
import { type AppData, type Category, type CollectionItem } from './data';
import { getState, mutate, uploadMedia, downloadBackup, signOut, sessionEpoch, assertSessionEpoch, errorMessage, ApiError, type ServerState, type SignedInSession } from './api';
import { navigate, useLocationPath } from './navigation';
import { parseRoute, tabs, type Tab } from './app/routes';
import Link from './app/Link';
import { Empty, IconButton, PageHeading, SectionHeading } from './shared/ui';
import PhotoCard from './features/collections/PhotoCard';
import { categories, catName, dateLabel } from './features/collections/presentation';
import { communityItem } from './features/explore/communityItem';
import ExplorePage from './features/explore/ExplorePage';
import SharesPage from './features/shares/SharesPage';
import ReliefPreview from './features/relief/ReliefPreview';

type Sheet = 'profile' | 'sync' | 'settings' | 'sort' | 'delete' | 'share' | null;
const emptyData = (): AppData => ({ collections: [], shares: [], community: [], profile: { name: '', bio: '' }, likedCommunityIds: [] });
const encoded = encodeURIComponent;

export default function Prototype({ session }: { session: SignedInSession }) {
  const path = useLocationPath();
  const route = parseRoute(path);
  const routeName = path.split('?')[0];
  const tab: Tab = route.type === 'main' ? route.tab : route.type === 'month' ? 'profile'
    : route.type === 'notifications' || route.type === 'detail' && route.source === 'shares' ? 'friends'
    : route.type === 'detail' && route.source === 'community' ? 'explore'
    : route.type === 'search' ? route.scope : 'collection';
  const [data, setData] = useState<AppData>(emptyData);
  const [ready, setReady] = useState(false);
  const [bootAttempt, setBootAttempt] = useState(0);
  const [storageError, setStorageError] = useState('');
  const [pollError, setPollError] = useState('');
  const [query, setQuery] = useState(new URLSearchParams(window.location.search).get('q') ?? '');
  const [source, setSource] = useState('all');
  const [sort, setSort] = useState('recent');
  const [exploreCategory, setExploreCategory] = useState('featured');
  const [onlyUnread, setOnlyUnread] = useState(false);
  const [sheet, setSheet] = useState<Sheet>(null);
  const [toast, setToast] = useState('');
  const [busy, setBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [formError, setFormError] = useState('');
  const [sheetError, setSheetError] = useState('');
  const [versionConflict, setVersionConflict] = useState(false);
  const [engine, setEngine] = useState<ServerState['generationEngine']>();
  const [profileDraft, setProfileDraft] = useState({ name: '', bio: '' });
  const [draft, setDraft] = useState({ id: '', version: 1, date: '', title: '', category: 'daily' as Category, tags: '', note: '', customImage: '', art: 'cafe' });
  const mutationLock = useRef(false);
  const alive = useRef(true);
  const latest = useRef(data);
  const revision = useRef({ serverId: '', value: -1 });
  const fileInput = useRef<HTMLInputElement>(null);
  const selectedFile = useRef<File | null>(null);
  const uploaded = useRef<{ file: File; assetId: string } | null>(null);
  const previewUrl = useRef('');
  const retryAction = useRef<(() => void) | null>(null);
  const heading = useRef<HTMLDivElement>(null);
  const unseen = data.shares.filter(share => share.unread).length;
  const hasActiveJobs = data.collections.some(item => ['queued', 'processing', 'blocked'].includes(item.generation));
  const share = route.type === 'detail' && route.source === 'shares' ? data.shares.find(item => item.id === route.id) : undefined;
  const community = route.type === 'detail' && route.source === 'community' ? data.community.find(item => item.id === route.id) : undefined;
  const currentItem = route.type !== 'detail' ? undefined : route.source === 'shares' ? share?.item
    : route.source === 'community' ? community ? communityItem(community) : undefined
    : data.collections.find(item => item.id === route.id);
  const owned = !!currentItem && !share && !community && data.collections.some(item => item.id === currentItem.id);

  const releasePreview = () => {
    if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
    previewUrl.current = '';
    selectedFile.current = null;
    uploaded.current = null;
  };
  useEffect(() => { alive.current = true; return () => { alive.current = false; releasePreview(); }; }, []);
  const acceptState = (state: ServerState) => {
    if (!state.initialized || !state.data) throw new Error('收藏空间正在准备中，请稍后重试。');
    if (revision.current.serverId && revision.current.serverId !== state.serverId) throw new Error('收藏空间已更新，请重新加载后继续。');
    if (state.revision >= revision.current.value) {
      revision.current = { serverId: state.serverId, value: state.revision };
      latest.current = state.data;
      setData(state.data);
      setEngine(state.generationEngine);
    }
    return latest.current;
  };
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setStorageError('');
    getState(controller.signal).then(state => {
      if (live) { acceptState(state); setReady(true); }
    }).catch(error => { if (live && error.name !== 'AbortError') setStorageError(errorMessage(error)); });
    return () => { live = false; controller.abort(); };
  }, [bootAttempt, session.csrfToken]);
  useEffect(() => {
    if (!ready || (!hasActiveJobs && route.type !== 'detail')) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try { const state = await getState(controller.signal); if (live) { acceptState(state); setPollError(''); } }
      catch (error) { if (live && !(error instanceof DOMException && error.name === 'AbortError')) setPollError(errorMessage(error)); }
      finally { if (live) timer = setTimeout(poll, hasActiveJobs ? 4000 : 10000); }
    };
    timer = setTimeout(poll, hasActiveJobs ? 4000 : 10000);
    return () => { live = false; clearTimeout(timer); controller.abort(); };
  }, [ready, hasActiveJobs, route.type]);
  useEffect(() => {
    setSheet(null);
    setSource('all');
    setFormError('');
    setQuery(new URLSearchParams(window.location.search).get('q') ?? '');
    window.scrollTo(0, 0);
    heading.current?.focus({ preventScroll: true });
  }, [routeName]);
  useEffect(() => {
    if (!ready) return;
    releasePreview();
    if (route.type !== 'add') return;
    const item = route.editId ? latest.current.collections.find(item => item.id === route.editId) : undefined;
    const now = new Date();
    const date = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    setDraft({ id: item?.id ?? crypto.randomUUID(), version: item?.version ?? 1, date: item?.date ?? date,
      title: item?.title ?? '', category: item?.category ?? 'daily', tags: item?.tags.join('、') ?? '',
      note: item?.note ?? '', customImage: item?.customImage ?? '', art: item?.art ?? 'cafe' });
    setVersionConflict(false);
  }, [ready, routeName]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 3200);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    document.title = `${currentItem?.title ?? tabs.find(item => item.id === tab)?.label ?? '我的收藏'} · MemoVerse`;
  }, [tab, currentItem?.title]);

  const runMutation = async (operation: () => Promise<ServerState>, success?: (next: AppData) => void, target: 'page' | 'form' | 'sheet' = 'page') => {
    if (mutationLock.current) return;
    mutationLock.current = true;
    setBusy(true);
    const setError = target === 'form' ? setFormError : target === 'sheet' ? setSheetError : setStorageError;
    setError('');
    try {
      const response = await operation();
      if (!alive.current) return;
      const next = acceptState(response);
      retryAction.current = null;
      success?.(next);
    } catch (error) {
      if (!alive.current || error instanceof DOMException && error.name === 'AbortError') return;
      setError(errorMessage(error));
      if (target === 'page') retryAction.current = () => { void runMutation(operation, success, target); };
      if (target === 'form' && error instanceof ApiError && error.status === 409) setVersionConflict(true);
    } finally {
      mutationLock.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const showSheet = (next: Sheet) => {
    if (busy) return;
    setSheetError('');
    if (next === 'profile') setProfileDraft({ ...data.profile });
    setSheet(next);
  };
  const back = () => {
    if (busy) return;
    if (Number(window.history.state?.memoverseDepth ?? 0) > 0) window.history.back();
    else navigate('/' + tab);
  };
  const viewCommunity = (id: string) => { void runMutation(() => mutate(`/api/community/${encoded(id)}/view`)); };
  const viewShare = (id: string) => { void runMutation(() => mutate(`/api/shares/${encoded(id)}/read`)); };
  const addShared = (shareId?: string, communityId?: string) => {
    const endpoint = communityId ? `/api/community/${encoded(communityId)}/collect` : shareId ? `/api/shares/${encoded(shareId)}/collect` : null;
    if (endpoint) void runMutation(() => mutate(endpoint), () => setToast('已加入我的收藏'));
  };
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
  const submitCollection = (event: FormEvent) => {
    event.preventDefault();
    if (!draft.title.trim()) { setFormError('给这件收藏起个名字吧。'); return; }
    const editing = route.type === 'add' && !!route.editId;
    const file = selectedFile.current;
    if (!editing && !file) { setFormError('请先选择一张图片。'); return; }
    const payload = { title: draft.title.trim(), category: draft.category,
      tags: draft.tags.trim() ? draft.tags.split(/[、,，]/).map(value => value.trim()).filter(Boolean) : editing ? [] : null,
      note: draft.note.trim(), date: draft.date, art: draft.art };
    const { id, version } = draft;
    const submittedScope = sessionEpoch();
    void runMutation(async () => {
      let assetId: string | undefined;
      if (file) {
        if (uploaded.current?.file !== file) { const asset = await uploadMedia(file); uploaded.current = { file, assetId: asset.assetId }; }
        assetId = uploaded.current!.assetId;
      }
      assertSessionEpoch(submittedScope);
      return editing ? mutate(`/api/collections/${encoded(id)}`, 'PATCH', { ...payload, version, ...(assetId ? { assetId } : {}) })
        : mutate('/api/collections', 'POST', { ...payload, id, assetId });
    }, next => {
      if (!next.collections.some(item => item.id === id)) throw new Error('服务未返回保存后的收藏，请刷新确认。');
      releasePreview();
      navigate(`/collection/${encoded(id)}`);
      setToast(editing ? '修改已保存' : '收藏已保存，正在准备立体效果');
    }, 'form');
  };
  const filtered = (items: CollectionItem[]) => [...items].filter(item => (source === 'all' || item.source === source)
    && (!query || `${item.title} ${item.tags.join(' ')} ${item.note}`.toLowerCase().includes(query.toLowerCase())))
    .sort((left, right) => sort === 'name' ? left.title.localeCompare(right.title, 'zh-CN')
      : sort === 'oldest' ? left.addedAt - right.addedAt : right.addedAt - left.addedAt);
  const backBar = () => <button className="back-link" onClick={back}><ArrowLeft size={19} />返回{tabs.find(item => item.id === tab)?.label}</button>;
  const headingBlock = (title: string, subtitle?: string, action?: ReactNode) => <PageHeading title={title} subtitle={subtitle} action={action} />;
  const sectionHeading = (title: string, to?: string, count?: number) => <SectionHeading title={title} to={to} count={count} />;
  const searchField = (placeholder: string) => <label className="search-field"><MagnifyingGlass size={20} /><span className="visually-hidden">{placeholder}</span><input type="search" placeholder={placeholder} value={query} onChange={event => setQuery(event.target.value)} /></label>;
  const sourceTabs = <div className="filter-tabs" role="group" aria-label="收藏来源">{[['all', '全部'], ['mine', '我的经历'], ['friend', '朋友推荐']].map(([value, label]) => <button key={value} aria-pressed={source === value} className={source === value ? 'active' : ''} onClick={() => setSource(value)}>{label}</button>)}</div>;
  const grid = (items: CollectionItem[]) => items.length ? <div className="collection-grid">{items.map(item => <PhotoCard key={item.id} item={item} />)}</div> : <Empty title="没有找到这件收藏" text="试试其他名字、标签或分类。" />;
  const toolbar = <div className="page-tools"><Link to={`/search?scope=${tab}`} className="search-trigger"><MagnifyingGlass size={20} />搜索{tab === 'explore' ? '主题与收藏' : tab === 'friends' ? '朋友的分享' : '收藏'}</Link>
    <Link to="/notifications" className="inbox-link"><Bell size={21} /><span className="visually-hidden">消息</span>{unseen > 0 && <span className="unread-dot" />}</Link></div>;

  const renderCollection = () => <>{toolbar}{headingBlock('我的收藏', '那些舍不得忘记的，慢慢收集在这里。', <Link to="/add" className="primary-button page-add"><Plus size={19} />添加收藏</Link>)}
    <div className="collection-overview"><span><strong>{data.collections.length}</strong> 件珍藏</span><span><strong>{new Set(data.collections.map(item => item.category)).size}</strong> 个分类</span><span>每张图片，都有自己的故事</span></div>
    {data.collections.length ? <>{sectionHeading('最近收藏')}<div className="recent-grid">{[...data.collections].sort((a, b) => b.addedAt - a.addedAt).slice(0, 3).map(item => <PhotoCard key={item.id} item={item} hero />)}</div>
      {[categories[0], categories[2], categories[1], categories[3]].filter(category => data.collections.some(item => item.category === category.id)).map(category => <section className="collection-section" key={category.id}>
        {sectionHeading(category.label, `/category/${category.id}`, data.collections.filter(item => item.category === category.id).length)}
        <div className="collection-rail" aria-label={`${category.label}收藏`}>{data.collections.filter(item => item.category === category.id).slice(0, 8).map(item => <PhotoCard key={item.id} item={item} />)}</div>
      </section>)}</> : <Empty><Link to="/add" className="primary-button">添加第一件收藏</Link></Empty>}</>;
  const renderCategory = (category: Category) => <>{backBar()}{headingBlock(catName(category), `${data.collections.filter(item => item.category === category).length} 件收藏`)}
    <div className="collection-toolbar">{searchField(`搜索${catName(category)}收藏`)}<button className="sort-button" onClick={() => showSheet('sort')}>{sort === 'recent' ? '最近添加' : sort === 'oldest' ? '最早添加' : '按名称排序'}<CaretDown size={17} /></button></div>{sourceTabs}{grid(filtered(data.collections.filter(item => item.category === category)))}</>;
  const renderProfile = () => {
    const months = [...new Set(data.collections.map(item => item.date.slice(0, 7)))].sort().reverse();
    return <>{headingBlock('我的', '把喜欢的日子，收成自己的故事。')}<div className="profile-card"><span className="profile-avatar">{(data.profile.name || session.user.name || session.user.email).slice(0, 1)}</span><div><h2>{data.profile.name || session.user.name}</h2><p>{data.profile.bio}</p><small>{session.user.email}</small></div><button className="secondary-button" onClick={() => showSheet('profile')}><PencilSimple size={17} />编辑资料</button></div>
      <div className="collection-overview"><span><strong>{data.collections.length}</strong> 件收藏</span><span><strong>{months.length}</strong> 个月的回忆</span></div>
      {sectionHeading('最近收藏', '/collection')}<div className="collection-rail">{[...data.collections].sort((a, b) => b.addedAt - a.addedAt).slice(0, 5).map(item => <PhotoCard key={item.id} item={item} />)}</div>
      <section className="collection-section">{sectionHeading('按月回看')}<div className="month-grid">{months.map(month => {
        const items = data.collections.filter(item => item.date.startsWith(month));
        return <Link key={month} to={`/month/${month}`} className="month-card"><Art name={items[0].art} src={items[0].customImage} alt={`${month}的收藏`} ratio={1.8} /><div><strong>{month.slice(0, 4)}年{Number(month.slice(5))}月</strong><span>{items.length} 件收藏</span></div></Link>;
      })}</div></section><div className="settings-list"><button onClick={() => showSheet('sync')}><Cloud size={22} /><span>账户与保存状态</span><CaretRight size={18} /></button><button onClick={() => showSheet('settings')}><GearSix size={22} /><span>隐私与数据导出</span><CaretRight size={18} /></button></div></>;
  };
  const renderDetail = () => {
    if (!currentItem) return <>{backBar()}<Empty title="这件收藏已不存在" text="请返回收藏列表查看其他内容。" /></>;
    const statusText = { reference: '图片收藏', queued: '图片已保存，正在排队生成', processing: '正在生成立体效果', blocked: '图片已保存，生成服务暂不可用', ready: '立体效果已准备好', failed: '生成未完成，原图仍保留' }[currentItem.generation];
    return <>{backBar()}<div className="detail-layout"><div className="detail-stage" data-model-url={currentItem.generation === 'ready' ? currentItem.modelUrl : undefined}>
      {currentItem.generation === 'ready' && currentItem.modelUrl ? <ReliefPreview modelUrl={currentItem.modelUrl} title={currentItem.title} imageUrl={currentItem.customImage} art={currentItem.art} />
        : <Art name={currentItem.art} src={currentItem.customImage} alt={currentItem.title} />}</div>
      <section className="detail-information"><span className="eyebrow">{catName(currentItem.category)} · {community ? '社区推荐' : currentItem.source === 'friend' ? '朋友推荐' : '我的经历'}</span><h1>{currentItem.title}</h1>{currentItem.date && <p className="card-meta">{dateLabel(currentItem.date)}</p>}
        <div className="detail-tags">{currentItem.tags.map(tag => <span key={tag}>{tag}</span>)}</div>{currentItem.note && <p className="detail-note">{currentItem.note}</p>}{currentItem.sender && <p className="card-meta">来自 {currentItem.sender} 的分享</p>}
        <p className="generation-status" role="status"><span className={hasActiveJobs ? 'pending' : ''} />{statusText}</p>
        {owned && ['failed', 'blocked'].includes(currentItem.generation) && <button className="secondary-button" disabled={busy} onClick={() => void runMutation(() => mutate(`/api/collections/${encoded(currentItem.id)}/retry-generation`), () => setToast('已重新检查生成任务'))}>重试生成</button>}
        <div className="detail-actions">{owned ? <><Link to={`/collection/${encoded(currentItem.id)}/edit`} className="primary-button" disabled={busy}><PencilSimple size={18} />编辑收藏</Link><button className="secondary-button" disabled={busy} onClick={() => showSheet('share')}><Export size={18} />分享</button><IconButton label="删除收藏" disabled={busy} onClick={() => showSheet('delete')}><Trash size={20} /></IconButton></>
          : <button className="primary-button" disabled={busy} onClick={() => addShared(share?.id, community?.id)}><BookmarkSimple size={19} />加入我的收藏</button>}
          {community && <IconButton label={data.likedCommunityIds.includes(community.id) ? '取消喜欢' : '喜欢这件纪念品'} disabled={busy} onClick={() => void runMutation(() => mutate(`/api/community/${encoded(community.id)}/like`, 'PUT', { liked: !data.likedCommunityIds.includes(community.id) }))}><Heart size={22} weight={data.likedCommunityIds.includes(community.id) ? 'fill' : 'regular'} /></IconButton>}
        </div>
      </section></div></>;
  };
  const renderAdd = () => {
    if (route.type === 'add' && route.editId && !data.collections.some(item => item.id === route.editId)) return <>{backBar()}<Empty title="这件收藏已不存在" /></>;
    return <div className="form-page">{backBar()}{headingBlock(route.type === 'add' && route.editId ? '编辑收藏' : '留下一件美好', '一张照片，把这一刻留在身边。')}
      <form className="collection-form" onSubmit={submitCollection}><fieldset disabled={busy} className="mutation-fields">
        <div className="upload-column"><button type="button" className="upload-card" onClick={() => fileInput.current?.click()}>{draft.customImage ? <Art name={draft.art} src={draft.customImage} alt="收藏图片预览" ratio={1.2} /> : <div className="upload-placeholder"><Camera size={38} weight="light" /><strong>选择一张照片</strong><span>JPG、PNG 或 WebP，最大 12 MB</span></div>}<span className="upload-label"><Camera size={18} />{draft.customImage ? '更换图片' : '选择图片'}</span></button><input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" className="visually-hidden" tabIndex={-1} aria-label="选择收藏图片" onChange={event => { readFile(event.target.files?.[0]); event.target.value = ''; }} /><p className="field-hint">图片保存后会自动准备立体效果，你可以先继续浏览。</p></div>
        <div className="form-column"><label className="field-label">名称<input value={draft.title} onChange={event => setDraft(previous => ({ ...previous, title: event.target.value }))} placeholder="给这件收藏起个名字" maxLength={70} required /></label>
          <fieldset className="category-picker"><legend>分类</legend>{categories.map(category => <button key={category.id} type="button" className={draft.category === category.id ? 'selected' : ''} aria-pressed={draft.category === category.id} onClick={() => setDraft(previous => ({ ...previous, category: category.id }))}>{category.label}</button>)}</fieldset>
          <label className="field-label">日期<input type="date" value={draft.date} required onChange={event => setDraft(previous => ({ ...previous, date: event.target.value }))} /></label>
          <label className="field-label">标签 <span>选填，用顿号分隔</span><input value={draft.tags} onChange={event => setDraft(previous => ({ ...previous, tags: event.target.value }))} placeholder="例如：果茶、周末" maxLength={100} /></label>
          <label className="field-label">想记住的话 <span>选填</span><textarea value={draft.note} onChange={event => setDraft(previous => ({ ...previous, note: event.target.value }))} placeholder="那天的天气，或想推荐给朋友的理由…" maxLength={500} rows={4} /></label>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          {versionConflict && <button type="button" className="secondary-button" onClick={() => void runMutation(() => getState(), next => {
            const item = next.collections.find(item => item.id === draft.id);
            if (!item) throw new Error('收藏已被删除，当前草稿仍保留。');
            setDraft(previous => ({ ...previous, version: item.version }));
            setVersionConflict(false);
            setFormError('已读取最新版本。再次保存会使用当前草稿更新内容。');
          }, 'form')}>读取最新版本，保留草稿</button>}
          <button className="primary-button save-button" type="submit"><Check size={19} />{busy ? '正在保存…' : '保存收藏'}</button>
        </div></fieldset></form></div>;
  };
  const renderSearch = () => {
    const scope = route.type === 'search' ? route.scope : 'collection';
    return <>{backBar()}{headingBlock(scope === 'explore' ? '找一份灵感' : scope === 'friends' ? '找朋友的分享' : '找一件收藏')}{searchField('搜索名称、标签或备注')}
      {scope === 'explore' ? <div className="collection-grid search-results">{data.community.filter(item => !query || `${item.title} ${item.author} ${item.description}`.includes(query)).map(item => <PhotoCard key={item.id} item={communityItem(item)} to={`/community/${encoded(item.id)}`} />)}</div>
        : scope === 'friends' ? <div className="collection-grid search-results">{data.shares.filter(item => !query || `${item.sender} ${item.item.title}`.includes(query)).map(item => <PhotoCard key={item.id} item={item.item} to={`/shares/${encoded(item.id)}`} />)}</div> : <>{sourceTabs}{grid(filtered(data.collections))}</>}</>;
  };
  let content: ReactNode;
  if (route.type === 'main') content = route.tab === 'collection' ? renderCollection() : route.tab === 'explore' ? <ExplorePage items={data.community} exploreCategory={exploreCategory} onCategoryChange={setExploreCategory} onView={viewCommunity} toolbar={toolbar} /> : route.tab === 'friends' ? <SharesPage items={data.shares} onlyUnread={onlyUnread} unseen={unseen} busy={busy} onUnreadChange={setOnlyUnread} onView={viewShare} onCollect={addShared} toolbar={toolbar} /> : renderProfile();
  else if (route.type === 'category') content = renderCategory(route.category);
  else if (route.type === 'detail') content = renderDetail();
  else if (route.type === 'add') content = renderAdd();
  else if (route.type === 'search') content = renderSearch();
  else if (route.type === 'month') content = <>{backBar()}{headingBlock(`${route.month.replace('-', '年')}月`)}{grid(filtered(data.collections.filter(item => item.date.startsWith(route.month))))}</>;
  else if (route.type === 'notifications') content = <>{backBar()}{headingBlock('消息', undefined, <button className="text-button" disabled={busy} onClick={() => void runMutation(() => mutate('/api/shares/read-all'), () => setToast('已全部标为查看'))}>全部已读</button>)}<div className="notifications-list">{data.shares.map(item => <Link key={item.id} to={`/shares/${encoded(item.id)}`} className="notification-row" onNavigate={() => viewShare(item.id)}><Art name={item.item.art} src={item.item.customImage} alt={item.item.title} ratio={1} /><span><strong>{item.sender} 分享了一件收藏</strong><small>{item.item.title} · {dateLabel(item.receivedAt)}</small></span>{item.unread && <span className="notice-label">未查看</span>}</Link>)}</div></>;
  else content = <Empty title="没有找到这个页面" text="返回收藏，继续看看喜欢的日子。"><Link to="/collection" className="primary-button">回到我的收藏</Link></Empty>;

  const logout = async () => {
    if (logoutBusy) return;
    setLogoutBusy(true);
    try { await signOut(window.location.origin); }
    catch (error) { if (alive.current) { setStorageError(errorMessage(error)); setLogoutBusy(false); } }
  };
  const navLinks = tabs.map(({ id, label, icon: Icon }) => <Link key={id} to={'/' + id} current={tab === id} className={tab === id ? 'selected' : ''}><Icon size={23} weight={tab === id ? 'fill' : 'regular'} /><span>{label}</span>{id === 'friends' && unseen > 0 && <span className="nav-count">{unseen}</span>}</Link>);

  return <div className="memoverse-shell" data-authenticated="true" data-user-id={session.user.id}>
    <a href="#main-content" className="skip-link">跳到主要内容</a>
    <aside className="sidebar"><Link to="/collection" className="brand-link"><Brand /></Link><p className="sidebar-caption">给记忆一个位置</p><nav aria-label="主导航">{navLinks}</nav><Link to="/add" className="primary-button sidebar-add"><Plus size={20} />添加收藏</Link>
      <div className="sidebar-account"><Link to="/profile"><span className="letter-avatar">{(data.profile.name || session.user.name || session.user.email).slice(0, 1)}</span><span><strong>{data.profile.name || session.user.name}</strong><small>{session.user.email}</small></span></Link><button onClick={() => void logout()} disabled={logoutBusy}><SignOut size={18} />{logoutBusy ? '正在退出…' : '退出登录'}</button></div>
    </aside>
    <header className="mobile-header"><Link to="/collection"><Brand /></Link><button className="icon-button" onClick={() => void logout()} disabled={logoutBusy} aria-label="退出登录"><SignOut size={21} /></button></header>
    <main id="main-content" className="workspace" aria-busy={busy}><div ref={heading} className="page-focus-anchor" tabIndex={-1} />
      {!ready ? <Empty title={storageError ? '暂时无法打开收藏' : '正在打开你的收藏'} text={storageError || '很快就好。'}>{storageError && <button className="primary-button" onClick={() => setBootAttempt(value => value + 1)}>重试连接</button>}</Empty> : <>
        {(storageError || pollError) && <div className="storage-error" role="alert"><WarningCircle size={20} /><span>{storageError || pollError}</span><button disabled={busy} onClick={() => {
          if (storageError && retryAction.current) retryAction.current();
          else void getState().then(state => { if (alive.current) { acceptState(state); setPollError(''); setStorageError(''); } }).catch(error => { if (alive.current) setStorageError(errorMessage(error)); });
        }}>重试</button></div>}{content}
      </>}
    </main>
    <nav className="mobile-navigation" aria-label="手机主导航">{navLinks.slice(0, 2)}<Link to="/add" className="mobile-add"><Plus size={25} /><span>添加</span></Link>{navLinks.slice(2)}</nav>
    {toast && <div className="toast" role="status"><Check size={17} />{toast}</div>}
    <WebDialog open={sheet !== null} onOpenChange={open => { if (!open && !busy) setSheet(null); }} title={sheet === 'profile' ? '编辑资料' : sheet === 'sync' ? '账户与保存状态' : sheet === 'settings' ? '隐私与数据导出' : sheet === 'sort' ? '收藏排序' : sheet === 'delete' ? '删除这件收藏？' : '分享这件美好'}>
      {sheetError && <p className="form-error" role="alert">{sheetError}</p>}
      {sheet === 'profile' && <form onSubmit={event => { event.preventDefault(); void runMutation(() => mutate('/api/profile', 'PATCH', profileDraft), () => { setSheet(null); setToast('资料已保存'); }, 'sheet'); }}><fieldset disabled={busy} className="mutation-fields"><label className="field-label">昵称<input value={profileDraft.name} maxLength={24} required onChange={event => setProfileDraft(previous => ({ ...previous, name: event.target.value }))} /></label><label className="field-label">简介<textarea value={profileDraft.bio} maxLength={100} rows={3} onChange={event => setProfileDraft(previous => ({ ...previous, bio: event.target.value }))} /></label><button className="primary-button wide" type="submit">{busy ? '正在保存…' : '保存资料'}</button></fieldset></form>}
      {sheet === 'sort' && <div className="sheet-options">{[['recent', '最近添加'], ['oldest', '最早添加'], ['name', '按名称排序']].map(([value, label]) => <button key={value} onClick={() => { setSort(value); setSheet(null); }}>{label}{sort === value && <Check size={20} />}</button>)}</div>}
      {sheet === 'sync' && <><div className="status-panel"><Cloud size={35} weight="light" /><h3>收藏已保存在你的账户中</h3><p>共 {data.collections.length} 件收藏</p></div><div className="info-row"><span>登录邮箱</span><strong>{session.user.email}</strong></div><div className="info-row"><span>立体生成</span><strong>{engine?.available === false ? '服务暂不可用，图片已保留' : hasActiveJobs ? '任务处理中' : '查看各收藏的生成状态'}</strong></div><p className="sheet-copy">你可以在浏览器登录同一账户，继续查看和管理收藏。</p></>}
      {sheet === 'settings' && <><p className="sheet-copy">导入的图片不会自动发布到社区。你可以导出当前账户的收藏记录、原图与已有模型。</p><button className="secondary-button wide" disabled={busy} onClick={async () => { if (mutationLock.current) return; mutationLock.current = true; setBusy(true); setSheetError(''); try { await downloadBackup(); if (alive.current) setToast('备份已开始下载'); } catch (error) { if (alive.current) setSheetError(errorMessage(error)); } finally { mutationLock.current = false; if (alive.current) setBusy(false); } }}><Export size={19} />导出账户备份</button></>}
      {sheet === 'delete' && currentItem && <><p className="sheet-copy">“{currentItem.title}”将从你的收藏中移除，其他账户的独立副本不受影响。</p><button className="danger-button wide" disabled={busy} onClick={() => void runMutation(() => mutate(`/api/collections/${encoded(currentItem.id)}`, 'DELETE', { version: currentItem.version }), () => { setSheet(null); navigate('/collection'); setToast('已删除收藏'); }, 'sheet')}>删除收藏</button><button className="secondary-button wide" disabled={busy} onClick={() => setSheet(null)}>保留收藏</button></>}
      {sheet === 'share' && currentItem && <><Art name={currentItem.art} src={currentItem.customImage} alt={currentItem.title} ratio={1.5} /><p className="sheet-copy">先把这段推荐文字分享给朋友。面对面传送完整收藏的功能将在后续客户端中提供。</p><button className="secondary-button wide" onClick={async () => { try { await navigator.clipboard.writeText(`${currentItem.title}\n${currentItem.note}\n${currentItem.tags.join(' · ')}`); setToast('推荐文字已复制'); } catch { setSheetError('暂时无法复制，请检查浏览器权限。'); } }}>复制推荐文字</button></>}
    </WebDialog>
  </div>;
}
