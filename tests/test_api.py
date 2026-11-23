from fastapi.testclient import TestClient

from wellink_engine.api import app

client = TestClient(app)


def test_health_and_catalog():
    assert client.get("/health").json()["status"] == "ok"
    goals = client.get("/v1/catalog/goals").json()
    assert {"id": "fatigue", "name": "피로·컨디션 관리"} in goals
    products = client.get("/v1/catalog/products", params={"platform_only": True}).json()
    assert products and all(p["sold_on_platform"] for p in products)
    assert client.get("/v1/catalog/drugs", params={"q": "와파"}).json()[0]["name"] == "와파린"
    assert client.get("/v1/catalog/ingredients", params={"q": "비타민d"}).json()[0]["accepts_iu"] is True


def test_recommend_then_check_flow():
    profile = {"age": 34, "sex": "male", "goals": ["bone", "cardio"], "medications": ["아스피린"]}
    rec = client.post("/v1/recommendations", json={
        "profile": profile,
        "current_products": [{"product_id": "ext_multi"}, {"product_id": "ext_calmagd"}],
    })
    assert rec.status_code == 200
    body = rec.json()
    assert body["report"]["summary"]["overall_label"]
    assert body["combination"][0]["product_id"] == "ext_multi"

    # 사용자가 수정안을 적용한 조합을 그대로 재점검
    revised_items = body["revision"]["items"]
    chk = client.post("/v1/checks", json={"profile": profile, "items": revised_items})
    assert chk.status_code == 200
    assert chk.json()["report"]["summary"]["counts_by_severity"]["warning"] == 0


def test_validation_errors():
    bad_goal = client.post("/v1/checks", json={
        "profile": {"age": 30, "sex": "male", "goals": ["nope"]}, "items": [{"product_id": "wl_multi"}]})
    assert bad_goal.status_code == 422
    bad_product = client.post("/v1/checks", json={
        "profile": {"age": 30, "sex": "male", "goals": ["fatigue"]}, "items": [{"product_id": "nope"}]})
    assert bad_product.status_code == 422
    minor = client.post("/v1/checks", json={
        "profile": {"age": 15, "sex": "male", "goals": ["fatigue"]}, "items": [{"product_id": "wl_multi"}]})
    assert minor.status_code == 422


def test_demo_page_served():
    r = client.get("/")
    assert r.status_code == 200 and "WelLink" in r.text
