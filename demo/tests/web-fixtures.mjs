export const signedSession = (id = 'a') => ({
  authenticated: true, configured: true, providers: { email: true, google: false },
  user: { id, email: `${id}@example.test`, name: `用户 ${id}` }, csrfToken: `test-csrf-${id}`,
});
export const media = (cloudStatus = 'saved') => ({ assetId: 'a'.repeat(64), sha256: 'a'.repeat(64), url: '/api/media/' + 'a'.repeat(64), mimeType: 'image/png', size: 68, width: 1, height: 1, cloudStatus });
export const sampleItem = (id = 'item-1') => ({
  id, fields: { title: '星际穿越', cinema: '光影影院', hall: '3 号厅', seat: '5 排 6 座', showtime: '2026-10-22T19:30' },
  fieldSources: { title: 'user', cinema: 'user', hall: 'user', seat: 'user', showtime: 'user' },
  createdAt: 100, updatedAt: 100, version: 1, inputVersion: 1, deletedAt: null, operationId: 'initial',
  media: { original: media(), poster: null }, processing: { status: 'not_started', error: null },
});
export const sampleData = () => ({ tickets: [sampleItem()] });
export const serverState = (id = 'a') => ({
  initialized: true, serverId: 'server', userId: id, revision: 7, data: sampleData(),
  capabilities: { processing: false, cloudStorage: true, deviceSync: false },
});
