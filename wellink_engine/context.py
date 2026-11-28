"""사용자 프로필을 엔진이 쓰기 좋은 형태로 해석한다.

- 건강 관리 목적 → 기능성 태그별 '필요도(demand)'
- 가족력 → 관련 목적을 낮은 가중치로 추가, 우선 확인 영양소 추가
- 연령·성별 → 생애주기별 우선 확인 영양소
- 복용 약 → 약효군 인식, 장기 복용 시 부족해질 수 있는 영양소
- 알레르기 → 알레르기 ID
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, DrugMatch, Goal
from .models import Profile

# 사용자가 고른 목적의 순서별 가중치 (첫 번째 목적이 가장 중요)
GOAL_WEIGHTS = [1.0, 0.85, 0.7, 0.6, 0.5]
# 가족력으로 추가되는 목적의 기본 가중치 (사용자가 직접 고른 목적보다 낮게)
FAMILY_BASE_WEIGHT = 0.8
# 목적 충족으로 인정하는 태그 가중치·강도 기준
GOAL_CORE_TAG_WEIGHT = 0.8
GOAL_COVERED_STRENGTH = 0.8


class InputError(ValueError):
    """요청 값이 카탈로그와 맞지 않을 때 (API에서 422로 응답)."""


@dataclass
class PriorityReason:
    origin: str  # goal:<id> / family:<id> / life:<id> / drug:<class>
    text: str


@dataclass
class ProfileContext:
    profile: Profile
    goals: list[Goal]
    goal_weights: dict[str, float]
    family_goal_weights: dict[str, float]
    demand: dict[str, float]
    demand_origin: dict[str, list[str]]  # tag → 목적 이름 목록 (추천 이유 설명용)
    drug_matches: list[DrugMatch]
    drug_class_ids: set[str]
    unknown_drugs: list[str]
    allergen_ids: set[str]
    unknown_allergies: list[str]
    priority_nutrients: dict[str, list[PriorityReason]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def core_tags(self, goal: Goal) -> list[str]:
        return [t for t, w in goal.tags.items() if w >= GOAL_CORE_TAG_WEIGHT]


def build_context(catalog: Catalog, profile: Profile) -> ProfileContext:
    goals: list[Goal] = []
    goal_weights: dict[str, float] = {}
    for i, gid in enumerate(dict.fromkeys(profile.goals)):
        goal = catalog.goals.get(gid)
        if goal is None:
            raise InputError(f"알 수 없는 건강 관리 목적이에요: '{gid}'")
        goals.append(goal)
        goal_weights[gid] = GOAL_WEIGHTS[min(i, len(GOAL_WEIGHTS) - 1)]

    demand: dict[str, float] = {}
    demand_origin: dict[str, list[str]] = {}

    def add_demand(goal: Goal, weight: float, label: str) -> None:
        for tag, tw in goal.tags.items():
            value = round(weight * tw, 3)
            if value > demand.get(tag, 0.0):
                demand[tag] = value
            demand_origin.setdefault(tag, [])
            if label not in demand_origin[tag]:
                demand_origin[tag].append(label)

    for goal in goals:
        add_demand(goal, goal_weights[goal.id], f"‘{goal.name}’ 목적")

    notes: list[str] = []
    priority: dict[str, list[PriorityReason]] = {}

    def add_priority(iid: str, origin: str, text: str) -> None:
        reasons = priority.setdefault(iid, [])
        if all(r.origin != origin for r in reasons):
            reasons.append(PriorityReason(origin, text))

    for goal in goals:
        for iid in goal.priority_nutrients:
            add_priority(iid, f"goal:{goal.id}", f"‘{goal.name}’ 목적의 핵심 영양소예요.")

    family_goal_weights: dict[str, float] = {}
    for fid in dict.fromkeys(profile.family_history):
        fh = catalog.family_history.get(fid)
        if fh is None:
            raise InputError(f"알 수 없는 가족력 항목이에요: '{fid}'")
        notes.append(f"가족력({fh.name}): {fh.note}")
        for gid, w in fh.related_goals.items():
            if gid in goal_weights:
                continue
            weight = round(FAMILY_BASE_WEIGHT * w, 3)
            if weight > family_goal_weights.get(gid, 0.0):
                family_goal_weights[gid] = weight
            add_demand(catalog.goals[gid], weight, f"가족력({fh.name})")
        for iid in fh.priority_nutrients:
            add_priority(iid, f"family:{fh.id}", f"가족력({fh.name})을 고려해 확인해요.")

    for ls in catalog.life_stages:
        w = ls.when
        if "sex" in w and w["sex"] != profile.sex:
            continue
        if "age_min" in w and profile.age < int(w["age_min"]):
            continue
        if "age_max" in w and profile.age > int(w["age_max"]):
            continue
        notes.append(ls.note)
        for iid in ls.priority_nutrients:
            add_priority(iid, f"life:{ls.id}", ls.note)

    drug_matches: list[DrugMatch] = []
    unknown_drugs: list[str] = []
    for text in profile.medications:
        if not text.strip():
            continue
        found = catalog.match_drugs(text)
        if found:
            drug_matches.extend(found)
        else:
            unknown_drugs.append(text.strip())
    drug_class_ids = {m.drug_class.id for m in drug_matches}
    for rule in catalog.depletion_rules:
        if rule.drug_class in drug_class_ids:
            notes.append(rule.note)
            for iid in rule.nutrients:
                add_priority(iid, f"drug:{rule.drug_class}", rule.note)

    allergen_ids: set[str] = set()
    unknown_allergies: list[str] = []
    for text in profile.allergies:
        if not text.strip():
            continue
        aid = catalog.match_allergen(text)
        if aid:
            allergen_ids.add(aid)
        else:
            unknown_allergies.append(text.strip())

    return ProfileContext(
        profile=profile,
        goals=goals,
        goal_weights=goal_weights,
        family_goal_weights=family_goal_weights,
        demand=demand,
        demand_origin=demand_origin,
        drug_matches=drug_matches,
        drug_class_ids=drug_class_ids,
        unknown_drugs=unknown_drugs,
        allergen_ids=allergen_ids,
        unknown_allergies=unknown_allergies,
        priority_nutrients=priority,
        notes=notes,
    )
