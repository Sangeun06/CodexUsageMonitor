const state = { data: null, query: "", expanded: new Set(), month: null, view: "weekly", explorers: null };
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ko-KR");
const compactNumber = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[char]);

function nodeColorMap(nodes) {
  return new Map(nodes.map(node => {
    let hash = 0;
    for (const char of node.key) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    return [node.key, `hsl(${hash % 360} 65% 65%)`];
  }));
}

function switchView(view, focus = false) {
  state.view = view;
  document.querySelectorAll("[data-view]").forEach(tab => {
    const active = tab.dataset.view === view;
    tab.setAttribute("aria-selected", active); tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !active;
    if (active && focus) tab.focus();
  });
}

function renderAnalytics(data) {
  const analytics = data.analytics;
  if (!state.explorers) state.explorers = { weekly: new AnalyticsView.Explorer("weekly"), monthly: new AnalyticsView.Explorer("monthly") };
  const months = analytics?.months || [];
  if (!months.some(month => month.id === state.month)) state.month = months[0]?.id || null;
  const month = months.find(item => item.id === state.month);
  const colors = nodeColorMap(data.nodes || []);
  $("monthSelect").innerHTML = months.map(item => `<option value="${item.id}">${item.id.replace("-", "년 ")}월</option>`).join("");
  $("monthSelect").value = state.month || "";
  $("monthlyTokens").textContent = month?.account_tokens == null ? "확인 불가" : number.format(month.account_tokens);
  $("monthlyTracked").textContent = month ? number.format(month.attributed_tokens) : "—";
  $("monthlyParticipants").textContent = month ? `${month.participant_count}명` : "—";
  const currentMonth = data.meta.collected_at.slice(0, 7);
  $("monthlyTodayCard").hidden = state.month !== currentMonth;
  $("monthlyCoverage").textContent = month ? `보고된 일별 합계 · ${month.reported_days}/${month.elapsed_days}일${month.reported_days < month.elapsed_days ? " · 누락 있음" : ""}${state.month === currentMonth ? " · 오늘은 진행 중" : ""}` : "월별 기록 대기 중";
  const max = Math.max(1, ...months.map(item => item.account_tokens || 0));
  $("monthComparison").innerHTML = months.map(item => `<button class="month-row ${item.id === state.month ? "chosen" : ""}" data-month="${item.id}" aria-pressed="${item.id === state.month}"><strong>${item.id}</strong><span class="month-meter"><i style="width:${(item.account_tokens || 0) / max * 100}%"></i></span><span>${item.account_tokens == null ? "기록 없음" : number.format(item.account_tokens)}<small>${item.reported_days}/${item.elapsed_days}일 보고${item.reported_days < item.elapsed_days ? " · 부분 집계" : ""}</small></span></button>`).join("");
  $("weeklyScope").textContent = data.weekly_usage ? `${new Date(data.weekly_usage.window_started_at).toISOString().replace("T", " ").slice(0, 16)} → ${new Date(data.weekly_usage.resets_at).toISOString().replace("T", " ").slice(0, 16)} UTC · 계정 초기화 기준` : "주간 한도 정보를 기다리는 중입니다.";
  state.explorers.weekly.update(analytics?.weekly, data.nodes || [], colors);
  if (analytics?.weekly && data.weekly_token_breakdown) {
    const pending = Math.max(0, data.weekly_token_breakdown.attributed_session_tokens - analytics.weekly.attributed_tokens);
    $("weeklyCoverageNote").textContent += ` 날짜 미확인 사용자량 ${number.format(pending)} 토큰은 날짜별 막대에서 제외하며, 주간 전체 구성에는 포함합니다.`;
  }
  state.explorers.monthly.update(month, data.nodes || [], colors);
}

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
  $("totalTokens").textContent = data.summary.lifetime_tokens == null ? "확인 불가" : number.format(data.summary.lifetime_tokens);
  $("sessionCount").textContent = number.format(data.summary.session_count);
  $("activeCount").textContent = number.format(data.summary.active_count);
  $("projectCount").textContent = number.format(data.summary.project_count);
  $("modelCount").textContent = number.format(data.summary.model_count);
  $("sessionTokens").textContent = number.format(data.summary.session_tokens ?? data.summary.total_tokens);
  $("totalSourceLabel").textContent = data.summary.lifetime_tokens != null
    ? "Codex 계정이 보고한 전체 누적 처리 토큰"
    : "계정 전체 누적값을 기다리는 중 · 세션 합계로 대체하지 않습니다";
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
  const todayDate = new Date(data.meta.collected_at).toISOString().slice(0, 10);
  const today = daily.find(bucket => bucket.date === todayDate)?.tokens ?? null;
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
  const unique = [...new Map(windows.map(item => [item.id, item])).values()]
    .sort((a, b) => (a.window_minutes || Infinity) - (b.window_minutes || Infinity));
  $("rateLimitGrid").innerHTML = unique.map(window => {
    const remaining = Math.max(0, 100 - window.used_percent);
    const reset = window.resets_at_iso ? `${untilTime(window.resets_at_iso)} 초기화` : "초기화 시각 없음";
    return `<article class="rate-card">
      <div><span>${usage.rate_limits.length > 1 ? `${escapeHtml(window.name)} · ` : ""}${escapeHtml(durationLabel(window.window_minutes))}</span><strong>${window.used_percent.toFixed(1)}% 사용</strong></div>
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
    $("reconPeriodRange").textContent = "관측 시작 대기";
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

function weeklyShare(period, tokens) {
  const total = Number(period.account_tokens);
  if (!period.comparable || !Number.isFinite(total) || total <= 0 || tokens == null) return null;
  return Number(tokens) / total * 100;
}

function weeklyShareLabel(share) {
  return share == null ? "계산 대기" : `${share.toFixed(1)}%`;
}

function renderWeeklyBreakdown(data) {
  const period = data.weekly_token_breakdown;
  const available = Boolean(period);
  $("weeklyBreakdownEmpty").style.display = available ? "none" : "block";
  for (const id of ["weeklyAccountTokens", "weeklyParticipantCount", "weeklyTrackedTokens", "weeklyUntrackedTokens"]) {
    $(id).textContent = "—";
  }
  $("weeklyBreakdownStack").innerHTML = "";
  $("weeklyBreakdownLegend").innerHTML = "";
  $("weeklyBreakdownNote").textContent = "";
  if (!available) {
    $("weeklyBreakdownRange").textContent = "현재 주기 관측값을 확인하는 중입니다.";
    $("weeklyBreakdownStack").setAttribute("aria-label", "사용자별 토큰 사용량을 확인하는 중입니다");
    return;
  }
  const accountTokens = period.account_tokens;
  const trackedTokens = Number(period.attributed_session_tokens || 0);
  const untrackedTokens = period.untracked_tokens;
  const users = period.user_tokens || [];
  const nodes = data.nodes || [];
  const nodeByKey = new Map(nodes.map(node => [node.key, node]));
  const colors = nodeColorMap(nodes);
  const labelFor = key => {
    const node = nodeByKey.get(key);
    return node ? `${node.display_name || node.os_user} · ${node.hostname}` : key;
  };
  const startLabel = period.coverage_started_at ? new Date(period.coverage_started_at).toLocaleString("ko-KR") : "관측 대기";
  const endLabel = new Date(period.coverage_ended_at).toLocaleString("ko-KR");
  $("weeklyBreakdownRange").textContent = `${startLabel}–${endLabel} · ${period.basis === "daily_lower_bound" ? "계정 일별 기록을 반영한 주간 최소량" : "계정은 관측 이후 증가분만 확인 · 주간 전체 아님"}`;
  const lowerBound = value => value == null ? "확인 불가" : `${period.is_lower_bound ? "최소 " : ""}${number.format(value)}`;
  $("weeklyAccountTokens").textContent = lowerBound(accountTokens);
  $("weeklyParticipantCount").textContent = `${period.participant_count ?? users.length}명`;
  $("weeklyTrackedTokens").textContent = number.format(trackedTokens);
  $("weeklyUntrackedTokens").textContent = lowerBound(untrackedTokens);
  const segments = weeklyShare(period, 0) != null ? users.map(user => {
    const share = weeklyShare(period, user.tokens);
    return `<span style="width:${share.toFixed(4)}%;background:${colors.get(user.node_key) || "#849b92"}" title="${escapeHtml(labelFor(user.node_key))}: ${weeklyShareLabel(share)} · ${number.format(user.tokens)} 토큰"></span>`;
  }) : [];
  const untrackedShare = weeklyShare(period, untrackedTokens);
  if (untrackedShare != null && untrackedTokens > 0) {
    segments.push(`<span class="untracked" style="width:${untrackedShare.toFixed(4)}%" title="미추적 ${weeklyShareLabel(untrackedShare)} · ${lowerBound(untrackedTokens)} 토큰"></span>`);
  }
  $("weeklyBreakdownStack").innerHTML = segments.join("");
  $("weeklyBreakdownStack").setAttribute("aria-label", `사용 ${period.participant_count ?? users.length}명, 사용자별 확인 ${number.format(trackedTokens)} 토큰, 미추적 ${lowerBound(untrackedTokens)}`);
  $("weeklyBreakdownLegend").innerHTML = users.map(user => `<span><i style="background:${colors.get(user.node_key) || "#849b92"}"></i><span class="weekly-share-name">${escapeHtml(labelFor(user.node_key))}</span><strong>${weeklyShareLabel(weeklyShare(period, user.tokens))}</strong><small>${number.format(user.tokens)} 토큰</small></span>`).join("")
    + `<span><i style="background:var(--amber)"></i><span class="weekly-share-name">미추적</span><strong>${weeklyShareLabel(untrackedShare)}</strong><small>${lowerBound(untrackedTokens)} 토큰</small></span>`;
  $("weeklyBreakdownNote").textContent = period.basis === "daily_lower_bound"
    ? `계정 집계 날짜 ${period.account_dates.join(", ")} (UTC 날짜 기준). 리셋 경계일·오늘·누락된 날짜는 계정 전체량에 포함하지 않습니다.`
    : "계정 일별 기록이 부족해 계정량은 서버 관측 이후 증가분을 최소량으로 사용합니다. 사용자량은 현재 주간 기준입니다.";
  $("weeklyBreakdownNote").textContent += ` 비중은 계정 주간 집계량을 100%로 계산하며 미추적량을 포함합니다.${period.is_lower_bound ? " 계정량이 최소 집계이므로 실제 전체 주간 비중과 다를 수 있습니다." : ""} 사용자량은 보관 세션을 포함하며, 이벤트와 세션 수치는 중복 합산하지 않습니다. 분 단위 이벤트 ${number.format(period.verified_event_tokens ?? trackedTokens)} · 세션 기준 보완 ${number.format(period.snapshot_only_tokens || 0)} 토큰. 오래된 세션의 관측 전 사용량은 제외되므로 미추적분에는 사용자 확인이 부족한 양도 포함됩니다.`;
  if (!period.comparable) {
    $("weeklyBreakdownNote").textContent += " 계정 집계가 부족하거나 사용자량보다 작아 미추적량과 비중을 확정할 수 없습니다.";
  }
}

