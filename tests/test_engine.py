"""엔진 단위·시나리오 테스트.

시나리오는 P파트 설문의 대표 고객 장면(직장인 A, 대학생 B, 부모님 사례)을 바탕으로 한다.
"""

import pytest

from wellink_engine import service
from wellink_engine.catalog import get_catalog
from wellink_engine.context import InputError, build_context
from wellink_engine.models import (
    CheckRequest,
    FindingType,
    IngredientAmountInput,
    ItemInput,
    Profile,
    RecommendRequest,
    Severity,
    SuggestionAction,
)
from wellink_engine.text import josa
from wellink_engine.units import UnitError, to_canonical


@pytest.fixture(scope="module")
def cat():
    return get_catalog()


def profile(**kw):
    base = dict(age=35, sex="male", goals=["basic_health"])
    base.update(kw)
    return Profile(**base)


def check(items, **profile_kw):
    return service.check(CheckRequest(profile=profile(**profile_kw), items=items))


def ids(*product_ids):
    return [ItemInput(product_id=p) for p in product_ids]


def findings_of(report, ftype):
    return [f for f in report.findings if f.type == ftype]


# ---------------------------------------------------------------------------
# 기초: 단위 환산, 별칭, 기준치
# ---------------------------------------------------------------------------

def test_unit_conversion():
    assert to_canonical(1000, "IU", "μg", 0.025) == pytest.approx(25)       # 비타민D
    assert to_canonical(1, "g", "mg") == pytest.approx(1000)
    assert to_canonical(500, "mcg", "μg") == pytest.approx(500)
    assert to_canonical(0.5, "mg", "μg") == pytest.approx(500)
    assert to_canonical(2, "µg", "μg") == pytest.approx(2)                  # 마이크로 기호 변형
    assert to_canonical(10_000_000_000, "CFU", "억CFU") == pytest.approx(100)
    assert to_canonical(700, "μg RAE", "μg") == pytest.approx(700)
    with pytest.raises(UnitError):
        to_canonical(10, "IU", "mg", None)
    with pytest.raises(UnitError):
        to_canonical(10, "정", "mg")


def test_ingredient_aliases(cat):
    assert cat.resolve_ingredient("Vitamin D3").id == "vitamin_d"
    assert cat.resolve_ingredient("오메가-3").id == "epa_dha"
    assert cat.resolve_ingredient("EPA 및 DHA의 합").id == "epa_dha"
    assert cat.resolve_ingredient("니코틴산아마이드").id == "nicotinamide"
    assert cat.resolve_ingredient("없는성분") is None


def test_drug_matching(cat):
    assert [m.drug_class.id for m in cat.match_drugs("에스오메프라졸 20mg")] == ["ppi"]
    assert {m.drug_class.id for m in cat.match_drugs("리피토10mg, 아스피린")} == {"statin", "antiplatelet"}
    assert [m.drug_class.id for m in cat.match_drugs("씬지로이드 50mcg")] == ["thyroid_hormone"]
    assert cat.match_drugs("타이레놀") == []


def test_kdri_by_age_and_sex(cat):
    assert cat.kdri_for("calcium", 30, "female").value == 700
    assert cat.kdri_for("calcium", 55, "female").value == 800
    assert cat.kdri_for("calcium", 55, "female").ul == 2000
    assert cat.kdri_for("vitamin_d", 70, "male").value == 15
    assert cat.kdri_for("iron", 30, "female").value == 14


def test_claim_threshold_and_multinutrient(cat):
    multi = cat.tags_for_amounts(cat.product_amounts(cat.products["wl_multi"]))
    bcomplex = cat.tags_for_amounts(cat.product_amounts(cat.products["wl_bcomplex"]))
    assert multi["multinutrient"].strength == 1.0
    assert "multinutrient" not in bcomplex  # 비타민B군만 많은 제품은 종합 영양이 아님
    # 영양성분 기준치 30% 미만이면 기능성 근거로 쓰지 않음
    assert cat.tags_for_amounts({"zinc": 2.0}) == {}


def test_josa():
    assert josa("145μg", "으로/로") == "145μg으로"
    assert josa("비타민D", "을/를") == "비타민D를"
    assert josa("칼슘", "이/가") == "칼슘이"
    assert josa("WL 루테인 20", "으로/로") == "WL 루테인 20으로"


def test_unknown_goal_rejected(cat):
    with pytest.raises(InputError):
        build_context(cat, profile(goals=["no_such_goal"]))


# ---------------------------------------------------------------------------
# 성분 점검: 과다·중복·부족·병용 주의
# ---------------------------------------------------------------------------

