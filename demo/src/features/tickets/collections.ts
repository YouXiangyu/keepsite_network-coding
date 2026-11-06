import { validShowtime, type Ticket } from '../../data';

export const RECENT_TICKET_LIMIT = 10;
export const HOME_MONTH_LIMIT = 6;
export const MONTH_PREVIEW_LIMIT = 2;
export type TicketOrder = 'recent' | 'oldest';
export interface MonthCollection { month: string; label: string; tickets: Ticket[] }

/** Showtime is a local wall clock, not an instant to convert between time zones. */
export function ticketMonth(showtime: string, createdAt: number): string | null {
  if (showtime && validShowtime(showtime)) return showtime.slice(0, 7);
  if (!Number.isSafeInteger(createdAt)) return null;
  const date = new Date(createdAt), year = date.getFullYear();
  // Unrepresentable dates stay accessible in the library without an invented month.
  if (!Number.isFinite(date.getTime()) || year < 1 || year > 9999) return null;
  return `${String(year).padStart(4, '0')}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

export function monthLabel(month: string): string | null {
  if (!/^(?!0000)\d{4}-(0[1-9]|1[0-2])$/.test(month)) return null;
  return `${month.slice(0, 4)}年${Number(month.slice(5))}月`;
}

export function orderedTickets(tickets: readonly Ticket[], order: TicketOrder = 'recent'): Ticket[] {
  return tickets.filter(ticket => ticket.deletedAt === null).sort((a, b) => {
    const aValid = Number.isSafeInteger(a.createdAt), bValid = Number.isSafeInteger(b.createdAt);
    if (aValid !== bValid) return aValid ? -1 : 1;
    if (aValid && a.createdAt !== b.createdAt) return (a.createdAt < b.createdAt ? -1 : 1) * (order === 'oldest' ? 1 : -1);
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

/** Always derive from the current account snapshot; never retain a second business store. */
export function ticketCollections(tickets: readonly Ticket[]) {
  const active = orderedTickets(tickets), groups = new Map<string, Ticket[]>(), undated: Ticket[] = [];
  for (const ticket of active) {
    const month = ticketMonth(ticket.fields.showtime, ticket.createdAt);
    if (month === null) { undated.push(ticket); continue; }
    const group = groups.get(month);
    if (group) group.push(ticket);
    else groups.set(month, [ticket]);
  }
  const months: MonthCollection[] = [...groups].sort(([a], [b]) => a < b ? 1 : a > b ? -1 : 0)
    .map(([month, values]) => ({ month, label: monthLabel(month)!, tickets: values }));
  return { tickets: active, recent: active.slice(0, RECENT_TICKET_LIMIT), months, undated };
}
