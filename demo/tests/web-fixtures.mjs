export const signedSession = (id = 'a') => ({
  authenticated: true, configured: true, providers: { email: true, google: false },
  user: { id, email: `${id}@example.test`, name: `用户 ${id}` }, csrfToken: `test-csrf-${id}`,
});
export const sampleItem = (id = 'item-1') => ({
  id, title: '海边的一天', category: 'travel', tags: ['旅行'], note: '沿着海岸走了一整天。',
  date: '2026-10-14', addedAt: 1, source: 'mine', art: 'sea', version: 1,
  generation: 'ready', customImage: `/api/media/${id}`, modelUrl: `/api/models/${id}/model.glb`,
});
export const sampleData = () => ({
  collections: [sampleItem()], shares: [],
  community: [{ id: 'community-1', title: '社区的海边', category: 'travel', art: 'sea', author: '小雨',
    description: '一份推荐', exposures: 100, saves: 10, likes: 20, generation: 'ready',
    customImage: '/api/media/community-1', modelUrl: '/api/models/community-1/model.glb' }],
  profile: { name: '测试用户', bio: '保留喜欢的瞬间' }, likedCommunityIds: [],
});
export const serverState = (id = 'a') => ({
  generationRecovery: [], initialized: true, serverId: 'server', userId: id, revision: 7, data: sampleData(),
  generationEngine: { available: true, reason: 'fixture', pipeline: 'AnyImage' },
});
