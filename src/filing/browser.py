"""真实门户的 Playwright 驱动落地骨架。

本模块只在安装了 playwright 的环境中可用；离线测试与演示统一使用
``filing.portal.FakePortalDriver``。选择器通过构造参数注入，
避免把具体门户的页面结构写死在业务代码里。

安全说明：
- 不在日志或数据库中保存门户口令；凭据从环境变量读取。
- 每一步操作前等待元素出现，超时向上抛出，由 SubmissionService
  走"先核对门户状态再决定"的统一对账流程，禁止盲目重试。
"""
from __future__ import annotations

import os
from typing import Optional

from .portal import PortalDriver, PortalStatus, UploadTimeout


class PlaywrightDriver(PortalDriver):
    """基于 Playwright 同步 API 的门户填报驱动。

    ``selectors`` 示例：
        {
            "field": {"app": "#appId", "company": "#companyName", ...},
            "file_input": 'input[type=file]',
            "submit_button": "#submitBtn",
            "ref_selector": ".portal-ref",
        }
    """

    def __init__(
        self,
        selectors: dict,
        headless: bool = True,
        timeout_ms: int = 30000,
        username_env: str = "PORTAL_USER",
        password_env: str = "PORTAL_PASSWORD",
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise RuntimeError(
                "未安装 playwright，请先 `pip install playwright` 并执行 "
                "`playwright install chromium`，或改用 FakePortalDriver。"
            ) from exc
        self.selectors = selectors
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.username_env = username_env
        self.password_env = password_env
        self._pw = None
        self._browser = None
        self._page = None
        self._last_key: Optional[str] = None

    def __enter__(self) -> "PlaywrightDriver":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def open_form(self, portal_url: str) -> None:
        self._page.goto(portal_url, wait_until="networkidle")
        self._login_if_required()

    def _login_if_required(self) -> None:
        user = os.environ.get(self.username_env)
        password = os.environ.get(self.password_env)
        if not user or not password:
            return
        user_sel = self.selectors.get("login_user")
        pwd_sel = self.selectors.get("login_password")
        login_btn = self.selectors.get("login_button")
        if user_sel and self._page.locator(user_sel).count():
            self._page.fill(user_sel, user)
            self._page.fill(pwd_sel, password)
            self._page.click(login_btn)
            self._page.wait_for_load_state("networkidle")

    def fill_fields(self, values: dict[str, str]) -> None:
        self._last_key = values["_idempotency_key"]
        field_map = self.selectors["field"]
        for key, value in values.items():
            if key.startswith("_"):
                continue
            selector = field_map.get(key)
            if selector:
                self._page.fill(selector, str(value))

    def upload_files(self, files: list[str]) -> None:
        self._page.set_input_files(self.selectors["file_input"], files)

    def submit(self) -> str:
        self._page.click(self.selectors["submit_button"])
        try:
            self._page.wait_for_selector(
                self.selectors["ref_selector"], timeout=self.timeout_ms
            )
        except Exception as exc:  # pragma: no cover - 依赖真实页面
            raise UploadTimeout(f"提交后未等到门户受理编号：{exc}") from exc
        return self._page.inner_text(self.selectors["ref_selector"]).strip()

    def query_status(self, idempotency_key: str) -> PortalStatus:
        """打开门户查询页，按业务编号/幂等键核对申请是否已存在。"""
        query_url = self.selectors["query_url"].format(key=idempotency_key)
        self._page.goto(query_url, wait_until="networkidle")
        exists = self._page.locator(self.selectors["query_result"]).count() > 0
        if not exists:
            return PortalStatus(exists=False)
        return PortalStatus(
            exists=True,
            portal_ref=self._page.inner_text(self.selectors["ref_selector"]).strip(),
            state=self._page.inner_text(self.selectors["query_state"]).strip(),
            files=[
                node.inner_text().strip()
                for node in self._page.query_selector_all(
                    self.selectors["query_files"]
                )
            ],
        )
