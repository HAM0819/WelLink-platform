# 시판 제품 등록 · 페르소나 검증

실제 시판 제품의 표시 정보를 넣고, 가상의 사용자(페르소나)에게 추천·점검 결과가 적절하게 나오는지 확인하는 절차입니다.

## 1. 제품 표시 정보를 CSV로 정리

`products_template.csv`를 복사해 채웁니다(엑셀에서 열고 CSV UTF-8로 저장). **성분 1개 = 1행**이고, 제품 정보는 그 제품의 첫 행에만 적습니다.

| 열 | 내용 | 예 |
|---|---|---|
| `product_id` | 영문·숫자로 된 고유 ID (같은 제품의 행은 같은 ID) | `mk_brand_multi` |
| `product_name` | 제품명 | 예시 종합비타민 |
| `brand` | 제조·판매사 | |
| `sold_on_platform` | 플랫폼에서 팔 제품이면 `true`, 사용자가 이미 먹는 타사 제품 예시면 `false` | `false` |
| `daily_dose_text` | 표시된 섭취 방법 그대로 | 1일 2회, 1회 1정 |
| `amount_basis` | 함량이 **1일 섭취량당**이면 `per_day`, **1회 섭취량당**이면 `per_serving` | `per_serving` |
| `servings_per_day` | 표시된 1일 섭취 횟수 | `2` |
| `price_per_day` | 하루 가격(원), 모르면 0 | |
| `timing` | 권장 시간 `morning` / `evening` / `any` | |
| `allergens` | 알레르기 유발 원료, 여러 개는 `|`로 구분 | 우유\|대두 |
| `cautions` | 섭취 시 주의사항 문구, 여러 개는 `|`로 구분 | |
| `ingredient` | 성분명 (한글·영문·원료명 모두 가능) | 비타민D, 콜레칼시페롤 |
| `amount` | 표시 함량 숫자 | `400` |
| `unit` | 표시 단위 그대로 | `IU`, `mg`, `μg`, `mcg`, `g`, `CFU`, `억CFU` |

단위와 1회/1일 기준이 제품마다 달라도 그대로 적으면 됩니다. 엔진이 모두 **1일 기준 · 같은 단위**로 바꾸고, 결과 화면과 보고서에 환산 과정(예: `1회 400IU × 1일 2회 = 20μg`)을 남깁니다. 사용자가 표시와 다르게 먹는 경우(예: 1일 2회 제품을 1회만)는 앱에서 `actual_servings_per_day`로 받습니다.

## 2. 엔진 데이터로 변환

```bash
.venv/bin/python -m tools.import_products 내파일.csv --out wellink_engine/data/products.d/market.json
```

- 인식하지 못한 성분명, 환산할 수 없는 단위, 형식 오류를 행 번호와 함께 알려 줍니다. 그 성분은 빼고 저장합니다(`--strict`를 붙이면 문제가 있을 때 저장하지 않음).
- 성분을 못 찾으면 `wellink_engine/data/ingredients.json`에 별칭을 추가하거나 새 성분을 등록하세요.
- `products.d/` 폴더의 JSON은 서버가 자동으로 함께 읽습니다.

## 3. 페르소나 정의와 검증

`personas.example.json`을 참고해 페르소나를 만듭니다.

- `profile`: 나이·성별·목적·가족력·복용 약 등 (API의 profile과 같음)
- `current_products`: 지금 먹는 제품 (`product_id` 또는 직접 입력)
- `mode`: `recommend`(기본, 추천 후 점검) 또는 `check`(current_products 조합만 점검)
- `options`: `budget_per_day`, `max_products` 등 추천 옵션
- `expect`: 기대 조건

| 기대 조건 | 의미 |
|---|---|
| `recommend_includes_any` / `recommend_includes_all` | 추천에 이 제품(들)이 들어가야 함 |
| `recommend_excludes` | 추천에 들어가면 안 되는 제품 |
| `excluded_with_reason` | 이유와 함께 '제외'로 표시돼야 하는 제품 |
| `max_recommended` | 추천 제품 수 상한 |
| `findings_include` / `findings_exclude` | 점검 결과에 있어야(없어야) 하는 항목. `type`, `ingredient`, `severity`, `title_contains`로 지정 |
| `overall_in` | 종합 판정(`ok`, `review`, `adjust`, `avoid`) |
| `all_goals_covered` | 모든 목적이 충족돼야 함 |
| `ingredient_total` | 성분 하루 합계 범위, 예: `{"vitamin_d": {"max": 100}}` |

```bash
.venv/bin/python -m tools.validate_personas validation/personas.example.json --out validation/report.md
```

보고서(`report.md`)에는 페르소나별로 기대 조건 일치 여부, 추천 제품과 이유, 제외한 제품, 점검 결과, 자동 수정안, 제품별 환산 과정이 정리됩니다. 기대와 다르면 ❌로 표시되고 종료 코드 1을 반환합니다.

`findings_include`의 `type` 값: `excess`(과다), `near_upper`(상한 근접), `duplicate`(중복), `below_effective`(기능성 기준량 미달), `nutrient_low`(우선 영양소 부족), `goal_gap`(목적 미충족), `drug_interaction`, `drug_timing`, `supplement_interaction`, `supplement_timing`, `profile_caution`, `allergy`, `unknown_drug`, `unknown_ingredient`.
