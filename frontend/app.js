import '@fontsource-variable/dm-sans/wght.css';
import '@fontsource/ibm-plex-mono/latin-400.css';
import '@fontsource/ibm-plex-mono/latin-500.css';
import './style.css';
import { makeGlobe } from './globe.js';
import { charts, precipitationChart } from './charts.js';
import { startEvidence } from './evidence.js';
import { esc, external as link } from './evidence_format.js';
import { startTrade } from './trade.js';
import { startBacktest } from './backtest.js';
import { chooseBucket, createTimeline, stamp, timelineState } from './timeline.js';
import { MPH_PER_MPS, pointValue, pointWind } from './probe.js';

const q = selector => document.querySelector(selector);
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const params = new URLSearchParams(location.search);
const fixture = params.get('fixture') === '1';
const context = fixture ? 'fixture=1&' : '';
const workspace = ['trade', 'weather', 'backtest', 'markets', 'research'].includes(params.get('view')) ? params.get('view')
  : [...params.keys()].length ? 'weather' : 'home';
const decisionWorkspace = ['home', 'trade', 'weather'].includes(workspace);
let tradeView;
const layerControls = { weather: '#weather-toggle', clouds: '#cloud-toggle', wind: '#wind-toggle',
  precipitation: '#precip-toggle', radar: '#radar-toggle', marine: '#marine-toggle', waves: '#wave-toggle', markets: '#markets-toggle' };
