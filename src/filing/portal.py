"""门户浏览器自动化与提交编排。

关键防重复逻辑（对应业务要求）：
- 每次提交按 *申请 + 材料版本 + 政策版本* 生成幂等键；同一键只允许一次门户申请。
- 上传失败或等待超时后，**先查询门户实际状态**，再决定继续、复用还是重提，
  绝不盲目重试造成重复申请。
- 浏览器驱动为接口抽象：``FakePortalDriver`` 用于离线演示/测试，
  ``PlaywrightDriver`` 为真实填报的落地骨架（环境具备 playwright 时启用）。
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    AppState,
    Application,
    Submission,
    SubmissionState,
)
from .storage import Storage, now_iso


class PortalError(RuntimeError):
    pass


class UploadTimeout(PortalError):
    """上传后门户未在规定时间内给出确定结果。"""


@dataclass
class PortalStatus:
    """从门户侧查询到的申请真实状态。"""

    exists: bool
    portal_ref: Optional[str] = None
    state: str = "unknown"
    files: list[str] = field(default_factory=list)


class PortalDriver(abc.ABC):
    """门户填报驱动接口。"""

    @abc.abstractmethod
    def open_form(self, portal_url: str) -> None: ...

    @abc.abstractmethod
    def fill_fields(self, values: dict[str, str]) -> None: ...

    @abc.abstractmethod
    def upload_files(self, files: list[str]) -> None: ...

    @abc.abstractmethod
    def submit(self) -> str:
        """返回门户受理编号。"""

    @abc.abstractmethod
    def query_status(self, idempotency_key: str) -> PortalStatus: ...


class FakePortalDriver(PortalDriver):
    """内存模拟门户，支持注入失败/超时/部分受理等场景。

    - ``fail_next``：下一次提交抛上传失败。
    - ``timeout_next``：下一次提交抛超时，但可通过 ``materialize_on_timeout``
      控制门户侧实际是否已落单（模拟“超时其实已受理”）。
    - ``partial_files``：设置后门户只接受给定附件，其余退回。
    """

    def __init__(
        self,
        fail_next: int = 0,
        timeout_next: int = 0,
        materialize_on_timeout: bool = True,
        partial_files: Optional[set[str]] = None,
    ) -> None:
        self._fail_left = fail_next
        self._timeout_left = timeout_next
        self._materialize = materialize_on_timeout
        self._partial = partial_files
        self._counter = 900
        self.portal_side: dict[str, PortalStatus] = {}
        self.calls: list[str] = []

    def open_form(self, portal_url: str) -> None:
        self.calls.append(f"open:{portal_url}")

    def fill_fields(self, values: dict[str, str]) -> None:
        self.calls.append("fill:" + ",".join(sorted(values)))
        self._last_values = values

    def upload_files(self, files: list[str]) -> None:
        self.calls.append("upload:" + "|".join(files))
        self._last_files = list(files)

    def submit(self) -> str:
        key = self._last_values["_idempotency_key"]
        self.calls.append(f"submit:{key}")
        if self._fail_left > 0:
            self._fail_left -= 1
            raise PortalError("门户返回 502，上传失败")
        if self._timeout_left > 0:
            self._timeout_left -= 1
            if self._materialize:
                # 真实场景常见：前端超时，后端实际已建单。
                ref = self._new_ref()
                self.portal_side[key] = PortalStatus(
                    exists=True, portal_ref=ref, state="submitted",
                    files=list(self._last_files),
                )
            raise UploadTimeout("等待门户回执超时")
        ref = self._new_ref()
        files = self._last_files
        if self._partial is not None:
            files = [f for f in files if f in self._partial]
        self.portal_side[key] = PortalStatus(
            exists=True, portal_ref=ref, state="submitted", files=files,
        )
        return ref

    def query_status(self, idempotency_key: str) -> PortalStatus:
        self.calls.append(f"query:{idempotency_key}")
        return self.portal_side.get(
            idempotency_key, PortalStatus(exists=False)
        )

    def _new_ref(self) -> str:
        self._counter += 1
        return f"EXT-{self._counter}"


def make_idempotency_key(
    app_id: str, material_version: int, rule: str, revision: int
) -> str:
    raw = f"{app_id}|v{material_version}|{rule}v{revision}"
    return "IDM-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class SubmissionService:
    """生成提交计划并驱动门户，全程记录提交历史。"""

    def __init__(
        self, storage: Storage, driver: PortalDriver,
        portal_url: str = "https://portal.example.gov/filing",
    ) -> None:
        self.storage = storage
        self.driver = driver
        self.portal_url = portal_url

    def plan_submission(self, app: Application) -> Submission:
        """按当前材料版本生成提交记录（PLANNED），不立即填报。"""
        open_items = self.storage.list_pending_items(app.app_id, open_only=True)
        if open_items:
            raise PortalError(
                f"申请 {app.app_id} 存在 {len(open_items)} 个待确认项，提交被阻断"
            )
        if not app.rule or app.policy_revision is None:
            raise PortalError(f"申请 {app.app_id} 尚未确定适用条款版本")

        snapshot = self.storage.latest_material_snapshot(app.app_id)
        if not snapshot:
            raise PortalError(f"申请 {app.app_id} 没有可上传的材料")
        material_version = max(m.version for m in snapshot)
        files = sorted(m.filename for m in snapshot)
        key = make_idempotency_key(
            app.app_id, material_version, app.rule, app.policy_revision
        )
        existing = self.storage.get_submission_by_key(key)
        if existing is not None:
            # 同版本重复发起（含已提交/已受理状态）：直接回放既有记录，
            # 由调用方走状态核对流程，绝不产生第二张门户申请。
            return existing
        if app.state not in (AppState.READY, AppState.CORRECTION, AppState.PARTIAL):
            raise PortalError(
                f"申请 {app.app_id} 状态为 {app.state.value}，不允许新的提交"
            )
        sub = self.storage.add_submission(Submission(
            submission_id=None, app_id=app.app_id, idempotency_key=key,
            material_version=material_version, policy_rule=app.rule,
            policy_revision=app.policy_revision, files=files,
            state=SubmissionState.PLANNED, portal_ref=None,
            created_at=now_iso(), updated_at=now_iso(),
        ))
        return sub

    def submit(self, app_id: str) -> Submission:
        """执行一次填报；失败/超时自动核对门户状态后落定结论。"""
        app = self.storage.get_application(app_id)
        sub = self.plan_submission(app)

        if sub.state in (SubmissionState.SUCCEEDED, SubmissionState.DUPLICATE):
            return sub  # 已成功或已确认重复，不重复填报。

        # 提交前先核对门户，防止本地记录缺失但门户侧已建单。
        remote = self.driver.query_status(sub.idempotency_key)
        if remote.exists:
            sub.state = SubmissionState.DUPLICATE
            self.storage.update_submission(
                sub.submission_id, SubmissionState.DUPLICATE,
                portal_ref=remote.portal_ref,
                detail=f"提交前核对发现门户已存在申请 {remote.portal_ref}，停止重复提交",
            )
            return sub

        self.storage.update_submission(sub.submission_id, SubmissionState.UPLOADING)
        try:
            self.driver.open_form(self.portal_url)
            self.driver.fill_fields({
                "_idempotency_key": sub.idempotency_key,
                "app": app.app_id,
                "company": app.company,
                "category": app.category,
                "location": app.location,
                "investment": str(app.investment),
                "rule": sub.policy_rule,
                "policy_revision": str(sub.policy_revision),
            })
            self.driver.upload_files(sub.files)
            portal_ref = self.driver.submit()
        except UploadTimeout as exc:
            return self._reconcile_after_uncertain(sub, str(exc))
        except PortalError as exc:
            return self._reconcile_after_uncertain(sub, str(exc))

        # 保留此前失败/核对留痕，在其基础上追加成功结论。
        previous = self.storage.get_submission(sub.submission_id)
        detail = "门户已接收"
        if previous and previous.detail:
            detail = previous.detail + " → 重试后门户已接收"
        self.storage.update_submission(
            sub.submission_id, SubmissionState.SUCCEEDED,
            portal_ref=portal_ref, detail=detail,
        )
        self.storage.update_application_state(app_id, AppState.SUBMITTED)
        return self.storage.get_submission(sub.submission_id)

    def _reconcile_after_uncertain(
        self, sub: Submission, error_detail: str
    ) -> Submission:
        """失败/超时后的唯一安全路径：先查门户真实状态再决定。"""
        remote = self.driver.query_status(sub.idempotency_key)
        if remote.exists:
            # 门户实际已建单：标记为成功并挂接门户编号，禁止重提。
            self.storage.update_submission(
                sub.submission_id, SubmissionState.SUCCEEDED,
                portal_ref=remote.portal_ref,
                detail=f"本地报错（{error_detail}），核对门户确认已建单，复用该申请",
            )
            self.storage.update_application_state(sub.app_id, AppState.SUBMITTED)
        else:
            # 门户侧确认无单，才允许后续重试；本次按失败留痕。
            self.storage.update_submission(
                sub.submission_id, SubmissionState.FAILED,
                detail=f"核对门户确认无此单：{error_detail}",
            )
        return self.storage.get_submission(sub.submission_id)

    def retry_failed(self, app_id: str) -> Submission:
        """仅当门户侧确认无单时重试；同幂等键沿用同一条提交记录。"""
        app = self.storage.get_application(app_id)
        sub = self.plan_submission(app)
        if sub.state != SubmissionState.FAILED:
            raise PortalError(
                f"提交 {sub.submission_id} 状态为 {sub.state.value}，不可重试"
            )
        remote = self.driver.query_status(sub.idempotency_key)
        if remote.exists:
            self.storage.update_submission(
                sub.submission_id, SubmissionState.DUPLICATE,
                portal_ref=remote.portal_ref,
                detail="重试前核对发现门户已建单，转为 DUPLICATE",
            )
            return self.storage.get_submission(sub.submission_id)
        return self.submit(app_id)
