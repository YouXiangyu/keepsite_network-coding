export const fieldNames = ['title', 'cinema', 'hall', 'seat', 'showtime'] as const;
export type FieldName = typeof fieldNames[number];
export type TicketFields = Record<FieldName, string>;
export interface MediaRef {
  assetId: string; sha256: string; mimeType: string; size: number; width: number; height: number;
  url: string; cloudStatus: 'saved' | 'not_configured';
}
export interface Ticket {
  id: string; fields: TicketFields;
  fieldSources: Record<FieldName, 'user' | 'ocr' | 'migration' | 'empty'>;
  createdAt: number; updatedAt: number; version: number; inputVersion: number;
  deletedAt: number | null; operationId: string;
  media: { original: MediaRef; poster: MediaRef | null };
  processing: { status: 'not_started'; error: null };
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
function validTicket(value: unknown): value is Ticket {
  if (!object(value) || !identifier(value.id) || !object(value.fields) || !object(value.fieldSources)) return false;
  const fields = value.fields, sources = value.fieldSources;
  return fieldNames.every(key => typeof fields[key] === 'string' && fields[key].length <= (key === 'title' ? 120 : 200) && ['user', 'ocr', 'migration', 'empty'].includes(String(sources[key])))
    && showtime(fields.showtime)
    && integer(value.createdAt) && integer(value.updatedAt) && integer(value.version, 1) && integer(value.inputVersion, 1)
    && (value.deletedAt === null || integer(value.deletedAt)) && identifier(value.operationId)
    && object(value.media) && validMedia(value.media.original) && (value.media.poster === null || validMedia(value.media.poster))
    && object(value.processing) && value.processing.status === 'not_started' && value.processing.error === null;
}
export function validateData(value: unknown): asserts value is AppData {
  if (!object(value) || !Array.isArray(value.tickets) || !value.tickets.every(validTicket)
    || new Set(value.tickets.map(ticket => ticket.id)).size !== value.tickets.length) {
    throw new Error('票根数据格式异常，未更新已有界面。');
  }
}
