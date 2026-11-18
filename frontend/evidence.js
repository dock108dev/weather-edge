import { monthlyRainChart, weatherIndexChart } from './charts.js';
import {
  q, count, figure, esc, name, label, paths, value, read, external, metadata, raw, block, message, timelineLink, metrics,
} from './evidence_format.js';
import { initStudies } from './research_view.js';

function bookSummary(books) {
  if (!books?.length) return '';
  const [latest] = books;
  return metadata([['Book receipt UTC', latest.at], ['Exchange clock UTC', latest.source_at ?? 'Unavailable'],
    ['YES bids', (latest.bids || latest.yes_bids)?.slice(0, 3).map(level => level.join(' × '))],
    ['YES offers', (latest.offers || latest.yes_offers)?.slice(0, 3).map(level => level.join(' × '))],
    ['HTTP age in seconds', latest.cache_age_seconds ?? 'Unavailable'],
    ['Book evidence', latest.depth_kind ?? latest.kind], ['Issue', latest.reason]])
    + raw('Recent native books', books);
}

function comparisonData(market) {
  return market.related?.map(item => `<details class="study-case">
    <summary>${esc(name(item.venue))} · ${esc(item.label || item.instrument_id)} · ${esc(item.comparison.payout_equivalent ? 'Payout-equivalent' : 'Weather and price comparison')}</summary>
    ${metadata(Object.entries(item.comparison.fields).map(([key, verdict]) => [label(key), label(verdict)]))}
    ${item.comparison.basis ? `<p>${esc(item.comparison.basis)}</p>` : ''}
    <a data-market-link href="${esc(paths.market(item.venue, item.instrument_id))}">Open ${esc(item.instrument_id)}</a>
  </details>`).join('') || '';
}

