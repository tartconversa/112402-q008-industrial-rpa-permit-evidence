"""SQLite 持久化层。

一个应用对应一个数据库文件，所有写操作在事务内完成。
表结构同时保存政策快照、材料版本、提交历史与确认/办理记录，
保证已受理申请可回溯提交时的政策与材料版本。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .models import (
    ActionItem,
    ActionStatus,
    AppState,
    Application,
    AttachmentVersion,
    Material,
    PendingItem,
    PendingStatus,
    PendingType,
    PolicyRule,
    Receipt,
    ReceiptKind,
    Role,
    Submission,
    SubmissionState,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_rules (
    rule TEXT NOT NULL,
    revision INTEGER NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL,
    min_investment INTEGER,
    valid_from TEXT,
    valid_to TEXT,
    state TEXT NOT NULL,
    source_url TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (rule, revision)
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    revision_hint TEXT,
    content_hash TEXT NOT NULL,
    bytes_len INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    app_id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL,
    investment INTEGER NOT NULL,
    rule TEXT,
    policy_revision INTEGER,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    locked_rule TEXT,
    locked_policy_revision INTEGER,
    locked_material_version INTEGER,
    accepted_at TEXT,
    rule_confirmed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS materials (
    material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL REFERENCES applications(app_id),
    filename TEXT NOT NULL,
    material_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    bytes_len INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    supersedes INTEGER,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (app_id, filename, version)
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL REFERENCES applications(app_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    material_version INTEGER NOT NULL,
    policy_rule TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    files TEXT NOT NULL,
    state TEXT NOT NULL,
    portal_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL REFERENCES applications(app_id),
    submission_id INTEGER NOT NULL REFERENCES submissions(submission_id),
    portal_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    accepted_files TEXT NOT NULL,
    rejected_files TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL REFERENCES applications(app_id),
    type TEXT NOT NULL,
    role TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL,
    related_rule TEXT,
    related_material TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL REFERENCES applications(app_id),
    receipt_id INTEGER NOT NULL REFERENCES receipts(receipt_id),
    condition_ref TEXT NOT NULL,
    material_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    supplied_material_id INTEGER REFERENCES materials(material_id)
);

CREATE TABLE IF NOT EXISTS confirmation_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    role TEXT NOT NULL,
    actor TEXT NOT NULL,
    decision TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    # 保留微秒：同一秒内的挂起、补件、重评估需要可靠的先后顺序。
    return datetime.now().isoformat()


def _parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


class Storage:
    """封装所有数据库访问。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """对已存在的数据库补齐后加列，保持文件库可升级。"""
        columns = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(applications)")
        }
        if "rule_confirmed" not in columns:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN rule_confirmed "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 政策

    def upsert_policy_rule(self, rule: PolicyRule) -> bool:
        """插入或更新条款版本；内容未变化时返回 False。"""
        row = rule.to_row()
        existing = self.conn.execute(
            "SELECT 1 FROM policy_rules WHERE rule=? AND revision=?",
            (rule.rule, rule.revision),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """INSERT INTO policy_rules
                   (rule, revision, category, location, min_investment, valid_from,
                    valid_to, state, source_url, snapshot_id, fetched_at)
                   VALUES (:rule, :revision, :category, :location, :min_investment,
                           :valid_from, :valid_to, :state, :source_url, :snapshot_id, :fetched_at)""",
                row,
            )
            return True
        current = self.conn.execute(
            """SELECT category, location, min_investment, valid_from, valid_to,
                      state, source_url, snapshot_id
               FROM policy_rules WHERE rule=? AND revision=?""",
            (rule.rule, rule.revision),
        ).fetchone()
        changed = (
            current["category"] != row["category"]
            or current["location"] != row["location"]
            or current["min_investment"] != row["min_investment"]
            or current["valid_from"] != row["valid_from"]
            or current["valid_to"] != row["valid_to"]
            or current["state"] != row["state"]
            or current["source_url"] != row["source_url"]
        )
        if changed:
            self.conn.execute(
                """UPDATE policy_rules SET category=:category, location=:location,
                   min_investment=:min_investment, valid_from=:valid_from,
                   valid_to=:valid_to, state=:state, source_url=:source_url,
                   snapshot_id=:snapshot_id, fetched_at=:fetched_at
                   WHERE rule=:rule AND revision=:revision""",
                row,
            )
        return changed

    def add_attachment(self, att: AttachmentVersion) -> bool:
        """同一 attachment_id 下内容哈希变化即新版本，返回是否新增。"""
        exists = self.conn.execute(
            "SELECT 1 FROM attachments WHERE attachment_id=? AND content_hash=?",
            (att.attachment_id, att.content_hash),
        ).fetchone()
        if exists:
            return False
        self.conn.execute(
            """INSERT INTO attachments
               (attachment_id, url, filename, revision_hint, content_hash,
                bytes_len, snapshot_id, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (att.attachment_id, att.url, att.filename, att.revision_hint,
             att.content_hash, att.bytes_len, att.snapshot_id, att.fetched_at),
        )
        return True

    def list_policy_rules(self, active_only: bool = False) -> list[PolicyRule]:
        sql = "SELECT * FROM policy_rules"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY rule, revision"
        return [self._rule_from_row(r) for r in self.conn.execute(sql)]

    def get_rule_revisions(self, rule: str) -> list[PolicyRule]:
        rows = self.conn.execute(
            "SELECT * FROM policy_rules WHERE rule=? ORDER BY revision", (rule,)
        )
        return [self._rule_from_row(r) for r in rows]

    def list_attachments(self) -> list[AttachmentVersion]:
        rows = self.conn.execute(
            "SELECT * FROM attachments ORDER BY attachment_id, fetched_at"
        )
        return [AttachmentVersion(**dict(r)) for r in rows]

    @staticmethod
    def _rule_from_row(r: sqlite3.Row) -> PolicyRule:
        return PolicyRule(
            rule=r["rule"], revision=r["revision"], category=r["category"],
            location=r["location"], min_investment=r["min_investment"],
            valid_from=_parse_date(r["valid_from"]),
            valid_to=_parse_date(r["valid_to"]),
            state=r["state"], source_url=r["source_url"],
            snapshot_id=r["snapshot_id"], fetched_at=r["fetched_at"],
        )

    # --------------------------------------------------------------- 申请/材料

    def create_application(self, app: Application) -> None:
        self.conn.execute(
            """INSERT INTO applications
               (app_id, company, category, location, investment, rule,
                policy_revision, state, created_at, updated_at, locked_rule,
                locked_policy_revision, locked_material_version, accepted_at,
                rule_confirmed, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (app.app_id, app.company, app.category, app.location, app.investment,
             app.rule, app.policy_revision, app.state.value, app.created_at,
             app.updated_at, app.locked_rule, app.locked_policy_revision,
             app.locked_material_version, app.accepted_at,
             int(app.rule_confirmed), app.note),
        )

    def get_application(self, app_id: str) -> Optional[Application]:
        r = self.conn.execute(
            "SELECT * FROM applications WHERE app_id=?", (app_id,)
        ).fetchone()
        return self._app_from_row(r) if r else None

    def list_applications(self) -> list[Application]:
        return [
            self._app_from_row(r)
            for r in self.conn.execute("SELECT * FROM applications ORDER BY app_id")
        ]

    def update_application_state(
        self, app_id: str, state: AppState, **fields
    ) -> None:
        sets = ["state=?", "updated_at=?"]
        params: list = [state.value, now_iso()]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            params.append(value)
        params.append(app_id)
        self.conn.execute(
            f"UPDATE applications SET {', '.join(sets)} WHERE app_id=?", params
        )

    @staticmethod
    def _app_from_row(r: sqlite3.Row) -> Application:
        return Application(
            app_id=r["app_id"], company=r["company"], category=r["category"],
            location=r["location"], investment=r["investment"], rule=r["rule"],
            policy_revision=r["policy_revision"], state=AppState(r["state"]),
            created_at=r["created_at"], updated_at=r["updated_at"],
            locked_rule=r["locked_rule"],
            locked_policy_revision=r["locked_policy_revision"],
            locked_material_version=r["locked_material_version"],
            accepted_at=r["accepted_at"],
            rule_confirmed=bool(r["rule_confirmed"]), note=r["note"],
        )

    def add_material(self, material: Material) -> Material:
        cur = self.conn.execute(
            """INSERT INTO materials
               (app_id, filename, material_type, version, content_hash,
                bytes_len, uploaded_at, supersedes, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (material.app_id, material.filename, material.material_type,
             material.version, material.content_hash, material.bytes_len,
             material.uploaded_at, material.supersedes, material.note),
        )
        material.material_id = cur.lastrowid
        return material

    def next_material_version(self, app_id: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM materials WHERE app_id=?",
            (app_id,),
        ).fetchone()
        return int(r["v"]) + 1

    def list_materials(
        self, app_id: str, version: Optional[int] = None
    ) -> list[Material]:
        sql = "SELECT * FROM materials WHERE app_id=?"
        params: list = [app_id]
        if version is not None:
            sql += " AND version<=?"
            params.append(version)
        sql += " ORDER BY material_id"
        return [self._material_from_row(r) for r in self.conn.execute(sql, params)]

    def latest_material_snapshot(self, app_id: str) -> list[Material]:
        """每个文件名取最新版本，构成当前材料快照。"""
        rows = self.conn.execute(
            """SELECT m.* FROM materials m
               JOIN (SELECT filename, MAX(version) AS v FROM materials
                     WHERE app_id=? GROUP BY filename) latest
               ON m.filename=latest.filename AND m.version=latest.v
               WHERE m.app_id=?""",
            (app_id, app_id),
        )
        return [self._material_from_row(r) for r in rows]

    @staticmethod
    def _material_from_row(r: sqlite3.Row) -> Material:
        return Material(
            material_id=r["material_id"], app_id=r["app_id"],
            filename=r["filename"], material_type=r["material_type"],
            version=r["version"], content_hash=r["content_hash"],
            bytes_len=r["bytes_len"], uploaded_at=r["uploaded_at"],
            supersedes=r["supersedes"], note=r["note"],
        )

    # ------------------------------------------------------------------ 提交

    def add_submission(self, sub: Submission) -> Submission:
        cur = self.conn.execute(
            """INSERT INTO submissions
               (app_id, idempotency_key, material_version, policy_rule,
                policy_revision, files, state, portal_ref, created_at,
                updated_at, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sub.app_id, sub.idempotency_key, sub.material_version,
             sub.policy_rule, sub.policy_revision, json.dumps(sub.files,
             ensure_ascii=False), sub.state.value, sub.portal_ref,
             sub.created_at, sub.updated_at, sub.detail),
        )
        sub.submission_id = cur.lastrowid
        return sub

    def get_submission_by_key(self, key: str) -> Optional[Submission]:
        r = self.conn.execute(
            "SELECT * FROM submissions WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._submission_from_row(r) if r else None

    def get_submission(self, submission_id: int) -> Optional[Submission]:
        r = self.conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        return self._submission_from_row(r) if r else None

    def list_submissions(self, app_id: str) -> list[Submission]:
        rows = self.conn.execute(
            "SELECT * FROM submissions WHERE app_id=? ORDER BY submission_id",
            (app_id,),
        )
        return [self._submission_from_row(r) for r in rows]

    def update_submission(
        self, submission_id: int, state: SubmissionState,
        portal_ref: Optional[str] = None, detail: Optional[str] = None,
    ) -> None:
        fields = ["state=?", "updated_at=?"]
        params: list = [state.value, now_iso()]
        if portal_ref is not None:
            fields.append("portal_ref=?")
            params.append(portal_ref)
        if detail is not None:
            fields.append("detail=?")
            params.append(detail)
        params.append(submission_id)
        self.conn.execute(
            f"UPDATE submissions SET {', '.join(fields)} WHERE submission_id=?",
            params,
        )

    @staticmethod
    def _submission_from_row(r: sqlite3.Row) -> Submission:
        return Submission(
            submission_id=r["submission_id"], app_id=r["app_id"],
            idempotency_key=r["idempotency_key"],
            material_version=r["material_version"], policy_rule=r["policy_rule"],
            policy_revision=r["policy_revision"],
            files=json.loads(r["files"]), state=SubmissionState(r["state"]),
            portal_ref=r["portal_ref"], created_at=r["created_at"],
            updated_at=r["updated_at"], detail=r["detail"],
        )

    # ------------------------------------------------------------------ 回执

    def add_receipt(self, receipt: Receipt) -> Receipt:
        cur = self.conn.execute(
            """INSERT INTO receipts
               (app_id, submission_id, portal_ref, kind, received_at, raw_text,
                accepted_files, rejected_files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt.app_id, receipt.submission_id, receipt.portal_ref,
             receipt.kind.value, receipt.received_at, receipt.raw_text,
             json.dumps(receipt.accepted_files, ensure_ascii=False),
             json.dumps(receipt.rejected_files, ensure_ascii=False)),
        )
        receipt.receipt_id = cur.lastrowid
        return receipt

    def list_receipts(self, app_id: str) -> list[Receipt]:
        rows = self.conn.execute(
            "SELECT * FROM receipts WHERE app_id=? ORDER BY receipt_id", (app_id,)
        )
        return [self._receipt_from_row(r) for r in rows]

    @staticmethod
    def _receipt_from_row(r: sqlite3.Row) -> Receipt:
        return Receipt(
            receipt_id=r["receipt_id"], app_id=r["app_id"],
            submission_id=r["submission_id"], portal_ref=r["portal_ref"],
            kind=ReceiptKind(r["kind"]), received_at=r["received_at"],
            raw_text=r["raw_text"], accepted_files=json.loads(r["accepted_files"]),
            rejected_files=json.loads(r["rejected_files"]),
        )

    # ------------------------------------------------------------ 待确认/事项

    def add_pending_item(self, item: PendingItem) -> PendingItem:
        cur = self.conn.execute(
            """INSERT INTO pending_items
               (app_id, type, role, question, status, created_at, resolved_at,
                resolution, options, related_rule, related_material)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.app_id, item.type.value, item.role.value, item.question,
             item.status.value, item.created_at, item.resolved_at,
             item.resolution, json.dumps(item.options, ensure_ascii=False),
             item.related_rule, item.related_material),
        )
        item.item_id = cur.lastrowid
        return item

    def resolve_pending_item(
        self, item_id: int, resolution: str, dismissed: bool = False
    ) -> None:
        self.conn.execute(
            """UPDATE pending_items SET status=?, resolution=?, resolved_at=?
               WHERE item_id=?""",
            (PendingStatus.DISMISSED.value if dismissed
             else PendingStatus.RESOLVED.value, resolution, now_iso(), item_id),
        )

    def list_pending_items(
        self, app_id: Optional[str] = None, open_only: bool = False
    ) -> list[PendingItem]:
        sql = "SELECT * FROM pending_items"
        clauses, params = [], []
        if app_id:
            clauses.append("app_id=?")
            params.append(app_id)
        if open_only:
            clauses.append("status='open'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY item_id"
        return [self._pending_from_row(r) for r in self.conn.execute(sql, params)]

    @staticmethod
    def _pending_from_row(r: sqlite3.Row) -> PendingItem:
        return PendingItem(
            item_id=r["item_id"], app_id=r["app_id"],
            type=PendingType(r["type"]), role=Role(r["role"]),
            question=r["question"], status=PendingStatus(r["status"]),
            created_at=r["created_at"], resolved_at=r["resolved_at"],
            resolution=r["resolution"], options=json.loads(r["options"]),
            related_rule=r["related_rule"], related_material=r["related_material"],
        )

    def add_action_item(self, item: ActionItem) -> ActionItem:
        cur = self.conn.execute(
            """INSERT INTO action_items
               (app_id, receipt_id, condition_ref, material_type, reason, status,
                created_at, resolved_at, supplied_material_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.app_id, item.receipt_id, item.condition_ref, item.material_type,
             item.reason, item.status.value, item.created_at, item.resolved_at,
             item.supplied_material_id),
        )
        item.action_id = cur.lastrowid
        return item

    def update_action_item(self, action_id: int, **fields) -> None:
        if "status" in fields and isinstance(fields["status"], ActionStatus):
            fields["status"] = fields["status"].value
        sets = [f"{k}=?" for k in fields]
        self.conn.execute(
            f"UPDATE action_items SET {', '.join(sets)} WHERE action_id=?",
            list(fields.values()) + [action_id],
        )

    def list_action_items(
        self, app_id: Optional[str] = None, open_only: bool = False
    ) -> list[ActionItem]:
        sql = "SELECT * FROM action_items"
        clauses, params = [], []
        if app_id:
            clauses.append("app_id=?")
            params.append(app_id)
        if open_only:
            clauses.append("status IN ('open','material_supplied')")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY action_id"
        rows = self.conn.execute(sql, params)
        return [
            ActionItem(
                action_id=r["action_id"], app_id=r["app_id"],
                receipt_id=r["receipt_id"], condition_ref=r["condition_ref"],
                material_type=r["material_type"], reason=r["reason"],
                status=ActionStatus(r["status"]), created_at=r["created_at"],
                resolved_at=r["resolved_at"],
                supplied_material_id=r["supplied_material_id"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ 日志

    def log_confirmation(
        self, app_id: str, role: Role, actor: str, decision: str, detail: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO confirmation_log
               (app_id, role, actor, decision, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (app_id, role.value, actor, decision, detail, now_iso()),
        )

    def list_confirmation_log(self, app_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM confirmation_log WHERE app_id=? ORDER BY log_id",
            (app_id,),
        )
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
