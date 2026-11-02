/**
 * 个税测算前端。
 *
 * 设计约束（为了可测试性）：
 * - 所有可交互元素都带稳定的 `data-testid`，UI 用例只依赖 testid，不依赖文案与样式；
 * - 显式的 loading / error / empty 状态，使每个分支都可被断言；
 * - 纯原生 DOM 操作，无框架、无构建步骤，Playwright 可直接加载。
 */
(function () {
  "use strict";

  const TOKEN_KEY = "taxtestops.token";

  const $ = (testId) => document.querySelector(`[data-testid="${testId}"]`);
  const api = {
    token: () => localStorage.getItem(TOKEN_KEY),
    setToken: (token) => localStorage.setItem(TOKEN_KEY, token),
    clearToken: () => localStorage.removeItem(TOKEN_KEY),
  };

  async function request(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    const token = api.token();
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(path, Object.assign({}, options, { headers }));
    const text = await response.text();
    const body = text ? JSON.parse(text) : null;

    if (!response.ok) {
      const detail = body && body.detail;
      const message = Array.isArray(detail)
        ? detail.map((item) => `${item.loc ? item.loc.join(".") : ""} ${item.msg}`).trim().join("；")
        : detail || `请求失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return body;
  }

  function show(element, visible) {
    if (element) element.hidden = !visible;
  }

  function setError(testId, message) {
    const node = $(testId);
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function formatAmount(value) {
    return Number(value).toLocaleString("zh-CN");
  }

  // ------------------------------------------------------------------ //
  // 登录
  // ------------------------------------------------------------------ //

  function renderSession(username) {
    const loggedIn = Boolean(username);
    show($("login-panel"), !loggedIn);
    show($("calculator-panel"), loggedIn);
    show($("history-panel"), loggedIn);
    show($("logout-button"), loggedIn);
    show($("current-user"), loggedIn);

    const chip = $("current-user");
    if (chip) chip.textContent = loggedIn ? `已登录：${username}` : "";
  }

  async function bootstrap() {
    try {
      if (!api.token()) {
        renderSession(null);
        return;
      }
      const me = await request("/api/v1/auth/me");
      renderSession(me.username);
      await loadHistory();
    } catch (error) {
      api.clearToken();
      renderSession(null);
    } finally {
      // 显式暴露「初始化完成」信号。
      // UI 自动化若靠等待某个业务元素来判定就绪，会与业务状态耦合（例如登录态不同，
      // 可见面板就不同）。这个属性把「脚本已跑完首屏渲染」变成一条稳定的就绪契约。
      document.body.setAttribute("data-app-ready", "true");
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("login-error", "");

    const username = $("login-username").value.trim();
    const password = $("login-password").value;
    const button = $("login-submit");

    if (!username || !password) {
      setError("login-error", "请输入用户名和密码");
      return;
    }

    button.disabled = true;
    button.textContent = "登录中…";
    try {
      const result = await request("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      api.setToken(result.access_token);
      renderSession(username);
      await loadHistory();
    } catch (error) {
      setError("login-error", error.message);
    } finally {
      button.disabled = false;
      button.textContent = "登录";
    }
  });

  $("logout-button").addEventListener("click", () => {
    api.clearToken();
    renderSession(null);
    setError("calculate-error", "");
    show($("result-panel"), false);
  });

  // ------------------------------------------------------------------ //
  // 测算
  // ------------------------------------------------------------------ //

  $("calculator-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("calculate-error", "");

    const button = $("calculate-submit");
    const payload = {
      salary: Number($("input-salary").value),
      person: {
        children_num: Number($("input-children").value || 0),
        is_keep_stu: Number($("input-keep-stu").value),
        house_loans: $("input-loans").checked,
        is_rent_house: Number($("input-rent").value),
        supporting_elderly: $("input-elderly").checked,
        sibling_num: Number($("input-siblings").value || 0),
      },
    };

    button.disabled = true;
    button.textContent = "计算中…";
    try {
      const result = await request("/api/v1/tax/calculate", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderResult(result);
      await loadHistory();
    } catch (error) {
      setError("calculate-error", error.message);
      show($("result-panel"), false);
    } finally {
      button.disabled = false;
      button.textContent = "计算个税";
    }
  });

  function renderResult(result) {
    $("result-tax-amount").textContent = formatAmount(result.tax_amount);
    $("result-taxable-income").textContent = formatAmount(result.taxable_income);
    $("result-impl").textContent = result.calculator_impl;
    $("result-record-id").textContent = result.record_id === null ? "未落库" : result.record_id;

    const body = $("result-deductions");
    body.innerHTML = "";

    if (!result.deductions.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "无专项附加扣除";
      cell.style.color = "#6b7690";
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      result.deductions.forEach((item) => {
        const row = document.createElement("tr");
        row.setAttribute("data-deduction-code", item.code);

        const label = document.createElement("td");
        label.textContent = item.label;

        const amount = document.createElement("td");
        amount.className = "numeric";
        amount.textContent = formatAmount(item.amount);

        row.append(label, amount);
        body.appendChild(row);
      });
    }

    show($("result-panel"), true);
  }

  // ------------------------------------------------------------------ //
  // 历史记录
  // ------------------------------------------------------------------ //

  $("refresh-history").addEventListener("click", loadHistory);

  async function loadHistory() {
    if (!api.token()) return;

    const rows = $("history-rows");
    try {
      const page = await request("/api/v1/tax/records?limit=20");
      rows.innerHTML = "";

      show($("history-empty"), page.total === 0);
      show($("history-table"), page.total > 0);

      page.items.forEach((record) => {
        const row = document.createElement("tr");
        row.setAttribute("data-record-id", String(record.id));
        [
          record.id,
          formatAmount(record.salary),
          formatAmount(record.taxable_income),
          formatAmount(record.tax_amount),
          record.calculator_impl,
        ].forEach((value, index) => {
          const cell = document.createElement("td");
          if (index > 0 && index < 4) cell.className = "numeric";
          cell.textContent = value;
          row.appendChild(cell);
        });
        rows.appendChild(row);
      });
    } catch (error) {
      setError("calculate-error", `加载历史记录失败：${error.message}`);
    }
  }

  bootstrap();
})();
