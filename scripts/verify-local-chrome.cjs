/* Run against a local build with Chrome and a separately installed playwright-core. */
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync, writeFileSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { chromium } = require(resolve(process.env.PLAYWRIGHT_CORE));

const [mode, port, out] = process.argv.slice(2);
const base = `http://127.0.0.1:${Number(port)}`;
const url = (view, args = {}) => base + '/?' + new URLSearchParams({ view, ...args });
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const instrument = 'tc-temp-nychigh-2026-11-15-gte49lt50f';
const identity = { venue: 'polymarket-us', instrument, side: 'YES' };
const miami = '40ee74c6230a6dd3846fcbb7';

async function json(response, expected = 200) {
  assert.equal(response.status(), expected, response.url());
  assert.equal(response.headers()['cache-control'], 'no-store', response.url());
  return response.json();
}

async function empty(page) {
  const cases = [
    ['trade', '#trade-ready', /Ready\s*·\s*0/],
    ['weather', '#station-content', /Awaiting first capture/],
    ['backtest', '#backtest-assumptions', /Choose up to/],
    ['markets', '#market-results', /No contracts matched/],
    ['research', '#research-results', /No recorded studies match/],
  ];
  for (const [view, selector, pattern] of cases) {
    await page.goto(url(view));
    await page.waitForFunction(([sel, words, flags]) =>
      new RegExp(words, flags).test(document.querySelector(sel)?.innerText), [selector, pattern.source, pattern.flags]);
  }
  await page.goto(url('weather'));
  await page.locator('#model-card').scrollIntoViewIfNeeded();
  await page.getByText('Run the model training command to load the holdout evaluation.').waitFor();
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 320, height: viewport.height });
  await page.goto(url('trade'));
  await page.getByText('No paper opportunities qualify right now.').waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320);
  await page.setViewportSize(viewport);
  console.log('Empty Chrome views passed:', cases.map(([view]) => view).join(', '));
}

