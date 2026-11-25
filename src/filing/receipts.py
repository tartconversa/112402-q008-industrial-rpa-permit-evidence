"""门户回执处理与补正闭环。

流程：
1. 登记回执（受理/部分受理/补正/退回）。
2. 把回执中的每个问题映射到 *政策条件*（条款版本 + 条件编号）与
   *企业材料*（材料类型/文件名），生成可办理事项 ActionItem。
3. 企业按事项补件（材料产生新版本），审核员复核通过后事项办结。
4. 仅针对受影响附件重新提交；受理时锁定政策与材料版本。
"""
from __future__ import annotations

import re
from typing import Optional

from .models import (
    ActionItem,
    ActionStatus,
    AppState,
    Application,
    Material,
    Receipt,
    ReceiptKind,
    Role,
    Submission,
    SubmissionState,
)
from .portal import SubmissionService, make_idempotency_key
from .rules import REQUIRED_MATERIALS
from .storage import Storage, now_iso

# 回执原文中的问题关键词 → 材料类型，用于从非结构化文本提取补正要求。
ISSUE_PATTERNS: dict[str, re.Pattern] = {
    "budget_proof": re.compile(r"(投资|预算|资金|budget)"),
    "scope_statement": re.compile(r"(范围|改造内容|scope)"),
    "site_photo": re.compile(r"(现场|地点|照片|site)"),
}


class ReceiptError(RuntimeError):
    pass


