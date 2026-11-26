import { pinyin } from 'pinyin-pro';

type SearchForms = { original: string; fullPinyin: string; initials: string };

const formsCache = new Map<string, SearchForms>();

function normalize(value: string): string {
  return Array.from(value.toLowerCase()).filter(character => /[\p{L}\p{N}]/u.test(character)).join('');
}

function formsFor(value: string): SearchForms {
  const cached = formsCache.get(value);
  if (cached) return cached;

  const forms: SearchForms = {
    original: normalize(value),
    fullPinyin: normalize(String(pinyin(value, { toneType: 'none', nonZh: 'consecutive' }))),
    initials: normalize(String(pinyin(value, { pattern: 'first', toneType: 'none', nonZh: 'consecutive' }))),
  };
  formsCache.set(value, forms);
  if (formsCache.size > 512) formsCache.delete(formsCache.keys().next().value!);
  return forms;
}

function isSubsequence(query: string, target: string): boolean {
  const wanted = Array.from(query);
  let index = 0;
  for (const character of target) {
    if (character === wanted[index]) index += 1;
    if (index === wanted.length) return true;
  }
  return false;
}

/** Match a query against a ticket title and cinema using identical rules. */
export function matchesTicketSearch(query: string, ...fields: string[]): boolean {
  const normalizedQuery = normalize(query.trim());
  if (!normalizedQuery) return true;

  return fields.some(field => {
    const forms = formsFor(field);
    return forms.original.includes(normalizedQuery)
      || isSubsequence(normalizedQuery, forms.original)
      || forms.fullPinyin.includes(normalizedQuery)
      || forms.initials.includes(normalizedQuery);
  });
}
