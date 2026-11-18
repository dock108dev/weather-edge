export const q = selector => document.querySelector(selector);
export const count = new Intl.NumberFormat('en-US');
export const figure = new Intl.NumberFormat('en-US', { maximumFractionDigits: 5 });
const names = { kalshi: 'Kalshi', 'polymarket-us': 'Polymarket US' };
export const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
export const name = value => names[value] || String(value || 'Unknown');
const human = value => String(value || '').replaceAll('_', ' ').replaceAll('-', ' ');
export const label = value => { const text = human(value); return text.charAt(0).toUpperCase() + text.slice(1); };
export const paths = {
  market: (venue, instrument, indexAt) => '/?' + new URLSearchParams({
    view: 'markets', venue, instrument, ...(indexAt ? { index_at: indexAt } : {}),
  }),
  research: (kind, run, caseId) => '/?' + new URLSearchParams({
    view: 'research', kind, run, ...(caseId != null ? { case: caseId } : {}),
  }),
};
export const value = data => {
  if (data == null || data === '') return '—';
  if (typeof data === 'boolean') return data ? 'Yes' : 'No';
  if (typeof data === 'number') return figure.format(data);
  if (Array.isArray(data)) return data.map(value).join(' · ');
  return typeof data === 'object' ? JSON.stringify(data) : String(data);
};

export async function read(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

export function external(url, title) {
  try {
    if (new URL(url).protocol === 'https:') return `<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(title)} ↗</a>`;
  } catch {}
  return '';
}

export function metadata(rows) {
  const supported = rows.filter(([, data]) => data != null && data !== '');
  return supported.length ? `<dl>${supported.map(([key, data]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value(data))}</dd>`).join('')}</dl>` : '';
}

export function raw(title, data) {
  return data == null ? '' : `<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
}

export function block(title, body) {
  return body ? `<div class="evidence-block">${title ? `<h3>${esc(title)}</h3>` : ''}${body}</div>` : '';
}

export function message(text) { return `<p class="evidence-placeholder">${esc(text)}</p>`; }

export function timelineLink(station, date, options = {}, title = 'Open weather timeline') {
  if (!station || !date) return '';
  const params = new URLSearchParams({ station, day: date });
  for (const [key, item] of Object.entries(options)) {
    if (item != null && item !== '') params.set(key, item);
  }
  return `<a href="/?${esc(params)}">${esc(title)}</a>`;
}

export function metrics(rows) {
  if (!Array.isArray(rows) || !rows.length) return '';
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  return `<div class="table-wrap"><table><thead><tr>${columns.map(key => `<th scope="col">${esc(label(key))}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(row => '<tr>' + columns.map(k => `<td>${esc(value(row[k]))}</td>`).join('') + '</tr>').join('')}</tbody></table></div>`;
}
