"""제품 표시사항 CSV → 엔진 제품 데이터(JSON) 변환.

사용법:
  .venv/bin/python -m tools.import_products validation/products_template.csv \
      --out wellink_engine/data/products.d/market.json

CSV는 '성분 1개 = 1행'이고, 제품 정보(이름·섭취 방법 등)는 그 제품의 첫 행에만 적으면 됩니다.
열 설명은 validation/README.md를 참고하세요.

성분명은 ingredients.json의 이름·별칭으로 찾습니다. 찾지 못한 성분이나 환산할 수 없는 단위는
목록으로 알려 주고 그 성분은 빼고 저장합니다(--strict면 저장하지 않음).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from wellink_engine.catalog import Catalog, Product, get_catalog
from wellink_engine.units import UnitError, to_canonical

PRODUCT_FIELDS = [
    "product_name", "brand", "sold_on_platform", "daily_dose_text", "amount_basis",
    "servings_per_day", "price_per_day", "timing", "allergens", "cautions",
]


def _bool(v: str) -> bool:
    return v.strip().lower() in ("true", "1", "y", "yes", "예", "o")


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def convert(rows: list[dict], catalog: Catalog) -> tuple[list[dict], list[str]]:
    products: dict[str, dict] = {}
    problems: list[str] = []
    for n, row in enumerate(rows, start=2):  # 1행은 머리글
        pid = (row.get("product_id") or "").strip()
        if not pid:
            problems.append(f"{n}행: product_id가 비어 있어요.")
            continue
        prod = products.get(pid)
        if prod is None:
            if not (row.get("product_name") or "").strip():
                problems.append(f"{n}행: 제품 '{pid}'의 첫 행에 product_name이 필요해요.")
                continue
            allergens = []
            for a in _split(row.get("allergens", "")):
                aid = catalog.match_allergen(a)
                if aid:
                    allergens.append(aid)
                else:
                    problems.append(f"{n}행: 알레르기 '{a}'를 인식하지 못했어요 (products.json의 allergens에 추가 필요).")
            prod = {
                "id": pid,
                "name": row["product_name"].strip(),
                "brand": (row.get("brand") or "").strip() or None,
                "sold_on_platform": _bool(row.get("sold_on_platform", "false")),
                "daily_dose_text": (row.get("daily_dose_text") or "").strip() or None,
                "amount_basis": (row.get("amount_basis") or "per_day").strip(),
                "servings_per_day": float(row.get("servings_per_day") or 1),
                "price_per_day": int(float(row.get("price_per_day") or 0)),
                "timing": (row.get("timing") or "any").strip(),
                "ingredients": [],
                "allergens": allergens,
                "cautions": _split(row.get("cautions", "")),
            }
            products[pid] = prod
        name = (row.get("ingredient") or "").strip()
        if not name:
            continue
        ing = catalog.resolve_ingredient(name)
        if ing is None:
            problems.append(f"{n}행 [{pid}]: 성분 '{name}'을(를) 찾지 못했어요 (ingredients.json에 별칭·성분 추가 필요).")
            continue
        unit = (row.get("unit") or "").strip() or ing.unit
        try:
            amount = float(str(row.get("amount", "")).replace(",", ""))
            to_canonical(amount, unit, ing.unit, ing.iu_factor)
        except (ValueError, UnitError) as e:
            problems.append(f"{n}행 [{pid}]: {name} {row.get('amount')} {unit} → 환산 불가 ({e})")
            continue
        prod["ingredients"].append({"id": ing.id, "amount": amount, "unit": unit})

    out = []
    for prod in products.values():
        try:
            Product(**prod)
        except Exception as e:  # 형식 오류(amount_basis, timing 값 등)
            problems.append(f"제품 '{prod['id']}': 형식 오류 — {e}")
            continue
        if not prod["ingredients"]:
            problems.append(f"제품 '{prod['id']}': 인식한 성분이 없어 제외했어요.")
            continue
        out.append(prod)
    return out, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path)
    ap.add_argument("--out", type=Path, default=Path("wellink_engine/data/products.d/market.json"))
    ap.add_argument("--strict", action="store_true", help="문제가 하나라도 있으면 저장하지 않음")
    args = ap.parse_args(argv)

    catalog = get_catalog()
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    products, problems = convert(rows, catalog)

    # 같은 출력 파일을 다시 만드는 경우는 겹침으로 보지 않는다
    previous_ids: set[str] = set()
    if args.out.exists():
        with open(args.out, encoding="utf-8") as f:
            previous_ids = {p["id"] for p in json.load(f).get("products", [])}
    clash = [p["id"] for p in products if p["id"] in catalog.products and p["id"] not in previous_ids]
    for pid in clash:
        problems.append(f"제품 ID '{pid}'가 기존 데이터와 겹쳐요. 다른 ID를 쓰세요.")
    if clash:
        print("제품 ID가 겹쳐 저장하지 않았어요:", ", ".join(clash))
        return 1

    print(f"제품 {len(products)}개 변환, 확인 필요 {len(problems)}건")
    for p in problems:
        print("  -", p)
    if problems and args.strict:
        print("--strict: 저장하지 않았어요.")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"version": "market-import", "products": products}, f, ensure_ascii=False, indent=2)
    print(f"저장: {args.out}")
    # 저장한 데이터를 포함해 카탈로그가 정상적으로 읽히는지 확인
    try:
        if args.out.parent.name == "products.d":
            Catalog(args.out.parent.parent)
    except ValueError as e:
        print("카탈로그 검증 실패:\n" + str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
