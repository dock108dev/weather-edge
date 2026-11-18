// Timeline state changes do not start requests or render the workspace.
const HOUR = 3_600_000;
export const stamp = value => Date.parse(value);
const numeric = n => Number.isFinite(n);
// Chart coordinates use milliseconds. Explicit evidence links retain the fractional receipt time.
const clockTail = value => (String(value).match(/\.\d{3}(\d+)/)?.[1] || '').padEnd(6, '0');

export function timelineState(params, fixture) {
  return {
    mode: fixture || params.get('mode') === 'live' ||
      (!params.has('mode') && params.get('archived') !== '1' && !params.has('day') && !params.has('at')) ? 'live' : 'replay',
    session: null, eventId: '', bucketId: '', loadedMode: null,
    cursor: 0, bounds: null, pivot: null, timeAnchor: null,
    replayDay: params.get('day') || '2026-11-05', preferredStation: params.get('station') || 'KNYC',
    linkedBucket: params.get('bucket') || params.get('instrument'), linkedAt: params.get('at'),
    selectedSide: params.get('side') === 'NO' ? 'NO' : 'YES',
    linkedMarket: params.has('venue') && params.has('instrument')
      ? { venue: params.get('venue'), instrument: params.get('instrument') } : null,
    follow: !params.has('at'), preferTrade: !!params.get('instrument'),
  };
}

export function chooseBucket(event) {
  return event?.buckets.find(bucket => bucket.id.endsWith('gte71lt72f'))
    || event?.buckets.find(bucket => bucket.label.includes('51') && bucket.label.includes('52'))
    || event?.buckets[Math.floor(event.buckets.length / 2)];
}

