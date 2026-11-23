"""하루 섭취 시간표 제안.

제품별 권장 시간(아침/저녁)을 기본으로 하고, 시간 간격이 필요한 규칙
(예: 갑상선호르몬제와 칼슘, 칼슘과 철)이 겹치지 않도록 시간대를 옮긴다.
앱의 섭취 알림·사전 주문 화면에서 그대로 쓸 수 있다.
"""

from __future__ import annotations

from collections import defaultdict

from .catalog import Catalog
from .context import ProfileContext
from .items import ResolvedItem
from .models import Schedule, ScheduleSlot
from .text import josa

SLOT_LABEL = {"morning": "아침", "lunch": "점심", "evening": "저녁"}
SLOT_ORDER = ["morning", "lunch", "evening"]
_MOVE_PREFERENCE = ["evening", "lunch", "morning"]


def _has_any(item: ResolvedItem, ingredient_ids: list[str], min_amounts: dict[str, float]) -> bool:
    return any(item.amounts.get(i, 0) > 0 and item.amounts.get(i, 0) >= min_amounts.get(i, 0) for i in ingredient_ids)


def build_schedule(catalog: Catalog, ctx: ProfileContext, items: list[ResolvedItem]) -> Schedule:
    slot_of: dict[str, str] = {}
    for item in items:
        slot_of[item.key] = "evening" if item.product.timing == "evening" else "morning"

    notes: list[str] = []
    forbidden: dict[str, set[str]] = defaultdict(set)

    # 1) 아침 공복 복용이 일반적인 약과 흡수를 방해하는 성분은 다른 시간대로
    for rule in catalog.drug_rules:
        if rule.kind != "timing":
            continue
        for cid in rule.drug_classes:
            if cid not in ctx.drug_class_ids:
                continue
            usual = catalog.drug_classes[cid].usual_slot
            names = sorted({m.member_name for m in ctx.drug_matches if m.drug_class.id == cid})
            hit_items = [it for it in items if _has_any(it, rule.ingredients, rule.min_amounts)]
            if not hit_items:
                continue
            if usual:
                for it in hit_items:
                    forbidden[it.key].add(usual)
                notes.append(
                    f"{', '.join(names)}: {SLOT_LABEL[usual]}에 복용하는 경우가 많아, "
                    f"{josa(', '.join(i.product.name for i in hit_items), '은/는')} "
                    f"다른 시간대에 배치했어요. ({rule.guidance})"
                )
            else:
                notes.append(f"{', '.join(names)}: {rule.guidance}")

    for key, fb in forbidden.items():
        if slot_of[key] in fb:
            for s in _MOVE_PREFERENCE:
                if s not in fb:
                    slot_of[key] = s
                    break

    # 2) 성분끼리 시간 간격이 필요한 경우 (예: 칼슘 ↔ 철)
    for rule in catalog.pair_rules:
        if rule.kind != "timing":
            continue
        a_items = [it for it in items if _has_any(it, rule.ingredients_a, rule.min_amounts)]
        b_items = [it for it in items if _has_any(it, rule.ingredients_b, rule.min_amounts)]
        both = {it.key for it in a_items} & {it.key for it in b_items}
        a_items = [it for it in a_items if it.key not in both]
        b_items = [it for it in b_items if it.key not in both]
        if not a_items or not b_items:
            continue
        b_slots = {slot_of[it.key] for it in b_items}
        moved: list[str] = []
        for it in a_items:
            if slot_of[it.key] not in b_slots:
                continue
            for s in _MOVE_PREFERENCE:
                if s not in b_slots and s not in forbidden[it.key]:
                    slot_of[it.key] = s
                    moved.append(it.product.name)
                    break
        if moved:
            notes.append(f"{', '.join(moved)}: {rule.guidance}")
        elif any(slot_of[a.key] in b_slots for a in a_items):
            notes.append(rule.guidance)

    slots: list[ScheduleSlot] = []
    for s in SLOT_ORDER:
        members = [it for it in items if slot_of[it.key] == s]
        if members:
            slots.append(
                ScheduleSlot(
                    slot=s,
                    label=SLOT_LABEL[s],
                    item_keys=[m.key for m in members],
                    product_names=[m.product.name for m in members],
                )
            )
    if not notes and items:
        notes.append("제품에 표시된 섭취 방법을 따르고, 가능하면 식후에 드세요.")
    return Schedule(slots=slots, notes=notes)

