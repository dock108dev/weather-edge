import { paperCapitalChart, stormTrackChart, venuePriceTimeline } from './charts.js';
import {
  q, count, figure, esc, name, label, paths, value, read, external, metadata, raw, block, message, timelineLink, metrics,
} from './evidence_format.js';

function attachPaper(record, selectedIndex) {
  const panel = q('#study-paper');
  if (!panel) return;
  const items = Object.entries(record.records || {}).filter(([, item]) => Array.isArray(item?.cash_over_time));
  const selected = record.cases?.[selectedIndex];
  const selectedKey = selected && [selected.window, selected.strategy, selected.freshness].join('/');
  const initial = items.find(([key]) => key === selectedKey)
    || items.find(([key, item]) => key.startsWith('forward/') && item.positions?.length) || items[0];
  if (!initial) return;
  panel.innerHTML = `<h3>Capital committed over time</h3><label for="study-paper-rule">Paper strategy and window</label>
    <select id="study-paper-rule">${items.map(([key]) => `<option value="${esc(key)}"${key === initial[0] ? ' selected' : ''}>
      ${esc(key.split('/').map(label).join(' · '))}</option>`).join('')}</select>
    <canvas class="chart" id="study-paper-chart" role="img" aria-label="Cumulative committed paper cash at actual fill and settlement clocks"></canvas>
    <div class="chart-key">Paper cash committed · time in UTC</div><div id="study-paper-summary"></div>`;
  const selector = q('#study-paper-rule'), canvas = q('#study-paper-chart'), body = q('#study-paper-summary');
  const usd = n => Number.isFinite(n) ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n) : null;
  function draw() {
    const item = record.records[selector.value], totals = item.metrics || {};
    const [stage] = selector.value.split('/');
    const clocks = record.date_window || {};
    const bounds = (stage === 'forward' ? [clocks.split, clocks.through] : [clocks.from, clocks.split]).map(Date.parse);
    const sameStage = items.filter(([key]) => key.startsWith(stage + '/')).map(([, value]) => value.metrics?.peak_committed || 0);
    if (bounds.every(Number.isFinite) && bounds[1] > bounds[0]) {
      canvas.hidden = false;
      paperCapitalChart(canvas, item.cash_over_time, bounds, Math.max(25, ...sameStage));
    } else canvas.hidden = true;
    body.innerHTML = metadata([['Cash at end', usd(totals.cash_at_end)], ['Committed at end', usd(totals.committed_at_end)],
      ['Peak committed', usd(totals.peak_committed)], ['Forecast EV', usd(totals.forecast_ev_dollars)],
      ['Realized paper P&L', usd(totals.realized_pnl)], ['Maximum drawdown at cost', usd(totals.max_drawdown_at_cost)],
      ['Settled station-days', totals.settled_station_days]])
      + (item.station_days?.length ? '<h4>Exposure by station and day</h4>' + metrics(item.station_days.map(row => ({
        station: row.station, day: row.date, fills: row.fills, settled: row.settled,
        cash_committed: usd(row.cash_committed), realized_pnl: usd(row.realized_pnl),
      }))) : '');
  }
  selector.addEventListener('change', draw);
  const observer = new ResizeObserver(() => {
    if (!panel.isConnected) observer.disconnect();
    else if (!canvas.hidden && canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) draw();
  });
  observer.observe(canvas);
  draw();
}

function dates(window) {
  if (!window || typeof window !== 'object') return '';
  return Object.entries(window).filter(([, entry]) => entry != null)
    .map(([key, entry]) => `${label(key)} ${value(entry)}`).join(' · ');
}