async function backtest(page) {
  const fixture = await json(await page.request.get(base + '/api/session?mode=live&fixture=1'));
  assert.equal(fixture.cached, true);
  assert.equal(fixture.captured_at, '2026-11-15T17:10:30Z');
  const route = url('backtest', { fixture: 1 });
  await page.goto(route);
  const saved = page.waitForResponse(r => new URL(r.url()).pathname === '/api/backtests' && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Run and save backtest' }).click();
  const run = await json(await saved);
  assert.match(run.id, /^[a-f0-9]{24}$/);
  assert.ok(run.fills > 0 && run.skipped > 0, JSON.stringify(run));
  const runUrl = url('backtest', { fixture: 1, run: run.id });
  await page.waitForURL(runUrl);
  await page.locator('#backtest-result').getByText(run.id, { exact: true }).waitFor();
  const read = page.waitForResponse(r => new URL(r.url()).pathname === '/api/backtests/' + run.id);
  await page.goto(runUrl);
  const reopened = await json(await read);
  assert.deepEqual(reopened, run);
  await page.locator('#backtest-result').getByText(run.id, { exact: true }).waitFor();
  await page.screenshot({path: join(out, 'fixture-backtest.png')});
  writeFileSync(join(out, 'run-id.txt'), run.id + '\n');
  writeFileSync(join(out, 'fixture-backtest.json'), JSON.stringify(run, null, 2) + '\n');
  console.log('Saved and reopened:', runUrl,
    JSON.stringify({ signals: run.signals, fills: run.fills, skipped: run.skipped, awaiting: run.awaiting,
      unresolved_payouts: run.unresolved_payouts }));
}

function chartAudit() {
  const canvas = CanvasRenderingContext2D.prototype;
  window.bands = [];
  for (const name of ['beginPath', 'moveTo', 'lineTo', 'fill']) {
    const original = canvas[name];
    canvas[name] = function (...args) {
      if (this.canvas.id === 'temp-chart') {
        if (name === 'beginPath') this.auditPath = [];
        if (name === 'moveTo' || name === 'lineTo') this.auditPath?.push(args);
        if (name === 'fill' && this.fillStyle.startsWith('rgba(78, 131, 185,')) window.bands.push(this.auditPath);
      }
      return original.apply(this, args);
    };
  }
}

async function compatibility(page, leak = false) {
  const fixture = await json(await page.request.get(base + '/api/session?mode=live&fixture=1'));
  const weather = url('weather', { fixture: 1, ...identity, at: fixture.captured_at });
  const selector = '[data-station-detail="research"]';
  // First use the unmodified response with the optional airport series absent.
  assert.equal(fixture.events[0].madis_airport_temperature, undefined);
  await page.goto(weather);
  await page.locator(selector).waitFor();
  assert.doesNotMatch(await page.locator(selector).textContent(), /airport file/);
  assert.deepEqual(await page.locator('#capture-at option').evaluateAll(rows => rows.map(row => row.value)),
    ['', fixture.captured_at]);
  await page.locator('#temp-chart').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => window.bands.length > 0);
  const band = await page.evaluate(() => window.bands.at(-1));
  const half = band.length / 2;
  assert.ok(Number.isInteger(half) && half >= 2);
  assert.deepEqual(band.slice(0, half).map(([x]) => x), band.slice(half).map(([x]) => x).reverse());
  assert.ok(band[0][0] < band[half - 1][0] && band[0][1] < band.at(-1)[1]);

  const sessions = Array.from({length: 82}, (_, i) => ({
    captured_at: new Date(Date.parse(fixture.captured_at) - (81 - i) * 1000).toISOString(),
  }));
  const event = fixture.events[0];
  const at = '2026-11-15T17:10:08Z';
  const origin = '2026-11-15T17:10:30.000123Z';
  const afterOrigin = '2026-11-15T17:10:30.000124Z';
  event.madis_airport_temperature = [
    { at, available_at: at, temp_f: 50, temp_c_whole: 10 },
    { at, available_at: at, temp_f: 51.8, temp_c_whole: 11 },
    { at, available_at: leak ? origin : afterOrigin, temp_f: 176, temp_c_whole: 80 },
  ].map(row => ({ ...row, source: 'https://example.invalid/airport', madis_received_at: row.available_at,
    source_at: row.available_at, ingested_at: row.available_at }));
  const late = { ...event.forecasts[0], issued_at: '2026-11-15T17:10:20Z',
    available_at: afterOrigin, hourly: [{ valid_at: '2026-11-15T18:00:00Z', temp_f: 177, sd_f: 1 }] };
  event.forecasts.push(late);
  await page.route('**/api/session?**', r => r.fulfill({
    json: { ...fixture, captured_at: origin, sessions: [...sessions, sessions.at(-1)] },
  }));
  await page.goto(url('weather', { fixture: 1, ...identity, station: event.station, at: origin }));
  await page.locator(selector).waitFor();
  const airport = await page.locator(selector).textContent();
  assert.match(airport, /11°C \(about 51\.8°F\)/);
  assert.doesNotMatch(airport, /10°C|80°C/);
  assert.deepEqual(await page.locator('#capture-at option').evaluateAll(rows => rows.map(row => row.value)),
    ['', ...sessions.slice(-79).reverse().map(row => row.captured_at), sessions[0].captured_at]);

  await page.locator('#preview').click();
  const hours = page.locator('[data-forecast-at]');
  await hours.first().waitFor();
  await hours.first().click();
  const address = new URL(page.url());
  assert.equal(address.searchParams.get('forecast'), '1');
  assert.equal(address.searchParams.get('at'), origin);
  assert.equal(address.searchParams.get('forecast_valid'), '2026-11-15T18:00:00.000Z');
  assert.match(await hours.first().innerText(), /51°F/);
  assert.doesNotMatch(await page.locator('#forecast-outlook').innerText(), /177°F/);
  assert.doesNotMatch(await page.locator(selector).textContent(), /80°C/);
  const exact = page.waitForRequest(request => {
    const link = new URL(request.url());
    return link.pathname === '/api/trade' && link.searchParams.get('at') === origin &&
      link.searchParams.get('forecast') === '1' &&
      Object.entries(identity).every(([key, value]) => link.searchParams.get(key) === value);
  });
  await page.reload();
  await exact;
  await page.waitForURL(reopened =>
    [...address.searchParams].every(([key, value]) => reopened.searchParams.get(key) === value));
  await hours.first().waitFor();
  assert.equal(address.searchParams.get('station'), event.station);
  assert.match(await hours.first().innerText(), /51°F/);
  assert.doesNotMatch(await page.locator('#forecast-outlook').innerText(), /177°F/);
  assert.doesNotMatch(await page.locator(selector).textContent(), /80°C/);
  console.log('ES2022 chart band vertices, capture options, airport winner and pinned Forecast ahead:',
    band.length, 80, '11°C', page.url());
  await page.screenshot({path: join(out, 'forecast-ahead.png')});
}

