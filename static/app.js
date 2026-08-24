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

function renderSummary(data) {
  $("totalTokens").textContent = number.format(data.summary.total_tokens);
  $("sessionCount").textContent = number.format(data.summary.session_count);
  $("activeCount").textContent = number.format(data.summary.active_count);
  $("projectCount").textContent = number.format(data.summary.project_count);
  $("modelCount").textContent = number.format(data.summary.model_count);
  $("lastUpdated").textContent = new Date(data.meta.collected_at).toLocaleTimeString("ko-KR");
  $("refreshLabel").textContent = `${data.meta.refresh_seconds}초마다 자동 새로고침`;
}

function renderChart(history) {
  const svgWidth = 960, svgHeight = 260, pad = 12;
  const grid = $("gridLines");
  grid.innerHTML = [0, 1, 2, 3].map(i => {
    const y = pad + i * ((svgHeight - pad * 2) / 3);
    return `<line class="grid-line" x1="0" y1="${y}" x2="${svgWidth}" y2="${y}" />`;
  }).join("");
  if (!history.length) {
    $("emptyChart").style.display = "grid";
    $("linePath").setAttribute("d", ""); $("areaPath").setAttribute("d", "");
    return;
  }
  $("emptyChart").style.display = history.length < 2 ? "grid" : "none";
  if (history.length < 2) return;
  const values = history.map(point => point.tokens);
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min = Math.max(0, min * .98); max = max * 1.02 || 1; }
  const points = history.map((point, index) => {
    const x = index * (svgWidth / (history.length - 1));
    const y = pad + (max - point.tokens) / (max - min) * (svgHeight - pad * 2);
    return [x, y];
  });
  const line = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${svgWidth},${svgHeight} L0,${svgHeight} Z`;
  $("linePath").setAttribute("d", line); $("areaPath").setAttribute("d", area);
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
      projects.set(key, { key, name: session.project_name, sessions: 0, tokens: 0, delta: 0, active: false, updatedAt: session.updated_at, models: new Set() });
    }
    const project = projects.get(key);
    project.sessions += 1;
    project.tokens += session.tokens;
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
      <span class="project-stat tokens">${number.format(project.tokens)}</span>
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
  state.data = data; renderSummary(data); renderAccounts(data.nodes); renderChart(data.history); renderSessions(data.sessions);
}

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    document.querySelector(".connection").classList.remove("error");
    $("connectionText").textContent = "로컬 DB 연결됨";
  } catch (error) {
    document.querySelector(".connection").classList.add("error");
    $("connectionText").textContent = "연결 오류";
    console.error(error);
  }
}

$("searchInput").addEventListener("input", event => { state.query = event.target.value; if (state.data) renderSessions(state.data.sessions); });
refresh();
setInterval(refresh, 10_000);