def test_vitamin_d_excess_from_multiple_products():
    """직장인 A 장면: 종합비타민 + 칼마디 + 고함량 비타민D → 비타민D 상한 초과."""
    r = check(ids("wl_multi", "wl_calmagd", "ext_vitd5000"))
    excess = [f for f in findings_of(r.report, FindingType.EXCESS) if f.ingredient_ids == ["vitamin_d"]]
    assert len(excess) == 1
    f = excess[0]
    assert f.severity == Severity.WARNING
    assert "145μg" in f.message and "100μg" in f.message
    assert len(f.item_keys) == 3
    # 근거: 기준치 출처 + 제품별 표시 함량
    sources = [e.source for e in f.evidence]
    assert any("영양소 섭취기준" in s for s in sources)
    assert sum("제품 표시사항" in s for s in sources) == 3
    # 수정안: 고함량 비타민D를 빼면 해결
    assert any(s.action == SuggestionAction.REMOVE and s.target_product_id == "ext_vitd5000" for s in f.suggestions)
    dist = {d.ingredient_id: d for d in r.report.distribution}
    assert dist["vitamin_d"].status == "excess" and dist["vitamin_d"].is_duplicate


def test_magnesium_supplement_only_upper_limit():
    r = check(ids("wl_multi", "wl_calmagd"))
    near = [f for f in findings_of(r.report, FindingType.NEAR_UPPER) if f.ingredient_ids == ["magnesium"]]
    assert near and "보충제 기준" in near[0].message


def test_duplicate_within_limits_is_info():
    r = check(ids("wl_multi", "wl_vitc"))
    dups = findings_of(r.report, FindingType.DUPLICATE)
    assert dups and all(f.severity == Severity.INFO for f in dups)
    assert "비타민C" in dups[0].title


def test_functional_excess_and_below_effective():
    r = check(ids("wl_probiotics", "ext_probiotics"), goals=["gut"])
    ex = [f for f in findings_of(r.report, FindingType.EXCESS) if f.ingredient_ids == ["probiotics"]]
    assert ex and ex[0].severity == Severity.CAUTION

    low = check([ItemInput(name="저함량 오메가3", ingredients=[IngredientAmountInput(name="EPA 및 DHA", amount=300, unit="mg")])],
                goals=["cardio"])
    below = findings_of(low.report, FindingType.BELOW_EFFECTIVE)
    assert below and below[0].ingredient_ids == ["epa_dha"]
    # 부족: 기준량을 채우는 플랫폼 제품으로 바꾸는 수정안
    assert any(s.action == SuggestionAction.REPLACE for s in below[0].suggestions)


def test_warfarin_vitamin_k_and_bleeding():
    r = check(ids("wl_multi", "wl_omega3", "wl_ginkgo"), medications=["와파린 2mg"])
    titles = [f.title for f in r.report.findings]
    vk = [f for f in findings_of(r.report, FindingType.DRUG_INTERACTION) if "vitamin_k" in f.ingredient_ids]
    assert vk and vk[0].severity == Severity.WARNING
    ginkgo = [f for f in findings_of(r.report, FindingType.DRUG_INTERACTION) if "ginkgo" in f.ingredient_ids]
    assert ginkgo and ginkgo[0].severity == Severity.WARNING
    assert any("함께 섭취 시 참고" in t for t in titles)  # 출혈 경향 원료 2가지 이상
    assert r.report.recognized_drugs[0]["name"] == "와파린"
    # 비타민K 문제의 수정안: 비타민K 없는 멀티로 교체
    assert any(s.new_product_id == "wl_multi_basic" for s in vk[0].suggestions)


def test_statin_red_yeast_rice_is_avoid():
    r = check(ids("wl_redyeast"), goals=["cardio"], medications=["로수바스타틴"])
    f = findings_of(r.report, FindingType.DRUG_INTERACTION)[0]
    assert f.severity == Severity.AVOID
    assert r.report.summary.overall == "avoid"
    assert r.revision and r.revision.applied
    revised = [i.product_id for i in r.revision.items]
    assert "wl_redyeast" not in revised
    # 목적(혈중 지질)은 유지하는 방향으로 교체
    assert any(p in revised for p in ("wl_omega3", "wl_lutein_omega3"))
    assert r.revision.after.overall != "avoid"


