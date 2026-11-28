"""데이터 카탈로그: JSON 시드 데이터를 읽고 검증한 뒤 조회 기능을 제공한다.

데이터 폴더 위치는 환경변수 WELLINK_DATA_DIR로 바꿀 수 있다(기본: 패키지 내 data/).
데이터 파일에 오타(존재하지 않는 성분 ID 등)가 있으면 서버 시작 시 바로 오류를 낸다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from .models import Severity
from .text import fmt_amount, normalize
from .units import UnitError, to_canonical

DEFAULT_DATA_DIR = Path(__file__).parent / "data"

# 영양소 기능성 표시 요건: 1일 섭취량이 영양성분 기준치의 30% 이상
NUTRIENT_CLAIM_MIN_RATIO = 0.3
# 이 개수 이상의 영양소가 기능성 표시 요건을 채우고, 그 영양소가 3개 군 이상에 걸쳐 있으면
# '종합 영양' 제품으로 본다 (비타민B 컴플렉스처럼 한 군만 많은 제품은 제외)
MULTINUTRIENT_MIN_COUNT = 8
MULTINUTRIENT_MIN_GROUPS = 3
_B_VITAMINS = {"thiamin", "riboflavin", "niacin", "nicotinamide", "vitamin_b6", "folate",
               "vitamin_b12", "biotin", "pantothenic_acid"}
_FAT_SOLUBLE = {"vitamin_a", "vitamin_d", "vitamin_e", "vitamin_k"}


def nutrient_group(ingredient_id: str) -> str:
    if ingredient_id in _B_VITAMINS:
        return "vitamin_b"
    if ingredient_id in _FAT_SOLUBLE:
        return "fat_soluble"
    if ingredient_id == "vitamin_c":
        return "vitamin_c"
    return "mineral"


# ---------------------------------------------------------------------------
# 데이터 스키마
# ---------------------------------------------------------------------------

class Claim(BaseModel):
    text: str
    tags: dict[str, float] = Field(default_factory=dict)


class FunctionalRange(BaseModel):
    min: float
    max: float


class Ingredient(BaseModel):
    id: str
    name: str
    category: Literal["nutrient", "functional"]
    unit: str
    unit_label: str
    aliases: list[str] = Field(default_factory=list)
    nrv: Optional[float] = None
    iu_factor: Optional[float] = None
    claims: list[Claim] = Field(default_factory=list)
    functional_range: Optional[FunctionalRange] = None
    excess_severity: Severity = Severity.CAUTION
    notes: Optional[str] = None


class ProductIngredient(BaseModel):
    id: str
    amount: float
    unit: Optional[str] = Field(None, description="표시 단위 (생략하면 성분 기준 단위). 예: IU, mg, μg, g, 억CFU")
    estimated: bool = Field(False, description="표시사항으로 확인하지 못한 가정값")


class Product(BaseModel):
    """제품 표시사항 그대로의 정보.

    amount_basis가 per_serving이면 ingredients의 amount는 '1회 섭취량당' 함량이고,
    servings_per_day(1일 섭취 횟수)를 곱해 하루 섭취량으로 환산한다.
    엔진 내부의 모든 계산은 이 환산된 '1일 기준 · 성분 기준 단위' 값으로 한다.
    """

    id: str
    name: str
    brand: Optional[str] = None
    sold_on_platform: bool = True
    form: Optional[str] = None
    package: Optional[str] = None
    daily_dose_text: Optional[str] = None
    amount_basis: Literal["per_day", "per_serving"] = "per_day"
    servings_per_day: float = Field(1.0, gt=0)
    data_status: Literal["sample", "verified", "partial"] = "sample"
    data_note: Optional[str] = None
    data_sources: list[str] = Field(default_factory=list)
    facility_allergens: list[str] = Field(default_factory=list, description="같은 제조시설에서 쓰는 알레르기 원료")
    price_per_day: int = 0
    timing: Literal["morning", "evening", "any"] = "any"
    ingredients: list[ProductIngredient]
    allergens: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class Goal(BaseModel):
    id: str
    name: str
    tier: Literal["core", "extended"] = "core"
    description: str = ""
    tags: dict[str, float]
    priority_nutrients: list[str] = Field(default_factory=list)


class DrugMember(BaseModel):
    name: str
    aliases: list[str]


class DrugClass(BaseModel):
    id: str
    name: str
    usual_slot: Optional[Literal["morning", "lunch", "evening"]] = None
    members: list[DrugMember]


class DrugIngredientRule(BaseModel):
    id: str
    drug_classes: list[str]
    ingredients: list[str]
    min_amounts: dict[str, float] = Field(default_factory=dict)
    severity: Severity
    kind: Literal["interaction", "timing"]
    separation_hours: Optional[float] = None
    effect: str
    guidance: str
    basis: str


class IngredientPairRule(BaseModel):
    id: str
    ingredients_a: list[str]
    ingredients_b: list[str]
    min_amounts: dict[str, float] = Field(default_factory=dict)
    different_products: bool = False
    severity: Severity
    kind: Literal["interaction", "timing"]
    separation_hours: Optional[float] = None
    effect: str
    guidance: str
    basis: str


class IngredientGroupRule(BaseModel):
    id: str
    ingredients: list[str]
    min_amounts: dict[str, float] = Field(default_factory=dict)
    min_count: int = 2
    severity: Severity
    kind: Literal["interaction", "timing"] = "interaction"
    effect: str
    guidance: str
    basis: str


class ProfileIngredientRule(BaseModel):
    id: str
    condition: Literal["smoker", "pregnant_or_lactating"]
    ingredients: list[str]
    min_amounts: dict[str, float] = Field(default_factory=dict)
    severity: Severity
    effect: str
    guidance: str
    basis: str


class DepletionRule(BaseModel):
    drug_class: str
    nutrients: list[str]
    note: str


class FamilyHistory(BaseModel):
    id: str
    name: str
    related_goals: dict[str, float] = Field(default_factory=dict)
    priority_nutrients: list[str] = Field(default_factory=list)
    note: str


class LifeStage(BaseModel):
    id: str
    when: dict[str, Union[str, int]]
    priority_nutrients: list[str]
    note: str


class NutrientRef(BaseModel):
    type: Literal["rni", "ai", "none"]
    male: Optional[list[float]] = None
    female: Optional[list[float]] = None
    ul: Optional[Union[float, list[float]]] = None
    ul_basis: Optional[str] = None
    ul_note: Optional[str] = None


class Allergen(BaseModel):
    name: str
    aliases: list[str]


# ---------------------------------------------------------------------------
# 조회 결과 구조
# ---------------------------------------------------------------------------

@dataclass
class KdriValues:
    kind: str                 # rni / ai / none
    value: Optional[float]    # 권장섭취량 또는 충분섭취량
    ul: Optional[float]       # 상한섭취량
    ul_basis: Optional[str]   # supplement_only 등
    ul_note: Optional[str]
    source: str


@dataclass
class TagInfo:
    strength: float
    claims: list[tuple[str, str]] = field(default_factory=list)  # (성분명, 기능성 문구)


@dataclass
class DoseLine:
    """표시값 → 1일 기준값 환산 기록 (근거 표시용)."""

    ingredient_id: str
    per_day: float      # 성분 기준 단위, 표시된 1일 섭취량 기준
    label_text: str     # 예: "1회 1,000IU × 1일 2회 = 50μg"


@dataclass
class NormalizedDose:
    per_day: dict[str, float]
    lines: dict[str, list[DoseLine]]
    errors: list[str]


@dataclass
class DrugMatch:
    input_text: str
    drug_class: DrugClass
    member_name: str


# ---------------------------------------------------------------------------
# 카탈로그
# ---------------------------------------------------------------------------

class Catalog:
    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        ing = self._load("ingredients.json")
        ref = self._load("nutrient_reference.json")
        prod = self._load("products.json")
        extra_products: list[dict] = []
        for extra in sorted((self.data_dir / "products.d").glob("*.json")):
            with open(extra, encoding="utf-8") as f:
                extra_products += json.load(f).get("products", [])
        goals = self._load("goals.json")
        drugs = self._load("drugs.json")
        inter = self._load("interactions.json")
        prof = self._load("profile_rules.json")

        self.ingredients: dict[str, Ingredient] = {
            d["id"]: Ingredient(**d) for d in ing["ingredients"]
        }
        self.kdri_source: str = ref["source"]
        self.age_bands: list[list[int]] = ref["age_bands"]
        self.kdri: dict[str, NutrientRef] = {k: NutrientRef(**v) for k, v in ref["nutrients"].items()}
        self.products: dict[str, Product] = {}
        for d in [*prod["products"], *extra_products]:
            if d["id"] in self.products:
                raise ValueError(f"제품 ID가 중복됐어요: '{d['id']}'")
            self.products[d["id"]] = Product(**d)
        self._dose_cache: dict[str, NormalizedDose] = {}
        self.allergens: dict[str, Allergen] = {k: Allergen(**v) for k, v in prod.get("allergens", {}).items()}
        self.goals: dict[str, Goal] = {d["id"]: Goal(**d) for d in goals["goals"]}
        self.tag_labels: dict[str, str] = goals.get("tag_labels", {})
        self.drug_classes: dict[str, DrugClass] = {d["id"]: DrugClass(**d) for d in drugs["classes"]}
        self.drug_rules = [DrugIngredientRule(**d) for d in inter.get("drug_ingredient", [])]
        self.pair_rules = [IngredientPairRule(**d) for d in inter.get("ingredient_ingredient", [])]
        self.group_rules = [IngredientGroupRule(**d) for d in inter.get("ingredient_group", [])]
        self.profile_rules = [ProfileIngredientRule(**d) for d in inter.get("profile_ingredient", [])]
        self.depletion_rules = [DepletionRule(**d) for d in inter.get("drug_nutrient_depletion", [])]
        self.family_history: dict[str, FamilyHistory] = {
            d["id"]: FamilyHistory(**d) for d in prof.get("family_history", [])
        }
        self.life_stages = [LifeStage(**d) for d in prof.get("life_stage", [])]

        self.versions = {
            "ingredients": ing.get("version", ""),
            "nutrient_reference": ref.get("version", ""),
            "products": prod.get("version", ""),
            "goals": goals.get("version", ""),
            "drugs": drugs.get("version", ""),
            "interactions": inter.get("version", ""),
            "profile_rules": prof.get("version", ""),
        }

        self._build_indexes()
        self._validate()

    # -- 로딩 ---------------------------------------------------------------
    def _load(self, name: str) -> dict:
        with open(self.data_dir / name, encoding="utf-8") as f:
            return json.load(f)

    def _build_indexes(self) -> None:
        self._ingredient_alias: dict[str, str] = {}
        for ing in self.ingredients.values():
            for alias in [ing.id, ing.name, *ing.aliases]:
                self._ingredient_alias.setdefault(normalize(alias), ing.id)

        self._drug_alias: list[tuple[str, DrugClass, str]] = []
        for cls in self.drug_classes.values():
            for member in cls.members:
                for alias in [member.name, *member.aliases]:
                    self._drug_alias.append((normalize(alias), cls, member.name))
        # 긴 별칭부터 비교해 '에스오메프라졸'이 '오메프라졸'보다 먼저 잡히게 한다
        self._drug_alias.sort(key=lambda t: -len(t[0]))

        self._allergen_alias: dict[str, str] = {}
        for aid, allergen in self.allergens.items():
            for alias in [aid, allergen.name, *allergen.aliases]:
                self._allergen_alias[normalize(alias)] = aid

    def _validate(self) -> None:
        errors: list[str] = []
        known_tags = set(self.tag_labels) | {"multinutrient"}

        def need_ing(where: str, iid: str) -> None:
            if iid not in self.ingredients:
                errors.append(f"{where}: 알 수 없는 성분 ID '{iid}'")

        for ing in self.ingredients.values():
            for claim in ing.claims:
                for tag in claim.tags:
                    if tag not in known_tags:
                        errors.append(f"ingredients.json {ing.id}: 알 수 없는 기능성 태그 '{tag}'")
        for iid in self.kdri:
            need_ing("nutrient_reference.json", iid)
        for p in self.products.values():
            for pi in p.ingredients:
                need_ing(f"products.json {p.id}", pi.id)
            if all(pi.id in self.ingredients for pi in p.ingredients):
                for err in self.normalize_dose(p).errors:
                    errors.append(f"products.json {p.id}: {err}")
            for a in [*p.allergens, *p.facility_allergens]:
                if a not in self.allergens:
                    errors.append(f"products.json {p.id}: 알 수 없는 알레르기 ID '{a}'")
        for g in self.goals.values():
            for tag in g.tags:
                if tag not in known_tags:
                    errors.append(f"goals.json {g.id}: 알 수 없는 기능성 태그 '{tag}'")
            for n in g.priority_nutrients:
                need_ing(f"goals.json {g.id}", n)
        for r in self.drug_rules:
            for c in r.drug_classes:
                if c not in self.drug_classes:
                    errors.append(f"interactions.json {r.id}: 알 수 없는 약효군 '{c}'")
            for i in [*r.ingredients, *r.min_amounts]:
                need_ing(f"interactions.json {r.id}", i)
        for r in self.pair_rules:
            for i in [*r.ingredients_a, *r.ingredients_b, *r.min_amounts]:
                need_ing(f"interactions.json {r.id}", i)
        for r in self.group_rules:
            for i in [*r.ingredients, *r.min_amounts]:
                need_ing(f"interactions.json {r.id}", i)
        for r in self.profile_rules:
            for i in [*r.ingredients, *r.min_amounts]:
                need_ing(f"interactions.json {r.id}", i)
        for r in self.depletion_rules:
            if r.drug_class not in self.drug_classes:
                errors.append(f"interactions.json depletion: 알 수 없는 약효군 '{r.drug_class}'")
            for n in r.nutrients:
                need_ing("interactions.json depletion", n)
        for fh in self.family_history.values():
            for g in fh.related_goals:
                if g not in self.goals:
                    errors.append(f"profile_rules.json {fh.id}: 알 수 없는 목적 '{g}'")
            for n in fh.priority_nutrients:
                need_ing(f"profile_rules.json {fh.id}", n)
        for ls in self.life_stages:
            for n in ls.priority_nutrients:
                need_ing(f"profile_rules.json {ls.id}", n)
        if errors:
            raise ValueError("데이터 검증 실패:\n" + "\n".join(errors))

    # -- 조회 ---------------------------------------------------------------
    def resolve_ingredient(self, name: str) -> Optional[Ingredient]:
        iid = self._ingredient_alias.get(normalize(name))
        return self.ingredients.get(iid) if iid else None

    def match_drugs(self, text: str) -> list[DrugMatch]:
        """약 이름(자유 입력)에서 알려진 약효군을 찾는다. 복합제는 여러 개가 잡힐 수 있다."""
        norm = normalize(text)
        if not norm:
            return []
        matches: list[DrugMatch] = []
        seen: set[str] = set()
        for alias, cls, member in self._drug_alias:
            if len(alias) < 2:
                continue
            hit = alias in norm or (len(norm) >= 3 and norm in alias)
            if hit and cls.id not in seen:
                seen.add(cls.id)
                matches.append(DrugMatch(text, cls, member))
                norm = norm.replace(alias, " ")  # 같은 글자로 다른 약을 중복 인식하지 않도록
        return matches

    def match_allergen(self, text: str) -> Optional[str]:
        return self._allergen_alias.get(normalize(text))

    def search_drugs(self, query: str, limit: int = 20) -> list[dict]:
        q = normalize(query)
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for cls in self.drug_classes.values():
            for m in cls.members:
                if not q or any(q in normalize(a) for a in [m.name, *m.aliases]):
                    key = (cls.id, m.name)
                    if key not in seen:
                        seen.add(key)
                        out.append({"name": m.name, "class_id": cls.id, "class_name": cls.name})
        return out[:limit]

    def _band_index(self, age: int) -> int:
        for i, (lo, hi) in enumerate(self.age_bands):
            if lo <= age <= hi:
                return i
        return len(self.age_bands) - 1

    def kdri_for(self, ingredient_id: str, age: int, sex: str) -> Optional[KdriValues]:
        ref = self.kdri.get(ingredient_id)
        if ref is None:
            return None
        idx = self._band_index(age)
        values = ref.male if sex == "male" else ref.female
        value = values[idx] if values else None
        ul = ref.ul[idx] if isinstance(ref.ul, list) else ref.ul
        return KdriValues(ref.type, value, ul, ref.ul_basis, ref.ul_note, self.kdri_source)

    def claim_factor(self, ing: Ingredient, amount: float) -> float:
        """기능성 표시 요건 충족 정도(0~1). 요건 미달이면 0."""
        if amount <= 0:
            return 0.0
        if ing.category == "functional":
            if ing.functional_range is None:
                return 1.0
            return 1.0 if amount >= ing.functional_range.min else 0.0
        if not ing.nrv:
            return 0.0
        ratio = amount / ing.nrv
        if ratio < NUTRIENT_CLAIM_MIN_RATIO:
            return 0.0
        return min(1.0, ratio)

    def tags_for_amounts(self, amounts: dict[str, float]) -> dict[str, TagInfo]:
        """성분별 하루 함량 → 기능성 태그 강도(0~1)와 근거 문구."""
        tags: dict[str, TagInfo] = {}
        qualifying_nutrients = 0
        groups: set[str] = set()
        for iid, amount in amounts.items():
            ing = self.ingredients.get(iid)
            if ing is None:
                continue
            factor = self.claim_factor(ing, amount)
            if factor <= 0:
                continue
            if ing.category == "nutrient":
                qualifying_nutrients += 1
                groups.add(nutrient_group(iid))
            for claim in ing.claims:
                for tag, weight in claim.tags.items():
                    strength = round(weight * factor, 3)
                    if strength <= 0:
                        continue
                    info = tags.setdefault(tag, TagInfo(0.0))
                    info.strength = max(info.strength, strength)
                    info.claims.append((ing.name, claim.text))
        if qualifying_nutrients >= MULTINUTRIENT_MIN_COUNT and len(groups) >= MULTINUTRIENT_MIN_GROUPS:
            tags["multinutrient"] = TagInfo(
                1.0, [("종합", f"영양소 {qualifying_nutrients}종을 기능성 표시 기준 이상 함유")]
            )
        return tags

    def normalize_dose(self, product: Product) -> NormalizedDose:
        """제품 표시값(단위·1회/1일 기준·1일 섭취 횟수)을 '1일 기준 · 성분 기준 단위'로 환산한다."""
        cached = self._dose_cache.get(product.id)
        is_catalog = self.products.get(product.id) is product
        if cached is not None and is_catalog:
            return cached
        per_day: dict[str, float] = {}
        lines: dict[str, list[DoseLine]] = {}
        errors: list[str] = []
        times = product.servings_per_day if product.amount_basis == "per_serving" else 1.0
        for pi in product.ingredients:
            ing = self.ingredients[pi.id]
            unit = pi.unit or ing.unit
            try:
                one = to_canonical(pi.amount, unit, ing.unit, ing.iu_factor)
            except UnitError as e:
                errors.append(f"{ing.name}: {e}")
                continue
            value = one * times
            canon = fmt_amount(value, ing.unit_label.split(" (")[0])
            label = fmt_amount(pi.amount, unit if pi.unit else ing.unit_label.split(" (")[0])
            if product.amount_basis == "per_serving":
                text = f"1회 {label} × 1일 {times:g}회 = {canon}"
            elif pi.unit and _unit_differs(pi.unit, ing.unit):
                text = f"1일 {label} = {canon}"
            else:
                text = f"1일 {canon}"
            if pi.estimated:
                text += " (가정값)"
            per_day[pi.id] = per_day.get(pi.id, 0.0) + value
            lines.setdefault(pi.id, []).append(DoseLine(pi.id, value, text))
        result = NormalizedDose(per_day, lines, errors)
        if is_catalog:
            self._dose_cache[product.id] = result
        return result

    def product_amounts(self, product: Product, servings: float = 1.0) -> dict[str, float]:
        """하루 섭취량(성분 기준 단위). servings는 표시된 1일 섭취량 대비 실제 섭취 배수."""
        return {k: v * servings for k, v in self.normalize_dose(product).per_day.items()}

    def product_functions(self, product: Product) -> list[str]:
        """제품이 표시할 수 있는 기능성 문구 (성분명: 문구)."""
        out: list[str] = []
        for iid, amount in self.normalize_dose(product).per_day.items():
            ing = self.ingredients[iid]
            if self.claim_factor(ing, amount) <= 0:
                continue
            for claim in ing.claims:
                line = f"{ing.name}: {claim.text}"
                if line not in out:
                    out.append(line)
        return out

    def platform_products(self) -> list[Product]:
        return [p for p in self.products.values() if p.sold_on_platform]

    def tag_label(self, tag: str) -> str:
        return self.tag_labels.get(tag, "종합 영양" if tag == "multinutrient" else tag)


def _unit_differs(a: str, b: str) -> bool:
    return normalize(a).replace("µ", "μ") != normalize(b).replace("µ", "μ")


@lru_cache(maxsize=4)
def _cached_catalog(path: str) -> Catalog:
    return Catalog(path)


def get_catalog(data_dir: Optional[str] = None) -> Catalog:
    path = data_dir or os.environ.get("WELLINK_DATA_DIR") or str(DEFAULT_DATA_DIR)
    return _cached_catalog(str(Path(path).resolve()))
