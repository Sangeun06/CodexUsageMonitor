const test = require('node:test');
const assert = require('node:assert/strict');
const { segments, chartMarkup, donutMarkup, breakdownRows, quotaReport } = require('../static/analytics-view.js');
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

test('weekly quota charts and inspector share actual allowance percentages', () => {
  const quota = { used_percent: 10, remaining_percent: 90, unattributed_percent: 5,
    user_usage: [{node_key: 'a', used_percent: 1}, {node_key: 'b', used_percent: 4}],
    days: { '2026-10-06': { used_percent: 5, unattributed_percent: 0, users: {a: 1, b: 4} } } };
  const report = quotaReport({ days: [day], overview: {...day, scope: 'weekly', date: '__weekly'} }, quota);
  const overview = report.overview;
  assert.deepEqual(segments(overview).map(part => part.share), [4, 1, 5, 90]);
  assert.match(donutMarkup(overview, colors), /10\.0%/);
  assert.match(breakdownRows(overview, new Map(), colors), /100<\/td><td>1\.0%/);
  const daily = report.days[0];
  assert.deepEqual(segments(daily).map(part => part.share), [4, 1]);
  assert.match(chartMarkup([daily], colors, daily.date), /5\.0%/);
  assert.match(donutMarkup(daily, colors), /당일 관측 소모/);
  assert.doesNotMatch(donutMarkup(daily, colors), /stroke-dasharray="80/);
});

test('token-only dates keep missing quota unknown rather than drawing token shares', () => {
  const report = quotaReport({ days: [day], overview: {...day, scope: 'weekly'} }, null);
  assert.deepEqual(segments(report.days[0]), []);
  assert.match(chartMarkup(report.days, colors, day.date), /usage-activity-marker/);
  assert.match(breakdownRows(report.days[0], new Map(), colors), /—/);
  assert.doesNotMatch(donutMarkup(report.overview, colors), /stroke-dasharray/);
  assert.match(donutMarkup(report.days[0], colors), /—/);
  assert.doesNotMatch(donutMarkup(report.days[0], colors), />0\.0%</);
});

test('positive usage logs are not shown as zero when the provider percentage is unchanged', () => {
  const unchanged = {...day, unit: 'quota_percent', quota: {used_percent: 0, users: {}, unattributed_percent: 0}};
  assert.match(chartMarkup([unchanged], colors, day.date), /usage-activity-marker/);
  assert.doesNotMatch(chartMarkup([unchanged], colors, day.date), /관측 한도 증가 0%/);
  assert.match(donutMarkup(unchanged, colors), /—/);
  assert.doesNotMatch(donutMarkup(unchanged, colors), />0\.0%</);
});

test('weekly inspector ignores obsolete token-derived estimates', () => {
  const quota = {used_percent: 20, remaining_percent: 80, unattributed_percent: 20,
    estimated_unattributed_percent: 2, days: {},
    user_usage: [{node_key: 'a', used_percent: null, estimated_used_percent: 18}]};
  const report = quotaReport({days: [], overview: {...day, scope: 'weekly', date: '__weekly'}}, quota);
  assert.deepEqual(segments(report.overview).map(part => part.share), [20, 80]);
  assert.doesNotMatch(breakdownRows(report.overview, new Map(), colors), /18\.0%/);
});
