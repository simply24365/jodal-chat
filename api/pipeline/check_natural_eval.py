"""natural_queries.json 검증 — 깨진 문자열이 섞였는지 잡는다.

이 eval 을 만들 때 실제로 겪은 문제: 질의 문자열에 러시아어·중국어·타이어·깨진
한글이 섞여 들어갔다(예: "부산tho", "조달청 지청별로 활동 실적 stats", "地方国企").
그런 문자열은 어떤 임베딩도 절대 못 맞추므로 정답률을 부풀린다. 숫자로 드러나지
않으므로 사람이 읽어야만 발견된다 — 그래서 프로그램이 막는다.

  python -m pipeline.check_natural_eval
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1] / "data" / "eval" / "natural_queries.json"

# 허용: 한글(음성포함 Hangul), ASCII, 공백, 기본 구두점
ALLOWED_PUNCT = set(" ?.,!?:;()[]{}'\"-_%/·~^+=<>@#$&*0123456789")


def bad_chars(s: str) -> list[tuple[str, str]]:
    out = []
    for ch in s:
        if ch.isspace() or ch in ALLOWED_PUNCT:
            continue
        if "가" <= ch <= "힣":  # 한글 음성/자모
            continue
        if "ㄱ" <= ch <= "ㅎ" or "ㅏ" <= ch <= "ㅣ":
            continue
        name = unicodedata.name(ch, "?")
        script = "?"
        for key, label in (
            ("CYRILLIC", "러시아"),
            ("CJK", "중국/일본"),
            ("THAI", "타이"),
            ("HANGUL", None),
            ("LATIN", None),
            ("GREEK", "그리스"),
        ):
            if key in name:
                script = label
                break
        out.append((ch, script or name.split()[0]))
    return out


def main() -> int:
    if not EVAL.is_file():
        print(f"없음: {EVAL}")
        return 1
    data = json.loads(EVAL.read_text(encoding="utf-8"))
    qs = data.get("queries", [])
    fails = 0

    for i, item in enumerate(qs):
        q = item.get("q", "")
        bad = bad_chars(q)
        if bad:
            fails += 1
            print(f"[BAD] #{i} {item.get('type','?')}: {q!r}")
            for ch, why in bad:
                print(f"       '{ch}' = {why}")

    # latin+hangul 무 Mixing: 라틴 단어가 한글 문장에 붙어 있으면 흔한 오식
    for i, item in enumerate(qs):
        q = item.get("q", "")
        if re.search(r"[가-힣][A-Za-z]{2,}", q) or re.search(r"[A-Za-z]{2,}[가-힣]", q):
            fails += 1
            print(f"[MIX] #{i} 라틴/한글 오식 의심: {q!r}")

    # gold report_id 가 실제 카탈로그에 있는지
    cat_path = EVAL.parents[1] / "catalog" / "report_catalog_w2d.json"
    cat = {r["report_id"]: r for r in json.loads(cat_path.read_text(encoding="utf-8"))}
    for i, item in enumerate(qs):
        for rid in item.get("gold") or []:
            if rid not in cat:
                fails += 1
                print(f"[GOLD] #{i} 존재하지 않는 report_id: {rid}")

    n_ans = sum(1 for x in qs if x.get("gold") or x.get("gold_any"))
    n_none = sum(1 for x in qs if not x.get("gold") and not x.get("gold_any"))
    print(f"\n질의 {len(qs)}개 — 정답있음 {n_ans} / 정답없음(garbage) {n_none}")
    print("이상 없음" if not fails else f"문제 {fails}건")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
