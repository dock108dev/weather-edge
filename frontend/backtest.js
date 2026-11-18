const q = selector => document.querySelector(selector);
const STRATEGIES = {
  'two-sided': 'YES or NO with positive net EV',
  'two-sided-buffered': 'YES or NO with a probability buffer',
  'no-trade': 'Hold cash',
};

export function startBacktest(params, { get, post, esc, when, money, pct, table, row, stat }) {
  const fixture = params.get('fixture') === '1';
  const form = q('#backtest-form');
  const fields = form.elements;
  const source = q('#backtest-source');
  const status = q('#backtest-status');
  const saved = q('#backtest-saved');
  const host = q('#backtest-result');
  const format = value => Number(value ?? 0).toLocaleString('en-US');
  const clock = value => value ? when(value, 'UTC', 'day') + ' · ' + when(value, 'UTC', 'seconds') : '—';
  const strategy = key => STRATEGIES[key] || key;
  let generation = 0;

  function url(view, query = {}) {
    return `/?${new URLSearchParams({ view, ...(fixture ? { fixture: '1' } : {}), ...query })}`;
  }

  function endpoint(path, query = {}) {
    return path + '?' + new URLSearchParams({ ...(fixture ? { fixture: '1' } : {}), ...query });
  }

  function updateInputs() {
    const dateRange = !fixture && source.value === 'range';
    for (const field of form.querySelectorAll('.backtest-range')) {
      field.hidden = !dateRange;
      field.querySelector('input').disabled = !dateRange;
    }
    q('#backtest-capture-field').hidden = fixture || dateRange;
    fields.capture_at.disabled = fixture || dateRange;
    fields.capture_at.required = !fields.capture_at.disabled;
  }

  function rememberRun(run) {
    if (![...saved.options].some(option => option.value === run.id)) {
      saved.add(new Option(`${run.id.slice(0, 8)} · ${strategy(run.evaluation.strategy)} · ${run.fills} fills`, run.id), 1);
    }
    saved.value = run.id;
  }

  async function loadSources() {
    const data = await get(endpoint('/api/backtests'));
    fields.capture_at.replaceChildren(...data.captures.map(at => new Option(clock(at), at)));
    if (!data.captures.length) source.querySelector('[value="capture"]').disabled = true;
    for (const run of data.runs) if (![...saved.options].some(option => option.value === run.id)) {
      saved.add(new Option(`${run.id.slice(0, 8)} · ${clock(run.as_of)} · ${run.fills} fills`, run.id));
    }
    q('#backtest-assumptions').textContent = fixture
      ? 'This synthetic capture uses its own saved results and balance. The 5-second recheck shows both fill and skip behavior.'
      : `Choose up to ${data.max_days} UTC dates or one exact retained capture. Every run starts with $500 and a $25 station-day cap. Real quotes require a confirmed origin response and a fresh exchange image.`;
  }

  function caseDetail(detail) {
    if (!detail) return '';
    const { attempt, position, reading, rule } = detail;
    const recheck = attempt.reaction_quote;
    const timeline = url('weather', {
      mode: 'live', archived: '1', history_mode: 'live',
      day: attempt.date, station: attempt.station, venue: detail.venue, instrument: attempt.bucket_id,
      side: attempt.side, at: attempt.signal_at,
    });
    const price = value => Number.isFinite(value) ? `${(value * 100).toFixed(2)}¢` : '—';
    const quote = quote => quote ? [
      format(quote.quantity), price(quote.entry_price), money(quote.fee), money(quote.max_loss),
      pct(quote.break_even_probability, 1), money(quote.ev_dollars),
    ] : ['—', '—', '—', '—', '—', '—'];
    return `<section class="backtest-case" aria-labelledby="backtest-case-title">
      <h3 id="backtest-case-title" tabindex="-1">${esc(attempt.station)} · ${esc(attempt.label)} · ${esc(attempt.side)}</h3>
      <p>${esc(attempt.bucket_id)} · ${esc(attempt.date)} · ${esc(attempt.result)}</p>
      ${rule ? `<p>${esc(rule)}</p>` : ''}
      <dl>
        <div><dt>Qualified signal</dt><dd>${esc(clock(attempt.signal_at))} · ${pct(attempt.probability, 1)} modeled chance of ${esc(attempt.side)}</dd></div>
        <div><dt>Model vintage</dt><dd>${esc(attempt.model_version || 'Unavailable')}${attempt.model_vintage ? ' · ' + esc(attempt.model_vintage) : ''}</dd></div>
        ${attempt.model_availability ? `<div><dt>Model availability evidence</dt>
          <dd>${esc(clock(attempt.model_availability.available_at))} · ${esc(attempt.model_availability.basis)}
          ${attempt.model_availability.source.run_id ? ' · Trial ' + esc(attempt.model_availability.source.run_id) : ''}
          <br>Receipt <code>${esc(attempt.model_availability.receipt_sha256)}</code>
          ${attempt.model_evidence_cutoff ? '<br>Training evidence cutoff ' + esc(attempt.model_evidence_cutoff) : ''}
          </dd></div>` : ''}
        <div><dt>Forecast at signal</dt><dd>Issued ${esc(clock(attempt.forecast_issued_at))} · Available ${esc(clock(attempt.forecast_available_at))}</dd></div>
        <div><dt>Latest known reading</dt><dd>${reading ? esc(`${reading.temp_f}°F · observed ${clock(reading.at)} · received ${clock(reading.available_at)}`) : 'No reading retained at the signal'}</dd></div>
        <div><dt>Delayed recheck</dt><dd>Due ${esc(clock(attempt.reaction_due_at))} · ${recheck ?
          'Observed ' + esc(clock(attempt.reaction_at)) + ' · ' + pct(recheck.probability, 1) + ' modeled chance' : 'No eligible recheck in this result'}</dd></div>
      </dl>
      ${table(['Stage', 'Contracts', 'Weighted entry', 'Fees', 'All-in cost', 'Break-even', 'Net EV'],
        [['Signal', attempt], ['Recheck', recheck], ...(position ? [['Simulated fill', position]] : [])].map(([label, item]) => row([label, ...quote(item)])))}
      ${Object.keys(attempt.recheck_gaps || {}).length ? `<p>Excluded rechecks · ${Object.entries(attempt.recheck_gaps)
        .map(([reason, n]) => `${esc(reason)} (${format(n)})`).join(' · ')}</p>` : ''}
      <p>${position?.settled ? `Verified payout ${money(position.payout)} became known ${esc(clock(position.settlement_available_at))}.`
        : position ? 'Simulated fill · payout unresolved at the end of this run.' : 'No simulated fill for this signal.'}</p>
      ${detail.books.length ? `<details><summary>Exact book receipts</summary>
        ${table(['Local receipt', 'First app availability', 'Exchange image', 'YES offers (price × size)', 'YES bids (price × size)'], detail.books.map(book =>
          row([clock(book.at), clock(book.available_at), clock(book.source_at), book.offers?.map(level => level.join(' × ')).join(' · '),
            book.bids?.map(level => level.join(' × ')).join(' · ')])))}</details>` : ''}
      <a href="${esc(timeline)}">Open weather at the signal</a>
    </section>`;
  }

  function render(run, focusCase) {
    const { evaluation, coverage, settings } = run;
    const archiveDays = evaluation.archive_station_days;
    const summary = stat('Simulated fills', format(run.fills)) + stat('Skipped signals', format(run.skipped))
      + stat('Awaiting recheck', format(run.awaiting)) + stat('Unresolved payouts', format(run.unresolved_payouts))
      + stat('Realized P&L', money(run.realized_pnl));
    const noFills = !run.fills ? (evaluation.strategy === 'no-trade' ? 'The cash baseline holds the opening balance.'
      : !coverage.book_receipts ? 'No executable book receipts were retained in this window.'
        : !coverage.eligible_books ? 'No retained side passed the paper evidence checks.'
          : !coverage.modeled_books ? 'No eligible book had odds from a model available at that decision.'
            : 'No order cleared the strategy and delayed recheck in this window.') : '';
    host.hidden = false;
    host.innerHTML = `<h2 tabindex="-1">Backtest result</h2>
      <p class="backtest-id"><strong>${esc(strategy(evaluation.strategy))}</strong> · ${fixture ? 'Synthetic fixture' :
        evaluation.source === 'capture' ? 'Exact retained capture' : 'Retained UTC date range'} · Saved ID <code>${esc(run.id)}</code></p>
      <p>${esc(clock(evaluation.from))} to ${esc(clock(evaluation.through))} · ${money(settings.order_size)} max order ·
        ${settings.reaction_seconds}s reaction · ${money(run.cash_committed)} open simulated cost ·
        ${money(run.cash)} available. ${esc(noFills)}</p>
      <div class="backtest-metrics">${summary}</div>
      <p>${format(coverage.book_receipts)} book receipts across ${format(coverage.station_days)} station-days ·
        ${format(coverage.eligible_books)} eligible books · ${format(coverage.modeled_books)} with model odds · ${format(run.signals)} signals.
        ${archiveDays.length ? `${format(run.display_samples)} archived display samples across ${format(archiveDays.length)} archived station-days supply zero executable fills.` : ''}</p>
      ${Object.entries(run.eligibility_rejections || { books: coverage.rejected_books || {} }).map(([phase, counts]) =>
        Object.keys(counts).length ? `<p>${phase === 'signal' ? 'Signal exclusions' : phase === 'recheck' ? 'Recheck exclusions' : 'Excluded books'}
        · ${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([reason, n]) => `${esc(reason)} (${format(n)})`).join(' · ')}</p>` : '').join('')}
      ${run.signal_rejections?.length ? `<details><summary>Rejected sides</summary>
        ${table(['Contract', 'Side', 'Decision UTC', 'Posted offer', 'Reason', 'Applied cutoff UTC'], run.signal_rejections.map(item =>
          row([item.bucket_id, item.side, clock(item.at), pct(item.posted_price, 1), item.reason, clock(item.closes_at)])))}</details>` : ''}
      ${run.comparison ? `<p>Evaluated from <a href="${esc(url('backtest', { run: run.source_run }))}">${esc(run.source_run)}</a>.
        Signals ${run.comparison.signals.join(' → ')} · fills ${run.comparison.fills.join(' → ')}.</p>
        ${Object.entries(run.evidence_gaps).map(([key, value]) => `<p>${esc({
          books_without_first_app_availability: 'Books with no first app availability',
          instruments_without_retained_listing_revisions: 'Instruments with no witnessed listing revisions',
          probability_lookups_without_frozen_clock: 'Required model clocks absent from the freeze',
        }[key])} · ${format(typeof value === 'number' ? value : value.length)}</p>`).join('')}
        ${table(['Contract', 'Side', 'Book receipt UTC', 'Original', 'Current'], run.comparison.changed_decisions.map(item =>
          row([item.instrument, item.side, clock(item.book_at), item.original, item.current]))) }` : ''}
      ${run.engine === 'book-recheck-v2' ? '<button type="button" class="small-button" id="backtest-evaluate">Evaluate frozen evidence under current rules</button>' : ''}
      ${run.settled ? `<p>${format(run.settled)} settled fills. Matched cash baseline ${money(run.cash_baseline)} available, $0.00 realized P&amp;L.</p>` : ''}
      <details><summary>Frozen assumptions and coverage</summary>
        <p>${esc(run.engine)} · ${esc(run.clock_basis === 'app-availability' ? 'First app availability' : 'HTTP receipt')} ·
        ${esc(evaluation.freshness)} book policy${evaluation.max_exchange_age ? ` · exchange image age up to ${evaluation.max_exchange_age}s` : ''}.
        Max station-day exposure ${money(settings.max_station_day)}. Model vintages ${esc(run.model_vintages.join(' · ') || 'Unavailable')}.</p>
        ${table(['Fee schedule effective UTC', 'Taker coefficient'], run.fee_schedule.map(fee => row([fee.from, fee.coefficient])))}
        ${archiveDays.length ? table(['Archived day', 'Station', 'Display samples'], archiveDays.map(day =>
          row([day.day, day.station, format(day.display_samples)]))) : ''}
        ${Object.entries(evaluation.receipt_issues).map(([reason, n]) => `<p>${esc(reason)} · ${format(n)}</p>`).join('')}
      </details>
      ${caseDetail(run.case)}
      ${run.cases.length ? `<h3>Review a signal</h3>
        <ul class="backtest-signals">${run.cases.map(item => `<li><a data-backtest-case="${item.index}"
          href="${esc(url('backtest', { run: run.id, case: item.index }))}">${esc(item.station)} · ${esc(item.label)} · ${esc(item.side)}
          <small>${esc(clock(item.signal_at))} · ${esc(item.result)}</small></a></li>`).join('')}</ul>` : ''}
      ${run.next_offset == null ? '' : `<button type="button" class="small-button" id="backtest-next" data-offset="${run.next_offset}">Next signals</button>`}`;
    rememberRun(run);
    if (focusCase) q('#backtest-case-title')?.focus();
    for (const link of host.querySelectorAll('[data-backtest-case]')) {
      link.onclick = event => {
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
        event.preventDefault();
        openRun(run.id, { case: link.dataset.backtestCase });
      };
    }
    q('#backtest-next')?.addEventListener('click', event => openRun(run.id, { offset: event.currentTarget.dataset.offset }));
    q('#backtest-evaluate')?.addEventListener('click', async event => {
      const button = event.currentTarget;
      const request = ++generation;
      button.disabled = true;
      status.textContent = 'Evaluating frozen evidence…';
      try {
        const result = await post('/api/backtests/' + encodeURIComponent(run.id) + '/evaluate', { fixture });
        if (request === generation) await openRun(result.id);
      } catch (error) {
        if (request === generation) status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  async function openRun(id, query = {}) {
    if (!id) return;
    const request = ++generation;
    status.textContent = 'Opening saved backtest…';
    try {
      const run = await get(endpoint('/api/backtests/' + encodeURIComponent(id), query));
      if (generation !== request) return;
      history.replaceState(null, '', url('backtest', { run: id, ...query }));
      status.textContent = '';
      render(run, query.case != null);
    } catch (error) {
      if (generation === request) status.textContent = error.message;
    }
  }

  source.onchange = updateInputs;
  updateInputs();
  if (fixture) {
    q('#backtest-source-field').hidden = true;
    q('#backtest-subtitle').textContent = 'Synthetic depth and weather with a separate saved backtest history.';
    q('#backtest-fixture-link').textContent = 'Use retained real evidence';
    q('#backtest-fixture-link').href = '/?view=backtest';
    fields.reaction.value = '5';
  }
  saved.onchange = () => openRun(saved.value);
  q('#backtest-open').onsubmit = event => {
    event.preventDefault();
    openRun(event.currentTarget.elements.run.value);
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const request = ++generation;
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    host.hidden = true;
    status.textContent = 'Running strategy on retained evidence…';
    const { start, end, capture_at, strategy, size, reaction } = Object.fromEntries(new FormData(form));
    try {
      const run = await post('/api/backtests', { fixture, start, end, capture_at, strategy, size: Number(size), reaction: Number(reaction) });
      if (request !== generation) return;
      history.replaceState(null, '', url('backtest', { run: run.id }));
      status.textContent = 'Backtest saved.';
      render(run);
      q('#backtest-result h2').focus();
    } catch (error) {
      if (request === generation) status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  loadSources().catch(error => { q('#backtest-assumptions').textContent = error.message; });
  if (params.has('run')) openRun(params.get('run'), Object.fromEntries([...params].filter(([key]) => ['case', 'offset'].includes(key))));
}
