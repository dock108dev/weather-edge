const VENUES = { 'polymarket-us': 'Polymarket US', kalshi: 'Kalshi' };
const BOOK_LANES = {
  'Polymarket US books': ['polymarket-us', 'Polymarket US · Daily highs'],
  'Polymarket US low and rain books': ['polymarket-us', 'Polymarket US · Low and rain'],
  'Kalshi public books': ['kalshi', 'Kalshi · Public books'],
};
const BLOCKERS = {
  stale: 'stale books', freshness: 'unconfirmed book freshness', depth: 'missing usable depth',
  model: 'uncertain model odds', archived: 'closing or closed markets',
  fees: 'unverified fees', edge: 'no positive edge after fees', venue: 'unavailable venue rechecks',
  listing: 'listing restrictions', clock: 'unverified book availability',
};
const q = selector => document.querySelector(selector);
const usd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4,
});
const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const key = row => `${row.venue}|${row.instrument}|${row.side}`;
const identity = row => ({ venue: row.venue, instrument: row.instrument, side: row.side });
const listReasons = new Intl.ListFormat('en-US', { style: 'long', type: 'conjunction' });
const ageLabel = seconds => !Number.isFinite(seconds) ? '—' : seconds < 60
  ? `${Math.max(0, Math.floor(seconds))}s`
  : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m`;
const MAX_HIGHLIGHTS = 3;
const riskGroup = row => [row.station || row.instrument, row.day, row.family].join('|');

export function startTrade(params, {
  get, post, esc, when, money, pct, signed, imageLag, selectWeather, onBoard, initialHistory, clockReady,
}) {
  const fixture = params.get('fixture') === '1';
  const fixtureQuery = fixture ? { fixture: '1' } : {};
  let selected = params.has('instrument') && params.has('venue')
    ? { venue: params.get('venue'), instrument: params.get('instrument'), side: params.get('side') === 'NO' ? 'NO' : 'YES' } : null;
  let historical = initialHistory;
  let board, boardError, ticket, ticketError, ledger, ledgerError, timelineRows = [], sourceHealth = {};
  let boardBusy = false, ledgerBusy = false, ticketBusy = false, saving = false, fetchingBook = false;
  let ticketGeneration = 0, intent, limit = 8, lastList = '', clockTimer, pinKey = '';
  let contextKey = JSON.stringify([historical, clockReady]);
  let boardGeneration = 0, showRelated = false, explicitSelection = !!selected;
  let collectionState, priceLane = '';
  let reviewedCard;
  let tab = historical ? 'watch' : ['watch', 'prices'].includes(params.get('tab')) ? params.get('tab') : 'ready';
  let budget = 25;
  const list = q('#trade-list');
  const quoteHost = q('#trade-quote');
  const size = q('#trade-budget');
  const feedback = q('#trade-feedback');

  const endpoint = (path, query = {}) => `${path}?${new URLSearchParams({ ...fixtureQuery, ...query })}`;
  const price = n => Number.isFinite(n) ? `${(n * 100).toFixed(2).replace(/\.?0+$/, '')}¢` : '—';
  const quantity = n => Number.isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
  const shownAge = (row, elapsed = 0) => row.receipt_age == null ? null : row.receipt_age + (fixture || row.archived ? 0 : elapsed);
  const age = (row, elapsed = 0) => ageLabel(shownAge(row, elapsed));
  const elapsed = reading => reading ? (performance.now() - reading.loaded) / 1000 : 0;
  const fresh = (row, reading) => row.valid_for != null && (fixture || elapsed(reading) <= row.valid_for);
  const current = row => !historical && row?.order && row.budget === budget && fresh(row, ticket);
  const reviewable = row => row?.review_key && current(row);
  const setFeedback = (message, warning = false) => {
    feedback.textContent = message;
    feedback.hidden = !message;
    feedback.dataset.warning = String(warning);
  };

  function updateUrl() {
    const patch = new URLSearchParams(location.search);
    for (const name of ['venue', 'instrument', 'side', 'bucket', 'archived', 'history_mode', 'forecast', 'at', 'tab']) patch.delete(name);
    if (tab !== 'ready') patch.set('tab', tab);
    if (selected) {
      if (!historical) patch.set('mode', 'live');
      if (ticket && key(ticket) === key(selected) && ticket.station && ticket.day) {
        patch.set('station', ticket.station);
        patch.set('day', ticket.day);
      }
      for (const [name, value] of Object.entries({ ...selected, ...historical })) patch.set(name, value);
    }
    const url = patch.size ? `/?${patch}` : '/';
    if (location.pathname + location.search !== url) history.replaceState(null, '', url + location.hash);
  }

  function marketLink(path, row, extra = {}) {
    return `/?${new URLSearchParams({ view: path, ...fixtureQuery, venue: row.venue, instrument: row.instrument, ...extra })}`;
  }

  function setTab(next) {
    tab = next;
    limit = 8;
    updateUrl();
    renderBoard();
  }

  function renderBoard() {
    q('#trade-preview-jump').hidden = !selected;
    q('#trade-ready').hidden = !!historical;
    q('#trade-quotes').hidden = !!historical;
    q('#trade-controls').hidden = !!historical;
    q('#trade-refresh').hidden = !!historical;
    if (historical) q('#practice-result').hidden = true;
    const view = historical ? 'watch' : tab;
    q('#trade-heading').textContent = historical?.forecast ? 'At the forecast origin' : historical ? 'Replay markets' : 'Opportunities';
    q('#trade-ready').textContent = `Ready${board?.budget === budget ? ' · ' + board.ready.filter(boardReady).length : ''}`;
    q('#trade-quotes').setAttribute('aria-pressed', String(view === 'prices'));
    q('#trade-watch').hidden = !!historical;
    q('#trade-ready').setAttribute('aria-pressed', String(view === 'ready'));
    q('#trade-watch').setAttribute('aria-pressed', String(view === 'watch'));
    q('#trade-filters').hidden = view === 'ready' || !!historical;
    const feeds = q('#trade-feeds');
    feeds.hidden = !!historical || fixture || !board?.lane_coverage;
    if (!feeds.hidden) {
      const content = Object.entries(BOOK_LANES).map(([lane, [venue, label]]) => {
        const evidence = board.lane_coverage[lane];
        const source = sourceHealth[lane];
        const status = { partial: 'Partial response', error: 'Source error', empty: 'Empty selection',
          empty_depth: 'Returned ladders have no usable prices', interrupted: 'Interrupted by Stop' };
        const health = [collectionState === 'stopped' && 'Collection stopped',
          collectionState === 'offline' && 'Worker offline', status[source?.outcome],
          source?.data_stale && 'Publication stale',
          source?.last_receipt_at ? 'Latest lane receipt ' + when(source.last_receipt_at, localZone, 'clock')
            : source?.last_success ? 'Last check ' + when(source.last_success, localZone, 'clock') : 'Book check unavailable',
        ].filter(Boolean).join(' · ');
        return `<button class="trade-feed" type="button" data-price-venue="${venue}" data-lane="${esc(lane)}"
          aria-pressed="${priceLane === lane}"><strong>${label}</strong>
          <span>${evidence?.sides ? `${evidence.sides} posted sides · latest local receipt ${esc(when(evidence.latest,
            localZone, 'clock'))}` : 'No posted prices in this view'}</span><span>${esc(health)}</span></button>`;
      }).join('');
      if (feeds.innerHTML !== content) feeds.innerHTML = content;
      for (const control of feeds.querySelectorAll('button')) control.onclick = () => {
        priceLane = priceLane === control.dataset.lane ? '' : control.dataset.lane;
        q('#trade-venue').value = priceLane ? control.dataset.priceVenue : '';
        q('#trade-search').value = '';
        tabTouched = true;
        setTab('prices');
      };
    }
    let markup;
    let eligible = [];
    if (!historical && (!board || board.budget !== budget || boardError)) {
      markup = `<p class="trade-empty">${esc(boardError || 'Reading retained books and model evidence…')}</p>`;
    } else {
      eligible = historical ? [] : board.ready.filter(boardReady);
      if (!historical) {
        const currentPins = board.at + '|' + eligible.map(key).join('|');
        if (currentPins !== pinKey) {
          pinKey = currentPins;
          onBoard({ ...board, ready: eligible });
        }
      }
      const aged = historical ? [] : board.ready.filter(row => !boardReady(row)).map(row => ticketSupersedes(row)
        ? { ...row, ...ticket, order: null, frozenAge: elapsed(board) - elapsed(ticket) }
        : { ...row, blocker: 'Book evidence expired', category: 'stale', order: null });
      const search = q('#trade-search').value.toLowerCase().trim();
      const venue = q('#trade-venue').value;
      const agedBySide = new Map(aged.map(row => [key(row), row]));
      const prices = historical ? [] : board.prices.map(row => agedBySide.get(key(row)) || row);
      const marketRows = historical ? timelineRows : view === 'prices' ? prices : [...aged, ...board.watch];
      const groups = new Set();
      const highlights = eligible.filter(row => {
        const group = riskGroup(row);
        if (groups.has(group)) return false;
        groups.add(group);
        return true;
      }).slice(0, MAX_HIGHLIGHTS);
      const rows = view === 'ready' ? (showRelated ? eligible : highlights) : marketRows.filter(row => historical ||
        (!venue || venue === row.venue) && (view !== 'prices' || !priceLane || row.book_lane === priceLane) && (!search ||
          [row.place, row.station, row.title, row.label, row.instrument, row.day].some(v => v?.toLowerCase().includes(search))));
      const blockers = {};
      for (const item of prices) if (item.category) blockers[item.category] = (blockers[item.category] || 0) + 1;
      const reasons = Object.entries(blockers).sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([kind]) => BLOCKERS[kind] || kind);
      const empty = view === 'ready'
        ? `<div class="trade-empty"><strong>No paper opportunities qualify right now.</strong>
          <p>${esc(reasons.length ? 'Common blockers are ' + listReasons.format(reasons) + '.'
            : 'No retained offers are available to qualify.')}</p>
          <button id="trade-open-prices" class="small-button" type="button">Browse exchange prices</button></div>`
        : `<p class="trade-empty">${historical ? 'Select a market pin or station with timeline evidence.'
          : 'No matching cases. Search Market Archive for earlier contracts.'}</p>`;
      const recent = view === 'ready' && !eligible.length && !fixture
        ? Object.keys(VENUES).map(venue => prices.find(row => row.venue === venue)).filter(Boolean) : [];
      const cards = (rows.length ? rows.slice(0, limit) : recent).map(row => {
        const order = row.order;
        const showPosted = row.posted && (row.blocker || view === 'prices');
        const title = `${row.place || row.station || row.venue} · ${row.label}`;
        const relativeAge = value => Number.isFinite(value) ? value - (row.frozenAge ?? 0) : null;
        const receiptAge = relativeAge(showPosted ? row.posted_receipt_age : row.receipt_age);
        const sourceAge = relativeAge(showPosted ? row.posted_exchange_age : row.exchange_age);
        const relative = (value, label) => Number.isFinite(value)
          ? `${label} <span data-trade-age="${value}">${ageLabel(value)}</span>` : '';
        return `<li><button type="button" class="trade-candidate" data-trade-key="${esc(key(row))}"
            aria-pressed="${selected && key(selected) === key(row) ? 'true' : 'false'}">
          <span class="candidate-kicker"><span>${esc(VENUES[row.venue])} · ${esc(row.day || 'Date unavailable')}</span>
            ${row.blocker ? `<span class="trade-tag trade-tag-watch">${historical ? 'As of timeline' : row.market_open ? 'Open market' : 'Watch'}</span>` : ''}</span>
          <span class="candidate-title">${esc(title)} <span class="trade-side" data-side="${row.side}">${row.side}</span></span>
          <span class="candidate-meaning">${esc(row.meaning)}</span>
          ${showPosted ? `<span class="candidate-posted"><strong>${price(row.posted.price)}</strong>
            ${esc(row.posted.kind)}${row.posted.quantity != null ? ' · ' + quantity(row.posted.quantity) + ' at price' : ''}
            ${row.posted.at ? ' · ' + esc(when(row.posted.at, row.timezone, 'seconds')) : ''}</span>` : ''}
          ${row.blocker ? `<span class="candidate-blocker">${esc(row.blocker)}</span>` :
            `<span class="candidate-metrics">
              <span><strong>${price(order.break_even_probability)}</strong> all-in · ${quantity(order.quantity)} contracts${order.partial ? ' · partial size' : ''}</span>
              <span><strong class="good">${signed(order.ev_dollars)}</strong> net EV · ${usd.format(order.max_loss)} max loss</span>
              <span>${pct(row.probability, 1)} modeled chance vs. ${pct(order.break_even_probability, 1)} to break even</span>
            </span>`}
          <span class="candidate-meta"><span>${[
            relative(receiptAge, 'Local receipt'), relative(sourceAge, 'Exchange image') ||
            (row.posted?.kind === 'Book offer' ? 'Exchange image time unavailable' : ''),
            row.posted_image_state === 'stale' ? 'Stale image' : '',
            row.posted?.kind === 'Book offer' && !row.posted_available_at ? 'App availability unwitnessed' : '',
          ].filter(Boolean).join(' · ')}</span>
            <strong>${row.blocker ? 'Inspect evidence' : 'Review order'} <span aria-hidden="true">→</span></strong></span>
          <code>${esc(row.instrument)}</code>
        </button></li>`;
      });
      const checked = esc(when(historical?.at || board.at, localZone, 'seconds'));
      markup = `<p class="trade-list-summary">${view === 'ready'
        ? (eligible.length ? `Highest estimated net EV at ${money(budget)}` : `Checked ${checked}`)
        : `${rows.length} sides · ${checked}`}</p>
        ${view === 'prices' ? '<p class="trade-list-summary price-basis">Posted exchange prices and receipt times. Ready shows orders that qualify.</p>' : ''}
        ${rows.length ? `<ol class="trade-candidates" data-stack="${view === 'ready' && !showRelated}">${cards.join('')}</ol>` : empty}
        ${view === 'ready' && eligible.length > highlights.length ? `<div class="trade-more">
          <button id="trade-related" class="small-button" type="button" aria-expanded="${showRelated}">
          ${showRelated ? 'Show highlights' : 'Show all ' + eligible.length + ' qualified sides'}</button>
          <span>Highlights show one side per station, date and weather measure. Other sides can share the same weather risk.</span></div>` : ''}
        ${recent.length ? `<p class="trade-list-summary price-basis">Latest posted prices</p>
          <ol class="trade-candidates">${cards.join('')}</ol>` : ''}
        ${rows.length > limit ? '<div class="trade-more"><button id="trade-more" class="small-button" type="button">Show more sides</button></div>' : ''}`;
      if (lastList !== markup) {
        const previous = document.activeElement?.dataset.tradeKey;
        list.innerHTML = markup;
        for (const button of list.querySelectorAll('[data-trade-key]')) {
          const row = (rows.length ? rows : recent).find(candidate => key(candidate) === button.dataset.tradeKey);
          button.onclick = () => choose(row, { focus: true });
          if (button.dataset.tradeKey === previous) button.focus({ preventScroll: true });
        }
        q('#trade-more')?.addEventListener('click', () => {
          limit += 8;
          renderBoard();
          const firstNew = list.querySelectorAll('[data-trade-key]')[limit - 8];
          firstNew?.focus();
        });
        q('#trade-related')?.addEventListener('click', () => {
          showRelated = !showRelated;
          limit = 8;
          renderBoard();
          q('#trade-related')?.focus();
        });
        q('#trade-open-prices')?.addEventListener('click', () => {
          tabTouched = true;
          setTab('prices');
          q('#trade-search').focus();
        });
      }
    }
    if (!historical && (!board || board.budget !== budget || boardError) && lastList !== markup) list.innerHTML = markup;
    lastList = markup;
    for (const clock of list.querySelectorAll('[data-trade-age]')) {
      clock.textContent = ageLabel(Number(clock.dataset.tradeAge) + (fixture || historical ? 0 : elapsed(board)));
    }
  }

  function ticketSupersedes(row) {
    return !historical && ticket?.blocker && ticket.budget === board.budget && key(row) === key(ticket) &&
      Date.parse(ticket.at) >= Date.parse(board.at);
  }

  function boardReady(row) {
    return fresh(row, board) && !ticketSupersedes(row);
  }

  function renderTicket() {
    const row = ticket;
    if (!row) {
      q('#trade-identity').innerHTML = `<p class="trade-empty">${esc(ticketError || (selected
        ? clockReady ? 'Reading the exact contract and side…' : 'Waiting for the saved timeline…'
        : 'Choose an opportunity or a posted price to see its exact contract here.'))}</p>`;
      quoteHost.innerHTML = '';
      delete quoteHost.dataset.ticket;
      return;
    }
    const sameTicket = quoteHost.dataset.ticket === key(row);
    const active = document.activeElement;
    const previous = sameTicket && quoteHost.contains(active) ? active.id : '';
    const disclosure = id => sameTicket && q('#' + id)?.open ? 'open' : '';
    const contract = `<div class="trade-contract">
      <div><span class="trade-venue">${esc(VENUES[row.venue])}</span><h3>${esc(row.label)}</h3>
      <p>${esc(row.title)}${row.station ? ' · ' + esc(row.place || row.station) : ''}</p></div>
      <div class="mode-picker ticket-sides" role="group" aria-label="Order side">${['YES','NO'].map(side =>
        `<button type="button" id="trade-side-${side}" data-preview-side="${side}" aria-pressed="${row.side === side}">${side}</button>`).join('')}</div>
      <p class="trade-meaning"><span class="trade-side" data-side="${row.side}">${row.side}</span> ${esc(row.meaning)}</p>
      <code class="trade-instrument">${esc(row.instrument)}</code>
    </div>`;
    const identityHost = q('#trade-identity');
    if (identityHost.innerHTML !== contract) identityHost.innerHTML = contract;
    for (const button of identityHost.querySelectorAll('[data-preview-side]')) {
      button.onclick = () => choose({ ...identity(row), side: button.dataset.previewSide }, { retainHistorical: true });
    }
    const order = !ticketError && (current(row) || (row.archived && row.venue === 'kalshi' && row.budget === budget))
      ? row.order : null;
    const blocker = ticketError || row.blocker || (row.budget !== budget ? 'Repricing the order…' :
      !order ? 'Book evidence expired. Refresh to review the latest quote.' : null);
    const gap = row.model_gap && row.model_gap !== blocker ? row.model_gap : '';
    const liveReady = !blocker && reviewable(row);
    const status = `<div class="ticket-status ${blocker ? 'ticket-status-watch' : ''}">
      <strong>${blocker ? esc(blocker) : 'Ready to review'}</strong>
      ${gap ? `<span>${esc(gap)}</span>` : ''}
      ${row.fee_evidence?.reason && !blocker?.includes(row.fee_evidence.reason) ? `<span>${esc(row.fee_evidence.reason)}</span>` : ''}
      ${row.source_trace?.exchange_image_state === 'stale' ? '<span>Exchange image stale at the evaluation clock</span>' : ''}
      ${liveReady && !fixture ? `<span>Evidence valid for up to <strong id="ticket-valid-for">${ageLabel(Math.max(0, row.valid_for - elapsed(ticket)))}</strong></span>` : ''}
      ${row.retained_origin ? '<span>The latest receipt repeated this image. The eligibility clock still uses the original origin receipt.</span>' : ''}
      ${row.source_trace?.recheck?.reason && row.source_trace.recheck.reason !== blocker ?
        `<span>Paper recheck · ${esc(row.source_trace.recheck.reason)}</span>` : ''}
    </div>`;
    const posted = row.posted && !order ? `<p class="ticket-posted"><strong>${price(row.posted.price)}</strong>
      ${esc(row.posted.kind)}${row.posted.quantity != null ? ' · ' + quantity(row.posted.quantity) + ' at price' : ''}
      · ${esc(when(row.posted.at, row.timezone, 'seconds'))}</p>` : '';
    const metrics = order ? `<dl class="trade-metrics">
        ${[['Potential payout', usd.format(order.payout)], ['Quantity', quantity(order.quantity) + ' contracts'],
          ['Weighted entry', price(order.entry_price)], ['Highest price used', price(order.highest_price)],
          ['Estimated fees', money(order.fee)], ['All-in per contract', price(order.break_even_probability)],
          ['Modeled probability', pct(row.probability, 1)],
          [row.uncertain ? 'Model EV (uncertain)' : 'Estimated net EV', signed(order.ev_dollars)],
          ['Total displayed size', quantity(order.available_size)]].map(([name, value]) =>
          `<div><dt>${name}</dt><dd>${value}</dd></div>`).join('')}
      </dl>${order.partial ? `<p class="trade-partial"><strong>Partial size</strong> · ${esc(order.partial)}.
        ${money(row.budget - order.max_loss)} of the budget is unused.</p>` : ''}
      ${row.venue === 'kalshi' ? '<p class="trade-help">Indicative whole-contract estimate using Kalshi’s fee schedule and cent-aligned balance.</p>' : ''}` :
      `${posted}<div class="trade-help">
        ${row.probability != null ? `Model probability ${pct(row.probability, 1)}${row.uncertain ? ' · uncertain' : ''}` : ''}</div>`;
    const failedTransport = row.source_trace?.reviewed_book?.http_status === 0;
    const differentOffer = ['Book offer', 'Retained book offer'].includes(row.posted?.kind)
      && row.posted.at !== row.quote_at;
    const evidence = `<div class="trade-evidence">
      ${row.book_lane ? `<div><span>Book lane</span><strong>${esc(BOOK_LANES[row.book_lane]?.[1] || row.book_lane)}</strong></div>` : ''}
      ${differentOffer ? `<div><span>Posted offer local receipt</span><strong>${esc(when(row.posted.at, row.timezone, 'seconds'))}</strong></div>
        <div><span>Posted offer first app availability</span><strong>${esc(row.posted_available_at
          ? when(row.posted_available_at, row.timezone, 'seconds') : 'Unwitnessed')}</strong></div>
        <div><span>Posted offer exchange image</span><strong>${esc(row.posted_source_at ? when(row.posted_source_at, row.timezone, 'seconds') : 'Time unavailable')}</strong></div>` : ''}
      <div><span>${failedTransport ? 'Request failure recorded' : row.retained_origin ? 'Origin receipt used for order'
        : differentOffer ? 'Book checked for paper eligibility' : 'Local book receipt'}</span><strong>${esc(when(row.quote_at, row.timezone, 'seconds'))}${row.quote_at
        ? ` · <span id="ticket-age">${esc(age(row, elapsed(ticket)))}</span> old` : ''}</strong></div>
      ${row.quote_at ? `<div><span>First app availability</span><strong>${esc(row.quote_available_at
        ? when(row.quote_available_at, row.timezone, 'seconds') : 'Unwitnessed')}</strong></div>
        <div><span>Exchange image</span><strong>${row.quote_source_at ? `${esc(when(row.quote_source_at, row.timezone, 'seconds'))}
        · <span id="ticket-image-age">${esc(ageLabel(row.exchange_age))}</span> old` : 'Timestamp unavailable'}</strong></div>` : ''}
      <div><span>Expected settlement</span><strong>${esc(when(row.settles_at, row.timezone))}${row.settles_at ? ' local' : ''}</strong></div>
      ${row.forecast_at ? `<div><span>Model input issued</span><strong>${esc(when(row.forecast_at, row.timezone))} local</strong></div>` : ''}
    </div>`;
    const used = order?.fills?.length ? `<p><strong>Depth used</strong> ${order.fills.map(([p, n]) => `${quantity(n)} at ${price(p)}`).join(' · ')}</p>` : '';
    const depth = row.levels?.length ? `<p><strong>Retained offers for ${row.side}</strong> ${row.levels.map(([p, n]) => `${quantity(n)} at ${price(p)}`).join(' · ')}</p>` : '';
    const lag = imageLag(row.quote_at, row.quote_source_at);
    const trace = row.source_trace;
    const batchLine = (label, target) => target ? `<p><strong>${label}</strong> · ${esc(target.scope || '')}
      ${target.batch_id == null ? '' : ' · Batch #' + esc(target.batch_id)}
      <br>${esc(({usable: 'Usable depth returned', missing_or_invalid: 'Requested book missing or invalid',
        empty_depth: 'Valid response with empty depth', failed: 'Book request failed',
        unrequested: 'Selected, request unwitnessed', outside_sample: 'Outside priority sample',
        unobserved: 'Selection unwitnessed'})[target.state] || target.state)}${target.reason ? ' · ' + esc(target.reason) : ''}
      ${target.receipt_id == null ? '' : ' · Receipt #' + esc(target.receipt_id)}
      ${target.requested_at ? '<br>Requested ' + esc(target.requested_at) : ''}
      ${target.received_at ? '<br>Received ' + esc(target.received_at) : ''}</p>` : '';
    const proof = trace || used || depth || lag ? `<details id="trade-depth" class="trade-details" ${disclosure('trade-depth')}>
      <summary id="trade-depth-summary">Book evidence</summary>${used}${depth}
      ${batchLine('Automatic collection', trace?.automatic_selection)}
      ${batchLine('Manual public fetch', trace?.manual_request)}
      ${trace?.observed_book?.reason ? `<p>Latest exact book outcome · ${esc(trace.observed_book.reason)}</p>` : ''}
      ${trace?.observed_book?.cache_status ? `<p>Latest HTTP cache status · ${esc(trace.observed_book.cache_status)}</p>` : ''}
      ${lag ? `<p>Exchange image ${esc(lag)} than local receipt.</p>` : ''}
      ${row.cache_age != null ? `<p>HTTP cache Age ${row.cache_age}s.</p>` : ''}
      ${row.book_receipt_id != null ? `<p>Book receipt #${esc(row.book_receipt_id)}</p>` : ''}</details>` : '';
    const listing = row.listing_evidence;
    const fees = row.fee_evidence;
    const payout = row.verified_payout;
    const showReceipt = receipt => receipt ? `#${esc(receipt.receipt_id)} · ${esc(receipt.received_at)}
      <br><code>${esc(receipt.source)}</code>` : 'Unavailable by this clock';
    const termsProof = listing ? `<details id="trade-terms" class="trade-details" ${disclosure('trade-terms')}>
      <summary id="trade-terms-summary">Listing and fee evidence</summary>
      <p><strong>Decision clock</strong> ${esc(row.at)}</p>
      ${listing.receipt_id != null ? `<p><strong>Listing receipt</strong> ${showReceipt(listing)}</p>` : ''}
      ${listing.status === 'verified' ? `<p>Event <code>${esc(listing.event_id)}</code> · native event
        <code>${esc(listing.native_event_id)}</code><br>Native market <code>${esc(listing.native_id)}</code> ·
        ${esc(row.side)} side <code>${esc(row.side_id)}</code></p>
        <p>Scheduled market opening · ${esc(listing.opened_at || 'Unavailable')}<br>
        Listed event end · ${esc(listing.listed_event_end_at || 'Unavailable')}<br>
        Native market close · ${esc(listing.closed_at || 'Unavailable')}<br>
        Native listing state · ${esc(listing.native_status || 'Unavailable')}</p>` :
        `<p>${esc(listing.reason)}${listing.conflicting_receipts ? ` · Receipts ${esc(listing.conflicting_receipts.join(', '))}` : ''}</p>`}
      ${fees ? `<p><strong>Fee terms</strong> · ${esc(fees.available ? 'Supported at the decision clock' : fees.reason)}
        <br>${esc(fees.policy_basis)}
        ${fees.policy_warning ? `<br>${esc(fees.policy_warning)} <a href="${esc(fees.policy_warning_source)}"
          target="_blank" rel="noopener noreferrer">Filing ↗</a>` : ''}<br><a href="${esc(fees.schedule)}" target="_blank" rel="noopener noreferrer">Venue fee schedule ↗</a>
        ${fees.rounding ? ` · <a href="${esc(fees.rounding)}" target="_blank" rel="noopener noreferrer">Rounding ↗</a>` : ''}</p>
        ${row.venue === 'kalshi' ? `<p>Series terms receipt ${showReceipt(fees.series_receipt)}<br>
          Series fee history ${showReceipt(fees.series_change_proof)}<br>
          Complete event fee history · ${fees.event_change_receipts?.length ?
            fees.event_change_receipts.map(showReceipt).join('<br>') : 'Unavailable by this clock'}
          ${fees.event_receipt ? `<br>Event terms receipt ${showReceipt(fees.event_receipt)}` : ''}</p>
          ${fees.available ? `<p>Taker rate ${esc(fees.taker_rate)} × multiplier ${esc(fees.multiplier)}</p>` : ''}
          <p>Series change effective · ${esc(fees.effective_change || 'None applied')}<br>
          Event override effective · ${esc(fees.event_effective_change || 'None applied')}<br>
          Instrument waiver expires · ${esc(fees.fee_waiver_expiration_at || 'Not listed')}</p>` :
          `<p>Listed coefficient · ${esc(fees.advertised_coefficient ?? 'Unavailable')}<br>
          Paper coefficient · ${esc(fees.policy_coefficient)}<br>
          Policy boundary · ${esc(fees.policy_effective_at === '0001-01-01T00:00:00Z'
            ? 'Earlier paper policy; start unverified' : fees.policy_effective_at)}<br>
          Fee lock · ${esc(fees.lock_at)}</p>`}` : ''}
    </details>` : '';
    const payoutProof = payout ? `<p class="trade-help"><strong>Verified payout · ${esc(payout.side)} ${usd.format(Number(payout.side_payout))}</strong>
      <br>Available ${esc(payout.available_at)}${payout.exchange_settled_at ? ` · Exchange settled ${esc(payout.exchange_settled_at)}` : ''}
      ${payout.receipt_id != null ? ` · Receipt #${esc(payout.receipt_id)}` : ''}<br><code>${esc(payout.source)}</code></p>` : '';
    const receipt = row.model_availability;
    const modelProof = receipt ? `<details id="trade-model" class="trade-details" ${disclosure('trade-model')}>
      <summary id="trade-model-summary">Model evidence</summary><p>Artifact <code>${esc(row.model_vintage)}</code>
      <br>Availability ${esc(receipt.available_at)} · ${esc(receipt.basis)}
      <br>Receipt <code>${esc(receipt.receipt_sha256)}</code>
      <br>Training evidence cutoff ${esc(row.model_evidence_cutoff)}
      <br>Forecast issued ${esc(row.forecast_at)} · available ${esc(row.forecast_available_at)}</p></details>` : '';
    const weather = row.station && row.day && ['daily-high', 'daily-low', 'daily-rain'].includes(row.family);
    const references = `<div class="trade-reference">
        ${weather ? `<a id="trade-weather-link" href="${esc(marketLink('weather', row, {
          mode: !historical || historical.history_mode === 'live' ? 'live' : 'replay',
          ...historical, day: row.day, station: row.station, side: row.side, from_trade: '1',
        }))}" data-ticket-weather>Forecast &amp; station readings ↓</a>` : ''}
      ${fixture ? '' : `<a id="trade-archive-link" href="${esc(marketLink('markets', row))}">Market Archive →</a>`}
    </div>`;
    const rule = row.rule ? `<details id="trade-rule" class="trade-details" ${disclosure('trade-rule')}>
      <summary id="trade-rule-summary">Contract rule</summary><p>${esc(row.rule)}</p>
      ${row.rules_secondary ? `<p>${esc(row.rules_secondary)}</p>` : ''}</details>` : '';
    const exposure = ledger?.exposure.find(group => group.station === row.station && group.day === row.day)?.cost ?? 0;
    const paperRoom = ledger && Math.max(0, Math.min(ledger.cash, ledger.station_limit - exposure));
    const noRoom = liveReady && paperRoom != null && order.max_loss > paperRoom + 1e-8;
    const remainingBudget = noRoom ? Math.floor((paperRoom + 1e-8) * 100) / 100 : null;
    const allowed = liveReady && Number(size.value) === row.budget && !!ledger && !ledgerError && !noRoom && !saving;
    const exchange = !fixture && !historical?.forecast && row.exchange_url ? `<a id="trade-venue-link" class="${liveReady ? 'small-button' : 'action-button'}" target="_blank"
      rel="noopener noreferrer" href="${esc(row.exchange_url)}">${row.market_open ? 'Open' : 'View'} on ${VENUES[row.venue]} <span aria-hidden="true">↗</span></a>` : '';
    quoteHost.innerHTML = `${status}
      ${order ? `<p class="ticket-risk"><strong>${usd.format(order.max_loss)}</strong> maximum loss at the previewed prices
        ${row.blocker ? ' · indicative' : ''}</p>` : posted}
      <div class="trade-actions">
        ${liveReady ? `<button class="action-button" id="record-paper-order" type="button"
          data-budget-allowed="${!!ledger && !ledgerError && !noRoom}" ${allowed ? '' : 'disabled'}>Record paper order</button>` : ''}
        ${exchange}
        ${!fixture ? '<button class="small-button" id="copy-trade-order" type="button">Copy details</button>' : ''}
        ${!fixture && !row.archived && row.market_open ? `<button class="small-button" id="public-trade-book" type="button"
          ${fetchingBook ? 'disabled' : ''}>${fetchingBook ? 'Fetching book…' : 'Fetch public book'}</button>` : ''}
      </div>
      ${exchange ? `<p class="trade-help">Exchange event page · Select <strong>${esc(row.label)} · ${row.side}</strong>
        ${row.station ? ` for ${esc(row.place || row.station)} (${esc(row.station)})` : ''}.</p>` : ''}
      ${!exchange && !fixture && !historical?.forecast ? '<p class="trade-help">Public event link unavailable for this contract.</p>' : ''}
      ${exchange && order && order.cost_at_limit > order.max_loss
        ? `<p class="trade-help">If all ${quantity(order.quantity)} contracts fill at ${price(order.highest_price)}, estimated cost is ${usd.format(order.cost_at_limit)} including fees.</p>` : ''}
      ${noRoom ? `<p class="trade-help">${usd.format(paperRoom)} available for this station-day in the paper ledger.</p>
        ${remainingBudget >= Number(size.min) ? `<button class="small-button" id="use-paper-room" type="button">
          Reprice with ${money(remainingBudget)} available</button>`
          : '<p class="trade-help">The remaining paper balance is below the minimum order budget.</p>'}` : ''}
      ${liveReady ? `<p class="trade-help">Paper fills require a later captured book. ${fixture ? 'Uses the retained fixture recheck.' : 'Recheck begins after 30 seconds.'}</p>` : ''}
      ${order ? metrics : ''}${evidence}${references}${rule}${proof}${termsProof}${payoutProof}${modelProof}`;
    quoteHost.dataset.ticket = key(row);
    if (previous) {
      const replacement = document.getElementById(previous);
      if (previous === 'record-paper-order' && (!replacement || replacement.disabled)) {
        q('#trade-ticket-heading').focus({ preventScroll: true });
        if (blocker) setFeedback(blocker, true);
      } else if (quoteHost.contains(replacement)) replacement.focus({ preventScroll: true });
    }
    q('#record-paper-order')?.addEventListener('click', saveOrder);
    q('#use-paper-room')?.addEventListener('click', () => {
      size.value = remainingBudget.toFixed(2);
      q('#trade-size-form').requestSubmit();
    });
    q('#public-trade-book')?.addEventListener('click', fetchPublicBook);
    q('#copy-trade-order')?.addEventListener('click', async () => {
      const summary = [VENUES[row.venue], row.title, row.label, row.instrument, row.side,
        order && `${quantity(order.quantity)} contracts · highest price ${price(order.highest_price)}${liveReady ? '' : ' · indicative'}`,
        order && order.cost_at_limit > order.max_loss && `Estimated cost if all fill at that price ${usd.format(order.cost_at_limit)}`,
        row.exchange_url].filter(Boolean).join('\n');
      try {
        await navigator.clipboard.writeText(summary);
        setFeedback('Contract details copied.');
      } catch {
        setFeedback('Clipboard unavailable. The contract and side are shown above.', true);
      }
    });
  }

  async function fetchPublicBook() {
    if (!ticket || fetchingBook || historical) return;
    const row = ticket;
    fetchingBook = true;
    renderTicket();
    try {
      const result = await post('/api/trade/book', identity(row));
      await Promise.all([refreshBoard(), refreshTicket()]);
      if (selected && key(selected) === key(row)) setFeedback(result.received
        ? 'Public book received. Its evidence status is shown above.' : 'The exchange returned no usable book.', !result.received);
    } catch (error) {
      if (selected && key(selected) === key(row)) setFeedback(error.message, true);
    } finally {
      fetchingBook = false;
      renderTicket();
    }
  }

  function choose(row, { focus = false, notify = true } = {}) {
    if (focus) {
      explicitSelection = true;
      const heading = q('#trade-ticket-heading');
      if (innerWidth <= 600) {
        heading.focus({ preventScroll: true });
        q('.trade-ticket').scrollIntoView({ block: 'start' });
      } else heading.focus();
    }
    if (selected && key(selected) === key(row) && ticket && !ticketError) return;
    reviewedCard = !historical && row.review_key && !row.blocker ? row : null;
    const selection = identity(row);
    selected = selection;
    ticket = null;
    ticketError = '';
    setFeedback('');
    updateUrl();
    renderBoard();
    renderTicket();
    // Read the actionable ticket before asking the weather session to expand a
    // station timeline. Both paths can wait on the same capture snapshot.
    clearTimeout(clockTimer);
    const opening = refreshTicket();
    if (notify) selectWeather(row, opening);
  }

  async function refreshBoard(force = false) {
    if ((boardBusy && !force) || historical) return;
    const generation = ++boardGeneration;
    const started = performance.now();
    boardBusy = true;
    try {
      const result = await get(endpoint('/api/trade', { budget }));
      if (generation !== boardGeneration) return;
      board = { ...result, loaded: started };
      boardError = '';
    } catch (error) {
      if (generation !== boardGeneration) return;
      boardError = `Opportunity evidence unavailable. ${error.message}`;
    } finally {
      if (generation === boardGeneration) {
        boardBusy = false;
        renderBoard();
      }
    }
  }

  async function refreshTicket() {
    if (!selected || saving || !clockReady) return;
    const generation = ++ticketGeneration;
    const started = performance.now();
    ticketBusy = true;
    try {
      const row = await get(endpoint('/api/trade', { ...selected, ...historical, budget }));
      if (generation !== ticketGeneration) return;
      const reviewed = ticket || reviewedCard;
      if (reviewed?.review_key && (reviewed.budget ?? budget) === row.budget && reviewed.review_key !== row.review_key) {
        setFeedback(row.blocker ? `Eligibility changed while opening or refreshing this ticket. ${row.blocker}.`
          : 'Quote changed. Review the updated size and cost.', true);
      }
      reviewedCard = null;
      ticket = { ...row, loaded: started };
      if (row.station && row.day) updateUrl();
      ticketError = '';
      renderBoard();
      if (params.has('instrument') && !historical && row.blocker && !board?.ready.some(item => key(item) === key(row))
        && !params.has('tab') && !tabTouched) setTab('prices');
    } catch (error) {
      if (generation !== ticketGeneration) return;
      ticketError = `Could not refresh this side. ${error.message}`;
    } finally {
      if (generation === ticketGeneration) {
        ticketBusy = false;
        renderTicket();
      }
    }
  }

  async function refreshLedger() {
    if (ledgerBusy) return;
    ledgerBusy = true;
    try {
      ledger = await get(endpoint('/api/trade/orders'));
      ledgerError = '';
    } catch (error) {
      ledgerError = `Paper ledger unavailable. ${error.message}`;
    } finally {
      ledgerBusy = false;
      renderLedger();
      if (ticket && !ticket.archived && !historical) renderTicket();
    }
  }

  function renderLedger() {
    q('#trade-balance').textContent = ledger && !ledgerError
      ? `${money(ledger.cash)} available · ${money(ledger.committed)} reserved or committed · $25 station-day cap` : '';
    const host = q('#trade-orders');
    if (ledgerError || !ledger?.orders.length) {
      host.innerHTML = `<p class="trade-empty">${esc(ledgerError || 'No single paper orders recorded.')}</p>`;
      return;
    }
    host.innerHTML = `<ul class="trade-order-list">${ledger.orders.map(record => {
      const row = record.ticket;
      const position = record.result?.position;
      const attempt = record.result?.attempt;
      const settlement = position?.settled ? ` · Settled · ${money(position.payout)} payout` : '';
      const result = position
        ? `${quantity(position.quantity)} contracts · ${usd.format(position.max_loss)} total cost · ${usd.format(position.quantity)} max payout${settlement}` :
        record.result?.reason || `Recheck due ${when(record.reaction_due_at, row.timezone, 'seconds')}`;
      return `<li><div><strong>${esc(row.label)} · ${row.side}</strong>
        <small>${VENUES[row.venue]} · ${esc(row.instrument)}</small></div>
        <div><strong data-order-status="${esc(record.status)}">${esc(record.status)}</strong><small>${esc(result)}</small></div>
        ${attempt ? `<details><summary>Paper evidence</summary>
          <p>Signal book ${esc(when(attempt.quote_at, row.timezone, 'seconds'))} ·
          Recheck decision ${esc(when(attempt.reaction_at, row.timezone, 'seconds'))} · Ledger run <code>${esc(record.result.run_id)}</code></p>
          ${attempt.reaction_quote?.quote_at ? `<p>Recheck HTTP receipt ${esc(when(attempt.reaction_quote.quote_at, row.timezone, 'seconds'))}</p>` : ''}
          ${attempt.reaction_quote?.fills?.length ? `<p>Rechecked depth ${attempt.reaction_quote.fills
            .map(([p,n]) => `${quantity(n)} at ${price(p)}`).join(' · ')}</p>` : ''}
          ${Object.keys(attempt.recheck_gaps || {}).length ? `<p>Excluded rechecks · ${esc(Object.entries(attempt.recheck_gaps)
            .map(([reason, count]) => `${reason} (${count})`).join(' · '))}</p>` : ''}</details>` : ''}
      </li>`;
    }).join('')}</ul>`;
  }

  async function saveOrder() {
    if (saving || !reviewable(ticket) || Number(size.value) !== ticket.budget) return;
    const row = ticket;
    const remembered = `weather-edge-paper:${fixture ? 'fixture' : 'live'}:${row.review_key}`;
    const requestId = sessionStorage.getItem(remembered) || crypto.randomUUID();
    sessionStorage.setItem(remembered, requestId);
    saving = true;
    renderTicket();
    try {
      const record = await post('/api/trade/orders', {
        ...identity(row), fixture, budget: row.budget, review_key: row.review_key, request_id: requestId,
      });
      intent = record.id;
      setFeedback(`${record.status} · ${record.result?.reason || 'Saved to Paper orders below.'}`);
      await refreshLedger();
      showIntent();
    } catch (error) {
      setFeedback(error.message, true);
    } finally {
      saving = false;
      refreshTicket();
    }
  }

  function showIntent() {
    const record = ledger?.orders.find(order => order.id === intent);
    if (!record || !selected || key(record.ticket) !== key(selected)) return;
    if (record.status !== 'Pending recheck') {
      setFeedback(`${record.status} · ${record.result.reason}. See Paper orders below.`, record.status === 'Skipped');
      intent = null;
    }
  }

  let tabTouched = false;
  q('#trade-ready').onclick = () => { tabTouched = true; setTab('ready'); };
  q('#trade-quotes').onclick = () => { tabTouched = true; setTab('prices'); };
  q('#trade-watch').onclick = () => { tabTouched = true; setTab('watch'); };
  q('#trade-search').oninput = () => { limit = 8; renderBoard(); };
  q('#trade-venue').onchange = () => { priceLane = ''; limit = 8; renderBoard(); };
  q('#trade-size-form').onsubmit = event => {
    event.preventDefault();
    budget = Number(size.value);
    setFeedback('');
    renderBoard();
    renderTicket();
    refreshBoard(true);
    refreshTicket();
  };
  size.oninput = () => {
    const action = q('#record-paper-order');
    if (action) action.disabled = Number(size.value) !== ticket?.budget || action.dataset.budgetAllowed !== 'true' || saving;
  };
  q('#trade-refresh').onclick = () => {
    refreshBoard();
    refreshTicket();
    if (!historical) refreshLedger().then(showIntent);
  };
  q('#practice-order').onclick = async () => {
    const button = q('#practice-order');
    const result = q('#practice-result');
    button.disabled = true;
    result.hidden = false;
    result.textContent = 'Checking the synthetic recheck…';
    try {
      const { ticket, position, reason, starting_balance, available_balance } = await post('/api/trade/practice', {});
      result.textContent = position
        ? `Simulated fill · ${ticket.place}, ${ticket.label}°F ${ticket.side}. ${quantity(position.quantity)} contracts, ${money(position.max_loss)} cost including fees. Demo balance ${money(starting_balance)} → ${money(available_balance)}.`
        : `Practice order skipped · ${reason}. Demo balance ${money(available_balance)}.`;
    } catch (error) {
      result.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  q('#trade-fixture').hidden = !fixture;
  if (!fixture) setInterval(() => {
    if (document.hidden) return;
    refreshBoard();
    if (!historical && !ticketBusy) refreshTicket();
  }, 8000);
  setInterval(() => {
    if (document.hidden) return;
    renderBoard();
    if (ticket?.order && !ticket.archived && !current(ticket) && q('.trade-metrics')) renderTicket();
    const clock = q('#ticket-age');
    if (clock && ticket) clock.textContent = age(ticket, elapsed(ticket));
    const imageClock = q('#ticket-image-age');
    if (imageClock && ticket) imageClock.textContent = ageLabel(ticket.exchange_age + (fixture || ticket.archived ? 0 : elapsed(ticket)));
    const reviewClock = q('#ticket-valid-for');
    if (reviewClock) reviewClock.textContent = ageLabel(Math.max(0, ticket.valid_for - elapsed(ticket)));
  }, 1000);
  setInterval(() => {
    if (!document.hidden && (ledger?.active || ledgerError)) refreshLedger().then(showIntent);
  }, 3500);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshLedger().then(showIntent);
  });
  renderBoard();
  renderTicket();
  refreshBoard();
  refreshTicket();
  refreshLedger();
  return {
    hasSelection: () => !!selected,
    hasExplicitSelection: () => explicitSelection,
    selection: () => selected,
    showStation(station) {
      if (historical) return;
      q('#trade-search').value = station;
      q('#trade-venue').value = '';
      priceLane = '';
      tabTouched = true;
      setTab('prices');
    },
    setSourceHealth(sources, state) {
      sourceHealth = sources || {};
      collectionState = state;
      renderBoard();
    },
    align(row, focus, notify = false) {
      if (row) choose(row, { focus, notify });
      else if (selected) {
        selected = ticket = null;
        ticketGeneration += 1;
        renderTicket();
        renderBoard();
        updateUrl();
      }
    },
    setContext(clock, rows, ready) {
      const next = JSON.stringify([clock, ready]);
      const wasHistorical = !!historical;
      historical = clock;
      clockReady = ready;
      timelineRows = rows;
      if (next === contextKey) {
        if (historical) renderBoard();
        return;
      }
      contextKey = next;
      clearTimeout(clockTimer);
      if (wasHistorical !== !!historical || historical) {
        ticketGeneration += 1;
        ticketBusy = false;
        ticket = null;
        if (clockReady && selected) updateUrl();
        renderTicket();
        renderBoard();
        if (!historical) refreshBoard();
        if (clockReady) clockTimer = setTimeout(refreshTicket, historical ? 220 : 0);
      }
    },
  };
}