function attachModelMetrics(record) {
  const panel = q('#study-model-metrics');
  if (!panel) return;
  const column = ['group', 'window'].find(key => record.metrics.every(row => typeof row[key] === 'string')
    && new Set(record.metrics.map(row => row[key])).size > 1);
  if (!column) { panel.innerHTML = metrics(record.metrics); return; }
  const groups = [...new Set(record.metrics.map(row => row[column]))];
  const preferred = groups.find(group => /^all$/i.test(group)) || groups[0];
  panel.innerHTML = `<label class="filter-label">Evaluation group <select id="study-metric-group">${groups.map(group =>
    `<option${group === preferred ? ' selected' : ''}>${esc(group)}</option>`).join('')}</select></label>
    <div id="study-metric-table"></div>`;
  const select = q('#study-metric-group'), table = q('#study-metric-table');
  function draw() { table.innerHTML = metrics(record.metrics.filter(row => row[column] === select.value)); }
  select.addEventListener('change', draw);
  draw();
}

function pairedDifferences(record) {
  const source = record.uncertainty || record.contrasts || record.comparisons;
  const metricNames = { brier: 'Bucket Brier', mae: 'Mean absolute error' };
  if (!Array.isArray(source) || !source.length || !source.every(row =>
    Object.hasOwn(metricNames, row?.metric) && typeof row.delta === 'number' &&
    row.date_bootstrap_interval_95?.length === 2 && row.date_bootstrap_interval_95.every(Number.isFinite))) {
    return raw('Paired differences and uncertainty', source);
  }
  const names = new Map(record.metrics?.filter(row => row.method && row.label).map(row => [row.method, row.label]));
  const format = n => (n > 0 ? '+' : '') + n.toFixed(5);
  const rows = source.map(row => ({
    ...(row.group ? { group: row.group } : {}),
    metric: metricNames[row.metric],
    baseline: names.get(row.baseline) || label(row.baseline),
    candidate: names.get(row.method) || label(row.method),
    candidate_minus_baseline: format(row.delta),
    '95% interval': row.date_bootstrap_interval_95.map(format).join(' to '),
    dates: row.calendar_dates,
  }));
  return `<details id="study-paired-differences"><summary>Paired differences and uncertainty</summary>
    <p>Negative Brier or MAE differences favor the candidate. Intervals resample whole calendar dates.</p>
    ${metrics(rows)}</details>`;
}

const paperHighlights = [
  'window', 'strategy', 'freshness', 'signals', 'rechecked', 'missing_rechecks', 'fills', 'settled_fills',
  'settled_station_days', 'forecast_ev_dollars', 'realized_pnl', 'peak_committed', 'max_drawdown_at_cost',
];

function decisionEvidence(row, reactionAt) {
  const signal = row.decision;
  const quote = signal?.reaction_quote;
  const terms = row.bucket || row.market;
  const position = row.position;
  const [initialBook, reactionBook] = row.books || [];
  const origin = book => book && (book.http_cache?.['cf-cache-status'] || book.http_headers?.['cf-cache-status']);
  const age = book => book && (book.cache_age_seconds == null ? 'Unknown' : `${book.cache_age_seconds}s`);
  return metadata([
    ['Market bucket', terms?.label], ['Side', row.side], ['Result', signal?.result],
    ['Forecast model', signal?.model_method], ['Forecast known UTC', signal?.forecast_available_at],
    ['Signal probability', signal?.probability], ['Signal price per contract', signal?.entry_price],
    ['Signal book source UTC', initialBook?.source_at], ['Signal HTTP cache', origin(initialBook)],
    ['Signal HTTP age', age(initialBook)],
    ['Recheck due UTC', signal?.reaction_due_at], ['Eligible recheck UTC', reactionAt],
    ['Recheck book source UTC', reactionBook?.source_at], ['Recheck HTTP cache', origin(reactionBook)],
    ['Recheck HTTP age', age(reactionBook)],
    ['Recheck price per contract', quote?.entry_price], ['Recheck fee', quote?.fee],
    ['All-in cost', position?.max_loss], ['Quantity', position?.quantity],
    ['Break-even probability', quote?.break_even_probability], ['Forecast EV dollars', quote?.ev_dollars],
    ['Fee coefficient', quote?.coefficient], ['Cost before fee', quote?.notional], ['Payout', position?.payout],
  ]) + (row.books?.length ? raw('Signal and recheck order books', row.books) : '')
    + raw('Decision, fees, and fill allocation', signal)
    + raw('As-issued forecast', row.forecast)
    + raw('Official climate report', row.official_report)
    + raw('Contract terms', terms)
    + raw('Case evidence', row.evidence || row.data);
}

