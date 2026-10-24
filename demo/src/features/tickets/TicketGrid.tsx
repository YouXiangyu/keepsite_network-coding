import { useLayoutEffect, useRef, type ReactNode } from 'react';

export default function TicketGrid({ children }: { children: ReactNode }) {
  const grid = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!grid.current) return;
    const cards = Array.from(grid.current.children) as HTMLElement[];
    const mobile = window.matchMedia('(max-width: 760px)');
    const measure = () => {
      const rows = cards.map(card => {
        const content = card.firstElementChild;
        // Measure content rather than the card whose grid span we change.
        return mobile.matches && content ? String(Math.ceil(content.getBoundingClientRect().height + 8)) : '';
      });
      cards.forEach((card, index) => {
        if (card.style.getPropertyValue('--ticket-rows') === rows[index]) return;
        if (rows[index]) card.style.setProperty('--ticket-rows', rows[index]);
        else card.style.removeProperty('--ticket-rows');
      });
    };
    const observer = new ResizeObserver(measure);
    for (const card of cards) if (card.firstElementChild) observer.observe(card.firstElementChild);
    mobile.addEventListener('change', measure);
    measure();
    return () => {
      observer.disconnect();
      mobile.removeEventListener('change', measure);
      for (const card of cards) card.style.removeProperty('--ticket-rows');
    };
  }, [children]);
  return <div ref={grid} className="ticket-grid">{children}</div>;
}
