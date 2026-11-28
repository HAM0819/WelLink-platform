"""문자열 정규화와 한국어 조사 처리."""

from __future__ import annotations

import re
import unicodedata

_STRIP_CHARS = re.compile(r"[\s\-_·•()\[\]{},./+]+")


def normalize(text: str) -> str:
    """검색·별칭 비교용 정규화: 전각/마이크로 기호 통일, 소문자, 공백·기호 제거."""
    text = unicodedata.normalize("NFKC", text).lower()
    return _STRIP_CHARS.sub("", text)


# 영문자·숫자를 한국어로 읽었을 때 받침 유무 (예: D=디 → 없음, L=엘 → 있음, 0=영 → 있음)
_LATIN_FINAL = set("lmnr")
_DIGIT_FINAL = set("013678")


# 'mg', 'μg'처럼 '그램'으로 읽히는 단위는 받침(ㅁ)이 있다
_GRAM_UNITS = ("mg", "μg", "µg", "kg", "mcg")


def _has_final_consonant(word: str) -> tuple[bool, bool]:
    """(받침 있음, 받침이 ㄹ인지)."""
    stripped = word.strip()
    lower = stripped.lower()
    if lower.endswith(_GRAM_UNITS) or (lower.endswith("g") and len(lower) > 1 and lower[-2].isdigit()):
        return True, False
    for ch in reversed(stripped):
        if ch.isspace() or ch in ")]}'\"":
            continue
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            final = (code - 0xAC00) % 28
            return final != 0, final == 8
        lower = ch.lower()
        if lower.isdigit():
            return lower in _DIGIT_FINAL, lower in "178"
        if lower.isalpha():
            return lower in _LATIN_FINAL, lower in "lr"
        # 그 밖의 기호는 건너뛴다
    return False, False


def josa(word: str, pair: str) -> str:
    """단어에 알맞은 조사를 붙인다. pair 예: '을/를', '이/가', '은/는', '과/와', '으로/로'."""
    has_final, is_rieul = _has_final_consonant(word)
    first, second = pair.split("/")
    if pair == "으로/로":
        return word + ("로" if (not has_final or is_rieul) else "으로")
    return word + (first if has_final else second)


def fmt_amount(value: float, unit: str) -> str:
    """숫자를 보기 좋게 표시 (불필요한 소수점 제거, 천 단위 구분)."""
    if value >= 100 or float(value).is_integer():
        text = f"{value:,.0f}"
    elif value >= 10:
        text = f"{value:,.1f}".rstrip("0").rstrip(".")
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"
