import type { CollectionItem, CommunityItem } from '../../data';
import { catName } from '../collections/presentation';

export function communityItem(item: CommunityItem): CollectionItem {
  return { id: `community-${item.id}`, title: item.title, category: item.category, tags: [catName(item.category)],
    note: item.description, date: '', addedAt: 0, source: 'friend', sender: item.author, art: item.art, version: 1,
    customImage: item.customImage, assetId: item.assetId, modelId: item.modelId, modelUrl: item.modelUrl,
    modelCamera: item.modelCamera, generation: item.generation, generationJobId: item.generationJobId };
}
