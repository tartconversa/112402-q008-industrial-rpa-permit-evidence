"""规则引擎：条款适用判断、材料覆盖检查与待确认项生成。

核心原则（对应业务要求）：
- 信息冲突或指南适用性不清时，生成 *待确认项* 并阻断自动提交，
  绝不自动选择门槛最低（最宽松）的条款。
- 已受理申请按 *提交时锁定* 的条款修订与材料版本办理；
  政策更新是否影响在办项目，只生成待判断项，由工作人员决定。
- 确认职责按角色拆分：政策专员管条款，企业经办人管事实，材料审核员管证明。
"""
from __future__ import annotations

import dataclasses
from datetime import date
from typing import Optional

from .models import (
    AppState,
    Application,
    PendingItem,
    PendingStatus,
    PendingType,
    PolicyRule,
    Role,
)
from .storage import Storage, now_iso

# 每个条款要求企业提供的材料类型。真实系统中该映射来自附件指南解析，
# 这里集中声明，便于回执问题与材料类型对应。
REQUIRED_MATERIALS: dict[str, list[str]] = {
    "P1": ["budget_proof", "scope_statement"],
    "P2": ["budget_proof", "scope_statement", "site_photo"],
}


@dataclasses.dataclass
class EvaluationResult:
    """一次适用性评估的结论。"""

    candidate_rules: list[PolicyRule]
    chosen_rule: Optional[PolicyRule]
    pending_created: list[PendingItem]
    blocking: bool

    @property
    def ready(self) -> bool:
        return not self.blocking and self.chosen_rule is not None


