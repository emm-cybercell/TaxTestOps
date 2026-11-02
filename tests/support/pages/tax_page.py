"""Page Object：个税测算页面。

Page Object Model 的核心约定
---------------------------
**定位器只出现在这里，不出现在测试里。** 测试用例只表达业务意图
（``page.calculate(salary=20000)``），页面结构变化时只需改这一个文件。

定位策略：全部使用 ``data-testid`` 而非文案或 CSS 路径。文案会随产品调整，
CSS 类名会随样式重构，只有显式声明的 testid 是稳定契约——这也意味着**前端开发
必须把 testid 当成接口来维护**。
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page, expect

DEFAULT_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class CalculationInput:
    """一次测算的输入。"""

    salary: int = 20_000
    children_num: int = 0
    is_keep_stu: int = 0
    is_rent_house: int = 0
    sibling_num: int = 0
    house_loans: bool = False
    supporting_elderly: bool = False


class TaxCalculatorPage:
    """个税测算页的页面对象。"""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #

    def open(self) -> TaxCalculatorPage:
        """打开首页并等待脚本初始化完成。

        就绪判据是 ``body[data-app-ready="true"]``（由 ``app.js`` 在首屏渲染结束后设置），
        而不是「某个面板可见」——后者与登录态耦合，会让就绪判断随业务状态变化。
        """
        self.page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        self.page.wait_for_selector('body[data-app-ready="true"]')
        return self

    def reload(self) -> TaxCalculatorPage:
        """刷新页面并等待重新就绪。"""
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector('body[data-app-ready="true"]')
        return self

    # ------------------------------------------------------------------ #
    # 定位器
    # ------------------------------------------------------------------ #

    @property
    def login_panel(self):
        return self.page.get_by_test_id("login-panel")

    @property
    def calculator_panel(self):
        return self.page.get_by_test_id("calculator-panel")

    @property
    def history_panel(self):
        return self.page.get_by_test_id("history-panel")

    @property
    def login_error(self):
        return self.page.get_by_test_id("login-error")

    @property
    def calculate_error(self):
        return self.page.get_by_test_id("calculate-error")

    @property
    def result_panel(self):
        return self.page.get_by_test_id("result-panel")

    @property
    def current_user_chip(self):
        return self.page.get_by_test_id("current-user")

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #

    def login(self, username: str, password: str) -> TaxCalculatorPage:
        """通过登录表单登录，等待登录接口返回。"""
        self.page.get_by_test_id("login-username").fill(username)
        self.page.get_by_test_id("login-password").fill(password)
        with self.page.expect_response(self._is_login_response):
            self.page.get_by_test_id("login-submit").click()
        expect(self.calculator_panel).to_be_visible()
        return self

    def attempt_login(self, username: str, password: str) -> TaxCalculatorPage:
        """提交登录但不等待成功——用于断言失败场景。"""
        self.page.get_by_test_id("login-username").fill(username)
        self.page.get_by_test_id("login-password").fill(password)
        self.page.get_by_test_id("login-submit").click()
        return self

    def logout(self) -> TaxCalculatorPage:
        self.page.get_by_test_id("logout-button").click()
        expect(self.login_panel).to_be_visible()
        return self

    # ------------------------------------------------------------------ #
    # 测算
    # ------------------------------------------------------------------ #

    def fill(self, data: CalculationInput) -> TaxCalculatorPage:
        """填写测算表单。"""
        self.page.get_by_test_id("input-salary").fill(str(data.salary))
        self.page.get_by_test_id("input-children").fill(str(data.children_num))
        self.page.get_by_test_id("input-keep-stu").select_option(str(data.is_keep_stu))
        self.page.get_by_test_id("input-rent").select_option(str(data.is_rent_house))
        self.page.get_by_test_id("input-siblings").fill(str(data.sibling_num))
        self._set_checkbox("input-loans", data.house_loans)
        self._set_checkbox("input-elderly", data.supporting_elderly)
        return self

    def _set_checkbox(self, test_id: str, checked: bool) -> None:
        checkbox = self.page.get_by_test_id(test_id)
        if checkbox.is_checked() != checked:
            checkbox.click()

    @property
    def submit_button(self):
        return self.page.get_by_test_id("calculate-submit")

    def wait_until_idle(self) -> TaxCalculatorPage:
        """等待前端把本次交互彻底处理完。

        ``expect_response`` 在**响应头到达**时就返回，而页面后续还要渲染结果、刷新
        历史列表，最后才在 ``finally`` 里恢复按钮文案。若测试在响应到达后立刻读按钮
        状态，就会偶发读到「计算中…」——这类「等待条件比真实完成时机更早」的竞态
        是 UI 自动化最典型的 flaky 来源。

        按钮恢复为可点击且文案回到默认值，才是交互真正结束的信号。
        用 ``expect(...).to_have_text(...)``（自动重试）而不是 ``inner_text()``（立即取值）。
        """
        expect(self.submit_button).to_be_enabled()
        expect(self.submit_button).to_have_text("计算个税")
        return self

    def submit(self) -> TaxCalculatorPage:
        """提交测算并等待**本次**请求完整处理完毕。

        必须等待网络响应而不是「结果面板可见」——面板在首次提交后就一直可见，
        后续提交若用可见性作为等待条件会立刻返回，读到上一次的结果。
        """
        with self.page.expect_response(self._is_calculate_response):
            self.submit_button.click()
        expect(self.result_panel).to_be_visible()
        return self.wait_until_idle()

    def submit_expecting_failure(self) -> TaxCalculatorPage:
        """提交并等待请求返回（无论成功或失败），用于断言错误提示。"""
        with self.page.expect_response(self._is_calculate_response):
            self.submit_button.click()
        return self.wait_until_idle()

    def calculate(self, data: CalculationInput | None = None) -> TaxCalculatorPage:
        """填写并提交（最常用的组合动作）。"""
        return self.fill(data or CalculationInput()).submit()

    # ------------------------------------------------------------------ #
    # 结果读取
    # ------------------------------------------------------------------ #

    @property
    def tax_amount_text(self) -> str:
        return self.page.get_by_test_id("result-tax-amount").inner_text()

    @property
    def taxable_income_text(self) -> str:
        return self.page.get_by_test_id("result-taxable-income").inner_text()

    @property
    def calculator_impl(self) -> str:
        return self.page.get_by_test_id("result-impl").inner_text()

    @property
    def record_id_text(self) -> str:
        return self.page.get_by_test_id("result-record-id").inner_text()

    @property
    def tax_amount(self) -> int:
        """应纳税额（去掉千分位后的整数）。"""
        return int(self.tax_amount_text.replace(",", ""))

    @property
    def taxable_income(self) -> int:
        return int(self.taxable_income_text.replace(",", ""))

    def deductions(self) -> dict[str, int]:
        """返回 ``{扣除项编码: 金额}``。"""
        rows = self.page.get_by_test_id("result-deductions").locator("tr")
        collected: dict[str, int] = {}
        for index in range(rows.count()):
            row = rows.nth(index)
            code = row.get_attribute("data-deduction-code")
            if code is None:
                continue
            cells = row.locator("td")
            collected[code] = int(cells.nth(1).inner_text().replace(",", ""))
        return collected

    # ------------------------------------------------------------------ #
    # 历史记录
    # ------------------------------------------------------------------ #

    def refresh_history(self) -> TaxCalculatorPage:
        """点击刷新并等待记录接口返回。"""
        with self.page.expect_response(self._is_records_response):
            self.page.get_by_test_id("refresh-history").click()
        return self

    def history_rows(self) -> list[list[str]]:
        """返回历史记录表格的全部行（每行为单元格文本列表）。"""
        rows = self.page.get_by_test_id("history-rows").locator("tr")
        return [
            [cell.inner_text() for cell in rows.nth(index).locator("td").all()]
            for index in range(rows.count())
        ]

    @property
    def history_is_empty(self) -> bool:
        return self.page.get_by_test_id("history-empty").is_visible()

    # ------------------------------------------------------------------ #
    # 等待辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_calculate_response(response) -> bool:
        return response.url.endswith("/api/v1/tax/calculate") and response.request.method == "POST"

    @staticmethod
    def _is_records_response(response) -> bool:
        return "/api/v1/tax/records" in response.url and response.request.method == "GET"

    @staticmethod
    def _is_login_response(response) -> bool:
        return response.url.endswith("/api/v1/auth/login") and response.request.method == "POST"

    def wait_for_calculate_error(self) -> str:
        expect(self.calculate_error).to_be_visible()
        return self.calculate_error.inner_text()

    def wait_for_login_error(self) -> str:
        expect(self.login_error).to_be_visible()
        return self.login_error.inner_text()

    def wait_for_history(self) -> TaxCalculatorPage:
        """等待历史记录接口返回。

        读取表格前必须调用——否则可能在 ``loadHistory()`` 完成前就读到旧数据，
        表现为「结果随机少一行」的 flaky 失败。
        """
        self.page.wait_for_load_state("domcontentloaded")
        with self.page.expect_response(self._is_records_response):
            self.page.get_by_test_id("refresh-history").click()
        return self
