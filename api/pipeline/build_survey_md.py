#!/usr/bin/env python3
"""전수 상세 메타 → 공통/개별 분석 md 생성.

읽기: data/details131.json + public/reports131.json (네트워크 없음)
쓰기: docs/details-full-survey.md

구성:
  1. 전체 요약 (UI 분포, 형식별 조건 보유, 옵션값 총합)
  2. 공통 조건 Top 30 + 슬롯 매핑 제안
  3. 보고서별 전수 표 (조건 목록 포함)
  4. 개별 specific (고유 조건)
  5. 조건 0건 목록
  6. 계열 클러스터링 (조건 세트 Jaccard 유사도)
"""
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = json.loads((BASE / "data" / "details131.json").read_text("utf-8"))
REPORTS = {r["hubReptNo(reptId)"]: r for r in
           json.loads((BASE / "public" / "reports131.json").read_text("utf-8"))}
OUT = BASE / "docs" / "details-full-survey.md"

SLOT_RULES = [  # (정규식, 슬롯키) — 순서대로 매칭
    (r"일자.*from|from.*일자|\(from\)", "period_from"),
    (r"일자.*to|to.*일자|\(to\)", "period_to"),
    (r"기준일자|기준년도", "period_base"),
    (r"^(수요기관|발주기관|수요처)", "org_demand"),
    (r"^업체|공급업체|계약업체|납품업체", "org_vendor"),
    (r"중앙관서|최상위기관", "org_gov"),
    (r"물품분류", "item_class"),
    (r"세부품명", "item_name"),
    (r"물품식별", "item_id"),
    (r"시도", "region_sido"),
    (r"시군구", "region_sigungu"),
    (r"조달방식", "method_proc"),
    (r"업무구분", "method_work"),
    (r"소관구분", "method_juris"),
    (r"단가|금액", "amount"),
    (r"여부$", "flag"),
    (r"번호$", "code_no"),
]


def norm(t):
    t = re.sub(r"\(From\)|\(To\)", "", t).replace("_전일", "")
    return re.sub(r"\(.*", "", t).strip()


def slot_key(title):
    for pat, key in SLOT_RULES:
        if re.search(pat, title, re.I):
            return key
    return "etc"


