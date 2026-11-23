"""조합 항목(ItemInput)을 카탈로그 제품과 성분 함량으로 해석한다."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, Product, ProductIngredient
from .context import InputError
from .models import ItemInput
from .units import UnitError, to_canonical


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
                try:
                    amount = to_canonical(ia.amount, ia.unit, ing.unit, ing.iu_factor)
                except UnitError as e:
                    unknown.append(f"{ia.name} ({e})")
                    continue
                ingredients.append(ProductIngredient(id=ing.id, amount=amount))
            product = Product(
                id=f"custom-{custom_count}",
                name=item.name,
                brand="직접 입력",
                sold_on_platform=False,
                ingredients=ingredients,
                price_per_day=0,
            )
            base_key = product.id
            custom = True
        else:
            raise InputError("각 제품은 product_id 또는 name과 ingredients를 함께 보내야 해요.")

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
                servings=item.servings,
                locked=item.locked,
                custom=custom,
                input=item,
                amounts=catalog.product_amounts(product, item.servings),
                unknown_ingredients=unknown,
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
    )


def with_unique_key(item: ResolvedItem, existing: list[ResolvedItem]) -> ResolvedItem:
    keys = {i.key for i in existing}
    key, n = item.key, 2
    while key in keys:
        key = f"{item.product.id}#{n}"
        n += 1
    item.key = key
    return item
