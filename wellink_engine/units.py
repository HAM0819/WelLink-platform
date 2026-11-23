"""단위 환산. 사용자가 입력한 타사 제품 함량을 성분별 기준 단위로 바꾼다."""

from __future__ import annotations

import unicodedata

# 질량 단위 → μg
_MASS_TO_UG = {"g": 1_000_000.0, "mg": 1_000.0, "μg": 1.0}
_MASS_ALIASES = [("mcg", "μg"), ("μg", "μg"), ("ug", "μg"), ("mg", "mg"), ("g", "g")]


class UnitError(ValueError):
    pass


def _clean(unit: str) -> str:
    u = unicodedata.normalize("NFKC", unit).strip().lower().replace(" ", "")
    return u.replace("µ", "μ")


def _mass_unit(u: str) -> str | None:
    for prefix, canonical in _MASS_ALIASES:
        if u.startswith(prefix):
            return canonical
    return None


def to_canonical(amount: float, unit: str, canonical_unit: str, iu_factor: float | None = None) -> float:
    """amount(unit)를 성분의 기준 단위(canonical_unit) 값으로 환산한다.

    - 질량: g / mg / μg(mcg, ug) 상호 환산. 'μg RAE', 'mg α-TE'처럼 뒤에 붙은 표기는 무시한다.
    - IU: 성분별 iu_factor(1 IU당 기준 단위 값)가 있을 때만 환산한다.
    - 균수: CFU, 억CFU
    """
    u = _clean(unit)
    target = _clean(canonical_unit)

    # 균수
    if target == "억cfu":
        if u in ("억cfu", "억", "억개"):
            return amount
        if u in ("cfu", "개"):
            return amount / 1e8
        raise UnitError(f"균수 단위를 알 수 없어요: {unit}")

    # IU
    if u.startswith("iu"):
        if iu_factor is None:
            raise UnitError(f"이 성분은 IU 단위를 환산할 수 없어요: {unit}")
        return amount * iu_factor

    src = _mass_unit(u)
    dst = _mass_unit(target)
    if src is None or dst is None:
        raise UnitError(f"단위를 알 수 없어요: {unit}")
    return amount * _MASS_TO_UG[src] / _MASS_TO_UG[dst]