function caseTimeline(row) {
  if (!row.decision_timeline?.length) return '';
  const p = item => typeof item === 'number' ? item * 100 : null;
  return metrics(row.decision_timeline.map(point => ({
    decision_utc: point.at,
    observed_low_f: point.observed_min_f,
    noaa_yes_percent: p(point.noaa_yes),
    model_yes_percent: p(point.selected_yes),
    display_yes_percent: p(point.market_midpoint),
    forecast_known_utc: point.forecast?.available_at,
  })));
}

function recordedPriceTimeline(timeline) {
  if (!timeline?.series?.length) return '';
  const markers = timeline.markers.map(row => ({ evidence: row.label, utc: row.at }));
  return `<div class="recorded-price-timeline"><canvas class="chart" data-price-timeline role="img"
      aria-label="Archived display prices and native trades around the report editions and market close clocks"></canvas>
    <div class="chart-key" data-price-key></div>${metrics(markers)}</div>`;
}

function attachPriceTimelines(record) {
  const colors = ['#24776a', '#346aa2', '#a66b36'];
  (record.cases || []).forEach((row, index) => {
    const detail = q('#case-' + index), canvas = detail?.querySelector('[data-price-timeline]');
    if (!canvas) return;
    const series = row.venue_price_timeline.series.map((lane, i) => ({ ...lane, color: colors[i % colors.length] }));
    detail.querySelector('[data-price-key]').innerHTML = series.map(lane =>
      `<span><i class="legend-chip" style="--chip:${lane.color}"></i>${esc(lane.label)}</span>`).join('') + ' · UTC axis';
    function draw() {
      if (detail.open) venuePriceTimeline(canvas, series, row.venue_price_timeline.markers);
    }
    detail.addEventListener('toggle', draw);
    const observer = new ResizeObserver(() => {
      if (!canvas.isConnected) observer.disconnect();
      else if (canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) draw();
    });
    observer.observe(canvas);
    draw();
  });
}

function stormTrack(track) {
  if (!track?.positions?.length || !track.boundary_rings?.length) return '';
  const crossing = Math.max(0, track.positions.findIndex(row => row.at === track.crossing_observed_inside_at));
  return `<figure class="storm-track"><figcaption class="chart-head"><h3>NHC advisory track</h3>
    <label><input type="checkbox" data-storm-full> Full track</label></figcaption>
    <canvas class="chart storm-track-map" data-storm-map role="img"
      aria-label="Original NHC storm centers and the contract region boundary through the selected advisory"></canvas>
    <div class="chart-key">
      <span><i class="legend-chip" style="--chip:#24776a"></i>Both endpoints at least ${esc(track.threshold_mph)} mph</span>
      <span><i class="legend-chip" style="--chip:#a66b36"></i>Wind below threshold at an endpoint</span>
      <span><i class="legend-chip" style="--chip:#993f58"></i>Highlighted boundary crossing</span>
    </div>
    <input type="range" data-storm-at aria-label="NHC advisory time" min="0" max="${track.positions.length - 1}" value="${crossing}">
    <div data-storm-reading aria-live="polite"></div></figure>`;
}