class RuleEngine:
    def __init__(self, storage: Storage, today: Optional[date] = None) -> None:
        self.storage = storage
        self.today = today or date.today()

    # ----------------------------------------------------------- 申请录入

    def ingest_application(
        self,
        app_id: str,
        company: str,
        category: str,
        location: str,
        investment: int,
        rule: Optional[str] = None,
        policy_revision: Optional[int] = None,
        note: str = "",
    ) -> Application:
        """登记企业申请。已存在时做事实一致性比对，冲突则生成待确认项。"""
        existing = self.storage.get_application(app_id)
        if existing is not None:
            return self._update_facts(
                existing, company, category, location, investment, note
            )
        app = Application(
            app_id=app_id, company=company, category=category, location=location,
            investment=investment, rule=rule, policy_revision=policy_revision,
            state=AppState.DRAFT, created_at=now_iso(), updated_at=now_iso(),
            note=note,
        )
        self.storage.create_application(app)
        self.storage.log_confirmation(
            app_id, Role.ENTERPRISE_CONTACT, company, "申请登记",
            f"类别={category} 地点={location} 投资额={investment}",
        )
        return app

    def _update_facts(
        self, app: Application, company: str, category: str,
        location: str, investment: int, note: str,
    ) -> Application:
        conflicts = []
        if app.company != company:
            conflicts.append(f"企业名称 {app.company} → {company}")
        if app.category != category:
            conflicts.append(f"项目类别 {app.category} → {category}")
        if app.location != location:
            conflicts.append(f"实施地点 {app.location} → {location}")
        if app.investment != investment:
            conflicts.append(f"投资额 {app.investment} → {investment}")
        if conflicts:
            self.storage.add_pending_item(PendingItem(
                item_id=None, app_id=app.app_id, type=PendingType.FACT_CONFLICT,
                role=Role.ENTERPRISE_CONTACT,
                question="企业重新提交的事实与既有登记不一致：" + "；".join(conflicts),
                status=PendingStatus.OPEN, created_at=now_iso(),
                resolved_at=None, resolution="",
                options=[
                    f"维持原值({app.category}/{app.location}/{app.investment})",
                    f"采用新值({category}/{location}/{investment})",
                ],
            ))
            # 类别或地点变化会改变条款适用性，原人工确认不再有效。
            if app.category != category or app.location != location:
                self.storage.update_application_state(
                    app.app_id, AppState.PENDING_CONFIRM, rule_confirmed=False,
                )
            else:
                self.storage.update_application_state(
                    app.app_id, AppState.PENDING_CONFIRM,
                )
        if note:
            self.storage.update_application_state(
                app.app_id,
                self.storage.get_application(app.app_id).state,
                note=note,
            )
        return self.storage.get_application(app.app_id)

    # ----------------------------------------------------------- 适用判断

    def evaluate(self, app_id: str) -> EvaluationResult:
        """评估申请当前适用的条款版本，并做材料覆盖检查。"""
        app = self.storage.get_application(app_id)
        if app is None:
            raise KeyError(app_id)

        # 已受理：锁定版本优先，只评估政策变更影响。
        if app.state == AppState.ACCEPTED and app.locked_rule:
            return self._evaluate_accepted(app)

        active = [
            r for r in self.storage.list_policy_rules(active_only=True)
            if r.category == app.category and r.location == app.location
        ]
        if not active:
            item = self._open_item(
                app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                f"类别 {app.category}、地点 {app.location} 无任何有效条款，无法判断适用条件。",
                options=["补充条款来源", "确认该项目不适用本指南"],
            )
            return EvaluationResult([], None, [item], True)

        in_force = [r for r in active if self._in_force(r)]
        not_yet = [r for r in active if r.valid_from and r.valid_from > self.today]

        chosen: Optional[PolicyRule] = None
        pending: list[PendingItem] = []

        if not in_force and not_yet:
            # 当前无生效版本、仅有未来版本：生效边界需政策专员确认。
            pending.append(self._open_item(
                app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                "当前无生效条款，仅有尚未到生效日期的新版本："
                + ", ".join(f"{r.rule}v{r.revision}({r.valid_from})" for r in not_yet),
                options=["按旧指南办理", "等待新版本生效", "确认提前适用新版本"],
            ))
        elif not in_force:
            pending.append(self._open_item(
                app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                "条款生效时间信息不足，无法确定适用版本。",
                options=["人工指定适用版本"],
            ))
        elif app.rule_confirmed and app.rule and app.policy_revision is not None:
            # 政策专员已明确指定版本：尊重人工判断，不再重复挂起同一冲突。
            confirmed = next(
                (r for r in in_force
                 if r.rule == app.rule and r.revision == app.policy_revision),
                None,
            )
            if confirmed is None:
                pending.append(self._open_item(
                    app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                    f"已确认的 {app.rule}v{app.policy_revision} 已不在生效范围内，"
                    "需重新确认适用版本。",
                    options=["人工重新指定适用版本"],
                ))
            else:
                chosen = confirmed
        else:
            # 同一条款的多个修订：取生效日最新且已生效的一版
            # （旧申请在受理时已锁定版本，不受此收敛影响）。
            latest_by_rule: dict[str, PolicyRule] = {}
            for rule in in_force:
                current = latest_by_rule.get(rule.rule)
                if current is None or self._effective_order(rule) > \
                        self._effective_order(current):
                    latest_by_rule[rule.rule] = rule
            finalists = list(latest_by_rule.values())
            if len(finalists) > 1:
                # 不同条款同时命中同一项目，适用范围不清。
                pending.append(self._rule_conflict_item(app, finalists))
            else:
                winner = finalists[0]
                same_day = [
                    r for r in in_force
                    if r.rule == winner.rule
                    and self._effective_order(r) == self._effective_order(winner)
                ]
                if len(same_day) > 1:
                    # 同一日生效的多个修订无法自动分辨新旧。
                    pending.append(self._rule_conflict_item(app, same_day))
                else:
                    chosen = winner

        # 企业指定条款与引擎结论不一致时，同样挂起而非静默覆盖。
        if chosen and app.rule and (app.rule != chosen.rule or
                                    (app.policy_revision is not None
                                     and app.policy_revision != chosen.revision)):
            pending.append(self._open_item(
                app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                f"企业申报引用 {app.rule}v{app.policy_revision}，"
                f"与当前适用 {chosen.rule}v{chosen.revision} 不一致。",
                options=[f"采用 {chosen.rule}v{chosen.revision}",
                         f"维持企业引用 {app.rule}v{app.policy_revision}"],
                related_rule=chosen.rule,
            ))
            chosen = None

        if chosen:
            threshold_ok = (
                chosen.min_investment is None
                or app.investment >= chosen.min_investment
            )
            if not threshold_ok:
                pending.append(self._open_item(
                    app, PendingType.RULE_UNCLEAR, Role.POLICY_OFFICER,
                    f"投资额 {app.investment} 低于 {chosen.rule}v{chosen.revision} "
                    f"门槛 {chosen.min_investment}，是否符合备案条件需确认。",
                    options=["不符合，退回企业", "按例外情形办理（需说明理由）"],
                    related_rule=chosen.rule,
                ))
                chosen = None
            else:
                pending.extend(self._check_materials(app, chosen))

        open_pending = self.storage.list_pending_items(app_id, open_only=True)
        blocking = bool(open_pending) or chosen is None
        if blocking and app.state not in (AppState.ACCEPTED, AppState.SUBMITTED):
            self.storage.update_application_state(app.app_id, AppState.PENDING_CONFIRM)
        elif not blocking and app.state in (AppState.DRAFT, AppState.PENDING_CONFIRM,
                                            AppState.CORRECTION, AppState.PARTIAL):
            self.storage.update_application_state(
                app.app_id, AppState.READY,
                rule=chosen.rule, policy_revision=chosen.revision,
            )
        return EvaluationResult(active, chosen, pending, blocking)

    def _in_force(self, rule: PolicyRule) -> bool:
        if rule.valid_from and rule.valid_from > self.today:
            return False
        if rule.valid_to and rule.valid_to < self.today:
            return False
        return True

    @staticmethod
    def _effective_order(rule: PolicyRule) -> date:
        """修订排序键：未标注生效日的版本视为最早，排在有日期版本之前。"""
        return rule.valid_from or date.min

    def _rule_conflict_item(
        self, app: Application, rules: list[PolicyRule]
    ) -> PendingItem:
        desc = ", ".join(
            f"{r.rule}v{r.revision}(门槛={r.min_investment}, 生效={r.valid_from})"
            for r in rules
        )
        return self._open_item(
            app, PendingType.RULE_CONFLICT, Role.POLICY_OFFICER,
            f"同一项目同时匹配到多个生效条款版本：{desc}。"
            "系统不会自动选择门槛最低的版本，请确认适用哪一版。",
            options=[f"指定 {r.rule}v{r.revision}" for r in rules],
            related_rule=rules[0].rule,
        )

    # ----------------------------------------------------------- 材料覆盖

    def _check_materials(
        self, app: Application, rule: PolicyRule
    ) -> list[PendingItem]:
        required = REQUIRED_MATERIALS.get(rule.rule, [])
        snapshot = self.storage.latest_material_snapshot(app.app_id)
        have_types = {m.material_type: m for m in snapshot}
        items: list[PendingItem] = []
        open_gaps = [
            p for p in self.storage.list_pending_items(app.app_id, open_only=True)
            if p.type == PendingType.MATERIAL_GAP
        ]
        for gap in open_gaps:
            latest = have_types.get(gap.related_material)
            # 仅当企业在缺口提出之后补交了新材料，缺口才自动解除；
            # 审核员已判定不充分的旧材料保持挂起，等待下一版补件。
            if latest is not None and latest.uploaded_at > gap.created_at:
                self.storage.resolve_pending_item(
                    gap.item_id,
                    f"已收到新版 {gap.related_material} 材料，缺口解除，待审核员复核",
                )
        for material_type in required:
            if material_type not in have_types:
                items.append(self._open_item(
                    app, PendingType.MATERIAL_GAP, Role.MATERIAL_REVIEWER,
                    f"条款 {rule.rule}v{rule.revision} 要求的 {material_type} "
                    "尚未提供，无法证明项目满足该条件。",
                    options=[f"补交 {material_type}", "提交等效证明并说明"],
                    related_rule=rule.rule, related_material=material_type,
                ))
        return items

    def review_material(
        self, app_id: str, material_type: str, sufficient: bool,
        actor: str, comment: str = "",
    ) -> Optional[PendingItem]:
        """材料审核员判断某类证明是否覆盖对应条件。"""
        target = next(
            (p for p in self.storage.list_pending_items(app_id, open_only=True)
             if p.type == PendingType.MATERIAL_GAP
             and p.related_material == material_type),
            None,
        )
        decision = "材料充分" if sufficient else "材料不足"
        self.storage.log_confirmation(
            app_id, Role.MATERIAL_REVIEWER, actor, decision,
            f"{material_type}: {comment}",
        )
        if target is not None and sufficient:
            self.storage.resolve_pending_item(
                target.item_id, f"{actor} 认定 {material_type} 已覆盖条件：{comment}"
            )
        elif target is not None:
            # 审核不通过：保持挂起并记录意见，等待企业补件。
            return target
        return target

    # ----------------------------------------------------- 已受理与政策变更

    def _evaluate_accepted(
        self, app: Application
    ) -> EvaluationResult:
        locked = next(
            (r for r in self.storage.get_rule_revisions(app.locked_rule)
             if r.revision == app.locked_policy_revision),
            None,
        )
        open_change = [
            p for p in self.storage.list_pending_items(app.app_id, open_only=True)
            if p.type == PendingType.POLICY_CHANGE
        ]
        newer = [
            r for r in self.storage.get_rule_revisions(app.locked_rule)
            if r.revision > (app.locked_policy_revision or 0)
        ]
        if newer and not open_change:
            item = self._open_item(
                app, PendingType.POLICY_CHANGE, Role.POLICY_OFFICER,
                f"申请已按 {app.locked_rule}v{app.locked_policy_revision} 受理，"
                f"现存在新版本 "
                + ", ".join(f"v{r.revision}({r.valid_from})" for r in newer)
                + "。是否影响在办项目需人工判断，受理版本不会被自动替换。",
                options=["维持受理版本", "按新版本补正", "撤回后重新申报"],
                related_rule=app.locked_rule,
            )
            open_change.append(item)
        return EvaluationResult(
            [locked] if locked else [], locked, open_change, bool(open_change)
        )

    def resolve_policy_change(
        self, app_id: str, keep_locked: bool, actor: str, comment: str = ""
    ) -> None:
        """工作人员决定政策变更是否影响在办项目。"""
        items = [
            p for p in self.storage.list_pending_items(app_id, open_only=True)
            if p.type == PendingType.POLICY_CHANGE
        ]
        decision = "维持受理版本" if keep_locked else "要求按新版本补正"
        for item in items:
            self.storage.resolve_pending_item(item.item_id, f"{actor}: {decision} {comment}")
        self.storage.log_confirmation(
            app_id, Role.POLICY_OFFICER, actor, decision, comment
        )

    # ----------------------------------------------------------- 人工确认

    def confirm_pending(
        self, item_id: int, resolution: str, actor: str,
        role: Role, dismissed: bool = False,
    ) -> None:
        """对应角色人工处置待确认项，并记录确认日志。"""
        item = next(
            (p for p in self.storage.list_pending_items() if p.item_id == item_id),
            None,
        )
        if item is None:
            raise KeyError(item_id)
        if item.role != role:
            raise PermissionError(
                f"待确认项 {item_id} 应由 {item.role.value} 处理，而非 {role.value}"
            )
        self.storage.resolve_pending_item(item_id, resolution, dismissed)
        self.storage.log_confirmation(
            item.app_id, role, actor,
            "驳回待确认项" if dismissed else "确认待确认项", resolution,
        )
        # 重新评估以驱动状态流转。
        self.evaluate(item.app_id)

    def confirm_facts(
        self, app_id: str, actor: str, adopt_new: bool,
        category: Optional[str] = None, location: Optional[str] = None,
        investment: Optional[int] = None,
    ) -> None:
        """企业经办人确认事实冲突的处置方式。"""
        if adopt_new:
            app = self.storage.get_application(app_id)
            self.storage.update_application_state(
                app_id, app.state, category=category or app.category,
                location=location or app.location,
                investment=investment if investment is not None else app.investment,
            )
        for item in self.storage.list_pending_items(app_id, open_only=True):
            if item.type == PendingType.FACT_CONFLICT:
                self.storage.resolve_pending_item(
                    item.item_id,
                    f"{actor} 确认采用{'新' if adopt_new else '原'}事实",
                )
        self.storage.log_confirmation(
            app_id, Role.ENTERPRISE_CONTACT, actor,
            "事实确认", f"采用{'新' if adopt_new else '原'}申报值",
        )
        self.evaluate(app_id)

    def confirm_rule_choice(
        self, item_id: int, rule: str, revision: int, actor: str
    ) -> None:
        """政策专员在冲突/不清条款中指定适用版本。"""
        item = next(
            (p for p in self.storage.list_pending_items() if p.item_id == item_id),
            None,
        )
        if item is None:
            raise KeyError(item_id)
        if item.role != Role.POLICY_OFFICER:
            raise PermissionError("条款适用只能由政策专员确认")
        chosen = next(
            (r for r in self.storage.get_rule_revisions(rule)
             if r.revision == revision),
            None,
        )
        if chosen is None:
            raise ValueError(f"未知条款版本 {rule}v{revision}")
        self.storage.resolve_pending_item(
            item_id, f"{actor} 指定适用 {rule}v{revision}"
        )
        self.storage.log_confirmation(
            item.app_id, Role.POLICY_OFFICER, actor, "指定条款版本",
            f"{rule}v{revision}",
        )
        app = self.storage.get_application(item.app_id)
        self.storage.update_application_state(
            app.app_id, app.state, rule=rule, policy_revision=revision,
            rule_confirmed=True,
        )
        self.evaluate(item.app_id)

    # ----------------------------------------------------------- 内部工具

    def _open_item(
        self, app: Application, ptype: PendingType, role: Role,
        question: str, options: list[str], related_rule: Optional[str] = None,
        related_material: Optional[str] = None,
    ) -> PendingItem:
        # 同一申请同一问题不重复挂起。
        for existing in self.storage.list_pending_items(app.app_id, open_only=True):
            if existing.type == ptype and existing.question == question:
                return existing
        return self.storage.add_pending_item(PendingItem(
            item_id=None, app_id=app.app_id, type=ptype, role=role,
            question=question, status=PendingStatus.OPEN, created_at=now_iso(),
            resolved_at=None, resolution="", options=options,
            related_rule=related_rule, related_material=related_material,
        ))
