// Run with: node --test tests/test_charts.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function createUi() {
  const elements = new Map();
  const document = {
    querySelectorAll() { return []; },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        style: {}, innerHTML: "", textContent: "", attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; }, addEventListener() {},
      });
      return elements.get(id);
    },
  };
  const context = vm.createContext({ document, Intl, console, setInterval() {}, fetch: () => new Promise(() => {}) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/usage-view.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8"), context);
  return { context, elements };
}

function render(quota) {
  const { context, elements } = createUi();
  const data = {
    nodes: [{key: "a", display_name: "Alice <team>", hostname: "worker"}],
    weekly_quota: quota === null ? null : {
      basis: "rate_limit_delta", used_percent: 20, remaining_percent: 80,
      attributed_percent: 2, unattributed_percent: 18,
      coverage_started_at: "2026-10-03T06:37:17Z", coverage_ended_at: "2026-10-05T08:00:00Z",
      user_usage: [{node_key: "a", tokens: 100, used_percent: 2}], ...quota,
    },
  };
  context.renderWeeklyBreakdown(data);
  context.renderComparison(data);
  return elements;
}

test("new user's snapshot-only weekly usage is visible alongside lifetime counters", () => {
  const { context, elements } = createUi();
  const session = {
    node_key: "new", hostname: "worker", os_user: "new-user", key: "session",
    project_key: "project", project_name: "project", tokens: 200, weekly_tokens: 200,
    verified_weekly_tokens: 0, delta: 0, active: false, model: "test", updated_at: "2026-10-06T10:00:00Z",
  };
  const data = { sessions: [session], weekly_user_tokens: [{ node_key: "new", tokens: 250 }] };
  vm.runInContext(`state.data = ${JSON.stringify(data)}`, context);
  context.renderSessions(data.sessions);
  const html = elements.get("sessionRows").innerHTML;
  assert.match(html, /주간 250 \(보관 포함\)/);
  assert.match(html, /class="tokens">200/);
  const projects = context.aggregateProjects(data.sessions);
  assert.equal(projects[0].weeklyTokens, 200);
  assert.match(context.renderProjectDetails(projects), /주간 200/);
});

test("weekly quota uses the full allowance and retains remaining capacity", () => {
  const elements = render();
  assert.equal(elements.get("weeklyUsedPercent").textContent, "20.00%");
  assert.equal(elements.get("weeklyRemainingPercent").textContent, "80.00%");
  assert.equal(elements.get("weeklyAttributedPercent").textContent, "2.00%");
  assert.equal(elements.get("weeklyUnattributedPercent").textContent, "18.00%");
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:2\.0000%/);
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:18\.0000%/);
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:80\.0000%/);
  assert.match(elements.get("comparisonList").innerHTML, /<strong>2\.00%<\/strong>/);
  assert.match(elements.get("comparisonList").innerHTML, /100 토큰 · 처리량 참고/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /Alice &lt;team&gt;/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /남은 한도/);
  assert.match(elements.get("weeklyBreakdownNote").textContent, /추론/);
});

test("a large token counter cannot override a small observed quota", () => {
  const elements = render({ user_usage: [{node_key: "a", tokens: 1000000000, used_percent: 0.5}], attributed_percent: 0.5, unattributed_percent: 19.5 });
  assert.match(elements.get("comparisonList").innerHTML, /<strong>0\.50%<\/strong>/);
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:0\.5000%/);
  assert.doesNotMatch(elements.get("weeklyBreakdownStack").innerHTML, /NaN|Infinity/);
});

test("today never displays the last reported day or a future bucket", () => {
  const { context, elements } = createUi();
  const data = {
    meta: { collected_at: "2026-10-05T16:00:00Z" },
    account_usage: {
      summary: {}, received_at: "2026-10-05T16:00:00Z", rate_limits: [],
      daily_usage: [{ date: "2026-10-04", tokens: 500 }, { date: "2026-10-06", tokens: 900 }],
    },
  };
  context.renderUsage(data);
  assert.equal(elements.get("todayTokens").textContent, "—");
  data.account_usage.daily_usage.push({ date: "2026-10-05", tokens: 100 });
  context.renderUsage(data);
  assert.equal(elements.get("todayTokens").textContent, "100");
});

test("different limits with the same reset time stay visible", () => {
  const { context, elements } = createUi();
  context.renderUsage({
    meta: { collected_at: "2026-10-05T16:00:00Z" },
    account_usage: {
      summary: {}, daily_usage: [], received_at: "2026-10-05T16:00:00Z",
      rate_limits: [
        { id: "codex", name: "Codex", primary: { window_minutes: 10080, resets_at: 1700000, used_percent: 20 } },
        { id: "other", name: "Other", primary: { window_minutes: 10080, resets_at: 1700000, used_percent: 70 } },
      ],
    },
  });
  assert.equal((elements.get("rateLimitGrid").innerHTML.match(/class="rate-card"/g) || []).length, 2);
  assert.match(elements.get("rateLimitGrid").innerHTML, /20\.00%/);
  assert.match(elements.get("rateLimitGrid").innerHTML, /70\.00%/);
});