def main():
    n = len(DATA)
    counts = sorted(len(v["prompts"]) for v in DATA.values())
    avg = sum(counts) / n
    zero = sorted(k for k, v in DATA.items() if not v["prompts"])
    ui = Counter(p["ui"] for v in DATA.values() for p in v["prompts"])
    norm_c = Counter((norm(p["title"]), p["ui"])
                     for v in DATA.values() for p in v["prompts"])
    slot_c = Counter(slot_key(p["title"])
                     for v in DATA.values() for p in v["prompts"])
    opts = sum(p["optsTotal"] for v in DATA.values()
               for p in v["prompts"])
    fmt_zero = Counter(REPORTS[k].get("mstrFrmt", "?") for k in zero)

    L = [f"# 보고서 상세 메타 전수 분석 ({n}건)\n",
         f"> 생성일: {date.today().isoformat()} "
         f"(`tools/build_survey_md.py` 자동 생성)\n",
         "## 1. 전체 요약\n",
         f"- 조건 있음: **{n - len(zero)}건**, 없음: **{len(zero)}건**",
         f"- 조건 수: 최소 {counts[0]}, 최대 {counts[-1]}, 평균 {avg:.1f}",
         f"- 고정 옵션값 총합: {opts:,}개",
         f"- 조건 0건 보고서 형식 분포: {dict(fmt_zero) or '없음'}\n",
         "### UI 타입 분포\n"]
    for u, c in ui.most_common():
        L.append(f"- `{u}`: {c}")

    L.append("\n### 슬롯 분포 (공통키 매핑)\n")
    L.append("| 슬롯키 | 조건 수 | 의미 |")
    L.append("|---|---|---|")
    slot_desc = {
        "period_from": "기간 From", "period_to": "기간 To",
        "period_base": "기준일/기준년도", "org_demand": "수요·발주기관",
        "org_vendor": "업체", "org_gov": "중앙관서·최상위기관",
        "item_class": "물품분류", "item_name": "세부품명",
        "item_id": "물품식별", "region_sido": "시도",
        "region_sigungu": "시군구", "method_proc": "조달방식",
        "method_work": "업무구분", "method_juris": "소관구분",
        "amount": "금액·단가", "flag": "Y/N 속성",
        "code_no": "번호 검색", "etc": "기타(개별 확인 필요)",
    }
    for s, c in slot_c.most_common():
        L.append(f"| `{s}` | {c} | {slot_desc.get(s, '')} |")

    L.append("\n### 공통 조건 Top 30 (정규화)\n")
    L.append("| 횟수 | 조건명 | UI | 슬롯 |")
    L.append("|---|---|---|---|")
    for (t, u), c in norm_c.most_common(30):
        L.append(f"| {c} | {t} | `{u}` | `{slot_key(t)}` |")

    L.append("\n## 2. 보고서별 전수\n")
    for rid in sorted(DATA):
        r = REPORTS.get(rid, {})
        v = DATA[rid]
        L.append(f"\n### {rid} — {r.get('보고서명', '')} "
                 f"({r.get('mstrFrmt', '')}, 조건 {len(v['prompts'])}개)")
        for p in v["prompts"]:
            o = f", 옵션 {p['optsTotal']}개" if p["optsTotal"] else ""
            L.append(f"- [{p['ui']}] {p['title']} "
                     f"→ `{slot_key(p['title'])}`{o}")

    L.append("\n## 3. 개별 specific (Top 30 밖 고유 조건)\n")
    top30 = {k for k, _ in norm_c.most_common(30)}
    for rid in sorted(DATA):
        uniq = [(p["title"], p["ui"], p["optsTotal"])
                for p in DATA[rid]["prompts"]
                if (norm(p["title"]), p["ui"]) not in top30]
        if uniq:
            L.append(f"\n### {rid} — "
                     f"{REPORTS.get(rid, {}).get('보고서명', '')}")
            for t, u, o in uniq:
                L.append(f"- [{u}] {t} (옵션 {o}개)")

    L.append("\n## 4. 조건 0건 보고서\n")
    if zero:
        for rid in zero:
            L.append(f"- {rid} — "
                     f"{REPORTS.get(rid, {}).get('보고서명', '')}")
    else:
        L.append("없음 (전수 모두 조건 확보).")

    L.append("\n## 5. 계열 클러스터링 (조건 세트 유사도)\n")
    L.append("> 정규화 조건명 집합의 Jaccard 유사도 ≥ 0.5 로 묶음. "
             "같은 묶음은 메타데이터 상속 후보.\n")
    sets = {k: {(norm(p["title"]), p["ui"]) for p in v["prompts"]}
            for k, v in DATA.items() if v["prompts"]}
    done, groups = set(), []
    for a in sorted(sets):
        if a in done:
            continue
        g = [a]
        done.add(a)
        for b in sorted(sets):
            if b in done or not sets[a] or not sets[b]:
                continue
            j = (len(sets[a] & sets[b]) /
                 len(sets[a] | sets[b]))
            if j >= 0.5:
                g.append(b)
                done.add(b)
        if len(g) > 1:
            groups.append(g)
    groups.sort(key=len, reverse=True)
    single = n - len(zero) - sum(map(len, groups))
    L.append(f"- 묶음 {len(groups)}개, 단독 {single}건\n")
    for g in groups[:30]:
        names = " / ".join(
            f"{k} {REPORTS.get(k, {}).get('보고서명', '')[:18]}" for k in g)
        L.append(f"\n### {len(g)}건 묶음\n- {names}")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"저장: {OUT} ({len(L)}줄, 묶음 {len(groups)}개)")


if __name__ == "__main__":
    sys.exit(main())
