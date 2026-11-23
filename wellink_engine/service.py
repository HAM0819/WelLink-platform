"""엔진 진입점: 추천(recommend)과 점검(check).

앱 흐름 (S파트 이용 흐름 ③ 구성 선택 → ④ 성분 점검)
  1. recommend(): 프로필 + 현재 먹는 제품 → 추천 조합 + 점검 결과 + 자동 수정안
  2. 사용자가 조합을 수정(빼기/바꾸기/고정)
  3. check(): 수정한 조합 → 점검 결과 + 항목별 수정안 + 자동 수정안
"""

from __future__ import annotations

from typing import Optional

from .catalog import Catalog, get_catalog
from .checker import Checker
from .context import build_context
from .items import resolve_items
from .models import (
    SEVERITY_RANK,
    CheckRequest,
    CheckResponse,
    FindingType,
    RecommendRequest,
    RecommendResponse,
    Revision,
    Severity,
)
from .recommender import Recommender
from .reviser import Reviser


def _needs_revision(report) -> bool:
    return any(
        SEVERITY_RANK[f.severity] >= SEVERITY_RANK[Severity.WARNING]
        or f.type in (FindingType.EXCESS, FindingType.GOAL_GAP, FindingType.BELOW_EFFECTIVE)
        for f in report.findings
    )


def check(request: CheckRequest, catalog: Optional[Catalog] = None) -> CheckResponse:
    cat = catalog or get_catalog()
    ctx = build_context(cat, request.profile)
    checker = Checker(cat, ctx)
    items = resolve_items(cat, request.items)
    report = checker.check(items)
    reviser = Reviser(cat, ctx, checker)
    reviser.attach_suggestions(report.findings, items)
    revision: Optional[Revision] = reviser.plan(items) if _needs_revision(report) else None
    return CheckResponse(report=report, revision=revision)


def recommend(request: RecommendRequest, catalog: Optional[Catalog] = None) -> RecommendResponse:
    cat = catalog or get_catalog()
    ctx = build_context(cat, request.profile)
    checker = Checker(cat, ctx)
    current = resolve_items(cat, request.current_products)
    result = Recommender(cat, ctx, checker).recommend(
        current,
        max_new=request.max_products,
        max_total=request.max_total_products,
        budget_per_day=request.budget_per_day,
        exclude_ids=request.exclude_product_ids,
    )
    combo = result.combination
    report = checker.check(combo)
    reviser = Reviser(cat, ctx, checker)
    reviser.attach_suggestions(report.findings, combo)
    revision = reviser.plan(combo) if _needs_revision(report) else None
    return RecommendResponse(
        recommended=result.recommended,
        combination=[i.input for i in combo],
        excluded=result.excluded,
        profile_notes=[*ctx.notes, *result.notes],
        report=report,
        revision=revision,
    )
