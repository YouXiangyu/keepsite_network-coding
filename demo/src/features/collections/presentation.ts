import type { Category } from '../../data';

export const categories: { id: Category; label: string }[] = [
  { id: 'movie', label: '电影' }, { id: 'travel', label: '旅行' },
  { id: 'daily', label: '日常' }, { id: 'exhibition', label: '展览' },
];
export const catName = (id: Category) => categories.find(category => category.id === id)?.label ?? '日常';
export const dateLabel = (date: string) => date.replaceAll('-', '.');