function attachStormTracks(record) {
  (record.cases || []).forEach((row, index) => {
    const detail = q('#case-' + index), canvas = detail?.querySelector('[data-storm-map]');
    if (!canvas) return;
    const slider = detail.querySelector('[data-storm-at]'), full = detail.querySelector('[data-storm-full]');
    const reading = detail.querySelector('[data-storm-reading]');
    const track = row.storm_track;
    function draw() {
      if (!detail.open) return;
      const position = Number(slider.value);
      const current = track.positions[position], prior = track.positions[position - 1];
      stormTrackChart(canvas, track, position, full.checked);
      reading.innerHTML = metadata([
        ['Advisory UTC', current.at], ['Sustained wind', `${current.sustained_mph} mph`],
        ['Center', `${current.latitude}°, ${current.longitude}°`],
        ['Previous advisory UTC', prior?.at],
        ['Previous sustained wind', prior ? `${prior.sustained_mph} mph` : null],
      ]);
      if (current.at === track.crossing_observed_inside_at && prior?.at === track.crossing_after_at) {
        reading.insertAdjacentHTML('beforeend', '<p>This segment intersects the retained boundary. The contract requires both endpoint winds to meet the threshold.</p>');
      }
    }
    slider.addEventListener('input', draw);
    full.addEventListener('change', draw);
    detail.addEventListener('toggle', draw);
    const observer = new ResizeObserver(() => {
      if (!canvas.isConnected) observer.disconnect();
      else if (canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) draw();
    });
    observer.observe(canvas);
    draw();
  });
}

function calibrationRows(record) {
  const input = record.calibration || record.metrics || [];
  const rows = Array.isArray(input) ? input : Object.values(input).filter(Array.isArray).flat();
  return rows.map(row => ({
    label: [row.venue && name(row.venue), row.method, row.side].filter(Boolean).join(' · '),
    band: row.group,
    forecast: row.mean_probability ?? row.mean_forecast ?? row.mean_confidence,
    observed: row.realized_rate ?? row.observed_rate ?? row.observed_hit_rate ?? row.observed_frequency,
    dates: row.calendar_dates ?? row.distinct_dates,
    stationDays: row.station_days ?? row.distinct_station_days,
    rows: row.bucket_calls ?? row.bucket_cases ?? row.rows ?? row.cases,
    misses: row.miss_rows,
    missDates: row.miss_dates,
  })).filter(row => Number.isFinite(row.forecast) && Number.isFinite(row.observed));
}

function drawCalibration(canvas, rows) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);
  const inset = { left: 54, top: 19, right: 18, bottom: 37 };
  const x = p => inset.left + p * (width - inset.left - inset.right);
  const y = p => height - inset.bottom - p * (height - inset.top - inset.bottom);
  ctx.font = '11px IBM Plex Mono, monospace';
  ctx.fillStyle = '#50655b';
  ctx.strokeStyle = '#e1e7dc';
  for (const p of [0, .25, .5, .75, 1]) {
    ctx.beginPath();
    ctx.moveTo(x(0), y(p));
    ctx.lineTo(x(1), y(p));
    ctx.moveTo(x(p), y(0));
    ctx.lineTo(x(p), y(1));
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText((p * 100) + '%', inset.left - 7, y(p) + 3);
    ctx.textAlign = 'center';
    ctx.fillText((p * 100) + '%', x(p), y(0) + 17);
  }
  ctx.fillText('Mean forecast probability', (x(0) + x(1)) / 2, height - 5);
  ctx.textAlign = 'left';
  ctx.fillText('Observed frequency', inset.left, 12);
  ctx.strokeStyle = '#7a9087';
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(x(0), y(0));
  ctx.lineTo(x(1), y(1));
  ctx.stroke();
  ctx.setLineDash([]);
  for (const row of rows) {
    ctx.fillStyle = '#247c68b5';
    ctx.strokeStyle = '#196454';
    ctx.beginPath();
    ctx.arc(x(row.forecast), y(row.observed), Math.min(8, 3 + Math.sqrt(row.dates || 0)), 0, 2 * Math.PI);
    ctx.fill();
    ctx.stroke();
  }
}

