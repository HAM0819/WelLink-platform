"""엔진 입출력 모델.

API 요청/응답 스키마와 엔진 내부 결과 구조를 함께 정의한다.
앱(Flutter/React Native)은 이 스키마(OpenAPI 문서: /docs)를 기준으로 연동한다.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 공통 열거형
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """점검 결과의 심각도. AVOID가 가장 높다."""

    AVOID = "avoid"      # 섭취·병용을 피하세요
    WARNING = "warning"  # 조정을 권장해요
    CAUTION = "caution"  # 주의해서 드세요
    INFO = "info"        # 참고하세요


SEVERITY_RANK = {Severity.AVOID: 3, Severity.WARNING: 2, Severity.CAUTION: 1, Severity.INFO: 0}
SEVERITY_LABEL = {
    Severity.AVOID: "피하세요",
    Severity.WARNING: "조정 권장",
    Severity.CAUTION: "주의",
    Severity.INFO: "참고",
}


class FindingType(str, Enum):
    EXCESS = "excess"                          # 상한섭취량·기능성 최대량 초과 (과다)
    NEAR_UPPER = "near_upper"                  # 상한섭취량 근접
    DUPLICATE = "duplicate"                    # 같은 성분을 여러 제품에서 섭취 (중복)
    BELOW_EFFECTIVE = "below_effective"        # 기능성 최소량 미달 (부족)
    NUTRIENT_LOW = "nutrient_low"              # 우선 확인 영양소 보충량 적음 (부족 가능)
    GOAL_GAP = "goal_gap"                      # 선택한 목적을 채우는 제품이 없음
    DRUG_INTERACTION = "drug_interaction"      # 복용 약과 병용 주의
    DRUG_TIMING = "drug_timing"                # 복용 약과 시간 간격 필요
    SUPPLEMENT_INTERACTION = "supplement_interaction"  # 건강기능식품 간 병용 주의
    SUPPLEMENT_TIMING = "supplement_timing"    # 건강기능식품 간 시간 간격 필요
    PROFILE_CAUTION = "profile_caution"        # 흡연·임신 등 개인 상태에 따른 주의
    ALLERGY = "allergy"                        # 알레르기 성분 포함
    UNKNOWN_DRUG = "unknown_drug"              # 인식하지 못한 약
    UNKNOWN_INGREDIENT = "unknown_ingredient"  # 인식하지 못한 성분
    ESTIMATED_DATA = "estimated_data"          # 제품 함량 중 가정값이 있음 (표시사항 확인 전)


# 앱 화면에서 탭/칩으로 묶어 보여 주기 위한 분류 (설문 '확인하고 싶은 정보' 기준)
FINDING_CATEGORY = {
    FindingType.EXCESS: "excess",
    FindingType.NEAR_UPPER: "excess",
    FindingType.DUPLICATE: "duplicate",
    FindingType.BELOW_EFFECTIVE: "deficiency",
    FindingType.NUTRIENT_LOW: "deficiency",
    FindingType.GOAL_GAP: "deficiency",
    FindingType.DRUG_INTERACTION: "interaction",
    FindingType.DRUG_TIMING: "interaction",
    FindingType.SUPPLEMENT_INTERACTION: "interaction",
    FindingType.SUPPLEMENT_TIMING: "interaction",
    FindingType.PROFILE_CAUTION: "caution",
    FindingType.ALLERGY: "caution",
    FindingType.UNKNOWN_DRUG: "check_needed",
    FindingType.UNKNOWN_INGREDIENT: "check_needed",
    FindingType.ESTIMATED_DATA: "check_needed",
}
CATEGORY_LABEL = {
    "excess": "과다",
    "duplicate": "중복",
    "deficiency": "부족",
    "interaction": "병용 주의",
    "caution": "섭취 주의",
    "check_needed": "확인 필요",
}


# ---------------------------------------------------------------------------
# 요청 모델
# ---------------------------------------------------------------------------

class Profile(BaseModel):
    """사용자 건강 프로필. 저장하지 않고 요청마다 계산에만 사용한다."""

    age: int = Field(..., ge=19, le=120, description="만 나이 (성인 기준 데이터만 제공하므로 19세 이상)")
    sex: Literal["male", "female"] = Field(..., description="성별 (영양소 섭취기준 구간 선택에 사용)")
    goals: list[str] = Field(..., min_length=1, max_length=5, description="건강 관리 목적 ID, 중요한 순서대로")
    family_history: list[str] = Field(default_factory=list, description="가족력 ID 목록 (선택 입력)")
    medications: list[str] = Field(default_factory=list, description="복용 중인 약 성분명 또는 제품명 (자유 입력)")
    allergies: list[str] = Field(default_factory=list, description="알레르기 (예: 갑각류, 우유)")
    pregnant_or_lactating: bool = Field(False, description="임신 또는 수유 중")
    smoker: bool = Field(False, description="흡연 여부")


class IngredientAmountInput(BaseModel):
    name: str = Field(..., description="성분명 (한글/영문/별칭 모두 가능)")
    amount: float = Field(..., gt=0, description="제품 표시사항의 함량 (amount_basis 기준)")
    unit: str = Field(..., description="표시 단위 그대로: mg, μg(mcg), g, IU, 억CFU, CFU 등")


class ItemInput(BaseModel):
    """조합에 들어가는 제품 1개.

    - 플랫폼 취급 제품·DB 등록 제품: product_id만 지정
    - DB에 없는 타사 제품: name + ingredients를 표시사항 그대로 입력
      (단위·1회 기준/1일 기준·1일 섭취 횟수가 제품마다 달라도 엔진이 1일 기준으로 환산)

    실제 섭취량이 표시와 다르면 actual_servings_per_day(하루 실제 섭취 횟수) 또는 servings(배수)로 지정한다.
    """

    product_id: Optional[str] = None
    name: Optional[str] = None
    ingredients: Optional[list[IngredientAmountInput]] = None
    amount_basis: Literal["per_day", "per_serving"] = Field(
        "per_day", description="직접 입력 제품의 함량 기준: per_day(1일 섭취량당) / per_serving(1회 섭취량당)")
    servings_per_day: float = Field(1.0, gt=0, le=10, description="직접 입력 제품의 표시된 1일 섭취 횟수")
    actual_servings_per_day: Optional[float] = Field(
        None, gt=0, le=20, description="사용자가 실제로 하루에 먹는 횟수 (지정하면 servings 대신 사용)")
    servings: float = Field(1.0, gt=0, le=10, description="표시된 1일 섭취량 대비 실제 섭취 배수 (1 = 표시대로)")
    locked: bool = Field(False, description="사용자가 고정한 제품 (수정안에서 빼거나 바꾸지 않음)")


class RecommendRequest(BaseModel):
    profile: Profile
    current_products: list[ItemInput] = Field(default_factory=list, description="현재 먹고 있는 제품")
    max_products: int = Field(3, ge=1, le=6, description="새로 추천할 최대 제품 수")
    max_total_products: int = Field(6, ge=1, le=10, description="현재 제품 포함 최대 제품 수")
    budget_per_day: Optional[int] = Field(None, ge=0, description="새 제품의 하루 예산(원)")
    exclude_product_ids: list[str] = Field(default_factory=list, description="추천에서 뺄 제품")


class CheckRequest(BaseModel):
    profile: Profile
    items: list[ItemInput] = Field(..., min_length=1, description="점검할 조합 (현재 제품 + 선택 제품)")


# ---------------------------------------------------------------------------
# 결과 모델
# ---------------------------------------------------------------------------

class Evidence(BaseModel):
    source: str = Field(..., description="근거 출처")
    detail: str = Field(..., description="근거 내용")


class ProductContribution(BaseModel):
    item_key: str
    product_id: str
    product_name: str
    amount: float = Field(..., description="실제 하루 섭취량 (성분 기준 단위)")
    calculation: str = Field(..., description="표시값에서 하루 섭취량으로 환산한 과정")


class ReferenceValues(BaseModel):
    kind: Literal["rni", "ai", "functional_range", "none"]
    label: str = Field(..., description="권장섭취량/충분섭취량/기능성 일일섭취량 등")
    value: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    ul: Optional[float] = None
    ul_basis: Optional[str] = None
    source: str


class DistributionEntry(BaseModel):
    ingredient_id: str
    name: str
    category: Literal["nutrient", "functional"]
    unit: str
    total: float = Field(..., description="하루 합계 (성분 기준 단위, 모든 제품 공통)")
    total_iu: Optional[float] = Field(None, description="IU 표기가 흔한 성분(비타민A·D·E)의 IU 환산값")
    reference: ReferenceValues
    percent_of_reference: Optional[float] = Field(None, description="권장(충분)섭취량 또는 기능성 최소량 대비 %")
    percent_of_upper: Optional[float] = Field(None, description="상한섭취량 또는 기능성 최대량 대비 %")
    status: Literal["excess", "near_upper", "adequate", "low", "no_reference"]
    status_label: str
    is_duplicate: bool
    sources: list[ProductContribution]


class SuggestionAction(str, Enum):
    REMOVE = "remove"
    REPLACE = "replace"
    ADD = "add"
    REDUCE = "reduce"
    SEPARATE_TIMING = "separate_timing"
    CONSULT = "consult"


class Suggestion(BaseModel):
    id: str
    action: SuggestionAction
    description: str
    target_item_key: Optional[str] = None
    target_product_id: Optional[str] = None
    new_product_id: Optional[str] = None
    new_servings: Optional[float] = None
    resolves: list[str] = Field(default_factory=list, description="이 수정으로 해결되는 점검 항목 ID")
    introduces: list[str] = Field(default_factory=list, description="이 수정으로 새로 생기는 주의 항목 제목")
    price_change_per_day: Optional[int] = None


class Finding(BaseModel):
    id: str
    type: FindingType
    category: str
    category_label: str
    severity: Severity
    severity_label: str
    title: str
    message: str
    guidance: Optional[str] = None
    ingredient_ids: list[str] = Field(default_factory=list)
    item_keys: list[str] = Field(default_factory=list)
    drug_names: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)


class ItemView(BaseModel):
    key: str
    product_id: str
    name: str
    brand: Optional[str] = None
    source: Literal["platform", "external"]
    servings: float = Field(..., description="표시된 1일 섭취량 대비 실제 섭취 배수")
    label_servings_per_day: float = Field(..., description="표시된 1일 섭취 횟수")
    actual_servings_per_day: float = Field(..., description="실제 1일 섭취 횟수")
    locked: bool
    price_per_day: int
    daily_dose_text: Optional[str] = None
    functions: list[str] = Field(default_factory=list, description="이 제품이 표시할 수 있는 기능성 문구")
    data_status: str = Field("sample", description="제품 데이터 상태: sample(가상) / verified(표시사항 확인) / partial(일부 가정값·확인 필요)")
    data_note: Optional[str] = None
    estimated_ingredients: list[str] = Field(default_factory=list, description="함량이 가정값인 성분명")
    cautions: list[str] = Field(default_factory=list, description="제품 표시 주의사항")


class GoalCoverage(BaseModel):
    goal_id: str
    name: str
    covered: bool
    coverage: float
    by_items: list[str]
    note: Optional[str] = Field(None, description="여러 제품을 합쳐서 채운 경우 등 설명")


class ScheduleSlot(BaseModel):
    slot: Literal["morning", "lunch", "evening"]
    label: str
    item_keys: list[str]
    product_names: list[str]


class Schedule(BaseModel):
    slots: list[ScheduleSlot]
    notes: list[str]


class ReportSummary(BaseModel):
    overall: Literal["ok", "review", "adjust", "avoid"]
    overall_label: str
    counts_by_category: dict[str, int]
    counts_by_severity: dict[str, int]
    total_price_per_day: int


class CheckReport(BaseModel):
    items: list[ItemView]
    summary: ReportSummary
    findings: list[Finding]
    distribution: list[DistributionEntry]
    goal_coverage: list[GoalCoverage]
    schedule: Schedule
    recognized_drugs: list[dict]
    disclaimers: list[str]
    data_version: dict[str, str]


class RevisionStep(BaseModel):
    order: int
    suggestion: Suggestion


class Revision(BaseModel):
    """자동 수정안: 위험도가 높은 항목부터 하나씩 고친 결과."""

    applied: bool
    steps: list[RevisionStep]
    items: list[ItemInput] = Field(..., description="수정안을 적용한 조합 (그대로 /v1/checks에 다시 보낼 수 있음)")
    before: ReportSummary
    after: ReportSummary
    remaining_findings: list[str] = Field(default_factory=list, description="수정 후에도 남는 주요 항목 제목")
    note: Optional[str] = None


class Reason(BaseModel):
    goal_id: Optional[str] = None
    text: str


class AlternativeProduct(BaseModel):
    product_id: str
    name: str
    price_per_day: int
    reason: str


class RecommendedProduct(BaseModel):
    item_key: str
    product_id: str
    name: str
    price_per_day: int
    reasons: list[Reason]
    considerations: list[str] = Field(default_factory=list)
    alternatives: list[AlternativeProduct] = Field(default_factory=list)


class ExcludedProduct(BaseModel):
    product_id: str
    name: str
    reason: str


class RecommendResponse(BaseModel):
    recommended: list[RecommendedProduct]
    combination: list[ItemInput] = Field(..., description="현재 제품 + 추천 제품 (사용자가 수정 후 /v1/checks로 재점검)")
    excluded: list[ExcludedProduct]
    profile_notes: list[str]
    report: CheckReport
    revision: Optional[Revision] = None


class CheckResponse(BaseModel):
    report: CheckReport
    revision: Optional[Revision] = None