test("charts clear the previous curve when samples fall below two", () => {
  const { context, elements } = createUi();
  context.renderChart([{ tokens: 100 }, { tokens: 200 }]);
  assert.ok(elements.get("linePath").attributes.d);
  context.renderChart([{ tokens: 150 }]);
  assert.equal(elements.get("linePath").attributes.d, "");
  assert.equal(elements.get("areaPath").attributes.d, "");
  assert.equal(elements.get("emptyChart").style.display, "grid");
});

test("missing weekly usage clears previous pace estimates", () => {
  const { context, elements } = createUi();
  context.renderPace({ pace: {
    label: "빠름", status: "fast", deviation_percent: 20,
    daily_budget_percent: 14, remaining_daily_budget_percent: 5, projected_end_percent: 150,
  } });
  assert.equal(elements.get("projectedUsage").textContent, "150.00%");
  context.renderPace(null);
  assert.equal(elements.get("projectedUsage").textContent, "—");
  assert.equal(elements.get("paceStatus").className, "");
});

test("old token-derived percentages are ignored when personal measurement is missing", () => {
  const elements = render({user_usage: [{node_key: "a", tokens: 1000000, used_percent: null,
    estimated_used_percent: 42, observed_used_percent: 0, estimate_method: "account_average"}],
    attributed_percent: 0, unattributed_percent: 20, estimated_attributed_percent: 42});
  assert.match(elements.get("comparisonList").innerHTML, /개인별 실측 없음/);
  assert.doesNotMatch(elements.get("comparisonList").innerHTML, /42\.00%/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /개인별 실측 없음/);
  assert.equal(elements.get("weeklyAttributedPercent").textContent, "0.00%");
  assert.equal(elements.get("weeklyUnattributedPercent").textContent, "20.00%");
  assert.doesNotMatch(elements.get("weeklyBreakdownStack").innerHTML, /NaN|Infinity|width:42/);
});

test("missing weekly limits clear previous quota and comparison", () => {
  const elements = render(null);
  assert.equal(elements.get("weeklyUsedPercent").textContent, "—");
  assert.equal(elements.get("weeklyBreakdownStack").innerHTML, "");
  assert.equal(elements.get("comparisonList").innerHTML, "");
});

test("fractional quota stays visible in user values, totals and tooltips", () => {
  const elements = render({used_percent: 3.1274, remaining_percent: 96.8726,
    attributed_percent: 3.1274, unattributed_percent: 0,
    user_usage: [{node_key: "a", tokens: 100, used_percent: 3.1274}]});
  assert.equal(elements.get("weeklyUsedPercent").textContent, "3.1274%");
  assert.match(elements.get("comparisonList").innerHTML, /3\.1274%/);
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /3\.1274%/);
  const tiny = render({user_usage: [{node_key: "a", tokens: 100, used_percent: 0.0032}]});
  assert.match(tiny.get("comparisonList").innerHTML, /0\.0032%/);
  const {formatPercent} = require('../static/usage-view.js');
  assert.equal(formatPercent(3), '3.00%');
  assert.equal(formatPercent(0.00001), '<0.0001%');
  assert.equal(formatPercent(null), '관측 대기');
});

test("actual plan and recent account change use provider values without a guessed denominator", () => {
  const {context, elements} = createUi();
  const contract = {plan_code: 'prolite', recent_changes: [{status: 'reported_increase', change_percent_points: 0.0321,
    started_at: '2026-10-27T08:00:00Z', ended_at: '2026-10-27T08:02:00Z'}]};
  context.renderQuotaContract({weekly_quota: {account_quota: contract}});
  assert.match(elements.get('quotaPlan').textContent, /prolite/);
  assert.match(elements.get('quotaCapacityNote').textContent, /절대 토큰·크레딧량을 제공하지/);
  assert.match(elements.get('recentAccountChange').textContent, /0\.0321%p/);
  contract.recent_changes[0].status = 'reported_unchanged';
  contract.recent_changes[0].change_percent_points = 0;
  context.renderQuotaContract({weekly_quota: {account_quota: contract}});
  assert.match(elements.get('recentAccountChange').textContent, /변동 없음/);
  assert.doesNotMatch(elements.get('recentAccountChange').textContent, /0\.00%/);
  assert.match(elements.get('recentAccountRange').textContent, /실제 사용량 0으로 판정하지/);
});
