const fullScope = document.querySelector("#full-scope");
const projectOptions = document.querySelector("#project-options");
const runButton = document.querySelector("#run");
const rerunButton = document.querySelector("#rerun");
const selectedStatus = document.querySelector("#selected-status");
const message = document.querySelector("#message");
const latestReport = document.querySelector("#quality-report");

let statusData = null;
let overviewData = null;
let csrfToken = "";
let submitting = false;
let refreshing = false;

function selectedProjects() {
  return [...projectOptions.querySelectorAll("input[data-project]:checked")]
    .map((input) => input.dataset.project);
}

function selection() {
  return { full: fullScope.checked, projects: selectedProjects() };
}

function previousProjectRun(project) {
  const info = statusData?.projects?.[project];
  return info?.last || overviewData?.history?.find((item) => item.project === project);
}

function selectedBusy() {
  const current = selection();
  if (current.full) return statusData?.full?.busy || false;
  return current.projects.some((project) => statusData?.projects?.[project]?.busy);
}

function updateSelection() {
  const current = selection();
  const selected = current.full || current.projects.length > 0;
  const busy = selectedBusy();
  const available = Boolean(statusData) && selected && !busy && !submitting;
  const hasHistory = current.full
    ? Boolean(statusData?.full?.last || overviewData?.history?.some((item) => item.project === "__full__"))
    : current.projects.length > 0 && current.projects.every((project) => Boolean(previousProjectRun(project)));

  runButton.disabled = !available;
  rerunButton.hidden = !selected || !hasHistory;
  rerunButton.disabled = !available;

  if (!selected) {
    selectedStatus.textContent = "选择一个或多个项目运行增量测试，或选择全量测试。";
    return;
  }

  if (busy) {
    selectedStatus.textContent = "所选范围中有任务正在排队或运行。";
    return;
  }

  if (current.full) {
    const lastRun = statusData?.full?.last || overviewData?.history?.find((item) => item.project === "__full__");
    selectedStatus.textContent = lastRun
      ? `全量测试上次运行：${lastRun.result || lastRun.status || "结果未知"} · ${lastRun.finished_at || "时间未知"}`
      : "全量测试会运行安全配置中的全部项目。";
    return;
  }

  selectedStatus.textContent = `${current.projects.length} 个项目将组成一个测试批次。`;
  if (hasHistory && current.projects.length === 1) {
    const lastRun = previousProjectRun(current.projects[0]);
    selectedStatus.textContent = `上次运行：${lastRun.result || lastRun.status || "结果未知"} · ${lastRun.finished_at || "时间未知"}`;
  }
}

function renderQuality(data) {
  const full = data.full || {};
  const summary = full.summary || {};
  const total = Number(summary.total || 0);
  const passed = Number(summary.passed || 0);
  const failed = Number(summary.failed || 0);
  const skipped = Number(summary.skipped || 0);
  const state = document.querySelector("#quality-state");

  if (!total) {
    state.textContent = full.busy ? "全量测试正在排队或运行" : "尚无全量结果";
    document.querySelector("#quality-summary").textContent = full.busy
      ? "任务完成后，最新质量结果会显示在这里。"
      : "运行全量测试后，结果会显示在这里。";
    document.querySelector("#quality-score").textContent = "—";
    latestReport.hidden = true;
    return;
  }

  const passedRun = full.last?.status === "passed";
  state.textContent = passedRun ? "全量测试通过" : "全量测试未通过";
  state.dataset.result = passedRun ? "passed" : "failed";
  document.querySelector("#quality-summary").textContent =
    `${passed}/${total} 项通过 · ${failed} 项失败 · ${skipped} 项跳过 · ${full.last?.finished_at || "时间未知"}`;
  document.querySelector("#quality-score").textContent = `${Math.round((passed * 100) / total)}%`;

  const report = overviewData?.history?.find((item) => item.project === "__full__")?.report;
  latestReport.hidden = !report;
  if (report) latestReport.href = report;
}

function projectStatusLabel(item) {
  if (item.busy) return { label: item.busy, state: "busy" };
  const result = item.status || "pending";
  const labels = { passed: "通过", failed: "失败", pending: "等待测试" };
  return { label: labels[result] || "运行失败", state: result };
}

function renderProjects(data) {
  const grid = document.querySelector("#project-grid");
  grid.replaceChildren();

  for (const [name, item] of Object.entries(data.projects || {})) {
    const row = document.createElement("div");
    row.className = "project-row";

    const title = document.createElement("strong");
    title.textContent = name;

    const status = projectStatusLabel(item);
    const badge = document.createElement("span");
    badge.className = `status status-${status.state}`;
    badge.textContent = status.label;

    const detail = document.createElement("span");
    detail.className = "project-detail muted";
    detail.textContent = item.last?.finished_at || "暂无运行记录";

    row.append(title, detail, badge);
    grid.append(row);
  }
}