async function monthlyRain(page) {
  const reports = ['2026-11-02T12:00:00Z', '2026-11-02T12:00:00Z', '2026-11-05T12:00:00Z']
    .map((published_at, index) => ({ published_at, climate_day: published_at.slice(0, 10),
      amount_in: index + 1, monthly_raw: String(index + 1) }));
  await page.route('**/api/markets/detail?**', r => r.fulfill({ json: {
    instrument_id: 'monthly-rain-control', venue: 'kalshi', family: 'monthly-rain', status: 'closed',
    monthly_rain: { reports, climate_start: '2026-11-01T00:00:00Z', climate_end: '2026-11-06T00:00:00Z', threshold_in: 2 },
  } }));
  await page.goto(url('markets', { venue: 'kalshi', instrument: 'monthly-rain-control' }));
  const chart = page.locator('#monthly-rain-chart');
  await chart.waitFor();
  const width = (await chart.boundingBox()).width;
  await chart.click({position: {x: width / 2, y: 20}});
  assert.equal(await page.locator('#rain-edition').inputValue(), '1');
  assert.match(await page.locator('#rain-edition-summary').innerText(), /2 in/);
  await chart.click({position: {x: 32, y: 20}});
  assert.equal(await page.locator('#rain-edition').inputValue(), '0');
  console.log('Monthly rain chart selected the last equal-clock edition and clamped before the first report');
}

async function focus(page, broken) {
  const fixtureQuery = new URLSearchParams({fixture: 1, ...identity});
  const sample = await json(await page.request.get(base + '/api/trade?' + fixtureQuery));
  assert.deepEqual([sample.instrument, sample.side, sample.quote_at, sample.posted.price, sample.posted.quantity],
    [instrument, 'YES', '2026-11-15T17:10:00Z', 0.12, 45]);
  const board = await json(await page.request.get(base + '/api/trade?fixture=1'));
  const session = await json(await page.request.get(base + '/api/session?mode=live&fixture=1'));
  let receipt = 1;
  await page.route('**/api/session?**', r => r.fulfill({json: session}));
  await page.route('**/api/trade?**', r => {
    const query = new URL(r.request().url()).searchParams;
    if (!query.has('instrument')) return r.fulfill({json: {...board, fixture: false}});
    assert.deepEqual(Object.fromEntries(Object.keys(identity).map(key => [key, query.get(key)])), identity);
    return r.fulfill({json: { ...sample, fixture: false, valid_for: 120, book_receipt_id: receipt,
      review_key: String(receipt), quote_at: `2026-11-15T17:10:0${receipt}Z` }});
  });
  await page.clock.install();
  await page.goto(url('trade', identity));
  const summary = page.locator('#trade-depth-summary');
  await summary.click();
  await page.locator('#trade-depth').getByText('Book receipt #1', {exact: true}).waitFor();
  await summary.focus();
  await summary.evaluate((element, block) => {
    window.originalSummary = element;
    if (!block) return;
    const focus = HTMLElement.prototype.focus;
    HTMLElement.prototype.focus = function (...args) {
      if (this.id !== element.id || this === element) focus.apply(this, args);
    };
  }, broken);
  receipt = 2;
  await page.clock.runFor(8250);
  await page.locator('#trade-depth').getByText('Book receipt #2', {exact: true}).waitFor();
  assert.equal(await page.locator('#trade-depth').evaluate(el => el.open), true);
  assert.equal(await summary.evaluate(el => el !== window.originalSummary), true);
  assert.equal(await summary.evaluate(el => el === document.activeElement), true, 'Exact ticket lost focus after receipt changed');
  console.log('Trade refresh kept Book evidence open and focused, receipt #1 → #2');
}

