const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const OBS = '#24776a';
const FORECAST = '#346aa2';
const MARKET = '#a66b36';
const BUCKET = 'rgba(196, 156, 71, .17)';
const OFFICIAL = '#993f58';
const SUPPLEMENTARY = '#8172a4';
const available = row => Date.parse(row.available_at);
const at = row => Date.parse(row.at);
const value = n => Number.isFinite(n);

function split(series, maxGap) {
  let current = [];
  const segments = [];
  for (const point of series) {
    if (!value(point.value) || (current.length && point.at - current.at(-1).at > maxGap)) {
      if (current.length) segments.push(current);
      current = [];
    }
    if (value(point.value)) current.push(point);
  }
  if (current.length) segments.push(current);
  return segments;
}

function setup(canvas, bounds, yBounds, ticks, fmt, labels, cursor) {
  const rect = canvas.getBoundingClientRect();
  const w = rect.width, h = rect.height;
  const scale = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext('2d');
  ctx.scale(scale, scale);
  const left = 31, right = w - 7, top = 8, bottom = h - 16;
  const [start, end] = bounds, [low, high] = yBounds;
  const x = at => left + (at - start) / (end - start) * (right - left);
  const y = temp => bottom - (temp - low) / (high - low) * (bottom - top);
  ctx.font = '9px IBM Plex Mono, monospace';
  ctx.lineWidth = .7;
  ctx.strokeStyle = '#e2e6da';
  ctx.fillStyle = '#626e64';
  ctx.textBaseline = 'middle';
  for (const tick of ticks) {
    ctx.beginPath(); ctx.moveTo(left, y(tick)); ctx.lineTo(right, y(tick)); ctx.stroke();
    ctx.textAlign = 'right'; ctx.fillText(fmt(tick), left - 5, y(tick));
  }
  ctx.textBaseline = 'bottom';
  for (const [time, label] of labels) {
    ctx.textAlign = time === start ? 'left' : time === end ? 'right' : 'center';
    ctx.fillText(label, x(time), h - 1);
  }
  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
  return {
    ctx, x, y, left, right, top, bottom,
    end() {
      ctx.strokeStyle = '#4a6053'; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
      if (cursor >= start && cursor <= end) {
        ctx.beginPath(); ctx.moveTo(x(cursor), top); ctx.lineTo(x(cursor), bottom); ctx.stroke();
      }
      ctx.restore();
    },
    seek(handler) {
      canvas.onclick = event => {
        const offset = event.clientX - canvas.getBoundingClientRect().left;
        const progress = Math.max(0, Math.min(1, (offset - left) / (right - left)));
        handler(Math.round(start + progress * (end - start)));
      };
    },
  };
}

function drawLine(plot, series, color, { gap = Infinity, dots = false, dash = [], stepped = false, width = 1.55 } = {}) {
  const { ctx, x, y } = plot;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = 'round';
  ctx.setLineDash(dash);
  for (const points of split(series, gap)) {
    ctx.beginPath(); ctx.moveTo(x(points[0].at), y(points[0].value));
    for (let i = 1; i < points.length; i++) {
      const point = points[i];
      if (stepped) ctx.lineTo(x(point.at), y(points[i - 1].value));
      ctx.lineTo(x(point.at), y(point.value));
    }
    ctx.stroke();
    if (dots || points.length === 1) {
      for (const point of points) {
        ctx.beginPath(); ctx.arc(x(point.at), y(point.value), dots ? 2.1 : 1.4, 0, Math.PI * 2); ctx.fill();
      }
    }
  }
  ctx.setLineDash([]);
}

