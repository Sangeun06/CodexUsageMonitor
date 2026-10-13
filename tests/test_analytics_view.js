const test = require('node:test');
const assert = require('node:assert/strict');
const { segments, chartMarkup, donutMarkup, breakdownRows } = require('../static/analytics-view.js');
const colors = new Map([['a', '#00cc99'], ['b', '#cc99ff']]);
const day = { date: '2026-10-06', users: { a: 100, b: 200 }, total_tokens: 300, account_tokens: 1000, untracked_tokens: 700, comparable: true };

test('daily slices preserve account denominator including untracked', () => {
  const parts = segments(day);
  assert.equal(parts.reduce((sum, part) => sum + part.tokens, 0), 1000);
  assert.deepEqual(parts.map(part => part.share), [20, 10, 70]);
});
test('unknown account does not produce invented untracked slices', () => {
  const parts = segments({ ...day, account_tokens: null, comparable: false, untracked_tokens: null });
  assert.equal(parts.length, 2);
  assert.equal(parts.reduce((sum, part) => sum + part.tokens, 0), 300);
  assert.match(donutMarkup({ ...day, comparable: false }, colors), /사용자 확인량만/);
});
test('empty day has a neutral ring and missing bar, not a fabricated full pie', () => {
  const empty = { ...day, users: {}, total_tokens: 0, account_tokens: null, untracked_tokens: null, comparable: false };
  assert.deepEqual(segments(empty), []);
  assert.match(chartMarkup([empty], colors, empty.date), /missing-bar/);
  assert.doesNotMatch(donutMarkup(empty, colors), /stroke-dasharray/);
});
test('chart has focusable dates, token axis and selection state', () => {
  const svg = chartMarkup([day], colors, day.date);
  assert.match(svg, /data-date="2026-10-06"/);
  assert.match(svg, /role="button" tabindex="0"/);
  assert.match(svg, /aria-pressed="true"/);
  assert.match(svg, /10\/06/);
});
test('explicit account zero uses a zero marker rather than the missing-data dash', () => {
  const zero = { ...day, users: {}, total_tokens: 0, account_tokens: 0, untracked_tokens: 0 };
  const svg = chartMarkup([zero], colors, zero.date);
  assert.match(svg, /계정 보고량 0/);
  assert.doesNotMatch(svg, /class="missing-bar"/);
});
test('user-controlled labels are escaped in tooltips and date inspector', () => {
  const nodes = new Map([['a', {display_name: '<img onerror=evil>', hostname: 'host'}]]);
  const html = breakdownRows(day, nodes, colors);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /700/);
});
test('weekly overview includes undated users and the canonical untracked share', () => {
  const overview = { ...day, date: '__weekly', scope: 'weekly' };
  assert.deepEqual(segments(overview).map(part => part.share), [20, 10, 70]);
  assert.match(donutMarkup(overview, colors), /주간 전체 집계량/);
  assert.match(breakdownRows(overview, new Map(), colors), /700<\/td><td>70\.0%/);
  assert.match(chartMarkup([day], colors, '__weekly'), /tabindex="0"/);
});
test('weekly overview with an unknown denominator does not normalize users to 100%', () => {
  const overview = { ...day, date: '__weekly', scope: 'weekly', account_tokens: null, untracked_tokens: null, comparable: false };
  assert.deepEqual(segments(overview).map(part => part.share), [null, null]);
  assert.doesNotMatch(donutMarkup(overview, colors), /stroke-dasharray/);
  assert.match(breakdownRows(overview, new Map(), colors), /계산 대기/);
});
test('observed growth exposes dated untracked usage as an explicitly labelled lower bound', () => {
  const observed = { ...day, account_basis: 'observed_delta' };
  const svg = chartMarkup([observed], colors, observed.date);
  assert.match(svg, /계정 관측 최소량 1,000/);
  assert.match(svg, /fill="#f4c96b"/);
  assert.match(donutMarkup(observed, colors), /계정 관측 최소량/);
});
