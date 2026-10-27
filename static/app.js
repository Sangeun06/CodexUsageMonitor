const state = { data: null, query: "", expanded: new Set(), month: null, view: "weekly", explorers: null, accountKey: null, refreshSequence: 0 };
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
  state.explorers.weekly.update(AnalyticsView.quotaReport(analytics?.weekly, data.weekly_quota), data.nodes || [], colors);
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
      <div><span>${usage.rate_limits.length > 1 ? `${escapeHtml(window.name)} · ` : ""}${escapeHtml(durationLabel(window.window_minutes))}</span><strong>${quotaLabel(window.used_percent)} 사용</strong></div>
      <div class="rate-track"><i style="width:${window.used_percent}%"></i></div>
      <small>${quotaLabel(remaining)} 남음 · ${escapeHtml(reset)}</small>
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

const quotaLabel = UsageView.formatPercent;
const userQuota = user => user.used_percent;
const allocatedQuota = quota => quota.attributed_percent;
const unallocatedQuota = quota => quota.unattributed_percent;

function renderWeeklyBreakdown(data) {
  const quota = data.weekly_quota;
  $("weeklyBreakdownEmpty").style.display = quota ? "none" : "block";
  for (const id of ["weeklyUsedPercent", "weeklyRemainingPercent", "weeklyAttributedPercent", "weeklyUnattributedPercent"]) $(id).textContent = "—";
  $("weeklyBreakdownStack").innerHTML = "";
  $("weeklyBreakdownLegend").innerHTML = "";
  $("weeklyBreakdownNote").textContent = "";
  if (!quota) {
    $("weeklyBreakdownRange").textContent = "실제 주간 한도 사용률을 기다리는 중입니다.";
    $("weeklyBreakdownStack").setAttribute("aria-label", "한도 사용률 확인 불가");
    return;
  }
  $("weeklyUsedPercent").textContent = quotaLabel(quota.used_percent);
  $("weeklyRemainingPercent").textContent = quotaLabel(quota.remaining_percent);
  $("weeklyAttributedPercent").textContent = quotaLabel(allocatedQuota(quota));
  $("weeklyUnattributedPercent").textContent = quotaLabel(unallocatedQuota(quota));
  const nodes = new Map((data.nodes || []).map(node => [node.key, node]));
  const colors = nodeColorMap(data.nodes || []);
  const labelFor = key => {
    const node = nodes.get(key);
    return node ? `${node.display_name || node.os_user} · ${node.hostname}` : key;
  };
  const rows = (quota.user_usage || []).map(user => ({
    name: labelFor(user.node_key), value: userQuota(user),
    color: colors.get(user.node_key) || "#849b92", detail: `${number.format(user.tokens)} 토큰 · ${user.attribution_reason || "관측 구간만 · 주간 전체 사용량 아님"}`,
  }));
  rows.push({ name: "사용자 미귀속", value: unallocatedQuota(quota), color: "var(--amber)", detail: "개인에게 임의 배분하지 않은 계정 사용분" });
  rows.push({ name: "남은 한도", value: quota.remaining_percent, color: "#3d4855", detail: "주간 제공량 기준" });
  $("weeklyBreakdownRange").textContent = quota.coverage_started_at
    ? `${new Date(quota.coverage_started_at).toLocaleString("ko-KR")}부터 관측 · 계정 실제 사용 ${quotaLabel(quota.used_percent)}`
    : `계정 실제 사용 ${quotaLabel(quota.used_percent)} · 사용자별 귀속 관측 대기`;
  $("weeklyBreakdownStack").innerHTML = rows.filter(row => row.value > 0).map(row =>
    `<span style="width:${row.value.toFixed(4)}%;background:${row.color}" title="${escapeHtml(row.name)}: ${quotaLabel(row.value)}"></span>`
  ).join("");
  $("weeklyBreakdownStack").setAttribute("aria-label", `주간 제공량 100% 중 사용 ${quotaLabel(quota.used_percent)}, 남음 ${quotaLabel(quota.remaining_percent)}, 사용자 미귀속 ${quotaLabel(unallocatedQuota(quota))}`);
  $("weeklyBreakdownLegend").innerHTML = rows.map(row => `<span><i style="background:${row.color}"></i><span class="weekly-share-name">${escapeHtml(row.name)}</span><strong>${row.value == null ? "개인별 실측 없음" : quotaLabel(row.value)}</strong><small>${escapeHtml(row.detail)}</small></span>`).join("");
  $("weeklyBreakdownNote").textContent = `계정 실제 사용률은 ${quotaLabel(quota.used_percent)}입니다. 서비스가 제공하는 한도 사용률은 공유 계정 전체 값입니다. 사용자별 숫자는 단독 활동과 한도 증가가 일치한 관측 구간만 표시하며, 해당 사용자의 실제 주간 전체 사용률을 뜻하지 않습니다. 나머지는 사용자 미귀속으로 남깁니다. 토큰 수·계정 평균·다른 모델의 소모율로 개인별 사용률을 만들어 내지 않습니다. 요청별 모델·추론 강도·캐시·출력 내역은 아래 로그 상세에서 확인할 수 있습니다.`;

}