function renderComparison(data) {
  const breakdown = data.weekly_token_breakdown;
  $("comparisonEmpty").style.display = breakdown ? "none" : "block";
  if (!breakdown) { $("comparisonList").innerHTML = ""; return; }
  const nodes = new Map((data.nodes || []).map(node => [node.key, node]));
  const rows = (breakdown.user_tokens || []).map(user => {
    const node = nodes.get(user.node_key) || {};
    return { ...user, name: node.display_name || node.os_user || user.node_key, detail: `${node.hostname || ""} · 주간 확인량 (보관 포함)` };
  });
  rows.push({ name: "미추적", detail: "사용자에게 귀속되지 않은 사용량", tokens: breakdown.untracked_tokens, untracked: true });
  $("comparisonList").innerHTML = rows.map(item => {
    const share = weeklyShare(breakdown, item.tokens);
    const amount = item.tokens == null ? "확인 불가" : `${item.untracked && breakdown.is_lower_bound ? "최소 " : ""}${number.format(item.tokens)}`;
    const shareLabel = share == null ? "계정 집계 대기 · 비중 계산 불가" : `계정 주간 ${breakdown.is_lower_bound ? "최소 집계량" : "집계량"} 대비`;
    return `<article class="comparison-row">
      <div class="comparison-user"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.detail)}</small></div>
      <div class="comparison-meter"><div><i style="width:${share ?? 0}%;${item.untracked ? "background:var(--amber)" : ""}"></i></div><small>${shareLabel}</small></div>
      <div class="comparison-value"><strong>${weeklyShareLabel(share)}</strong><small>${amount} 토큰</small></div>
      <span class="comparison-status ${item.untracked ? "high" : "normal"}">${item.untracked ? "미추적" : "확인된 사용"}</span>
    </article>`;
  }).join("");
}