function radarAudit(broken) {
  const gl = WebGLRenderingContext.prototype;
  const canvas = CanvasRenderingContext2D.prototype;
  const uniforms = new WeakMap();
  window.radarAudit = { linked: null, draws: 0, fallback: 0, shaderError: null };
  const { shaderSource, linkProgram, getUniformLocation, uniform1i, drawArrays } = gl;
  gl.shaderSource = function (shader, source) {
    if (broken && source.includes('showRadar')) source += '\ninvalid_radar_shader;\n';
    return shaderSource.call(this, shader, source);
  };
  gl.linkProgram = function (program) {
    linkProgram.call(this, program);
    window.radarAudit.linked = this.getProgramParameter(program, this.LINK_STATUS);
  };
  gl.getUniformLocation = function (program, name) {
    const location = getUniformLocation.call(this, program, name);
    if (location) uniforms.set(location, name);
    return location;
  };
  gl.uniform1i = function (location, value) {
    if (uniforms.get(location) === 'showRadar') window.radarOn = value;
    return uniform1i.call(this, location, value);
  };
  gl.drawArrays = function (...args) {
    drawArrays.apply(this, args);
    if (window.radarOn) window.radarAudit.draws++;
  };
  const put = canvas.putImageData;
  canvas.putImageData = function (...args) {
    put.apply(this, args);
    window.radarAudit.fallback++;
  };
  const warn = console.warn;
  console.warn = (...args) => {
    if (args[0] === 'WebGL globe fallback') window.radarAudit.shaderError = String(args[1]);
    warn(...args);
  };
}

async function radar(page, gpu, broken) {
  for (const [at, frame, lon, lat, value, published] of [
    ['18:01:00', '2026111217-f000', -73.97, 40.78, '1.10 mm', '17:02:42'],
    ['18:08:00', '2026111218-f000', -73.97, 40.78, '4.62 mm', '18:02:44'],
    ['18:08:00', '2026111218-f000', -67, 27, 'Unavailable', '18:02:44'],
    ['18:08:00', '2026111218-f000', -121.49, 38.58, '0 mm', '18:02:44'],
  ]) {
    await page.goto(url('weather', {mode: 'replay', day: '2026-11-12', station: 'KNYC',
      at: '2026-11-12T' + at + 'Z', layers: '', probe_lon: lon, probe_lat: lat}));
    await page.waitForFunction(() => !document.querySelector('#time').disabled);
    const startup = await page.evaluate(() => window.radarAudit);
    const toggle = page.locator('#radar-toggle');
    assert.equal(await toggle.isChecked(), false);
    const before = await page.locator('#world').screenshot();
    await page.locator('.layer-options summary').click();
    const frameResponse = page.waitForResponse(r => r.url() === base + '/api/radar/' + frame);
    await toggle.check();
    const raw = await json(await frameResponse);
    assert.equal(raw.id, frame);
    await page.locator('#probe-panel').getByText(value, {exact: true}).waitFor();
    assert.equal(Date.parse(await page.locator('#probe-panel time').getAttribute('datetime')),
      Date.parse('2026-11-12T' + published + 'Z'));
    await page.waitForFunction(([gpu, previous]) => gpu ? window.radarAudit.draws > 0 :
      window.radarAudit.fallback > previous, [gpu && !broken, startup.fallback]);
    if (lon === -73.97) {
      assert.notDeepEqual(await page.locator('#world').screenshot(), before);
      await page.screenshot({path: join(out, `${mode}-${frame}.png`)});
    }
    console.log('Radar', frame, lon, lat, value, JSON.stringify(await page.evaluate(() => window.radarAudit)));
    if (broken) assert.match(startup.shaderError, /invalid_radar_shader|syntax error/);
    assert.equal(startup.linked, gpu ? true : null, 'GPU check requires a linked WebGL program even if Canvas 2D draws');
  }
}