const state = {
  ...timelineState(params, fixture),
  playing: null, request: 0,
  paper: null, paperTimer: null, paperRequest: null, grid: null, cloud: null, wind: null, precipitation: null, waves: null, radar: null,
  historicalDecision: null, decisionRequest: null, decisionTimer: null,
  hrrrIssue: params.get('hrrr_issue') || '',
  hrrrProbe: params.get('hrrr_point') === '1',
  collection: null, collectionRevision: '', point: null, probeRequest: 0,
  refreshingLive: false, queuedLiveRefresh: false, selectingEvent: false,
  recordedPaper: null,
  saved: null, savedGeneration: 0,
  ledgerVisible: false,
  marineStations: [], marineKey: null, marinePending: false,
  tradeBoard: null, focus: workspace,
};
const timeline = createTimeline(state, params);
const { exactClock, known, frameKnown, lastBefore, knowledge, updateBounds, hourlyForecast, forecastHours } = timeline;
const formatters = new Map();
const cachedFrames = new Map();
const frameRequests = new Map();
const METHODS = {
  noaa: 'NOAA', station_bias: 'Station/time bias', ridge: 'Ridge', ridge_sky: 'Ridge + forecast sky',
  guarded_cap: 'METAR-bounded low', p06_and_gauge: 'NOAA P06 + gauge evidence',
};
const VENUES = { 'polymarket-us': 'Polymarket US', kalshi: 'Kalshi' };
const stationName = event => event.station_name || event.station;
const stationChoice = event => event.place || stationName(event);
const measure = event => ({ 'daily-low': 'Daily low', 'daily-rain': 'Daily rain' })[event.family] || 'Daily high';
const quoteKnown = (row, at) => known(row.available_at || row.at, at);
const contractClose = (event, bucket) => bucket?.native_trading_closed_at || event.trading_closed_at || event.ends_at;
const numeric = n => Number.isFinite(n);
const temp = (n, digits = 0) => numeric(n) ? `${n.toFixed(digits)}°` : '—';
const pct = (n, digits = 0) => numeric(n) ? `${(n * 100).toFixed(digits)}%` : '—';
const number = (n, digits = 2) => numeric(n) ? n.toFixed(digits) : '—';
const money = n => numeric(n) ? `${n < 0 ? '−' : ''}$${Math.abs(n).toFixed(2)}` : '—';
const signed = n => numeric(n) ? `${n > 0 ? '+' : ''}${money(n)}` : '—';
const options = {
  day: { month: 'short', day: 'numeric', year: 'numeric' },
  short: { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' },
  clock: { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' },
  seconds: { hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short' },
};
function when(value, zone = 'UTC', kind = 'short') {
  const t = typeof value === 'number' ? value : stamp(value);
  if (!numeric(t)) return '—';
  const key = zone + kind;
  if (!formatters.has(key)) formatters.set(key, new Intl.DateTimeFormat('en-US', { ...options[kind], timeZone: zone }));
  return formatters.get(key).format(t);
}
function imageLag(receivedAt, sourceAt) {
  const seconds = Math.floor((stamp(receivedAt) - stamp(sourceAt)) / 1000);
  if (!numeric(seconds) || seconds < 0) return '';
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h ${minutes % 60}m older` : minutes ? `${minutes}m ${seconds % 60}s older` : `${seconds}s older`;
}
function stat(label, value) { return `<div class="stat"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`; }
function chip(color, label) {
  return `<span><i class="legend-chip" style="--chip:${color}"></i>${esc(label)}</span>`;
}

const globe = decisionWorkspace ? makeGlobe({ onPin: chooseEvent, onProbe: point => {
  state.point = point;
  q('.probe-options').open = true;
  showProbe();
} }) : null;
const currentEvent = () => state.session?.events.find(item => item.id === state.eventId);
const currentBucket = event => event?.buckets.find(item => item.id === state.bucketId);

async function get(url, init) {
  const response = await fetch(url, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function post(url, body) {
  return get(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
function note(message, kind = 'info') {
  const el = q('#notice');
  el.hidden = !message;
  el.dataset.kind = kind;
  el.textContent = message;
}
function eventDate(event) {
  const parts = event?.date?.split('-');
  if (!parts || parts.length !== 3) return '';
  return when(`${event.date}T12:00:00Z`, 'UTC', 'day');
}
function retainWeatherMarket(event) {
  const bucket = currentBucket(event);
  if (bucket && (event.venue === 'kalshi' || event.replay_market_window_exact != null)) {
    state.linkedMarket = { venue: event.venue || 'polymarket-us', instrument: bucket.id };
    state.linkedBucket = bucket.id;
  } else {
    state.linkedMarket = null;
    state.linkedBucket = null;
  }
}
function chooseEvent(id) {
  const prior = currentEvent();
  if (prior?.id === id) return;
  const event = state.session?.events.find(item => item.id === id);
  if (!event) return;
  stopPlayback();
  state.preferredStation = event.station;
  const url = new URL(location.href);
  url.searchParams.set('station', event.station);
  history.replaceState(null, '', url);
  state.hrrrIssue = '';
  state.selectedSide = 'YES';
  state.linkedMarket = null;
  state.linkedBucket = null;
  state.preferTrade = false;
  tradeView?.showStation(event.station);
  globe.centerOn(event);
  if (event.summary_only) {
    note(`Loading ${stationChoice(event)} station evidence…`);
    return loadMode(state.mode, true, id);
  }
  state.eventId = id;
  state.bucketId = chooseBucket(event)?.id;
  retainWeatherMarket(event);
  updateBounds(event, state.mode === 'replay' && prior?.date !== event.date);
  render();
  selectWeatherBucket();
}

const historicalContext = () => timeline.historicalContext(currentEvent());

// A live-history link supplies its own decision clock. Replay first resolves
// the requested clock against the saved timeline for the exact contract.
const ticketClockReady = () => state.mode !== 'replay' || (!!state.bounds &&
  (state.session?.weather_window || !linkedWeatherStatus(currentEvent(), currentBucket(currentEvent()))));

function decisionQuery(event, at) {
  if (!event || event.summary_only || event.replay_no_model || !historicalContext() ||
      !['daily-high', 'daily-low'].includes(event.family || 'daily-high')) return null;
  const bucket = currentBucket(event);
  return new URLSearchParams({ mode: state.mode, day: event.date, event: event.id, at: exactClock(at),
    ...(fixture ? { fixture: '1' } : {}),
    ...(bucket ? { venue: event.venue || 'polymarket-us', instrument: bucket.id } : {}) }).toString();
}

function stationDecision(event, at) {
  if (!historicalContext()) {
    return state.paper?.decisions?.[event?.id] || event?.live_decision ||
      lastBefore(event?.live_shadow_decisions || (fixture ? event?.decisions : []), 'at', at);
  }
  const key = decisionQuery(event, at);
  return key && state.historicalDecision?.key === key ? state.historicalDecision.decision : null;
}

function refreshStationDecision(event) {
  const key = decisionQuery(event, knowledge());
  if (state.decisionRequest?.key === key) return;
  clearTimeout(state.decisionTimer);
  state.decisionRequest?.controller.abort();
  state.decisionRequest = null;
  if (!key || state.historicalDecision?.key === key) return;
  const controller = new AbortController();
  state.decisionRequest = { key, controller };
  state.decisionTimer = setTimeout(async () => {
    try {
      const { decision } = await get('/api/decision?' + key, { signal: controller.signal });
      if (controller.signal.aborted) return;
      state.historicalDecision = { key, decision };
    } catch (error) {
      if (controller.signal.aborted) return;
      state.historicalDecision = { key, error: error.message };
    }
    const current = currentEvent();
    const bucket = currentBucket(current);
    showStation(current, bucket);
    showWeatherSummary(current, bucket);
    if (state.mode === 'replay') showOpportunities(current);
  }, 120);
}

function selectWeatherBucket(focus = false) {
  const event = currentEvent();
  const bucket = currentBucket(event);
  if (!bucket) {
    tradeView?.align(null, focus);
    return;
  }
  tradeView?.align({ venue: event.venue || 'polymarket-us', instrument: bucket.id,
    side: state.selectedSide, station: event.station, day: event.date }, focus);
}

function linkedWeatherStatus(event, bucket) {
  const market = state.preferTrade && state.linkedMarket;
  if (!market || (!event?.summary_only && bucket?.id === market.instrument &&
      (event.venue || 'polymarket-us') === market.venue)) return '';
  return state.selectingEvent ? 'Loading weather for the selected contract…'
    : 'Weather for the selected contract is unavailable in this timeline.';
}

async function selectTradeWeather(row, opening) {
  state.selectedSide = row.side;
  state.preferTrade = true;
  const market = state.linkedMarket = { venue: row.venue, instrument: row.instrument };
  const request = ++state.request;
  const match = state.session?.events.find(item => (item.venue || 'polymarket-us') === row.venue &&
    item.buckets.some(bucket => bucket.id === row.instrument));
  if (match && !match.summary_only) {
    state.selectingEvent = false;
    state.linkedBucket = null;
    state.eventId = match.id;
    state.bucketId = row.instrument;
    state.preferredStation = match.station;
    updateBounds(match, false);
    render();
    globe.centerOn(match);
    return;
  }
  state.linkedBucket = row.instrument;
  if (row.station) state.preferredStation = row.station;
  state.selectingEvent = true;
  const event = currentEvent();
  showWeatherSummary(event, currentBucket(event));
  showStation(event, currentBucket(event));
  q('#globe-contract').hidden = true;
  await opening;
  if (request !== state.request) return;
  if (state.linkedMarket !== market) {
    state.selectingEvent = false;
    return;
  }
  loadMode(state.mode, !!state.session);
}

function chooseMode(mode) {
  stopPlayback();
  state.paperRequest?.abort();
  state.paper = null;
  const clearSession = timeline.resetMode(mode);
  tradeView?.setContext(historicalContext(), [], ticketClockReady());
  q('#paper-content').innerHTML = '';
  q('#archive-control').hidden = mode !== 'replay';
  q('#capture-control').hidden = mode !== 'live';
  for (const el of document.querySelectorAll('[data-mode]')) el.setAttribute('aria-pressed', String(el.dataset.mode === mode));
  if (clearSession) render();
  return loadMode(mode);
}
function switchMode(mode) {
  const url = new URL(location.href);
  url.searchParams.set('mode', mode);
  for (const name of ['at', 'archived', 'history_mode', 'forecast', 'forecast_valid']) url.searchParams.delete(name);
  if (mode === 'replay') {
    url.searchParams.set('day', state.replayDay);
    state.preferTrade = false;
    state.linkedMarket = null;
    state.linkedBucket = null;
  }
  history.replaceState(null, '', url);
  if (state.linkedMarket) state.linkedBucket = state.linkedMarket.instrument;
  state.linkedAt = null;
  if (mode === 'replay' && !q('#archive-day').options.length) loadArchive();
  return chooseMode(mode);
}
async function loadMode(mode, refresh = false, eventId = null) {
  const request = ++state.request;
  state.selectingEvent = !!(eventId || state.linkedBucket || !refresh);
  try {
    const query = new URLSearchParams({ mode });
    if (fixture) query.set('fixture', '1');
    if (mode === 'replay') query.set('day', state.replayDay);
    else if (!fixture) {
      query.set('compact', '1');
      query.set('station', state.preferredStation);
      if (eventId || (refresh && state.eventId)) query.set('event', eventId || state.eventId);
    }
    if (state.linkedMarket) {
      query.set('venue', state.linkedMarket.venue);
      query.set('instrument', state.linkedMarket.instrument);
    }
    const session = await get('/api/session?' + query);
    if (request !== state.request) return;
    const { event, prior, linkedEvent, missingClock } = timeline.selectSession(session, mode, refresh);
    if (mode === 'live') {
      const sessions = [...new Map((session.sessions || []).map(item => [item.captured_at, item])).values()];
      const recent = (sessions.length > 80 ? [sessions[0], ...sessions.slice(-79)] : sessions.slice()).reverse();
      const control = q('#capture-at');
      const selection = refresh ? control.value : '';
      control.innerHTML = '<option value="">Latest data</option>' + recent.map(record =>
        `<option value="${esc(record.captured_at)}">${esc(when(record.captured_at, event?.timezone, 'seconds'))}</option>`).join('');
      control.value = selection;
    }
    const missingStation = state.linkedBucket && !linkedEvent;
    const stationNotice = 'The saved timeline has no matching station readings for this contract.';
    if (missingClock) note(`The linked time is outside the saved timeline. Showing its nearest recorded endpoint: ${exactClock(state.cursor)}.` +
      (missingStation ? ' ' + stationNotice : ''));
    else if (missingStation) note(stationNotice);
    else note('');
    state.linkedBucket = null;
    state.selectingEvent = false;
    render();
    if (event && (!refresh || eventId || linkedEvent && state.preferTrade && prior?.id !== linkedEvent.id)) globe.centerOn(event);
    if ((state.focus === 'weather' || mode === 'replay' || tradeView?.hasExplicitSelection())
      && (!state.preferTrade || !tradeView?.hasSelection())) selectWeatherBucket();
  } catch (error) {
    if (request !== state.request) return;
    if (!refresh) {
      state.session = null;
      state.loadedMode = null;
      state.bounds = null;
    }
    state.selectingEvent = false;
    note(`${refresh ? 'Could not refresh' : 'Could not read'} the local cache. ${error.message}`, 'error');
    render();
  } finally {
    if (request === state.request) state.selectingEvent = false;
  }
}

function stopPlayback() {
  if (state.playing) clearInterval(state.playing);
  state.playing = null;
  q('#play').textContent = '▶';
  q('#play').setAttribute('aria-label', 'Play timeline');
}
function seek(ms, iso = null) {
  if (timeline.moveCursor(ms, iso)) render();
}
function forecastToggle() {
  if (!state.bounds) return;
  stopPlayback();
  const event = currentEvent();
  if (state.pivot == null) {
    // Prevent an in-flight live refresh from replacing the fixed-origin inputs.
    state.request += 1;
    state.selectingEvent = false;
    timeline.fixForecastOrigin();
    state.paperRequest?.abort();
    updateBounds(event, false);
    const next = timeline.firstForecastValidTime(event);
    state.paper = null;
    seek(next);
  } else {
    const url = new URL(location.href);
    url.searchParams.delete('forecast');
    url.searchParams.delete('forecast_valid');
    if (state.mode === 'live' && state.follow) {
      url.searchParams.delete('at');
      url.searchParams.delete('archived');
      url.searchParams.delete('history_mode');
    }
    history.replaceState(null, '', url);
    timeline.returnToOrigin(event);
    render();
    if (followingLiveClock()) refreshFollowingLive();
  }
}

function forecastAddress(event, bucket) {
  const url = new URL(location.href);
  for (const [key, value] of Object.entries({
    ...historicalContext(), mode: state.mode, forecast_valid: new Date(state.cursor).toISOString(),
    ...(event ? { station: event.station } : {}),
  })) url.searchParams.set(key, value);
  if (bucket) {
    url.searchParams.set('venue', event.venue || 'polymarket-us');
    url.searchParams.set('instrument', bucket.id);
    url.searchParams.set('side', state.selectedSide);
    url.searchParams.delete('bucket');
  }
  if (url.href !== location.href) history.replaceState(null, '', url);
}

function showForecastOutlook(event, bucket) {
  const section = q('#forecast-outlook');
  section.hidden = state.pivot == null;
  if (section.hidden) return;
  forecastAddress(event, bucket);
  const zone = event?.timezone;
  const origin = state.pivot;
  const forecast = hourlyForecast(event, origin);
  const points = forecastHours(event);
  const outlook = lastBefore(event?.extended_outlooks, 'known_at', origin);
  const days = (outlook?.daily_extrema || []).filter(day => stamp(day.climate_ends_at) > origin).slice(0, 4);
  const host = q('#forecast-outlook-content');
  const focused = host.contains(document.activeElement) ? document.activeElement.dataset.forecastAt : null;
  host.innerHTML = `<div class="forecast-head"><div><h2 id="outlook-heading">Forecast ahead</h2>
    <p>${event ? esc(stationChoice(event)) + ' · ' + esc(event.date) : 'Global weather'}
      ${bucket ? ' · ' + esc(VENUES[event.venue || 'polymarket-us']) + ' · ' + esc(bucket.label) + ' · ' + state.selectedSide : ''}</p></div>
    <button type="button" class="small-button" id="forecast-return">${state.mode === 'live' ? 'Return to live' : 'Return to replay'}</button></div>
    <div class="forecast-clock-pair">${stat('Fixed origin', when(origin, zone, 'seconds'))}
    ${stat('Viewing valid time', when(state.cursor, zone))}</div>
    ${points.length ? `<p class="forecast-clocks">Hourly NOAA guidance · Issued ${esc(when(forecast.issued_at, zone))}
      · Available ${esc(when(forecast.available_at, zone))}</p>
      <div class="forecast-hours" role="group" aria-label="Forecast valid hours">${points.map(point =>
        `<button type="button" class="small-button" data-forecast-at="${esc(point.valid_at)}"
        aria-pressed="${stamp(point.valid_at) === state.cursor}"><span>${esc(when(point.valid_at, zone))}</span>
        <strong>${esc(temp(point.temp_f))}F</strong></button>`).join('')}</div>`
      : '<p>No later local hourly forecast was available at this origin. Check the map timeline or extended outlook where present.</p>'}
    ${days.length ? `<details ${q('#forecast-days')?.open ? 'open' : ''} id="forecast-days"><summary>Next days from NOAA</summary>
      <p class="forecast-clocks">Issued ${esc(when(outlook.issued_at, zone))} · Available ${esc(when(outlook.known_at, zone))}</p>
      ${table(['Station date', 'Low °F', 'Low valid', 'High °F', 'High valid'], days.map(day => row([
        day.target_date, day.minimum.temp_f, when(day.minimum.valid_at, zone), day.maximum.temp_f,
        when(day.maximum.valid_at, zone),
      ])))}</details>` : ''}
    <p class="forecast-clocks">Check live again after the next station report or NOAA update. ${bucket ?
      'The reported weather and a fresh exchange book can change whether this exact side qualifies.' :
      'The next report can confirm or change the expected high or low.'}</p>`;
  q('#forecast-return').onclick = () => {
    if (state.mode === 'live') switchMode('live');
    else forecastToggle();
    q('#preview').focus({ preventScroll: true });
  };
  for (const button of host.querySelectorAll('[data-forecast-at]')) {
    button.onclick = () => seek(stamp(button.dataset.forecastAt));
    if (button.dataset.forecastAt === focused) button.focus({ preventScroll: true });
  }
}
function getFrame(path, frame) {
  if (!frame) return Promise.resolve(null);
  const url = path + encodeURIComponent(frame.id);
  if (cachedFrames.has(url)) return Promise.resolve(cachedFrames.get(url));
  if (!frameRequests.has(url)) frameRequests.set(url, get(url).then(data => {
    cachedFrames.set(url, data);
    // Each frame is small. Bound the browser cache for long browsing sessions.
    if (cachedFrames.size > 100) cachedFrames.delete(cachedFrames.keys().next().value);
    return data;
  }).catch(() => null).finally(() => frameRequests.delete(url)));
  return frameRequests.get(url);
}
function loadFrame(slot, frame, path, set) {
  if (frame?.id === state[slot]?.id) return;
  const ticket = frame ? { id: frame.id } : null;
  state[slot] = ticket;
  q('#world').classList.remove('loading-' + slot);
  if (!ticket) { set(null); return; }
  const cached = cachedFrames.get(path + encodeURIComponent(ticket.id));
  set(cached ?? null);
  if (cached) return;
  q('#world').classList.add('loading-' + slot);
  getFrame(path, frame).then(data => {
    if (state[slot] !== ticket) return;
    q('#world').classList.remove('loading-' + slot);
    set(data);
    if (!data) state[slot] = null;
  });
}
function latestFrame(frames) {
  return (frames || []).filter(row => frameKnown(row, knowledge()) && known(row.valid_at, state.cursor))
    .sort((a, b) => stamp(b.valid_at) - stamp(a.valid_at) || stamp(b.issued_at) - stamp(a.issued_at))[0];
}

function precipitationFrame() {
  const frame = latestFrame(state.session?.precipitation);
  return frame && stamp(frame.valid_at) >= state.cursor - 3 * HOUR ? frame : undefined;
}

function waveFrame() {
  const pinned = state.mode === 'replay' && state.session?.waves?.find(row => row.id === params.get('wave_frame'));
  if (pinned && frameKnown(pinned, knowledge()) && stamp(pinned.valid_at) <= state.cursor
      && stamp(pinned.valid_at) >= state.cursor - 3 * HOUR) return pinned;
  const frame = latestFrame(state.session?.waves);
  return frame && stamp(frame.valid_at) >= state.cursor - 3 * HOUR ? frame : undefined;
}

function radarFrame() {
  const frame = latestFrame(state.session?.radar);
  return frame && stamp(frame.valid_at) >= state.cursor - 75 * MINUTE ? frame : undefined;
}

function refreshMarine(enabled) {
  const at = knowledge();
  if (!enabled || !numeric(at) || !at || fixture) {
    state.marineKey = null;
    state.marineStations = [];
    state.marinePending = false;
    globe.marine([]);
    return;
  }
  const key = exactClock(at);
  if (key === state.marineKey) return;
  state.marineKey = key;
  state.marineStations = [];
  state.marinePending = true;
  globe.marine([]);
  get('/api/marine?' + new URLSearchParams({ at: key })).then(data => {
    if (state.marineKey !== key) return;
    state.marineStations = data.stations;
    globe.marine(data.stations);
  }).catch(() => {}).finally(() => {
    if (state.marineKey !== key) return;
    state.marinePending = false;
    if (state.point) showProbe();
  });
}

function nearestMarine([lon, lat]) {
  const radians = Math.PI / 180;
  const close = Math.sin(lat * radians);
  const cross = Math.cos(lat * radians);
  let nearest = null, score = -Infinity;
  for (const report of state.marineStations) {
    const phi = report.lat * radians;
    const similarity = close * Math.sin(phi) + cross * Math.cos(phi) * Math.cos((report.lon - lon) * radians);
    if (similarity > score) { score = similarity; nearest = report; }
  }
  if (!nearest) return null;
  return { ...nearest, distanceKm: Math.acos(Math.max(-1, Math.min(1, score))) * 6371 };
}

function marineReading(report) {
  if (!q('#marine-toggle').checked) return '';
  if (!report) return `<div class="probe-meta">${state.marinePending ? 'Loading marine reports…' : 'No marine report known by this time.'}</div>`;
  const fahrenheit = c => numeric(c) ? (c * 1.8 + 32).toFixed(1) + '°F' : '';
  const fields = [
    numeric(report.water_temperature_c) ? `water ${fahrenheit(report.water_temperature_c)}` : '',
    numeric(report.wave_height_m) ? `waves ${number(report.wave_height_m, 1)} m` : '',
    numeric(report.dominant_period_s) && report.dominant_period_s > 0 ? `dominant period ${number(report.dominant_period_s, 1)} s` : '',
    numeric(report.wind_speed_mps) ? `wind ${number(report.wind_speed_mps * MPH_PER_MPS, 1)} mph` : '',
    numeric(report.pressure_hpa) ? `pressure ${number(report.pressure_hpa, 1)} hPa` : '',
  ].filter(Boolean).join(' · ');
  return `<div class="probe-meta">${link('https://www.ndbc.noaa.gov/station_page.php?station=' + encodeURIComponent(report.station),
    'NDBC ' + report.station)} · ${number(report.distanceKm, 0)} km from probe${fields ? ' · ' + esc(fields) : ''}
    <br>Measured ${when(report.measured_at, 'UTC', 'seconds')} · received ${when(report.available_at, 'UTC', 'seconds')}</div>`;
}

function hrrrProbeFrame() {
  const valid = Math.floor(state.cursor / HOUR) * HOUR;
  const selected = currentEvent()?.hrrr_temperature;
  const lane = selected?.length ? selected : state.session?.events.find(event => event.hrrr_temperature?.length)?.hrrr_temperature;
  return lastBefore(lane, 'issued_at', knowledge(), entry => stamp(entry.valid_at) === valid &&
    (!state.hrrrIssue || entry.issued_at === state.hrrrIssue) && frameKnown(entry, knowledge()));
}

async function showProbe() {
  const panel = q('#probe-panel');
  if (!state.point) {
    ++state.probeRequest;
    state.probeKey = '';
    panel.innerHTML = '<span>Point probe</span><span>Click the globe or focus it and press Enter to inspect local weather.</span>';
    return;
  }
  const point = state.point;
  const [lon, lat] = point;
  const grid = latestFrame(state.session?.grids);
  const cloud = latestFrame(state.session?.clouds);
  const wind = latestFrame(state.session?.winds);
  const precip = precipitationFrame();
  const wave = q('#wave-toggle').checked ? waveFrame() : undefined;
  const componentClock = wave?.components && frameKnown(wave.components, knowledge()) ? wave.components : null;
  const radar = q('#radar-toggle').checked ? radarFrame() : undefined;
  const hrrrArea = !fixture && lat >= 20 && lat <= 56 && lon >= -141 && lon <= -52;
  const hrrrFrame = hrrrArea && state.hrrrProbe ? hrrrProbeFrame() : null;
  const buoy = q('#marine-toggle').checked ? nearestMarine(point) : null;
  const archivedBuoy = state.mode === 'replay' && wave && Math.abs(lat - Number(params.get('probe_lat'))) < .00001
    && Math.abs(lon - Number(params.get('probe_lon'))) < .00001 ? params.get('probe_buoy') : null;
  const forecastOrigin = grid || cloud;
  const horizon = (state.session?.grids || []).filter(frame => frame.issued_at === forecastOrigin?.issued_at
    && frameKnown(frame, knowledge()) && stamp(frame.valid_at) > state.cursor)
    .sort((a, b) => stamp(a.valid_at) - stamp(b.valid_at)).slice(0, 5);
  const key = [lon, lat, grid?.id, cloud?.id, wind?.id, precip?.id, wave?.id, componentClock?.sha256, radar?.id,
    state.hrrrProbe, state.mode, hrrrFrame?.sha256 || hrrrFrame?.issued_at, hrrrFrame?.valid_at,
    q('#marine-toggle').checked, state.marinePending, buoy?.station, buoy?.available_at, archivedBuoy, ...horizon.map(item => item.id)].join('|');
  if (key === state.probeKey) return;
  state.probeKey = key;
  const request = ++state.probeRequest;
  panel.textContent = 'Loading point weather…';
  const [thermal, ceiling, airflow, rainfall, waveModel, nativeWave, radarEstimate, marineArchive, nativeHrrr, ...series] = await Promise.all([
    getFrame('/api/grid/', grid), getFrame('/api/cloud/', cloud), getFrame('/api/wind/', wind),
    getFrame('/api/precipitation/', precip),
    getFrame('/api/wave/', wave),
    wave ? get('/api/wave-point?' + new URLSearchParams({ frame: wave.id, lat, lon, ...(componentClock ? {
      at: exactClock(knowledge()), local: state.mode === 'live' ? '1' : '0',
    } : {}) })).catch(() => null) : null,
    getFrame('/api/radar/', radar),
    archivedBuoy ? get('/api/marine/archive?' + new URLSearchParams({ station: archivedBuoy, at: wave.valid_at })).catch(() => null) : null,
    hrrrFrame ? get('/api/hrrr-point?' + new URLSearchParams({
      lat, lon, at: exactClock(knowledge()), valid: hrrrFrame.valid_at, issue: hrrrFrame.issued_at,
      local: state.mode === 'live' ? '1' : '0',
    })).then(data => data.sample).catch(() => null) : null,
    ...horizon.map(frame => getFrame('/api/grid/', frame)),
  ]);
  if (request !== state.probeRequest) return;
  const coordinates = `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? 'E' : 'W'}`;
  const temperature = pointValue(thermal, point);
  const cover = pointValue(ceiling, point);
  const liquid = pointValue(rainfall, point);
  const mapWaveHeight = pointValue(waveModel, point);
  const waveHeight = nativeWave ? nativeWave.height_m : mapWaveHeight;
  const componentHeights = nativeWave?.components && [
    ['Wind sea', nativeWave.components.wind_sea_m], ['Primary swell', nativeWave.components.primary_swell_m],
    ['Secondary swell', nativeWave.components.secondary_swell_m], ['Third swell', nativeWave.components.tertiary_swell_m],
  ].filter(([, height]) => numeric(height)).map(([label, height]) => label + ' ' + number(height, 2) + ' m');
  const archived = marineArchive?.observation;
  const radarLiquid = pointValue(radarEstimate, point);
  const windVector = pointWind(airflow, point);
  const speed = windVector ? Math.hypot(...windVector) * MPH_PER_MPS : null;
  const direction = windVector ? ((Math.atan2(-windVector[0], -windVector[1]) * 180 / Math.PI + 360) % 360) : null;
  const reading = (data, label) => data ? `${link(data.source, label)} 1° · issued ${when(data.issued_at)} UTC · valid ${when(data.valid_at)} UTC` : '';
  panel.innerHTML = `<div class="probe-head"><strong>Point probe · ${coordinates}</strong><div>
    ${hrrrArea ? `<button type="button" id="hrrr-point-control" class="small-button" aria-pressed="${state.hrrrProbe}">HRRR cell</button>` : ''}
    <button type="button" id="clear-probe" class="small-button">Clear</button></div></div>
    <div class="probe-readings"><span><strong>${numeric(temperature) ? `${temperature.toFixed(1)}°F` : '—'}</strong> GFS 2 m temperature</span>
      ${nativeHrrr ? `<span><strong>${temp(nativeHrrr.temp_f, 1)}F</strong> HRRR 2 m temperature</span>` : ''}
      <span><strong>${numeric(cover) ? cover.toFixed(0) + '%' : '—'}</strong> Cloud cover</span>
      ${numeric(speed) ? `<span><strong>${speed.toFixed(1)} mph</strong> 10 m wind ${speed >= 1 ? 'from ' + (Math.round(direction) % 360) + '° true' : 'calm'}</span>` : ''}
      ${numeric(liquid) ? `<span><strong>${liquid.toFixed(2)} mm</strong> 3-hour precipitation forecast</span>` : ''}
      ${radarEstimate ? `<span><strong>${numeric(radarLiquid)
        ? `${radarLiquid === 0 ? '0' : radarLiquid.toFixed(2)} mm` : 'Unavailable'}</strong> 1-hour radar estimate</span>` : ''}
      ${waveModel ? `<span><strong>${numeric(waveHeight) ? `${waveHeight.toFixed(2)} m` : '—'}</strong> Significant wave height forecast</span>` : ''}
      ${nativeWave?.interpolation_corners > 1 && numeric(nativeWave.interpolated_height_m)
        ? `<span><strong>${number(nativeWave.interpolated_height_m, 2)} m</strong> Interpolated between wet cells</span>` : ''}</div>
    <div class="probe-meta">${reading(thermal, 'NOAA GFS temperature') || 'No cached temperature field at this time'}
      ${ceiling ? '<br>' + reading(ceiling, 'NOAA GFS clouds') : ''}
      ${airflow ? '<br>' + reading(airflow, 'NOAA GFS 10 m wind') : ''}
      ${nativeHrrr ? `<br>${link(nativeHrrr.source, 'NOAA HRRR')} · native 3 km cell
        ${number(nativeHrrr.grid_lat, 3)}°, ${number(nativeHrrr.grid_lon, 3)}° · ${number(nativeHrrr.distance_km, 1)} km from probe
        · issued ${when(nativeHrrr.issued_at)} UTC · published ${when(nativeHrrr.available_at)} UTC · valid ${when(nativeHrrr.valid_at)} UTC
        ${state.mode === 'live' ? ` · received ${when(nativeHrrr.ingested_at, 'UTC', 'seconds')}` : ''}`
        : hrrrArea && state.hrrrProbe ? '<br>No native HRRR cell available for this point and hour.' : ''}
      ${rainfall ? `<br>${link(rainfall.source, 'NOAA GFS precipitation')} 1° liquid equivalent
        · issued ${when(rainfall.issued_at)} UTC · published ${when(rainfall.available_at)} UTC
        · accumulated ${when(rainfall.accumulation_starts_at)}–${when(rainfall.valid_at)} UTC` : ''}
      ${waveModel ? `<br>${link(waveModel.source, 'NOAA GFS Wave')} · ${nativeWave && numeric(nativeWave.height_m)
        ? `native 0.25° cell ${number(nativeWave.cell_lat, 2)}°, ${number(nativeWave.cell_lon, 2)}°`
        : 'native wet-cell value unavailable'} · whole-degree map ${numeric(mapWaveHeight) ? number(mapWaveHeight, 2) + ' m' : 'cell missing'}
        · issued ${when(waveModel.issued_at)} UTC · published ${when(waveModel.available_at)} UTC · valid ${when(waveModel.valid_at)} UTC
        ${numeric(waveHeight) ? '' : ' · No ocean-grid value at this point'}` : ''}
      ${componentHeights?.length ? `<br>${esc(componentHeights.join(' · '))} · native 0.25° component heights
        ${state.mode === 'live' ? ` · received ${when(nativeWave.components.ingested_at, 'UTC', 'seconds')}` : ''}` : ''}
      ${radarEstimate ? `<br>${link(radarEstimate.source, 'NOAA MRMS radar')} · mean of native cells in the 0.25° map box
        · accumulated ${when(radarEstimate.accumulation_starts_at)}–${when(radarEstimate.valid_at)} UTC
        · published <time datetime="${esc(radarEstimate.available_at)}">${when(radarEstimate.available_at, 'UTC', 'seconds')}</time>${numeric(radarLiquid) ? '' : ' · no coverage at this point'}` : ''}</div>
    ${marineReading(buoy)}
    ${archived ? `<div class="probe-meta">${link(archived.source, `NDBC buoy ${archived.station}`)} · <strong>${number(archived.height_m, 2)} m</strong>
      measured ${esc(when(archived.measured_at))} UTC · retrospective archive received ${esc(when(archived.archive_received_at))} UTC</div>` : ''}
    ${series.some(Boolean) ? `<ul class="probe-list" aria-label="As-issued point forecast">${series.filter(Boolean).map(frame =>
      `<li>${esc(when(frame.valid_at))} UTC · ${temp(pointValue(frame, point), 1)}F</li>`).join('')}</ul>` : ''}`;
  q('#clear-probe').onclick = () => { state.point = null; globe.clearProbe(); showProbe(); q('#world').focus(); };
  const hrrrControl = q('#hrrr-point-control');
  if (hrrrControl) hrrrControl.onclick = () => { state.hrrrProbe = !state.hrrrProbe; showProbe(); };
}
function showGrid(event) {
  const weather = q('#weather-toggle').checked;
  const clouds = q('#cloud-toggle').checked;
  const wind = q('#wind-toggle').checked;
  const precipitation = q('#precip-toggle').checked;
  const marine = q('#marine-toggle').checked;
  const waves = q('#wave-toggle').checked;
  const radar = q('#radar-toggle').checked;
  const market = q('#markets-toggle').checked;
  q('#thermal-key').hidden = !weather;
  q('#cloud-key').hidden = !clouds;
  q('#wind-key').hidden = !wind;
  q('#precip-key').hidden = !precipitation;
  q('#marine-key').hidden = !marine;
  q('#wave-key').hidden = !waves;
  q('#radar-key').hidden = !radar;
  q('#market-key').hidden = !market;
  globe.layers(weather, market, clouds, wind, precipitation, waves, radar);
  refreshMarine(marine);
  const byEvent = new Map();
  const reason = new Map();
  if (!historicalContext()) {
    for (const row of state.tradeBoard?.ready || []) {
      byEvent.set(row.station + row.day, Math.max(byEvent.get(row.station + row.day) ?? -Infinity, row.order.ev_dollars));
    }
    for (const row of state.tradeBoard?.watch || []) {
      if (!reason.has(row.station + row.day)) reason.set(row.station + row.day, row.blocker);
    }
  }
  const pins = new Map();
  for (const item of state.session?.events || []) {
    if (!item.buckets.length) continue;
    if (state.mode === 'live') {
      if (item.id !== state.eventId && (!known(item.starts_at, knowledge()) || known(item.ends_at, knowledge()))) continue;
    } else if (item.date !== event?.date) continue;
    const edge = byEvent.get(item.station + item.date);
    const prior = pins.get(item.station);
    if (!prior || item.id === state.eventId ||
      (prior.id !== state.eventId && numeric(edge) && (!numeric(prior.edge) || edge > prior.edge))) {
      pins.set(item.station, { ...item, edge,
        reason: historicalContext() ? 'Timeline market evidence' : reason.get(item.station + item.date) || 'No depth at this cursor' });
    }
  }
  globe.stations([...pins.values()], state.eventId, event);

  // Use a valid time no later than the cursor. The publish timestamp gates all leads.
  const frame = weather ? latestFrame(state.session?.grids) : undefined;
  const cloud = clouds ? latestFrame(state.session?.clouds) : undefined;
  const flow = wind ? latestFrame(state.session?.winds) : undefined;
  const precip = precipitation ? precipitationFrame() : undefined;
  const swell = waves ? waveFrame() : undefined;
  const observed = radar ? radarFrame() : undefined;
  loadFrame('grid', frame, '/api/grid/', globe.temperature);
  loadFrame('cloud', cloud, '/api/cloud/', globe.cloudCover);
  loadFrame('wind', flow, '/api/wind/', globe.wind);
  loadFrame('precipitation', precip, '/api/precipitation/', globe.precipitation);
  loadFrame('waves', swell, '/api/wave/', globe.waves);
  loadFrame('radar', observed, '/api/radar/', globe.radar);
  const zone = event?.timezone;
  const primary = observed || swell || precip || frame || cloud || flow;
  q('#grid-issue-label').textContent = observed ? 'Radar published' : 'Grid issue';
  q('#grid-valid-label').textContent = observed ? 'Radar window ends' : 'Grid valid';
  q('#grid-issue').textContent = when(observed ? observed.available_at : primary?.issued_at, zone, observed ? 'seconds' : 'short');
  q('#grid-issue').title = observed?.available_at || '';
  q('#grid-valid').textContent = when(primary?.valid_at, zone);
  const mapLag = state.cursor - stamp(primary?.valid_at);
  const gridAge = q('#grid-age');
  gridAge.hidden = !numeric(mapLag) || mapLag < 3 * HOUR;
  if (!gridAge.hidden) gridAge.textContent = `Showing the last retained field available here, valid ${Math.floor(mapLag / HOUR)} h before the selected time.`;
  q('#cloud-key').title = cloud ?
    `Cloud issue ${when(cloud.issued_at, zone)}, valid ${when(cloud.valid_at, zone)}` : 'Cloud data unavailable at this time';
  q('#globe-source').textContent = radar ? 'NOAA MRMS · RADAR ESTIMATE' : waves ? 'NOAA GFS WAVE · SIGNIFICANT HEIGHT' : state.pivot != null ? 'NOAA GFS · FORECAST AS ISSUED' :
    precipitation ? 'NOAA GFS · 3 H PRECIPITATION' : clouds && !weather ? 'NOAA GFS · TOTAL CLOUD COVER'
      : wind && !weather ? 'NOAA GFS · 10 M WIND' : 'NOAA GFS · 2 M TEMPERATURE';
  q('#wind-key').title = flow ? `Wind issue ${when(flow.issued_at, zone)}, valid ${when(flow.valid_at, zone)}` : 'Wind grid unavailable at this time';
  q('#precip-key').title = precip ? `Forecast ${when(precip.accumulation_starts_at, zone)}–${when(precip.valid_at, zone)}` : 'Precipitation grid unavailable at this time';
  q('#wave-key').title = swell ? `Wave issue ${when(swell.issued_at, zone)}, valid ${when(swell.valid_at, zone)}` : 'Wave grid unavailable at this time';
  q('#radar-key').title = observed ? `Observed ${when(observed.accumulation_starts_at, zone)}–${when(observed.valid_at, zone)}` : 'Radar grid unavailable at this time';
  if (state.point) showProbe();
}
function showTimeline(event, bucket) {
  const zone = event?.timezone;
  const slider = q('#time');
  const enabled = Boolean(state.bounds);
  for (const el of [slider, q('#play'), q('#preview')]) el.disabled = !enabled;
  q('#preview').setAttribute('aria-pressed', String(state.pivot != null));
  const realQuotes = (bucket?.prices || []).filter(row => quoteKnown(row, knowledge()) &&
    (state.mode === 'live' ? row.is_book : numeric(row.yes) || numeric(row.no)));
  const quote = lastBefore(realQuotes, 'at', knowledge());
  q('#quote-kind').textContent = state.mode === 'live' ? 'Latest book receipt' : 'Latest display sample';
  q('#quote-at').textContent = when(quote?.at, zone, state.mode === 'live' ? 'seconds' : 'short');
  q('#quote-at').title = quote?.source_at ? 'Exchange timestamp ' + when(quote.source_at, zone, 'seconds') :
    quote?.available_at ? 'Shown after ' + when(quote.available_at, zone, 'seconds') : '';
  q('#cursor-clock').textContent = enabled ? when(state.cursor, zone, state.mode === 'live' && state.pivot == null ? 'seconds' : 'clock')
    : emptyTitle();
  q('#cursor-date').textContent = enabled ? (state.pivot == null ? when(state.cursor, zone, 'day') :
    `Forecast from ${when(state.pivot, zone)}`) : '';
  if (!enabled) {
    slider.value = 0;
    slider.style.setProperty('--progress', '0%');
    for (const id of ['#start-label', '#mid-label', '#end-label']) q(id).textContent = '';
    return;
  }
  const [start, end] = state.bounds;
  const span = end - start;
  const precision = state.mode === 'live' && span < 10 * MINUTE ? 1000 : MINUTE;
  slider.max = Math.ceil(span / precision);
  slider.value = Math.round((state.cursor - start) / precision);
  slider.dataset.precision = precision;
  slider.style.setProperty('--progress', `${100 * (state.cursor - start) / span}%`);
  slider.setAttribute('aria-valuetext', `${when(state.cursor, zone, 'day')} ${when(state.cursor, zone, 'seconds')}`);
  const detail = state.mode === 'live' && span < 10 * MINUTE ? 'seconds' : 'short';
  q('#start-label').textContent = when(start, zone, detail);
  q('#mid-label').textContent = when(start + span / 2, zone, detail);
  q('#end-label').textContent = when(end, zone, detail);
}

function emptyTitle() {
  if (!state.loadedMode && !q('#notice').dataset.kind) return 'Loading station evidence…';
  if (state.session?.weather_window) return 'Global weather';
  if (state.mode === 'replay') return 'Replay cache required';
  return state.session?.cached ? 'No supported markets captured' : 'Awaiting first capture';
}
function stationReadings(event, at) {
  const ordered = (event.observations || []).filter(row =>
    known(row.available_at, at) && known(row.at, at) && numeric(row.temp_f))
    .sort((a, b) => stamp(a.available_at) - stamp(b.available_at));
  const readings = new Map();
  const metar = row => (row.report || row.raw || '').trim().toUpperCase()
    .replace(/^(?:METAR|SPECI)\s+/, '').replace(/=$/, '').replace(/\s+/g, ' ');
  for (const row of ordered) {
    const key = stamp(row.at), old = readings.get(key);
    const raw = metar(row);
    if (old && raw && row.temp_f === old.temp_f && raw === metar(old)) continue;
    if (!old || old.source !== 'NWS' || row.source === 'NWS') readings.set(key, row);
  }
  return [...readings.values()].sort((a, b) => stamp(a.at) - stamp(b.at));
}

function showWeatherSummary(event, bucket) {
  const control = q('#city-select');
  const choices = new Map();
  for (const item of state.session?.events || []) {
    if (state.mode === 'replay' && event && item.date !== event.date) continue;
    const prior = choices.get(item.station);
    if (!prior || item.id === event?.id || (prior.id !== event?.id && (
      item.date > prior.date && stamp(item.starts_at) <= knowledge() ||
      item.date === prior.date && prior.weather_only && !item.weather_only
    ))) choices.set(item.station, item);
  }
  const options = [...choices.values()].sort((a, b) => stationChoice(a).localeCompare(stationChoice(b)));
  const markup = options.length ? options.map(item =>
    `<option value="${esc(item.id)}">${esc(stationChoice(item))}</option>`).join('') : '<option>No stations in this view</option>';
  if (control.innerHTML !== markup) control.innerHTML = markup;
  control.value = event?.id || '';
  control.disabled = !options.length;
  const linkedStatus = linkedWeatherStatus(event, bucket);
  q('#city-markets').hidden = !!linkedStatus || !event || !!historicalContext();
  const host = q('#weather-current');
  if (linkedStatus) { host.textContent = linkedStatus; return; }
  if (!event) { host.textContent = ''; return; }
  const at = knowledge(), zone = event.timezone;
  const low = event.family === 'daily-low';
  const rain = event.family === 'daily-rain';
  const forecast = lastBefore(event.forecasts, low ? 'low_issued_at' : 'issued_at', at, row =>
    low ? known(row.low_available_at, at) && numeric(row.low_f) :
      known(row.available_at, at) && (rain || numeric(row.high_f)));
  const issue = low ? forecast?.low_issued_at : forecast?.issued_at;
  const available = low ? forecast?.low_available_at : forecast?.available_at;
  const valid = low ? forecast?.low_valid_at : forecast?.valid_at;
  const reading = stationReadings(event, at).at(-1);
  const minutes = reading ? Math.max(0, Math.floor((at - stamp(reading.at)) / MINUTE)) : null;
  const age = minutes == null ? '' : minutes < 60 ? `${minutes} min old` :
    `${Math.floor(minutes / 60)} h ${minutes % 60} min old`;
  const chance = stationDecision(event, at)?.buckets.find(row => row.id === bucket?.id)?.probability;
  const expected = low ? forecast?.low_f : forecast?.high_f;
  const value = rain ? numeric(chance) ? pct(chance, 0) + ' chance of YES' : 'Rain probability unavailable'
    : numeric(expected) ? temp(expected) + 'F' : 'Unavailable';
  const hasRange = numeric(bucket?.lower_f) || numeric(bucket?.upper_f);
  const within = hasRange && expected >= (bucket.lower_f ?? -Infinity) && expected <= (bucket.upper_f ?? Infinity);
  const scenario = !rain && numeric(expected) && hasRange
    ? `An official ${low ? 'low' : 'high'} of ${temp(expected)}F would pay ${within ? 'YES' : 'NO'} on this bucket.` : '';
  const summary = `<p class="weather-identity">${esc(eventDate(event))} · ${bucket ? 'Settlement station' : 'Station'} <strong>${esc(stationName(event))} (${esc(event.station)})</strong>
    ${bucket ? `<br>${esc(VENUES[event.venue || 'polymarket-us'])} · ${esc(bucket.label)} · ${state.selectedSide}` : ''}</p>
    <dl><div><dt>${rain ? 'Rain guidance' : `NOAA forecast ${low ? 'low' : 'high'}`}</dt><dd>${esc(value)}</dd></div>
    <div><dt>Latest observed</dt><dd>${reading ? esc(temp(reading.temp_f, 1) + 'F') : 'Awaiting receipt'}</dd>
    ${reading ? `<small>${esc(when(reading.at, zone, 'clock'))} · ${age}</small>` : ''}</div></dl>
    ${issue && !rain ? `<p class="forecast-clocks">Issued ${esc(when(issue, zone))} · Available ${esc(when(available, zone))}${valid ?
      ' · Valid ' + esc(when(valid, zone)) : ''}</p>` : ''}
    ${scenario ? `<p class="weather-scenario">${esc(scenario)}</p>` : ''}`;
  if (host.innerHTML !== summary) host.innerHTML = summary;
}
function showCoverage(event, at, readings, official) {
  const total = event.coverage;
  if (!total) return '';
  const forecasts = event.forecasts.filter(row => known(row.available_at, at)).length;
  const prices = event.buckets.flatMap(bucket => bucket.prices || []).filter(row => quoteKnown(row, at));
  const payoutCount = event.buckets.filter(bucket => known(bucket.settlement?.available_at || event.settlement?.payout_available_at, at)).length;
  const labels = [
    `${readings.length} readings`, `${forecasts} forecast runs`, `${prices.filter(row => !row.is_book).length} display samples`,
    `${prices.filter(row => row.is_book).length} books`, `${official.length} official reports`, `${payoutCount} payouts`,
  ];
  if (total.unmatched_observations > 0) labels.push(`${total.unmatched_observations} archived receipt gaps`);
  if (total.proxy_observations > 0) labels.push('Some reading times use a receipt proxy');
  return `<div class="coverage-line" aria-label="Evidence available at this time">${esc(labels.join(' · '))}</div>`;
}
function stationResearch(event, at, reading) {
  const dense = lastBefore(event.research_observations, 'at', at, row => known(row.available_at, at));
  const omo = lastBefore(event.madis_airport_temperature?.slice().reverse(), 'at', at, row => known(row.available_at, at));
  const retro = event.retrospective;
  if (!dense && !omo && !retro && !reading) return '';
  const rows = (event.research_observations || []).filter(row => known(row.available_at, at));
  return `<details class="station-extra" data-station-detail="research"><summary>Station readings and receipt clocks</summary>
    ${reading ? `<p>${esc(reading.source)} · ${temp(reading.temp_f, 1)}F
      · measured ${esc(when(reading.at, event.timezone, 'seconds'))}
      · received ${esc(when(reading.available_at, event.timezone, 'seconds'))}
      ${link(reading.receipt_source || reading.source_url, 'Observation source')}</p>` : ''}
    ${dense ? `<p>${esc(dense.source)} · ${rows.length} reports received by the cursor.
      Latest ${temp(dense.temp_f, 1)}F${numeric(dense.dewpoint_f) ? ', dew point ' + temp(dense.dewpoint_f, 1) + 'F' : ''}
      from ${esc(when(dense.at, event.timezone, 'seconds'))},
      received ${esc(when(dense.available_at, event.timezone, 'seconds'))}. Shown separately from modeled readings.</p>` : ''}
    ${omo ? `<p>${link(omo.source, 'NOAA MADIS five-minute airport file')} · ${esc(omo.temp_c_whole)}°C (about ${esc(temp(omo.temp_f, 1))}F)
      · measured ${esc(when(omo.at, event.timezone, 'seconds'))} · NOAA internal receipt ${esc(when(omo.madis_received_at, event.timezone, 'seconds'))}
      · public file ${esc(when(omo.source_at, event.timezone, 'seconds'))} · local receipt ${esc(when(omo.ingested_at, event.timezone, 'seconds'))}.
      Whole-Celsius supplementary observations.</p>` : ''}
    ${retro ? `<p>${link(retro.source, retro.source_name || 'IEM one-minute ASOS')} · ${retro.count} delayed archive rows,
      maximum ${temp(retro.maximum_f, 1)}F. These retrospective readings lack original receipt times.
      The time-linked chart and odds use evidence with causal timing.</p>` : ''}</details>`;
}

function satelliteObservations(event, at) {
  const latest = product => lastBefore(event.satellite_observations, 'valid_at', at, row =>
    row.product === product && known(row[row.availability_basis], at) && at - stamp(row.valid_at) <= 2 * HOUR);
  const clouds = latest('ABI-L2-ACMC');
  const sun = latest('ABI-L2-DSRF');
  if (!clouds && !sun) return '';
  const receipt = row => `scan ended ${esc(when(row.scan_ends_at, event.timezone, 'seconds'))}
    · published ${esc(when(row.available_at, event.timezone, 'seconds'))}
    · local receipt ${esc(when(row.received_at, event.timezone, 'seconds'))}`;
  return `<details class="station-extra" data-station-detail="satellite"><summary>GOES-19 satellite observations</summary>
    ${clouds ? `<p>${link(clouds.source, 'ABI cloud mask')} · ${pct(clouds.point.window_cloud_mask_fraction, 0)} of quality-checked pixels cloudy
      · central pixel ${esc(clouds.point.center_class)} · ${receipt(clouds)}</p>` : ''}
    ${sun ? `<p>${link(sun.source, 'ABI surface shortwave retrieval')} · ${number(sun.point.window_mean_w_m2, 1)} W/m² near the station
      · ${receipt(sun)}</p>` : ''}</details>`;
}

function airportRain(event, at) {
  const readings = [...new Map((event.madis_airport_rain || [])
    .filter(row => known(row.at, at) && known(row.available_at, at)).map(row => [row.at, row])).values()];
  const latest = readings.at(-1);
  if (!latest) return '';
  const largest = readings.reduce((a, b) => a.amount_hundredths_in >= b.amount_hundredths_in ? a : b);
  return `<details class="station-extra" data-station-detail="airport-rain"><summary>Five-minute airport gauge readings</summary>
    <div class="stats">${stat('Latest rolling accumulation', `${number(latest.amount_hundredths_in / 100, 2)} in`)}
      ${stat('Largest reported accumulation', `${number(largest.amount_hundredths_in / 100, 2)} in`)}</div>
    <p>The readings have overlapping accumulation windows. The station’s formal daily total and market payout have their own clocks.</p>
    <p>${link(largest.source, 'Original NOAA MADIS gauge file')} · measured ${esc(when(largest.at, event.timezone, 'seconds'))}
      · internal MADIS receipt ${esc(when(largest.madis_received_at, event.timezone, 'seconds'))}
      · public file ${esc(when(largest.source_at, event.timezone, 'seconds'))}
      · local receipt ${esc(when(largest.ingested_at, event.timezone, 'seconds'))}.</p></details>`;
}

function minimumEvidence(event, at, readings) {
  if (!event.minimum_forecasts?.length && !event.official_low_reports?.length) return '';
  const estimate = lastBefore(event.minimum_forecasts, 'issued_at', at, row => known(row.available_at, at));
  const report = lastBefore(event.official_low_reports || event.official_reports, 'available_at', at,
    row => numeric(row.low_f));
  const low = readings.reduce((best, row) => Math.min(best, row.temp_f), Infinity);
  return `<section class="minimum-evidence" aria-label="Daily-low evidence">
    <h3>Daily-low evidence</h3>
    <div class="stats">${stat('Observed low', temp(low, low % 1 === 0 ? 0 : 1))}
      ${stat('NOAA TXN minimum', temp(estimate?.mean_f))}${stat('Official low', temp(report?.low_f))}</div>
    <p class="contract-line">${estimate ? `${link(estimate.source, 'NOAA guidance')}
      · published ${esc(when(estimate.available_at, event.timezone))}
      · forecast window ${esc(when(estimate.nbm_window_start, event.timezone))}–${esc(when(estimate.nbm_window_end, event.timezone))}`
      : 'No minimum forecast published by this time.'}
      ${report ? `<br>${link(report.source, 'Official low report')} · ${esc(when(report.available_at, event.timezone))}` : ''}</p>
  </section>`;
}

function stationPrecipitation(event, at) {
  const archive = event.mrms_precipitation || [];
  if (!archive.length && event.family !== 'daily-rain') return null;
  const start = stamp(event.climate_starts_at);
  const products = [
    ['RadarOnly_QPE_01H', 'Radar-only estimate', '#317c9c'],
    ['MultiSensor_QPE_01H_Pass1', 'Multisensor estimate', '#6b639f'],
  ];
  const estimates = (archive.length ? products : []).map(([id, label, color]) => {
    const hours = archive.filter(row => row.product === id && known(row.available_at, at)
      && (state.mode !== 'live' || known(row.received_at, at)))
      .sort((a, b) => stamp(a.valid_at) - stamp(b.valid_at));
    const points = [];
    let total = 0, through = start, latest;
    for (const row of hours) {
      const window = stamp(row.accumulation_starts_at);
      if (window < through) continue;
      if (window > through) break;
      if (!points.length) points.push({ at: start, value: 0 });
      through = stamp(row.valid_at);
      total += row.amount_mm;
      latest = row;
      points.push({ at: through, value: total });
    }
    return { label, color, points, total, latest };
  });
  const officialAmount = row => row.precip_in ?? row.precipitation_inches_as_reported ?? row.precip_as_reported;
  const report = lastBefore(event.official_reports, 'available_at', at, row => {
    const amount = officialAmount(row);
    return amount === 'T' || (amount != null && amount !== '' && numeric(Number(amount)));
  });
  const inches = report && officialAmount(report);
  const official = report ? { at: stamp(report.available_at), value: inches === 'T' ? null : Number(inches) * 25.4 } : null;
  return { estimates, official, report, inches };
}

function precipitationEvidence(rain, event) {
  if (!rain) return '';
  const { estimates, official, report, inches } = rain;
  if (!estimates.length && !official) return '';
  return `<figure class="station-chart">
    <figcaption class="chart-head"><h3>Precipitation in the climate day</h3><span>${estimates.length ? 'NOAA MRMS · 0.01°' : 'NWS climate station gauge'}</span></figcaption>
    <canvas class="chart" id="station-precip-chart" role="img" aria-label="Cumulative radar and multisensor estimates by hour, and official station accumulation when released"></canvas>
    <div class="chart-key">${estimates.map(item => chip(item.color, item.latest
      ? `${item.label} ${number(item.total, 1)} mm · ${item.points.length - 1} contiguous hours`
      : `${item.label} pending`)).join('')}
      ${report ? chip('#993f58', inches === 'T' ? 'Official gauge trace' : `Official gauge ${number(official.value, 1)} mm`) : ''}</div>
    <div class="chart-key">${estimates.filter(item => item.latest).map(item =>
      `${link(item.latest.source, item.label)} published ${esc(when(item.latest.available_at, event.timezone))}`).join(' · ')}
      ${report ? ' · ' + link(report.source, 'Official station report') : ''}</div>
  </figure>`;
}

function ensemblePrecipitation(event, at) {
  const forecast = event.rain_ensemble;
  const ensemble = forecast && known(forecast.available_at, at);
  const fine = event.rain_hrrr;
  const shortRange = fine && known(fine.available_at, at);
  if (!ensemble && !shortRange) return '';
  return `<div class="minimum-evidence"><h3>As-issued rainfall forecast</h3>
    <div class="stats">${ensemble ? stat('GEFS members at 0.01 in', `${forecast.members_at_threshold} of ${forecast.members}`)
      + stat('GEFS median', `${number(forecast.median_mm, 2)} mm`) : ''}
      ${shortRange ? stat('HRRR daily liquid', `${number(fine.amount_mm, 2)} mm`) : ''}</div>
    <p class="contract-line">${ensemble ? `${link(forecast.source, 'NOAA GEFS · 0.5° grid')} · published ${esc(when(forecast.available_at, event.timezone))}.` : ''}
      ${shortRange ? ` HRRR 3 km grid · ${number(fine.distance_km, 2)} km from station · published ${esc(when(fine.available_at, event.timezone))}.` : ''}</p></div>`;
}

function lateHighShadow(event, bucket, at) {
  const result = lastBefore(event.late_high_shadow, 'knowledge_at', at);
  const estimate = result?.buckets.find(item => item.id === bucket?.id);
  const witness = result?.context?.temperature_witness;
  if (!result || !estimate || !witness) return '';
  const comparison = '/?' + new URLSearchParams({ view: 'research', kind: 'models', run: result.study_id });
  return `<details class="station-extra" data-station-detail="late-high"><summary>Late high shadow</summary>
    <div class="stats">${stat('Shadow daily high', temp(result.model_high_f, 1))}
      ${stat('Selected bucket YES', pct(estimate.probability, 1))}
      ${stat('METAR high bound', temp(witness.lower_f))}</div>
    <p>Decision ${esc(when(result.knowledge_at, event.timezone, 'seconds'))} · exact station reading received
      ${esc(when(witness.available_at, event.timezone, 'seconds'))} · ${esc(estimate.validation_days)} prior cases.
      ${estimate.uncertain ? 'Limited support for this threshold. ' : ''}The main odds use NOAA.</p>
    <p><a href="${esc(comparison)}">Open the high forecast comparison</a></p></details>`;
}

function lowBoundBasis(estimate) {
  if (!estimate || !Object.hasOwn(estimate, 'metar_implied_side')) return '';
  return estimate.metar_implied_side
    ? `The METAR bound implies ${estimate.metar_implied_side} if the official low stays within it.`
    : 'Both sides remain possible within the METAR bound.';
}

function lowReceiptShadow(event, bucket, scheduled, at) {
  const update = lastBefore(event.low_receipt_shadow, 'knowledge_at', at, row =>
    stamp(row.scheduled_knowledge_at) === stamp(scheduled?.knowledge_at));
  const estimate = update?.buckets.find(item => item.id === bucket?.id);
  if (!estimate) return '';
  const basis = lowBoundBasis(estimate);
  const link = '/?' + new URLSearchParams({ view: 'research', kind: 'models', run: update.qualification_study_id });
  return `<details class="station-extra" data-station-detail="low-receipt"><summary>Low bound from a newly received METAR</summary>
    <div class="stats">${stat('Earlier scheduled bound', temp(update.scheduled_upper_bound_f))}
      ${stat('Bound at receipt', temp(update.metar_upper_bound_f))}${stat('Shadow bucket YES', pct(estimate.probability, 1))}</div>
    <p>The receipt at ${esc(when(update.knowledge_at, event.timezone, 'seconds'))} tightened the physical bound.
      This shadow uses the weather forecast from ${esc(when(update.scheduled_knowledge_at, event.timezone, 'seconds'))}.</p>
    ${basis ? `<p>${esc(basis)}</p>` : ''}
    <p><a href="${esc(link)}">Open the original-receipt comparison</a></p></details>`;
}

function hrrrHighShadow(event, bucket, at) {
  const result = lastBefore(event.hrrr_high_shadow, 'knowledge_at', at);
  const estimate = result?.buckets.find(item => item.id === bucket?.id);
  if (!estimate) return '';
  const research = '/?' + new URLSearchParams({ view: 'research', kind: 'models', run: result.study_id });
  return `<details class="station-extra" data-station-detail="hrrr-high-shadow"><summary>HRRR and NBM high forecast comparison</summary>
    <div class="stats">${stat('NBM remainder YES', pct(estimate.nbm.probability, 1))}
      ${stat('HRRR remainder YES', pct(estimate.hrrr.probability, 1))}${stat('Equal blend YES', pct(estimate.blend.probability, 1))}</div>
    <p>Research probabilities at ${esc(when(result.knowledge_at, event.timezone, 'seconds'))}. Both forecasts use the same
      METAR lower bound of ${esc(result.witness.lower_f)}°F and ${esc(estimate.blend.calibration_days)} earlier dates.
      The climate-report disagreement reserve is ${esc(pct(result.climate_report_disagreement_reserve, 1))}.
      ${estimate.blend.uncertain ? 'This bucket has limited calibration support. ' : ''}</p>
    <p>${link(result.hrrr.source, 'Original HRRR field')} · ${esc(result.hrrr.complete_frames)} remaining hourly frames received by
      ${esc(when(result.hrrr.all_frames_received_at, event.timezone, 'seconds'))}. ${link(result.nbm.source, 'Original NBM forecast')} · received
      ${esc(when(result.nbm.received_at, event.timezone, 'seconds'))}.</p>
    <p><a href="${esc(research)}">Open the frozen HRRR comparison</a></p></details>`;
}

function extendedOutlook(event, at) {
  const outlook = lastBefore(event.extended_outlooks, 'known_at', at);
  const days = outlook?.daily_extrema.filter(day => at < stamp(day.climate_ends_at));
  if (!days?.length) return '';
  return `<details class="station-extra" data-station-detail="nbe-outlook">
    <summary>NOAA extended temperature outlook</summary>
    <p>${link(outlook.source, 'Original NOAA extended bulletin')} · issued ${esc(when(outlook.issued_at, event.timezone, 'seconds'))}
      · published ${esc(when(outlook.source_published_at, event.timezone, 'seconds'))}
      ${state.mode === 'live' ? ` · received ${esc(when(outlook.received_at, event.timezone, 'seconds'))}` : ''}</p>
    ${table(['Station date', 'Low °F', 'High °F'], days.map(day => row([
      day.target_date, day.minimum.temp_f, day.maximum.temp_f,
    ])))}
    <p>The low and high each use their own 18-hour NOAA forecast window.</p></details>`;
}

async function loadRecordedPaper() {
  const id = params.get('paper_study'), index = Number(params.get('paper_case'));
  if (fixture || !id || !/^[a-z][a-z0-9-]{1,159}$/.test(id) || !params.has('paper_case') ||
      !Number.isSafeInteger(index) || index < 0) return;
  try {
    const record = await get('/api/research/strategies/' + encodeURIComponent(id));
    const entry = record.cases?.[index];
    if (record.id === id && entry?.decision && entry.instrument_id) state.recordedPaper = { id, index, entry };
  } catch {}
}

function frozenBook(book, title) {
  if (!book) return '';
  const levels = side => Array.isArray(side) && side.length ? side.slice(0, 3).map(([price, quantity]) =>
    `${price} × ${quantity}`).join(' · ') : Array.isArray(side) ? 'Empty' : 'Unavailable';
  const fields = [['YES bids', book.yes_bids ?? book.bids], ['YES offers', book.yes_offers ?? book.offers],
    ['NO bids', book.no_bids], ['NO offers', book.no_offers]];
  const cache = book.http_cache?.['cf-cache-status'] || book.http_headers?.['cf-cache-status'];
  const lag = imageLag(book.at, book.source_at);
  return `<h3>${esc(title)}</h3><p>Local receipt ${esc(book.at)} · exchange image ${esc(book.source_at || 'unavailable')}
    ${lag ? `(${esc(lag)})` : ''}
    · cache ${esc(cache || 'unreported')} · HTTP age ${book.cache_age_seconds == null ? 'unreported' : esc(book.cache_age_seconds) + ' s'}</p>
    ${table(['Side', 'First three price and size levels'], fields.map(([name, side]) => row([name, levels(side)])))}`;
}

function recordedPaperEvidence(event, bucket, at) {
  const selected = state.recordedPaper, entry = selected?.entry;
  if (!entry || entry.instrument_id !== bucket?.id || entry.station !== event.station || entry.date !== event.date ||
      entry.venue !== (event.venue || 'polymarket-us') || !known(entry.at, at)) return '';
  const decision = entry.decision, [signal, check] = entry.books || [];
  const recheckAt = entry.reaction_at || decision.reaction_at;
  const recheck = known(recheckAt, at);
  const position = recheck ? entry.position : null;
  const destination = recheck ? entry.at : recheckAt;
  const href = '/?' + new URLSearchParams({ view: 'research', kind: 'strategies', run: selected.id, case: selected.index });
  return `<section class="minimum-evidence" aria-label="Frozen paper decision">
    <h3>Frozen paper decision</h3><p>${esc(entry.title)} · forecast known ${esc(decision.forecast_available_at || 'unavailable')}</p>
    <div class="stats">${stat(`Forecast ${entry.side} at signal`, pct(decision.probability, 1))}
      ${stat('Recheck due UTC', decision.reaction_due_at || '—')}
      ${position ? stat('All-in paper debit', money(position.max_loss)) :
        recheck ? stat('Recheck result', String(decision.result || 'unavailable').replaceAll('_', ' ')) : ''}</div>
    ${frozenBook(known(signal?.at, at) ? signal : null, 'Signal book')}
    ${frozenBook(recheck && known(check?.at, at) ? check : null, 'Delayed recheck book')}
    ${position ? `<p>${esc(number(position.quantity))} shares at ${esc(number(position.entry_price * 100, 3))}¢ weighted entry
      · ${esc(money(position.notional))} before fees · ${esc(money(position.fee))} fee ·
      ${esc(money(position.max_loss))} total.</p>` : ''}
    <div class="record-links">${destination && numeric(stamp(destination)) ? `<button class="small-button" type="button"
      data-paper-clock="${esc(destination)}">${recheck ? 'Jump to signal' : 'Jump to recorded recheck'}</button>` : ''}
      <a href="${esc(href)}">Open frozen paper decision</a></div></section>`;
}

function showStation(event, bucket) {
  const host = q('#station-content');
  const linkedStatus = linkedWeatherStatus(event, bucket);
  if (linkedStatus) {
    host.innerHTML = `<div class="station-empty"><h2 id="station-title" tabindex="-1">Station evidence</h2><p>${linkedStatus}</p></div>`;
    return;
  }
  if (!event) {
    const title = emptyTitle();
    const detail = state.session?.weather_window ? 'Choose a weather layer, then select a point or move the timeline in UTC.'
      : state.mode === 'live' ? 'Public collection runs in the background. Open collection status for source updates.'
      : 'Run the archive backfill command for the selected date.';
    host.innerHTML = `<div class="station-empty"><div class="eyebrow">STATION</div><h2 id="station-title">${title}</h2><p>${detail}</p></div>`;
    return;
  }
  const at = knowledge();
  const lowTarget = event.family === 'daily-low';
  const rainTarget = event.family === 'daily-rain';
  const target = lowTarget ? 'low' : rainTarget ? 'rain' : 'high';
  const reportField = lowTarget ? 'low_f' : 'high_f';
  const readings = stationReadings(event, at);
  const latestObs = readings.at(-1);
  const observed = readings.reduce((best, row) =>
    lowTarget ? Math.min(best, row.temp_f) : Math.max(best, row.temp_f), lowTarget ? Infinity : -Infinity);
  const forecast = lastBefore(event.forecasts, 'issued_at', at, row => known(row.available_at, at));
  const eligibleGrid = (event.hrrr_temperature || []).filter(row =>
    known(row.available_at, at) && (state.mode !== 'live' || known(row.ingested_at, at)));
  const gridIssues = [...new Set(eligibleGrid.map(row => row.issued_at))].reverse();
  const gridIssue = gridIssues.includes(state.hrrrIssue) ? state.hrrrIssue : gridIssues[0];
  const localGrid = eligibleGrid.filter(row => row.issued_at === gridIssue);
  const rain = stationPrecipitation(event, at);
  const dailyForecast = lastBefore(event.forecasts, 'issued_at', at, row =>
    known(row.available_at, at) && row.high_source === 'noaa-nbm-nbs-txn');
  const decision = stationDecision(event, at);
  const historical = !!historicalContext();
  const decisionKey = decisionQuery(event, at);
  const available = state.historicalDecision?.key === decisionKey && state.historicalDecision;
  const actualLowForecast = lastBefore(event.forecasts, 'low_issued_at', at,
    row => row.low_availability_basis === 'local_receipt' && known(row.low_available_at, at));
  const modelBucket = decision?.buckets.find(item => item.id === bucket?.id);
  const boundBasis = lowBoundBasis(modelBucket);
  const quote = lastBefore(bucket?.prices, 'at', at, row => quoteKnown(row, at) && (numeric(row.yes) || numeric(row.no)));
  const closed = !event.weather_only && known(contractClose(event, bucket), at);
  const displayAge = state.mode === 'live' ? event.venue === 'kalshi' ? 60_000 : 30_000
    : event.venue === 'kalshi' ? 90 * MINUTE : 90_000;
  const displayNow = quote && !closed && at - stamp(quote.available_at || quote.at) <= displayAge;
  const settlement = event.settlement;
  const reports = (event.official_reports?.length ? event.official_reports : settlement?.available_at ? [settlement] : [])
    .filter(item => known(item.available_at, at) && numeric(item[reportField]));
  const official = lastBefore(reports, 'available_at', at);
  const payoutClock = bucket?.settlement?.available_at || settlement?.payout_available_at;
  const payoutKnown = known(payoutClock, at);
  const livePayout = payoutKnown ? bucket?.settlement?.yes_payout : null;
  const winning = payoutKnown && bucket?.id === settlement?.winning_bucket;
  const payout = livePayout != null && numeric(Number(livePayout)) ? Number(livePayout)
    : payoutKnown && bucket ? (winning ? 1 : 0) : null;
  const focus = host.contains(document.activeElement) ? document.activeElement.dataset.bucket : '';
  const oldSelect = host.contains(document.activeElement) && document.activeElement.id === 'station-select';
  const issueFocused = host.contains(document.activeElement) && document.activeElement.id === 'station-hrrr-issue';
  const openDetails = new Set([...host.querySelectorAll('details[open]')].map(node => node.dataset.stationDetail));
  if (!host.querySelector('[data-station-detail]') && params.get('from_trade') === '1') {
    openDetails.add('rules');
    openDetails.add('research');
  }
  const modePrice = event.price_basis || (state.mode === 'live' ? 'YES book midpoint' : 'YES display price');
  const site = event.market_url || `https://polymarket.us/event/${encodeURIComponent(event.id)}`;
  const officialLabel = rainTarget ? rain?.report : official;
  const settlementSource = officialLabel ? link(officialLabel.source, 'NWS report') : '';
  const exchangeSource = payoutKnown ? link(bucket?.settlement?.source || settlement?.payout_report_source || event.market_source, 'Payout record') : '';
  const detailsLink = [link(event.rules_source, 'Exchange rules'), settlementSource, exchangeSource].filter(Boolean).join(' · ');
  const support = modelBucket
    ? `${modelBucket.uncertain ? 'Limited support · ' : ''}${modelBucket.validation_days} calibration dates`
    : decisionKey && !available ? 'Checking model evidence…'
    : available?.error ? 'Could not read model evidence at this clock'
    : historical ? 'Model availability unverified at this clock' : 'Model probability unavailable';
  const method = (METHODS[decision?.method] || 'Model') + (decision?.mode === 'shadow' ? ' shadow' : '');
  const dense = (event.research_observations || []).some(row => known(row.available_at, at));
  const omo = (event.madis_airport_temperature || []).some(row => known(row.available_at, at));
  const selectedRow = state.paper?.rows?.find(row => row.bucket_id === bucket?.id && row.side === (state.selectedSide || 'YES'));
  const quoteBasis = selectedRow ? `${selectedRow.side} · ${selectedRow.status} · ${signed(selectedRow.ev_dollars)} net EV` : '';
  const quoteLag = imageLag(selectedRow?.quote_at, selectedRow?.quote_source_at);
  const entry = selectedRow && numeric(selectedRow.entry_price) ? `${number(selectedRow.entry_price * 100, 1)}¢ weighted entry ·
    ${number(selectedRow.quantity)} shares · ${money(selectedRow.fee)} fee` : '';
  const previousQuote = quote && lastBefore(bucket?.prices, 'at', stamp(quote.at) - 1,
    row => quoteKnown(row, at) && numeric(row.yes) && row.yes !== quote.yes);
  const schedule = event.settles_at ? `Scheduled ${when(event.settles_at, event.timezone)}` : '';
  const standardZone = `Etc/GMT+${-event.standard_utc_offset_hours}`;
  const climateWindow = `${when(event.climate_starts_at || event.starts_at, standardZone)}–${when(event.climate_ends_at || event.ends_at, standardZone)}`;
  const contract = bucket?.description || bucket?.rules;
  const archiveMarket = event.venue && bucket ? '/?' + new URLSearchParams({
    view: 'markets', venue: event.venue, instrument: bucket.id,
  }) : '';
  const archivePayout = bucket?.archived_payout;
  const bucketLabel = item => `${item.label}${rainTarget ? '' : '°'}`;
  const rainAmount = rain?.inches === 'T' ? 'Trace' : `${number(Number(rain?.inches), 2)} in`;
  const officialDisplay = officialLabel ? `${rainTarget ? rainAmount : temp(official[reportField]) + 'F'} · ${when(officialLabel.available_at, event.timezone)}`
    : schedule || 'Awaiting report';

  host.innerHTML = `
    <div class="station-top">
      <div><div class="eyebrow">${esc(event.station)} · DAILY ${target.toUpperCase()}</div>
      <h2 id="station-title" tabindex="-1">${esc(event.station_name || event.station)}</h2>
      <p>${esc(eventDate(event))} · ${event.weather_only ? 'Weather only' : 'Daily-' + target + ' contract'}</p></div>
      ${archiveMarket ? `<a href="${esc(archiveMarket)}">Contract archive</a>` : event.weather_only ? '' : link(site, 'Market')}
    </div>
    ${state.session.events.length > 1 ? `<select id="station-select" class="station-select" aria-label="Select station and market date">${state.session.events.map(item =>
      `<option value="${esc(item.id)}" ${item.id === event.id ? 'selected' : ''}>${esc(item.station_name || item.station)} · ${esc(eventDate(item))}
        ${item.weather_only ? ' · Weather only' : item.family === 'daily-low' ? ' · Daily low'
          : item.family === 'daily-rain' ? ' · Daily rain' : ' · Daily high'}</option>`).join('')}</select>` : ''}
    ${showCoverage(event, at, readings, reports)}
    ${event.buckets.length ? `<div class="bucket-picker" role="group" aria-label="${rainTarget ? 'Rain threshold' : 'Temperature bucket'}">${event.buckets.map(item =>
      `<button type="button" data-bucket="${esc(item.id)}" aria-pressed="${item.id === bucket?.id}"
        class="${known(item.settlement?.available_at || settlement?.payout_available_at, at) &&
          (Number(item.settlement?.yes_payout) === 1 || item.result) ? 'winner' : ''}">${esc(bucketLabel(item))}</button>`).join('')}</div>` : ''}
    ${bucket ? `<div class="station-trade-link">
      <a class="small-button" data-open-ticket href="${esc(tradeUrl(event, bucket.id, state.selectedSide))}">Review ${state.selectedSide} ticket →</a>
      <span>${esc(VENUES[event.venue || 'polymarket-us'])} · ${esc(bucketLabel(bucket))}</span>
    </div>` : ''}
    ${rainTarget ? ensemblePrecipitation(event, at) + precipitationEvidence(rain, event) : `<div class="stats">
      ${stat('Observed ' + target, temp(observed, observed % 1 === 0 ? 0 : 1))}
      ${stat('NOAA daily ' + target, temp(lowTarget ? actualLowForecast?.low_f ?? decision?.forecast?.mean_f : dailyForecast?.high_f))}
      ${event.weather_only && numeric(official?.low_f) && numeric(official?.high_f)
        ? stat('Official low / high', `${temp(official.low_f)}F / ${temp(official.high_f)}F`)
        : lowTarget ? stat('Shadow daily low', temp(decision?.model_low_f, 1))
        : stat(decision?.method === 'noaa' ? 'Official high' : `${method} daily high`,
          decision?.method === 'noaa' ? temp(official?.high_f) : temp(decision?.model_high_f, 1))}
    </div>`}
    ${rainTarget && decision?.target === 'daily-rain' ? `<section class="minimum-evidence" aria-label="Rain shadow guidance">
      <h3>Rain shadow guidance</h3>
      <div class="stats">${stat('Measurable rain', pct(modelBucket?.probability, 1))}
        ${stat('Earlier outcome interval', `${pct(modelBucket?.yes_lower, 1)}–${pct(modelBucket?.yes_upper, 1)}`)}
        ${stat('Remaining hours guidance', pct(decision.remaining_forecast?.p06_overlap_probability, 1))}</div>
      <p class="contract-line">Full-day probability uses the same pre-day NOAA cycle. Local evidence through ${esc(when(decision.knowledge_at, event.timezone))}.
        ${decision.gauge_witness ? `Measured precipitation evidence received ${esc(when(decision.gauge_witness.available_at, event.timezone))}.` : 'Waiting for qualifying measured precipitation.'} Research only.</p></section>` : ''}
    <figure class="station-chart">
      <figcaption class="chart-head"><h3>Temperature</h3><span>${latestObs ? `Latest ${esc(temp(latestObs.temp_f, 1))} · ${esc(when(latestObs.at, event.timezone, 'clock'))}` : 'Waiting for a station report'}</span></figcaption>
      <canvas class="chart" id="temp-chart" role="img" aria-label="Station temperatures, as-issued hourly forecast band, and selected settlement bucket"></canvas>
      <div class="chart-key">${chip('#24776a', 'Readings & receipt ticks')}${chip('#346aa2', 'NBM forecast band')}
        ${localGrid.length ? chip('#855785', 'HRRR 3 km forecast') : ''}
        ${dense ? chip('#8172a4', 'Supplementary reports') : ''}${omo ? chip('#837247', 'MADIS five-minute temperature') : ''}
        ${bucket && !rainTarget ? chip('#d8b874', 'YES range') : ''}
        ${official ? chip('#993f58', 'Official report') : ''}</div>
      <div class="chart-key">${forecast ? `Issued ${esc(when(forecast.issued_at, event.timezone))} · Available ${esc(when(forecast.available_at, event.timezone))}` : 'No forecast available at this time'}
      ${!lowTarget && dailyForecast ? ` · TXN valid ${esc(when(dailyForecast.valid_at, event.timezone))}` : ''}</div>
      ${localGrid.length ? `<div class="chart-key">${link(localGrid[0].source, 'NOAA HRRR')}
        · ${esc(when(gridIssue, 'UTC'))} UTC issue · grid cell ${esc(number(localGrid[0].distance_km, 1))} km from station
        · ${localGrid.length} published hourly forecasts</div>` : ''}
      ${gridIssues.length > 1 ? `<label class="filter-label hrrr-issue-picker">Displayed HRRR run <select id="station-hrrr-issue">
        <option value="">Latest known</option>${gridIssues.map(issue => `<option value="${esc(issue)}"
          ${issue === state.hrrrIssue ? ' selected' : ''}>${esc(when(issue, 'UTC'))} UTC</option>`).join('')}</select></label>` : ''}
      ${decision && !lowTarget ? `<div class="chart-key">${esc(method)} daily high 10th–90th percentile ${esc(temp(decision.q10_f, 1))}–${esc(temp(decision.q90_f, 1))}</div>` : ''}
      ${lowTarget && decision ? `<div class="chart-key">Shadow forecast trained on earlier dates · decision ${esc(when(decision.knowledge_at, event.timezone))}
        · NOAA receipt ${esc(when(decision.available_at, event.timezone))}</div>` : ''}
      ${numeric(decision?.metar_upper_bound_f) ? `<div class="chart-key">${esc(method)} · compatible METAR minimum at or below
        ${esc(temp(decision.metar_upper_bound_f))}F · exception allowance
        ${esc(pct(decision.report_exception_allowance?.uncapped_weight, 1))}
        ${decision.metar_evidence?.raw_report?.trim().endsWith('$') ? ' · METAR maintenance check marker $' : ''}</div>` : ''}
    </figure>
    ${bucket ? `<figure class="station-chart">
      <figcaption class="chart-head"><h3>YES odds</h3><span>${esc(bucketLabel(bucket))}</span></figcaption>
      <canvas class="chart" id="odds-chart" role="img" aria-label="Modeled probability and market YES price on the same time axis"></canvas>
      <div class="chart-key">${chip('#24776a', `${closed ? 'Last ' : ''}${method} ${pct(modelBucket?.probability, 1)}`)}
        ${chip('#a66b36', `${modePrice} ${displayNow ? pct(quote.yes, 1) : '—'}`)}
        ${state.mode === 'replay' && displayNow ? chip('#a66b36', `NO display ${pct(quote.no, 1)}`) : ''}
        ${payoutKnown ? chip('#993f58', 'Payout receipt') : ''}</div>
      <div class="chart-key">${esc(support)}${state.pivot != null ? ' · Odds frozen at forecast origin' : ''}${closed ? ' · Market closed' : ''}</div>
      ${decision?.model_availability ? `<details class="station-extra" data-station-detail="model-evidence"><summary>Model evidence</summary>
        <p>Artifact <code>${esc(decision.vintage)}</code><br>Availability ${esc(decision.model_availability.available_at)}
          · ${esc(decision.model_availability.basis)}<br>Receipt <code>${esc(decision.model_availability.receipt_sha256)}</code>
          <br>Training evidence cutoff ${esc(decision.model_evidence_cutoff)}
          <br>Forecast issued ${esc(decision.issued_at)} · available ${esc(decision.available_at)}</p></details>` : ''}
      ${closed && bucket.native_trading_closed_at ? `<div class="chart-key">Observed native close ${esc(when(bucket.native_trading_closed_at, 'UTC', 'seconds'))}</div>` : ''}
      ${boundBasis ? `<div class="chart-key">${esc(boundBasis)}</div>` : ''}
      ${state.mode === 'live' && event.venue === 'kalshi' && quote ? `<div class="chart-key">
        Local book receipt ${esc(when(quote.at, event.timezone, 'seconds'))} · exchange book clock ${esc(quote.source_at || 'unavailable')}
        · HTTP age ${quote.cache_age_seconds == null ? 'unknown' : esc(quote.cache_age_seconds) + ' s'} · display evidence</div>` : ''}
      ${quoteBasis ? `<div class="chart-key">${esc(quoteBasis)} · ${esc(when(selectedRow.quote_at, event.timezone, 'seconds'))}
        ${quoteLag ? ` · exchange image ${esc(quoteLag)}` : ''}${entry ? ' · ' + esc(entry) : ''}</div>` : ''}
      ${previousQuote && numeric(quote.yes) ? `<div class="chart-key">Last displayed YES move ${pct(previousQuote.yes, 1)} → ${pct(quote.yes, 1)}
        at ${esc(when(quote.at, event.timezone, 'seconds'))}</div>` : ''}
    </figure>` : ''}
    <p class="contract-line">Station climate window ${esc(climateWindow)} local standard time.${detailsLink ? ' ' + detailsLink : ''}
      ${event.reporting_source ? `<br>Venue resolution source ${esc(event.reporting_source)}.
        ${event.replay_market_window_exact ? 'Exact reporting window matched.' : 'Venue reporting window still unverified.'}` : ''}</p>
    <div class="settlement ${officialLabel || numeric(payout) ? 'released' : ''}">
      <div><span>${officialLabel ? 'NWS official ' + target : `Official ${target} pending`}</span><strong>${esc(officialDisplay)}</strong></div>
      ${bucket ? `<div><span>${numeric(payout) ? 'Verified payout per share' : 'Payout'}</span><strong>${numeric(payout) ? `YES ${money(payout)} · NO ${money(1 - payout)}` : 'Pending'}</strong></div>` : ''}
    </div>
    ${reports.length > 1 ? `<details class="station-extra" data-station-detail="climate-editions"><summary>Published climate report editions</summary>
      ${reports.map(report => `<p>${esc(when(report.available_at, event.timezone, 'seconds'))} · ${reportField === 'low_f' ? 'Low' : 'High'}
        ${esc(temp(report[reportField]))}F · ${link(report.source, report.product || 'NWS report')}</p>`).join('')}</details>` : ''}
    ${archivePayout && closed && !payoutKnown && !historical && known(archivePayout.archive_received_at, at)
      ? `<details class="station-extra" data-station-detail="payout"><summary>Archive payout evidence</summary>
        <p>YES ${esc(money(archivePayout.yes_payout))}. Locally archived ${esc(when(archivePayout.archive_received_at, event.timezone))}.
        ${link(archivePayout.source, 'Public payout record')}</p></details>` : ''}
    ${contract ? `<details class="station-extra" data-station-detail="rules"><summary>Selected contract rules and identifiers</summary>
      <p>${esc(contract)}</p>${bucket.rules_secondary ? `<p>${esc(bucket.rules_secondary)}</p>` : ''}<p>${esc(bucket.id)} · Exchange market ${esc(bucket.market_id)} · YES ${esc(bucket.yes_side_id)} · NO ${esc(bucket.no_side_id)}</p>
      ${link(event.market_source || event.source, 'Public exchange listing')}</details>` : ''}
    ${rainTarget ? airportRain(event, at) : precipitationEvidence(rain, event)}${lowTarget || rainTarget ? '' : minimumEvidence(event, at, readings)}
    ${recordedPaperEvidence(event, bucket, at)}
    ${extendedOutlook(event, at)}${historical ? '' : lateHighShadow(event, bucket, at) + hrrrHighShadow(event, bucket, at)}
    ${historical ? '' : lowReceiptShadow(event, bucket, decision, at)}
    ${satelliteObservations(event, at)}${stationResearch(event, at, latestObs)}`;
  q('#station-select')?.addEventListener('change', e => chooseEvent(e.target.value));
  q('#station-hrrr-issue')?.addEventListener('change', event => {
    state.hrrrIssue = event.target.value;
    showStation(currentEvent(), currentBucket(currentEvent()));
  });
  for (const button of host.querySelectorAll('[data-bucket]')) {
    button.addEventListener('click', () => {
      state.bucketId = button.dataset.bucket;
      state.selectedSide = 'YES';
      state.preferTrade = false;
      retainWeatherMarket(event);
      render();
      selectWeatherBucket();
    });
  }
  host.querySelector('[data-paper-clock]')?.addEventListener('click', event => {
    const clock = event.currentTarget.dataset.paperClock;
    seek(stamp(clock), clock);
  });
  for (const node of host.querySelectorAll('[data-station-detail]')) node.open = openDetails.has(node.dataset.stationDetail);
  if (focus) [...host.querySelectorAll('[data-bucket]')].find(node => node.dataset.bucket === focus)?.focus({ preventScroll: true });
  if (oldSelect) q('#station-select')?.focus({ preventScroll: true });
  if (issueFocused) q('#station-hrrr-issue')?.focus({ preventScroll: true });
  const chartEvent = !historical && event.live_shadow_decisions ? { ...event, decisions: event.live_shadow_decisions } : event;
  charts({ event: chartEvent, bucket, readings, reports, exactDecision: decision, payoutClock, cursor: state.cursor, knowledge: at, forecast, localGrid,
    evidenceKnown: clock => known(clock, at), reportField, zone: event.timezone,
    projected: state.pivot != null, capture: state.mode === 'live', onSeek: seek });
  if (rain) precipitationChart({ event, ...rain, cursor: state.cursor, onSeek: seek });
}

function marketRows(event) {
  const at = knowledge();
  const output = [];
  const decision = stationDecision(event, at);
  for (const bucket of event?.buckets || []) {
    const model = decision?.buckets.find(item => item.id === bucket.id);
    const quote = lastBefore(bucket.prices, 'at', at, row => quoteKnown(row, at));
    const fresh = quote && at - stamp(quote.available_at || quote.at) <= (event.venue === 'kalshi' ? 90 * MINUTE : 90_000);
    for (const side of ['YES', 'NO']) {
      output.push({
        event_id: event.id, bucket_id: bucket.id, station: event.station, date: event.date, label: bucket.label, side,
        rain_threshold: event.family === 'daily-rain',
        venue: event.venue || 'polymarket-us',
        quote_at: quote?.at, price: fresh ? quote[side.toLowerCase()] : null, settles_at: event.settles_at,
        status: known(contractClose(event, bucket), at) ? 'Closed · display only' : 'Display prices only',
        probability: !model ? null : side === 'YES' ? model.probability : 1 - model.probability,
        uncertain: model?.uncertain,
      });
    }
  }
  return output;
}
function moneyClass(value) { return value > 0 ? 'good' : value < 0 ? 'bad' : ''; }
function paperNumber(selector) {
  const element = q(selector);
  return Math.min(Number(element.max), Math.max(Number(element.min), Number(element.value) || Number(element.defaultValue)));
}
function paperKey() {
  return `${fixture}|${exactClock(knowledge())}|${paperNumber('#order-size')}|${paperNumber('#reaction')}`;
}
function table(header, rows) {
  return `<div class="table-wrap"><table><thead><tr>${header.map(label => `<th scope="col">${esc(label)}</th>`).join('')}</tr></thead><tbody>
    ${rows.join('')}</tbody></table></div>`;
}
function row(cells, leading = '') {
  return `<tr>${leading}${cells.map(item => Array.isArray(item) ? `<td class="${item[1]}">${esc(item[0])}</td>` : `<td>${esc(item)}</td>`).join('')}</tr>`;
}
function captureLog(issues, open) {
  return issues.length ? `<details id="capture-issues" ${open ? 'open' : ''}><summary>Capture log (${issues.length})</summary><ul>
    ${issues.map(issue => `<li>${esc(typeof issue === 'string' ? issue : JSON.stringify(issue))}</li>`).join('')}</ul></details>` : '';
}
function tradeUrl(event, instrument, side) {
  const query = new URLSearchParams({ view: 'trade', venue: event.venue || 'polymarket-us', instrument, side });
  if (fixture) query.set('fixture', '1');
  const clock = historicalContext();
  if (clock) {
    for (const [key, value] of Object.entries(clock)) query.set(key, value);
    query.set('mode', state.mode);
  }
  return '/?' + query;
}
function levels(item) {
  if (!item.fills?.length) return '<td>—</td>';
  const summary = `${item.fills.length} used ${item.fills.length === 1 ? 'level' : 'levels'}`;
  return `<td class="book-levels"><details><summary>${summary}</summary><ul>${item.fills.map(([price, size]) =>
    `<li>${esc(number(size))} shares at ${esc(number(price * 100, 1))}¢</li>`).join('')}</ul></details></td>`;
}
function showOpportunities(event) {
  const host = q('#opportunity-content');
  const live = state.mode === 'live';
  const mode = q('#opportunity-filter').value;
  const source = live ? (state.paper?.rows || []) : (state.session?.events || []).flatMap(marketRows);
  const venues = [...new Set(source.map(row => row.venue || 'polymarket-us'))];
  q('#opportunity-venues').textContent = (venues.length ? venues : ['polymarket-us'])
    .map(venue => (VENUES[venue] || venue).toUpperCase()).join(' · ');
  const entries = source.filter(item => {
    if (mode === 'selected') return item.event_id === event?.id;
    if (mode === 'positive') return !item.uncertain && !item.paper_evidence_gap && numeric(item.ev_dollars) && item.ev_dollars > 0;
    return true;
  });
  if (live) entries.sort((a, b) => Number(!!a.paper_evidence_gap) - Number(!!b.paper_evidence_gap)
    || Number(!!a.uncertain) - Number(!!b.uncertain)
    || (b.ev_dollars ?? -Infinity) - (a.ev_dollars ?? -Infinity) || a.station.localeCompare(b.station));
  const limit = state.opportunityLimit || 18;
  const zoneByEvent = new Map((state.session?.events || []).map(item => [item.id, item.timezone]));
  const header = ['Station / bucket', ...(venues.length > 1 ? ['Venue'] : []), 'Side', 'Model odds', ...(live
    ? ['Break-even', 'Available', 'Used shares', 'Avg price / posted offer', 'Fee', 'Net EV', 'Max loss', 'HTTP age', 'Book receipt', 'Depth used']
    : ['Display price', 'Display sample']), 'Est. settlement', 'Evidence'];
  const rows = entries.slice(0, limit).map(item => {
    const zone = zoneByEvent.get(item.event_id);
    const label = `${item.station} · ${item.date} · ${item.label}${item.rain_threshold ? '' : '°'}`;
    const contractEvent = state.session?.events.find(event => event.id === item.event_id);
    const leading = contractEvent ? `<td><a class="opportunity-link" data-open-ticket
      href="${esc(tradeUrl(contractEvent, item.bucket_id, item.side))}">${esc(label)}</a></td>` : `<td>${esc(label)}</td>`;
    const moneyCells = live ? [
      [pct(item.break_even_probability, 1), 'num'], [number(item.available_size, 2), 'num'],
      [number(item.quantity, 2), 'num'], [numeric(item.entry_price) ? number(item.entry_price * 100, 1) + '¢' :
        numeric(item.display_price) ? number(item.display_price * 100, 1) + '¢ posted' : '—', 'num'],
      [money(item.fee), 'num'], [signed(item.ev_dollars), `num ${moneyClass(item.ev_dollars)}`],
      [money(item.max_loss), 'num'], [numeric(item.response_cache_age)
        ? number(item.response_cache_age, 1) + 's' : item.quote_at ? 'Unknown' : '—', 'num'],
      when(item.quote_at, zone, 'seconds'),
    ] : [[pct(item.price, 1), 'num'], when(item.quote_at, zone, 'seconds')];
    const venueCells = venues.length > 1 ? [VENUES[item.venue] || item.venue || VENUES['polymarket-us']] : [];
    const base = row([...venueCells, item.side, [`${pct(item.probability, 1)}${item.uncertain && numeric(item.probability) ? ' †' : ''}`, 'num'], ...moneyCells]);
    const lag = live ? imageLag(item.quote_at, item.quote_source_at) : '';
    return base.replace('<tr>', '<tr>' + leading).replace('</tr>', (live ? levels(item) : '') +
      `<td>${esc(when(item.settles_at, zone))}</td><td>${esc([
        item.status, item.response_cache_status, lag && `Exchange image ${lag}`, item.paper_evidence_gap !== item.status && item.paper_evidence_gap,
      ].filter(Boolean).join(' · '))}</td></tr>`);
  });
  const message = state.pivot != null && live
    ? 'Scenario priced from books at the forecast origin. The forward clock adds no new quotes.'
    : live ? (fixture ? 'Synthetic books use fixture freshness. † Limited calibration support.' :
      'Paper eligibility uses origin-confirmed books and their first app availability. † Limited calibration support.')
      : 'Archived display prices can be compared with model odds. They supply no executable size.';
  const empty = live && !state.paper && event ? 'Loading captured depth…'
    : mode === 'positive' ? 'No supported positive-EV candidate at this time.' : 'No matching market evidence at this time.';
  host.innerHTML = `<p class="paper-message">${esc(message)} ${entries.length ? esc(`Showing ${Math.min(limit, entries.length)} of ${entries.length} sides.`) : ''}</p>
    ${rows.length ? table(header, rows) : `<p class="paper-message">${esc(empty)}</p>`}
    ${entries.length > limit ? '<div class="paper-message"><button type="button" id="more-opportunities" class="small-button">Show more sides</button></div>' : ''}`;
  q('#more-opportunities')?.addEventListener('click', () => {
    state.opportunityLimit = limit + 18;
    showOpportunities(event);
  });
}
function trialDescription(trial) {
  if (!trial) return '';
  const { coverage } = trial;
  return `${trial.signals} signal observations across ${trial.distinct_signal_sides} market sides.
    ${trial.fills} paper ${trial.fills === 1 ? 'fill' : 'fills'}, ${trial.settled} settled.
    Rechecks use the first eligible book at least ${trial.reaction_seconds}s after the signal.
    ${trial.profitable_station_days} profitable of ${trial.settled_station_days} fully settled station-days.`
    + (coverage?.from ? ` ${coverage.book_receipts} receipts across ${coverage.station_days} station-days,
    ${coverage.eligible_books} eligible and ${coverage.modeled_books} with model odds.
    Coverage ${when(coverage.from)}–${when(coverage.through)} UTC.` : ' This trial has no recorded books.');
}
function showPaper() {
  const host = q('#paper-content');
  const issues = state.session?.issues || [];
  const open = new Set([...host.querySelectorAll('details[open]')].map(item => item.id));
  const live = state.mode === 'live';
  const trial = state.saved?.result || state.paper?.trial;
  q('#save-run').disabled = !live || state.pivot != null || !state.paper?.rows?.some(row => row.quote_at);
  q('#follow-payouts').hidden = !state.saved?.result?.positions.some(position => !position.settled);
  const noteText = trial ? trialDescription(trial) : live
    ? state.paper?.trialReady ? 'No captured market books are available for a trial.' : 'Loading the paper ledger from recorded depth…'
    : 'Display-price history produces zero simulated fills.';
  const reasons = trial?.attempt_results || (trial?.attempts || []).reduce((counts, item) => {
    counts[item.result] = (counts[item.result] || 0) + 1;
    return counts;
  }, {});
  const positions = trial?.positions?.map(p => row([
    p.group.join(' / '), `${p.label}° ${p.side}`, when(p.opened_at, 'UTC', 'seconds'),
    [number(p.quantity), 'num'], [money(p.max_loss), 'num'], [money(p.fee), 'num'],
    p.settled ? money(p.payout) : 'Cash committed', p.model_version || '—',
  ])) || [];
  const stationDays = trial?.station_days?.map(day => row([
    day.station, day.date, [`${day.settled} / ${day.fills}`, 'num'], [money(day.cash_committed), 'num'],
    [signed(day.realized_pnl), `num ${moneyClass(day.realized_pnl)}`],
  ])) || [];
  const cashHistory = trial?.cash_over_time?.map(change => row([
    when(change.at, 'UTC', 'seconds'), change.kind, change.bucket_id, [money(change.cash), 'num'], [money(change.cash_committed), 'num'],
  ])) || [];
  const attempts = trial?.attempts?.slice(-300).map(attempt => {
    const recheck = attempt.reaction_quote;
    return row([
      `${attempt.station} ${attempt.date}`, `${attempt.label}° ${attempt.side}`, when(attempt.signal_at, 'UTC', 'seconds'),
      [pct(attempt.probability, 1), 'num'], [signed(attempt.ev_dollars), 'num'],
      when(attempt.reaction_at, 'UTC', 'seconds'), [recheck ? pct(recheck.probability, 1) : '—', 'num'],
      [pct(recheck?.entry_price, 1), 'num'], [signed(attempt.reaction_ev_dollars), 'num'], attempt.result,
      attempt.model_version || '—', when(attempt.forecast_issued_at, 'UTC'), number(attempt.coefficient, 4),
    ]);
  }) || [];
  const section = (id, label, header, rows) => rows.length ? `<details id="${id}" ${open.has(id) ? 'open' : ''}>
    <summary>${label}</summary>${table(header, rows)}</details>` : '';
  const base = trial?.cash ?? (live ? null : 500);
  const committed = trial?.cash_committed ?? (live ? null : 0);
  const ledger = trial ? `${trial.persisted} / ${trial.rechecked}` : '—';
  const saved = state.saved;
  const vintages = saved?.model_vintages?.map(item => item.slice(0, 12)).join(', ');
  const assumptions = saved ? `Saved ${saved.id} · ${saved.engine} · ${money(saved.settings.order_size)} all-in ·
    ${saved.settings.reaction_seconds}s reaction · ${saved.settings.freshness || 'legacy freshness'} ·
    ${saved.clock_basis === 'app-availability' ? 'First app availability' : 'HTTP receipt'} ·
    ${saved.models.join(', ') || 'No model support'}${vintages ? ' · model ' + vintages : ''}.` : '';
  host.innerHTML = `<div class="paper-metrics">${stat('Cash available', money(base))}${stat('Cash committed', money(committed))}
    ${stat('Persisted / rechecked', ledger)}${stat('Realized P&L', signed(trial?.realized_pnl))}</div>
    <p class="paper-message">${esc(assumptions)} ${esc(noteText)}</p>
    ${Object.keys(reasons).length ? `<p class="paper-message">${esc(Object.entries(reasons).map(([key, count]) => `${key} ${count}`).join(' · '))}</p>` : ''}
    ${Object.entries(trial?.eligibility_rejections || {}).map(([phase, counts]) =>
      Object.keys(counts).length ? `<p class="paper-message">${phase === 'signal' ? 'Signal exclusions' : 'Recheck exclusions'}
      · ${esc(Object.entries(counts).map(([reason, count]) => `${reason} (${count})`).join(' · '))}</p>` : '').join('')}
    ${section('positions','Paper positions',['Station / day','Bucket','Opened UTC','Shares','Max loss','Fee','Payout','Model version'],positions)}
    ${section('station-days','Paper performance by station day',['Station','Climate day','Settled / fills','Cash committed','Realized P&L'],stationDays)}
    ${section('cash-history','Cash committed over time',['Receipt UTC','Action','Market','Cash','Committed'],cashHistory)}
    ${section('attempts',(trial?.attempt_count || trial?.attempts?.length) > attempts.length ? 'Last 300 reaction checks' : 'Reaction checks',
      ['Station day','Bucket','Signal UTC','Signal odds','Signal EV','Recheck UTC','Recheck odds','Recheck price','Recheck EV','Result','Model','Forecast UTC','Fee coefficient'],attempts)}
    ${captureLog(issues, open.has('capture-issues'))}`;
}
async function loadRuns() {
  try {
    const records = await get('/api/runs?' + context);
    q('#paper-run').innerHTML = '<option value="">Current evidence</option>' + records.map(run =>
      `<option value="${esc(run.id)}">${esc(when(run.as_of))} UTC · ${esc(run.id.slice(0, 8))} · ${run.fills} fills · ${esc(signed(run.realized_pnl))}</option>`).join('');
    q('#paper-run').value = state.saved?.id || '';
  } catch (error) { q('#run-note').textContent = error.message; }
}
async function loadRun(id) {
  const generation = ++state.savedGeneration;
  try {
    const run = id ? await get('/api/runs/' + encodeURIComponent(id)) : null;
    if (generation !== state.savedGeneration) return;
    state.saved = run;
    q('#run-note').textContent = run ? 'Saved entry assumptions, book evidence, and forecast probabilities are frozen.' : '';
    showPaper();
    refreshPaper();
  } catch (error) { q('#run-note').textContent = error.message; }
}

function refreshPaper() {
  clearTimeout(state.paperTimer);
  if (state.mode !== 'live' || state.pivot != null || !currentEvent()) return;
  if (!state.ledgerVisible && !q('#timeline-markets').open &&
    (state.follow || currentEvent().venue === 'kalshi')) return;
  const key = paperKey();
  const needScan = state.paper?.key !== key;
  const wantLedger = state.ledgerVisible && !state.saved && !state.paper?.trialReady;
  if (!needScan && !wantLedger) return;
  state.paperTimer = setTimeout(async () => {
    state.paperRequest?.abort();
    const controller = new AbortController();
    state.paperRequest = controller;
    try {
      const query = `${context}compact=1&live=${historicalContext() ? 0 : 1}&at=${encodeURIComponent(exactClock(knowledge()))}&size=${paperNumber('#order-size')}&reaction=${Math.round(paperNumber('#reaction'))}`;
      const phases = needScan ? [false, ...(wantLedger ? [true] : [])] : [true];
      for (const trialReady of phases) {
        const paper = await get('/api/paper?' + query + '&trial=' + Number(trialReady), { signal: controller.signal });
        if (state.mode !== 'live' || key !== paperKey() || controller.signal.aborted) return;
        state.paper = { ...paper, key, trialReady };
        showPaper(currentEvent());
        showOpportunities(currentEvent());
        showStation(currentEvent(), currentBucket(currentEvent()));
        showGrid(currentEvent());
      }
    } catch (error) {
      if (error.name !== 'AbortError') q('#paper-content').innerHTML = `<p class="paper-message">Could not read the paper ledger. ${esc(error.message)}</p>`;
    }
  }, 120);
}
function showLegacyModel(report) {
  const host = q('#model-content');
  if (!report) {
    host.innerHTML = '<p class="eval-text">Run the model training command to load the holdout evaluation.</p>';
    return;
  }
  const benchmark = report.benchmark;
  const groups = report.groups;
  const matched = report.matched_market;
  const best = Object.entries(report.standardized_coefficients_f || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))[0];
  const features = {
    observed_high_relative_to_nbmtxn: 'observed high relative to NOAA’s daily forecast',
    observed_high_minus_nbs_f: 'observed high relative to NOAA’s daily forecast',
    high_so_far_minus_noaa_f: 'observed high relative to NOAA’s daily forecast',
  };
  const dates = label => `${groups[label].days} dates (${groups[label].first_date} to ${groups[label].last_date})`;
  const scoreRows = ['noaa', 'ridge'].map(key => {
    const value = benchmark[key];
    return row([key === 'noaa' ? 'NOAA NBS TXN' : 'Ridge', [number(value.mae_f, 3), 'num'], [number(value.rmse_f, 3), 'num'],
      [number(value.bias_f, 3), 'num'], [number(value.bucket_brier, 5), 'num']]);
  });
  const misses = benchmark.worst_misses.map(item => row([
    item.date, item.band, [temp(item.actual_f), 'num'], [temp(item.prediction_f, 1), 'num'],
    [temp(item.noaa_high_f), 'num'], [pct(item.winning_probability, 1), 'num'],
  ]));
  const byTime = Object.entries(benchmark.by_time_band).map(([label, item]) => row([
    label, [item.snapshots, 'num'], [number(item.noaa.mae_f, 3), 'num'], [number(item.ridge.mae_f, 3), 'num'],
    [number(item.noaa.bucket_brier, 5), 'num'], [number(item.ridge.bucket_brier, 5), 'num'],
  ]));
  host.innerHTML = `
    <p class="eval-text">${esc(report.model)} for ${esc(report.station)}. Training ${dates('train')}. Calibration ${dates('validation')}. Test ${dates('test')}, ${benchmark.snapshots} snapshots and ${benchmark.bucket_cases} temperature-bucket cases.
    ${best ? `Largest fitted coefficient per standard deviation ${esc(features[best[0]] || best[0].replaceAll('_', ' '))}, ${number(best[1], 3)}°F.` : ''}</p>
    ${table(['Baseline / model', 'MAE °F', 'RMSE °F', 'Bias °F', 'Bucket Brier'], scoreRows)}
    <p class="eval-text">${matched.cases ? `Matched exchange buckets on ${matched.days} independent ${matched.days === 1 ? 'date' : 'dates'}, ${matched.snapshots} snapshots, ${matched.cases} cases. Brier ${number(matched.brier.noaa, 5)} NOAA, ${number(matched.brier.ridge, 5)} Ridge, ${number(matched.brier.market_display_yes, 5)} YES display prices.` :
        'No aligned market-display quotes in the model test set.'} Measures support for weather estimates and price comparison.</p>
    <details><summary>Holdout scores by time of day</summary>${table(['Timing', 'Snapshots', 'NOAA MAE °F', 'Ridge MAE °F', 'NOAA Brier', 'Ridge Brier'], byTime)}</details>
    <details><summary>Largest Ridge misses</summary>${table(['Date', 'Timing', 'Official high', 'Ridge high', 'NOAA high', 'Winning bucket odds'], misses)}</details>`;
}

function showModel(payload) {
  const summary = payload?.study;
  showLegacyModel(payload?.legacy);
  if (!summary) return;
  const host = q('#model-content');
  const legacy = host.innerHTML;
  const test = summary.test;
  const label = key => summary.method_labels[key] || (key === 'market_display_yes' ? 'Polymarket US display YES' : key);
  const scoreRows = Object.entries(test.methods).map(([key, score]) => row([
    label(key), [number(score.mae_f, 3), 'num'], [number(score.bias_f, 3), 'num'], [number(score.bucket_brier, 5), 'num'],
  ]));
  const selected = summary.selection_method;
  const dimensions = grid => Object.entries(grid).map(([name, group]) => row([
    name, [group.noaa.station_days, 'num'], [group.noaa.snapshots, 'num'],
    [number(group.noaa.mae_f, 3), 'num'], [number(group[selected].mae_f, 3), 'num'],
    [number(group.noaa.bucket_brier, 5), 'num'], [number(group[selected].bucket_brier, 5), 'num'],
  ]));
  const cols = ['Group', 'Station-days', 'Decisions', 'NOAA MAE', 'Selected MAE', 'NOAA Brier', 'Selected Brier'];
  const worst = (test.worst_misses[selected] || []).map(entry => row([
    entry.station, entry.date, entry.band, [temp(entry.actual_f), 'num'], [temp(entry.prediction_f, 1), 'num'],
    [temp(entry.noaa_high_f), 'num'], [pct(entry.winning_probability, 1), 'num'],
  ]));
  const market = summary.matched_market;
  const lag = Object.entries(summary.observation_lag_sensitivity).map(([minutes, result]) => row([
    minutes, [number(result.methods.ridge.mae_f, 3), 'num'], [number(result.methods.ridge.bucket_brier, 5), 'num'],
    [number(result.methods.ridge_sky.mae_f, 3), 'num'], [number(result.methods.ridge_sky.bucket_brier, 5), 'num'],
  ]));
  host.innerHTML = `
    <p class="eval-text"><strong>${esc(summary.operational_method_label)} is the live default</strong> from ${esc(when(summary.operational_from))} UTC.
    ${esc(summary.selection_method_label)} was selected before the later test. These retrospective validation results are from ${esc(summary.version)}.</p>
    <p class="eval-text">${esc(Object.entries(summary.groups).map(([name, group]) =>
      `${name} ${group.station_days} station-days / ${group.snapshots} decisions (${group.window.join(' to ')})`).join(' · '))}</p>
    ${table(['Weather method', 'Holdout MAE °F', 'Bias °F', 'Bucket Brier'], scoreRows)}
    <p class="eval-text">${esc(`${market.cases} matched exchange buckets over ${market.station_days} station-days and ${market.calendar_dates} calendar dates.`)}
    Brier uses the quoted YES display price directly at each matching decision.</p>
    ${table(['Matched source', 'Bucket Brier'], Object.entries(market.brier).map(([key, score]) =>
      row([label(key), [number(score, 5), 'num']])))}
    <details><summary>Holdout by station</summary>${table(cols, dimensions(test.by_station))}</details>
    <details><summary>Holdout by time of day</summary>${table(cols, dimensions(test.by_time_band))}</details>
    <details><summary>Largest misses for ${esc(summary.selection_method_label)}</summary>
    ${table(['Station', 'Date', 'Timing', 'Actual', 'Selected forecast', 'NOAA', 'Winning bucket odds'], worst)}</details>
    <details><summary>Observation timing sensitivity</summary>${table(['Receipt proxy minutes', 'Ridge MAE', 'Ridge Brier', 'Ridge + sky MAE', 'Ridge + sky Brier'], lag)}</details>
    <details class="model-section"><summary>Original Central Park benchmark</summary>${legacy}</details>`;
}
async function loadArchive() {
  try {
    const result = await get('/api/archive');
    const hasDay = result.days.some(day => day.date === state.replayDay);
    if (!hasDay && !params.has('day') && result.days.length) state.replayDay = result.days[0].date;
    const el = q('#archive-day');
    el.innerHTML = result.days.map(day => {
      const markets = day.stations.filter(station => station.market).length;
      if (day.global_weather) return `<option value="${esc(day.date)}">${esc(when(day.date + 'T12:00:00Z', 'UTC', 'day'))} · global weather</option>`;
      return `<option value="${esc(day.date)}">${esc(when(day.date + 'T12:00:00Z', 'UTC', 'day'))} · ${day.stations.length} stations${markets ? ` · ${markets} saved markets` : ''}</option>`;
    }).join('');
    if (!hasDay && params.has('day')) el.add(new Option(state.replayDay + ' · no saved replay', state.replayDay));
    el.value = state.replayDay;
    el.disabled = !result.days.length;
  } catch (error) {
    q('#archive-day').disabled = true;
  }
}
function followingLiveClock() {
  return !fixture && state.mode === 'live' && state.loadedMode === 'live' && state.follow &&
    !state.pivot && !state.playing && !state.selectingEvent && !document.hidden;
}

async function refreshFollowingLive() {
  if (state.refreshingLive) {
    state.queuedLiveRefresh = true;
    return;
  }
  state.refreshingLive = true;
  try {
    do {
      state.queuedLiveRefresh = false;
      await loadMode('live', true);
    } while (state.queuedLiveRefresh && followingLiveClock());
  } finally {
    state.refreshingLive = false;
  }
}

let collectionRequest = 0;
let collectionControlPending = false;

function collectionStatus(data) {
  const status = q('#collection-headline');
  const labels = { running: 'Collecting live data', stopped: 'Collection stopped',
    starting: 'Collector starting', offline: 'Collector offline' };
  status.textContent = labels[data.state] || (data.running ? 'Collecting live data' : 'Collector idle');
  q('#collection-dot').dataset.running = String(data.state === 'running');
  const action = q('#collection-action');
  action.disabled = false;
  action.textContent = data.enabled ? 'Stop collection' : 'Start collection';
  const sources = Object.values(data.sources || {});
  const telemetry = data.telemetry_issues || [];
  const gaps = sources.filter(source => source.issue).length + telemetry.length;
  q('#collection-summary').textContent = `Captures and source health${gaps ? ' · ' + gaps + ' with errors' : ''}`;
  const families = Object.entries(data.market_families || {}).map(([key, n]) => `${key} ${n}`);
  const selected = (data.selected_markets || []).map(item => item.slice(0, 2).join(' '));
  const clock = value => when(value, 'UTC', 'seconds');
  const outcomes = {published: 'Published', unchanged: 'Unchanged check', empty: 'Empty selection',
    empty_depth: 'Returned ladders have no usable prices', partial: 'Partial response',
    interrupted: 'Interrupted by Stop', error: 'Source error', legacy: 'Last check from older collector'};
  const batches = Object.values(data.book_batches || {}).flatMap(Object.values);
  const expanded = new Set([...q('#collection-sources').querySelectorAll('[data-book-batch][open]')]
    .map(detail => detail.dataset.bookBatch));
  q('#collection-sources').innerHTML = `<p class="eval-text">${esc(data.stations?.join(', ') || 'No stations yet')}
    · ${esc(families.join(' · '))}${selected.length ? '<br>Book sample ' + esc(selected.join(', ')) : ''}</p>`
    + telemetry.map(issue => `<p class="eval-text">${esc(issue.source)} · ${esc(issue.reason)}</p>`).join('')
    + table(['Source', 'Attempt and completion UTC', 'Last successful check UTC', 'Latest HTTP receipt UTC',
      'Last usable publication UTC', 'Newest upstream UTC', 'Rows', 'Status and retry UTC'],
    sources.map(source => row([source.name,
      [clock(source.attempt_started_at), clock(source.completed_at || source.last_attempt),
        source.duration_seconds == null ? '' : source.duration_seconds.toFixed(2) + 's'].filter(Boolean).join(' / '),
      clock(source.last_success), clock(source.last_receipt_at), clock(source.last_successful_publication_at),
      clock(source.source_at), source.latest_rows ?? '—',
      [outcomes[source.outcome], source.issue, source.check_stale && 'Check stale', source.data_stale && 'Publication stale',
        source.failures && `${source.failures} consecutive failures`,
        source.backoff_until && 'Backoff until ' + clock(source.backoff_until),
        source.provider_not_before && 'Provider pacing until ' + clock(source.provider_not_before),
        source.next_check_not_before && 'Next check at or after ' + clock(source.next_check_not_before),
        !source.issue && source.last_error && 'Previous error ' + clock(source.last_error_at) + ' · ' + source.last_error,
      ].filter(Boolean).join(' · ')])))
    + batches.map(batch => {
      const key = batch.venue + '|' + batch.scope;
      return `<details class="trade-details" data-book-batch="${esc(key)}" ${expanded.has(key) ? 'open' : ''}>
      <summary>${esc(batch.venue)} · ${esc(batch.scope)} ·
      ${esc(clock(batch.completed_at))} · ${batch.selected} selected / ${batch.requested} requested /
      ${batch.usable} usable / ${batch.empty_depth || 0} empty / ${batch.missing_or_invalid} missing or invalid / ${batch.failed} failed /
      ${batch.unrequested} unrequested</summary>
      ${batch.exchange_images ? `<p class="eval-text">Usable exchange images · ${batch.exchange_images.recent} recent ·
        ${batch.exchange_images.stale} stale · ${batch.exchange_images.timestamp_unavailable} timestamps unavailable</p>` : ''}`
      + table(['Instrument', 'Outcome', 'Requested UTC', 'Receipt UTC', 'App publication UTC', 'Exchange image UTC'],
        Object.entries(batch.targets).map(([instrument, target]) => row([
          [target.state.replaceAll('_', ' '), target.reason, target.receipt_id == null ? null : '#' + target.receipt_id,
            target.cache_status && 'HTTP cache ' + target.cache_status]
            .filter(Boolean).join(' · '),
          clock(target.requested_at), clock(target.received_at), clock(target.available_at), clock(target.source_at)],
        `<td><code>${esc(instrument)}</code><br>${['YES', 'NO'].map(side =>
          `<a href="/?${esc(new URLSearchParams({ view: 'trade', tab: 'prices', venue: batch.venue, instrument, side }))}">${side}</a>`
        ).join(' · ')}</td>`)))
      + '</details>';
    }).join('');
  const revision = JSON.stringify(data.data_revision || []);
  const changed = revision !== state.collectionRevision && (data.data_revision?.length || !telemetry.length);
  state.collection = data;
  tradeView?.setSourceHealth(data.sources, data.state);
  if (changed) state.collectionRevision = revision;
  if (changed && followingLiveClock()) refreshFollowingLive();
}
async function pollCollection() {
  if (collectionControlPending) return;
  const generation = ++collectionRequest;
  try {
    const data = await get('/api/collection');
    if (generation === collectionRequest) collectionStatus(data);
  } catch (error) {
    if (generation !== collectionRequest) return;
    q('#collection-headline').textContent = 'Collector status unavailable';
    q('#collection-summary').textContent = 'Collection status unavailable · ' + error.message;
  }
}
function render() {
  const event = currentEvent();
  const bucket = currentBucket(event);
  const linkedStatus = linkedWeatherStatus(event, bucket);
  const history = historicalContext();
  const globeContext = q('#globe-contract');
  const contextLabel = history
    ? `${state.pivot != null ? 'Forecast ahead · Fixed origin' : 'Timeline evidence'} · ${when(history.at, event?.timezone, 'seconds')}`
    : '';
  const contextHeading = q('#workspace-context');
  contextHeading.hidden = !contextLabel;
  if (contextHeading.textContent !== contextLabel) contextHeading.textContent = contextLabel;
  q('#world').dataset.loading = String(!state.loadedMode && !state.session && !q('#notice').dataset.kind);
  globeContext.hidden = !!linkedStatus || !event;
  if (!globeContext.hidden) {
    const forecast = lastBefore(event.forecasts, 'issued_at', knowledge(), row => known(row.available_at, knowledge()));
    const forecastValue = event.family === 'daily-low' ? forecast?.low_f : event.family === 'daily-rain' ? null : forecast?.high_f;
    globeContext.innerHTML = `<strong>${esc(event.station)} · ${esc(event.date)}${bucket
      ? ' · ' + esc(VENUES[event.venue || 'polymarket-us']) + ' · ' + esc(bucket.label) + ' · ' + state.selectedSide : ''}</strong>
      <a href="#station-title">${numeric(forecastValue) ? 'NOAA ' + temp(forecastValue) + 'F · ' : ''}Station evidence ↓</a>`;
  }
  tradeView?.setContext(history, history && event ? marketRows(event).map(row => ({
    ...row, instrument: row.bucket_id, day: row.date, title: event.title, timezone: event.timezone,
    meaning: row.side === 'YES' ? 'Pays if the venue condition is met' : 'Pays if the venue condition is unmet',
    blocker: 'Timeline evidence', category: 'archived', archived: true,
    posted: numeric(row.price) ? { price: row.price, kind: 'Timeline sample', at: row.quote_at } : null,
  })) : [], ticketClockReady());
  showWeatherSummary(event, bucket);
  showForecastOutlook(linkedStatus ? null : event, linkedStatus ? null : bucket);
  q('.trade-ledger').hidden = state.pivot != null;
  q('#timeline-markets').hidden = state.pivot != null;
  q('.paper-card').hidden = state.pivot != null;
  for (const control of document.querySelectorAll('[data-mode]'))
    control.setAttribute('aria-pressed', String(state.pivot == null && control.dataset.mode === state.mode));
  if (state.paper && state.paper.key !== paperKey()) state.paper = null;
  showTimeline(event, bucket);
  showStation(event, bucket);
  refreshStationDecision(event);
  showGrid(event);
  showPaper(event);
  showOpportunities(event);
  refreshPaper();
}
for (const control of document.querySelectorAll('[data-mode]')) {
  control.addEventListener('click', () => switchMode(control.dataset.mode));
}
q('#timeline-markets').addEventListener('toggle', () => refreshPaper());
q('#archive-day').addEventListener('change', event => {
  state.replayDay = event.target.value;
  state.linkedMarket = null;
  state.preferTrade = false;
  const url = new URL(location.href);
  url.searchParams.set('day', state.replayDay);
  url.searchParams.delete('venue');
  url.searchParams.delete('instrument');
  url.searchParams.delete('bucket');
  history.replaceState(null, '', url);
  switchMode('replay');
});
q('#capture-at').addEventListener('change', event => {
  if (!event.target.value) { switchMode('live'); return; }
  stopPlayback();
  state.pivot = null;
  updateBounds(currentEvent(), false);
  seek(stamp(event.target.value), event.target.value || null);
  state.follow = false;
  render();
});
q('#opportunity-filter').addEventListener('change', () => {
  state.opportunityLimit = 18;
  showOpportunities(currentEvent());
});
q('#collection-action').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  collectionControlPending = true;
  collectionRequest += 1;
  try {
    const action = state.collection?.enabled ? 'stop' : 'start';
    collectionStatus(await post('/api/collection', { action }));
  } catch (error) {
    note('Could not change collection. ' + error.message, 'error');
    collectionControlPending = false;
    await pollCollection();
  } finally {
    collectionControlPending = false;
    button.disabled = false;
  }
});
q('#paper-run').addEventListener('change', event => loadRun(event.target.value));
q('#save-run').addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  q('#run-note').textContent = 'Saving evidence and decisions…';
  try {
    const saved = await post('/api/runs', { fixture, at: exactClock(knowledge()),
      size: paperNumber('#order-size'), reaction: Math.round(paperNumber('#reaction')) });
    state.saved = saved;
    await loadRuns();
    q('#run-note').textContent = 'Saved entry assumptions, book evidence, and forecast probabilities are frozen.';
  } catch (error) { q('#run-note').textContent = error.message; }
  showPaper();
});
q('#follow-payouts').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    state.saved = await post('/api/runs/' + encodeURIComponent(state.saved.id) + '/payouts', {});
    await loadRuns();
    q('#run-note').textContent = 'Later payouts were added. Original entry assumptions are preserved in both runs.';
  } catch (error) { q('#run-note').textContent = error.message; }
  button.disabled = false;
  showPaper();
});
for (const id of Object.values(layerControls)) {
  q(id).addEventListener('change', () => showGrid(currentEvent()));
}
for (const id of ['#order-size', '#reaction']) {
  const control = q(id);
  control.addEventListener('input', () => {
    state.paper = null;
    showOpportunities(currentEvent());
    showPaper(currentEvent());
    showGrid(currentEvent());
    refreshPaper();
  });
  control.addEventListener('change', () => {
    control.value = Math.round(paperNumber(id));
    render();
  });
}
q('#time').addEventListener('input', event => {
  stopPlayback();
  seek(state.bounds[0] + Number(event.target.value) * Number(event.target.dataset.precision));
});
q('#play').addEventListener('click', () => {
  if (state.playing) { stopPlayback(); return; }
  if (state.cursor >= state.bounds[1]) seek(state.bounds[0]);
  q('#play').textContent = 'Ⅱ';
  q('#play').setAttribute('aria-label', 'Pause timeline');
  const stride = state.mode === 'live' && state.bounds[1] - state.bounds[0] < 10 * MINUTE ? 1000 : 10 * MINUTE;
  state.playing = setInterval(() => {
    seek(state.cursor + stride);
    if (state.cursor >= state.bounds[1]) stopPlayback();
  }, 270);
});
q('#latest').addEventListener('click', () => switchMode('live'));
q('#preview').addEventListener('click', forecastToggle);
q('#city-select').addEventListener('change', event => chooseEvent(event.target.value));
q('#city-markets').addEventListener('click', () => {
  const event = currentEvent();
  if (!event) return;
  tradeView?.showStation(event.station);
  focusWorkspace('trade', true);
});
let resizePending;
window.addEventListener('resize', () => {
  if (!decisionWorkspace) return;
  cancelAnimationFrame(resizePending);
  resizePending = requestAnimationFrame(() => showStation(currentEvent(), currentBucket(currentEvent())));
});
function focusWorkspace(next, navigate = false) {
  state.focus = next;
  q('#decision-workspace').dataset.focus = next;
  q('#decision-heading').textContent = next === 'weather' ? 'Weather' : 'Trade';
  const first = q(next === 'weather' ? '#weather-workspace' : '#trade-workspace');
  if (first.previousElementSibling) first.parentElement.prepend(first);
  for (const link of document.querySelectorAll('[data-workspace]')) {
    if (link.dataset.workspace === next) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if (navigate) {
    const url = new URL(location.href);
    url.searchParams.set('view', next);
    history.replaceState(null, '', url);
    q(next === 'trade' ? '#trade-heading' : '#weather-summary-heading').focus();
  }
}

focusWorkspace(workspace);
q('#decision-workspace').hidden = !decisionWorkspace;
for (const route of ['backtest', 'markets', 'research']) q('#' + route + '-workspace').hidden = route !== workspace;
if (fixture) q('[data-workspace="backtest"]').href = '/?view=backtest&fixture=1';
pollCollection();
setInterval(pollCollection, 8000);
if (decisionWorkspace) {
  q('#world').dataset.loading = 'true';
  for (const link of document.querySelectorAll('.primary-destination')) {
    link.addEventListener('click', event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      focusWorkspace(link.dataset.workspace, true);
    });
  }
  document.addEventListener('click', event => {
    const weatherLink = event.target.closest('[data-ticket-weather]');
    if (weatherLink) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const current = tradeView.selection();
      const station = currentEvent();
      focusWorkspace('weather', true);
      if (station && current && state.bucketId === current.instrument &&
        (station.venue || 'polymarket-us') === current.venue) q('#station-title')?.focus();
      else {
        q('#globe-heading').focus();
        if (q('#notice').hidden) note('Weather for the selected bucket is loading or unavailable in this timeline.');
      }
      return;
    }
    const anchor = event.target.closest('[data-open-ticket]');
    if (!anchor || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const target = new URL(anchor.href).searchParams;
    focusWorkspace('trade', true);
    tradeView.align({ venue: target.get('venue'), instrument: target.get('instrument'), side: target.get('side') }, true, true);
  });
  tradeView = startTrade(params, {
    get, post, esc, when, money, pct, signed, imageLag,
    initialHistory: historicalContext(),
    clockReady: ticketClockReady(),
    selectWeather: selectTradeWeather,
    onBoard(board) { state.tradeBoard = board; if (state.session) showGrid(currentEvent()); },
  });
  if (fixture) q('#reaction').defaultValue = '5';
  if (params.has('layers')) {
    const layers = new Set(params.get('layers').split(','));
    for (const [layer, selector] of Object.entries(layerControls)) q(selector).checked = layers.has(layer);
  }
  const modelObserver = new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) return;
    modelObserver.disconnect();
    get('/api/model').then(showModel).catch(() => showModel(null));
  }, { rootMargin: '200px' });
  modelObserver.observe(q('#model-card'));
  if (state.mode === 'replay') loadArchive();
  loadRecordedPaper().then(() => { if (state.recordedPaper && state.session) render(); });
  const fromTrade = params.get('from_trade') === '1';
  if (fromTrade) q('#station-content .station-empty').textContent = 'Loading the selected contract’s weather and station readings…';
  chooseMode(state.mode).then(() => {
    if (params.get('forecast') === '1' && state.bounds) {
      forecastToggle();
      const linkedValid = stamp(params.get('forecast_valid'));
      if (numeric(linkedValid)) seek(linkedValid);
    }
    const lat = Number(params.get('probe_lat') ?? NaN), lon = Number(params.get('probe_lon') ?? NaN);
    if (!fixture && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) globe.inspect([lon, lat]);
    if (fromTrade) q('#station-title')?.focus();
    else if (location.hash === '#paper-heading') q('#paper-heading').scrollIntoView();
  });
  let runsRequested = false;
  new IntersectionObserver(([entry]) => {
    state.ledgerVisible = entry.isIntersecting;
    if (state.ledgerVisible && !runsRequested) {
      runsRequested = true;
      loadRuns();
    }
    refreshPaper();
  }).observe(q('.paper-card'));
} else if (workspace === 'backtest') {
  startBacktest(params, { get, post, esc, when, money, pct, table, row, stat });
} else {
  startEvidence(workspace, params);
}
