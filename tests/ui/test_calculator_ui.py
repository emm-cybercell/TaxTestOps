"""UI 自动化层 · 个税测算页。

设计原则
--------
UI 用例**不重复验证业务算法**——税额算得对不对由单元层与接口层负责（那两层快几百倍）。
UI 层只验证「页面把接口返回的数据正确呈现出来，并且交互状态符合预期」：

- 元素可见性、状态切换（未登录 → 登录 → 登出）；
- 表单输入到结果的正确传递；
- 格式化（千分位）、空态、错误提示；
- 关键交互不会导致页面崩溃。
"""

from __future__ import annotations

import pytest

from support.pages.tax_page import CalculationInput, TaxCalculatorPage

pytestmark = [pytest.mark.ui, pytest.mark.slow]


class TestLoginFlow:
    """登录相关的界面状态。"""

    def test_shows_login_panel_initially(self, tax_page: TaxCalculatorPage) -> None:
        """未登录时只显示登录面板，测算与历史面板隐藏。"""
        assert tax_page.login_panel.is_visible()
        assert not tax_page.calculator_panel.is_visible()
        assert not tax_page.history_panel.is_visible()

    def test_successful_login_reveals_workspace(
        self, tax_page: TaxCalculatorPage, registered_ui_user: tuple[str, str]
    ) -> None:
        username, password = registered_ui_user

        tax_page.login(username, password)

        assert tax_page.calculator_panel.is_visible()
        assert tax_page.history_panel.is_visible()
        assert not tax_page.login_panel.is_visible()
        assert username in tax_page.current_user_chip.inner_text()

    def test_wrong_password_shows_error(
        self, tax_page: TaxCalculatorPage, registered_ui_user: tuple[str, str]
    ) -> None:
        username, _ = registered_ui_user

        tax_page.attempt_login(username, "definitely-wrong-password")

        message = tax_page.wait_for_login_error()
        assert "用户名或密码错误" in message
        assert tax_page.login_panel.is_visible()

    def test_empty_form_is_rejected_client_side(self, tax_page: TaxCalculatorPage) -> None:
        """前端应做基础校验，避免无意义的空请求。"""
        tax_page.attempt_login("", "")

        assert "请输入用户名和密码" in tax_page.wait_for_login_error()

    def test_logout_returns_to_login(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.logout()

        assert logged_in_page.login_panel.is_visible()
        assert not logged_in_page.calculator_panel.is_visible()

    def test_session_survives_reload(self, logged_in_page: TaxCalculatorPage) -> None:
        """刷新后仍保持登录（令牌存于 localStorage）。"""
        logged_in_page.reload()

        assert logged_in_page.calculator_panel.is_visible()


class TestCalculationForm:
    """表单到结果的端到端呈现。"""

    def test_renders_tax_amount(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.calculate(CalculationInput(salary=20_000))

        assert logged_in_page.tax_amount > 0

    def test_renders_amount_with_thousands_separator(
        self, logged_in_page: TaxCalculatorPage
    ) -> None:
        """金额必须做千分位格式化——这是可直接断言的展示层需求。"""
        logged_in_page.calculate(CalculationInput(salary=105_000))

        assert "," in logged_in_page.tax_amount_text

    def test_reports_calculator_impl(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.calculate(CalculationInput(salary=20_000))

        assert logged_in_page.calculator_impl in {"legacy", "standard"}

    def test_shows_record_id(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.calculate(CalculationInput(salary=20_000))

        assert logged_in_page.record_id_text.isdigit()

    def test_zero_tax_for_low_salary(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.calculate(CalculationInput(salary=4_000))

        assert logged_in_page.tax_amount == 0

    def test_empty_deductions_shows_placeholder(self, logged_in_page: TaxCalculatorPage) -> None:
        """无扣除时显示占位文案，而不是空白表格。"""
        logged_in_page.calculate(CalculationInput(salary=20_000))

        assert logged_in_page.deductions() == {}
        assert (
            "无专项附加扣除" in logged_in_page.page.get_by_test_id("result-deductions").inner_text()
        )

    def test_renders_each_deduction_item(self, logged_in_page: TaxCalculatorPage) -> None:
        """每项扣除都应单独成行并带金额。"""
        logged_in_page.calculate(
            CalculationInput(
                salary=40_000,
                children_num=2,
                is_keep_stu=1,
                is_rent_house=1,
                sibling_num=1,
                supporting_elderly=True,
            )
        )

        deductions = logged_in_page.deductions()

        assert deductions["child_education"] == 2_000
        assert deductions["continuing_education"] == 400
        assert deductions["housing_rent"] == 1_500
        assert deductions["elderly_care"] == 1_000

    def test_deduction_rows_have_accessible_labels(self, logged_in_page: TaxCalculatorPage) -> None:
        """明细行必须有中文标签，不能只显示编码。"""
        logged_in_page.calculate(CalculationInput(salary=40_000, children_num=1))

        text = logged_in_page.page.get_by_test_id("result-deductions").inner_text()

        assert "子女教育" in text

    def test_recalculation_replaces_previous_result(
        self, logged_in_page: TaxCalculatorPage
    ) -> None:
        """二次计算必须**替换**结果，不能追加行——这是最常见的渲染缺陷。"""
        logged_in_page.calculate(CalculationInput(salary=20_000, children_num=2))
        first_rows = len(logged_in_page.deductions())

        logged_in_page.calculate(CalculationInput(salary=20_000, children_num=1))
        second_rows = len(logged_in_page.deductions())

        assert first_rows == second_rows == 1

    def test_submit_button_recovers_after_success(self, logged_in_page: TaxCalculatorPage) -> None:
        """提交后按钮必须恢复可点击，否则用户只能刷新页面。

        ``calculate()`` 内部已等待按钮回到空闲状态（见 POM 的 ``wait_until_idle``），
        这里再显式调用一次，把「按钮会恢复」固定为一条独立契约——若前端改成
        提交后禁用按钮且不恢复，这条用例会单独失败并明确指出原因。
        """
        logged_in_page.calculate(CalculationInput(salary=20_000))

        logged_in_page.wait_until_idle()


class TestValidationFeedback:
    """非法输入的界面反馈。"""

    def test_invalid_salary_shows_error(self, logged_in_page: TaxCalculatorPage) -> None:
        """超出范围的工资应给出可读的错误提示，而不是静默失败。"""
        logged_in_page.page.get_by_test_id("input-salary").fill("99999999")
        logged_in_page.submit_expecting_failure()

        message = logged_in_page.wait_for_calculate_error()

        assert message, "非法输入必须给出错误提示"
        assert not logged_in_page.result_panel.is_visible()

    def test_error_clears_on_next_successful_calculation(
        self, logged_in_page: TaxCalculatorPage
    ) -> None:
        """成功计算后应清除上一次的错误提示，避免用户误以为仍然出错。"""
        logged_in_page.page.get_by_test_id("input-salary").fill("99999999")
        logged_in_page.submit_expecting_failure()
        logged_in_page.wait_for_calculate_error()

        logged_in_page.calculate(CalculationInput(salary=20_000))

        assert not logged_in_page.calculate_error.is_visible()


class TestHistoryPanel:
    """历史记录面板。"""

    def test_empty_state_for_new_user(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.wait_for_history()

        assert logged_in_page.history_is_empty

    def test_shows_calculated_records(self, logged_in_page: TaxCalculatorPage) -> None:
        for salary in (10_000, 20_000, 30_000):
            logged_in_page.calculate(CalculationInput(salary=salary))

        logged_in_page.wait_for_history()
        rows = logged_in_page.history_rows()

        assert len(rows) == 3
        assert {row[1] for row in rows} == {"10,000", "20,000", "30,000"}

    def test_history_row_contains_amount_columns(self, logged_in_page: TaxCalculatorPage) -> None:
        logged_in_page.calculate(CalculationInput(salary=20_000))
        logged_in_page.wait_for_history()

        row = logged_in_page.history_rows()[0]

        assert len(row) == 5, f"历史行应有 5 列，实际 {len(row)}：{row}"
        assert row[4] in {"legacy", "standard"}

    def test_refresh_reflects_new_records(self, logged_in_page: TaxCalculatorPage) -> None:
        """手动刷新应拉到最新数据——验证刷新按钮真的重新请求了。"""
        logged_in_page.calculate(CalculationInput(salary=20_000))
        logged_in_page.wait_for_history()
        before = len(logged_in_page.history_rows())

        logged_in_page.calculate(CalculationInput(salary=30_000))
        logged_in_page.wait_for_history()

        assert len(logged_in_page.history_rows()) == before + 1


class TestStaticAssets:
    """静态资源与页面元信息。"""

    def test_page_has_title(self, tax_page: TaxCalculatorPage) -> None:
        assert "个税" in tax_page.page.title()

    def test_stylesheet_is_loaded(self, tax_page: TaxCalculatorPage) -> None:
        """样式表必须真的加载成功——否则页面能用但完全错乱。"""
        background = tax_page.page.evaluate("() => getComputedStyle(document.body).backgroundColor")

        assert background not in {"rgba(0, 0, 0, 0)", "transparent"}

    def test_no_console_errors_on_load(self, tax_page: TaxCalculatorPage) -> None:
        """页面加载不应产生控制台错误。

        前端报错往往不影响主流程，但会在用户侧留下难以复现的诡异状态，
        因此在 UI 层直接守住。
        """
        errors: list[str] = []
        tax_page.page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )

        tax_page.reload()

        assert not errors, f"控制台出现错误：{errors}"

    def test_api_docs_link_is_present(self, tax_page: TaxCalculatorPage) -> None:
        assert tax_page.page.get_by_test_id("api-docs-link").is_visible()
