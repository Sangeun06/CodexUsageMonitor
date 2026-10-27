/* Pure grouping helpers shared by the session table and project details. */
const UsageView = (() => {
  const percentNumber = new Intl.NumberFormat("ko-KR", {minimumFractionDigits: 2, maximumFractionDigits: 4});
  function formatPercent(value, missing = "관측 대기") {
    if (value == null || !Number.isFinite(Number(value))) return missing;
    const numeric = Number(value);
    if (numeric > 0 && numeric < 0.0001) return "<0.0001%";
    return `${percentNumber.format(numeric)}%`;
  }
  const sortGroups = groups => [...groups.values()].sort((a, b) => Number(b.active) - Number(a.active) || b.tokens - a.tokens);
  const weeklyAmount = session => session.weekly_tokens ?? session.verified_weekly_tokens ?? null;

  function aggregateProjects(sessions) {
    const projects = new Map();
    for (const session of sessions) {
      const key = session.project_key || session.project_name;
      if (!projects.has(key)) {
        projects.set(key, { key, name: session.project_name, sessions: 0, tokens: 0, weeklyTokens: 0, weeklySessions: 0, delta: 0, active: false, updatedAt: session.updated_at, models: new Set() });
      }
      const project = projects.get(key);
      project.sessions += 1;
      project.tokens += session.tokens;
      if (weeklyAmount(session) != null) {
        project.weeklyTokens += weeklyAmount(session);
        project.weeklySessions += 1;
      }
      project.delta += session.delta;
      project.active ||= session.active;
      project.models.add(session.model);
      if (new Date(session.updated_at) > new Date(project.updatedAt)) project.updatedAt = session.updated_at;
    }
    return sortGroups(projects);
  }

  function aggregateNodes(sessions, weeklyUsers = null) {
    const groups = new Map();
    const weeklyByNode = new Map((weeklyUsers || []).map(user => [user.node_key, user.tokens]));
    for (const session of sessions) {
      const key = session.node_key || `${session.hostname}:${session.os_user}`;
      if (!groups.has(key)) {
        groups.set(key, { key, hostname: session.hostname, osUser: session.os_user, displayUser: session.display_user || session.os_user, sessions: [], tokens: 0, weeklyTokens: weeklyUsers === null ? null : (weeklyByNode.get(key) || 0), delta: 0, active: false, updatedAt: session.updated_at, models: new Set() });
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
    return sortGroups(groups);
  }

  return { aggregateProjects, aggregateNodes, formatPercent };
})();

if (typeof module !== "undefined") module.exports = UsageView;
