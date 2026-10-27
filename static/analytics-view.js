/* Calendar visualizations: pure chart models plus a reusable, accessible controller. */
const AnalyticsView = (() => {
  const fmt = new Intl.NumberFormat("ko-KR");
  const compact = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const amount = value => value == null ? "기록 없음" : fmt.format(value);
  const label = (key, nodes) => { const node = nodes.get(key); return node ? `${node.display_name || node.os_user} · ${node.hostname}` : key; };
  const accountLabel = day => day.scope === "weekly" ? "주간 전체 집계량" : day.account_basis === "observed_delta" ? "계정 관측 최소량" : "계정 보고량";
  const isQuota = day => day?.unit === "quota_percent";
  const formatPercent = typeof module !== "undefined" && module.exports ? require("./usage-view.js").formatPercent : UsageView.formatPercent;
  const percent = value => formatPercent(value, "한도 관측 없음");
  const quotaSummary = day => `${day.scope === "weekly" ? "주간 실제 사용" : "당일 관측 소모"} ${percent(day.quota?.used_percent)}`;

  function quotaReport(report, quota) {
    if (!report) return null;
    const result = { ...report, unit: "quota_percent", days: report.days.map(day => ({
      ...day, unit: "quota_percent", quota: quota?.days?.[day.date] ? {
        ...quota.days[day.date], users: quota.days[day.date].users,
        unattributed_percent: quota.days[day.date].unattributed_percent,
      } : null,
    })) };
    result.overview = report.overview ? { ...report.overview, unit: "quota_percent", quota: quota ? {
      used_percent: quota.used_percent, unattributed_percent: quota.unattributed_percent,
      remaining_percent: quota.remaining_percent,
      users: Object.fromEntries(quota.user_usage.filter(user => user.used_percent != null).map(user => [user.node_key, user.used_percent])),
    } : null } : null;
    return result;
  }

  function segments(day) {
    if (isQuota(day)) {
      const parts = Object.entries(day.quota?.users || {}).filter(([, value]) => value > 0)
        .map(([key, value]) => ({ key, value, share: value, tokens: day.users?.[key] ?? null }))
        .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
      if (day.quota?.unattributed_percent > 0) parts.push({ key: "__untracked", value: day.quota.unattributed_percent, share: day.quota.unattributed_percent, tokens: null });
      if (day.quota?.remaining_percent > 0) parts.push({ key: "__remaining", value: day.quota.remaining_percent, share: day.quota.remaining_percent, tokens: null });
      return parts;
    }
    const parts = Object.entries(day.users || {}).filter(([, tokens]) => tokens > 0)
      .map(([key, tokens]) => ({ key, tokens })).sort((a, b) => b.tokens - a.tokens || a.key.localeCompare(b.key));
    if (day.comparable && day.untracked_tokens > 0) parts.push({ key: "__untracked", tokens: day.untracked_tokens });
    const total = parts.reduce((sum, part) => sum + part.tokens, 0);
    return parts.map(part => ({ ...part, share: day.scope === "weekly" && !day.comparable ? null : total ? part.tokens / total * 100 : 0 }));
  }

  function chartMarkup(days, colors, selected) {
    const left = 65, right = 14, top = 20, bottom = 255, width = 900;
    const step = (width - left - right) / Math.max(1, days.length);
    const quotaMode = days.some(isQuota);
    const max = Math.max(1, ...days.map(day => quotaMode ? day.quota?.used_percent || 0 : Math.max(day.total_tokens || 0, day.account_tokens || 0)));
    const grid = [0, 1, 2, 3].map(i => {
      const y = top + (bottom - top) * i / 3;
      return `<line x1="${left}" x2="${width - right}" y1="${y}" y2="${y}" class="trend-grid-line"/><text x="${left - 10}" y="${y + 4}" text-anchor="end" class="trend-axis">${esc(quotaMode ? percent(max * (3 - i) / 3) : compact.format(max * (3 - i) / 3))}</text>`;
    }).join("");
    const bars = days.map((day, index) => {
      const x = left + index * step, barWidth = Math.min(54, step * .65), middle = x + step / 2;
      let accumulated = 0;
      const parts = segments(day).map(part => {
        const height = (part.value ?? part.tokens) / max * (bottom - top);
        accumulated += height;
        return `<rect x="${middle - barWidth / 2}" y="${bottom - accumulated}" width="${barWidth}" height="${height}" fill="${colors.get(part.key) || "#f4c96b"}"/>`;
      }).join("");
      const empty = parts ? "" : (quotaMode ? day.quota?.used_percent === 0 : day.account_tokens === 0)
        ? `<circle cx="${middle}" cy="${bottom - 3}" r="3" fill="#a3b0bd"><title>${quotaMode ? "관측 한도 증가 0%" : "계정 보고량 0"}</title></circle>`
        : `<line x1="${middle - barWidth / 2}" x2="${middle + barWidth / 2}" y1="${bottom - 2}" y2="${bottom - 2}" class="missing-bar"/>`;
      const tick = days.length <= 10 || index % 3 === 0 || index === days.length - 1;
      const title = quotaMode ? `${day.date} · ${quotaSummary(day)}` : `${day.date} · 사용자 확인 ${amount(day.total_tokens)} · ${accountLabel(day)} ${amount(day.account_tokens)}`;
      const focusable = selected === day.date || (index === 0 && !days.some(item => item.date === selected));
      return `<g data-date="${day.date}" class="date-column ${selected === day.date ? "selected" : ""}" role="button" tabindex="${focusable ? 0 : -1}" aria-label="${esc(title)}" aria-pressed="${selected === day.date}"><rect x="${x + 1}" y="${top}" width="${Math.max(1, step - 2)}" height="${bottom - top}" class="date-highlight"/>${parts}${empty}${tick ? `<text x="${middle}" y="${bottom + 23}" text-anchor="middle" class="trend-axis">${day.date.slice(5).replace("-", "/")}</text>` : ""}<rect x="${x}" y="0" width="${step}" height="300" fill="transparent"/></g>`;
    }).join("");
    return `${grid}${bars}${days.length ? "" : '<text x="450" y="150" text-anchor="middle" class="trend-axis">선택한 범위에 날짜가 없습니다.</text>'}`;
  }

  function breakdownRows(day, nodes, colors) {
    if (isQuota(day)) {
      const parts = segments(day);
      // Retain token-only participants without pretending their quota is zero.
      const known = new Set(parts.map(part => part.key));
      for (const [key, tokens] of Object.entries(day.users || {})) {
        if (tokens > 0 && !known.has(key)) parts.push({ key, tokens, share: day.quota?.users?.[key] ?? null });
      }
      return parts.map(part => `<tr><td><span class="color-dot" style="background:${colors.get(part.key) || (part.key === "__remaining" ? "#3d4855" : "#f4c96b")}"></span>${part.key === "__untracked" ? "사용자 미귀속" : part.key === "__remaining" ? "남은 한도" : esc(label(part.key, nodes))}</td><td>${part.tokens == null ? "—" : fmt.format(part.tokens)}</td><td>${percent(part.share)}</td></tr>`).join("") || '<tr><td colspan="3">한도 관측 기록을 기다리는 중입니다.</td></tr>';
    }
    return segments(day).map(part => `<tr><td><span class="color-dot" style="background:${colors.get(part.key) || "#f4c96b"}"></span><span title="${esc(label(part.key, nodes))}">${part.key === "__untracked" ? "미귀속 / 미확인" : esc(label(part.key, nodes))}</span></td><td>${fmt.format(part.tokens)}</td><td>${formatPercent(part.share, "계산 대기")}</td></tr>`).join("") || `<tr><td colspan="3">${day.account_tokens === 0 ? "계정 보고량 0 · 확인된 사용자 사용량 없음" : "사용자를 확인할 기록이 없습니다."}</td></tr>`;
  }

  function donutMarkup(day, colors) {
    let offset = 0;
    const slices = segments(day).filter(part => part.share != null).map(part => {
      const circle = `<circle cx="90" cy="90" r="68" fill="none" stroke="${colors.get(part.key) || (part.key === "__remaining" ? "#3d4855" : "#f4c96b")}" stroke-width="22" pathLength="100" stroke-dasharray="${part.share} ${100 - part.share}" stroke-dashoffset="${-offset}" transform="rotate(-90 90 90)"/>`;
      offset += part.share;
      return circle;
    }).join("");
    if (isQuota(day)) return `<svg viewBox="0 0 180 180" role="img" aria-label="${esc(quotaSummary(day))} · 제공 한도 100% 기준"><circle cx="90" cy="90" r="68" fill="none" stroke="var(--line)" stroke-width="22"/>${slices}<text x="90" y="87" text-anchor="middle" class="donut-value">${percent(day.quota?.used_percent)}</text><text x="90" y="108" text-anchor="middle" class="donut-caption">${day.scope === "weekly" ? "주간 실제 사용" : "당일 관측 소모"}</text></svg>`;
    const total = day.comparable ? day.account_tokens : (day.total_tokens > 0 ? day.total_tokens : null);
    return `<svg viewBox="0 0 180 180" role="img" aria-label="${esc(day.scope === "weekly" ? "주간 전체" : day.date)} 사용자별 구성 · ${day.comparable ? accountLabel(day) : "사용자 확인량만"} ${amount(total)} 토큰"><circle cx="90" cy="90" r="68" fill="none" stroke="var(--line)" stroke-width="22"/>${slices}<text x="90" y="87" text-anchor="middle" class="donut-value">${total == null ? "—" : esc(compact.format(total))}</text><text x="90" y="108" text-anchor="middle" class="donut-caption">${day.comparable ? accountLabel(day) : "사용자 확인량"}</text></svg>`;
  }

  class Explorer {
    constructor(prefix) {
      this.prefix = prefix; this.selected = null; this.filter = "all"; this.hovered = null;
      this.el = suffix => document.getElementById(prefix + suffix);
      this.el("DayFilter").addEventListener("change", event => { this.filter = event.target.value; this.update(this.report, this.nodesList, this.colors); });
      this.el("DateSelect").addEventListener("change", event => this.select(event.target.value));
      const svg = this.el("DailyChart");
      svg.addEventListener("pointermove", event => {
        const date = event.target.closest("[data-date]")?.dataset.date;
        if (date) this.preview(date, event);
      });
      svg.addEventListener("pointerleave", () => { this.hovered = null; this.el("Tooltip").hidden = true; this.inspect(this.selected); });
      svg.addEventListener("click", event => { const date = event.target.closest("[data-date]")?.dataset.date; if (date) this.select(date); });
      svg.addEventListener("focusin", event => { const date = event.target.closest("[data-date]")?.dataset.date; if (date) this.preview(date); });
      svg.addEventListener("focusout", () => { this.el("Tooltip").hidden = true; });
      svg.addEventListener("keydown", event => {
        const date = event.target.closest("[data-date]")?.dataset.date;
        if (!date) return;
        const index = this.days.findIndex(day => day.date === date);
        if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? this.days.length - 1 : Math.max(0, Math.min(this.days.length - 1, index + (event.key === "ArrowRight" ? 1 : -1)));
          this.select(this.days[next].date); svg.querySelector(`[data-date="${this.selected}"]`)?.focus();
        } else if (["Enter", " "].includes(event.key)) { event.preventDefault(); this.select(date); }
        else if (event.key === "Escape") { this.el("Tooltip").hidden = true; this.inspect(this.selected); }
      });
    }

    update(report, nodesList, colors) {
      this.report = report; this.nodesList = nodesList; this.nodes = new Map(nodesList.map(node => [node.key, node])); this.colors = colors;
      this.days = (report?.days || []).filter(day => this.filter === "all" || day.day_type === this.filter);
      this.overview = this.filter === "all" ? report?.overview : null;
      if (!(this.overview && this.selected === "__weekly") && !this.days.some(day => day.date === this.selected)) {
        this.selected = this.overview ? "__weekly" : [...this.days].reverse().find(day => day.total_tokens > 0 || day.account_tokens != null)?.date || this.days[0]?.date || null;
      }
      const focused = this.el("DailyChart").contains(document.activeElement) ? document.activeElement?.dataset.date : null;
      this.el("DailyChart").innerHTML = chartMarkup(this.days, colors, this.selected);
      if (focused) this.el("DailyChart").querySelector(`[data-date="${focused}"]`)?.focus({ preventScroll: true });
      this.el("DateSelect").innerHTML = (this.overview ? '<option value="__weekly">주간 전체</option>' : '') + this.days.map(day => `<option value="${day.date}">${day.date}${day.status === "future" ? " · 예정" : ""}</option>`).join("");
      this.el("DateSelect").value = this.selected || "";
      this.el("Range").textContent = report ? `${report.start}부터 · UTC · ${this.days.length}일` : "주간 한도 정보를 기다리는 중입니다.";
      const keys = [...new Set(this.days.flatMap(day => Object.keys(day.users)))].sort();
      this.el("Legend").innerHTML = keys.map(key => `<span><i style="background:${colors.get(key)}"></i>${esc(label(key, this.nodes))}</span>`).join("") + '<span><i style="background:#f4c96b"></i>미귀속 / 미확인</span>';
      const observed = this.days.filter(day => day.account_basis === "observed_delta").length;
      this.el("CoverageNote").textContent = `계정량 확인 ${report?.reported_days || 0}/${report?.elapsed_days || 0}일.${observed ? ` 이 중 ${observed}일은 해당 UTC 날짜 안에서 관측한 누적 증가분의 최소량입니다.` : " 일별 기록과 같은 UTC 날짜로 비교합니다."}${this.overview ? " 주간 전체를 선택하면 아래 주간 구성과 동일한 사용자량·미귀속량·비중을 확인합니다." : ""} 분 단위 이벤트와 생성·마지막 활동이 같은 날짜인 세션만 날짜에 배정하며, 날짜 미확인 사용자량은 주간 전체에 포함됩니다. 날짜별 미귀속에는 날짜를 확인하지 못한 사용자량도 포함될 수 있습니다. 데이터가 부족하면 사용자 확인량 기준 비중만 표시합니다.`;
      if (report?.unit === "quota_percent") this.el("CoverageNote").textContent = "사용자별 수치는 단독 활동과 한도 증가가 일치한 관측 구간만 표시합니다. 해당 사용자의 주간 전체 소모율이 아닙니다. 나머지는 미귀속으로 남기며 토큰 비례로 나누지 않습니다. 자정을 넘는 증가분은 특정 날짜에 배분하지 않습니다. 점선은 해당 날짜의 한도 보고 기록이 없다는 뜻입니다.";
      if (this.hovered && this.days.some(day => day.date === this.hovered)) this.preview(this.hovered);
      else { this.el("Tooltip").hidden = true; this.inspect(this.selected); }
    }

    select(date) {
      this.selected = date; this.hovered = null; this.el("DateSelect").value = date;
      this.el("Tooltip").hidden = true;
      const focusDate = this.days.some(day => day.date === date) ? date : this.days[0]?.date;
      for (const column of this.el("DailyChart").querySelectorAll("[data-date]")) {
        const active = column.dataset.date === date;
        column.classList.toggle("selected", active); column.setAttribute("aria-pressed", active); column.setAttribute("tabindex", column.dataset.date === focusDate ? "0" : "-1");
      }
      this.inspect(date);
    }

    inspect(date) {
      const day = date === "__weekly" ? this.overview : this.days.find(day => day.date === date);
      this.el("SelectedDate").textContent = date === "__weekly" ? "주간 전체" : date || "날짜 없음";
      if (!day) { this.el("Donut").innerHTML = ""; this.el("DayRows").innerHTML = ""; this.el("DayNote").textContent = "확인할 데이터가 없습니다."; return; }
      this.el("Donut").innerHTML = donutMarkup(day, this.colors);
      this.el("DayRows").innerHTML = breakdownRows(day, this.nodes, this.colors);
      if (isQuota(day)) {
        this.el("DayNote").textContent = day.quota
          ? `${quotaSummary(day)} · 주간 제공량 100% 기준. 사용자별 수치는 관측 구간만이며 주간 전체 소모율이 아닙니다. 토큰은 처리량 참고값입니다.`
          : "이 날짜에는 계정 한도 사용률 보고가 없어 당일 증가분을 복원할 수 없습니다. 주간 전체를 선택하면 현재 실제 사용률에 맞춘 계정 전체 실제 사용률과 사용자별 관측 구간을 확인할 수 있습니다.";
        return;
      }
      const shareNote = day.comparable ? "계정 집계량 기준 비중" : day.scope === "weekly" ? "계정 집계 대기 · 주간 비중 계산 불가" : "계정량 미확인 또는 집계 지연 · 사용자 확인량만의 비중";
      this.el("DayNote").textContent = `${day.participant_count}명 · ${accountLabel(day)} ${amount(day.account_tokens)} / 사용자 ${amount(day.total_tokens)} 토큰. ${shareNote}${day.scope === "weekly" ? " · 아래 주간 구성과 같은 기준 · 날짜 미확인량 포함" : ""}${day.is_lower_bound ? " · 최소 집계량 기준" : ""}${day.status === "partial" ? " · 진행 중/주간 경계일" : ""}${day.status === "future" ? " · 아직 오지 않은 날짜" : ""}`;
    }

    preview(date, event) {
      const day = this.days.find(day => day.date === date);
      if (!day) return false;
      this.hovered = date; this.inspect(date);
      const tooltip = this.el("Tooltip");
      tooltip.innerHTML = `<strong>${day.date} · UTC</strong><p>${accountLabel(day)} ${amount(day.account_tokens)} · 사용자 ${amount(day.total_tokens)} 토큰</p><table class="day-table"><tbody>${breakdownRows(day, this.nodes, this.colors)}</tbody></table><small>${day.comparable ? "계정 집계량 대비" : "사용자 확인량 내 비중 · 미귀속량 확인 불가"}${day.account_basis === "observed_delta" ? " · 해당 날짜 안의 관측 증가분만 포함" : ""}</small>`;
      if (isQuota(day)) tooltip.innerHTML = `<strong>${day.date} · UTC</strong><p>${quotaSummary(day)}</p><table class="day-table"><tbody>${breakdownRows(day, this.nodes, this.colors)}</tbody></table><small>주간 제공량 100% 기준 · 사용자별 관측 구간만 · 토큰은 처리량 참고</small>`;
      tooltip.hidden = false;
      const bounds = this.el("DailyChart").getBoundingClientRect();
      const x = event ? event.clientX - bounds.left : bounds.width / 2;
      tooltip.style.left = `${Math.max(0, Math.min(x + 12, bounds.width - Math.min(360, bounds.width)))}px`;
      tooltip.style.top = "12px";
      return true;
    }
  }

  return { Explorer, segments, chartMarkup, donutMarkup, breakdownRows, quotaReport };
})();
if (typeof module !== "undefined") module.exports = AnalyticsView;
