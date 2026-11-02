"""UI 自动化层夹具（Playwright）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from support.factories import random_password, random_username
from support.pages.tax_page import TaxCalculatorPage
from support.servers import LiveServer

ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "ui"

# 未安装 playwright 时整个 UI 层自动跳过，而不是让收集阶段直接报错——
# 这样「只装了核心依赖」的环境仍能跑完其余各层。
pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过 UI 层")

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright  # noqa: E402


@pytest.fixture(scope="session")
def playwright_browser() -> Iterator[Browser]:
    """整个会话共用一个浏览器进程。

    每次用例都启动浏览器会让 UI 层慢到没人愿意跑；共用浏览器、每个用例独立
    ``BrowserContext``（相当于无痕窗口）既快又保证 cookie/localStorage 隔离。
    """
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - 浏览器未安装
            pytest.skip(f"Chromium 未安装或无法启动：{exc}")
        try:
            yield browser
        finally:
            with suppress(Exception):
                browser.close()


@pytest.fixture
def browser_context(playwright_browser: Browser) -> Iterator[BrowserContext]:
    context = playwright_browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        yield context
    finally:
        with suppress(Exception):
            context.close()


@pytest.fixture
def page(browser_context: BrowserContext, request) -> Iterator[Page]:
    """页面对象；用例失败时自动截图存档，便于定位。"""
    ui_page = browser_context.new_page()
    try:
        yield ui_page
    finally:
        if getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            target = ARTIFACTS_DIR / f"{request.node.name}.png"
            # 截图失败不应掩盖原始的用例失败，因此静默忽略
            with suppress(Exception):
                ui_page.screenshot(path=str(target), full_page=True)
        with suppress(Exception):
            ui_page.close()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):  # noqa: ANN001, ANN201
    """把各阶段结果挂到测试项上，供 ``page`` 夹具判断是否截图。"""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# --------------------------------------------------------------------------- #
# 被测页面
# --------------------------------------------------------------------------- #


@pytest.fixture
def tax_page(page: Page, live_server: LiveServer) -> TaxCalculatorPage:
    """已打开首页的页面对象（未登录）。"""
    return TaxCalculatorPage(page, live_server.base_url).open()


@pytest.fixture
def ui_credentials() -> tuple[str, str]:
    return random_username("ui"), random_password()


@pytest.fixture
def registered_ui_user(live_server: LiveServer, ui_credentials: tuple[str, str]) -> tuple[str, str]:
    """在真实服务上注册一个 UI 测试用户。

    通过 HTTP 接口注册而非直接写库，确保前置条件走的是真实路径；
    注册用的是 ``app_settings`` 中的低轮数 bcrypt，因此不会拖慢 UI 用例。
    """
    username, password = ui_credentials
    with httpx.Client(base_url=live_server.base_url, timeout=30.0) as client:
        response = client.post(
            "/api/v1/auth/register", json={"username": username, "password": password}
        )
        assert response.status_code == 201, f"注册 UI 测试用户失败：{response.text}"

    return username, password


@pytest.fixture
def logged_in_page(
    tax_page: TaxCalculatorPage, registered_ui_user: tuple[str, str]
) -> TaxCalculatorPage:
    """已登录的页面对象。"""
    username, password = registered_ui_user
    return tax_page.login(username, password)
