#!/usr/bin/env python3
"""P1: family 주석. report_catalog_w2c.json -> report_catalog_w2d.json.

규칙 (결정적, LLM 0회):
  정규화 v2: HTML엔티티 디코드 → 품명범위 제거 → X별 제거 → 채널 분리 →
            표현 접미사 제거 → family_overrides.json 병합/분할/개명 적용.
  dims: 이름의 X별 ∩ dim_vocab.json (순서 유지, 중복 제거).
  channel: 이름에 채널 키워드 포함 시 (첫 매치).
  item_scope: "(118개 품명)" -> "118", "(전체 품명)" -> "all", else null.
  is_visual: "시각화" 포함 여부.
출력 필드: family, dims, channel, item_scope, is_visual, family_conf(manual|rule).
assert: 131건, family null 금지 (미매칭이면 실패로 중단).
"""
import html
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json"
DIM_VOCAB = json.loads((BASE_DIR / "data" / "dim_vocab.json").read_text(encoding="utf-8"))["dims"]
OVERRIDES = json.loads((BASE_DIR / "data" / "family_overrides.json").read_text(encoding="utf-8"))

CHANNELS = ["나라장터", "벤처나라", "혁신장터", "자체전자조달시스템"]
SUFFIXES = ["실적통계", "시각화", "내역", "순위", "현황", "통계", "추이통계",
            "상세실적통계", "증감실적통계", "기업현황", "발주", "수주", "실적"]
SUFFIX_RE = re.compile("|".join(sorted(SUFFIXES, key=len, reverse=True)))


def norm_family(name):
    s = html.unescape(name)
    s = re.sub(r"\((118개 품명|전체 품명)\)", "", s)
    s = re.sub(r"\S+별", "", s)
    for ch in CHANNELS:
        s = s.replace(ch, "")
    s = SUFFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def annotate(name):
    channel = next((c for c in CHANNELS if c in name), None)
    item_scope = "118" if "118개 품명" in name else ("all" if "전체 품명" in name else None)
    is_visual = "시각화" in name
    dims = []
    for m in re.finditer(r"(\S+?)별", name):
        if m.group(1) in DIM_VOCAB and m.group(1) not in dims:
            dims.append(m.group(1))
    fam = norm_family(name)
    conf = "rule"
    # splits: 정규화명이 분할 대상이면 원본명 키워드로 판별 (첫 매치 우선)
    if fam in OVERRIDES.get("splits", {}):
        for rule in OVERRIDES["splits"][fam]:
            if rule["contains"] in name:
                return {"family": rule["family"], "dims": dims, "channel": channel,
                        "item_scope": item_scope, "is_visual": is_visual,
                        "family_conf": "manual"}
    for old, new in OVERRIDES.get("renames", {}).items():
        if fam == old:
            fam, conf = new, "manual"
    for target, srcs in OVERRIDES.get("merges", {}).items():
        if fam in srcs:
            fam, conf = target, "manual"
    return {"family": fam, "dims": dims, "channel": channel,
            "item_scope": item_scope, "is_visual": is_visual, "family_conf": conf}


def main():
    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    assert len(catalog) == 131, f"레코드 {len(catalog)} != 131"
    out = []
    for r in catalog:
        a = annotate(r.get("보고서명") or "")
        assert a["family"], f"family 미매칭: {r['report_id']} {r.get('보고서명')}"
        rec = dict(r)
        rec.update(a)
        out.append(rec)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    from collections import Counter
    print(f"[P1] records={len(out)} -> {OUT_PATH}")
    print("family 수:", len(set(r["family"] for r in out)))
    print("manual:", sum(1 for r in out if r["family_conf"] == "manual"))
    print("dims top:", Counter(d for r in out for d in r["dims"]).most_common(8))


if __name__ == "__main__":
    main()