def test_levothyroxine_timing_and_schedule():
    r = check(ids("wl_calmagd", "wl_vitc"), medications=["씬지로이드"])
    timing = findings_of(r.report, FindingType.DRUG_TIMING)
    assert timing and timing[0].suggestions[0].action == SuggestionAction.SEPARATE_TIMING
    slot_of = {k: s.slot for s in r.report.schedule.slots for k in s.item_keys}
    assert slot_of["wl_calmagd"] != "morning"


def test_calcium_iron_schedule_separation():
    r = check(ids("wl_iron_folate", "wl_calmagd"), sex="female", goals=["bone"])
    assert findings_of(r.report, FindingType.SUPPLEMENT_TIMING)
    slot_of = {k: s.slot for s in r.report.schedule.slots for k in s.item_keys}
    assert slot_of["wl_iron_folate"] != slot_of["wl_calmagd"]


def test_allergy_is_avoid_with_safe_replacement():
    r = check(ids("wl_glucosamine"), goals=["joint"], allergies=["새우"])
    f = findings_of(r.report, FindingType.ALLERGY)[0]
    assert f.severity == Severity.AVOID
    assert any(s.new_product_id == "wl_msm" for s in f.suggestions)


def test_smoker_beta_carotene_and_pregnancy():
    r = check(ids("ext_multi"), smoker=True)
    assert any(f.type == FindingType.PROFILE_CAUTION and "베타카로틴" in f.title for f in r.report.findings)
    p = check(ids("wl_garcinia"), sex="female", goals=["body_fat"], pregnant_or_lactating=True)
    preg = findings_of(p.report, FindingType.PROFILE_CAUTION)
    assert preg and preg[0].severity == Severity.AVOID
    assert any("임신·수유" in d for d in p.report.disclaimers)


def test_unknown_drug_and_ingredient_are_flagged_not_ignored():
    r = check(
        [ItemInput(name="직구 제품", ingredients=[
            IngredientAmountInput(name="비타민D3", amount=1000, unit="IU"),
            IngredientAmountInput(name="신비한추출물", amount=50, unit="mg"),
        ])],
        medications=["듣도보도못한약"],
    )
    types = {f.type for f in r.report.findings}
    assert FindingType.UNKNOWN_DRUG in types and FindingType.UNKNOWN_INGREDIENT in types
    dist = {d.ingredient_id: d for d in r.report.distribution}
    assert dist["vitamin_d"].total == pytest.approx(25)  # IU 환산


def test_priority_nutrient_from_drug_depletion():
    r = check(ids("wl_omega3"), goals=["cardio"], medications=["메트포르민"])
    low = findings_of(r.report, FindingType.NUTRIENT_LOW)
    assert any(f.ingredient_ids == ["vitamin_b12"] for f in low)


def test_goal_gap_and_add_suggestion():
    r = check(ids("wl_vitc"), goals=["eye"])
    gap = findings_of(r.report, FindingType.GOAL_GAP)
    assert gap and any(s.action == SuggestionAction.ADD for s in gap[0].suggestions)


def test_locked_items_are_not_changed():
    items = [ItemInput(product_id="wl_redyeast", locked=True)]
    r = service.check(CheckRequest(profile=profile(goals=["cardio"], medications=["아토르바스타틴"]), items=items))
    assert r.revision is not None
    assert "wl_redyeast" in [i.product_id for i in r.revision.items]
    assert r.revision.note and "고정" in r.revision.note


def test_servings_multiplier_and_reduce_suggestion():
    r = check([ItemInput(product_id="wl_vitc", servings=3)])
    ex = [f for f in findings_of(r.report, FindingType.EXCESS) if f.ingredient_ids == ["vitamin_c"]]
    assert ex  # 3,000mg > 상한 2,000mg
    assert any(s.action == SuggestionAction.REDUCE for s in ex[0].suggestions)


# ---------------------------------------------------------------------------
# 추천
# ---------------------------------------------------------------------------

def recommend(**kw):
    current = kw.pop("current", [])
    opts = {k: kw.pop(k) for k in ["max_products", "budget_per_day", "exclude_product_ids"] if k in kw}
    return service.recommend(RecommendRequest(profile=profile(**kw), current_products=current, **opts))


def test_recommendation_student_fatigue_immunity():
    """대학생 B 장면: 피로·면역 목적, 복용 약 없음."""
    r = recommend(age=22, sex="female", goals=["fatigue", "immunity"])
    assert r.recommended and len(r.recommended) <= 2  # 두 목적을 적은 제품으로 채움
    assert all(p.reasons for p in r.recommended)
    assert r.report.summary.overall in ("ok", "review")
    assert all(g.covered for g in r.report.goal_coverage)


