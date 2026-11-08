import type { Ticket } from '../../data';
import Link from '../../app/Link';
import TicketImage from './TicketImage';
import { processingLabels } from './processing';

export default function TicketCard({ ticket, to, onOpen }: { ticket: Ticket; to: string; onOpen?: () => void }) {
  return <article className="ticket-card"><Link to={to} onNavigate={onOpen}><TicketImage retry={false} media={ticket.media.poster ?? ticket.media.original} title={ticket.fields.title} /><div className="ticket-card-copy"><h2>{ticket.fields.title || '待补充片名'}</h2><p className="ticket-cinema">{ticket.fields.cinema || '影院未填写'}</p><p className="ticket-date">{ticket.fields.showtime ? <time dateTime={ticket.fields.showtime.slice(0, 10)}>{ticket.fields.showtime.slice(0, 10).replace(/-/g, '.')}</time> : '日期待补充'}</p>{!['not_started', 'ready'].includes(ticket.processing.status) && <span className="status-pill" data-state={ticket.processing.status}>{processingLabels[ticket.processing.status]}</span>}{ticket.media.original.cloudStatus !== 'saved' && <p className="ticket-cloud-warning">原票尚未云存</p>}{ticket.media.poster && ticket.media.poster.cloudStatus !== 'saved' && <p className="ticket-cloud-warning">海报尚未云存</p>}</div></Link></article>;
}
