"""목적 기반 조합 추천 (1단계: 규칙 기반).

동작 방식
1. 프로필에서 기능성 태그별 필요도(demand)를 만든다 (목적 순서·가족력 반영).
2. 이미 먹고 있는 제품이 채우는 태그는 '충족'으로 본다.
3. 플랫폼 제품마다 '아직 채워지지 않은 필요도'를 얼마나 채우는지(gain) 계산한다.
4. 후보를 조합에 넣어 실제로 성분 점검을 돌려 보고,
   - 조정 권장(warning) 이상 문제가 새로 생기면 제외하고 이유를 기록한다.
   - 주의(caution)·참고(info)가 생기면 감점한다.
5. 점수가 가장 높은 제품을 하나씩 추가한다(최대 개수·예산·최소 효용 기준까지).

추천 이유는 식약처 기능성 표시 문구로만 설명한다(질병 예방·치료 표현 사용 안 함).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, Product, TagInfo
from .checker import TIMING_TYPES, Checker, blocks_new_product
from .context import GOAL_COVERED_STRENGTH, ProfileContext
from .items import ResolvedItem, platform_item, with_unique_key
from .models import (
    AlternativeProduct,
    ExcludedProduct,
    Finding,
    FindingType,
    Reason,
    RecommendedProduct,
    Severity,
)

MIN_GAIN = 0.45           # 이보다 효용이 작으면 추가하지 않음 (조합을 가볍게 유지)
PRICE_PENALTY = 0.0001    # 하루 1,000원당 0.1점 감점
CAUTION_PENALTY = 0.2
TIMING_PENALTY = 0.05
COVERED_GOAL_FACTOR = 0.4  # 목적의 핵심 기능성이 채워지면 보조 기능성의 필요도를 낮춤     # 시간 간격으로 해결되는 항목은 작게 감점
INFO_PENALTY = 0.03
_IGNORED_NEW = {FindingType.GOAL_GAP, FindingType.NUTRIENT_LOW}


@dataclass
class _Scored:
    product: Product
    item: ResolvedItem
    gain: float
    score: float
    tags: dict[str, TagInfo]
    increments: dict[str, float]
    new_findings: list[Finding]
    finding_ids: set[str]


@dataclass
class RecommendationResult:
    recommended: list[RecommendedProduct]
    combination: list[ResolvedItem]
    excluded: list[ExcludedProduct]
    notes: list[str] = field(default_factory=list)


class Recommender:
    def __init__(self, catalog: Catalog, ctx: ProfileContext, checker: Checker):
        self.catalog = catalog
        self.ctx = ctx
        self.checker = checker

    # ------------------------------------------------------------------
    def _coverage(self, items: list[ResolvedItem]) -> dict[str, float]:
        covered: dict[str, float] = {}
        for it in items:
            for tag, info in self.catalog.tags_for_amounts(it.amounts).items():
                covered[tag] = max(covered.get(tag, 0.0), info.strength)
        return covered

    def _effective_demand(self, covered: dict[str, float]) -> dict[str, float]:
        """목적별 필요도. 핵심 기능성이 이미 채워진 목적은 나머지 보조 기능성의 필요도를 낮춘다."""
        eff: dict[str, float] = {}
        weighted = [(self.catalog.goals[g], w) for g, w in self.ctx.goal_weights.items()]
        weighted += [(self.catalog.goals[g], w) for g, w in self.ctx.family_goal_weights.items()]
        for goal, weight in weighted:
            core = self.ctx.core_tags(goal)
            done = max((covered.get(t, 0.0) for t in core), default=0.0) >= GOAL_COVERED_STRENGTH
            for tag, tw in goal.tags.items():
                d = weight * tw * (COVERED_GOAL_FACTOR if done and covered.get(tag, 0.0) < GOAL_COVERED_STRENGTH else 1.0)
                eff[tag] = max(eff.get(tag, 0.0), round(d, 3))
        return eff

    def _gain(self, tags: dict[str, TagInfo], covered: dict[str, float]) -> tuple[float, dict[str, float]]:
        gain = 0.0
        inc: dict[str, float] = {}
        for tag, demand in self._effective_demand(covered).items():
            s = tags[tag].strength if tag in tags else 0.0
            delta = s - covered.get(tag, 0.0)
            if delta > 0:
                gain += demand * delta
                inc[tag] = delta
        return round(gain, 4), inc

    def _blocking(self, findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if blocks_new_product(f)]

    def _penalty(self, product: Product, new: list[Finding]) -> float:
        p = product.price_per_day * PRICE_PENALTY
        for f in new:
            if f.type in TIMING_TYPES:
                p += TIMING_PENALTY
            elif f.severity == Severity.CAUTION:
                p += CAUTION_PENALTY
            elif f.severity == Severity.INFO:
                p += INFO_PENALTY
        return p

    # ------------------------------------------------------------------
    def recommend(
        self,
        current: list[ResolvedItem],
        *,
        max_new: int = 3,
        max_total: int = 6,
        budget_per_day: int | None = None,
        exclude_ids: list[str] | None = None,
    ) -> RecommendationResult:
        cat = self.catalog
        combo = list(current)
        covered = self._coverage(combo)
        base_findings, _, _ = self.checker.evaluate(combo)
        base_ids = {f.id for f in base_findings}
        in_combo = {it.product.id for it in combo}
        exclude = set(exclude_ids or [])
        pool = [p for p in cat.platform_products() if p.id not in in_combo and p.id not in exclude]
        excluded: dict[str, str] = {}
        chosen: list[_Scored] = []
        spent = 0
        slots = max(0, min(max_new, max_total - len(combo)))
        notes: list[str] = []
        drug_blocked = False

        for _ in range(slots):
            best: _Scored | None = None
            for p in pool:
                tags = cat.tags_for_amounts(cat.product_amounts(p))
                gain, inc = self._gain(tags, covered)
                if gain < MIN_GAIN:
                    continue
                if budget_per_day is not None and spent + p.price_per_day > budget_per_day:
                    excluded.setdefault(p.id, f"하루 예산({budget_per_day:,}원)을 넘어요.")
                    continue
                item = with_unique_key(platform_item(cat, p.id), combo)
                findings, _, _ = self.checker.evaluate([*combo, item])
                new = [f for f in findings if f.id not in base_ids and f.type not in _IGNORED_NEW]
                blocking = self._blocking(new)
                if blocking:
                    b = blocking[0]
                    excluded[p.id] = f"{b.title} — {b.message.split(' (해당 제품')[0]}"
                    if b.type == FindingType.DRUG_INTERACTION:
                        drug_blocked = True
                    continue
                score = gain - self._penalty(p, new)
                if best is None or score > best.score:
                    best = _Scored(p, item, gain, score, tags, inc, new, {f.id for f in findings})
            if best is None or best.score < MIN_GAIN * 0.5:
                break
            chosen.append(best)
            combo.append(best.item)
            spent += best.product.price_per_day
            for tag, delta in best.increments.items():
                covered[tag] = covered.get(tag, 0.0) + delta
            base_ids = best.finding_ids
            pool = [p for p in pool if p.id != best.product.id]
            excluded.pop(best.product.id, None)

        chosen_ids = {c.product.id for c in chosen}
        recommended = [self._describe(c, combo, chosen_ids, excluded) for c in chosen]

        if not chosen:
            if current and all(g.covered for g in self.checker.evaluate(current)[1]):
                notes.append("지금 드시는 제품으로 선택한 목적이 이미 채워져 있어 새로 추가할 제품이 없어요.")
            elif slots == 0:
                notes.append("제품 수 한도에 도달해 새 제품을 추천하지 않았어요.")
            else:
                notes.append("조건에 맞는 제품을 찾지 못했어요. 제외된 제품의 이유를 확인해 주세요.")

        if drug_blocked:
            notes.append(
                "복용 중인 약과 함께 먹을 때 주의가 필요한 제품은 자동 추천에서 뺐어요. "
                "의사·약사와 상담한 뒤 직접 추가할 수 있어요."
            )
        # 목적과 관련 있는 제품만 '제외 이유'로 보여 준다
        excluded_list = [
            ExcludedProduct(product_id=pid, name=cat.products[pid].name, reason=reason)
            for pid, reason in excluded.items()
            if pid not in chosen_ids
        ]
        return RecommendationResult(recommended, combo, excluded_list, notes)

    # ------------------------------------------------------------------
    def _goal_label_for_tag(self, tag: str) -> list[tuple[str | None, str]]:
        """태그가 어떤 목적에서 왔는지 (goal_id, 표시 문구)."""
        out: list[tuple[str | None, str]] = []
        for goal in self.ctx.goals:
            if goal.tags.get(tag, 0) > 0:
                out.append((goal.id, f"‘{goal.name}’ 목적"))
        for gid in self.ctx.family_goal_weights:
            goal = self.catalog.goals[gid]
            if goal.tags.get(tag, 0) > 0:
                origins = [o for o in self.ctx.demand_origin.get(tag, []) if o.startswith("가족력")]
                label = "·".join(origins) or "가족력"
                out.append((gid, f"{label} 고려(‘{goal.name}’)"))
        return out

    def _describe(
        self, scored: _Scored, combo: list[ResolvedItem], chosen_ids: set[str], excluded: dict[str, str]
    ) -> RecommendedProduct:
        reasons: list[Reason] = []
        seen: set[str] = set()
        # 기여도가 큰 태그부터 설명
        ordered = sorted(scored.increments.items(), key=lambda kv: -self.ctx.demand.get(kv[0], 0) * kv[1])
        for tag, _delta in ordered:
            for goal_id, label in self._goal_label_for_tag(tag):
                claims = list(dict.fromkeys(f"{n}: {t}" for n, t in scored.tags[tag].claims))
                text = f"{label} — {' / '.join(claims[:2])}"
                if text not in seen:
                    seen.add(text)
                    reasons.append(Reason(goal_id=goal_id, text=text))
                break
        considerations = []
        for f in scored.new_findings:
            if f.severity in (Severity.CAUTION, Severity.INFO) and f.type != FindingType.DUPLICATE:
                considerations.append(f"{f.title}: {f.guidance or f.message}")
            elif f.type == FindingType.DUPLICATE:
                considerations.append(f"{f.title} (합계는 기준 안이에요)")

        alternatives = self._alternatives(scored, combo, chosen_ids, excluded)
        return RecommendedProduct(
            item_key=scored.item.key,
            product_id=scored.product.id,
            name=scored.product.name,
            price_per_day=scored.product.price_per_day,
            reasons=reasons,
            considerations=considerations,
            alternatives=alternatives,
        )

    def _alternatives(
        self, scored: _Scored, combo: list[ResolvedItem], chosen_ids: set[str], excluded: dict[str, str], limit: int = 3
    ) -> list[AlternativeProduct]:
        """같은 목적을 채울 수 있는 다른 제품 (사용자가 직접 바꿀 때 쓰는 목록)."""
        cat = self.catalog
        main_tags = [t for t, d in scored.increments.items() if d >= 0.5 and self.ctx.demand.get(t, 0) >= 0.5]
        if not main_tags:
            main_tags = list(scored.increments)
        others = [it for it in combo if it.key != scored.item.key]
        base_findings, _, _ = self.checker.evaluate(others)
        base_ids = {f.id for f in base_findings}
        candidates = []
        in_combo = {it.product.id for it in combo}
        for p in cat.platform_products():
            if p.id in in_combo or p.id in chosen_ids or p.id in excluded:
                continue
            tags = cat.tags_for_amounts(cat.product_amounts(p))
            shared = [t for t in main_tags if t in tags and tags[t].strength >= 0.5]
            if not shared:
                continue
            item = with_unique_key(platform_item(cat, p.id), others)
            findings, _, _ = self.checker.evaluate([*others, item])
            new = [f for f in findings if f.id not in base_ids and f.type not in _IGNORED_NEW]
            if self._blocking(new):
                continue
            claim = tags[shared[0]].claims[0]
            diff = p.price_per_day - scored.product.price_per_day
            price_note = f"하루 {abs(diff):,}원 {'저렴' if diff < 0 else '추가'}" if diff else "가격 같음"
            reason = f"{claim[0]}: {claim[1]} ({price_note})"
            candidates.append((-len(shared), p.price_per_day, AlternativeProduct(
                product_id=p.id, name=p.name, price_per_day=p.price_per_day, reason=reason)))
        candidates.sort(key=lambda c: (c[0], c[1]))
        return [c[2] for c in candidates[:limit]]

