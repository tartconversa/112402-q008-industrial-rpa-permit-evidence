"""技改备案业务领域模型与枚举。

仅依赖标准库。所有枚举值使用稳定字符串，便于落库与跨进程传递。
"""
from __future__ import annotations

import dataclasses
import enum
from datetime import date
from typing import Optional


class Role(enum.Enum):
    """系统中的三类确认角色。"""

    POLICY_OFFICER = "policy_officer"        # 政策专员：条款生效时间、适用范围
    ENTERPRISE_CONTACT = "enterprise_contact"  # 企业经办人：投资额、实施地点等事实
    MATERIAL_REVIEWER = "material_reviewer"  # 材料审核员：证明材料是否覆盖条件


class AppState(enum.Enum):
    """申请生命周期状态。"""

    DRAFT = "draft"                    # 草稿，尚未提交
    PENDING_CONFIRM = "pending_confirm"  # 存在待确认项，阻断提交
    READY = "ready"                    # 确认完成、材料齐备，可提交
    SUBMITTED = "submitted"            # 已提交门户，等待回执
    CORRECTION = "correction"          # 回执要求补正
    PARTIAL = "partial"                # 门户只接受了部分附件
    ACCEPTED = "accepted"              # 已受理（锁定提交时的政策与材料版本）
    CLOSED = "closed"                  # 终结/撤回


class SubmissionState(enum.Enum):
    """单次门户提交的状态。"""

    PLANNED = "planned"        # 已生成提交计划，尚未驱动浏览器
    UPLOADING = "uploading"    # 浏览器填报进行中
    SUCCEEDED = "succeeded"    # 门户明确受理
    FAILED = "failed"          # 上传失败
    TIMEOUT = "timeout"        # 等待超时，结果未知
    DUPLICATE = "duplicate"    # 核对门户后发现重复，未再次提交
    ABANDONED = "abandoned"    # 核对后确认门户侧无此单且放弃重试


class ReceiptKind(enum.Enum):
    """门户回执类型。"""

    ACCEPTED = "accepted"      # 全部受理
    PARTIAL = "partial"        # 仅接受部分附件
    CORRECTION = "correction"  # 要求补正
    REJECTED = "rejected"      # 不予受理


class PendingType(enum.Enum):
    """待确认项的来源类型。"""

    RULE_CONFLICT = "rule_conflict"          # 多条规则同时匹配且口径不一致
    RULE_UNCLEAR = "rule_unclear"            # 指南适用性不清（如新版生效边界）
    FACT_CONFLICT = "fact_conflict"          # 企业事实前后不一致
    MATERIAL_GAP = "material_gap"            # 材料不足以证明满足条件
    POLICY_CHANGE = "policy_change"          # 指南更新对在办项目的影响待判断
    ATTACHMENT_VERSION = "attachment_version"  # 附件版本与指南版本对应关系不明


class PendingStatus(enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ActionStatus(enum.Enum):
    """回执派生的可办理事项状态。"""

    OPEN = "open"
    MATERIAL_SUPPLIED = "material_supplied"  # 企业已补件，待审核
    DONE = "done"
    DISMISSED = "dismissed"


@dataclasses.dataclass
class PolicyRule:
    """办事指南中的一条适用条款（某个修订版本）。"""

    rule: str
    revision: int
    category: str
    location: str
    min_investment: Optional[int]
    valid_from: Optional[date]
    valid_to: Optional[date]
    state: str
    source_url: str
    snapshot_id: str
    fetched_at: str

    def to_row(self) -> dict:
        return dataclasses.asdict(self) | {
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }


@dataclasses.dataclass
class AttachmentVersion:
    """政策页面附件的一个内容版本。"""

    attachment_id: str
    url: str
    filename: str
    revision_hint: Optional[str]
    content_hash: str
    bytes_len: int
    snapshot_id: str
    fetched_at: str


@dataclasses.dataclass
class Application:
    """企业备案申请及其事实字段。"""

    app_id: str
    company: str
    category: str
    location: str
    investment: int
    rule: Optional[str]
    policy_revision: Optional[int]
    state: AppState
    created_at: str
    updated_at: str
    locked_rule: Optional[str] = None
    locked_policy_revision: Optional[int] = None
    locked_material_version: Optional[int] = None
    accepted_at: Optional[str] = None
    rule_confirmed: bool = False
    note: str = ""


@dataclasses.dataclass
class Material:
    """企业提交的一份材料的某个版本。"""

    material_id: int | None
    app_id: str
    filename: str
    material_type: str
    version: int
    content_hash: str
    bytes_len: int
    uploaded_at: str
    supersedes: Optional[int] = None
    note: str = ""


@dataclasses.dataclass
class Submission:
    """按材料版本生成的一次门户提交记录。"""

    submission_id: Optional[int]
    app_id: str
    idempotency_key: str
    material_version: int
    policy_rule: str
    policy_revision: int
    files: list[str]
    state: SubmissionState
    portal_ref: Optional[str]
    created_at: str
    updated_at: str
    detail: str = ""


@dataclasses.dataclass
class Receipt:
    """门户回执原文及解析结论。"""

    receipt_id: Optional[int]
    app_id: str
    submission_id: int
    portal_ref: str
    kind: ReceiptKind
    received_at: str
    raw_text: str
    accepted_files: list[str]
    rejected_files: list[str]


@dataclasses.dataclass
class PendingItem:
    """阻断自动办理的待确认项。"""

    item_id: Optional[int]
    app_id: str
    type: PendingType
    role: Role
    question: str
    status: PendingStatus
    created_at: str
    resolved_at: Optional[str]
    resolution: str
    options: list[str]
    related_rule: Optional[str] = None
    related_material: Optional[str] = None


@dataclasses.dataclass
class ActionItem:
    """回执问题映射到政策条件与企业材料后形成的可办理事项。"""

    action_id: Optional[int]
    app_id: str
    receipt_id: int
    condition_ref: str          # 对应的政策条件（条款/附件）
    material_type: str         # 需要补交/修正的材料类型
    reason: str
    status: ActionStatus
    created_at: str
    resolved_at: Optional[str] = None
    supplied_material_id: Optional[int] = None
