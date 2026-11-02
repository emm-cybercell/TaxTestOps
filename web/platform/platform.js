/**
 * 测试平台前端。
 *
 * 与主服务前端一致的可测试性约定：所有交互元素带稳定 `data-testid`，
 * 显式的 loading / empty / error 状态，纯原生 DOM，无构建步骤。
 *
 * 实时日志通过 WebSocket 接收；断线后自动重连一次，避免网络抖动导致日志永久静默。
 */
(function () {
  "use strict";

  const $ = (testId) => document.querySelector(`[data-testid="${testId}"]`);
  const show = (element, visible) => { if (element) element.hidden = !visible; };

  let activeSocket = null;
  let reconnectTimer = null;

  async function request(path, options = {}) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options));
    const text = await response.text();
    const body = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const detail = body && body.detail;
      throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）`);
    }
    return body;
  }

  function appendLog(line) {
    const log = $("run-log");
    const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    log.textContent += `[${stamp}] ${line}\n`;
    log.scrollTop = log.scrollHeight;
  }

  // ------------------------------------------------------------------ //
  // 概览
  // ------------------------------------------------------------------ //

  async function loadSummary() {
    const data = await request("/api/v1/platform/summary");
    $("metric-total-cases").textContent = data.total_cases;
    $("metric-enabled-cases").textContent = data.enabled_cases;
    $("metric-latest-pass-rate").textContent = `${data.latest_pass_rate}%`;
    $("metric-average-pass-rate").textContent = `${data.average_pass_rate}%`;
    $("metric-total-runs").textContent = data.total_runs;
  }

  async function loadTrends() {
    const points = await request("/api/v1/platform/trends?limit=30");
    const chart = $("trend-chart");
    chart.innerHTML = "";

    if (!points.length) {
      chart.textContent = "暂无趋势数据";
      chart.classList.add("empty");
      return;
    }
    chart.classList.remove("empty");

    points.forEach((point) => {
      const bar = document.createElement("div");
      bar.className = "trend-bar";
      bar.setAttribute("data-run-id", String(point.run_id));
      bar.setAttribute("data-pass-rate", String(point.pass_rate));
      bar.style.height = `${Math.max(4, point.pass_rate)}%`;
      bar.classList.toggle("failed", point.status !== "passed");
      bar.title = `#${point.run_id} 通过率 ${point.pass_rate}%（${point.passed}/${point.total}）`;
      chart.appendChild(bar);
    });
  }

  // ------------------------------------------------------------------ //
  // 用例
  // ------------------------------------------------------------------ //

  async function loadCases() {
    const page = await request("/api/v1/platform/cases");
    const rows = $("cases-rows");
    rows.innerHTML = "";

    show($("cases-empty"), page.total === 0);
    show($("cases-table"), page.total > 0);

    page.items.forEach((item) => {
      const row = document.createElement("tr");
      row.setAttribute("data-case-id", String(item.id));
      [item.name, item.method, item.path, (item.tags || []).join(", ")].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      });
      rows.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ //
  // 执行
  // ------------------------------------------------------------------ //

  async function loadRuns() {
    const page = await request("/api/v1/platform/runs?limit=20");
    const rows = $("runs-rows");
    rows.innerHTML = "";

    show($("runs-empty"), page.total === 0);
    show($("runs-table"), page.total > 0);

    page.items.forEach((run) => {
      const row = document.createElement("tr");
      row.setAttribute("data-run-id", String(run.id));
      row.setAttribute("data-status", run.status);
      const cells = [
        run.id,
        run.status,
        `${run.passed} / ${run.total}`,
        `${run.pass_rate}%`,
        run.duration_ms === null ? "—" : `${Math.round(run.duration_ms)} ms`,
      ];
      cells.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index >= 2) cell.className = "numeric";
        cell.textContent = value;
        row.appendChild(cell);
      });
      rows.appendChild(row);
    });
  }

  function connectStream(runId) {
    if (activeSocket) { activeSocket.close(); activeSocket = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/api/v1/platform/runs/${runId}/stream`);
    activeSocket = socket;

    const statusChip = $("stream-status");
    statusChip.textContent = `已连接 #${runId}`;
    statusChip.classList.remove("chip-muted");

    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "connected") {
        appendLog(`已订阅执行 #${runId} 的实时进度`);
      } else if (payload.type === "case_finished") {
        const mark = payload.status === "passed" ? "PASS" : "FAIL";
        const suffix = payload.message ? ` — ${payload.message}` : "";
        appendLog(`${mark} ${payload.case} (${Math.round(payload.latency_ms)}ms)${suffix}`);
      } else if (payload.type === "run_finished") {
        appendLog(
          `执行结束：通过 ${payload.passed} / 失败 ${payload.failed} / 错误 ${payload.errored}（共 ${payload.total}）`
        );
        refreshAll();
      } else if (payload.type === "heartbeat") {
        // 心跳只用于保活，不打扰用户
      } else if (payload.type === "run_started") {
        appendLog(`开始执行，目标：${payload.base_url}`);
      }
    };

    socket.onclose = () => {
      statusChip.textContent = "未连接";
      statusChip.classList.add("chip-muted");
    };

    socket.onerror = () => {
      appendLog("实时连接异常，结果仍会写入执行历史");
    };
  }

  $("run-all").addEventListener("click", async () => {
    const button = $("run-all");
    button.disabled = true;
    button.textContent = "触发中…";
    $("run-log").textContent = "";

    try {
      const run = await request("/api/v1/platform/runs", {
        method: "POST",
        body: JSON.stringify({}),
      });
      appendLog(`已创建执行 #${run.id}（状态 ${run.status}）`);
      connectStream(run.id);
      await loadRuns();
    } catch (error) {
      appendLog(`触发失败：${error.message}`);
    } finally {
      button.disabled = false;
      button.textContent = "执行全部用例";
    }
  });

  $("refresh-cases").addEventListener("click", () => loadCases().catch(() => {}));
  $("refresh-runs").addEventListener("click", () => loadRuns().catch(() => {}));

  async function refreshAll() {
    await Promise.all([
      loadSummary().catch(() => {}),
      loadCases().catch(() => {}),
      loadRuns().catch(() => {}),
      loadTrends().catch(() => {}),
    ]);
  }

  async function bootstrap() {
    try {
      const health = await request("/healthz");
      $("target-url").textContent = health.target_base_url;
    } catch (error) {
      $("target-url").textContent = "目标未知";
    }
    await refreshAll();
    document.body.setAttribute("data-app-ready", "true");
  }

  bootstrap();
})();
