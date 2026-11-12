'use strict';

// This is a read-only renderer. No credentials, orders, or persistent browser state.
const STALE_AFTER_MS = 30 * 60 * 1000;
let report = null;
const view = { window: '24h', metric: 'realized_pnl', status: 'all', search: '', category: 'all', sort: 'newest' };

function number(value) {
  if (value === null || value === undefined || typeof value === 'boolean' || (typeof value === 'string' && !value.trim()) || !['string', 'number'].includes(typeof value)) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function validateReport(data) {
  if (!data || data.schema_version !== 2 || data.paper_only !== true || !data.summary || !data.periods || !data.performance || !Array.isArray(data.performance.points) || !data.performance.windows || !Array.isArray(data.positions) || !Array.isArray(data.strategies)) {
    throw new Error('Unsupported or incomplete paper ledger. Expected schema version 2.');
  }
  return data;
}

function text(id, value) { document.getElementById(id).textContent = value; }
function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}
function date(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Unavailable';
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function money(value) {
  const parsed = number(value);
  if (parsed === null) return 'Unavailable';
  return `${parsed < 0 ? '−' : ''}${new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Math.abs(parsed))}`;
}
function overview(data) {
  const s = data.summary;
  const unknown = number(s.unpriced_positions);
  const complete = unknown === 0;
  return {
    total: complete ? money(s.total_pnl) : 'Unavailable',
    realized: money(s.realized_pnl), open: complete ? money(s.unrealized_pnl) : 'Unavailable',
    cash: money(s.cash), risk: money(s.open_cost),
    unknown: unknown === null ? 'Mark coverage unavailable' : `${unknown} unpriced position${unknown === 1 ? '' : 's'} · ${complete ? 'all marks known' : 'total P&L is unknown'}`,
    periods: ['day', 'week', 'month'].map(key => {
      const p = data.periods[key] || {};
      return { key, realized: money(p.realized_pnl), fees: money(p.fees), settled: number(p.settled), wins: number(p.wins), losses: number(p.losses) };
    })
  };
}
function count(value) { const parsed = number(value); return parsed === null ? 'Unavailable' : String(parsed); }
function renderOverview(data) {
  const model = overview(data);
  for (const key of ['total', 'realized', 'open', 'cash', 'risk']) text(`metric-${key}`, model[key]);
  text('unknown-count', model.unknown);
  text('bankroll', `${money(data.summary.initial_bankroll)} initial paper bankroll`);
  text('portfolio-counts', `${count(data.summary.total_positions)} positions · ${count(data.summary.open_positions)} open · ${count(data.summary.settled)} settled`);
  const grid = document.getElementById('calendar-tallies');
  grid.replaceChildren();
  for (const period of model.periods) {
    const card = element('article', undefined, 'calendar-card');
    card.append(element('h3', {day:'Today', week:'This week', month:'This month'}[period.key]), element('p', period.realized, 'calendar-value'));
    card.append(element('p', `${count(period.settled)} settled · ${count(period.wins)} won · ${count(period.losses)} lost`, 'small muted'));
    card.append(element('p', `Fees ${period.fees}`, 'small muted'));
    grid.append(card);
  }
}
function chartModel(data, windowKey, metric) {
  const window = data.performance.windows[windowKey] || {};
  const inception = Date.parse(data.tracking_started_at);
  const start = Math.max(Date.parse(window.start), inception);
  const end = Date.parse(window.end);
  const points = data.performance.points.filter(p => p && Number.isFinite(Date.parse(p.time)))
    .map(p => ({ time: p.time, timestamp: Date.parse(p.time), value: number(p[metric]) }))
    .filter(p => p.timestamp >= start && p.timestamp <= end)
    .sort((a, b) => a.timestamp - b.timestamp);
  const segments = [];
  let segment = [];
  for (const point of points) {
    if (point.value === null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else segment.push(point);
  }
  if (segment.length) segments.push(segment);
  return { start, end, points, segments, missing: points.filter(p => p.value === null).length };
}
function chartGeometry(model, availableWidth) {
  const width = Math.max(240, Math.floor(availableWidth));
  const height = width < 600 ? 180 : 240;
  const known = model.points.filter(p => p.value !== null);
  const low = known.reduce((minimum, p) => Math.min(minimum, p.value), Infinity);
  const high = known.reduce((maximum, p) => Math.max(maximum, p.value), -Infinity);
  const pad = high === low ? Math.max(1, Math.abs(low) * .1) : (high - low) * .15;
  const minimum = low - pad, maximum = high + pad;
  return { width, height, ticks: [maximum, (maximum + minimum) / 2, minimum],
    x: point => 75 + (model.end === model.start ? .5 : (point.timestamp - model.start) / (model.end - model.start)) * (width - 87),
    y: value => 16 + (maximum - value) / (maximum - minimum) * (height - 32)
  };
}
function svgElement(tag, attributes = {}, value) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, val] of Object.entries(attributes)) node.setAttribute(key, String(val));
  if (value !== undefined) node.textContent = value;
  return node;
}
function renderChart(data) {
  const model = chartModel(data, view.window, view.metric);
  const label = view.metric === 'realized_pnl' ? 'Realized P&L' : 'Total P&L';
  const known = model.points.filter(p => p.value !== null);
  text('chart-current', money(model.points.at(-1)?.value));
  text('chart-metric-label', `${label} · USD · latest observation in window`);
  const flat = known.length > 1 && known.every(p => p.value === known[0].value);
  let summary = model.points.length ? `${model.points.length} observed points · ${model.missing} unavailable. ` : 'No observations in this window. ';
  summary += flat ? `Observed ${label.toLowerCase()} is flat at ${money(known[0].value)}. ` : '';
  summary += view.metric === 'total_pnl' ? 'Only known marks are drawn; gaps are unknown, not zero.' : 'Realized results change only on settlement. No sample returns.';
  text('chart-summary', summary);
  text('chart-inception', `Tracking began ${date(data.tracking_started_at)}. No history before inception; longer windows do not create earlier data. Display times are local.`);
  text('chart-start', date(Number.isFinite(model.start) ? new Date(model.start).toISOString() : null));
  text('chart-end', date(Number.isFinite(model.end) ? new Date(model.end).toISOString() : null));
  const container = document.getElementById('performance-chart');
  container.replaceChildren();
  const geometry = chartGeometry(model, container.clientWidth);
  const svg = svgElement('svg', { viewBox: `0 0 ${geometry.width} ${geometry.height}`, role: 'img', 'aria-labelledby': 'svg-title svg-description' });
  svg.append(svgElement('title', { id: 'svg-title' }, `${label} · ${view.window}`), svgElement('desc', { id: 'svg-description' }, summary));
  // Axis padding changes the scale only. It never adds a point or extends a line.
  if (known.length) {
    const { x, y } = geometry;
    for (const value of geometry.ticks) {
      svg.append(svgElement('line', { x1: 75, x2: geometry.width - 12, y1: y(value), y2: y(value), class: 'chart-grid' }));
      svg.append(svgElement('text', { x: 0, y: y(value) + 4, class: 'chart-axis' }, money(value)));
    }
    for (const segment of model.segments) {
      if (segment.length > 1) svg.append(svgElement('polyline', { points: segment.map(p => `${x(p)},${y(p.value)}`).join(' '), class: 'chart-line', 'vector-effect': 'non-scaling-stroke' }));
      for (const point of segment) {
        const dot = svgElement('circle', { cx: x(point), cy: y(point.value), r: 3.5, class: 'chart-dot' });
        dot.append(svgElement('title', {}, `${date(point.time)}: ${money(point.value)}`));
        svg.append(dot);
      }
    }
  } else {
    svg.append(svgElement('text', { x: geometry.width / 2, y: geometry.height / 2, 'text-anchor': 'middle', class: 'chart-empty' }, model.points.length ? 'No known marks in this window' : 'No observed history in this window'));
  }
  container.append(svg);
  const tbody = document.getElementById('chart-values');
  tbody.replaceChildren();
  text('chart-table-caption', `${label} · ${view.window} · actual observed values, local times`);
  for (const point of model.points) {
    const row = element('tr');
    row.append(element('td', date(point.time)), element('td', money(point.value)));
    tbody.append(row);
  }
}
function category(value) {
  return ['Weather', 'Technology', 'Politics', 'Economics', 'Crypto', 'Other'].find(c => c.toLowerCase() === String(value || '').toLowerCase()) || 'Other';
}
function positionStatus(position) {
  const status = String(position.status || '').toLowerCase();
  const result = String(position.result || '').toLowerCase();
  if (status === 'open') return 'open';
  if (['won', 'lost'].includes(status)) return status;
  if (status === 'settled' && ['yes', 'no'].includes(result) && ['yes', 'no'].includes(String(position.side).toLowerCase())) return result === String(position.side).toLowerCase() ? 'won' : 'lost';
  return 'unresolved';
}
function filterPositions(positions, filters) {
  const search = String(filters.search || '').toLowerCase().trim();
  const selected = positions.filter(p => (!filters.status || filters.status === 'all' || positionStatus(p) === filters.status)
    && (!filters.category || filters.category === 'all' || category(p.category) === filters.category)
    && (!search || `${p.title || ''} ${p.ticker || ''}`.toLowerCase().includes(search)));
  return selected.sort((a, b) => {
    if (filters.sort === 'cost') return (number(b.cost) ?? -Infinity) - (number(a.cost) ?? -Infinity);
    const time = p => Number.isFinite(Date.parse(p.opened_at)) ? Date.parse(p.opened_at) : 0;
    return filters.sort === 'oldest' ? time(a) - time(b) : time(b) - time(a);
  });
}
function safeURL(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch { return null; }
}
function percent(value) { const n = number(value); return n === null ? 'Unavailable' : `${(n * 100).toFixed(1)}%`; }
function positionModel(p) {
  const status = positionStatus(p);
  const settled = ['won', 'lost'].includes(status) || String(p.status).toLowerCase() === 'settled';
  return {
    status, settled, title: String(p.title || 'Event title unavailable'),
    category: category(p.category),
    outcomeLabel: settled ? 'Actual settled outcome' : status === 'open' ? 'Projected outcomes · not realized' : 'Unresolved · no confirmed result',
    outcomes: settled ? [['Actual payout', money(p.payout)], ['Actual net P&L', money(p.realized_pnl)]] : status === 'open' ? [['Projected net if win', money(p.estimated_net_win)], ['Projected net if loss', money(p.estimated_net_loss)]] : [['Actual payout', 'Unavailable'], ['Actual net P&L', 'Unavailable']]
  };
}
function definitionList(rows) {
  const dl = element('dl', undefined, 'detail-grid');
  for (const [label, value] of rows) {
    const pair = element('div');
    pair.append(element('dt', label), element('dd', value));
    dl.append(pair);
  }
  return dl;
}
function renderPositions(data) {
  const positions = filterPositions(data.positions, view);
  text('visible-count', `Showing ${positions.length} of ${data.positions.length} positions`);
  const list = document.getElementById('positions-list');
  list.replaceChildren();
  if (!positions.length) list.append(element('p', data.positions.length ? 'No positions match these filters. Try All or a different search.' : 'No paper positions recorded yet. The ledger will show every entry when available.', 'empty-state'));
  for (const p of positions) {
    const model = positionModel(p);
    const card = element('details', undefined, 'position');
    card.dataset.ticker = String(p.ticker || '');
    const summary = element('summary', undefined, 'position-summary');
    const title = element('span', undefined, 'position-heading');
    title.append(element('span', `${model.category} · ${String(p.ticker || 'Ticker unavailable')}`, 'ticker'), element('span', model.title, 'position-title'), element('span', `${String(p.side || 'Side unavailable').toUpperCase()} · ${count(p.qty)} contracts · opened ${date(p.opened_at)}`, 'position-meta'));
    const amount = element('span', undefined, 'position-amount');
    amount.append(element('span', model.settled ? money(p.realized_pnl) : money(p.cost)), element('span', model.settled ? 'actual net P&L' : 'entry cost', 'small muted'));
    summary.append(title, element('span', {open:'Open',won:'Won',lost:'Lost',unresolved:'Unresolved'}[model.status], `position-status status-${model.status}`), amount, element('span', '+', 'expand-icon'));
    card.append(summary);
    const body = element('div', undefined, 'position-body');
    body.append(element('h3', model.title), definitionList([
      ['Side / contracts', `${String(p.side || 'Unavailable').toUpperCase()} / ${count(p.qty)}`],
      ['Entry price / contract', money(p.entry_price)], ['Entry fee', money(p.fee)], ['Total entry cost', money(p.cost)],
      ['Opened', date(p.opened_at)], ['Settled', p.settled_at ? date(p.settled_at) : 'Not settled'],
      ['Recorded status / result', `${String(p.status || 'Unavailable')} / ${String(p.result || 'Not resolved')}`],
      ['Open mark P&L', model.settled ? 'Not applicable · settled' : money(p.unrealized_pnl)]
    ]));
    const outcomes = element('div', undefined, 'outcome-box');
    outcomes.append(element('h3', model.outcomeLabel), definitionList(model.outcomes));
    if (!model.settled && model.status === 'open') outcomes.append(element('p', 'Hypothetical net outcomes after recorded entry cost and fees; not a forecast, live mark, or guaranteed payout.', 'small muted'));
    body.append(outcomes);
    const method = element('div', undefined, 'method');
    method.append(element('h3', 'Method & decision'), element('p', String(p.strategy || 'Method unavailable'), 'method-name'), element('p', String(p.decision_reason || 'No decision explanation was recorded.')),
      definitionList([['Model P(YES) at entry', percent(p.p_yes)], ['Baseline P(YES) at entry', percent(p.baseline_p_yes)]]), element('p', 'Model probabilities are experimental, not validated trading edge.', 'small muted'));
    body.append(method);
    const sources = element('div', undefined, 'sources');
    sources.append(element('h3', 'Evidence & sources'));
    const ul = element('ul');
    for (const source of p.evidence || []) {
      const li = element('li');
      const href = safeURL(source.url);
      if (href) {
        const link = element('a', String(source.label || 'Source'));
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.referrerPolicy = 'no-referrer';
        link.setAttribute('aria-label', `${String(source.label || 'Source')} (opens in a new tab)`);
        li.append(link);
      } else li.textContent = `${String(source.label || 'Source')} (link unavailable)`;
      ul.append(li);
    }
    if (!ul.childElementCount) ul.append(element('li', 'No source evidence was recorded.'));
    sources.append(ul);
    body.append(sources);
    card.append(body);
    list.append(card);
  }
}
function csvCell(value) {
  let cell = value === null || value === undefined ? '' : String(value);
  // Quoting alone does not stop spreadsheet formulas, including whitespace-prefixed ones.
  if (/^[\s\u0000-\u001f]*[=+@-]/u.test(cell) || /^[\t\r\n]/.test(cell)) cell = `'${cell}`;
  return `"${cell.replace(/"/g, '""')}"`;
}
function positionsCSV(positions) {
  const columns = ['ticker', 'title', 'category', 'strategy', 'status', 'side', 'qty', 'opened_at', 'settled_at', 'entry_price', 'fee', 'cost', 'result', 'payout', 'realized_pnl', 'unrealized_pnl', 'estimated_net_win', 'estimated_net_loss', 'decision_reason'];
  return [columns.map(csvCell).join(','), ...positions.map(p => columns.map(key => csvCell(p[key])).join(','))].join('\r\n');
}
function exportCSV() {
  if (!report) return;
  const positions = filterPositions(report.positions, view);
  const blob = new Blob(['\uFEFF', positionsCSV(positions)], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = element('a');
  link.href = url;
  link.download = 'kalshi-lab-paper-view.csv';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function strategyModel(strategy) {
  const score = value => number(value) === null ? 'Unavailable' : number(value).toFixed(4);
  return { id: String(strategy.id || 'Unnamed method'), category: category(strategy.category), state: String(strategy.state || 'State unavailable'), settled: count(strategy.settled_events), brier: score(strategy.brier), baseline: score(strategy.baseline_brier), pnl: money(strategy.realized_pnl), note: String(strategy.note || 'No research note recorded.') };
}
function recordedValue(value) {
  if (value === null || value === undefined) return 'Unavailable';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function recordedRows(record) {
  return Object.entries(record).map(([key, value]) => [key.replace(/_/g, ' ').replace(/^./, char => char.toUpperCase()), recordedValue(value)]);
}
function renderResearch(data) {
  const list = document.getElementById('strategies-list');
  list.replaceChildren();
  if (!data.strategies.length) list.append(element('p', 'No strategy performance has been recorded. No validation is implied.', 'empty-state'));
  for (const strategy of data.strategies) {
    const model = strategyModel(strategy);
    const card = element('article', undefined, 'strategy-card');
    const heading = element('div', undefined, 'section-heading');
    heading.append(element('h3', model.id), element('span', `Recorded state: ${model.state}`, 'strategy-state'));
    card.append(heading, element('p', model.category, 'small muted'), definitionList([['Settled events', model.settled], ['Realized net P&L', model.pnl], ['Model Brier score', model.brier], ['Baseline Brier score', model.baseline]]), element('p', model.note, 'strategy-note'));
    list.append(card);
  }
  const coverage = document.getElementById('coverage-list');
  coverage.replaceChildren();
  if (!data.coverage?.length) coverage.append(element('p', 'No coverage breakdown was recorded in this snapshot.', 'small muted'));
  for (const record of data.coverage || []) {
    const card = element('article', undefined, 'coverage-card');
    if (record && typeof record === 'object') card.append(definitionList(recordedRows(record)));
    else card.append(element('p', recordedValue(record)));
    coverage.append(card);
  }
  const counts = document.getElementById('snapshot-counts');
  counts.replaceChildren();
  const rows = recordedRows(data.counts || {});
  counts.append(rows.length ? definitionList(rows) : element('p', 'No diagnostic counts recorded.', 'small muted'));
}
function render(data) {
  document.getElementById('dashboard').hidden = false;
  renderOverview(data);
  renderChart(data);
  renderPositions(data);
  renderResearch(data);
  text('as-of', `Snapshot as of ${date(data.generated_at)}`);
  text('observed-at', `Market observations ${date(data.observed_at)}`);
  const warnings = document.getElementById('warnings');
  warnings.replaceChildren();
  for (const message of data.warnings || []) warnings.append(element('p', String(message), 'notice'));
  const dashboard = document.getElementById('dashboard');
  dashboard.hidden = false;
  dashboard.setAttribute('aria-busy', 'false');
}
function freshness(data) {
  const generated = Date.parse(data.generated_at);
  const observed = Date.parse(data.observed_at);
  const stale = !Number.isFinite(generated) || !Number.isFinite(observed) || Date.now() - Math.min(generated, observed) > STALE_AFTER_MS;
  const status = document.getElementById('load-status');
  status.parentElement.dataset.state = stale ? 'stale' : 'ready';
  status.textContent = stale ? 'Stale snapshot · market marks may be out of date. This is not a live feed.' : 'Snapshot loaded · paper only · not a live feed';
}
async function load() {
  const retry = document.getElementById('retry');
  retry.hidden = true;
  text('load-status', 'Loading paper ledger…');
  try {
    const response = await fetch('./dashboard-data.json', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Snapshot request failed (${response.status}).`);
    report = validateReport(await response.json());
    render(report);
    freshness(report);
  } catch (error) {
    const prefix = report ? 'Refresh failed; the previous snapshot is still shown. ' : 'Unable to load the paper ledger. ';
    text('load-status', `${prefix}${error.message} No sample data is shown.`);
    document.getElementById('load-status').parentElement.dataset.state = 'error';
    retry.hidden = false;
  }
}
if (typeof document !== 'undefined') {
  document.getElementById('retry').addEventListener('click', load);
  document.getElementById('export-csv').addEventListener('click', exportCSV);
  for (const kind of ['window', 'metric', 'status']) {
    for (const button of document.querySelectorAll(`[data-${kind}]`)) button.addEventListener('click', () => {
      view[kind] = button.dataset[kind];
      for (const sibling of document.querySelectorAll(`[data-${kind}]`)) sibling.setAttribute('aria-pressed', String(sibling === button));
      if (report) kind === 'status' ? renderPositions(report) : renderChart(report);
    });
  }
  for (const [id, key, event] of [['position-search', 'search', 'input'], ['category-filter', 'category', 'change'], ['position-sort', 'sort', 'change']]) {
    document.getElementById(id).addEventListener(event, e => { view[key] = e.target.value; if (report) renderPositions(report); });
  }
  let resizeFrame;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => { if (report) renderChart(report); });
  });
  load();
  setInterval(() => { if (report && document.getElementById('retry').hidden) freshness(report); }, 60000);
}
if (typeof module !== 'undefined') module.exports = { number, validateReport, money, overview, chartModel, chartGeometry, filterPositions, positionStatus, positionModel, safeURL, strategyModel, csvCell, positionsCSV };