function renderPace(weekly) {
  const pace = weekly?.pace;
  if (!pace) {
    for (const id of ["paceStatus", "dailyBudget", "remainingDailyBudget", "projectedUsage"]) $(id).textContent = "—";
    $("paceStatus").className = "";
    $("paceDeviation").textContent = "권장선 계산 중";
    $("projectedDetail").textContent = "주간 사용 한도 대기 중";
    return;
  }
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
  const width = 960, left = 52, right = 14, top = 18, bottom = 216;
  const plot = width - left - right;
  $("plannedPath").setAttribute("d", "");
  if (history.length < 2) {
    $("emptyChart").style.display = "grid";
    $("gridLines").innerHTML = "";
    for (const id of ["linePath", "areaPath", "plannedPath"]) $(id).setAttribute("d", "");
    return;
  }
  $("emptyChart").style.display = "none";
  const values = history.map(point => Number(point[valueKey] || 0));
  let min = percentScale ? 0 : Math.min(...values), max = percentScale ? 100 : Math.max(...values);
  if (min === max) { min = Math.max(0, min * .98); max = max * 1.02 || 1; }
  const start = bounds ? new Date(bounds.start).getTime() : null;
  const end = bounds ? new Date(bounds.end).getTime() : null;
  $("gridLines").innerHTML = [0, 1, 2, 3, 4].map(i => {
    const y = top + i * (bottom - top) / 4;
    const value = max - i * (max - min) / 4;
    return '<line class="grid-line" x1="'+left+'" y1="'+y+'" x2="'+(width-right)+'" y2="'+y+'"/><text class="trend-axis" x="'+(left-9)+'" y="'+(y+4)+'" text-anchor="end">'+(percentScale ? value.toFixed(0)+'%' : compactNumber.format(value))+'</text>';
  }).join("") + (bounds ? [0,1,2,3,4].map(i => '<text class="trend-axis" x="'+(left+plot*i/4)+'" y="248" text-anchor="middle">'+new Date(start+(end-start)*i/4).toISOString().slice(5,10).replace('-','/')+'</text>').join('') : '');
  const points = history.map((point, index) => [
    left + (bounds ? Math.max(0, Math.min(1, (new Date(point.at).getTime() - start) / (end - start))) : index / (history.length - 1)) * plot,
    top + (max - Number(point[valueKey] || 0)) / (max - min) * (bottom - top),
  ]);
  const line = points.map(([x, y], i) => (i ? "L" : "M")+x.toFixed(1)+","+y.toFixed(1)).join(" ");
  $("linePath").setAttribute("d", line);
  $("areaPath").setAttribute("d", line+' L'+points.at(-1)[0].toFixed(1)+','+bottom+' L'+left+','+bottom+' Z');
  $("plannedPath").setAttribute("d", percentScale ? 'M'+left+','+bottom+' L'+(width-right)+','+top : "");
}

