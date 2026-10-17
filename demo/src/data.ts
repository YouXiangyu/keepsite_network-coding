export type Category = 'movie' | 'travel' | 'daily' | 'exhibition';
export type Generation = 'reference' | 'queued' | 'processing' | 'blocked' | 'ready' | 'failed';
export interface MediaFields {
  customImage?: string;
  assetId?: string;
  modelId?: string;
  modelUrl?: string;
  modelCamera?: unknown;
}
export interface CollectionItem extends MediaFields {
  id: string;
  title: string;
  category: Category;
  tags: string[];
  note: string;
  date: string;
  addedAt: number;
  source: 'mine' | 'friend';
  art: string;
  sender?: string;
  version: number;
  generation: Generation;
  generationJobId?: string;
  generationError?: string;
}
export interface CommunityItem extends MediaFields {
  id: string;
  title: string;
  category: Category;
  art: string;
  author: string;
  description: string;
  exposures: number;
  saves: number;
  likes: number;
  generation: Generation;
  generationJobId?: string;
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

const categories = ['movie', 'travel', 'daily', 'exhibition'];
const generations = ['reference', 'queued', 'processing', 'blocked', 'ready', 'failed'];
const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === 'string';
const isStrings = (value: unknown): value is string[] => Array.isArray(value) && value.every(isString);
const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const mediaValid = (value: Record<string, unknown>) =>
  ['assetId', 'modelId', 'generationJobId', 'generationError', 'sender'].every(key => value[key] == null || isString(value[key]))
  && ['customImage', 'modelUrl'].every(key => value[key] == null || (isString(value[key]) && value[key].startsWith('/api/')));

function isCollection(value: unknown): value is CollectionItem {
  return isObject(value) && ['id', 'title', 'note', 'date', 'art'].every(key => isString(value[key]))
    && isString(value.category) && categories.includes(value.category)
    && isStrings(value.tags) && isNumber(value.addedAt)
    && (value.source === 'mine' || value.source === 'friend')
    && isString(value.generation) && generations.includes(value.generation)
    && isNumber(value.version) && Number.isInteger(value.version) && value.version >= 1
    && mediaValid(value) && (value.generation !== 'ready' || isString(value.modelUrl));
}
function isCommunity(value: unknown): value is CommunityItem {
  return isObject(value) && ['id', 'title', 'art', 'author', 'description'].every(key => isString(value[key]))
    && isString(value.category) && categories.includes(value.category)
    && ['exposures', 'saves', 'likes'].every(key => isNumber(value[key]) && Number.isInteger(value[key]) && value[key] >= 0)
    && isString(value.generation) && generations.includes(value.generation)
    && mediaValid(value) && (value.generation !== 'ready' || isString(value.modelUrl));
}
export function validateData(value: unknown): asserts value is AppData {
  if (!isObject(value)
    || !Array.isArray(value.collections) || !value.collections.every(isCollection)
    || !Array.isArray(value.shares) || !value.shares.every(share => isObject(share) && isString(share.id)
      && isCollection(share.item) && isString(share.sender) && isString(share.receivedAt) && typeof share.unread === 'boolean')
    || !Array.isArray(value.community) || !value.community.every(isCommunity)
    || !isObject(value.profile) || !isString(value.profile.name) || !isString(value.profile.bio)
    || !isStrings(value.likedCommunityIds)) {
    throw new Error('收藏数据格式异常，未更新已有界面。');
  }
  for (const entries of [value.collections, value.shares, value.community]) {
    if (new Set(entries.map(entry => entry.id)).size !== entries.length) {
      throw new Error('收藏数据含重复标识，未更新已有界面。');
    }
  }
}

/** Literal historical name matching: never invent a label for a new subject. */
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
