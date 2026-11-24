"""WelLink 추천·성분 점검 API (FastAPI).

실행: uvicorn wellink_engine.api:app --reload
문서: http://localhost:8000/docs (앱 개발사는 이 OpenAPI 스펙으로 연동)
데모: http://localhost:8000/ (모바일 화면 크기의 웹 시연 페이지)

개인정보: 이 서버는 요청으로 받은 건강 정보를 저장하지 않는다(계산 후 폐기).
실서비스에서는 민감정보 별도 동의, 전송 구간 암호화(HTTPS), 접근 로그 분리를 적용한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import service
from .catalog import get_catalog
from .context import InputError
from .models import CheckRequest, CheckResponse, RecommendRequest, RecommendResponse

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="WelLink 추천·성분 점검 API",
    version="0.1.0",
    description=(
        "건강 프로필(나이·성별·목적·가족력·복용 약)을 바탕으로 플랫폼 건강기능식품 조합을 추천하고, "
        "성분 합산으로 과다·중복·부족·병용 주의를 점검해 수정안을 제시합니다. "
        "모든 점검은 규칙 기반이며 근거(기준치 출처·제품 표시 함량)를 함께 반환합니다."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health", tags=["system"])
def health() -> dict:
    cat = get_catalog()
    return {"status": "ok", "data_version": cat.versions}


@app.get("/v1/catalog/goals", tags=["catalog"], summary="선택 가능한 건강 관리 목적")
def list_goals(
    tier: Optional[Literal["core", "extended"]] = Query(
        None, description="core: 초기 앱에 노출하는 일반 목적 / extended: 고령층·특정층 확장 목적 / 생략: 전체"),
) -> list[dict]:
    cat = get_catalog()
    return [
        {"id": g.id, "name": g.name, "tier": g.tier, "description": g.description}
        for g in cat.goals.values()
        if tier is None or g.tier == tier
    ]


@app.get("/v1/catalog/family-history", tags=["catalog"], summary="선택 가능한 가족력 항목")
def list_family_history() -> list[dict]:
    cat = get_catalog()
    return [{"id": f.id, "name": f.name} for f in cat.family_history.values()]


@app.get("/v1/catalog/products", tags=["catalog"], summary="제품 목록")
def list_products(
    platform_only: bool = Query(False, description="true면 플랫폼 취급 제품만"),
) -> list[dict]:
    cat = get_catalog()
    out = []
    for p in cat.products.values():
        if platform_only and not p.sold_on_platform:
            continue
        out.append({
            "id": p.id,
            "name": p.name,
            "brand": p.brand,
            "sold_on_platform": p.sold_on_platform,
            "form": p.form,
            "package": p.package,
            "daily_dose_text": p.daily_dose_text,
            "price_per_day": p.price_per_day,
            "functions": cat.product_functions(p),
            "ingredients": [
                {"id": i.id, "name": cat.ingredients[i.id].name, "amount": i.amount,
                 "unit": cat.ingredients[i.id].unit_label}
                for i in p.ingredients
            ],
            "allergens": [cat.allergens[a].name for a in p.allergens],
            "cautions": p.cautions,
        })
    return out


@app.get("/v1/catalog/ingredients", tags=["catalog"], summary="성분 검색 (타사 제품 직접 입력용)")
def search_ingredients(q: str = Query("", description="성분명 일부")) -> list[dict]:
    from .text import normalize

    cat = get_catalog()
    nq = normalize(q)
    out = []
    for ing in cat.ingredients.values():
        names = [ing.name, *ing.aliases]
        if not nq or any(nq in normalize(n) for n in names):
            out.append({"id": ing.id, "name": ing.name, "unit": ing.unit_label, "category": ing.category,
                        "accepts_iu": ing.iu_factor is not None})
    return out


@app.get("/v1/catalog/drugs", tags=["catalog"], summary="약 성분 검색 (복용 약 입력 자동완성)")
def search_drugs(q: str = Query("", description="약 이름 일부")) -> list[dict]:
    return get_catalog().search_drugs(q)


@app.post("/v1/recommendations", response_model=RecommendResponse, tags=["engine"],
          summary="프로필 기반 조합 추천 + 성분 점검 + 수정안")
def recommend(req: RecommendRequest) -> RecommendResponse:
    try:
        return service.recommend(req)
    except InputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/v1/checks", response_model=CheckResponse, tags=["engine"],
          summary="사용자가 수정한 조합의 성분 점검 + 수정안")
def check(req: CheckRequest) -> CheckResponse:
    try:
        return service.check(req)
    except InputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.get("/", include_in_schema=False)
def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html")
