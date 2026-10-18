import type { ReactNode } from 'react';
import { ArrowUpRight } from '@phosphor-icons/react';
import { Art } from '../../art';
import Link from '../../app/Link';
import type { CommunityItem } from '../../data';
import { PageHeading, SectionHeading } from '../../shared/ui';
import PhotoCard from '../collections/PhotoCard';
import { categories, catName } from '../collections/presentation';
import { communityItem } from './communityItem';

export default function ExplorePage({ items, exploreCategory, onCategoryChange, onView, toolbar }: {
  items: CommunityItem[];
  exploreCategory: string;
  onCategoryChange: (category: string) => void;
  onView: (id: string) => void;
  toolbar: ReactNode;
}) {
  const exploreItems = items.filter(item => exploreCategory === 'featured' || item.category === exploreCategory)
    .sort((a, b) => (b.saves * 3 + b.likes * 2 + Math.log1p(b.exposures)) - (a.saves * 3 + a.likes * 2 + Math.log1p(a.exposures)));
  return <>
    {toolbar}
    <PageHeading title="探索" subtitle="从别人的珍藏里，遇见下一份生活灵感。" />
    <div className="filter-tabs" role="group" aria-label="探索分类">
      {[{ id: 'featured', label: '精选' }, ...categories].map(category => <button
        key={category.id}
        aria-pressed={exploreCategory === category.id}
        className={exploreCategory === category.id ? 'active' : ''}
        onClick={() => onCategoryChange(category.id)}
      >
        {category.label}
      </button>)}
    </div>
    <div className="collection-grid explore-grid">
      {exploreItems.map(item => <div key={item.id}>
        <PhotoCard item={communityItem(item)} to={`/community/${encodeURIComponent(item.id)}`} onNavigate={() => onView(item.id)} />
        <p className="community-byline">{item.author}<span>{item.saves} 次收藏 · {item.likes} 人喜欢</span></p>
      </div>)}
    </div>
    <section className="collection-section">
      <SectionHeading title="社区人气榜" />
      <div className="ranking-list">
        {exploreItems.slice(0, 5).map((item, index) => <Link
          key={item.id}
          to={`/community/${encodeURIComponent(item.id)}`}
          className="rank-row"
          onNavigate={() => onView(item.id)}
        >
          <span className="rank-number">{String(index + 1).padStart(2, '0')}</span>
          <Art name={item.art} src={item.customImage} alt={item.title} ratio={1} />
          <span><strong>{item.title}</strong><small>{catName(item.category)} · {item.author}</small></span>
          <ArrowUpRight size={18} />
        </Link>)}
      </div>
      <p className="section-footnote">当前为本次测试的统一初始内容，互动只影响你的账户。</p>
    </section>
  </>;
}
