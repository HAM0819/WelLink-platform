"""조합 항목(ItemInput)을 카탈로그 제품과 성분 함량으로 해석한다."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, DoseLine, Product, ProductIngredient
from .context import InputError
from .models import ItemInput
from .text import fmt_amount


@dataclass
class ResolvedItem:
    key: str
    product: Product
    servings: float
    locked: bool
    custom: bool
    input: ItemInput
    amounts: dict[str, float]
    unknown_ingredients: list[str] = field(default_factory=list)
    dose_lines: dict[str, list[DoseLine]] = field(default_factory=dict)

    @property
    def actual_servings_per_day(self) -> float:
        return round(self.product.servings_per_day * self.servings, 3)

    def calculation(self, ingredient_id: str, unit_label: str) -> str:
        """표시값 → 실제 하루 섭취량 환산 과정 (예: 1회 1,000IU × 1일 2회 = 50μg → 실제 1일 1회 섭취 = 25μg)."""
        base = " + ".join(line.label_text for line in self.dose_lines.get(ingredient_id, []))
        if self.servings != 1:
            actual = fmt_amount(self.amounts.get(ingredient_id, 0.0), unit_label)
            base += f" → 실제 1일 {self.actual_servings_per_day:g}회 섭취(표시의 {self.servings:g}배) = {actual}"
        return base

    @property
    def source(self) -> str:
        return "platform" if self.product.sold_on_platform else "external"

    @property
    def price_per_day(self) -> int:
        return int(round(self.product.price_per_day * self.servings))


def resolve_items(catalog: Catalog, items: list[ItemInput]) -> list[ResolvedItem]:
    resolved: list[ResolvedItem] = []
    used_keys: set[str] = set()
    custom_count = 0
    for item in items:
        unknown: list[str] = []
        if item.product_id:
            product = catalog.products.get(item.product_id)
            if product is None:
                raise InputError(f"알 수 없는 제품이에요: '{item.product_id}'")
            base_key = product.id
            custom = False
        elif item.name and item.ingredients:
            custom_count += 1
            ingredients: list[ProductIngredient] = []
            for ia in item.ingredients:
                ing = catalog.resolve_ingredient(ia.name)
                if ing is None:
                    unknown.append(ia.name)
                    continue
                ingredients.append(ProductIngredient(id=ing.id, amount=ia.amount, unit=ia.unit))
            basis_text = "1회" if item.amount_basis == "per_serving" else "1일"
            product = Product(
                id=f"custom-{custom_count}",
                name=item.name,
                brand="직접 입력",
                sold_on_platform=False,
                amount_basis=item.amount_basis,
                servings_per_day=item.servings_per_day,
                daily_dose_text=f"1일 {item.servings_per_day:g}회 (함량: {basis_text} 섭취량 기준 입력)",
                ingredients=ingredients,
                price_per_day=0,
            )
            base_key = product.id
            custom = True
        else:
            raise InputError("각 제품은 product_id 또는 name과 ingredients를 함께 보내야 해요.")

        dose = catalog.normalize_dose(product)
        unknown += [f"{e}" for e in dose.errors]
        servings = item.servings
        if item.actual_servings_per_day is not None:
            servings = item.actual_servings_per_day / product.servings_per_day

        key = base_key
        n = 2
        while key in used_keys:
            key = f"{base_key}#{n}"
            n += 1
        used_keys.add(key)
        resolved.append(
            ResolvedItem(
                key=key,
                product=product,
                servings=servings,
                locked=item.locked,
                custom=custom,
                input=item,
                amounts={k: v * servings for k, v in dose.per_day.items()},
                unknown_ingredients=unknown,
                dose_lines=dose.lines,
            )
        )
    return resolved


def platform_item(catalog: Catalog, product_id: str, servings: float = 1.0, locked: bool = False) -> ResolvedItem:
    product = catalog.products[product_id]
    return ResolvedItem(
        key=product.id,
        product=product,
        servings=servings,
        locked=locked,
        custom=False,
        input=ItemInput(product_id=product.id, servings=servings, locked=locked),
        amounts=catalog.product_amounts(product, servings),
        dose_lines=catalog.normalize_dose(product).lines,
    )


def with_unique_key(item: ResolvedItem, existing: list[ResolvedItem]) -> ResolvedItem:
    keys = {i.key for i in existing}
    key, n = item.key, 2
    while key in keys:
        key = f"{item.product.id}#{n}"
        n += 1
    item.key = key
    return item
