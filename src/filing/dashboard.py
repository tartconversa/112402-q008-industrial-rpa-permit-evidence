"""服务中心总览查询：当前环节、回执、待补材料、完整提交历史。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ActionItem, PendingItem, Receipt, Submission
from .storage import Storage


@dataclass
class ApplicationView:
    """单项申请的完整办理视图。"""

    app_id: str
    company: str
    stage: str
    rule: str | None
    policy_revision: int | None
    locked: dict | None
    pending_items: list[PendingItem] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "company": self.company,
            "stage": self.stage,
            "rule": self.rule,
            "policy_revision": self.policy_revision,
            "locked": self.locked,
            "pending_items": [
                {
                    "item_id": p.item_id, "type": p.type.value,
                    "role": p.role.value, "question": p.question,
                    "status": p.status.value, "options": p.options,
                    "resolution": p.resolution,
                }
                for p in self.pending_items
            ],
            "action_items": [
                {
                    "action_id": a.action_id, "condition_ref": a.condition_ref,
                    "material_type": a.material_type, "reason": a.reason,
                    "status": a.status.value,
                }
                for a in self.action_items
            ],
            "receipts": [
                {
                    "receipt_id": r.receipt_id, "portal_ref": r.portal_ref,
                    "kind": r.kind.value, "received_at": r.received_at,
                    "accepted_files": r.accepted_files,
                    "rejected_files": r.rejected_files,
                    "raw_text": r.raw_text,
                }
                for r in self.receipts
            ],
            "submissions": [
                {
                    "submission_id": s.submission_id,
                    "idempotency_key": s.idempotency_key,
                    "material_version": s.material_version,
                    "policy": f"{s.policy_rule}v{s.policy_revision}",
                    "files": s.files, "state": s.state.value,
                    "portal_ref": s.portal_ref, "detail": s.detail,
                    "created_at": s.created_at, "updated_at": s.updated_at,
                }
                for s in self.submissions
            ],
            "materials": self.materials,
        }


class Dashboard:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def view(self, app_id: str) -> ApplicationView:
        app = self.storage.get_application(app_id)
        if app is None:
            raise KeyError(app_id)
        materials = []
        for m in self.storage.list_materials(app_id):
            materials.append({
                "material_id": m.material_id, "filename": m.filename,
                "material_type": m.material_type, "version": m.version,
                "content_hash": m.content_hash[:12], "uploaded_at": m.uploaded_at,
                "supersedes": m.supersedes,
            })
        locked = None
        if app.locked_rule:
            locked = {
                "rule": app.locked_rule,
                "policy_revision": app.locked_policy_revision,
                "material_version": app.locked_material_version,
                "accepted_at": app.accepted_at,
            }
        return ApplicationView(
            app_id=app.app_id, company=app.company, stage=app.state.value,
            rule=app.rule, policy_revision=app.policy_revision, locked=locked,
            pending_items=self.storage.list_pending_items(app_id),
            action_items=self.storage.list_action_items(app_id),
            receipts=self.storage.list_receipts(app_id),
            submissions=self.storage.list_submissions(app_id),
            materials=materials,
        )

    def list_views(self) -> list[ApplicationView]:
        return [self.view(a.app_id) for a in self.storage.list_applications()]

    def stage_summary(self) -> dict[str, int]:
        """按当前环节统计申请数量。"""
        summary: dict[str, int] = {}
        for app in self.storage.list_applications():
            summary[app.state.value] = summary.get(app.state.value, 0) + 1
        return summary
