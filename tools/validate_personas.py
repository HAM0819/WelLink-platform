"""페르소나 검증: 가상 사용자별로 추천·점검을 돌려 기대한 결과가 나오는지 확인하고 보고서를 만든다.

사용법:
  .venv/bin/python -m tools.validate_personas validation/personas.example.json \
      --out validation/report.md

페르소나 파일 형식은 validation/README.md를 참고하세요. 기대 조건(expect)을 하나라도
만족하지 못하면 보고서에 '불일치'로 표시하고 종료 코드 1을 반환합니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wellink_engine import service
from wellink_engine.catalog import get_catalog
from wellink_engine.context import InputError
from wellink_engine.models import CheckRequest, ItemInput, Profile, RecommendRequest


def _finding_matches(f, cond: dict) -> bool:
    if "type" in cond and f.type.value != cond["type"]:
        return False
    if "ingredient" in cond and cond["ingredient"] not in f.ingredient_ids:
        return False
    if "severity" in cond and f.severity.value != cond["severity"]:
        return False
    if "title_contains" in cond and cond["title_contains"] not in f.title:
        return False
    return True


def evaluate(expect: dict, rec, final_report) -> list[tuple[str, bool, str]]:
    """(조건 설명, 통과 여부, 실제 값) 목록."""
    results = []
    rec_ids = [p.product_id for p in rec.recommended] if rec else []
    findings = final_report.findings

    if "recommend_includes_any" in expect:
        want = expect["recommend_includes_any"]
        results.append((f"추천에 다음 중 하나 포함: {want}", any(w in rec_ids for w in want), str(rec_ids)))
    for pid in expect.get("recommend_includes_all", []):
        results.append((f"추천에 '{pid}' 포함", pid in rec_ids, str(rec_ids)))
    for pid in expect.get("recommend_excludes", []):
        results.append((f"추천에 '{pid}' 없음", pid not in rec_ids, str(rec_ids)))
    if "max_recommended" in expect:
        results.append((f"추천 제품 수 ≤ {expect['max_recommended']}", len(rec_ids) <= expect["max_recommended"], str(len(rec_ids))))
    for pid in expect.get("excluded_with_reason", []):
        ex = {e.product_id: e.reason for e in rec.excluded} if rec else {}
        results.append((f"'{pid}'를 이유와 함께 제외", pid in ex, ex.get(pid, "제외되지 않음")))
    for cond in expect.get("findings_include", []):
        hit = [f.title for f in findings if _finding_matches(f, cond)]
        results.append((f"점검 결과 포함: {cond}", bool(hit), "; ".join(hit) or "없음"))
    for cond in expect.get("findings_exclude", []):
        hit = [f.title for f in findings if _finding_matches(f, cond)]
        results.append((f"점검 결과 없음: {cond}", not hit, "; ".join(hit) or "없음"))
    if "overall_in" in expect:
        ov = final_report.summary.overall
        results.append((f"종합 판정이 {expect['overall_in']} 중 하나", ov in expect["overall_in"], ov))
    if expect.get("all_goals_covered"):
        miss = [g.name for g in final_report.goal_coverage if not g.covered]
        results.append(("모든 목적 충족", not miss, ", ".join(miss) or "모두 충족"))
    for iid, limits in expect.get("ingredient_total", {}).items():
        dist = {d.ingredient_id: d for d in final_report.distribution}
        total = dist[iid].total if iid in dist else 0.0
        ok = limits.get("min", float("-inf")) <= total <= limits.get("max", float("inf"))
        results.append((f"{iid} 하루 합계 범위 {limits}", ok, f"{total:g}"))
    return results


def run_persona(persona: dict) -> dict:
    profile = Profile(**persona["profile"])
    current = [ItemInput(**i) for i in persona.get("current_products", [])]
    mode = persona.get("mode", "recommend")
    rec = None
    if mode == "recommend":
        rec = service.recommend(RecommendRequest(profile=profile, current_products=current, **persona.get("options", {})))
        report = rec.report
        revision = rec.revision
    else:
        chk = service.check(CheckRequest(profile=profile, items=current))
        report, revision = chk.report, chk.revision
    checks = evaluate(persona.get("expect", {}), rec, report)
    return {"persona": persona, "rec": rec, "report": report, "revision": revision, "checks": checks}


def to_markdown(results: list[dict], catalog) -> str:
    lines = ["# 페르소나 검증 보고서", ""]
    total = sum(len(r["checks"]) for r in results)
    passed = sum(ok for r in results for _, ok, _ in r["checks"])
    lines += [f"- 페르소나 {len(results)}명, 기대 조건 {total}개 중 {passed}개 일치", ""]
    lines += ["| 페르소나 | 일치 | 종합 판정 | 추천 제품 |", "|---|---|---|---|"]
    for r in results:
        p = r["persona"]
        ok = sum(c[1] for c in r["checks"])
        recs = ", ".join(x.name for x in r["rec"].recommended) if r["rec"] else "(점검 모드)"
        lines.append(f"| {p['name']} | {ok}/{len(r['checks'])} | {r['report'].summary.overall_label} | {recs} |")
    for r in results:
        p, report = r["persona"], r["report"]
        pr = p["profile"]
        goals = ", ".join(catalog.goals[g].name for g in pr["goals"])
        lines += ["", f"## {p['name']}", ""]
        if p.get("description"):
            lines += [p["description"], ""]
        lines += [
            f"- 프로필: {pr['age']}세 {'남성' if pr['sex'] == 'male' else '여성'} / 목적: {goals}"
            + (f" / 가족력: {', '.join(pr.get('family_history', []))}" if pr.get("family_history") else "")
            + (f" / 복용 약: {', '.join(pr.get('medications', []))}" if pr.get("medications") else ""),
            f"- 현재 제품: {', '.join(i.name for i in report.items if i.source == 'external') or '없음'}",
            "",
            "**기대 조건**",
            "",
        ]
        for desc, ok, actual in r["checks"]:
            lines.append(f"- {'✅' if ok else '❌'} {desc} — 실제: {actual}")
        if r["rec"]:
            lines += ["", "**추천**", ""]
            for x in r["rec"].recommended:
                lines.append(f"- {x.name} (하루 {x.price_per_day:,}원): " + " / ".join(rs.text for rs in x.reasons))
            for e in r["rec"].excluded:
                lines.append(f"- 제외: {e.name} — {e.reason}")
        lines += ["", f"**점검 결과** ({report.summary.overall_label})", ""]
        for f in report.findings:
            lines.append(f"- [{f.severity_label}] {f.title}: {f.message}")
        if r["revision"] and r["revision"].steps:
            lines += ["", "**자동 수정안**", ""]
            for st in r["revision"].steps:
                lines.append(f"{st.order}. {st.suggestion.description}")
        lines += ["", "**성분 분포 (상위)**", "", "| 성분 | 하루 합계 | 상태 | 제품별 환산 |", "|---|---|---|---|"]
        for d in report.distribution[:8]:
            calc = "<br>".join(f"{s.product_name}: {s.calculation}" for s in d.sources)
            lines.append(f"| {d.name} | {d.total:g}{d.unit} | {d.status_label} | {calc} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("personas", type=Path)
    ap.add_argument("--out", type=Path, default=Path("validation/report.md"))
    args = ap.parse_args(argv)
    with open(args.personas, encoding="utf-8") as f:
        personas = json.load(f)["personas"]
    results = []
    for p in personas:
        try:
            results.append(run_persona(p))
        except InputError as e:
            print(f"[{p.get('name')}] 입력 오류: {e}")
            return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(to_markdown(results, get_catalog()), encoding="utf-8")
    failed = [(r["persona"]["name"], d) for r in results for d, ok, _ in r["checks"] if not ok]
    print(f"보고서: {args.out}  (불일치 {len(failed)}건)")
    for name, d in failed:
        print(f"  ❌ {name}: {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