function attachCalibration(record) {
  const rows = calibrationRows(record);
  const panel = q('#study-calibration');
  if (!rows.length || !panel) return;
  const labels = [...new Set(rows.map(row => row.label))];
  panel.innerHTML = `<h3>Probability calibration</h3>
    <label class="filter-label">Forecast <select id="calibration-method">${labels.map(method =>
      `<option>${esc(method)}</option>`).join('')}</select></label>
    <canvas id="calibration-chart" role="img" aria-label="Observed frequency against predicted probability. The table shows the plotted values and sample sizes."></canvas>
    <div id="calibration-table"></div>`;
  const select = q('#calibration-method');
  const canvas = q('#calibration-chart');
  function redraw() {
    if (!panel.isConnected) return;
    const subset = rows.filter(row => row.label === select.value).sort((a, b) => a.forecast - b.forecast);
    const pct = p => figure.format(p * 100) + '%';
    drawCalibration(canvas, subset);
    q('#calibration-table').innerHTML = metrics(subset.map(row => ({
      ...(row.band ? { band: row.band } : {}), forecast: pct(row.forecast), observed: pct(row.observed), rows: row.rows,
      calendar_dates: row.dates, station_days: row.stationDays,
      ...(row.misses != null ? { misses: row.misses, dates_with_misses: row.missDates } : {}),
    })));
  }
  select.addEventListener('change', redraw);
  redraw();
  const observer = new ResizeObserver(() => {
    if (!panel.isConnected) observer.disconnect();
    else if (canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) redraw();
  });
  observer.observe(canvas);
}

function attachStationCalibration(record) {
  const host = q('#study-station-calibration');
  if (!host) return;
  const rows = record.station_and_hours;
  const method = row => `${name(row.venue)} · ${row.method}`;
  const methods = [...new Set(rows.map(method))], stations = [...new Set(rows.map(row => row.station))].sort();
  const options = items => items.map(item => `<option>${esc(item)}</option>`).join('');
  host.innerHTML = `<h3>Confidence by station and decision hour</h3><div class="research-filters">
    <label class="filter-label">Forecast <select id="station-calibration-model">${options(methods)}</select></label>
    <label class="filter-label">Station <select id="station-calibration-place"><option value="">All stations</option>${options(stations)}</select></label>
    </div><div id="station-calibration-table"></div>`;
  const selector = q('#station-calibration-model'), place = q('#station-calibration-place');
  function draw() {
    const selected = rows.filter(row => method(row) === selector.value && (!place.value || row.station === place.value));
    q('#station-calibration-table').innerHTML = metrics(selected.map(row => ({ station: row.station,
      hour_lst: row.local_standard_hour, calendar_dates: row.distinct_dates,
      station_days: row.distinct_station_days, total_calls: row.rows,
      'at_99%_or_higher': row.rows_99_plus, 'misses_at_99%_or_higher': row.misses_99_plus, brier: row.bucket_brier,
    }))) || message('No matched calibration data for this station and forecast.');
  }
  selector.addEventListener('change', draw);
  place.addEventListener('change', draw);
  draw();
}

