export const fieldNames = ['title', 'cinema', 'hall', 'seat', 'showtime'] as const;
export type FieldName = typeof fieldNames[number];
export type TicketFields = Record<FieldName, string>;
export interface MediaRef {
  assetId: string; sha256: string; mimeType: string; size: number; width: number; height: number;
  url: string; cloudStatus: 'saved' | 'not_configured';
}
export const processingStatuses = ['not_started', 'queued', 'ocr', 'poster', 'ready', 'no_match', 'failed'] as const;
export type ProcessingStatus = typeof processingStatuses[number];
export interface PosterSource {
  provider: 'wikimedia_commons'; sourcePage: string; sourceFile: string;
  licenseName: string; licenseUrl: string; author: string; fetchedAt: number;
}
export interface PosterMediaRef extends MediaRef { source: PosterSource }
export interface Ticket {
  id: string; fields: TicketFields;
  fieldSources: Record<FieldName, 'user' | 'ocr' | 'migration' | 'empty'>;
  createdAt: number; updatedAt: number; version: number; inputVersion: number;
  deletedAt: number | null; operationId: string;
  media: { original: MediaRef; poster: PosterMediaRef | null };
  processing: { status: ProcessingStatus; error: string | null };
}
export interface AppData { tickets: Ticket[] }
export const emptyFields = (): TicketFields => ({ title: '', cinema: '', hall: '', seat: '', showtime: '' });
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const identifier = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,120}$/.test(value);
const showtime = (value: unknown) => {
  if (value === '') return true;
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) || Number(value.slice(0, 4)) < 1) return false;
  const parsed = new Date(value + ':00Z');
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 16) === value;
};
const integer = (value: unknown, minimum = 0): value is number => Number.isSafeInteger(value) && Number(value) >= minimum;
export function validMedia(value: unknown): value is MediaRef {
  return object(value) && typeof value.assetId === 'string' && /^[a-f0-9]{64}$/.test(value.assetId)
    && typeof value.sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.sha256)
    && value.assetId === value.sha256 && value.url === `/api/media/${value.sha256}`
    && ['image/jpeg', 'image/png', 'image/webp'].includes(String(value.mimeType))
    && integer(value.size, 1) && integer(value.width, 1) && integer(value.height, 1)
    && ['saved', 'not_configured'].includes(String(value.cloudStatus));
}
export function validPosterSource(value: unknown): value is PosterSource {
  if (!object(value) || value.provider !== 'wikimedia_commons' || typeof value.sourceFile !== 'string' || (!value.sourceFile.startsWith('File:') || value.sourceFile.length <= 5)
    || typeof value.sourcePage !== 'string' || typeof value.licenseName !== 'string' || typeof value.licenseUrl !== 'string'
    || typeof value.author !== 'string' || !integer(value.fetchedAt)) return false;
  try {
    if (!value.sourcePage.startsWith('https://commons.wikimedia.org/wiki/File:') || /[\u0000-\u0020\\]/.test(value.sourcePage)) return false;
    if (value.sourceFile.length > 300 || /[\u0000-\u001f\\/<>\[\]{}|]/.test(value.sourceFile)) return false;
    const filename = encodeURIComponent(value.sourceFile.replaceAll(' ', '_')).replace(/['*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase()).replaceAll('%3A', ':').replaceAll('%2C', ',');
    if (value.sourcePage !== 'https://commons.wikimedia.org/wiki/' + filename) return false;
    const page = new URL(value.sourcePage);
    if (page.protocol !== 'https:' || page.hostname !== 'commons.wikimedia.org' || page.port || page.username || page.password
      || page.search || page.hash || !page.pathname.startsWith('/wiki/File:') || page.pathname.length <= '/wiki/File:'.length) return false;
    let licenseUrl: string;
    if (value.licenseName === 'Public domain') return value.licenseUrl === 'https://creativecommons.org/publicdomain/mark/1.0/' || value.licenseUrl === value.sourcePage + '#Licensing';
    if (value.licenseName === 'CC0 1.0') licenseUrl = 'https://creativecommons.org/publicdomain/zero/1.0/';
    else {
      const license = /^CC (BY|BY-SA) (2\.0|2\.5|3\.0|4\.0)$/.exec(value.licenseName);
      if (!license) return false;
      licenseUrl = `https://creativecommons.org/licenses/${license[1].toLowerCase()}/${license[2]}/`;
    }
    return value.licenseUrl === licenseUrl;
  } catch { return false; }
}
function validTicket(value: unknown): value is Ticket {
  if (!object(value) || !identifier(value.id) || !object(value.fields) || !object(value.fieldSources)) return false;
  const fields = value.fields, sources = value.fieldSources;
  return fieldNames.every(key => typeof fields[key] === 'string' && fields[key].length <= (key === 'title' ? 120 : 200) && ['user', 'ocr', 'migration', 'empty'].includes(String(sources[key])))
    && showtime(fields.showtime)
    && integer(value.createdAt) && integer(value.updatedAt) && integer(value.version, 1) && integer(value.inputVersion, 1)
    && (value.deletedAt === null || integer(value.deletedAt)) && identifier(value.operationId)
    && object(value.media) && validMedia(value.media.original) && (value.media.poster === null || validMedia(value.media.poster) && object(value.media.poster) && validPosterSource(value.media.poster.source))
    && object(value.processing) && processingStatuses.includes(value.processing.status as ProcessingStatus)
    && (value.processing.error === null || typeof value.processing.error === 'string')
    && (value.processing.status !== 'ready' || value.media.poster !== null && value.media.poster.cloudStatus === 'saved');
}
export function validateData(value: unknown): asserts value is AppData {
  if (!object(value) || !Array.isArray(value.tickets) || !value.tickets.every(validTicket)
    || new Set(value.tickets.map(ticket => ticket.id)).size !== value.tickets.length) {
    throw new Error('票根数据格式异常，未更新已有界面。');
  }
}