function renderQueue(queue) {
  const list = document.querySelector("#queue-list");
  const count = document.querySelector("#queue-count");
  list.replaceChildren();

  const active = queue?.active;
  const pending = queue?.pending || [];
  const taskCount = pending.length + (active ? 1 : 0);
  count.textContent = taskCount ? `${taskCount} 项任务` : "空闲";
  count.dataset.busy = String(taskCount > 0);

  if (active) {
    list.append(createQueueItem(active, "正在运行", "active"));
  }
  pending.forEach((task, index) => {
    const deferred = task.deferred_until ? ` · 预计 ${task.deferred_until}` : "";
    list.append(createQueueItem(task, `排队第 ${index + 1} 项${deferred}`, "pending"));
  });

  const recent = (queue?.recent || []).filter((item) => item.result);
  if (recent.length) {
    const heading = document.createElement("p");
    heading.className = "queue-section-label";
    heading.textContent = "最近完成的批次";
    list.append(heading);
    recent.slice(0, 5).forEach((item) => {
      const date = item.finished_at ? new Date(item.finished_at).toLocaleString() : "时间未知";
      list.append(createQueueItem(item, `${item.result} · ${date}`, "recent"));
    });
  }

  if (!taskCount) {
    const empty = document.createElement("p");
    empty.className = "queue-empty muted";
    empty.textContent = "当前没有运行中的测试任务。";
    list.append(empty);
  }
}

function createQueueItem(task, state, kind) {
  const row = document.createElement("div");
  row.className = `queue-item queue-${kind}`;
  const marker = document.createElement("span");
  marker.className = "queue-marker";
  marker.setAttribute("aria-hidden", "true");
  const details = document.createElement("div");
  const title = document.createElement("strong");
  const names = task.projects || [];
  title.textContent = task.scope === "full" || task.project === "__full__"
    ? "全量测试"
    : names.length > 1 ? `${names.length} 个项目 · ${names.slice(0, 3).join("、")}${names.length > 3 ? "…" : ""}`
      : names[0] || task.project;
  const meta = document.createElement("span");
  meta.className = "muted";
  const triggers = { manual: "手动", main: "main 更新", "daily-schedule": "定时全量", "tester-main": "Tester 更新" };
  const batchId = task.batch_id || task.id;
  const batch = batchId ? ` · 批次 ${batchId.slice(0, 8)}` : "";
  const revisions = Object.entries(task.target_shas || {}).slice(0, 2)
    .map(([project, revision]) => `${project}@${revision.slice(0, 7)}`);
  if (task.target_sha && task.source === "tester-main") revisions.push(`tester@${task.target_sha.slice(0, 7)}`);
  const revisionLabel = revisions.length ? ` · ${revisions.join("、")}` : "";
  meta.textContent = `${state} · ${triggers[task.source] || task.source || "测试"}触发 · ${task.mode === "deploy" ? "部署并测试" : "测试"}${batch}${revisionLabel}`;
  details.append(title, meta);
  row.append(marker, details);
  return row;
}

function renderHistory(history) {
  const list = document.querySelector("#history-list");
  list.replaceChildren();

  for (const item of history.slice(0, 8)) {
    const row = document.createElement("div");
    row.className = "history-row";
    const details = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.project === "__full__" ? "全量测试" : item.project;
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = `${item.status || item.result || "结果未知"} · ${item.finished_at || "时间未知"}`;
    details.append(title, meta);
    row.append(details);

    if (item.report?.startsWith("/report/")) {
      const link = document.createElement("a");
      link.href = item.report;
      link.textContent = "查看报告";
      row.append(link);
    }
    list.append(row);
  }

  if (!list.children.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无运行记录。";
    list.append(empty);
  }
}

function renderOverview(data) {
  overviewData = data;
  renderQuality(data);
  renderHistory(data.history || []);
}

