const AccountsView = (() => {
  const element = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
  let registry = { registered: [], discovered: [] }, changeAccount, reload, signature = "", busy = false;
  async function save(payload) {
    if (busy) return;
    busy = true;
    element("accountManagementMessage").textContent = "저장 중…";
    try {
      const response = await fetch("/api/accounts", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      signature = "";
      element("accountManagementMessage").textContent = "저장했습니다. 연결된 수집기에 다음 수집부터 적용됩니다.";
      await reload();
    } catch (error) {
      element("accountManagementMessage").textContent = `저장 실패: ${error.message}`;
    } finally { busy = false; }
  }
  function render(data) {
    registry = data.accounts || registry;
    const select = element("monitorAccountSelect");
    select.innerHTML = registry.registered.map(row => `<option value="${esc(row.account_key)}">${esc(row.label)}${row.enabled ? "" : " · 수집 정지"}</option>`).join("");
    select.value = data.selected_account_key || "";
    select.disabled = !registry.registered.length;
    const current = registry.registered.find(row => row.account_key === data.selected_account_key);
    element("monitorAccountStatus").textContent = current ? (current.enabled ? "수집 활성 · 계정별 집계" : "수집 정지 · 저장된 기록 조회") : "모니터링할 계정을 등록하세요";
    // Preserve an in-progress label edit across automatic refreshes.
    const nextSignature = JSON.stringify([registry.registered, registry.discovered.map(row => [row.account_key, row.account])]);
    if (signature === nextSignature) return;
    signature = nextSignature;
    element("registeredAccounts").innerHTML = registry.registered.map(row => `<div class="registered-account" data-account-key="${esc(row.account_key)}"><label>표시 이름 <input data-label value="${esc(row.label)}" maxlength="120" /></label><span class="account-key">${esc(row.account_key)}</span><button type="button" class="quiet-button" data-action="label">이름 저장</button><button type="button" class="quiet-button" data-action="toggle" aria-pressed="${row.enabled}">${row.enabled ? "수집 정지" : "수집 활성화"}</button><button type="button" class="quiet-button" data-action="default" ${registry.default_account_key === row.account_key ? "disabled" : ""}>${registry.default_account_key === row.account_key ? "기본 조회 계정" : "기본 계정으로 지정"}</button></div>`).join("");
    const registered = new Set(registry.registered.map(row => row.account_key));
    element("discoveredAccountSelect").innerHTML = '<option value="">계정 선택</option>' + registry.discovered.filter(row => !registered.has(row.account_key)).map(row => `<option value="${esc(row.account_key)}">${esc(row.account.email || row.account.name || row.account_key)} · ${esc(row.account_key)}</option>`).join("");
  }
  function init(onChange, onReload) {
    changeAccount = onChange; reload = onReload;
    element("monitorAccountSelect").addEventListener("change", event => changeAccount(event.target.value));
    element("manageAccounts").addEventListener("click", () => { element("accountManagement").open = !element("accountManagement").open; });
    element("discoveredAccountSelect").addEventListener("change", event => {
      element("registerAccountKey").value = event.target.value;
      const candidate = registry.discovered.find(row => row.account_key === event.target.value);
      element("registerAccountLabel").value = candidate?.account.email || candidate?.account.name || "";
    });
    element("registerAccountForm").addEventListener("submit", event => {
      event.preventDefault();
      const label = element("registerAccountLabel").value.trim();
      save({account_key: element("registerAccountKey").value.trim().toLowerCase(), ...(label ? {label} : {}), enabled: true});
    });
    element("registeredAccounts").addEventListener("click", event => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      const container = button.closest("[data-account-key]");
      const row = registry.registered.find(item => item.account_key === container.dataset.accountKey);
      save({account_key: row.account_key, label: button.dataset.action === "label" ? container.querySelector("[data-label]").value.trim() : row.label,
            enabled: button.dataset.action === "toggle" ? !row.enabled : row.enabled, make_default: button.dataset.action === "default"});
    });
  }
  return {render, init};
})();
