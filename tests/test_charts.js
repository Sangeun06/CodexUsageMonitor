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

function render(breakdown) {
  const { context, elements } = createUi();
  const data = {
    nodes: [{key: "a", display_name: "Alice <team>", hostname: "worker"}],
    weekly_token_breakdown: {
      basis: "daily_lower_bound", is_lower_bound: true,
      coverage_started_at: "2026-10-03T06:37:17Z", coverage_ended_at: "2026-10-05T08:00:00Z",
      account_dates: ["2026-10-04"], account_tokens: 1000, attributed_session_tokens: 100,
      user_tokens: [{node_key: "a", tokens: 100}], participant_count: 1,
      untracked_tokens: 900, comparable: true, ...breakdown,
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

test("weekly view explains snapshot supplementation without duplicating it", () => {
  const elements = render({ verified_event_tokens: 40, snapshot_only_tokens: 60 });
  assert.equal(elements.get("weeklyTrackedTokens").textContent, "100");
  assert.match(elements.get("weeklyBreakdownNote").textContent, /이벤트 40 · 세션 기준 보완 60/);
});

test("weekly and comparison bars include the missing 90% instead of normalizing users to 100%", () => {
  const elements = render();
  assert.equal(elements.get("weeklyUntrackedTokens").textContent, "최소 900");
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:10\.0000%/);
  assert.match(elements.get("weeklyBreakdownStack").innerHTML, /width:90\.0000%/);
  assert.match(elements.get("comparisonList").innerHTML, /10\.0%/);
  assert.match(elements.get("comparisonList").innerHTML, /<strong>10\.0%<\/strong><small>100 토큰<\/small>/);
  assert.match(elements.get("comparisonList").innerHTML, /<strong>90\.0%<\/strong><small>최소 900 토큰<\/small>/);
  assert.match(elements.get("comparisonList").innerHTML, /미추적/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /Alice &lt;team&gt;/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /<strong>10\.0%<\/strong>/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /<strong>90\.0%<\/strong>/);
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
  assert.match(elements.get("rateLimitGrid").innerHTML, /20\.0%/);
  assert.match(elements.get("rateLimitGrid").innerHTML, /70\.0%/);
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
  assert.equal(elements.get("projectedUsage").textContent, "150.0%");
  context.renderPace(null);
  assert.equal(elements.get("projectedUsage").textContent, "—");
  assert.equal(elements.get("paceStatus").className, "");
});

test("an insufficient account total shows unknown residual and no misleading full user bar", () => {
  const elements = render({account_tokens: 0, untracked_tokens: null, comparable: false});
  assert.equal(elements.get("weeklyUntrackedTokens").textContent, "확인 불가");
  assert.equal(elements.get("weeklyBreakdownStack").innerHTML, "");
  assert.match(elements.get("comparisonList").innerHTML, /비중 계산 불가/);
  assert.match(elements.get("comparisonList").innerHTML, /<strong>계산 대기<\/strong><small>100 토큰<\/small>/);
  assert.match(elements.get("weeklyBreakdownLegend").innerHTML, /<strong>계산 대기<\/strong>/);
  assert.doesNotMatch(elements.get("weeklyBreakdownLegend").innerHTML, /NaN|Infinity|100\.0%/);
  assert.doesNotMatch(elements.get("comparisonList").innerHTML, /width:100%/);
});