async function loadUsers() {
  const response = await fetch("/api/auth/users", { cache: "no-store" });
  if (!response.ok) throw new Error("成员列表暂时不可用，请稍后重试。");
  const users = await response.json();
  const list = document.querySelector("#user-list");
  list.replaceChildren();

  for (const user of users) {
    const row = document.createElement("div");
    row.className = "user-row";
    const details = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = user.display_name || user.did.slice(-10);
    const did = document.createElement("span");
    did.className = "identity muted";
    did.textContent = user.did;
    const state = document.createElement("span");
    state.className = "muted";
    state.textContent = user.role === "admin" ? "管理员" : user.status === "pending" ? "等待授权" : user.status === "active" ? "已授权" : "已停用";
    details.append(title, did, state);
    row.append(details);

    if (user.role !== "admin") {
      const action = document.createElement("button");
      action.className = "button button-quiet";
      action.textContent = user.status === "pending" ? "批准访问" : user.status === "active" ? "撤销访问" : "重新授权";
      action.addEventListener("click", async () => {
        action.disabled = true;
        const operation = user.status === "pending" || user.status === "disabled" ? "approve" : "revoke";
        const result = await fetch(`/api/auth/users/${operation}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Tester-CSRF": csrfToken },
          body: JSON.stringify({ did: user.did }),
        });
        if (result.ok) await loadUsers();
        else action.disabled = false;
      });
      row.append(action);
    }
    list.append(row);
  }
}

function updateStatusControls(data) {
  const selected = new Set(selectedProjects());
  projectOptions.replaceChildren();
  for (const name of Object.keys(data.projects || {})) {
    const label = document.createElement("label");
    label.className = "scope-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.project = name;
    input.checked = selected.has(name);
    const title = document.createElement("span");
    title.textContent = name;
    label.append(input, title);
    projectOptions.append(label);
  }
  if (fullScope.checked) {
    projectOptions.querySelectorAll("input[data-project]").forEach((input) => { input.checked = false; });
  }
  renderProjects(data);
  renderQueue(data.queue);
  updateSelection();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const authResponse = await fetch("/api/auth/status", { cache: "no-store" });
    if (authResponse.status === 401) {
      clearAnalysisToken();
      document.querySelector("#login-card").hidden = false;
      document.querySelector("#session-bar").hidden = true;
      document.querySelector("#console").hidden = true;
      document.querySelector("#service-error").hidden = true;
      return;
    }
    if (!authResponse.ok) throw new Error("身份状态暂时无法读取，请稍后重试。");

    const identity = await authResponse.json();
    if (!identity.authenticated) throw new Error("登录状态已失效，请重新登录。");
    csrfToken = identity.csrf;
    document.querySelector("#login-card").hidden = true;
    document.querySelector("#session-bar").hidden = false;
    document.querySelector("#console").hidden = false;
    document.querySelector("#service-error").hidden = true;
    document.querySelector("#identity").textContent = identity.did;
    document.querySelector("#admin-tools").hidden = identity.role !== "admin";
    if (identity.role !== "admin") clearAnalysisToken();

    const [statusResponse, overviewResponse] = await Promise.all([
      fetch("/api/status", { cache: "no-store" }),
      fetch("/api/overview", { cache: "no-store" }),
    ]);
    if (!statusResponse.ok) throw new Error("测试状态暂时无法读取，请稍后重试。");
    statusData = await statusResponse.json();
    if (overviewResponse.ok) renderOverview(await overviewResponse.json());
    updateStatusControls(statusData);
  } catch (error) {
    const serviceError = document.querySelector("#service-error");
    serviceError.textContent = error.message || "Tester 控制服务暂时无法连接，请稍后重试。";
    serviceError.hidden = false;
  } finally {
    refreshing = false;
  }
}

async function submitRun(isRerun) {
  const current = selection();
  if ((!current.full && !current.projects.length) || submitting || selectedBusy()) return;
  submitting = true;
  message.textContent = isRerun ? "正在提交重跑请求…" : "正在提交测试请求…";
  updateSelection();

  try {
    const body = current.full ? { scope: "full" } : { scope: "projects", projects: current.projects };
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tester-CSRF": csrfToken },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "测试请求提交失败，请稍后重试。");
    const label = current.full ? "全量测试" : `${current.projects.length} 个项目的测试批次`;
    message.textContent = `${label}已加入队列。`;
    await refresh();
  } catch (error) {
    message.textContent = error.message || "请求失败，请检查网络后重试。";
  } finally {
    submitting = false;
    updateSelection();
  }
}

fullScope.addEventListener("change", () => {
  if (fullScope.checked) projectOptions.querySelectorAll("input[data-project]").forEach((input) => { input.checked = false; });
  updateSelection();
});
projectOptions.addEventListener("change", (event) => {
  if (event.target.matches("input[data-project]:checked")) fullScope.checked = false;
  updateSelection();
});
runButton.addEventListener("click", () => submitRun(false));
rerunButton.addEventListener("click", () => submitRun(true));

document.querySelector("#users-toggle").addEventListener("click", async () => {
  const panel = document.querySelector("#user-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadUsers();
});

const tokenPanel = document.querySelector("#analysis-tokens");
const tokenForm = document.querySelector("#analysis-token-form");
const tokenStatus = document.querySelector("#analysis-token-status");
const tokenSecret = document.querySelector("#analysis-token-secret");
const tokenInput = tokenSecret.querySelector("input");

function clearAnalysisToken() {
  tokenInput.value = "";
  tokenInput.type = "password";
  tokenSecret.hidden = true;
}

async function tokenRequest(operation = "", body) {
  const options = body ? {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Tester-CSRF": csrfToken },
    body: JSON.stringify(body),
  } : { cache: "no-store" };
  const response = await fetch("/api/auth/analysis-tokens" + operation, options);
  if (!response.ok) throw new Error("Token 操作失败（HTTP " + response.status + "）。");
  return response.json();
}

async function loadAnalysisTokens() {
  const { tokens } = await tokenRequest();
  const list = document.querySelector("#analysis-token-list");
  list.replaceChildren();
  for (const token of tokens) {
    const row = document.createElement("div");
    row.className = "user-row";
    const info = document.createElement("span");
    const expired = Date.parse(token.expires_at) <= Date.now();
    const status = token.revoked_at ? "已撤销" : expired ? "已到期" : "有效";
    info.textContent = `${token.label} · ${status} · 到期 ${new Date(token.expires_at).toLocaleString()}`;
    row.append(info);
    if (!token.revoked_at && !expired) {
      const revoke = document.createElement("button");
      revoke.className = "button button-quiet";
      revoke.type = "button";
      revoke.textContent = "撤销";
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        try {
          await tokenRequest("/revoke", { id: token.id });
          clearAnalysisToken();
          await loadAnalysisTokens();
        } catch (error) {
          tokenStatus.textContent = error.message;
          revoke.disabled = false;
        }
      });
      row.append(revoke);
    }
    list.append(row);
  }
}

tokenPanel.addEventListener("toggle", async () => {
  if (!tokenPanel.open) {
    clearAnalysisToken();
    return;
  }
  tokenStatus.textContent = "";
  try {
    await loadAnalysisTokens();
  } catch (error) {
    tokenStatus.textContent = error.message;
  }
});

tokenForm.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = tokenForm.querySelector("button");
  submit.disabled = true;
  clearAnalysisToken();
  try {
    const form = new FormData(tokenForm);
    const { token } = await tokenRequest("", { label: form.get("label"), days: Number(form.get("days")) });
    if (tokenPanel.open && csrfToken) {
      tokenInput.value = token;
      tokenSecret.hidden = false;
    }
    tokenStatus.textContent = "已生成只读 Token。";
    tokenForm.reset();
    await loadAnalysisTokens();
  } catch (error) {
    tokenStatus.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#analysis-token-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(tokenInput.value);
    tokenStatus.textContent = "Token 已复制。";
  } catch {
    tokenInput.type = "text";
    tokenInput.select();
    tokenStatus.textContent = "请手动复制选中的 Token。";
  }
});

document.querySelector("#login").addEventListener("click", async () => {
  const loginMessage = document.querySelector("#login-message");
  loginMessage.textContent = "请在钱包中确认身份授权…";
  try {
    if (!window.YeYingWeb3?.loginWithWalletIdentity) {
      throw new Error("钱包登录组件暂不可用，请刷新后重试。");
    }
    await YeYingWeb3.loginWithWalletIdentity({
      baseUrl: "/api/auth",
      provider: window.ethereum,
      scopes: ["identity.basic", "identity.wallet", "identity.username", "identity.email"],
      storeToken: false,
    });
    loginMessage.textContent = "";
    await refresh();
  } catch (error) {
    const errorCode = String(error?.payload?.message || error?.message || "");
    const messages = {
      WALLET_PERMISSIONS_INVALID: "钱包权限请求没有返回有效结果，请重新授权。",
      IDENTITY_WALLET_NOT_VERIFIED: "钱包身份还没有完成钱包绑定验证。",
      IDENTITY_EMAIL_NOT_VERIFIED: "钱包身份还没有完成邮箱验证。",
      IDENTITY_USERNAME_NOT_VERIFIED: "钱包身份还没有完成用户名验证。",
      IDENTITY_CREDENTIAL_REISSUE_FAILED: "钱包身份凭证续签失败，请先在钱包中完成身份验证后重试。",
      "Wallet is locked": "请先解锁钱包，再重试登录。",
    };
    if (errorCode.includes("等待管理员审批")) loginMessage.textContent = "授权申请已提交，管理员批准后即可登录。";
    else loginMessage.textContent = messages[errorCode] || "登录未完成，请查看钱包提示后重试。";
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  clearAnalysisToken();
  await fetch("/api/auth/logout", {
    method: "POST",
    headers: { "X-Tester-CSRF": csrfToken },
  });
  csrfToken = "";
  await refresh();
});

refresh();
window.setInterval(refresh, 5000);