async function retained(page) {
  const read = page.waitForResponse(r => r.url().startsWith(base + '/api/backtests/' + miami));
  await page.goto(url('backtest', {run: miami}));
  const run = await json(await read);
  assert.deepEqual([run.id, run.signals, run.fills], [miami, 3, 1]);
  await page.locator('#backtest-result').getByText(miami, {exact: true}).waitFor();
  await page.screenshot({path: join(out, 'retained-miami.png')});
  console.log('Retained Miami Backtest', miami, run.signals, 'signals,', run.fills, 'fill');
}

async function main() {
  const gpu = mode !== 'fallback';
  const browser = await chromium.launch({ channel: 'chrome', args: ['--disable-background-networking', '--no-proxy-server',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
    ...(gpu ? ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] : ['--disable-webgl'])] });
  try {
    const ctx = await browser.newContext({viewport: {width: 1350, height: 1050}, serviceWorkers: 'block'});
    const external = [], errors = [];
    await ctx.route('**/*', r => {
      if (r.request().url().startsWith(base + '/')) return r.fallback();
      external.push(r.request().url());
      return r.abort();
    });
    await ctx.addInitScript(onlyReverse => {
      for (const method of onlyReverse ? ['toReversed'] : ['toReversed', 'toSorted', 'findLastIndex']) {
        delete Array.prototype[method];
        if (typeof Array.prototype[method] !== 'undefined') throw new Error(method + ' still present');
      }
    }, Boolean(process.env.BASELINE_BUNDLE));
    if (['compatibility', 'leaked-clock'].includes(mode)) await ctx.addInitScript(chartAudit);
    if (['radar', 'fallback', 'broken-shader'].includes(mode)) await ctx.addInitScript(radarAudit, mode === 'broken-shader');
    const page = await ctx.newPage();
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => {
      errors.push(error.message);
      console.error('Chrome page error:', error.message);
    });
    if (process.env.BASELINE_BUNDLE) {
      await page.route(/\/app-[A-Z0-9]+\.js$/, r => r.fulfill({
        contentType: 'text/javascript', path: process.env.BASELINE_BUNDLE,
      }));
    }
    // Check the actual JS response Chrome used, including on the installed listener.
    const loaded = page.waitForResponse(r => new URL(r.url()).pathname.match(/^\/app-[A-Z0-9]+\.js$/));
    await page.goto(url('backtest'));
    const response = await loaded;
    assert.equal(response.ok(), true);
    const asset = new URL(response.url()).pathname.slice(1);
    const bundle = await response.body();
    assert.equal(hash(bundle), hash(readFileSync(process.env.BASELINE_BUNDLE || join('dist', asset))));
    console.log('Chrome', browser.version(), base, asset, hash(bundle));
    const check = {empty, backtest, compatibility: async page => { await compatibility(page); await monthlyRain(page); },
      'leaked-clock': page => compatibility(page, true), retained,
      focus: page => focus(page, false), 'broken-focus': page => focus(page, true),
      radar: page => radar(page, true, false), fallback: page => radar(page, false, false),
      'broken-shader': page => radar(page, true, true)}[mode];
    await check(page);
    assert.deepEqual(external, [], 'Browser attempted non-loopback requests');
    assert.deepEqual(errors, [], 'Uncaught browser errors');
    console.log('Browser external requests:', external.length);
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
