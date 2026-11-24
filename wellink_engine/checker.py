"""성분 점검: 조합의 성분을 합산하고 과다·중복·부족·병용 주의를 판단한다.

모든 판단은 규칙 기반이며(생성형 AI 사용 안 함), 각 결과에는 근거(기준치 출처,
제품 표시 함량, 병용 규칙 근거)를 함께 붙인다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .catalog import Catalog, Ingredient, TagInfo
from .context import GOAL_COVERED_STRENGTH, ProfileContext
from .items import ResolvedItem
from .models import (
    CATEGORY_LABEL,
    FINDING_CATEGORY,
    SEVERITY_LABEL,
    SEVERITY_RANK,
    CheckReport,
    DistributionEntry,
    Evidence,
    Finding,
    FindingType,
    GoalCoverage,
    ItemView,
    ProductContribution,
    ReferenceValues,
    ReportSummary,
    Severity,
)
from .schedule import build_schedule
from .text import fmt_amount, josa

NEAR_UPPER_RATIO = 0.8
NUTRIENT_LOW_RATIO = 0.3
FUNCTIONAL_SOURCE = "건강기능식품의 기준 및 규격(식약처 고시) 일일섭취량"
LABEL_SOURCE = "제품 표시사항(1일 섭취량 기준)"
RULE_SOURCE = "WelLink 병용 점검 규칙"

_STATUS_LABEL = {
    "excess": "과다",
    "near_upper": "상한 근접",
    "adequate": "적정",
    "low": "낮음",
    "no_reference": "기준 없음",
}
_STATUS_ORDER = {"excess": 0, "near_upper": 1, "low": 2, "adequate": 3, "no_reference": 4}
_CATEGORY_ORDER = ["caution", "interaction", "excess", "deficiency", "duplicate", "check_needed"]


def short_unit(ing: Ingredient) -> str:
    return ing.unit_label.split(" (")[0]


def amt(ing: Ingredient, value: float) -> str:
    return fmt_amount(value, short_unit(ing))


@dataclass
class Totals:
    amounts: dict[str, float]
    contributions: dict[str, list[tuple[ResolvedItem, float]]]

    def has(self, iid: str, minimum: float = 0.0) -> bool:
        v = self.amounts.get(iid, 0.0)
        return v > 0 and v >= minimum


def compute_totals(items: list[ResolvedItem]) -> Totals:
    amounts: dict[str, float] = defaultdict(float)
    contributions: dict[str, list[tuple[ResolvedItem, float]]] = defaultdict(list)
    for item in items:
        for iid, value in item.amounts.items():
            if value <= 0:
                continue
            amounts[iid] += value
            contributions[iid].append((item, value))
    return Totals(dict(amounts), dict(contributions))


def _make_finding(
    ftype: FindingType,
    severity: Severity,
    title: str,
    message: str,
    *,
    fid_extra: str = "",
    guidance: str | None = None,
    ingredient_ids: list[str] | None = None,
    item_keys: list[str] | None = None,
    drug_names: list[str] | None = None,
    evidence: list[Evidence] | None = None,
) -> Finding:
    ings = sorted(set(ingredient_ids or []))
    fid = f"{ftype.value}:{fid_extra or '-'}:{'+'.join(ings) or '-'}"
    category = FINDING_CATEGORY[ftype]
    return Finding(
        id=fid,
        type=ftype,
        category=category,
        category_label=CATEGORY_LABEL[category],
        severity=severity,
        severity_label=SEVERITY_LABEL[severity],
        title=title,
        message=message,
        guidance=guidance,
        ingredient_ids=ings,
        item_keys=list(dict.fromkeys(item_keys or [])),
        drug_names=drug_names or [],
        evidence=evidence or [],
    )


def _label_evidence(ing: Ingredient, contribs: list[tuple[ResolvedItem, float]]) -> list[Evidence]:
    out = []
    for item, value in contribs:
        calc = item.calculation(ing.id, short_unit(ing))
        out.append(Evidence(source=LABEL_SOURCE, detail=f"{item.product.name}: {ing.name} {calc}"))
    return out


class Checker:
    def __init__(self, catalog: Catalog, ctx: ProfileContext):
        self.catalog = catalog
        self.ctx = ctx

    # ------------------------------------------------------------------
    def evaluate(self, items: list[ResolvedItem]) -> tuple[list[Finding], list[GoalCoverage], Totals]:
        """점검 항목만 계산한다(추천·수정안 탐색에서 반복 호출하는 가벼운 경로)."""
        cat = self.catalog
        totals = compute_totals(items)
        findings: list[Finding] = []

        findings += self._allergy(items)
        upper, below = self._amount_checks(totals)
        findings += upper + below
        flagged_upper = {f.ingredient_ids[0] for f in upper}
        findings += self._duplicates(totals, flagged_upper)
        findings += self._drug_rules(totals)
        findings += self._pair_rules(totals)
        findings += self._group_rules(totals)
        findings += self._profile_rules(totals)

        combo_tags = cat.tags_for_amounts(totals.amounts)
        coverage, gap_findings, gap_goals = self._goal_coverage(items, combo_tags)
        findings += gap_findings
        findings += self._nutrient_low(totals, gap_goals)
        findings += self._unknowns(items)

        findings.sort(
            key=lambda f: (-SEVERITY_RANK[f.severity], _CATEGORY_ORDER.index(f.category), f.title)
        )
        return findings, coverage, totals

    def check(self, items: list[ResolvedItem]) -> CheckReport:
        cat, ctx = self.catalog, self.ctx
        findings, coverage, totals = self.evaluate(items)
        distribution = self._distribution(totals)
        summary = summarize(findings, items)
        return CheckReport(
            items=[self._item_view(it) for it in items],
            summary=summary,
            findings=findings,
            distribution=distribution,
            goal_coverage=coverage,
            schedule=build_schedule(cat, ctx, items),
            recognized_drugs=[
                {"input": m.input_text, "name": m.member_name, "class_id": m.drug_class.id, "class_name": m.drug_class.name}
                for m in ctx.drug_matches
            ]
            + [{"input": t, "name": None, "class_id": None, "class_name": "인식하지 못함"} for t in ctx.unknown_drugs],
            disclaimers=self._disclaimers(),
            data_version={**cat.versions, "review_status": "seed (출시 전 전문가 검수 필요)"},
        )

    # ------------------------------------------------------------------
    def _item_view(self, item: ResolvedItem) -> ItemView:
        return ItemView(
            key=item.key,
            product_id=item.product.id,
            name=item.product.name,
            brand=item.product.brand,
            source=item.source,
            servings=item.servings,
            label_servings_per_day=item.product.servings_per_day,
            actual_servings_per_day=item.actual_servings_per_day,
            locked=item.locked,
            price_per_day=item.price_per_day,
            daily_dose_text=item.product.daily_dose_text,
            functions=self.catalog.product_functions(item.product),
            cautions=item.product.cautions,
        )

    def _allergy(self, items: list[ResolvedItem]) -> list[Finding]:
        out = []
        for item in items:
            for aid in item.product.allergens:
                if aid not in self.ctx.allergen_ids:
                    continue
                name = self.catalog.allergens[aid].name
                out.append(
                    _make_finding(
                        FindingType.ALLERGY,
                        Severity.AVOID,
                        f"{item.product.name}: {name} 알레르기 주의",
                        f"{item.product.name}에는 {name} 유래 원료가 들어 있어 입력하신 알레르기와 겹쳐요.",
                        fid_extra=f"{aid}@{item.product.id}",
                        guidance="알레르기가 있는 원료가 든 제품은 드시지 마세요.",
                        item_keys=[item.key],
                        evidence=[Evidence(source=LABEL_SOURCE, detail=f"{item.product.name} 알레르기 유발 원료: {name}")],
                    )
                )
        return out

    def _amount_checks(self, totals: Totals) -> tuple[list[Finding], list[Finding]]:
        cat, ctx = self.catalog, self.ctx
        upper: list[Finding] = []
        below: list[Finding] = []
        for iid, total in totals.amounts.items():
            ing = cat.ingredients[iid]
            contribs = totals.contributions[iid]
            keys = [c[0].key for c in contribs]
            n_items = len({c[0].key for c in contribs})
            dup_text = f" {n_items}개 제품에서 함께 섭취하고 있어요." if n_items > 1 else " 이 제품 하나로 기준을 넘어요."
            label_ev = _label_evidence(ing, contribs)

            if ing.category == "nutrient":
                kd = cat.kdri_for(iid, ctx.profile.age, ctx.profile.sex)
                if kd is None or kd.ul is None:
                    continue
                pct = total / kd.ul * 100
                basis = " (보충제 등 식품 외 섭취 기준)" if kd.ul_basis == "supplement_only" else ""
                ref_ev = Evidence(source=kd.source, detail=f"{ing.name} 상한섭취량 {amt(ing, kd.ul)}/일{basis}")
                extra_note = " ".join(n for n in [kd.ul_note, ing.notes] if n and ("상한" in n or "형태" in n))
                if total > kd.ul:
                    upper.append(
                        _make_finding(
                            FindingType.EXCESS,
                            Severity.WARNING,
                            f"{ing.name} 과다: 상한섭취량의 {pct:.0f}%",
                            f"{ing.name} 하루 합계가 {josa(amt(ing, total), '으로/로')} 상한섭취량 {josa(amt(ing, kd.ul), '을/를')} 넘어요.{dup_text}",
                            guidance=(
                                "상한섭취량을 넘겨 오래 섭취하면 건강에 해로울 수 있어요. 아래 수정안으로 합계를 줄여 보세요."
                                + (f" 참고: {extra_note}" if extra_note else "")
                            ),
                            ingredient_ids=[iid],
                            item_keys=keys,
                            evidence=[ref_ev, *label_ev],
                        )
                    )
                elif total >= NEAR_UPPER_RATIO * kd.ul:
                    if kd.ul_basis == "supplement_only":
                        msg = f"{ing.name} 합계가 {josa(amt(ing, total), '으로/로')}, 보충제 기준 상한섭취량 {amt(ing, kd.ul)}에 가까워요."
                    else:
                        msg = (
                            f"{ing.name} 합계가 {josa(amt(ing, total), '으로/로')} 상한섭취량 {amt(ing, kd.ul)}의 {pct:.0f}%예요. "
                            "식사로 섭취하는 양까지 더하면 상한을 넘을 수 있어요."
                        )
                    upper.append(
                        _make_finding(
                            FindingType.NEAR_UPPER,
                            Severity.CAUTION,
                            f"{ing.name} 상한 근접: {pct:.0f}%",
                            msg + (dup_text if n_items > 1 else ""),
                            guidance="이 성분이 든 제품을 더 추가하지 않는 것이 좋아요.",
                            ingredient_ids=[iid],
                            item_keys=keys,
                            evidence=[ref_ev, *label_ev],
                        )
                    )
            else:
                rng = ing.functional_range
                if rng is None:
                    continue
                range_text = (
                    f"{amt(ing, rng.min)}" if rng.min == rng.max else f"{amt(ing, rng.min)}~{amt(ing, rng.max)}"
                )
                ref_ev = Evidence(source=FUNCTIONAL_SOURCE, detail=f"{ing.name} {range_text}/일")
                if total > rng.max:
                    pct = total / rng.max * 100
                    upper.append(
                        _make_finding(
                            FindingType.EXCESS,
                            ing.excess_severity,
                            f"{ing.name} 과다: 일일섭취량 기준의 {pct:.0f}%",
                            f"{ing.name} 하루 합계가 {josa(amt(ing, total), '으로/로')} 기능성 일일섭취량 기준({range_text})보다 많아요.{dup_text}",
                            guidance="더 많이 먹는다고 기능성이 커지지 않아요. 기준 범위 안으로 줄이세요.",
                            ingredient_ids=[iid],
                            item_keys=keys,
                            evidence=[ref_ev, *label_ev],
                        )
                    )
                elif total < rng.min:
                    below.append(
                        _make_finding(
                            FindingType.BELOW_EFFECTIVE,
                            Severity.CAUTION,
                            f"{ing.name} 부족: 기능성 기준량 미달",
                            f"{ing.name} 하루 합계가 {josa(amt(ing, total), '으로/로')} 기능성 일일섭취량 기준({range_text})보다 적어요. "
                            "표시된 기능성을 기대하기 어려울 수 있어요.",
                            guidance="기준량을 채우는 제품으로 바꾸는 것을 고려해 보세요.",
                            ingredient_ids=[iid],
                            item_keys=keys,
                            evidence=[ref_ev, *label_ev],
                        )
                    )
        return upper, below

    def _duplicates(self, totals: Totals, skip: set[str]) -> list[Finding]:
        """같은 성분을 2개 이상 제품에서 섭취하는 경우를 제품 묶음별로 모아 알린다."""
        groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for iid, contribs in totals.contributions.items():
            keys = tuple(sorted({c[0].key for c in contribs}))
            if len(keys) < 2 or iid in skip:
                continue
            groups[keys].append(iid)
        out = []
        cat = self.catalog
        for keys, iids in groups.items():
            items = {c[0].key: c[0] for iid in iids for c in totals.contributions[iid]}
            names = [items[k].product.name for k in keys]
            ings = [cat.ingredients[i] for i in iids]
            ing_names = "·".join(i.name for i in ings[:4]) + (f" 등 {len(ings)}개 성분" if len(ings) > 4 else "")
            lines = []
            for ing in ings:
                total = totals.amounts[ing.id]
                kd = cat.kdri_for(ing.id, self.ctx.profile.age, self.ctx.profile.sex) if ing.category == "nutrient" else None
                if kd and kd.ul:
                    lines.append(f"{ing.name} 합계 {amt(ing, total)} (상한의 {total / kd.ul * 100:.0f}%)")
                elif ing.functional_range:
                    lines.append(f"{ing.name} 합계 {amt(ing, total)} (기준 최대 {amt(ing, ing.functional_range.max)})")
                else:
                    lines.append(f"{ing.name} 합계 {amt(ing, total)}")
            ev = []
            for ing in ings:
                ev += _label_evidence(ing, totals.contributions[ing.id])
            out.append(
                _make_finding(
                    FindingType.DUPLICATE,
                    Severity.INFO,
                    f"성분 중복: {ing_names}",
                    f"{', '.join(names)}에 {josa(ing_names, '이/가')} 함께 들어 있어요. 현재 합계는 기준 안이에요: "
                    + "; ".join(lines)
                    + ".",
                    fid_extra="@".join(keys),
                    guidance="같은 목적의 제품이 겹친다면 하나로 줄여 비용을 아낄 수 있어요. 제품을 추가할 때는 합계를 다시 확인하세요.",
                    ingredient_ids=iids,
                    item_keys=list(keys),
                    evidence=ev,
                )
            )
        return out

    def _items_with(self, totals: Totals, iids: list[str]) -> list[ResolvedItem]:
        seen: dict[str, ResolvedItem] = {}
        for iid in iids:
            for item, _ in totals.contributions.get(iid, []):
                seen.setdefault(item.key, item)
        return list(seen.values())

    def _drug_rules(self, totals: Totals) -> list[Finding]:
        cat, ctx = self.catalog, self.ctx
        out = []
        for rule in cat.drug_rules:
            classes = [c for c in rule.drug_classes if c in ctx.drug_class_ids]
            if not classes:
                continue
            present = [i for i in rule.ingredients if totals.has(i, rule.min_amounts.get(i, 0))]
            if not present:
                continue
            drug_names = sorted({m.member_name for m in ctx.drug_matches if m.drug_class.id in classes})
            items = self._items_with(totals, present)
            ing_names = "·".join(cat.ingredients[i].name for i in present)
            ftype = FindingType.DRUG_TIMING if rule.kind == "timing" else FindingType.DRUG_INTERACTION
            title = f"{', '.join(drug_names)} ↔ {ing_names}"
            title += ": 복용 간격 필요" if rule.kind == "timing" else ": 병용 주의"
            out.append(
                _make_finding(
                    ftype,
                    rule.severity,
                    title,
                    f"{rule.effect} (해당 제품: {', '.join(i.product.name for i in items)})",
                    fid_extra=rule.id,
                    guidance=rule.guidance,
                    ingredient_ids=present,
                    item_keys=[i.key for i in items],
                    drug_names=drug_names,
                    evidence=[Evidence(source=RULE_SOURCE, detail=rule.basis)],
                )
            )
        return out

    def _pair_rules(self, totals: Totals) -> list[Finding]:
        cat = self.catalog
        out = []
        for rule in cat.pair_rules:
            a = [i for i in rule.ingredients_a if totals.has(i, rule.min_amounts.get(i, 0))]
            b = [i for i in rule.ingredients_b if totals.has(i, rule.min_amounts.get(i, 0))]
            if not a or not b:
                continue
            a_items = self._items_with(totals, a)
            b_items = self._items_with(totals, b)
            if rule.different_products:
                a_keys = {i.key for i in a_items}
                b_keys = {i.key for i in b_items}
                if not any(x != y for x in a_keys for y in b_keys):
                    continue
            items = {i.key: i for i in [*a_items, *b_items]}
            names = "·".join(cat.ingredients[i].name for i in [*a, *b])
            ftype = FindingType.SUPPLEMENT_TIMING if rule.kind == "timing" else FindingType.SUPPLEMENT_INTERACTION
            out.append(
                _make_finding(
                    ftype,
                    rule.severity,
                    f"{names}: " + ("섭취 간격 필요" if rule.kind == "timing" else "병용 주의"),
                    f"{rule.effect} (해당 제품: {', '.join(i.product.name for i in items.values())})",
                    fid_extra=rule.id,
                    guidance=rule.guidance,
                    ingredient_ids=[*a, *b],
                    item_keys=list(items),
                    evidence=[Evidence(source=RULE_SOURCE, detail=rule.basis)],
                )
            )
        return out

    def _group_rules(self, totals: Totals) -> list[Finding]:
        cat = self.catalog
        out = []
        for rule in cat.group_rules:
            present = [i for i in rule.ingredients if totals.has(i, rule.min_amounts.get(i, 0))]
            if len(present) < rule.min_count:
                continue
            items = self._items_with(totals, present)
            names = "·".join(cat.ingredients[i].name for i in present)
            out.append(
                _make_finding(
                    FindingType.SUPPLEMENT_INTERACTION,
                    rule.severity,
                    f"{names}: 함께 섭취 시 참고",
                    f"{rule.effect} (해당 제품: {', '.join(i.product.name for i in items)})",
                    fid_extra=rule.id,
                    guidance=rule.guidance,
                    ingredient_ids=present,
                    item_keys=[i.key for i in items],
                    evidence=[Evidence(source=RULE_SOURCE, detail=rule.basis)],
                )
            )
        return out

    def _profile_rules(self, totals: Totals) -> list[Finding]:
        cat, p = self.catalog, self.ctx.profile
        out = []
        for rule in cat.profile_rules:
            if rule.condition == "smoker" and not p.smoker:
                continue
            if rule.condition == "pregnant_or_lactating" and not p.pregnant_or_lactating:
                continue
            present = [i for i in rule.ingredients if totals.has(i, rule.min_amounts.get(i, 0))]
            if not present:
                continue
            items = self._items_with(totals, present)
            cond = "흡연" if rule.condition == "smoker" else "임신·수유"
            names = "·".join(cat.ingredients[i].name for i in present)
            out.append(
                _make_finding(
                    FindingType.PROFILE_CAUTION,
                    rule.severity,
                    f"{cond} 중 {names} 섭취 주의",
                    f"{rule.effect} (해당 제품: {', '.join(i.product.name for i in items)})",
                    fid_extra=rule.id,
                    guidance=rule.guidance,
                    ingredient_ids=present,
                    item_keys=[i.key for i in items],
                    evidence=[Evidence(source=RULE_SOURCE, detail=rule.basis)],
                )
            )
        return out

    def _goal_coverage(
        self, items: list[ResolvedItem], combo_tags: dict[str, TagInfo]
    ) -> tuple[list[GoalCoverage], list[Finding], set[str]]:
        cat, ctx = self.catalog, self.ctx
        item_tags = {it.key: cat.tags_for_amounts(it.amounts) for it in items}
        coverage: list[GoalCoverage] = []
        findings: list[Finding] = []
        gap_goals: set[str] = set()
        for goal in ctx.goals:
            core = ctx.core_tags(goal)
            value = max((combo_tags[t].strength for t in core if t in combo_tags), default=0.0)
            by_items = [
                it.product.name for it in items if any(item_tags[it.key].get(t, None) and item_tags[it.key][t].strength >= 0.3 for t in core)
            ]
            covered = value >= GOAL_COVERED_STRENGTH
            coverage.append(GoalCoverage(goal_id=goal.id, name=goal.name, covered=covered, coverage=round(value, 2), by_items=by_items))
            if covered:
                continue
            gap_goals.add(goal.id)
            tag_names = ", ".join(cat.tag_label(t) for t in core)
            if value > 0:
                msg = (
                    f"‘{goal.name}’ 관련 성분({tag_names})이 들어 있지만 기능성 표시 기준에 비해 양이 적어요"
                    f" (충족도 {value * 100:.0f}%, 해당 제품: {', '.join(by_items) or '없음'})."
                )
            else:
                msg = f"지금 조합에는 ‘{goal.name}’ 목적에 맞는 기능성({tag_names})을 가진 제품이 없어요."
            if goal.priority_nutrients:
                msg += " 핵심 영양소: " + ", ".join(cat.ingredients[i].name for i in goal.priority_nutrients) + "."
            findings.append(
                _make_finding(
                    FindingType.GOAL_GAP,
                    Severity.INFO,
                    f"‘{goal.name}’ 목적 보완 필요",
                    msg,
                    fid_extra=goal.id,
                    guidance="아래 추천 제품을 추가하거나, 이미 드시는 제품이 있다면 '현재 먹는 제품'으로 등록해 주세요.",
                    evidence=[Evidence(source=FUNCTIONAL_SOURCE, detail="기능성 표시 기준량(영양소는 영양성분 기준치의 30% 이상) 충족 여부로 판단")],
                )
            )
        return coverage, findings, gap_goals

    def _nutrient_low(self, totals: Totals, gap_goals: set[str]) -> list[Finding]:
        cat, ctx = self.catalog, self.ctx
        out = []
        for iid, reasons in ctx.priority_nutrients.items():
            # 목적 보완 필요(goal_gap)로 이미 안내한 경우는 중복 안내하지 않는다
            effective = [r for r in reasons if not (r.origin.startswith("goal:") and r.origin[5:] in gap_goals)]
            if not effective:
                continue
            kd = cat.kdri_for(iid, ctx.profile.age, ctx.profile.sex)
            if kd is None or not kd.value:
                continue
            total = totals.amounts.get(iid, 0.0)
            if total >= NUTRIENT_LOW_RATIO * kd.value:
                continue
            ing = cat.ingredients[iid]
            ref_label = "권장섭취량" if kd.kind == "rni" else "충분섭취량"
            out.append(
                _make_finding(
                    FindingType.NUTRIENT_LOW,
                    Severity.INFO,
                    f"{ing.name} 보충량이 적어요",
                    " ".join(dict.fromkeys(r.text for r in effective))
                    + f" 지금 조합의 {ing.name} 합계는 {josa(amt(ing, total), '으로/로')} {ref_label} {amt(ing, kd.value)}의 {total / kd.value * 100:.0f}%예요.",
                    fid_extra="priority",
                    guidance="식사로 충분히 섭취하고 있다면 추가하지 않아도 돼요. 걱정된다면 아래 제품을 고려하거나 전문가와 상담하세요.",
                    ingredient_ids=[iid],
                    evidence=[Evidence(source=kd.source, detail=f"{ing.name} {ref_label} {amt(ing, kd.value)}/일")],
                )
            )
        return out

    def _unknowns(self, items: list[ResolvedItem]) -> list[Finding]:
        out = []
        for name in self.ctx.unknown_drugs:
            out.append(
                _make_finding(
                    FindingType.UNKNOWN_DRUG,
                    Severity.CAUTION,
                    f"‘{name}’: 자동 점검 목록에 없는 약",
                    f"‘{name}’과(와) 건강기능식품의 병용 여부는 자동으로 확인하지 못했어요.",
                    fid_extra=name,
                    guidance="약사에게 지금 조합을 보여 주고 함께 먹어도 되는지 확인하세요. 약 성분명(예: 아토르바스타틴)으로 입력하면 인식률이 높아져요.",
                )
            )
        for name in self.ctx.unknown_allergies:
            out.append(
                _make_finding(
                    FindingType.UNKNOWN_INGREDIENT,
                    Severity.CAUTION,
                    f"알레르기 ‘{name}’: 자동 확인 불가",
                    f"‘{name}’ 알레르기는 제품 정보와 자동으로 대조하지 못했어요.",
                    fid_extra=f"allergy:{name}",
                    guidance="각 제품의 원재료명을 직접 확인하세요.",
                )
            )
        for item in items:
            for name in item.unknown_ingredients:
                out.append(
                    _make_finding(
                        FindingType.UNKNOWN_INGREDIENT,
                        Severity.CAUTION,
                        f"{item.product.name}: 인식하지 못한 성분",
                        f"‘{name}’은(는) 성분 DB에 없거나 단위를 환산할 수 없어 합계에서 빠졌어요.",
                        fid_extra=f"{item.key}:{name}",
                        guidance="제품 표시사항의 성분명과 단위(mg, μg, IU 등)를 확인해 다시 입력하거나 약사에게 확인하세요.",
                        item_keys=[item.key],
                    )
                )
        return out

    def _distribution(self, totals: Totals) -> list[DistributionEntry]:
        cat, ctx = self.catalog, self.ctx
        entries = []
        for iid, total in totals.amounts.items():
            ing = cat.ingredients[iid]
            sources = [
                ProductContribution(
                    item_key=it.key, product_id=it.product.id, product_name=it.product.name, amount=round(v, 4),
                    calculation=it.calculation(iid, short_unit(ing)),
                )
                for it, v in totals.contributions[iid]
            ]
            pct_ref = pct_ul = None
            status = "no_reference"
            label = None
            if ing.category == "nutrient":
                kd = cat.kdri_for(iid, ctx.profile.age, ctx.profile.sex)
                if kd and kd.kind in ("rni", "ai"):
                    ref = ReferenceValues(
                        kind=kd.kind,
                        label="권장섭취량" if kd.kind == "rni" else "충분섭취량",
                        value=kd.value,
                        ul=kd.ul,
                        ul_basis=kd.ul_basis,
                        source=kd.source,
                    )
                    pct_ref = total / kd.value * 100 if kd.value else None
                elif kd:
                    ref = ReferenceValues(kind="none", label="상한섭취량만 설정", ul=kd.ul, source=kd.source)
                else:
                    ref = ReferenceValues(kind="none", label="기준 없음", source="-")
                if kd and kd.ul:
                    pct_ul = total / kd.ul * 100
                if pct_ul is not None and pct_ul > 100:
                    status = "excess"
                elif pct_ul is not None and pct_ul >= NEAR_UPPER_RATIO * 100:
                    status = "near_upper"
                elif pct_ref is not None and pct_ref < NUTRIENT_LOW_RATIO * 100:
                    status = "low"
                elif pct_ref is not None or pct_ul is not None:
                    status = "adequate"
                    if pct_ul is None and pct_ref is not None and pct_ref > 300:
                        label = "상한 미설정"
            else:
                rng = ing.functional_range
                if rng:
                    ref = ReferenceValues(
                        kind="functional_range", label="기능성 일일섭취량", min=rng.min, max=rng.max, source=FUNCTIONAL_SOURCE
                    )
                    pct_ref = total / rng.min * 100
                    pct_ul = total / rng.max * 100
                    status = "excess" if total > rng.max else "low" if total < rng.min else "adequate"
                else:
                    ref = ReferenceValues(kind="none", label="기준 확인 필요", source=FUNCTIONAL_SOURCE)
            entries.append(
                DistributionEntry(
                    ingredient_id=iid,
                    name=ing.name,
                    category=ing.category,
                    unit=short_unit(ing),
                    total=round(total, 4),
                    total_iu=round(total / ing.iu_factor, 1) if ing.iu_factor else None,
                    reference=ref,
                    percent_of_reference=round(pct_ref, 1) if pct_ref is not None else None,
                    percent_of_upper=round(pct_ul, 1) if pct_ul is not None else None,
                    status=status,
                    status_label=label or _STATUS_LABEL[status],
                    is_duplicate=len({s.item_key for s in sources}) > 1,
                    sources=sources,
                )
            )
        entries.sort(key=lambda e: (_STATUS_ORDER[e.status], e.category != "functional", e.name))
        return entries

    def _disclaimers(self) -> list[str]:
        p = self.ctx.profile
        out = [
            "이 결과는 제품 표시 정보와 한국인 영양소 섭취기준을 바탕으로 계산한 참고 정보이며, 질병의 진단·치료·예방을 위한 것이 아니에요.",
            "식사로 섭취하는 영양소는 합계에 포함되지 않았어요.",
        ]
        if p.medications:
            out.append("복용 중인 약이 있다면 새 제품을 시작하기 전에 의사·약사와 상담하세요.")
        if p.pregnant_or_lactating:
            out.append("임신·수유 중에는 모든 건강기능식품을 의사·약사와 상담한 뒤 드세요.")
        return out


_SAFETY_TYPES = {FindingType.DRUG_INTERACTION, FindingType.PROFILE_CAUTION, FindingType.ALLERGY}
TIMING_TYPES = {FindingType.DRUG_TIMING, FindingType.SUPPLEMENT_TIMING}


def blocks_new_product(f: Finding) -> bool:
    """엔진이 '새로 제안하는' 제품이 만들어서는 안 되는 점검 결과.

    - 조정 권장(warning) 이상은 모두
    - 복용 약·개인 상태·알레르기 관련 항목은 주의(caution) 단계부터
    사용자가 직접 추가하는 것은 막지 않고, 점검 결과로 알려 준다.
    """
    if SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.WARNING]:
        return True
    return f.type in _SAFETY_TYPES and SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.CAUTION]


def summarize(findings: list[Finding], items: list[ResolvedItem]) -> ReportSummary:
    by_cat: dict[str, int] = {k: 0 for k in CATEGORY_LABEL}
    by_sev: dict[str, int] = {s.value: 0 for s in Severity}
    for f in findings:
        by_cat[f.category] += 1
        by_sev[f.severity.value] += 1
    if by_sev["avoid"]:
        overall, label = "avoid", "피해야 할 조합이 있어요"
    elif by_sev["warning"]:
        overall, label = "adjust", "조정이 필요한 항목이 있어요"
    elif by_sev["caution"]:
        overall, label = "review", "주의해서 드시면 돼요"
    else:
        overall, label = "ok", "특별히 조정할 항목이 없어요"
    return ReportSummary(
        overall=overall,
        overall_label=label,
        counts_by_category=by_cat,
        counts_by_severity=by_sev,
        total_price_per_day=sum(i.price_per_day for i in items),
    )
