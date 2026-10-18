import { ArrowUpRight } from '@phosphor-icons/react';
import { Art, Tilt } from '../../art';
import Link from '../../app/Link';
import type { CollectionItem } from '../../data';
import { catName, dateLabel } from './presentation';

export default function PhotoCard({ item, to, onNavigate, hero = false }: {
  item: CollectionItem; to?: string; onNavigate?: () => void; hero?: boolean;
}) {
  return <Link to={to ?? `/collection/${encodeURIComponent(item.id)}`} className={`photo-card ${hero ? 'hero-card' : ''}`} onNavigate={onNavigate}>
    <Tilt>
      <Art name={item.art} src={item.customImage} alt={item.title} ratio={hero ? 1.5 : 1} />
      {item.generation === 'ready' && item.modelUrl && <span className="relief-badge">立体收藏</span>}
    </Tilt>
    <div className="card-caption">
      <div>
        <h3>{item.title}</h3>
        <p>{catName(item.category)}{item.date && <> · {dateLabel(item.date)}</>}</p>
      </div>
      <ArrowUpRight size={18} />
    </div>
  </Link>;
}