def test_lean_combination_when_goal_already_covered():
    """핵심 기능성이 채워진 목적의 보조 기능성만을 위해 제품을 더 넣지 않는다."""
    r = recommend(age=40, sex="female", goals=["body_fat", "fatigue"])
    assert [p.product_id for p in r.recommended] == ["wl_garcinia", "wl_redginseng"]


def test_inflammation_goal_uses_only_approved_claims():
    r = recommend(age=45, sex="female", goals=["inflammation_care"])
    texts = " ".join(x.text for p in r.recommended for x in p.reasons)
    assert "염증" not in texts
    assert "유해산소" in texts or "관절" in texts


def test_label_units_and_twice_daily_are_normalized():
    """표시 단위(IU·g·CFU)와 1회 기준·1일 2회 표시를 1일 기준으로 환산하고 과정을 남긴다."""
    r = check([
        ItemInput(name="직구 비타민D", amount_basis="per_serving", servings_per_day=2,
                  ingredients=[IngredientAmountInput(name="Vitamin D3", amount=2500, unit="IU")]),
        ItemInput(product_id="wl_calmagd", actual_servings_per_day=1),  # 표시는 1일 2회
    ], goals=["bone"])
    dist = {d.ingredient_id: d for d in r.report.distribution}
    assert dist["vitamin_d"].total == pytest.approx(130)   # 125 + 5
    assert dist["vitamin_d"].total_iu == pytest.approx(5200)
    assert dist["calcium"].total == pytest.approx(300)
    calcs = [s.calculation for s in dist["vitamin_d"].sources]
    assert "1회 2,500IU × 1일 2회 = 125μg" in calcs[0]
    assert "실제 1일 1회 섭취" in calcs[1]
    views = {i.name: i for i in r.report.items}
    assert views["WL 칼슘·마그네슘·비타민D"].actual_servings_per_day == 1


def test_recommendation_never_creates_warning_or_drug_issue():
    for medication in [["와파린"], ["아스피린"], ["메트포르민"], ["로수바스타틴"], []]:
        for goals in [["cardio", "eye"], ["blood_sugar", "bone"], ["fatigue", "immunity", "liver"]]:
            r = recommend(goals=goals, medications=medication, age=60, sex="female")
            new_keys = {p.item_key for p in r.recommended}
            for f in r.report.findings:
                if set(f.item_keys) & new_keys:
                    assert f.severity in (Severity.CAUTION, Severity.INFO), f.title
                    assert f.type != FindingType.DRUG_INTERACTION, f.title


def test_recommendation_respects_existing_products_and_limits():
    # 이미 오메가3를 먹고 있으면 혈행 목적에 오메가3를 또 추천하지 않음
    r = recommend(goals=["cardio"], current=ids("ext_omega3"))
    assert not any("omega3" in p.product_id for p in r.recommended)
    assert r.combination[0].product_id == "ext_omega3"

    r = recommend(goals=["fatigue", "immunity", "eye", "gut"], max_products=2)
    assert len(r.recommended) <= 2

    r = recommend(goals=["fatigue", "immunity"], budget_per_day=600)
    assert sum(p.price_per_day for p in r.recommended) <= 600


def test_recommendation_excludes_with_reason_for_drug_conflict():
    r = recommend(goals=["cardio"], medications=["와파린"])
    excluded = {e.product_id: e.reason for e in r.excluded}
    assert "wl_ginkgo" in excluded and "와파린" in excluded["wl_ginkgo"]
    assert "wl_omega3" in excluded
    assert any("자동 추천에서 뺐어요" in n for n in r.profile_notes)


def test_recommendation_fixes_existing_problem_via_revision():
    """현재 먹는 제품끼리 마그네슘 과다 → 자동 수정안 제시."""
    r = recommend(goals=["bone", "cardio"], current=ids("ext_multi", "ext_calmagd"), medications=["아스피린"])
    assert any(f.type == FindingType.EXCESS and f.ingredient_ids == ["magnesium"] for f in r.report.findings)
    assert r.revision and r.revision.applied
    assert not any("마그네슘 과다" in t for t in r.revision.remaining_findings)


def test_family_history_adds_priority_check():
    r = recommend(age=55, sex="female", goals=["eye"], family_history=["osteoporosis"])
    assert any("골다공증" in n for n in r.profile_notes)
    coverage_ids = [p.product_id for p in r.recommended]
    # 가족력은 사용자 목적보다 낮은 가중치로 반영되지만, 뼈 건강 제품을 함께 고려
    assert "wl_calmagd" in coverage_ids or any(f.type == FindingType.NUTRIENT_LOW for f in r.report.findings)
