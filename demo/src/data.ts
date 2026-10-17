export type Category = 'movie' | 'travel' | 'daily' | 'exhibition';

export interface CollectionItem {
  id: string;
  title: string;
  category: Category;
  tags: string[];
  note: string;
  date: string;
  addedAt: number;
  source: 'mine' | 'friend';
  art: string;
  customImage?: string;
  sender?: string;
  version: number;
  assetId?: string;
  generation: 'reference' | 'queued' | 'processing' | 'blocked' | 'ready' | 'failed';
  generationJobId?: string;
  generationError?: string;
  modelUrl?: string;
  modelCamera?: unknown;
}

export interface CommunityItem {
  id: string;
  title: string;
  category: Category;
  art: string;
  author: string;
  description: string;
  exposures: number;
  saves: number;
  likes: number;
}

export interface ShareItem {
  id: string;
  item: CollectionItem;
  sender: string;
  receivedAt: string;
  unread: boolean;
}

export interface AppData {
  collections: CollectionItem[];
  shares: ShareItem[];
  community: CommunityItem[];
  profile: { name: string; bio: string };
  likedCommunityIds: string[];
}

type LegacyCollectionItem = Omit<CollectionItem, 'version' | 'generation'> & {
  version?: number;
  generation: CollectionItem['generation'] | 'pending';
};
export type LegacyAppData = Omit<AppData, 'collections' | 'shares'> & {
  collections: LegacyCollectionItem[];
  shares: (Omit<ShareItem, 'item'> & { item: LegacyCollectionItem })[];
};
export interface MigrationSource {
  migrationId: string;
  completedServerIds: string[];
  data?: LegacyAppData;
}

/** All initial content and engagement counts are demonstration data, not live activity. */
export function makeSeedData(): AppData {
  const examples: [string, Category, string, string, string][] = [
    ['海边的一天', 'travel', 'sea', '旅行', '沿着海岸走了一整天，把海风留在这里。'],
    ['海边来信', 'movie', 'lighthouse', '剧情', '这部电影想推荐给你。'],
    ['雨夜列车', 'movie', 'train', '悬疑', '窗外的灯光，和一场安静的电影。'],
    ['春日放映', 'movie', 'spring', '电影', '想再看一次的春天。'],
    ['一张旧票', 'movie', 'ticket', '电影', '一张电影票，记得那次散场后的心情。'],
    ['散场之后', 'movie', 'cinema', '电影', '散场后，我们又聊了很久。'],
    ['光影之间', 'movie', 'film', '电影', '喜欢大银幕亮起来的瞬间。'],
    ['桂花拿铁', 'daily', 'cafe', '咖啡', '桂花的香气，刚好的温度。'],
    ['周末小馆', 'daily', 'pasta', '美食', '慢慢吃一顿饭，把周末留长一点。'],
    ['街角面包', 'daily', 'bread', '美食', '刚出炉的面包，是路过的小惊喜。'],
    ['午后阳光', 'daily', 'cat', '日常', '猫和阳光，都在窗边。'],
    ['城市的一天', 'travel', 'city', '旅行', '拐进小巷，遇见另一面的城市。'],
    ['爵士现场', 'daily', 'jazz', '音乐', '今晚的旋律，想留到明天。'],
    ['光的形状', 'exhibition', 'exhibition', '展览', '走进光里，停留了一会儿。'],
  ];
  const collections = examples.map(([title, category, art, tag, note], index): CollectionItem => {
    const date = ['2026-10-14','2026-10-12','2026-10-05','2026-09-28','2026-10-01','2026-09-20','2026-09-15','2026-10-11','2026-10-09','2026-10-06','2026-10-04','2026-10-03','2026-10-10','2026-10-08'][index];
    return {
      id: `sample-collection-${index + 1}`, title, category, art, tags: [tag],
      note, date, addedAt: Date.parse(`${date}T12:00:00+08:00`),
      source: index === 4 ? 'friend' : 'mine', sender: index === 4 ? '小林' : undefined, generation: 'reference', version: 1,
    };
  });
  const shares = [
    [1, '小林', '2026-10-14'], [11, '小雨', '2026-10-12'],
    [12, '阿哲', '2026-10-10'], [13, '小周', '2026-10-05'],
  ].map(([index, sender, receivedAt], position): ShareItem => ({
    id: `sample-share-${position + 1}`,
    item: {
      ...collections[Number(index)], tags: [...collections[Number(index)].tags],
      id: `sample-shared-item-${position + 1}`, source: 'friend', sender: String(sender),
    },
    sender: String(sender), receivedAt: String(receivedAt), unread: position < 3,
  }));
  const community = [11, 3, 13, 7, 4, 12].map((index, position): CommunityItem => {
    const item = collections[index];
    return {
      id: `sample-community-${position + 1}`, title: position === 1 ? '露天电影之夜' : item.title, category: item.category,
      art: position === 1 ? 'outdoor' : item.art, author: ['小雨', '阿宁', '小周', '安安', '阿远', '阿哲'][position],
      description: `示例内容 · ${item.note}`,
      exposures: [1280, 960, 842, 720, 605, 486][position],
      saves: [128, 96, 84, 72, 60, 48][position],
      likes: [236, 184, 156, 132, 108, 92][position],
    };
  });
  return {
    collections, shares, community,
    profile: { name: '小宇', bio: '把喜欢的日子留下来' }, likedCommunityIds: [],
  };
}

