"""수정안 생성.

1) 항목별 수정안(suggestions): 점검 결과 하나하나에 대해 '빼기/바꾸기/줄이기/추가하기/시간 나누기'
   후보를 만들고, 실제로 적용해 다시 점검한 뒤 그 항목이 해결되는 것만 보여 준다.
2) 자동 수정안(revision plan): 위험도가 높은 항목부터, 전체 점수가 가장 많이 좋아지는 수정을
   하나씩 적용한다. 사용자가 고정(locked)한 제품은 건드리지 않는다.

사용자의 기존 제품(타사 제품)보다 플랫폼에서 새로 고른 제품을 먼저 조정하도록 가중치를 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .catalog import Catalog
from .checker import Checker, blocks_new_product, summarize
from .context import ProfileContext
from .items import ResolvedItem, platform_item, with_unique_key
from .models import (
    SEVERITY_RANK,
    Finding,
    FindingType,
    Revision,
    RevisionStep,
    Severity,
    Suggestion,
    SuggestionAction,
)
from .text import josa

SEVERITY_WEIGHT = {Severity.AVOID: 100.0, Severity.WARNING: 30.0, Severity.CAUTION: 6.0, Severity.INFO: 0.5}
DUPLICATE_WEIGHT = 0.1     # 기준 안의 중복은 거의 문제가 아니므로 아주 작게
GOAL_GAP_WEIGHT = 20.0
ROLE_LOSS_WEIGHT = 2.0     # 원래 조합이 채우던 기능성(종합 영양 등)을 잃는 정도
ITEM_WEIGHT = 0.3
PRICE_WEIGHT = 0.0005
EXTERNAL_CHANGE_PENALTY = 4.0
MAX_PLAN_STEPS = 6
MAX_TOTAL_ITEMS = 7
MAX_SUGGESTIONS = 3

# 자동 수정안이 해결을 시도하는 항목
_PLAN_TYPES = {
    FindingType.ALLERGY,
    FindingType.PROFILE_CAUTION,
    FindingType.DRUG_INTERACTION,
    FindingType.EXCESS,
    FindingType.SUPPLEMENT_INTERACTION,
    FindingType.BELOW_EFFECTIVE,
    FindingType.GOAL_GAP,
}
# 해당 성분을 가진 제품을 빼거나 바꿔서 해결하는 항목
_ITEM_FIX_TYPES = {
    FindingType.ALLERGY,
    FindingType.PROFILE_CAUTION,
    FindingType.DRUG_INTERACTION,
    FindingType.EXCESS,
    FindingType.NEAR_UPPER,
    FindingType.SUPPLEMENT_INTERACTION,
    FindingType.BELOW_EFFECTIVE,
    FindingType.DUPLICATE,
}


@dataclass(frozen=True)
class _Action:
    kind: SuggestionAction
    target_key: Optional[str] = None
    new_product_id: Optional[str] = None
    new_servings: Optional[float] = None


@dataclass
class _Eval:
    action: _Action
    items: list[ResolvedItem]
    findings: list[Finding]
    score: float


class Reviser:
    def __init__(self, catalog: Catalog, ctx: ProfileContext, checker: Checker):
        self.catalog = catalog
        self.ctx = ctx
        self.checker = checker
        self._cache: dict[tuple, _Eval] = {}
        self._primary_cache: dict[tuple, dict[str, float]] = {}

    # ------------------------------------------------------------------
    def _tag_strengths(self, items: list[ResolvedItem]) -> dict[str, float]:
        out: dict[str, float] = {}
        for it in items:
            for t, info in self.catalog.tags_for_amounts(it.amounts).items():
                out[t] = max(out.get(t, 0.0), info.strength)
        return out

    def _primary_tags(self, items: list[ResolvedItem]) -> dict[str, float]:
        """조합이 확실히 채우고 있는 기능성(강도 0.8 이상). 수정 후 잃는 정도를 계산하는 기준."""
        key = tuple((i.key, i.servings) for i in items)
        if key not in self._primary_cache:
            self._primary_cache[key] = {t: v for t, v in self._tag_strengths(items).items() if v >= 0.8}
        return self._primary_cache[key]

    def score(self, findings: list[Finding], items: list[ResolvedItem], original: list[ResolvedItem]) -> float:
        """낮을수록 좋은 조합. 위험 항목·목적 미충족·기능성 손실·제품 수·가격을 함께 본다."""
        s = 0.0
        for f in findings:
            if f.type == FindingType.GOAL_GAP:
                s += GOAL_GAP_WEIGHT
            elif f.type == FindingType.DUPLICATE:
                s += DUPLICATE_WEIGHT
            elif f.type in (FindingType.UNKNOWN_DRUG, FindingType.UNKNOWN_INGREDIENT, FindingType.ESTIMATED_DATA):
                continue  # 수정으로 해결할 수 없는 항목
            else:
                s += SEVERITY_WEIGHT[f.severity]
        s += ITEM_WEIGHT * len(items)
        s += PRICE_WEIGHT * sum(i.price_per_day for i in items)
        current_keys = {i.key for i in items}
        for it in original:
            if it.source == "external" and it.key not in current_keys:
                s += EXTERNAL_CHANGE_PENALTY
        now = self._tag_strengths(items)
        for tag, before in self._primary_tags(original).items():
            s += ROLE_LOSS_WEIGHT * max(0.0, before - now.get(tag, 0.0))
        return s

    def _apply(self, items: list[ResolvedItem], action: _Action) -> list[ResolvedItem]:
        cat = self.catalog
        if action.kind == SuggestionAction.REMOVE:
            return [i for i in items if i.key != action.target_key]
        if action.kind == SuggestionAction.REPLACE:
            out = []
            for i in items:
                if i.key == action.target_key:
                    new = platform_item(cat, action.new_product_id)
                    out.append(with_unique_key(new, out))
                else:
                    out.append(i)
            return out
        if action.kind == SuggestionAction.ADD:
            return [*items, with_unique_key(platform_item(cat, action.new_product_id), items)]
        if action.kind == SuggestionAction.REDUCE:
            out = []
            for i in items:
                if i.key == action.target_key:
                    out.append(ResolvedItem(
                        key=i.key, product=i.product, servings=action.new_servings, locked=i.locked,
                        custom=i.custom,
                        input=i.input.model_copy(update={"servings": action.new_servings, "actual_servings_per_day": None}),
                        amounts={k: v / i.servings * action.new_servings for k, v in i.amounts.items()},
                        unknown_ingredients=i.unknown_ingredients,
                        dose_lines=i.dose_lines,
                    ))
                else:
                    out.append(i)
            return out
        return items

    def _evaluate(self, items: list[ResolvedItem], action: _Action, original: list[ResolvedItem]) -> _Eval:
        key = (tuple((i.key, i.servings) for i in items), action)
        if key in self._cache:
            return self._cache[key]
        new_items = self._apply(items, action)
        findings, _, _ = self.checker.evaluate(new_items)
        ev = _Eval(action, new_items, findings, self.score(findings, new_items, original))
        self._cache[key] = ev
        return ev

    # ------------------------------------------------------------------
    def _relevant_tags(self, item: ResolvedItem) -> dict[str, float]:
        """제품이 맡고 있는 역할: 사용자 목적과 관련된 기능성 + 제품의 주 기능성."""
        tags = self.catalog.tags_for_amounts(item.amounts)
        return {
            t: i.strength for t, i in tags.items()
            if i.strength >= 0.8 or (self.ctx.demand.get(t, 0) > 0 and i.strength >= 0.3)
        }

    def _replacements(self, item: ResolvedItem, items: list[ResolvedItem], limit: int = 5) -> list[str]:
        cat = self.catalog
        rel = self._relevant_tags(item)
        if not rel:
            return []
        in_combo = {i.product.id for i in items}
        scored = []
        for p in cat.platform_products():
            if p.id in in_combo:
                continue
            tags = cat.tags_for_amounts(cat.product_amounts(p))
            share = sum(
                min(tags[t].strength, s) * max(self.ctx.demand.get(t, 0), 0.2)
                for t, s in rel.items()
                if t in tags and tags[t].strength >= 0.5 * s
            )
            if share > 0:
                scored.append((-share, p.price_per_day, p.id))
        scored.sort()
        return [pid for _, _, pid in scored[:limit]]

    def _effective_dose_products(self, finding: Finding, items: list[ResolvedItem], limit: int = 5) -> list[str]:
        """기능성 기준량 이상을 함유한 플랫폼 제품 (부족 항목 교체용)."""
        cat = self.catalog
        in_combo = {i.product.id for i in items}
        out = []
        for iid in finding.ingredient_ids:
            rng = cat.ingredients[iid].functional_range
            for p in cat.platform_products():
                amount = cat.product_amounts(p).get(iid, 0)
                if p.id not in in_combo and rng and rng.min <= amount <= rng.max:
                    out.append((len(p.ingredients), p.price_per_day, p.id))
        out.sort()
        return [pid for *_, pid in out[:limit]]

    def _adders(self, finding: Finding, items: list[ResolvedItem], limit: int = 5) -> list[str]:
        cat = self.catalog
        in_combo = {i.product.id for i in items}
        out = []
        if finding.type == FindingType.GOAL_GAP:
            goal = cat.goals[finding.id.split(":")[1]]
            core = self.ctx.core_tags(goal)
            for p in cat.platform_products():
                if p.id in in_combo:
                    continue
                tags = cat.tags_for_amounts(cat.product_amounts(p))
                best = max((tags[t].strength for t in core if t in tags), default=0)
                if best >= 0.8:
                    out.append((-best, p.price_per_day, p.id))
        elif finding.type == FindingType.NUTRIENT_LOW:
            iid = finding.ingredient_ids[0]
            ing = cat.ingredients[iid]
            for p in cat.platform_products():
                if p.id in in_combo:
                    continue
                amount = cat.product_amounts(p).get(iid, 0)
                if cat.claim_factor(ing, amount) > 0:
                    out.append((len(p.ingredients), p.price_per_day, p.id))
        out.sort()
        return [pid for *_, pid in out[:limit]]

    def _candidate_actions(self, finding: Finding, items: list[ResolvedItem]) -> list[_Action]:
        by_key = {i.key: i for i in items}
        actions: list[_Action] = []
        if finding.type in _ITEM_FIX_TYPES:
            for key in finding.item_keys:
                item = by_key.get(key)
                if item is None or item.locked:
                    continue
                actions.append(_Action(SuggestionAction.REMOVE, target_key=key))
                if finding.type == FindingType.DUPLICATE:
                    continue  # 중복은 '빼도 되는 제품'만 제안
                replacements = (
                    self._effective_dose_products(finding, items)
                    if finding.type == FindingType.BELOW_EFFECTIVE
                    else self._replacements(item, items)
                )
                for pid in replacements:
                    actions.append(_Action(SuggestionAction.REPLACE, target_key=key, new_product_id=pid))
                if item.servings > 1 and finding.type in (FindingType.EXCESS, FindingType.NEAR_UPPER):
                    actions.append(_Action(SuggestionAction.REDUCE, target_key=key, new_servings=max(1.0, item.servings - 1)))
        if finding.type in (FindingType.GOAL_GAP, FindingType.NUTRIENT_LOW) and len(items) < MAX_TOTAL_ITEMS:
            for pid in self._adders(finding, items):
                actions.append(_Action(SuggestionAction.ADD, new_product_id=pid))
        return actions

    # ------------------------------------------------------------------
    def _describe(self, action: _Action, items: list[ResolvedItem], ev: _Eval, before: list[Finding]) -> Suggestion:
        cat = self.catalog
        by_key = {i.key: i for i in items}
        target = by_key.get(action.target_key) if action.target_key else None
        new_p = cat.products.get(action.new_product_id) if action.new_product_id else None
        after_ids = {f.id for f in ev.findings}
        before_ids = {f.id for f in before}
        resolved = [f for f in before if f.id not in after_ids]
        introduced = [
            f for f in ev.findings
            if f.id not in before_ids and (SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.CAUTION] or f.type == FindingType.GOAL_GAP)
        ]
        price_before = sum(i.price_per_day for i in items)
        price_after = sum(i.price_per_day for i in ev.items)

        if action.kind == SuggestionAction.REMOVE:
            text = f"{josa(target.product.name, '을/를')} 조합에서 빼요."
            if target.source == "external":
                text += " (현재 드시는 제품이에요. 중단이 걱정되면 약사와 상담하세요.)"
        elif action.kind == SuggestionAction.REPLACE:
            text = f"{josa(target.product.name, '을/를')} {josa(new_p.name, '으로/로')} 바꿔요."
        elif action.kind == SuggestionAction.ADD:
            funcs = cat.product_functions(new_p)
            text = f"{josa(new_p.name, '을/를')} 추가해요." + (f" ({funcs[0]})" if funcs else "")
        elif action.kind == SuggestionAction.REDUCE:
            times = target.product.servings_per_day * action.new_servings
            text = f"{target.product.name} 섭취를 표시대로 하루 {times:g}회로 줄여요."
        else:
            text = ""
        important = [f for f in resolved if f.type != FindingType.DUPLICATE or len(resolved) == 1]
        if important:
            text += " → 해결: " + ", ".join(f.title for f in important[:3])
        sid = f"{action.kind.value}:{action.target_key or '-'}:{action.new_product_id or '-'}:{action.new_servings or '-'}"
        return Suggestion(
            id=sid,
            action=action.kind,
            description=text,
            target_item_key=action.target_key,
            target_product_id=target.product.id if target else None,
            new_product_id=action.new_product_id,
            new_servings=action.new_servings,
            resolves=[f.id for f in resolved],
            introduces=[f.title for f in introduced],
            price_change_per_day=price_after - price_before,
        )

    def _introduces_blocking(self, ev: _Eval, before: list[Finding]) -> bool:
        """추가·교체로 새 제품을 넣을 때, 엔진이 먼저 제안하면 안 되는 문제가 생기는지."""
        if ev.action.kind not in (SuggestionAction.ADD, SuggestionAction.REPLACE):
            return False
        before_ids = {f.id for f in before}
        return any(blocks_new_product(f) for f in ev.findings if f.id not in before_ids)

    # ------------------------------------------------------------------
    def suggestions_for(self, finding: Finding, items: list[ResolvedItem], findings: list[Finding]) -> list[Suggestion]:
        """점검 항목 하나에 대한 수정안 (그 항목이 실제로 해결되는 것만)."""
        if finding.type in (FindingType.DRUG_TIMING, FindingType.SUPPLEMENT_TIMING):
            return [Suggestion(
                id=f"separate_timing:{finding.id}",
                action=SuggestionAction.SEPARATE_TIMING,
                description=finding.guidance or "섭취 시간을 나눠 드세요.",
                resolves=[],
            )]
        if finding.type == FindingType.ESTIMATED_DATA:
            return []
        if finding.type in (FindingType.UNKNOWN_DRUG, FindingType.UNKNOWN_INGREDIENT):
            return [Suggestion(
                id=f"consult:{finding.id}",
                action=SuggestionAction.CONSULT,
                description=finding.guidance or "약사에게 확인하세요.",
            )]
        base_score = self.score(findings, items, items)
        evals: list[_Eval] = []
        blocked: list[tuple[str, Finding]] = []
        for action in self._candidate_actions(finding, items):
            ev = self._evaluate(items, action, items)
            if finding.id in {f.id for f in ev.findings}:
                continue  # 이 항목을 해결하지 못하는 수정은 제외
            if self._introduces_blocking(ev, findings):
                # 새 제품이 복용 약·알레르기 등과 충돌하면 제외 (이유는 아래에서 안내)
                before_ids = {f.id for f in findings}
                reason = next(f for f in ev.findings if f.id not in before_ids and blocks_new_product(f))
                blocked.append((self.catalog.products[action.new_product_id].name, reason))
                continue
            new_severe = [
                f for f in ev.findings
                if f.id not in {x.id for x in findings} and SEVERITY_RANK[f.severity] >= SEVERITY_RANK[finding.severity]
                and f.type != FindingType.GOAL_GAP
            ]
            if new_severe and finding.type != FindingType.GOAL_GAP:
                continue  # 같은 수준 이상의 새 문제를 만드는 수정은 제외
            evals.append(ev)
        if finding.type == FindingType.DUPLICATE:
            # 중복은 빼도 목적·기능성이 유지되고 새 주의 항목이 생기지 않는 경우만 제안
            before_ids = {f.id for f in findings}
            evals = [
                e for e in evals
                if e.score < base_score
                and not any(
                    f.id not in before_ids
                    and (f.type == FindingType.GOAL_GAP or SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.CAUTION])
                    for f in e.findings
                )
            ]
        # 섭취량 줄이기는 가장 작은 변경이라 먼저 보여 주고, 나머지는 점수 순
        evals.sort(key=lambda e: (e.action.kind != SuggestionAction.REDUCE, e.score))
        out: list[Suggestion] = []
        seen: set[str] = set()
        for ev in evals:
            s = self._describe(ev.action, items, ev, findings)
            if s.id not in seen:
                seen.add(s.id)
                out.append(s)
            if len(out) >= MAX_SUGGESTIONS:
                break
        if not out and blocked and finding.type in (FindingType.GOAL_GAP, FindingType.NUTRIENT_LOW):
            names = ", ".join(dict.fromkeys(n for n, _ in blocked))
            reasons = ", ".join(dict.fromkeys(r.title for _, r in blocked))
            out.append(Suggestion(
                id=f"consult:{finding.id}",
                action=SuggestionAction.CONSULT,
                description=(
                    f"해당 기능성 제품({names})은 {reasons} 항목이 있어 자동으로 추가하지 않았어요. "
                    "의사·약사와 상담한 뒤 직접 추가할 수 있어요."
                ),
            ))
        if not out and finding.type in _ITEM_FIX_TYPES and finding.item_keys:
            if all(i.locked for i in items if i.key in finding.item_keys):
                out.append(Suggestion(
                    id=f"consult:{finding.id}",
                    action=SuggestionAction.CONSULT,
                    description="고정한 제품이라 자동 수정안에서 제외했어요. 필요하면 고정을 풀고 다시 점검하세요.",
                ))
        return out

    def attach_suggestions(self, findings: list[Finding], items: list[ResolvedItem]) -> None:
        for f in findings:
            f.suggestions = self.suggestions_for(f, items, findings)

    # ------------------------------------------------------------------
    def plan(self, items: list[ResolvedItem]) -> Revision:
        original = list(items)
        current = list(items)
        findings, _, _ = self.checker.evaluate(current)
        before_summary = summarize(findings, current)
        steps: list[RevisionStep] = []
        blocked_by_lock = False

        for order in range(1, MAX_PLAN_STEPS + 1):
            targets = [
                f for f in findings
                if f.type in _PLAN_TYPES
                and (SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.WARNING]
                     or f.type in (FindingType.EXCESS, FindingType.GOAL_GAP, FindingType.BELOW_EFFECTIVE))
            ]
            if not targets:
                break
            cur_score = self.score(findings, current, original)
            best: _Eval | None = None
            for f in targets:
                actions = self._candidate_actions(f, current)
                if not actions and f.item_keys and all(
                    i.locked for i in current if i.key in f.item_keys
                ):
                    blocked_by_lock = True
                for action in actions:
                    ev = self._evaluate(current, action, original)
                    if self._introduces_blocking(ev, findings):
                        continue
                    if best is None or ev.score < best.score:
                        best = ev
            if best is None or best.score >= cur_score - 0.5:
                break
            steps.append(RevisionStep(order=order, suggestion=self._describe(best.action, current, best, findings)))
            current = best.items
            findings = best.findings

        after_summary = summarize(findings, current)
        remaining = [
            f.title for f in findings
            if SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.CAUTION] or f.type == FindingType.GOAL_GAP
        ]
        note = None
        if blocked_by_lock:
            note = "고정한 제품은 수정안에서 빼거나 바꾸지 않았어요."
        elif not steps:
            note = "자동으로 고칠 항목이 없어요." if not remaining else "자동으로 고칠 수 있는 수정안을 찾지 못했어요. 전문가와 상담하세요."
        return Revision(
            applied=bool(steps),
            steps=steps,
            items=[i.input for i in current],
            before=before_summary,
            after=after_summary,
            remaining_findings=remaining,
            note=note,
        )