function renderUsageEvidence(item) {
  const evidence = item.evidence;
  const metadata = item.metadata;
  const status = metadata.backfill_pending > 0 ? "기존 로그 복구 중" : evidence.length ? "복구된 요청별 기록" : metadata.version === "0.7.1" ? "해당 계정으로 확인된 요청 기록 대기" : "수집기 0.7.1 업데이트 대기";
  const count = value => value == null ? "기록 없음" : number.format(value);
  return `<details class="usage-evidence"><summary>모델·추론·캐시 내역 · ${status}</summary>${evidence.length ? `<div class="table-wrap"><table><thead><tr><th>요청 모델 / 속도</th><th>추론 강도</th><th>입력</th><th>캐시 입력</th><th>출력</th><th>추론 (출력에 포함)</th></tr></thead><tbody>${evidence.map(row => `<tr><td>${escapeHtml(row.model)}<small>${escapeHtml(row.service_tier)} / ${escapeHtml(row.reasoning_mode)}</small></td><td>${escapeHtml(row.effort)}</td><td>${count(row.input_tokens)}</td><td>${count(row.cached_input_tokens)}</td><td>${count(row.output_tokens)}</td><td>${count(row.reasoning_output_tokens)}</td></tr>`).join("")}</tbody></table></div>` : "<p>로컬 로그와 계정 사용 구간을 대조해 복구합니다. 수집 전·계정 전환 내역을 검증할 수 없는 기록은 다른 계정에 붙이지 않습니다.</p>"}</details>`;
}

function renderComparison(data) {
  const quota = data.weekly_quota;
  $("comparisonEmpty").style.display = quota ? "none" : "block";
  if (!quota) { $("comparisonList").innerHTML = ""; return; }
  const nodes = new Map((data.nodes || []).map(node => [node.key, node]));
  const rows = (quota.user_usage || []).map(user => {
    const node = nodes.get(user.node_key) || {};
    return { name: node.display_name || node.os_user || user.node_key, detail: node.hostname || "", value: userQuota(user), tokens: user.tokens,
      reason: user.attribution_reason || "관측 구간만 · 주간 전체 사용량 아님", evidence: user.usage_details || [], metadata: user.collector_metadata || {} };
  });
  rows.push({ name: "사용자 미귀속", detail: "개인에게 임의 배분하지 않은 계정 사용분", value: unallocatedQuota(quota), untracked: true });
  $("comparisonList").innerHTML = rows.map(item => `<article class="comparison-row">
    <div class="comparison-user"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.detail)}</small></div>
    <div class="comparison-meter"><div><i style="width:${item.value ?? 0}%;${item.untracked ? "background:var(--amber)" : ""}"></i></div><small>${escapeHtml(item.reason || "동시 사용·관측 전 내역은 미귀속으로 유지")}</small></div>
    <div class="comparison-value"><strong>${item.value == null ? "개인별 실측 없음" : quotaLabel(item.value)}</strong><small>${item.tokens == null ? "사용자 귀속 불가" : `${number.format(item.tokens)} 토큰 · 처리량 참고`}</small></div>
    <span class="comparison-status normal">${item.untracked ? "미귀속" : item.value == null ? "한도 실측 미제공" : "관측 구간만"}</span>
    ${item.untracked ? "" : renderUsageEvidence(item)}
  </article>`).join("");
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
  $("dailyBudget").textContent = quotaLabel(pace.daily_budget_percent);
  $("remainingDailyBudget").textContent = quotaLabel(pace.remaining_daily_budget_percent);
  $("projectedUsage").textContent = quotaLabel(pace.projected_end_percent);
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
  state.accountKey = data.selected_account_key;
  if (typeof AccountsView !== "undefined") AccountsView.render(data);
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
    $("chartLegend").innerHTML = `<i></i> 현재 ${quotaLabel(weekly.used_percent)} · ${untilTime(weekly.resets_at)} 초기화`;
  } else {
    renderChart([]);
    renderPace(null);
    $("chartLegend").innerHTML = "<i></i> 주간 한도 정보 없음 · 누적량으로 대체하지 않습니다";
  }
  renderSessions(data.sessions);
}

async function refresh(force = false) {
  if (state.refreshing && force !== true) return;
  const sequence = ++state.refreshSequence;
  state.refreshing = true;
  $("manualRefresh").disabled = true;
  $("manualRefresh").textContent = "갱신 중…";
  try {
    const response = await fetch("/api/dashboard" + (state.accountKey ? "?account_key=" + encodeURIComponent(state.accountKey) : ""), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (sequence !== state.refreshSequence) return;
    render(data);
    document.querySelector(".connection").classList.remove("error");
    $("connectionText").textContent = "모니터 서버 연결됨";
  } catch (error) {
    if (sequence !== state.refreshSequence) return;
    document.querySelector(".connection").classList.add("error");
    $("connectionText").textContent = "연결 오류";
    console.error(error);
  } finally {
    if (sequence !== state.refreshSequence) return;
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
if (typeof AccountsView !== "undefined") AccountsView.init(key => {
  state.accountKey = key;
  state.month = null;
  state.expanded.clear();
  if (state.explorers) {
    for (const explorer of Object.values(state.explorers)) { explorer.selected = null; explorer.hovered = null; }
  }
  refresh(true);
}, () => refresh(true));
refresh();
setInterval(refresh, 10_000);