const DB_NAME = 'memoverse-prototype';
const STORE_NAME = 'app-data';
const DATA_KEY = 'current';
const MIGRATION_KEY = 'api-migration';
const categories: readonly string[] = ['movie', 'travel', 'daily', 'exhibition'];
const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === 'string';
const isStrings = (value: unknown): value is string[] => Array.isArray(value) && value.every(isString);
const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

function isCollection(value: unknown, legacy = false): boolean {
  return isObject(value) && isString(value.id) && isString(value.title)
    && isString(value.category) && categories.includes(value.category)
    && isStrings(value.tags) && isString(value.note) && isString(value.date)
    && isNumber(value.addedAt) && (value.source === 'mine' || value.source === 'friend')
    && isString(value.art) && (value.customImage === undefined || isString(value.customImage))
    && (value.sender === undefined || isString(value.sender))
    && isString(value.generation)
    && ['reference', 'queued', 'processing', 'blocked', 'ready', 'failed', ...(legacy ? ['pending'] : [])].includes(value.generation)
    && ((legacy && value.version === undefined) || (isNumber(value.version) && Number.isInteger(value.version) && value.version >= 1))
    && ['assetId', 'generationJobId', 'generationError', 'modelUrl'].every(key => value[key] === undefined || value[key] === null || isString(value[key]));
}

function isCommunity(value: unknown): value is CommunityItem {
  return isObject(value) && ['id', 'title', 'art', 'author', 'description'].every(key => isString(value[key]))
    && isString(value.category) && categories.includes(value.category)
    && ['exposures', 'saves', 'likes'].every(key => isNumber(value[key]) && Number.isInteger(value[key]) && value[key] >= 0);
}

export function validateData(value: unknown, legacy = false): asserts value is AppData {
  if (!isObject(value)
    || !Array.isArray(value.collections) || !value.collections.every(item => isCollection(item, legacy))
    || !Array.isArray(value.shares) || !value.shares.every(share =>
      isObject(share) && isString(share.id) && isCollection(share.item, legacy)
      && isString(share.sender) && isString(share.receivedAt) && typeof share.unread === 'boolean')
    || !Array.isArray(value.community) || !value.community.every(isCommunity)
    || !isObject(value.profile) || !isString(value.profile.name) || !isString(value.profile.bio)
    || !isStrings(value.likedCommunityIds)) {
    throw new Error('本地数据格式异常，未覆盖已有数据。');
  }
  for (const entries of [value.collections, value.shares, value.community]) {
    if (new Set(entries.map(entry => entry.id)).size !== entries.length) {
      throw new Error('本地数据含重复标识，未覆盖已有数据。');
    }
  }
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('当前环境无法使用 IndexedDB，本地数据未保存。'));
      return;
    }
    const request = indexedDB.open(DB_NAME, 1);
    let blocked = false;
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) request.result.createObjectStore(STORE_NAME);
    };
    request.onerror = () => reject(request.error ?? new Error('无法打开本地数据库。'));
    request.onblocked = () => {
      blocked = true;
      reject(new Error('本地数据库被其他页面占用，请关闭旧页面后重试。'));
    };
    request.onsuccess = () => {
      if (blocked) { request.result.close(); return; }
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
  });
}

