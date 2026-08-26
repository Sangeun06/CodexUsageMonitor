const state = { data: null, query: "", expanded: new Set() };
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ko-KR");
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[char]);

function relativeTime(iso) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 10) return "방금 전";
  if (seconds < 60) return `${seconds}초 전`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 전`;
  return `${Math.floor(seconds / 86400)}일 전`;
}

function untilTime(iso) {
  const seconds = Math.max(0, Math.floor((new Date(iso).getTime() - Date.now()) / 1000));
  if (seconds < 60) return `${seconds}초 후`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 후`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 후`;
  return `${Math.floor(seconds / 86400)}일 후`;
}

function renderSummary(data) {
  $("totalTokens").textContent = number.format(data.summary.total_tokens);
  $("sessionCount").textContent = number.format(data.summary.session_count);
  $("activeCount").textContent = number.format(data.summary.active_count);
  $("projectCount").textContent = number.format(data.summary.project_count);
  $("modelCount").textContent = number.format(data.summary.model_count);
  $("sessionTokens").textContent = number.format(data.summary.session_tokens ?? data.summary.total_tokens);
  $("totalSourceLabel").textContent = data.summary.lifetime_tokens != null
    ? "Codex 계정이 보고한 전체 누적 처리 토큰"
    : "계정 전체값을 기다리는 중 · 프로젝트 귀속 합계 표시";
  $("lastUpdated").textContent = new Date(data.meta.collected_at).toLocaleTimeString("ko-KR");
  $("refreshLabel").textContent = `${data.meta.refresh_seconds}초마다 자동 새로고침`;
}

function durationLabel(minutes) {
  if (!minutes) return "사용 한도";
  if (minutes % 10080 === 0) return `${minutes / 10080}주 한도`;
  if (minutes % 1440 === 0) return `${minutes / 1440}일 한도`;
  if (minutes % 60 === 0) return `${minutes / 60}시간 한도`;
  return `${minutes}분 한도`;
}

function renderUsage(data) {
  const usage = data.account_usage;
  const empty = $("usageEmpty");
  if (!usage) {
    empty.style.display = "block";
    $("rateLimitGrid").innerHTML = "";
    $("todayTokens").textContent = "—";
    $("peakDailyTokens").textContent = "—";
    $("currentStreak").textContent = "—";
    $("usageFreshness").innerHTML = "<i></i> 수집 대기 중";
    return;
  }
  empty.style.display = "none";
  const daily = usage.daily_usage || [];
  const today = daily.length ? daily[daily.length - 1].tokens : null;
  $("todayTokens").textContent = today == null ? "—" : number.format(today);
  $("peakDailyTokens").textContent = usage.summary.peak_daily_tokens == null ? "—" : number.format(usage.summary.peak_daily_tokens);
  $("currentStreak").textContent = usage.summary.current_streak_days == null ? "—" : number.format(usage.summary.current_streak_days);
  const reporter = usage.reported_by || {};
  const reporterLabel = reporter.hostname ? ` · ${reporter.hostname}/${reporter.display_name || reporter.os_user}` : "";
  $("usageFreshness").innerHTML = `<i></i> ${relativeTime(usage.received_at)} 동기화${escapeHtml(reporterLabel)}`;

  const windows = [];
  for (const limit of usage.rate_limits || []) {
    for (const key of ["primary", "secondary"]) {
      const window = limit[key];
      if (window) windows.push({ ...window, id: `${limit.id}-${key}`, name: limit.name });
    }
  }
  const unique = [...new Map(windows.map(item => [`${item.window_minutes}:${item.resets_at}`, item])).values()]
    .sort((a, b) => (a.window_minutes || Infinity) - (b.window_minutes || Infinity));
  $("rateLimitGrid").innerHTML = unique.map(window => {
    const remaining = Math.max(0, 100 - window.used_percent);
    const reset = window.resets_at_iso ? `${untilTime(window.resets_at_iso)} 초기화` : "초기화 시각 없음";
    return `<article class="rate-card">
      <div><span>${escapeHtml(durationLabel(window.window_minutes))}</span><strong>${window.used_percent.toFixed(1)}% 사용</strong></div>
      <div class="rate-track"><i style="width:${window.used_percent}%"></i></div>
      <small>${remaining.toFixed(1)}% 남음 · ${escapeHtml(reset)}</small>
    </article>`;
  }).join("");
}

function signedNumber(value) {
  const amount = Number(value || 0);
  return `${amount > 0 ? "+" : ""}${number.format(amount)}`;
}

function renderReconciliation(data) {
  const recon = data.reconciliation;
  if (!recon) {
    for (const id of ["reconAccountTotal", "reconSessionTotal", "reconLifetimeGap", "reconPeriodGap"]) $(id).textContent = "—";
    $("coverageLegend").innerHTML = "<i></i> 계정 전체값 대기 중";
    $("coverageReasons").innerHTML = "";
    $("coverageBar").style.width = "0%";
    return;
  }
  $("reconAccountTotal").textContent = number.format(recon.lifetime.account_tokens);
  $("reconSessionTotal").textContent = number.format(recon.lifetime.attributed_session_tokens);
  $("reconLifetimeGap").textContent = signedNumber(recon.lifetime.difference_tokens);
  $("reconPeriodGap").textContent = signedNumber(recon.period.difference_tokens);
  const coverage = recon.period.coverage_percent;
  $("coverageLegend").innerHTML = `<i></i> ${coverage == null ? "증감 관측 시작" : `관측 구간 ${coverage.toFixed(1)}% 귀속`}`;
  $("coverageBar").style.width = `${Math.max(0, Math.min(100, coverage || 0))}%`;
  $("reconPeriodRange").textContent = recon.period.coverage_started_at
    ? `${relativeTime(recon.period.coverage_started_at)}부터 · 계정 ${number.format(recon.period.account_tokens)} / 세션 ${number.format(recon.period.attributed_session_tokens)}`
    : "관측 시작 대기";
  $("coverageReasons").innerHTML = (recon.reasons || []).length
    ? recon.reasons.map(reason => `<span>${escapeHtml(reason)}</span>`).join("")
    : "<span class=\"coverage-ok\">현재 관측 구간에서 뚜렷한 미귀속 차이가 없습니다.</span>";
}

function renderComparison(data) {
  const comparison = data.reconciliation?.node_comparison || [];
  $("comparisonEmpty").style.display = comparison.length ? "none" : "block";
  $("comparisonList").innerHTML = comparison.map(item => {
    const label = item.status === "high" ? "평균보다 높음" : item.status === "low" ? "평균보다 낮음" : "평균 범위";
    const width = Math.max(1, Math.min(100, item.share_percent));
    const weeklyValue = item.partial
      ? `${number.format(item.tokens)}–${number.format(item.maximum_tokens)}`
      : number.format(item.tokens);
    const coverageLabel = item.partial
      ? `부분 관측 · ${item.partial_session_count}개 세션의 초기 사용분 범위 포함`
      : "주간 기준값 확인됨";
    const verifiedLabel = item.verified_event_tokens > 0
      ? ` · 시간 이벤트 확정 ${number.format(item.verified_event_tokens)}`
      : "";
    return `<article class="comparison-row">
      <div class="comparison-user"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.hostname)} · OS ${escapeHtml(item.os_user)} · 세션 ${item.session_count}</small><small>${escapeHtml(coverageLabel)}${verifiedLabel} · 전체 누적 ${number.format(item.attributed_total_tokens)}</small></div>
      <div class="comparison-meter"><div><i style="width:${width}%"></i></div><small>확인된 주간 사용량 비중 ${item.share_percent.toFixed(1)}%</small></div>
      <div class="comparison-value"><strong>${weeklyValue}</strong><small>${item.partial ? "주간 사용량 범위" : `평균의 ${item.average_ratio.toFixed(2)}배`}</small></div>
      <span class="comparison-status ${item.status}">${label}</span>
    </article>`;
  }).join("");
}

function renderPace(weekly) {
  const pace = weekly?.pace;
  if (!pace) return;
  $("paceStatus").textContent = pace.label;
  $("paceStatus").className = `pace-${pace.status}`;
  const deviation = pace.deviation_percent;
  $("paceDeviation").textContent = `현재 권장선보다 ${Math.abs(deviation).toFixed(1)}%p ${deviation > 0 ? "앞섬" : "여유"}`;
  $("dailyBudget").textContent = `${pace.daily_budget_percent.toFixed(1)}%`;
  $("remainingDailyBudget").textContent = `${pace.remaining_daily_budget_percent.toFixed(1)}%`;
  $("projectedUsage").textContent = `${pace.projected_end_percent.toFixed(1)}%`;
  $("projectedDetail").textContent = pace.projected_exhaustion_at
    ? `${new Date(pace.projected_exhaustion_at).toLocaleString("ko-KR")} 소진 예상`
    : "초기화 전 한도 내 예상";
}

function renderChart(history, valueKey = "tokens", percentScale = false, bounds = null) {
  const svgWidth = 960, svgHeight = 260, pad = 12;
  const grid = $("gridLines");
  $("plannedPath").setAttribute("d", "");
  grid.innerHTML = [0, 1, 2, 3].map(i => {
    const y = pad + i * ((svgHeight - pad * 2) / 3);
    return `<line class="grid-line" x1="0" y1="${y}" x2="${svgWidth}" y2="${y}" />`;
  }).join("");
  if (!history.length) {
    $("emptyChart").style.display = "grid";
    $("linePath").setAttribute("d", ""); $("areaPath").setAttribute("d", ""); $("plannedPath").setAttribute("d", "");
    return;
  }
  $("emptyChart").style.display = history.length < 2 ? "grid" : "none";
  if (history.length < 2) return;
  const values = history.map(point => Number(point[valueKey] || 0));
  let min = percentScale ? 0 : Math.min(...values), max = percentScale ? 100 : Math.max(...values);
  if (min === max) { min = Math.max(0, min * .98); max = max * 1.02 || 1; }
  const startTime = bounds ? new Date(bounds.start).getTime() : null;
  const endTime = bounds ? new Date(bounds.end).getTime() : null;
  const points = history.map((point, index) => {
    const x = bounds
      ? Math.max(0, Math.min(svgWidth, (new Date(point.at).getTime() - startTime) / (endTime - startTime) * svgWidth))
      : index * (svgWidth / (history.length - 1));
    const y = pad + (max - Number(point[valueKey] || 0)) / (max - min) * (svgHeight - pad * 2);
    return [x, y];
  });
  const line = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const lastX = points[points.length - 1][0].toFixed(1);
  const area = `${line} L${lastX},${svgHeight} L0,${svgHeight} Z`;
  $("linePath").setAttribute("d", line); $("areaPath").setAttribute("d", area);
  $("plannedPath").setAttribute("d", percentScale ? `M0,${svgHeight - pad} L${svgWidth},${pad}` : "");
}

function renderAccounts(nodes) {
  $("accountGrid").innerHTML = nodes.map(node => {
    const account = node.account || {};
    const displayName = node.display_name || node.os_user;
    const initial = (displayName || "?").slice(0, 1).toUpperCase();
    const email = account.email || "로그인 정보 없음";
    return `<article class="account-card">
      <div class="account-avatar">${escapeHtml(initial)}</div>
      <div><div class="account-title"><strong>${escapeHtml(displayName)}</strong><button class="alias-button" data-node-key="${escapeHtml(node.key)}" data-display-name="${escapeHtml(displayName)}" title="표시 이름 변경" aria-label="표시 이름 변경">✎</button></div><p>${escapeHtml(email)}</p>
        <div class="account-meta"><span>${escapeHtml(node.hostname)}</span><span>OS: ${escapeHtml(node.os_user)}</span>${account.name && account.name !== displayName ? `<span>${escapeHtml(account.name)}</span>` : ""}${account.plan ? `<span>${escapeHtml(account.plan)}</span>` : ""}${account.auth_provider ? `<span>${escapeHtml(account.auth_provider)}</span>` : ""}</div>
      </div>
    </article>`;
  }).join("");
  document.querySelectorAll(".alias-button").forEach(button => {
    button.addEventListener("click", async () => {
      const displayName = window.prompt("대시보드에 표시할 사용자명을 입력하세요. 빈 값은 OS 사용자명으로 되돌립니다.", button.dataset.displayName);
      if (displayName === null) return;
      try {
        const response = await fetch(`/api/nodes/${encodeURIComponent(button.dataset.nodeKey)}/alias`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: displayName })
        });
        if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        window.alert(`사용자명을 변경하지 못했습니다: ${error.message}`);
      }
    });
  });
}

function aggregateProjects(sessions) {
  const projects = new Map();
  for (const session of sessions) {
    const key = session.project_key || session.project_name;
    if (!projects.has(key)) {
      projects.set(key, { key, name: session.project_name, sessions: 0, tokens: 0, verifiedWeeklyTokens: 0, verifiedSessions: 0, delta: 0, active: false, updatedAt: session.updated_at, models: new Set() });
    }
    const project = projects.get(key);
    project.sessions += 1;
    project.tokens += session.tokens;
    if (session.verified_weekly_tokens != null) {
      project.verifiedWeeklyTokens += session.verified_weekly_tokens;
      project.verifiedSessions += 1;
    }
    project.delta += session.delta;
    project.active ||= session.active;
    project.models.add(session.model);
    if (new Date(session.updated_at) > new Date(project.updatedAt)) project.updatedAt = session.updated_at;
  }
  return [...projects.values()].sort((a, b) => Number(b.active) - Number(a.active) || b.tokens - a.tokens);
}

function aggregateNodes(sessions) {
  const groups = new Map();
  for (const session of sessions) {
    const key = session.node_key || `${session.hostname}:${session.os_user}`;
    if (!groups.has(key)) {
      groups.set(key, { key, hostname: session.hostname, osUser: session.os_user, displayUser: session.display_user || session.os_user, sessions: [], tokens: 0, delta: 0, active: false, updatedAt: session.updated_at, models: new Set() });
    }
    const group = groups.get(key);
    group.sessions.push(session);
    group.tokens += session.tokens;
    group.delta += session.delta;
    group.active ||= session.active;
    group.models.add(session.model);
    if (new Date(session.updated_at) > new Date(group.updatedAt)) group.updatedAt = session.updated_at;
  }
  for (const group of groups.values()) group.projects = aggregateProjects(group.sessions);
  return [...groups.values()].sort((a, b) => Number(b.active) - Number(a.active) || b.tokens - a.tokens);
}

function renderProjectDetails(projects) {
  return `<div class="project-tree">
    <div class="project-tree-heading"><span>프로젝트</span><span>세션</span><span>모델</span><span>토큰</span><span>최근 변화</span><span>마지막 활동</span></div>
    ${projects.map(project => `<div class="project-item">
      <div class="project-identity"><i class="${project.active ? "active" : ""}"></i><strong>${escapeHtml(project.name)}</strong></div>
      <span class="project-stat"><span class="count-badge">${project.sessions}</span></span>
      <span class="project-stat project-models">${escapeHtml([...project.models].join(", "))}</span>
      <span class="project-stat tokens">${number.format(project.tokens)}${project.verifiedSessions ? `<small>주간 확정 ${number.format(project.verifiedWeeklyTokens)}</small>` : ""}</span>
      <span class="project-stat delta">${project.delta ? "+" + number.format(project.delta) : "—"}</span>
      <span class="project-stat project-updated">${relativeTime(project.updatedAt)}</span>
    </div>`).join("")}
  </div>`;
}

function renderSessions(sessions) {
  const query = state.query.trim().toLowerCase();
  const groups = aggregateNodes(sessions).filter(group => !query || `${group.hostname} ${group.osUser} ${group.displayUser} ${[...group.models].join(" ")} ${group.projects.map(p => p.name).join(" ")}`.toLowerCase().includes(query));
  $("sessionRows").innerHTML = groups.map(group => {
    const open = state.expanded.has(group.key) || Boolean(query);
    return `<tr class="group-row ${open ? "expanded" : ""}" data-group-key="${escapeHtml(group.key)}" tabindex="0">
      <td><span class="status-pill ${group.active ? "active" : ""}"><i></i>${group.active ? "활성" : "대기"}</span></td>
      <td><span class="node-label">${escapeHtml(group.hostname)}<small><span class="user-alias">${escapeHtml(group.displayUser)}</span>${group.displayUser !== group.osUser ? ` · OS ${escapeHtml(group.osUser)}` : ""}</small></span></td>
      <td><span class="count-badge">${group.sessions.length}</span></td>
      <td><span class="count-badge">${group.projects.length}</span></td>
      <td><span class="models">${escapeHtml([...group.models].join(", "))}</span></td>
      <td class="tokens">${number.format(group.tokens)}</td>
      <td class="delta ${group.delta ? "" : "zero"}">${group.delta ? "+" + number.format(group.delta) : "—"}</td>
      <td>${relativeTime(group.updatedAt)}</td>
      <td><button class="expand-button ${open ? "open" : ""}" aria-expanded="${open}" aria-label="프로젝트 상세 보기">⌄</button></td>
    </tr>
    ${open ? `<tr class="detail-row"><td colspan="9">${renderProjectDetails(group.projects)}</td></tr>` : ""}`;
  }).join("");
  document.querySelectorAll(".group-row").forEach(row => {
    const toggle = () => {
      const key = row.dataset.groupKey;
      state.expanded.has(key) ? state.expanded.delete(key) : state.expanded.add(key);
      renderSessions(state.data.sessions);
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
  });
  $("emptyTable").style.display = groups.length ? "none" : "grid";
}

function render(data) {
  state.data = data;
  renderSummary(data);
  renderUsage(data);
  renderReconciliation(data);
  renderComparison(data);
  renderAccounts(data.nodes);
  const weekly = data.weekly_usage;
  if (weekly?.points?.length) {
    renderChart(weekly.points, "used_percent", true, { start: weekly.window_started_at, end: weekly.resets_at });
    renderPace(weekly);
    $("chartLegend").innerHTML = `<i></i> 현재 ${Number(weekly.used_percent).toFixed(1)}% · ${untilTime(weekly.resets_at)} 초기화`;
  } else {
    renderChart(data.history);
    $("chartLegend").innerHTML = "<i></i> 프로젝트 집계 fallback";
  }
  renderSessions(data.sessions);
}

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    document.querySelector(".connection").classList.remove("error");
    $("connectionText").textContent = "모니터 서버 연결됨";
  } catch (error) {
    document.querySelector(".connection").classList.add("error");
    $("connectionText").textContent = "연결 오류";
    console.error(error);
  }
}

$("searchInput").addEventListener("input", event => { state.query = event.target.value; if (state.data) renderSessions(state.data.sessions); });
refresh();
setInterval(refresh, 10_000);
