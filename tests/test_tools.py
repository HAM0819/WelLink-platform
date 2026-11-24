import csv
import json
from pathlib import Path

from tools import import_products, validate_personas
from wellink_engine.catalog import get_catalog

ROOT = Path(__file__).resolve().parents[1]


def test_import_template_converts_label_formats():
    with open(ROOT / "validation/products_template.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    products, problems = import_products.convert(rows, get_catalog())
    assert problems == []
    calmag = next(p for p in products if p["id"] == "mk_example_calmag")
    assert calmag["amount_basis"] == "per_serving" and calmag["servings_per_day"] == 2
    assert calmag["allergens"] == ["milk"]
    assert {"id": "vitamin_d", "amount": 400.0, "unit": "IU"} in calmag["ingredients"]


def test_import_reports_unknown_ingredient_and_unit():
    rows = [
        {"product_id": "x1", "product_name": "테스트", "ingredient": "없는성분", "amount": "10", "unit": "mg"},
        {"product_id": "x1", "ingredient": "비타민C", "amount": "100", "unit": "알"},
        {"product_id": "x1", "ingredient": "비타민C", "amount": "100", "unit": "mg"},
    ]
    products, problems = import_products.convert(rows, get_catalog())
    assert len(products) == 1 and len(products[0]["ingredients"]) == 1
    assert any("없는성분" in p for p in problems) and any("환산 불가" in p for p in problems)


def test_example_personas_all_pass(tmp_path):
    out = tmp_path / "report.md"
    code = validate_personas.main([str(ROOT / "validation/personas.example.json"), "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "기대 조건 21개 중 21개 일치" in text


def test_persona_mismatch_is_reported(tmp_path):
    personas = {"personas": [{
        "name": "불일치 예시",
        "profile": {"age": 30, "sex": "male", "goals": ["eye"]},
        "expect": {"recommend_includes_all": ["wl_garcinia"]},
    }]}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(personas, ensure_ascii=False), encoding="utf-8")
    assert validate_personas.main([str(path), "--out", str(tmp_path / "r.md")]) == 1