class ReceiptService:
    def __init__(self, storage: Storage, submissions: SubmissionService) -> None:
        self.storage = storage
        self.submissions = submissions

    # ----------------------------------------------------------- 回执登记

    def ingest(
        self,
        app_id: str,
        portal_ref: str,
        kind: ReceiptKind,
        raw_text: str,
        accepted_files: Optional[list[str]] = None,
        rejected_files: Optional[list[str]] = None,
        received_at: Optional[str] = None,
        issues: Optional[list[dict]] = None,
    ) -> Receipt:
        """登记一条门户回执并派生申请状态与可办理事项。"""
        app = self.storage.get_application(app_id)
        if app is None:
            raise KeyError(app_id)
        submission = self._find_submission(app_id, portal_ref)
        accepted_files = accepted_files or []
        rejected_files = rejected_files or []
        # 部分受理且回执未显式列退回件时：提交了但未出现在接受清单中的附件，
        # 视为未被接受，自动补入退回清单，避免漏生可办理事项。
        if kind == ReceiptKind.PARTIAL and not rejected_files:
            rejected_files = [
                f for f in submission.files if f not in accepted_files
            ]
        receipt = self.storage.add_receipt(Receipt(
            receipt_id=None, app_id=app_id,
            submission_id=submission.submission_id, portal_ref=portal_ref,
            kind=kind, received_at=received_at or now_iso(), raw_text=raw_text,
            accepted_files=accepted_files, rejected_files=rejected_files,
        ))

        if kind == ReceiptKind.ACCEPTED:
            self._lock_accepted(app, submission, receipt)
        elif kind in (ReceiptKind.PARTIAL, ReceiptKind.CORRECTION,
                      ReceiptKind.REJECTED):
            self._open_corrections(
                app, submission, receipt, accepted_files,
                rejected_files, issues or [],
            )
        self.storage.commit()
        return receipt

    def _find_submission(self, app_id: str, portal_ref: str) -> Submission:
        for sub in self.storage.list_submissions(app_id):
            if sub.portal_ref == portal_ref:
                return sub
        raise ReceiptError(
            f"门户编号 {portal_ref} 在申请 {app_id} 的提交历史中不存在"
        )

    def _lock_accepted(
        self, app: Application, submission: Submission, receipt: Receipt
    ) -> None:
        """受理：固化提交时的政策版本与材料版本，此后不受指南更新影响。"""
        self.storage.update_application_state(
            app.app_id, AppState.ACCEPTED,
            locked_rule=submission.policy_rule,
            locked_policy_revision=submission.policy_revision,
            locked_material_version=submission.material_version,
            accepted_at=receipt.received_at,
        )

    def _open_corrections(
        self, app, submission: Submission, receipt: Receipt,
        accepted_files: list[str], rejected_files: list[str],
        issues: list[dict],
    ) -> None:
        condition_base = f"{submission.policy_rule}v{submission.policy_revision}"
        filename_to_type = {
            m.filename: m.material_type
            for m in self.storage.list_materials(app.app_id)
        }
        required = REQUIRED_MATERIALS.get(submission.policy_rule, [])
        # 同一回执下，每种材料类型只生成一条事项，避免附件清单与文本重复派生。
        seen_types: set[str] = set()

        def add_action(material_type: str, reason: str) -> None:
            if material_type in seen_types:
                return
            seen_types.add(material_type)
            if material_type not in required:
                # 回执要求了指南之外的材料，仍建事项但标注待政策专员确认。
                condition_ref = f"{condition_base}#unknown:{material_type}"
            else:
                condition_ref = f"{condition_base}#{material_type}"
            self.storage.add_action_item(ActionItem(
                action_id=None, app_id=app.app_id, receipt_id=receipt.receipt_id,
                condition_ref=condition_ref, material_type=material_type,
                reason=reason, status=ActionStatus.OPEN, created_at=now_iso(),
            ))

        # 1) 被退回/未接受的附件直接映射到对应条件。
        for filename in rejected_files:
            material_type = filename_to_type.get(filename) \
                or self._guess_type(filename)
            add_action(
                material_type,
                f"门户未接受附件 {filename}，需补交后重新上传该附件",
            )

        # 2) 回执文本中声明的补正问题（结构化传入或从文本提取）。
        parsed = issues or self.parse_issues(receipt.raw_text)
        for issue in parsed:
            material_type = issue.get("material_type") \
                or self._guess_type(issue.get("text", ""))
            add_action(
                material_type,
                issue.get("reason") or issue.get("text", "按门户回执补正"),
            )

        new_state = (
            AppState.PARTIAL if receipt.kind == ReceiptKind.PARTIAL
            else AppState.CORRECTION
        )
        self.storage.update_application_state(app.app_id, new_state)

    @staticmethod
    def parse_issues(text: str) -> list[dict]:
        """从回执中文文本提取补正问题行。

        识别以“补正/缺少/退回/请补充”开头的行，并按关键词猜测材料类型。
        """
        findings: list[dict] = []
        for line in re.split(r"[\n；;]", text):
            line = line.strip()
            if not line or not re.search(r"(补正|缺少|退回|请补充|不予)", line):
                continue
            material_type = ReceiptService._guess_type(line)
            findings.append({
                "text": line,
                "material_type": material_type,
                "reason": line,
            })
        return findings

    @staticmethod
    def _guess_type(text: str) -> str:
        for material_type, pattern in ISSUE_PATTERNS.items():
            if pattern.search(text):
                return material_type
        return "other"

    # ----------------------------------------------------------- 补件闭环

    def supply_material(
        self,
        app_id: str,
        filename: str,
        material_type: str,
        content: bytes,
        note: str = "",
    ) -> Material:
        """企业补交材料：自动形成新版本并关联到对应可办理事项。"""
        from .crawler import sha256_hex

        app = self.storage.get_application(app_id)
        if app is None:
            raise KeyError(app_id)
        version = self.storage.next_material_version(app_id)
        previous = [
            m for m in self.storage.latest_material_snapshot(app_id)
            if m.filename == filename
        ]
        material = self.storage.add_material(Material(
            material_id=None, app_id=app_id, filename=filename,
            material_type=material_type, version=version,
            content_hash=sha256_hex(content), bytes_len=len(content),
            uploaded_at=now_iso(),
            supersedes=previous[0].material_id if previous else None, note=note,
        ))
        for action in self.storage.list_action_items(app_id, open_only=True):
            if action.material_type == material_type:
                self.storage.update_action_item(
                    action.action_id,
                    status=ActionStatus.MATERIAL_SUPPLIED,
                    supplied_material_id=material.material_id,
                )
        return material

    def review_supplied_material(
        self, action_id: int, sufficient: bool, actor: str, comment: str = ""
    ) -> ActionItem:
        """材料审核员复核补件：通过则办结，不通过则退回继续补。"""
        items = {a.action_id: a for a in self.storage.list_action_items()}
        action = items.get(action_id)
        if action is None:
            raise KeyError(action_id)
        if sufficient:
            self.storage.update_action_item(
                action_id, status=ActionStatus.DONE, resolved_at=now_iso()
            )
        else:
            self.storage.update_action_item(
                action_id, status=ActionStatus.OPEN,
                reason=action.reason + f"（{actor} 复核不通过：{comment}）",
            )
        self.storage.log_confirmation(
            action.app_id, Role.MATERIAL_REVIEWER,
            actor, "补件复核", comment,
        )
        return next(
            a for a in self.storage.list_action_items(action.app_id)
            if a.action_id == action_id
        )

    def open_action_summary(self, app_id: str) -> dict[str, list[ActionItem]]:
        """按材料类型汇总仍需办理的事项。"""
        summary: dict[str, list[ActionItem]] = {}
        for action in self.storage.list_action_items(app_id, open_only=True):
            summary.setdefault(action.material_type, []).append(action)
        return summary

    # ----------------------------------------------------------- 受影响部分重提

    def resubmit_affected(self, app_id: str) -> Submission:
        """补件完成后，仅重新上传受影响附件的新版本。

        - 仍有未办结事项时拒绝提交，避免把未补正的材料再次送进门。
        - 重提使用当前材料版本号生成新幂等键，门户侧表现为一次新的补正提交，
          但原受理编号与完整提交历史均保留可查。
        """
        app = self.storage.get_application(app_id)
        open_actions = self.storage.list_action_items(app_id, open_only=True)
        if open_actions:
            raise ReceiptError(
                f"还有 {len(open_actions)} 个可办理事项未办结，不能重新提交"
            )
        affected_types = {
            a.material_type for a in self.storage.list_action_items(app_id)
        }
        snapshot = self.storage.latest_material_snapshot(app_id)
        affected_files = sorted(
            m.filename for m in snapshot if m.material_type in affected_types
        ) or sorted(m.filename for m in snapshot)
        material_version = max(m.version for m in snapshot)
        key = make_idempotency_key(
            app_id, material_version, app.rule, app.policy_revision
        )
        existing = self.storage.get_submission_by_key(key)
        if existing is not None:
            return existing
        sub = self.storage.add_submission(Submission(
            submission_id=None, app_id=app_id, idempotency_key=key,
            material_version=material_version, policy_rule=app.rule,
            policy_revision=app.policy_revision, files=affected_files,
            state=SubmissionState.PLANNED, portal_ref=None,
            created_at=now_iso(), updated_at=now_iso(),
            detail="补正后仅重提受影响附件",
        ))
        self.storage.commit()
        return self.submissions.submit(app_id)