/** Read the original snapshot without creating seed data or changing `current`.
 * A separate metadata key establishes a stable identity atomically across tabs.
 */
export async function readMigrationSource(): Promise<MigrationSource> {
  const database = await openDatabase();
  try {
    return await new Promise<MigrationSource>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, 'readwrite');
      const store = transaction.objectStore(STORE_NAME);
      let result: MigrationSource;
      let failure: unknown;
      transaction.oncomplete = () => resolve(result);
      transaction.onabort = () => reject(failure ?? transaction.error ?? new Error('读取本地数据失败。'));
      const request = store.get(DATA_KEY);
      request.onsuccess = () => {
        try {
          const data: unknown = request.result;
          if (data !== undefined) validateData(data, true);
          const metadata = store.get(MIGRATION_KEY);
          metadata.onsuccess = () => {
            try {
              let record: unknown = metadata.result;
              if (record === undefined) {
                record = { migrationId: crypto.randomUUID(), completedServerIds: [] };
                store.put(record, MIGRATION_KEY);
              }
              if (!isObject(record) || !isString(record.migrationId) || !isStrings(record.completedServerIds)) {
                throw new Error('迁移记录损坏，原收藏未修改。');
              }
              result = { migrationId: record.migrationId, completedServerIds: record.completedServerIds, data: data as LegacyAppData | undefined };
            } catch (error) { failure = error; transaction.abort(); }
          };
        } catch (error) { failure = error; transaction.abort(); }
      };
    });
  } finally { database.close(); }
}

/** Only acknowledge migration after the server has committed the entire import. */
export async function markMigrationCompleted(migrationId: string, serverId: string): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, 'readwrite');
      transaction.oncomplete = () => resolve();
      let failure: unknown;
      transaction.onabort = () => reject(failure ?? transaction.error ?? new Error('无法记录迁移完成状态；旧收藏仍保留。'));
      const store = transaction.objectStore(STORE_NAME);
      const request = store.get(MIGRATION_KEY);
      request.onsuccess = () => {
        try {
          const record: unknown = request.result;
          if (!isObject(record) || record.migrationId !== migrationId || !isStrings(record.completedServerIds)) {
            throw new Error('迁移标识不一致，原收藏未修改。');
          }
          store.put({ migrationId, completedServerIds: [...new Set([...record.completedServerIds, serverId])] }, MIGRATION_KEY);
        } catch (error) { failure = error; transaction.abort(); }
      };
    });
  } finally { database.close(); }
}

/** Literal name/keyword matching only; each matching record contributes one vote per tag. */
export function inferTag(title: string, items: CollectionItem[]): string | undefined {
  const normalize = (text: string) => text.normalize('NFKC').trim().toLocaleLowerCase();
  const query = normalize(title);
  if (!query) return undefined;
  const keywords = query.split(/[\s\p{P}\p{S}]+/u).filter(Boolean);
  const counts = new Map<string, number>();
  for (const item of items) {
    const name = normalize(item.title);
    if (!name || !(name.includes(query) || query.includes(name)
      || keywords.some(keyword => name.includes(keyword)))) continue;
    for (const tag of new Set(item.tags.map(tag => tag.trim()).filter(Boolean))) {
      counts.set(tag, (counts.get(tag) ?? 0) + 1);
    }
  }
  let winner: string | undefined;
  let highest = 0;
  for (const [tag, count] of counts) {
    if (count > highest) { highest = count; winner = tag; }
  }
  return winner;
}