function drawBand(plot, data) {
  const { ctx, x, y } = plot;
  ctx.fillStyle = '#4e83b92c';
  for (const points of split(data, 2 * HOUR)) {
    if (points.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(x(points[0].at), y(points[0].value + (points[0].sd ?? 0)));
    for (const point of points.slice(1)) ctx.lineTo(x(point.at), y(point.value + (point.sd ?? 0)));
    for (let i = points.length - 1; i >= 0; i--) {
      const point = points[i];
      ctx.lineTo(x(point.at), y(point.value - (point.sd ?? 0)));
    }
    ctx.closePath();
    ctx.fill();
  }
}

function marks(plot, times, color, { fromTop = true, length = 5 } = {}) {
  plot.ctx.strokeStyle = color;
  plot.ctx.lineWidth = .75;
  plot.ctx.beginPath();
  for (const time of times) {
    const x = plot.x(time);
    plot.ctx.moveTo(x, fromTop ? plot.top : plot.bottom);
    plot.ctx.lineTo(x, fromTop ? plot.top + length : plot.bottom - length);
  }
  plot.ctx.stroke();
}

export function precipitationChart({ event, estimates, official, cursor, onSeek }) {
  const target = document.querySelector('#station-precip-chart');
  if (!target) return;
  const start = Date.parse(event.climate_starts_at);
  const last = Math.max(Date.parse(event.climate_ends_at), cursor + 10 * MINUTE);
  const hours = new Intl.DateTimeFormat('en-US', { timeZone: event.timezone, hour: 'numeric' });
  const labels = [0, 1/3, 2/3, 1].map(f => {
    const stamp = start + f * (last - start);
    return [stamp, hours.format(stamp)];
  });
  const amounts = estimates.flatMap(series => series.points.map(row => row.value));
  if (official && value(official.value)) amounts.push(official.value);
  const max = Math.max(2, ...amounts);
  const spacing = max >= 25 ? 10 : max > 8 ? 5 : 1;
  const ceiling = Math.ceil(max / spacing) * spacing;
  const ticks = Array.from({ length: ceiling / spacing + 1 }, (_, index) => index * spacing);
  const chart = setup(target, [start, last], [0, ceiling], ticks, String, labels, cursor);
  for (const item of estimates) drawLine(chart, item.points, item.color, { stepped: true });
  if (official && value(official.value)) drawLine(chart, [official], OFFICIAL, { dots: true });
  chart.end();
  chart.seek(onSeek);
}

export function monthlyRainChart(canvas, period, cursor, onSeek) {
  const start = Date.parse(period.climate_start);
  const end = Math.max(Date.parse(period.climate_end), cursor + HOUR);
  const readings = period.reports.map(row => ({ at: Date.parse(row.published_at), value: row.amount_in }));
  const ceiling = Math.max(1, Math.ceil(Math.max(period.threshold_in, ...readings.map(row => row.value))));
  const spacing = Math.max(1, Math.ceil(ceiling / 5));
  const top = Math.ceil(ceiling / spacing) * spacing;
  const ticks = Array.from({ length: top / spacing + 1 }, (_, index) => index * spacing);
  const format = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' });
  const labels = [0, .5, 1].map(fraction => {
    const clock = start + fraction * (end - start);
    return [clock, format.format(clock)];
  });
  const plot = setup(canvas, [start, end], [0, top], ticks, String, labels, cursor);
  drawLine(plot, [{ at: start, value: period.threshold_in }, { at: end, value: period.threshold_in }], MARKET, { dash: [4, 3] });
  drawLine(plot, readings.filter(point => point.at <= cursor), OBS, { stepped: true, dots: true });
  if (period.settled_at && Date.parse(period.settled_at) <= cursor) marks(plot, [Date.parse(period.settled_at)], OFFICIAL, { length: 18 });
  plot.end();
  plot.seek(onSeek);
}

export function weatherIndexChart(canvas, index, cursor, local, retrospective, threshold, onSeek) {
  const start = Date.parse(index.starts_at), end = Date.parse(index.ends_at);
  const series = points => points.map(row => ({ at: Date.parse(row.at), value: Number(row.temperature_f) }));
  const known = series(local), archive = series(retrospective);
  const amounts = [...known, ...archive].map(row => row.value);
  if (threshold != null) amounts.push(threshold);
  const midpoint = amounts.length ? amounts[0] : 0;
  const spacing = Math.max(1, Math.ceil((Math.max(midpoint, ...amounts) - Math.min(midpoint, ...amounts)) / 5));
  const low = Math.floor((Math.min(midpoint, ...amounts) - .3) / spacing) * spacing;
  const high = Math.ceil((Math.max(midpoint, ...amounts) + .3) / spacing) * spacing;
  const ticks = Array.from({ length: (high - low) / spacing + 1 }, (_, i) => low + spacing * i);
  const utc = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' });
  const labels = [start, (start + end) / 2, end].map(clock => [clock, utc.format(clock)]);
  const plot = setup(canvas, [start, end], [low, high], ticks, n => n + '°', labels, cursor);
  if (threshold != null) {
    drawLine(plot, [{ at: start, value: threshold }, { at: end, value: threshold }], MARKET, { dash: [4, 3] });
  }
  drawLine(plot, archive, '#8c9187', { gap: 2 * MINUTE, dash: [2, 2] });
  drawLine(plot, known, OBS, { gap: 2 * MINUTE });
  const target = Date.parse(index.target_at);
  if (target <= end) marks(plot, [target], OFFICIAL, { length: 16 });
  plot.end();
  plot.seek(onSeek);
}

export function venuePriceTimeline(canvas, series, markers) {
  const clocks = [...markers.map(row => Date.parse(row.at)), ...series.flatMap(row => row.points.map(at))].filter(value);
  if (clocks.length < 2) return;
  const earliest = Math.min(...clocks) - 2 * MINUTE, latest = Math.max(...clocks) + 2 * MINUTE;
  const utc = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' });
  const labels = [earliest, (earliest + latest) / 2, latest].map(clock => [clock, utc.format(clock)]);
  const plot = setup(canvas, [earliest, latest], [0, 1], [0, .25, .5, .75, 1], n => `${Math.round(n * 100)}¢`, labels, NaN);
  for (const marker of markers) {
    const x = plot.x(at(marker));
    plot.ctx.strokeStyle = marker.kind === 'report' ? OFFICIAL : '#758077';
    plot.ctx.setLineDash(marker.kind === 'report' ? [3, 2] : [1, 3]);
    plot.ctx.beginPath(); plot.ctx.moveTo(x, plot.top); plot.ctx.lineTo(x, plot.bottom); plot.ctx.stroke();
  }
  for (const lane of series) {
    const points = lane.points.map(row => ({ at: at(row), value: row.yes })).sort((a, b) => a.at - b.at);
    const trade = lane.kind === 'trade';
    drawLine(plot, points, lane.color, { gap: trade ? 0 : 2 * MINUTE, dots: trade, dash: trade ? [] : [4, 2] });
  }
  plot.end();
}

export function paperCapitalChart(canvas, history, bounds, limit) {
  const [start, end] = bounds;
  const spacing = Math.max(1, Math.ceil(limit / 4));
  const ceiling = Math.ceil(limit / spacing) * spacing;
  const ticks = Array.from({ length: ceiling / spacing + 1 }, (_, index) => index * spacing);
  const format = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', hour: 'numeric', minute: '2-digit' });
  const labels = [start, (start + end) / 2, end].map(clock => [clock, format.format(clock)]);
  const trace = [{ at: start, value: 0 }, ...history.map(row => ({ at: Date.parse(row.at), value: row.cash_committed }))];
  trace.push({ at: end, value: trace.at(-1).value });
  const plot = setup(canvas, bounds, [0, ceiling], ticks, n => '$' + n, labels, NaN);
  drawLine(plot, trace, MARKET, { stepped: true, dots: true });
  plot.end();
}

export function stormTrackChart(canvas, track, through, fullRoute) {
  const visible = track.positions.slice(0, through + 1);
  const rings = track.boundary_rings;
  const region = rings.flat();
  const coordinates = fullRoute ? [...region, ...visible.map(row => [row.longitude, row.latitude])] : region;
  const [width, height] = [canvas.clientWidth, canvas.clientHeight];
  if (width <= 0 || height <= 0 || !coordinates.length) return;
  const density = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * density);
  canvas.height = Math.round(height * density);
  const ctx = canvas.getContext('2d');
  ctx.scale(density, density);
  const [longitudes, latitudes] = [coordinates.map(row => row[0]), coordinates.map(row => row[1])];
  const bounds = [Math.min(...longitudes), Math.max(...longitudes), Math.min(...latitudes), Math.max(...latitudes)];
  const lonCenter = (bounds[0] + bounds[1]) / 2, latCenter = (bounds[2] + bounds[3]) / 2;
  const projection = Math.cos(latCenter * Math.PI / 180);
  const scale = Math.min((width - 30) / ((bounds[1] - bounds[0] + 1.2) * projection),
    (height - 26) / (bounds[3] - bounds[2] + 1.0));
  const x = lon => width / 2 + (lon - lonCenter) * projection * scale;
  const y = lat => height / 2 - (lat - latCenter) * scale;
  ctx.fillStyle = '#edf3ef';
  ctx.fillRect(0, 0, width, height);
  ctx.setLineDash([2, 4]);
  ctx.strokeStyle = '#b9c8be';
  ctx.lineWidth = .6;
  ctx.beginPath();
  for (let lon = Math.ceil(bounds[0]); lon <= Math.floor(bounds[1]); lon++) {
    ctx.moveTo(x(lon), 0);
    ctx.lineTo(x(lon), height);
  }
  for (let lat = Math.ceil(bounds[2]); lat <= Math.floor(bounds[3]); lat++) {
    ctx.moveTo(0, y(lat));
    ctx.lineTo(width, y(lat));
  }
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  for (const ring of rings) {
    ring.forEach(([lon, lat], index) => index === 0 ? ctx.moveTo(x(lon), y(lat)) : ctx.lineTo(x(lon), y(lat)));
    ctx.closePath();
  }
  ctx.fillStyle = '#e1e5ca';
  ctx.strokeStyle = '#5b705c';
  ctx.lineWidth = 1;
  ctx.fill('evenodd');
  ctx.stroke();

  for (const [index, row] of visible.slice(1).entries()) {
    const prior = visible[index];
    const crossing = row.at === track.crossing_observed_inside_at && prior.at === track.crossing_after_at;
    const qualifies = Math.min(prior.sustained_mph, row.sustained_mph) >= track.threshold_mph;
    ctx.strokeStyle = crossing ? OFFICIAL : qualifies ? OBS : MARKET;
    ctx.lineWidth = crossing ? 3.2 : 1.8;
    ctx.beginPath();
    ctx.moveTo(x(prior.longitude), y(prior.latitude));
    ctx.lineTo(x(row.longitude), y(row.latitude));
    ctx.stroke();
  }
  for (const row of visible) {
    const selected = row === visible.at(-1);
    ctx.beginPath();
    ctx.arc(x(row.longitude), y(row.latitude), selected ? 4.6 : 2.2, 0, 2 * Math.PI);
    ctx.fillStyle = row.sustained_mph >= track.threshold_mph ? OBS : MARKET;
    ctx.fill();
    if (selected) {
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#1f3630';
      ctx.stroke();
    }
  }
  ctx.fillStyle = '#384a3d';
  ctx.font = '11px IBM Plex Mono, monospace';
  ctx.fillText(track.region, 10, 16);
}