function studyCase(row, index, selected, record) {
  const indexAt = row.target_at && /^KXTEMP/.test(row.instrument_id || '') ? row.at : null;
  const market = row.venue && (row.instrument_id || row.bucket_id) ? `<a href="${esc(paths.market(row.venue, row.instrument_id || row.bucket_id, indexAt))}">Open exact contract</a>` : '';
  const live = row.mode === 'live' || (record.kind === 'strategies' && row.venue === 'polymarket-us' && row.decision);
  const clock = row.at || row.signal_at;
  const reactionAt = row.reaction_at || row.decision?.reaction_at;
  const instrument = row.instrument_id || row.bucket_id;
  const globeDate = row.globe_date || row.date;
  const targetDay = globeDate === row.date;
  const context = {
    ...(live ? { mode: 'live' } : {}),
    ...(targetDay && live && row.venue && instrument ? { venue: row.venue, instrument } : {}),
    ...(live && record.kind === 'strategies' && row.decision ? { paper_study: record.id, paper_case: index } : {}),
    ...(targetDay && row.venue === 'polymarket-us' && (String(instrument).includes('high-')
      || (live && String(instrument).includes('low-'))) ? { bucket: instrument } : {}),
  };
  if (targetDay && !live && row.venue && /^(tc-temp-[a-z]+(?:high|low)-|rain(?:c|cli)-|KX(?:HIGH|LOW|RAIN-))/i.test(instrument || '')) {
    Object.assign(context, { venue: row.venue, instrument, bucket: instrument });
  }
  const timeline = timelineLink(row.globe_station || row.station, globeDate, {
    ...context, at: clock, probe_lat: row.probe_lat, probe_lon: row.probe_lon, layers: row.layers, wave_frame: row.wave_frame, probe_buoy: row.probe_buoy,
  }, live && record.kind === 'strategies' ? 'Signal on globe' : targetDay ? 'Weather timeline' : 'Weather at the decision');
  const waveLeads = (row.wave_leads || []).map(item => timelineLink(row.globe_station, globeDate, {
    at: clock, probe_lat: row.probe_lat, probe_lon: row.probe_lon, layers: row.layers, wave_frame: item.frame, probe_buoy: row.probe_buoy,
  }, item.label)).join('');
  const hrrrIssues = (row.hrrr_issues || []).map(issue => timelineLink(row.station, globeDate, {
    ...context, at: clock, hrrr_issue: issue,
  }, `${issue.slice(11, 13)}Z HRRR forecast on globe`)).join('');
  const hrrrProbes = (row.hrrr_probes || []).map(item => timelineLink(row.station, globeDate, {
    ...context, at: clock, layers: 'weather', probe_lat: item.lat, probe_lon: item.lon,
    hrrr_issue: item.issued_at, hrrr_point: 1,
  }, item.label)).join('');
  const due = row.decision?.reaction_due_at;
  const recheck = reactionAt || due ? timelineLink(row.station, row.date, { ...context, at: reactionAt || due },
    reactionAt ? 'Recheck on globe' : 'Expected recheck time') : '';
  const compareVenue = row.related_venue || ((record.venues || []).includes('polymarket-us') &&
    (record.venues || []).includes('kalshi') ? row.venue === 'kalshi' ? 'polymarket-us' : 'kalshi' : null);
  const compared = row.related_instrument_id && instrument && compareVenue
    ? `<a href="/?${esc(new URLSearchParams({ view: 'markets', venue: row.venue, instrument,
        compare_venue: compareVenue, compare_instrument: row.related_instrument_id }))}">Compare native terms</a>` : '';
  const text = row.title || [row.station, row.date, row.instrument_id].filter(Boolean).join(' · ') || `Case ${index + 1}`;
  return `<details class="study-case" id="case-${index}"${selected ? ' open' : ''}><summary>${esc(text)}</summary>
    ${metadata([['Station', row.station], ['Day', row.date], ['Venue', row.venue && name(row.venue)],
      ['Strategy', row.strategy && label(row.strategy)], ['Test window', row.window && label(row.window)],
      ['Decision UTC', clock]])}
    ${typeof row.detail === 'string' ? `<p>${esc(row.detail)}</p>` : raw('Decision evidence', row.detail)}
    ${stormTrack(row.storm_track)}
    ${recordedPriceTimeline(row.venue_price_timeline)}
    ${decisionEvidence(row, reactionAt)}
    ${row.members && row.config_version ? metadata([
      ['Forecast issue UTC', row.issued_at], ['All nine forecasts available by UTC', row.available_at],
      ['Native target minute UTC', row.target_at], ['Venue configuration', row.config_version],
    ]) + `<details><summary>Central Park and member airports</summary>${metrics(Object.entries(row.members).map(([station, member]) => ({
      station, forecast_f: member.temp_f, published_utc: member.available_at,
    })))}</details>` : ''}
    ${caseTimeline(row)}
    <div class="record-links">${market}${compared}${timeline}${waveLeads}${hrrrIssues}${hrrrProbes}${recheck}<a href="${esc(paths.research(record.kind, record.id, index))}#case-${index}">Link to this case</a></div></details>`;
}