function renderDailyTrendChart(svgId, days, maxTokens, colors, nodeLabels) {
  const left = 58, right = 14, top = 16, bottom = 205, width = 900;
  const plotHeight = bottom - top;
  const plotWidth = width - left - right;
  const step = plotWidth / Math.max(1, days.length);
  const barWidth = Math.min(52, step * .58);
  const axis = [0, .5, 1].map(fraction => {
    const y = bottom - fraction * plotHeight;
    return `<line class="trend-grid-line" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" />
      <text class="trend-axis" x="${left - 7}" y="${y + 4}" text-anchor="end">${compactNumber.format(Math.round(maxTokens * fraction))}</text>`;
  }).join("");
  const bars = days.map((day, index) => {
    const center = left + step * (index + .5);
    let stackBottom = bottom;
    const segments = Object.entries(day.users || {}).sort(([a], [b]) => a.localeCompare(b)).map(([key, tokens]) => {
      const height = Math.max(0, Number(tokens) / maxTokens * plotHeight);
      stackBottom -= height;
      return `<rect x="${(center - barWidth / 2).toFixed(1)}" y="${stackBottom.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${height.toFixed(1)}" fill="${colors.get(key)}">
        <title>${escapeHtml(day.date)} · ${escapeHtml(nodeLabels.get(key) || key)}: ${number.format(tokens)} 토큰</title></rect>`;
    }).join("");
    return `${segments}<text class="trend-axis" x="${center.toFixed(1)}" y="229" text-anchor="middle">${escapeHtml(day.date.slice(5).replace("-", "."))}</text>`;
  }).join("");
  const totals = days.map((day, index) => {
    const x = left + step * (index + .5);
    const y = bottom - Number(day.total_tokens || 0) / maxTokens * plotHeight;
    return [x, y, day];
  });
  let previousHasUsage = false;
  const line = totals.map(([x, y, day]) => {
    if (!day.total_tokens) { previousHasUsage = false; return ""; }
    const command = previousHasUsage ? "L" : "M";
    previousHasUsage = true;
    return `${command}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const points = totals.filter(([, , day]) => day.total_tokens > 0).map(([x, y, day]) => `<circle class="trend-total-point" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"><title>${escapeHtml(day.date)} · 합계 ${number.format(day.total_tokens)} 토큰</title></circle>`).join("");
  const empty = totals.every(([, , day]) => !day.total_tokens)
    ? `<text class="trend-axis" x="${(left + width - right) / 2}" y="112" text-anchor="middle">확인된 이벤트 없음</text>` : "";
  $(svgId).innerHTML = `${axis}${bars}<path class="trend-total-line" d="${line}" />${points}${empty}`;
}

function renderDailyUserTrend(data) {
  const trend = data.daily_user_trend;
  const days = trend?.days || [];
  const hasUsage = days.some(day => day.total_tokens > 0);
  $("trendGrid").style.display = hasUsage ? "grid" : "none";
  $("trendEmpty").style.display = hasUsage ? "none" : "block";
  $("trendNote").textContent = `수집기가 확인한 세션 이벤트 기준${trend?.timezone ? ` · 서버 시간대 ${trend.timezone}` : ""} · 설치 전·미수집 구간은 제외되어 계정 전체값과 다를 수 있습니다. 흰 선은 일일 합계이며 빈 날짜는 사용량 0이 아닐 수 있습니다. 주간 집계의 세션 기준 보완량은 사용 날짜를 확정할 수 없어 일별 그래프에 배분하지 않습니다.`;
  if (!hasUsage) {
    $("trendUserLegend").innerHTML = "";
    return;
  }
  const nodes = new Map((data.nodes || []).map(node => [node.key, node]));
  const totalsByUser = new Map();
  for (const day of days) {
    for (const [key, tokens] of Object.entries(day.users || {})) {
      totalsByUser.set(key, (totalsByUser.get(key) || 0) + Number(tokens));
    }
  }
  const keys = [...totalsByUser.keys()].sort();
  const colors = nodeColorMap(data.nodes || []);
  const nodeLabels = new Map(keys.map(key => {
    const node = nodes.get(key);
    return [key, node ? `${node.display_name || node.os_user} · ${node.hostname}` : key];
  }));
  $("trendUserLegend").innerHTML = keys.map(key => `<span class="trend-legend-item"><i style="background:${colors.get(key)}"></i>${escapeHtml(nodeLabels.get(key))}<small>${number.format(totalsByUser.get(key))}</small></span>`).join("");
  const maxTokens = Math.max(1, ...days.map(day => Number(day.total_tokens || 0)));
  for (const [type, svgId, totalId] of [
    ["weekday", "weekdayTrendChart", "weekdayTrendTotal"],
    ["weekend", "weekendTrendChart", "weekendTrendTotal"],
  ]) {
    const selected = days.filter(day => day.day_type === type);
    const total = selected.reduce((sum, day) => sum + Number(day.total_tokens || 0), 0);
    $(totalId).textContent = `합계 ${number.format(total)} 토큰 · ${selected.length}일`;
    renderDailyTrendChart(svgId, selected, maxTokens, colors, nodeLabels);
  }
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
  return UsageView.aggregateProjects(sessions);
}

function aggregateNodes(sessions) {
  return UsageView.aggregateNodes(sessions, state.data?.weekly_user_tokens ?? null);
}

function renderProjectDetails(projects) {
  return `<div class="project-tree">
    <div class="project-tree-heading"><span>프로젝트</span><span>세션</span><span>모델</span><span>토큰</span><span>최근 변화</span><span>마지막 활동</span></div>
    ${projects.map(project => `<div class="project-item">
      <div class="project-identity"><i class="${project.active ? "active" : ""}"></i><strong>${escapeHtml(project.name)}</strong></div>
      <span class="project-stat"><span class="count-badge">${project.sessions}</span></span>
      <span class="project-stat project-models">${escapeHtml([...project.models].join(", "))}</span>
      <span class="project-stat tokens">${number.format(project.tokens)}${project.weeklySessions ? `<small>주간 ${number.format(project.weeklyTokens)}</small>` : ""}</span>
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
      <td class="tokens">${number.format(group.tokens)}${group.weeklyTokens != null ? `<small>주간 ${number.format(group.weeklyTokens)} (보관 포함)</small>` : ""}</td>
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
  renderWeeklyBreakdown(data);
  renderComparison(data);
  renderAccounts(data.nodes);
  renderDailyUserTrend(data);
  renderAnalytics(data);
  const weekly = data.weekly_usage;
  if (weekly?.points?.length) {
    renderChart(weekly.points, "used_percent", true, { start: weekly.window_started_at, end: weekly.resets_at });
    renderPace(weekly);
    $("chartLegend").innerHTML = `<i></i> 현재 ${Number(weekly.used_percent).toFixed(1)}% · ${untilTime(weekly.resets_at)} 초기화`;
  } else {
    renderChart([]);
    renderPace(null);
    $("chartLegend").innerHTML = "<i></i> 주간 한도 정보 없음 · 누적량으로 대체하지 않습니다";
  }
  renderSessions(data.sessions);
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  $("manualRefresh").disabled = true;
  $("manualRefresh").textContent = "갱신 중…";
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
  } finally {
    state.refreshing = false;
    $("manualRefresh").disabled = false;
    $("manualRefresh").textContent = "새로고침 ↻";
  }
}

$("searchInput").addEventListener("input", event => { state.query = event.target.value; if (state.data) renderSessions(state.data.sessions); });
$("monthSelect").addEventListener("change", event => { state.month = event.target.value; if (state.data) renderAnalytics(state.data); });
$("monthComparison").addEventListener("click", event => { const month = event.target.closest("[data-month]")?.dataset.month; if (month) { state.month = month; renderAnalytics(state.data); } });
$("manualRefresh").addEventListener("click", refresh);
const tabs = [...document.querySelectorAll("[data-view]")];
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
  tab.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      switchView(tabs[next].dataset.view, true);
    }
  });
});
$("usageChart").addEventListener("pointermove", event => {
  if (!state.data?.weekly_usage || !state.explorers) return;
  const weekly = state.data.weekly_usage, rect = event.currentTarget.getBoundingClientRect();
  const start = new Date(weekly.window_started_at).getTime(), end = new Date(weekly.resets_at).getTime();
  const ratio = ((event.clientX - rect.left) / rect.width * 960 - 52) / (960 - 52 - 14);
  const date = new Date(start + Math.max(0, Math.min(.99999, ratio)) * (end - start)).toISOString().slice(0, 10);
  // The percentage is an account limit, not a per-user token conversion.
  // Use the same UTC day inspector, and surface its tooltip beside this chart.
  if (!state.explorers.weekly.preview(date)) { $("limitTooltip").hidden = true; return; }
  const source = $("weeklyTooltip"), tooltip = $("limitTooltip");
  tooltip.innerHTML = source.innerHTML; tooltip.hidden = source.hidden;
  tooltip.style.left = `${Math.max(0, Math.min(event.clientX - rect.left, rect.width - 350))}px`;
  source.hidden = true;
});
$("usageChart").addEventListener("pointerleave", () => {
  $("limitTooltip").hidden = true;
  if (state.explorers) { state.explorers.weekly.hovered = null; state.explorers.weekly.inspect(state.explorers.weekly.selected); }
});
refresh();
setInterval(refresh, 10_000);