export function charts({ event, bucket, readings, reports, exactDecision, payoutClock, cursor, knowledge,
  forecast, localGrid = [], reportField = 'high_f', zone, onSeek, projected, capture = false, evidenceKnown }) {
  const climateStart = Date.parse(event.climate_starts_at || event.starts_at);
  const climateEnd = Date.parse(event.climate_ends_at || event.ends_at);
  const firstKnown = Math.min(climateStart, cursor);
  const lastKnown = Math.max(climateEnd, cursor + 10 * MINUTE, firstKnown + HOUR);
  const bounds = [firstKnown, lastKnown];
  const hours = new Intl.DateTimeFormat('en-US', { timeZone: zone, hour: 'numeric' });
  const labels = [0, 1/3, 2/3, 1].map(f => {
    const stamp = firstKnown + f * (lastKnown - firstKnown);
    return [stamp, hours.format(stamp)];
  });
  const station = readings.map(row => ({ at: at(row), value: row.temp_f }));
  const supplementary = (event.research_observations || [])
    .filter(row => evidenceKnown(row.available_at) && evidenceKnown(row.at) && value(row.temp_f))
    .map(row => ({ at: at(row), value: row.temp_f })).sort((a, b) => a.at - b.at);
  const dense = [...new Map((event.madis_airport_temperature || [])
    .filter(row => evidenceKnown(row.available_at) && evidenceKnown(row.at) && value(row.temp_f))
    .map(row => [at(row), { at: at(row), value: row.temp_f }])).values()];
  const official = reports.map(row => ({ at: available(row), value: row[reportField] }));
  const hourlies = (forecast?.hourly || []).map(row => ({ at: Date.parse(row.valid_at), value: row.temp_f, sd: row.sd_f }));
  const remaining = hourlies.filter(point => point.at >= Math.max(knowledge, climateStart) && point.at <= climateEnd);
  const hrrr = localGrid.map(row => ({ at: Date.parse(row.valid_at), value: row.temp_f }));
  const limits = [bucket?.lower_f, bucket?.upper_f].filter(value);
  const temperatures = [...hourlies.flatMap(point => [point.value - (point.sd || 0), point.value + (point.sd || 0)]),
    ...station.map(point => point.value), ...supplementary.map(point => point.value), ...dense.map(point => point.value), ...hrrr.map(point => point.value),
    ...official.map(point => point.value), ...limits, forecast?.high_f].filter(value);
  const min = temperatures.length ? Math.min(...temperatures) : 40;
  const max = temperatures.length ? Math.max(...temperatures) : 80;
  const spacing = max - min > 32 ? 10 : max - min > 15 ? 5 : 4;
  const lo = Math.floor((min - 1) / spacing) * spacing;
  const hi = Math.ceil((max + 1) / spacing) * spacing;
  const ticks = Array.from({ length: (hi - lo) / spacing + 1 }, (_, i) => lo + spacing * i);
  const tempCanvas = document.querySelector('#temp-chart');
  if (!tempCanvas) return;
  const tempPlot = setup(tempCanvas, bounds, [lo, hi], ticks, n => n + '°', labels, cursor);
  if (bucket && limits.length) {
    const top = tempPlot.y(bucket.upper_f == null ? hi : bucket.upper_f + .5);
    const bottom = tempPlot.y(bucket.lower_f == null ? lo : bucket.lower_f - .5);
    tempPlot.ctx.fillStyle = BUCKET;
    tempPlot.ctx.fillRect(tempPlot.left, top, tempPlot.right - tempPlot.left, bottom - top);
  }
  drawBand(tempPlot, remaining);
  drawLine(tempPlot, station, OBS, { gap: 90 * MINUTE, dots: true });
  drawLine(tempPlot, supplementary, SUPPLEMENTARY, { gap: 20 * MINUTE, width: .9 });
  drawLine(tempPlot, dense, '#837247', { gap: 15 * MINUTE, width: .9 });
  drawLine(tempPlot, official, OFFICIAL, { gap: 0, dots: true });
  drawLine(tempPlot, remaining, FORECAST, { gap: 2 * HOUR, dash: [3, 2] });
  drawLine(tempPlot, hrrr, '#855785', { gap: HOUR, dash: [2, 2] });
  const releases = (event.forecasts || []).filter(row => evidenceKnown(row.available_at)).map(available);
  marks(tempPlot, releases, FORECAST);
  marks(tempPlot, readings.map(available), OBS, { fromTop: false });
  marks(tempPlot, official.map(point => point.at), OFFICIAL, { length: 10 });
  tempPlot.end();
  tempPlot.seek(onSeek);

  const oddsCanvas = document.querySelector('#odds-chart');
  if (!oddsCanvas) return;
  const odds = setup(oddsCanvas, bounds, [0, 1], [0, .25, .5, .75, 1], n => Math.round(n * 100) + '%', labels, cursor);
  const decisions = (event.decisions || []).filter(row => evidenceKnown(row.at) && at(row) !== at(exactDecision ?? {}));
  if (exactDecision) decisions.push(exactDecision);
  decisions.sort((a, b) => at(a) - at(b));
  const model = decisions.map(row => {
    const probability = row.buckets.find(item => item.id === bucket?.id);
    return { at: at(row), value: probability?.probability };
  });
  const market = (bucket?.prices || []).filter(row => evidenceKnown(row.available_at || row.at))
    .map(row => ({ at: at(row), value: row.yes })).sort((a, b) => a.at - b.at);
  const marketGap = event.venue === 'kalshi' ? 90 * MINUTE : capture ? 30_000 : 90_000;
  drawLine(odds, market, MARKET, { gap: marketGap, width: 1.3 });
  drawLine(odds, model, OBS, { gap: event.family === 'daily-low' ? Infinity : 20 * MINUTE, stepped: true });
  const originOdds = exactDecision?.buckets.find(item => item.id === bucket?.id)?.probability;
  if (projected && value(originOdds) && cursor > knowledge) {
    drawLine(odds, [{ at: knowledge, value: originOdds }, { at: cursor, value: originOdds }], OBS, { dash: [3, 3] });
  }
  marks(odds, releases, FORECAST);
  const payout = Date.parse(payoutClock);
  if (evidenceKnown(payoutClock)) marks(odds, [payout], OFFICIAL, { length: 11 });
  const priceMoves = market.filter((row, idx) => value(row.value) && idx && value(market[idx - 1].value)
    && row.at - market[idx - 1].at <= marketGap && row.value !== market[idx - 1].value);
  marks(odds, priceMoves.map(row => row.at), '#a66b3677', { fromTop: false });
  odds.end();
  odds.seek(onSeek);
}