function showRecord(record, selected) {
  const uri = '/api/research/' + encodeURIComponent(record.kind) + '/' + encodeURIComponent(record.id);
  const hasPaperSummary = record.kind === 'strategies' && record.metrics?.some(row => row.strategy != null);
  const summary = hasPaperSummary
    ? record.metrics.map(row => Object.fromEntries(paperHighlights.filter(key => key in row).map(key => [key, row[key]])))
    : record.metrics;
  return `<header><h2>${esc(record.title || record.id)}</h2>
    <p class="evidence-byline">${esc([record.target && label(record.target), ...(record.venues || []).map(name), record.created_at].filter(Boolean).join(' · '))}</p>
    ${record.hypothesis ? `<p>${esc(record.hypothesis)}</p>` : ''}
    ${record.supersedes ? `<p><a href="${esc(paths.research(record.kind, record.supersedes))}">Open the earlier result</a>.</p>` : ''}
    <div class="artifact-links"><a href="${esc(uri)}?download=1" download>Download study JSON</a>
      ${record.evidence_download ? `<a href="${esc(uri)}/evidence.gz" download>Download frozen evidence</a>` : ''}</div></header>`
    + block('Evaluation', [record.summary, dates(record.date_window)].filter(Boolean).map(item => `<p>${esc(value(item))}</p>`).join(''))
    + (record.kind === 'models' && Array.isArray(record.metrics) && record.metrics.length > 10
      ? '<section class="evidence-block" id="study-model-metrics"></section>' : metrics(summary))
    + (Object.values(record.records || {}).some(item => Array.isArray(item?.cash_over_time)) ? '<section class="evidence-block" id="study-paper"></section>' : '')
    + (hasPaperSummary ? block('', `<details><summary>Full paper metrics</summary>${metrics(record.metrics)}</details>`) : '')
    + block('', raw('Eligible evidence', record.coverage || record.evidence)
      + raw('Selected settings before the test', record.selection)
      + pairedDifferences(record))
    + (calibrationRows(record).length ? '<section class="evidence-block" id="study-calibration"></section>' : '')
    + (record.station_and_hours?.length ? '<section class="evidence-block" id="study-station-calibration"></section>' : '')
    + block('Cases', record.cases?.map((row, index) => studyCase(row, index, index === selected, record)).join(''))
    + block('Evidence limits', record.limits?.length ? `<ul>${record.limits.map(item => `<li>${esc(value(item))}</li>`).join('')}</ul>` : '')
    + block('', raw('Reproduction and source provenance', record.provenance));
}