function contractComponents(components) {
  if (!components?.length) return '';
  return `<div class="table-wrap"><table class="market-comparison-table">
    <thead><tr><th scope="col">Native component</th><th scope="col">Required side</th><th scope="col">Component payout</th></tr></thead>
    <tbody>${components.map(row => {
      const contract = row.market;
      const payout = row.side === 'yes' ? contract?.yes_payout : contract?.no_payout;
      const title = contract?.title || row.instrument_id;
      const link = row.identity_confirmed ? `<a data-market-link href="${esc(paths.market(row.venue, row.instrument_id))}">${esc(title)}</a>`
        : `${esc(title)} <small>${esc(row.mismatch_reason || 'Archive identity pending')}
          ${row.archived_component_day ? ' · cited date ' + esc(row.archived_component_day) : ''}</small>`;
      return `<tr><td>${link}</td><td>${esc(row.side.toUpperCase())}</td><td>${payout == null || !row.identity_confirmed ? '—' : '$' + esc(payout)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}

function reportingDaysRain(period) {
  if (!period?.days.length) return '';
  const rows = period.days.map(row => ({ reporting_day: row.date, station: period.station,
    source_status: label(row.provider_status), precipitation: row.precipitation_in === 'T' ? 'Trace'
      : row.precipitation_in == null ? null : `${row.precipitation_in} in`,
    original_provider_issue_utc: row.provider_issued_at, local_receipt_utc: row.available_at }));
  const sources = period.days.filter(row => row.source_url).map(row => external(row.source_url, `Source for ${row.date}`)).join(' · ');
  return metrics(rows) + (sources ? `<p>${sources}</p>` : '');
}

function hddEvidence(projection) {
  if (!projection) return '';
  const { accrual, forecast, threshold } = projection;
  const percent = probability => figure.format(100 * probability) + '%';
  return metadata([
    ['Station', projection.station],
    ['Confirmed total', `${figure.format(accrual.hdd_65)} HDD through ${accrual.through_date}`],
    ['Complete source days', accrual.complete_days],
    ['Latest accrual source receipt UTC', accrual.last_source_available_at],
    ['Forecast week', forecast.dates.join(' to ')],
    ['NOAA forecast total', `${figure.format(forecast.hdd_65)} HDD`],
    ['Historical same-week average', `${figure.format(forecast.analog_same_dates_hdd_65)} HDD`],
    ['YES if below', `${figure.format(threshold.threshold_hdd)} HDD`],
    ['Historical scenario YES', percent(threshold.analog_yes_probability)],
    ['Forecast-adjusted scenario YES', percent(threshold.forecast_yes_probability)],
    ['NOAA issue UTC', forecast.issued_at], ['Source publication UTC', forecast.source_published_at],
    ['Local forecast receipt UTC', forecast.received_at], ['Projection known UTC', projection.known_at],
  ]) + `<p>${external(forecast.source, 'Original NOAA extended bulletin')} ·
    <a href="${esc(paths.research('models', projection.study_id))}">Open forecast test and source rules</a></p>
    <details><summary>Daily forecast for the seven-day window</summary>${metrics(forecast.daily.map(row => ({
      date: row.date, high_f: row.high.temp_f, low_f: row.low.temp_f, forecast_hdd_65: row.hdd_65,
    })))}</details>`;
}

function holidayRainOutlook(outlook) {
  if (!outlook) return '';
  const lower = outlook.known_wet_model_members, upper = outlook.possibly_wet_model_members;
  const members = lower === upper ? String(lower) : `${lower}–${upper}`;
  return metadata([
    ['Station', outlook.station],
    [`Model members reaching ${figure.format(outlook.rain_threshold_in)} inches`, `${members} of ${outlook.complete_members}`],
    ['Forecast window UTC', `${outlook.start} to ${outlook.end}`], ['Source reporting timezone', outlook.reporting_timezone],
    ['GEFS issue UTC', outlook.issue], ['All fields published by UTC', outlook.source_available_at],
    ['All fields received by UTC', outlook.local_received_at],
  ]) + `<p>The contract counts trace as zero. ${lower !== upper ? 'Some six-hour forecasts overlap the reporting boundary. The member range preserves that uncertainty. ' : ''}
    ${external(outlook.docs, 'NOAA GEFS')} ·
    <a href="${esc(paths.research('models', outlook.study_id))}">Open the original ensemble comparison</a></p>`;
}

function contractClocks(market) {
  const clocks = market.clocks;
  if (!clocks) return '';
  const receipt = clocks.native_closed_book_receipt;
  return metadata([
    ['Nominal event end UTC', clocks.nominal_event_end_at],
    ['Listed market end UTC', clocks.listed_market_end_at],
    ['Last recorded trade UTC', clocks.native_last_trade_at],
    ['Observed native close UTC', clocks.native_closed_book_at],
    ['Closed book received UTC', clocks.native_closed_book_received_at],
    ['Closed-book settlement statistic UTC', clocks.native_settlement_stat_at],
    ['Settlement statistic marked preliminary', clocks.native_settlement_preliminary],
  ]) + (clocks.native_close_conflict ? '<p>Retained CLOSED books disagree on the native close time.</p>' : '')
    + (receipt ? raw('Original closed-book evidence', { receipt, observations: clocks.native_closed_book_observations }) : '');
}

function sideBySide(data) {
  const { left, right, comparison } = data;
  const fields = Object.entries(comparison.fields);
  const rows = [...fields.map(([key, verdict]) => [label(key), left[key], right[key], label(verdict)]),
    ...['nominal_event_end_at', 'listed_market_end_at', 'native_closed_book_at', 'native_last_trade_at']
      .filter(key => left.clocks?.[key] != null || right.clocks?.[key] != null)
      .map(key => [label(key), left.clocks?.[key], right.clocks?.[key], 'Native clocks']),
    ['YES payout per $1', left.yes_payout, right.yes_payout, 'Observed'],
    ['Venue settled UTC', left.settlement_at, right.settlement_at, 'Observed']];
  const payout = comparison.observed_yes_payout_difference ? 'Different verified payouts'
    : comparison.payout_equivalent ? 'Payout-equivalent' : 'Payout equivalence unverified';
  return `<p><strong>${payout}</strong>
    ${esc(comparison.basis || '')}</p>
    <div class="table-wrap"><table class="market-comparison-table"><thead><tr>
      <th scope="col">Term</th><th scope="col">${esc(name(left.venue))} · ${esc(left.instrument_id)}</th>
      <th scope="col">${esc(name(right.venue))} · ${esc(right.instrument_id)}</th>
      <th scope="col">Check</th></tr></thead><tbody>${rows.map(columns =>
      `<tr>${columns.map(item => `<td>${esc(value(item))}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div>` + block('Nearby book receipts', compareBooks(data.books));
}

function compareBooks(books) {
  if (!books) return '';
  if (!books.latest) return message(`No usable pair within ${books.max_receipt_gap_seconds} seconds among ${books.qualified_polymarket_books} origin-confirmed Polymarket US and ${books.kalshi_books} local Kalshi books in the recent archive.`);
  const pair = books.latest;
  const top = quote => quote && `${figure.format(quote.price)} × ${figure.format(quote.size)}`;
  return metadata([['Local receipt gap in seconds', pair.receipt_difference_seconds],
    ['Polymarket US receipt UTC', pair.polymarket_at], ['Polymarket US exchange UTC', pair.polymarket_source_at],
    ['Kalshi receipt UTC', pair.at], ['Kalshi exchange UTC', pair.kalshi_source_at ?? 'Unavailable'],
    ['Polymarket US YES and NO offers', pair.same_sides.map(side => `${side} ${top(pair.polymarket_quotes[side])}`)],
    ['Kalshi YES and NO offers', pair.same_sides.map(side => `${side} ${top(pair.kalshi_quotes[side])}`)],
  ]) + metrics(books.same_side_cash?.map(row => ({
    side: row.side, contracts: row.common_whole_contracts, polymarket_us_total: '$' + row.polymarket.debit,
    polymarket_us_fee: '$' + row.polymarket.fee, kalshi_total: '$' + row.kalshi.debit, kalshi_fee: '$' + row.kalshi.fee,
  }))) + '<p>Receipt timing indicates when this computer saw each offer. Settlement and book clocks govern the comparison above.</p>';
}

function indexEvidence(index) {
  if (!index) return '';
  const captured = index.captured_by_decision;
  const target = index.target_point;
  const temperature = point => point ? figure.format(Number(point.temperature_f)) + '°F' : 'Unavailable';
  return metadata([
    ['Decision UTC', index.at], ['Target index minute UTC', index.target_at],
    ['Index captured by decision', temperature(captured)], ['Captured minute UTC', captured?.at],
    ['Captured receipt UTC', captured?.received_at], ['Target index archive value', temperature(target)],
    ['Target local archive receipt UTC', target?.received_at], ['Target contributors', target?.contributors],
    ['Target status', target && label(target.status)],
  ]) + (target || captured ? `<p>${external((target || captured).source, 'Venue index record')}</p>` : '')
    + (index.points?.some(row => row.temperature_f != null) ? `<canvas class="chart" id="weather-index-chart" role="img"
        aria-label="Kalshi Weather Index readings by event time and local receipt"></canvas>
      <div class="chart-key">Green · captured by cursor. Gold dash · contract threshold. Red mark · target minute. UTC axis.</div>
      <input id="index-clock" type="range" aria-label="Weather Index replay clock" min="0" step="1">
      <label class="index-archive-toggle"><input type="checkbox" id="index-archive-overlay"> Show archive recovered later</label>
      <div id="index-clock-summary" aria-live="polite"></div>` : '')
    + (index.points?.length ? raw('Archived minute values around the contract clock', index.points) : '');
}

function bindWeatherIndex(market) {
  const canvas = q('#weather-index-chart'), clock = q('#index-clock');
  const overlay = q('#index-archive-overlay'), summary = q('#index-clock-summary');
  if (!canvas || !clock || !overlay || !summary) return;
  const index = market.weather_index, start = Date.parse(index.starts_at), end = Date.parse(index.ends_at);
  const target = Date.parse(index.target_at), reference = Date.parse(index.at);
  const numeric = index.points.filter(row => row.temperature_f != null);
  const rawThreshold = market.lower ?? market.upper;
  const threshold = rawThreshold == null ? null : Number(rawThreshold);
  clock.max = String(Math.ceil((end - start) / 1000));
  clock.value = String(Math.round((reference - start) / 1000));
  function render() {
    const cursor = Math.min(end, start + Number(clock.value) * 1000);
    const observed = new Map(), archive = new Map();
    for (const row of numeric) {
      const minute = Date.parse(row.at);
      if (minute <= cursor && Date.parse(row.received_at) <= cursor) observed.set(minute, row);
      else if (overlay.checked) archive.set(minute, row);
    }
    const local = [...observed.values()];
    const recent = local.at(-1);
    const missed = recent ? null : 'Unavailable at this clock';
    const stamp = new Date(cursor).toISOString();
    clock.setAttribute('aria-valuetext', stamp);
    summary.innerHTML = metadata([['Replay cursor UTC', stamp], ['Index captured by cursor', recent ? `${recent.temperature_f}°F` : missed],
      ['Latest captured minute UTC', recent?.at], ['Its original local receipt UTC', recent?.received_at],
      ['Target at cursor', observed.has(target) ? `${observed.get(target).temperature_f}°F` : 'Awaiting a local target receipt']]);
    weatherIndexChart(canvas, index, cursor, local, [...archive.values()], threshold, time => {
      clock.value = String(Math.round((time - start) / 1000));
      render();
    });
  }
  clock.addEventListener('input', render);
  overlay.addEventListener('change', render);
  const observer = new ResizeObserver(() => {
    if (!canvas.isConnected) observer.disconnect();
    else if (canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) render();
  });
  observer.observe(canvas);
  render();
}

function marketDetail(market, pinned) {
  const replay = market.weather_replay;
  const temperature = ['daily-high', 'daily-low'].includes(market.family);
  const weatherContract = temperature || market.family === 'daily-rain';
  const liveRain = market.family === 'daily-rain' && market.venue === 'polymarket-us' && /^raincli-/.test(market.instrument_id);
  const archiveOnGlobe = liveRain || (market.venue === 'kalshi' && weatherContract);
  const weather = market.status === 'open'
    ? timelineLink(market.station, market.day, { mode: 'live',
      bucket: temperature || archiveOnGlobe ? market.instrument_id : undefined,
      ...(archiveOnGlobe ? { venue: market.venue, instrument: market.instrument_id } : {}) })
    : replay ? timelineLink(replay.station, replay.date, weatherContract
      ? { venue: market.venue, instrument: market.instrument_id, bucket: market.instrument_id }
      : {}) : '';
  const settlement = market.yes_payout == null ? '' : metadata([
    ['YES payout per $1', market.yes_payout], ['NO payout per $1', market.no_payout],
    ['Native result', market.settlement_result], ['Official value reported by venue', market.expiration_value],
    ['Venue settled UTC', market.settlement_at], ['Payout receipt UTC', market.settlement_received_at],
  ]) + `<p>${external(market.settlement_source, 'Public settlement evidence')}</p>`;
  const pinnedHere = pinned?.venue === market.venue && pinned?.instrument === market.instrument_id;
  const action = pinned
    ? `<p class="market-compare-note">Comparison pinned to <strong>${esc(pinned.instrument)}</strong>.
      ${pinnedHere ? 'Open any other contract.' : ''}</p><button type="button" class="small-button" data-compare="clear">Clear comparison</button>`
    : '<button type="button" class="small-button" data-compare="pin">Pin for comparison</button>';
  return `<header><h2>${esc(market.title || market.instrument_id)}</h2>
      <p class="evidence-byline">${esc([name(market.venue), market.instrument_id, label(market.status)].filter(Boolean).join(' · '))}</p>
      <div class="record-links">${weather}${external(market.rules_url || market.source, 'Public rules')}${action}</div></header>`
    + (pinned && !pinnedHere ? '<div class="evidence-block" id="market-comparison">Loading comparison…</div>' : '')
    + block('Contract', metadata([
      ['Family', label(market.family)], ['Bucket', market.label], ['Series', market.series_id],
      ['Event', market.event_id], ['Station', market.station], ['Region', market.region], ['Climate day', market.day],
      ['Reporting start UTC', market.climate_starts_at], ['Reporting end UTC', market.climate_ends_at],
      ['Units', market.units], ['Threshold', [market.operator, market.lower, market.upper].filter(x => x != null).join(' ')],
      ['Resolution source', market.resolution_source], ['Report selection', market.report_policy], ['Fallback', market.fallback],
    ]))
    + block('Listed components', contractComponents(market.components))
    + block('Exact precipitation reporting days', reportingDaysRain(market.reporting_days_rain))
    + block('Thanksgiving precipitation outlook', holidayRainOutlook(market.holiday_rain_outlook))
    + block('Trading clocks', contractClocks(market))
    + block('Cincinnati heating degree-days', hddEvidence(market.hdd_projection))
    + block('Verified payout', settlement)
    + block('Venue weather index', indexEvidence(market.weather_index))
    + (market.monthly_rain?.reports.length ? `<section class="evidence-block" id="monthly-rain-evidence">
      <h3>Published monthly rainfall</h3><canvas class="chart" id="monthly-rain-chart" role="img"
        aria-label="Published rainfall totals by report time with the contract threshold"></canvas>
      <div class="chart-key">NWS month to date · dashed line at ${esc(market.monthly_rain.threshold_in)} in</div>
      <input id="rain-edition" type="range" aria-label="Published daily climate report editions" min="0" max="${market.monthly_rain.reports.length - 1}"
        value="${market.monthly_rain.reports.length - 1}">
      <div id="rain-edition-summary" aria-live="polite"></div>
      ${raw('All archived editions', market.monthly_rain.reports)}</section>` : '')
    + block('Market evidence', metadata([
      ['Displayed YES bid', market.yes_bid], ['Displayed YES ask', market.yes_ask], ['Listed last price', market.last_price],
      ['First catalog receipt UTC', market.first_received_at], ['Latest catalog receipt UTC', market.last_received_at],
    ]) + bookSummary(market.books) + (market.history?.length ? raw('Recent price and trade reports', market.history) : ''))
    + block('Weather coverage', replay ? metadata(Object.entries(replay.coverage).map(([k, v]) => [label(k), v]))
      : market.weather_match ? '<p>The station and reporting window are identified. A historical weather timeline has yet to be linked.</p>' : '')
    + block('Related contracts', comparisonData(market))
    + block('Research cases', market.research_cases?.length ? `<ul>${market.research_cases.map(item =>
      `<li><a href="${esc(paths.research(item.kind, item.run, item.case))}#case-${item.case}">${esc(item.title)}</a></li>`).join('')}</ul>` : '')
    + block('', market.rules_primary || market.rules_secondary ? `<details><summary>Settlement rules</summary>
      ${[market.rules_primary, market.rules_secondary].filter(Boolean).map(text => `<p>${esc(text)}</p>`).join('')}</details>` : '')
    + block('', raw('Native market and receipt', { market: market.raw_market, receipt: market.receipt,
      settlement_receipt: market.settlement_receipt }));
}

function bindMonthlyRain(period) {
  const slider = q('#rain-edition'), canvas = q('#monthly-rain-chart'), summary = q('#rain-edition-summary');
  if (!period || !slider || !canvas || !summary) return;
  const clocks = period.reports.map(row => Date.parse(row.published_at));
  function render() {
    const selected = period.reports[Number(slider.value)];
    summary.innerHTML = metadata([['Published UTC', selected.published_at], ['Station climate day through', selected.climate_day],
      ['Month-to-date total', selected.monthly_raw === 'T' ? 'Trace' : `${selected.monthly_raw} in`],
      ['Threshold at this issue', selected.amount_in > period.threshold_in ? 'Exceeded' : 'Pending']])
      + `<p>${external(selected.source, 'Original NWS climate report')}</p>`;
    slider.setAttribute('aria-valuetext', selected.climate_day + ' · ' + selected.monthly_raw + ' inches');
    monthlyRainChart(canvas, period, Date.parse(selected.published_at), clock => {
      let index = clocks.length - 1;
      while (index > 0 && !(clocks[index] <= clock)) index--;
      slider.value = String(index);
      render();
    });
  }
  slider.addEventListener('input', render);
  const observer = new ResizeObserver(() => {
    if (!canvas.isConnected) observer.disconnect();
    else if (canvas.width !== Math.round(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))) render();
  });
  observer.observe(canvas);
  render();
}

function filterParams(form) {
  return new URLSearchParams([...new FormData(form)].map(([key, data]) => [key, data.trim()]).filter(([, data]) => data));
}

function initMarketArchive(params) {
  const form = q('#market-filters');
  const results = q('#market-results');
  const inspector = q('#market-inspector');
  const marketPageSize = 35;
  let offset = 0, resultGeneration = 0, detailGeneration = 0;
  let selectedVenue = params.get('venue'), instrument = params.get('instrument'), indexAt = params.get('index_at');
  let selected = null;
  let pinned = params.get('compare_venue') && params.get('compare_instrument')
    ? { venue: params.get('compare_venue'), instrument: params.get('compare_instrument') }
    : null;
  for (const [key, data] of params) {
    if (key !== 'family' && form.elements.namedItem(key)) form.elements.namedItem(key).value = data;
  }
  if (params.get('family')) {
    form.elements.family.add(new Option(label(params.get('family')), params.get('family'), true, true));
  }

  async function showCoverage() {
    try {
      const data = await read('/api/markets/coverage');
      const families = [...new Set(data.markets.map(row => row.family))].sort();
      const selected = form.elements.family.value;
      form.elements.family.innerHTML = '<option value="">All families</option>' + families.map(key =>
        `<option value="${esc(key)}">${esc(label(key))}</option>`).join('');
      if (selected && !families.includes(selected)) form.elements.family.add(new Option(label(selected), selected));
      form.elements.family.value = selected;
      q('#market-coverage').textContent = data.venues.length ? data.venues.map(row => {
        const books = data.books.filter(item => item.venue === row.venue).reduce((sum, item) => sum + item.snapshots, 0);
        return `${name(row.venue)} ${count.format(row.instruments)} instruments · ${count.format(row.payouts || 0)} payouts`
          + (books ? ` · ${count.format(books)} book receipts` : '');
      }).join(' / ') : 'Waiting for the first catalog receipts.';
      const performance = [
        ...data.books.map(row => ({
          venue: name(row.venue), evidence: row.scope || 'Native book receipts', rows: row.snapshots, instruments: row.instruments,
          with_exchange_clock: row.source_clock, from: row.first_at, through: row.last_at,
        })),
        ...data.history.map(row => ({
          venue: name(row.venue), evidence: label(row.kind), rows: row.points, instruments: row.instruments,
          from: row.first_at, through: row.last_at,
        })),
      ];
      const gaps = data.pending?.slice(0, 20).map(row => ({
        venue: name(row.venue), source: label(row.kind), scope: row.scope, state: row.state, pages: row.pages, issue: row.error,
      }));
      const byFamily = data.families?.map(row => ({ venue: name(row.venue), family: label(row.family),
        cataloged: row.cataloged, station_day_identified: row.exact_station_day, full_rule_window: row.exact_station_rule_windows,
        linked_station_weather: row.linked_station_weather, verified_payout: row.verified_payouts,
        archived_display_price: row.display_price_history, archived_book: row.depth_receipts,
        additional_indexed_high_books: row.in_direct_capture_index,
      }));
      const container = q('#market-coverage-details');
      container.hidden = !performance.length && !gaps?.length && !byFamily?.length;
      q('#market-coverage-detail-content').innerHTML = block('Instruments by family', metrics(byFamily)
        + '<p>Linked weather uses the same station and day. The contract rules identify which reports determine the payout.</p>')
        + block('Retained observations', metrics(performance)) + block('Collection gaps', metrics(gaps));
    } catch (error) { q('#market-coverage').textContent = error.message; }
  }

  function markSelected() {
    for (const row of results.querySelectorAll('tr[data-instrument]')) {
      row.dataset.active = String(row.dataset.instrument === instrument && row.dataset.venue === selectedVenue);
    }
  }

  function updateUrl() {
    const url = new URL(paths.market(selectedVenue, instrument, indexAt), location.origin);
    if (pinned) {
      url.searchParams.set('compare_venue', pinned.venue);
      url.searchParams.set('compare_instrument', pinned.instrument);
    }
    history.replaceState(null, '', url);
  }

  async function showDetail(generation) {
    inspector.innerHTML = marketDetail(selected, pinned);
    bindMonthlyRain(selected.monthly_rain);
    bindWeatherIndex(selected);
    const comparison = q('#market-comparison');
    if (!comparison) return;
    const query = new URLSearchParams({
      first_venue: pinned.venue, first_instrument: pinned.instrument,
      second_venue: selected.venue, second_instrument: selected.instrument_id,
    });
    try {
      const data = await read('/api/markets/compare?' + query);
      if (generation === detailGeneration && comparison.isConnected) comparison.innerHTML = sideBySide(data);
    } catch (error) {
      if (generation === detailGeneration && comparison.isConnected) comparison.textContent = error.message;
    }
  }

  async function loadDetail(venue, id, updateLocation = true, clock = null) {
    const generation = ++detailGeneration;
    selectedVenue = venue;
    instrument = id;
    indexAt = clock;
    markSelected();
    inspector.innerHTML = message('Loading contract…');
    if (updateLocation) updateUrl();
    try {
      const data = await read('/api/markets/detail?' + new URLSearchParams({
        venue, instrument: id, ...(indexAt ? { index_at: indexAt } : {}),
      }));
      if (generation === detailGeneration) {
        selected = data;
        showDetail(generation);
      }
    } catch (error) {
      if (generation === detailGeneration) inspector.innerHTML = message(error.message);
    }
  }

  async function loadResults() {
    const generation = ++resultGeneration;
    const filters = filterParams(form);
    for (const anchor of q('#market-exports').querySelectorAll('a')) {
      anchor.search = filters.toString();
    }
    const query = new URLSearchParams(filters);
    query.set('limit', marketPageSize);
    query.set('offset', offset);
    results.innerHTML = message('Loading archive…');
    try {
      const data = await read('/api/markets?' + query);
      if (generation !== resultGeneration) return;
      results.innerHTML = data.rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th scope="col">Contract</th><th scope="col">Venue</th><th scope="col">Place</th><th scope="col">Status</th></tr></thead>
        <tbody>${data.rows.map(item => `<tr data-venue="${esc(item.venue)}" data-instrument="${esc(item.instrument_id)}">
          <td><a data-market-link href="${esc(paths.market(item.venue, item.instrument_id))}">${esc(item.title || item.instrument_id)}</a>
            <small>${esc(item.instrument_id)}</small></td>
          <td>${esc(name(item.venue))}</td><td>${esc(item.station || item.region || '—')}<small>${esc(item.day || '')}</small></td>
          <td>${esc(label(item.status))}${item.yes_payout != null ? '<small>Verified payout</small>' : ''}</td>
        </tr>`).join('')}</tbody></table></div>` : message('No contracts matched these filters.');
      markSelected();
      q('#market-paging').hidden = data.total <= marketPageSize;
      q('#market-prev').disabled = offset === 0;
      q('#market-next').disabled = offset + marketPageSize >= data.total;
      q('#market-range').textContent = `${count.format(offset + 1)}–${count.format(offset + data.rows.length)} of ${count.format(data.total)}`;
    } catch (error) {
      if (generation === resultGeneration) results.innerHTML = message(error.message);
    }
  }

  function selectMarket(event) {
    const target = event.target.closest('a[data-market-link]');
    if (!target || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    const query = new URL(target.href).searchParams;
    loadDetail(query.get('venue'), query.get('instrument'), true, query.get('index_at'));
    if (window.matchMedia('(max-width: 760px)').matches) inspector.scrollIntoView({ block: 'start' });
  }
  results.addEventListener('click', selectMarket);
  inspector.addEventListener('click', event => {
    const action = event.target.closest('[data-compare]')?.dataset.compare;
    if (!action) { selectMarket(event); return; }
    pinned = action === 'pin' ? { venue: selected.venue, instrument: selected.instrument_id } : null;
    updateUrl();
    showDetail(++detailGeneration);
  });
  q('#market-prev').addEventListener('click', () => { offset = Math.max(0, offset - marketPageSize); loadResults(); });
  q('#market-next').addEventListener('click', () => { offset += marketPageSize; loadResults(); });
  form.addEventListener('submit', event => {
    event.preventDefault();
    offset = 0;
    const next = filterParams(form);
    next.set('view', 'markets');
    history.replaceState(null, '', '/?' + next);
    loadResults();
  });
  showCoverage();
  loadResults();
  if (selectedVenue && instrument) loadDetail(selectedVenue, instrument, false, indexAt);
}

export function startEvidence(workspace, params) {
  if (workspace === 'markets') initMarketArchive(params);
  if (workspace === 'research') initStudies(params);
}