export function createTimeline(state, params) {
  const knowledge = () => state.pivot ?? state.cursor;
  const clamp = ms => Math.max(state.bounds[0], Math.min(ms, state.bounds[1]));

  function exactClock(at) {
    return state.timeAnchor?.ms === at ? state.timeAnchor.iso : new Date(at).toISOString();
  }
  function known(value, at) {
    const ms = stamp(value);
    return Number.isFinite(ms) && (ms < at || ms === at &&
      clockTail(value) <= (state.timeAnchor?.ms === at ? clockTail(state.timeAnchor.iso) : '000000'));
  }

  const frameKnown = (row, at) => known(row.available_at, at) &&
    (state.mode !== 'live' || !row.ingested_at || known(row.ingested_at, at));

  function lastBefore(entries, key, at, predicate = () => true) {
    let latest, latestAt = -Infinity;
    for (const item of entries || []) {
      const t = stamp(item[key]);
      const newer = t > latestAt || (t === latestAt && clockTail(item[key]) > clockTail(latest[key]));
      if (newer && t <= at && known(item[key], at) && predicate(item)) { latest = item; latestAt = t; }
    }
    return latest;
  }

  function updateBounds(event, initial) {
    const global = state.mode === 'replay' ? state.session?.weather_window : null;
    if (!event && !global) { state.bounds = null; state.cursor = 0; return; }
    const start = state.mode === 'live'
      ? stamp(state.session.started_at || state.session.captured_at)
      : stamp(event?.climate_starts_at || event?.starts_at || global?.starts_at);
    const latest = state.mode === 'live'
      ? stamp(state.session.captured_at)
      : stamp(event?.timeline_ends_at || event?.settlement?.payout_available_at || event?.settles_at || event?.ends_at || global?.ends_at);
    if (!numeric(start) || !numeric(latest)) { state.bounds = null; state.cursor = 0; return; }
    const previewTimes = [...state.session.grids, ...(state.session.waves || [])].filter(row => frameKnown(row, state.pivot))
      .map(row => stamp(row.valid_at)).filter(numeric);
    if (state.pivot != null) {
      const forecast = lastBefore(event?.forecasts, 'issued_at', state.pivot, row => known(row.available_at, state.pivot));
      previewTimes.push(...(forecast?.hourly || []).map(row => stamp(row.valid_at)).filter(numeric));
    }
    const projectedEnd = state.pivot == null ? latest : Math.max(state.pivot + HOUR, ...previewTimes);
    state.bounds = [state.pivot == null ? start : state.pivot, Math.max(start + 1000, projectedEnd)];
    if (initial) {
      state.cursor = state.mode === 'live' ? latest : Math.min(latest, start + 16 * HOUR);
      if (state.mode === 'live' && state.follow && state.pivot == null)
        state.timeAnchor = { ms: latest, iso: state.session.captured_at };
    }
    state.cursor = clamp(state.cursor);
  }

  function applyLinkedClock(event) {
    const linkedAt = stamp(state.linkedAt);
    if (state.bounds && numeric(linkedAt)) {
      const missingClock = linkedAt < state.bounds[0] || linkedAt > state.bounds[1];
      state.cursor = clamp(linkedAt);
      const endpoint = state.mode === 'replay' && (linkedAt > state.bounds[1]
        ? event?.timeline_ends_at : event?.climate_starts_at || event?.starts_at);
      state.timeAnchor = !missingClock ? { ms: linkedAt, iso: state.linkedAt }
        : stamp(endpoint) === state.cursor ? { ms: state.cursor, iso: endpoint } : null;
      state.follow = false;
      state.linkedAt = null;
      return missingClock;
    }
    return false;
  }

  function selectSession(session, mode, refresh) {
    state.session = session;
    state.loadedMode = mode;
    const active = mode === 'live' ? session.events.filter(item => stamp(item.ends_at) > stamp(session.captured_at)) : [];
    const prior = session.events.find(item => item.id === state.eventId);
    const requestedBucket = state.linkedBucket || (state.preferTrade && state.linkedMarket?.instrument);
    const linkedEvent = requestedBucket && session.events.find(item =>
      (!state.linkedMarket || state.linkedMarket.venue === (item.venue || 'polymarket-us')) &&
      item.buckets.some(bucket => bucket.id === requestedBucket));
    const linkedStation = !refresh && params.has('day') && state.replayDay === params.get('day')
      && session.events.find(item => item.station === state.preferredStation && item.date === state.replayDay);
    const event = session.events.find(item => item.id === session.detail_event) || linkedEvent || linkedStation || (mode === 'live'
      ? (refresh ? prior : null) || active.find(item => item.station === state.preferredStation && !item.weather_only)
        || active.find(item => !item.weather_only) || active.find(item => item.station === state.preferredStation) || active[0] || session.events.at(-1)
      : prior || session.events.find(item => item.station === state.preferredStation) || session.events[0]);
    state.eventId = event?.id;
    state.bucketId = linkedEvent ? requestedBucket
      : event?.buckets.some(item => item.id === state.bucketId) ? state.bucketId : chooseBucket(event)?.id;
    updateBounds(event, !refresh || state.follow);
    return { event, prior, linkedEvent, missingClock: !refresh && applyLinkedClock(event) };
  }

  function historicalContext(event) {
    if (state.mode === 'live' && state.follow && state.pivot == null) return null;
    const day = event?.date || state.replayDay;
    return { archived: '1', day, at: state.bounds ? exactClock(knowledge()) : state.linkedAt || day + 'T12:00:00Z',
      ...(state.pivot != null || !state.loadedMode && params.get('forecast') === '1' && state.linkedAt ? { forecast: '1' } : {}),
      ...(state.mode === 'live' ? { history_mode: 'live' } : {}) };
  }

  function resetMode(mode) {
    const clearSession = state.mode !== mode || state.pivot != null;
    state.follow = !state.linkedAt;
    state.pivot = null;
    state.timeAnchor = null;
    state.mode = mode;
    state.loadedMode = null;
    if (clearSession) {
      state.session = null;
      state.bounds = null;
    }
    return clearSession;
  }

  function moveCursor(ms, iso = null) {
    if (!state.bounds) return;
    state.cursor = clamp(ms);
    if (state.pivot == null) state.timeAnchor = iso && state.cursor === ms ? { ms, iso } : null;
    if (state.mode === 'live' && state.pivot == null) state.follow = state.cursor >= state.bounds[1] - 500;
    return true;
  }

  function fixForecastOrigin() {
    state.pivot = state.cursor;
    const receipt = state.session?.captured_at;
    if (state.mode === 'live' && stamp(receipt) === state.pivot && state.timeAnchor?.ms !== state.pivot)
      state.timeAnchor = { ms: state.pivot, iso: receipt };
  }

  function firstForecastValidTime(event) {
    const nextGrid = [...state.session.grids, ...(state.session.waves || [])].filter(row => frameKnown(row, state.pivot)
      && stamp(row.valid_at) > state.pivot).map(row => stamp(row.valid_at));
    const nextHour = forecastHours(event)[0]?.valid_at;
    const next = Math.min(nextHour ? stamp(nextHour) : Infinity, ...nextGrid);
    return numeric(next) ? next : state.pivot;
  }

  function returnToOrigin(event) {
    state.cursor = state.pivot;
    state.pivot = null;
    updateBounds(event, false);
  }

  function hourlyForecast(event, at) {
    return lastBefore(event?.forecasts, 'issued_at', at, row => known(row.available_at, at) && row.hourly?.length);
  }

  function forecastHours(event) {
    return (hourlyForecast(event, state.pivot)?.hourly || []).filter(row =>
      stamp(row.valid_at) > state.pivot && numeric(row.temp_f)).sort((a, b) => stamp(a.valid_at) - stamp(b.valid_at));
  }

  return {
    exactClock, known, frameKnown, lastBefore, knowledge, updateBounds, selectSession,
    historicalContext, resetMode, moveCursor, fixForecastOrigin, firstForecastValidTime,
    returnToOrigin, hourlyForecast, forecastHours,
  };
}