export async function initStudies(params) {
  const selector = q('#research-kind');
  const results = q('#research-results');
  const inspector = q('#research-inspector');
  const search = q('#research-search');
  const matches = q('#research-matches');
  let generation = 0;
  let selected = params.get('run');
  let kind = params.get('kind');
  selector.value = ['models', 'strategies'].includes(kind) ? kind : '';
  let records, issues;
  try { ({ records, issues } = await read('/api/research')); }
  catch (error) { results.innerHTML = message(error.message); return; }
  if (issues?.length) {
    const warning = q('#research-issues');
    warning.querySelector('summary').textContent = issues.length === 1
      ? 'One study file could not be read' : `${count.format(issues.length)} study files could not be read`;
    warning.querySelector('ul').innerHTML = issues.map(({ kind, id, reason }) =>
      `<li><strong>${esc(kind)}/${esc(id)}</strong> · ${esc(reason)}</li>`).join('');
    warning.hidden = false;
  }
  const corrected = new Map(records.filter(row => row.supersedes).map(row => [row.supersedes, row]));
  const titles = new Map();
  for (const { title } of records) if (title) titles.set(title, (titles.get(title) || 0) + 1);

  function displayList() {
    const term = search.value.trim().toLowerCase();
    const visible = records.filter(row => (!selector.value || row.kind === selector.value) && (!term
      || [row.id, row.title, row.hypothesis, row.target, ...(row.venues || []).map(name)].join(' ').toLowerCase().includes(term)));
    matches.textContent = `${count.format(visible.length)} ${visible.length === 1 ? 'study' : 'studies'}`;
    results.innerHTML = visible.length ? visible.map(row => `<a class="study-item" data-study-link
      aria-current="${row.kind === kind && row.id === selected}" href="${esc(paths.research(row.kind, row.id))}">
      <strong>${esc(row.title || row.id)}</strong>
      <span>${esc([row.kind === 'models' ? 'Forecast model' : 'Paper strategy', row.target && label(row.target),
      ...(row.venues || []).map(name), corrected.has(row.id) ? 'Newer result available' : ''].filter(Boolean).join(' · '))}</span>
      ${row.created_at ? `<span>Created <time datetime="${esc(row.created_at)}">${esc(row.created_at.slice(0, 19).replace('T', ' '))} UTC</time></span>` : ''}
      ${titles.get(row.title) > 1 ? `<span>Record ${esc(row.id.split('-').at(-1))}</span>` : ''}</a>`).join('')
      : message('No recorded studies match.');
    return visible;
  }
  async function loadRecord(type, id, caseId) {
    const request = ++generation;
    kind = type;
    selected = id;
    inspector.hidden = false;
    const href = paths.research(kind, selected);
    for (const link of results.querySelectorAll('[data-study-link]')) {
      link.setAttribute('aria-current', String(link.getAttribute('href') === href));
    }
    inspector.innerHTML = message('Loading study…');
    try {
      const record = await read('/api/research/' + encodeURIComponent(type) + '/' + encodeURIComponent(id));
      if (request === generation) {
        inspector.innerHTML = showRecord(record, caseId != null ? Number(caseId) : null);
        const correction = corrected.get(record.id);
        if (correction) {
          inspector.querySelector('header').insertAdjacentHTML('beforeend',
            `<p><a href="${esc(paths.research(correction.kind, correction.id))}">Open the newer result</a>.</p>`);
        }
        attachCalibration(record);
        attachStationCalibration(record);
        attachModelMetrics(record);
        attachPaper(record, caseId != null ? Number(caseId) : null);
        attachPriceTimelines(record);
        attachStormTracks(record);
        if (caseId != null) q('#case-' + Number(caseId))?.scrollIntoView({ block: 'center' });
      }
    } catch (error) { if (request === generation) inspector.innerHTML = message(error.message); }
  }
  selector.addEventListener('change', () => {
    const visible = displayList();
    if (visible.some(row => row.kind === kind && row.id === selected)) return;
    const [first] = visible;
    if (first) {
      history.replaceState(null, '', paths.research(first.kind, first.id));
      loadRecord(first.kind, first.id);
    } else {
      ++generation;
      inspector.hidden = true;
    }
  });
  search.addEventListener('input', displayList);
  results.addEventListener('click', event => {
    const anchor = event.target.closest('a[data-study-link]');
    if (!anchor || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    history.replaceState(null, '', anchor.href);
    const query = new URL(anchor.href).searchParams;
    loadRecord(query.get('kind'), query.get('run'));
    if (event.detail && window.matchMedia('(max-width: 760px)').matches) inspector.scrollIntoView({ block: 'start' });
  });
  const list = displayList();
  if (selected && kind) await loadRecord(kind, selected, params.get('case'));
  else if (list.length) await loadRecord(list[0].kind, list[0].id);
}
