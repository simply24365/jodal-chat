#!/usr/bin/env python3
"""P2b: Stage solver (결정적, LLM 불사용). 설계서 §5.

입력: 쿼리 + 파서 출력 + w2d 카탈로그.
  S0 명시 하드 제거 (channel/item_scope/visual positivie 불일치, dims 교집합 0)
     + excluded_dims 겹침은 감점 0.5 (제거 아님).
  S1 family narrowing (hard=통과만, strong=+2, weak=+0.5, none=스킵).
  S2 dims coverage = |Q∩D|/|Q| (Q 없으면 1.0). extra 무벌점, 동점용 specificity -0.1.
  S3 metric coverage (+1/-1). "단독" 엄격 요구는 No Match 사유 후보로 기록만.
  S4 soft (±0.3 상한): visual 언급 +0.2, 조회수 로그 +0.1.
  S5 text similarity: RRF 점수 × 0.5 합산 (폴백).
출력: [(rid, score, log[{stage, reason}])...] 내림차순.
"""
import json
import math
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = API_ROOT / "data"
CATALOG_PATH = BASE_DIR / "catalog" / "report_catalog_w2d.json"
WEIGHTS = json.loads((BASE_DIR / "stage_weights.json").read_text(encoding="utf-8"))

_catalog = None
_concepts_cache = None


def _concepts():
    global _concepts_cache
    if _concepts_cache is None:
        _concepts_cache = {c["id"]: c for c in json.loads(
            (BASE_DIR / "catalog" / "concepts.json").read_text(encoding="utf-8")).get("concepts", [])}
    return _concepts_cache


_concepts_cache = None


def _cat():
    global _catalog
    if _catalog is None:
        _catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog


def _views(r):
    # 조회수 항 제거됨 (S4). 호환용 잔재 — 사용처 없음.
    return 0


def _qnorm(s):
    import re
    return re.sub(r"\s+", "", str(s or "").lower())


def solve(parsed, rrf_scores=None, pool=50, top_k=10, consensus_rid=None, qtext=""):
    """rrf_scores: {rid: rrf_score} (S5용, 없으면 S5 스킵).
    consensus_rid: BM25·Vector 양쪽 1위 일치 rid (컬럼감점 면제 — 텍스트 확정 신호).
    qtext: 원문 쿼리 (S1.5 이름 직접언급 보너스용)."""
    W = WEIGHTS
    out = []
    for r in _cat():
        rid = r["report_id"]
        score, log = 0.0, []
        dead = False

        # S0 (null=미상=통과. dims 킬은 명시 dims에만 — 주제어 오살 방지)
        if parsed.get("channel") and r.get("channel") and r.get("channel") != parsed["channel"]:
            log.append({"stage": 0, "reason": f"channel 불일치({r.get('channel')})"})
            dead = True
        if parsed.get("item_scope") and r.get("item_scope") and r.get("item_scope") != parsed["item_scope"]:
            log.append({"stage": 0, "reason": "item_scope 불일치"})
            dead = True
        if parsed.get("visual") and not r.get("is_visual"):
            log.append({"stage": 0, "reason": "visual 아님"})
            dead = True
        xdims = parsed.get("dims_explicit", parsed.get("dims") or [])
        if xdims and not (set(xdims) & set(r.get("dims") or [])):
            log.append({"stage": 0, "reason": "dims 교집합 0"})
            dead = True
        if dead:
            out.append((rid, -1e9, log))
            continue
        if set(parsed.get("excluded_dims") or []) & set(r.get("dims") or []):
            score -= W["EXCLUDED_PENALTY"]
            log.append({"stage": 0, "reason": "excluded 겹침 감점"})

        # S1
        fam = (parsed.get("family") or {})
        if fam.get("confidence") == "hard":
            if r.get("family") == fam.get("value"):
                log.append({"stage": 1, "reason": "family hard 통과"})
            else:
                log.append({"stage": 1, "reason": "family hard 불일치"})
                dead = True
        elif fam.get("confidence") == "strong":
            if r.get("family") == fam.get("value"):
                score += W["S1_STRONG"]
                log.append({"stage": 1, "reason": "family strong +2"})
        elif fam.get("confidence") == "weak":
            if r.get("family") == fam.get("value"):
                score += W["S1_WEAK"]
                log.append({"stage": 1, "reason": "family weak +0.5"})
        if dead:
            out.append((rid, -1e9, log))
            continue

        # S1.5 이름 직접언급 보너스 (+2). 쿼리에 보고서명 전체가 있거나,
        # 6자 이상 쿼리핵이 보고서명에 포함되면 (정확 지목 신호).
        import re as _re2
        _qc = _re2.sub(r"(보여줘|알려줘|찾아줘|궁금해|궁금합니다|주세요)$", "", _qnorm(qtext))
        _nm = _qnorm(r.get("보고서명") or "")
        if (_nm and _nm in _qnorm(qtext)) or (len(_qc) >= 6 and _qc in _nm):
            score += W["NAME_MENTION_BONUS"]
            log.append({"stage": 1, "reason": "이름 직접언급 +2"})

        # S2 (명시 dims만 점수화. 주제어 오탐은 점수 미반영 — S0와 동일 원칙)
        qd = parsed.get("dims_explicit", parsed.get("dims") or [])
        if qd:
            cov = len(set(qd) & set(r.get("dims") or [])) / len(qd)
            score += cov
            log.append({"stage": 2, "reason": f"dims coverage {cov:.2f}"})
        else:
            log.append({"stage": 2, "reason": "query dims 없음, 통과"})

        # S2c non-split 기본값: 쿼리에 명시 dims가 없으면 안 나눈 버전 우대.
        # (dims_explicit 기준. 암시적 dims만으로는 발동 안 함)
        if not parsed.get("dims_explicit"):
            if not (r.get("dims") or []):
                score += W.get("NO_SPLIT_BONUS", 0.5)
                log.append({"stage": 2, "reason": "non-split 기본값"})
        # S2b 개념 커버리지: 태그된 개념의 label/alias가 보고서 컬럼에 있으면 +0.3
        colnames = [c["name"] for c in (r.get("columns") or [])
                    if not (c.get("w2c") or {}).get("quarantine")]
        for cid in (parsed.get("concepts") or []):
            c = _concepts().get(cid) or {}
            terms = [c.get("label")] + list(c.get("aliases") or [])
            if any(t and any(t in n or n in t for n in colnames) for t in terms if t):
                score += W["CONCEPT_HIT_BONUS"]
                log.append({"stage": 2, "reason": f"개념 적중 {cid}"})

        # S3
        qm = parsed.get("metric") or []
        if qm:
            mets = r.get("metrics") or []
            hit = [m for m in qm if any(m in x or x in m for x in mets)]
            if hit:
                score += W["S3_HIT"]
                log.append({"stage": 3, "reason": f"metric 적중 {hit}"})
            else:
                score -= abs(W["S3_MISS"])
                log.append({"stage": 3, "reason": "metric 없음 감점"})

        # S4 (visual 명시 + 모호 쿼리 한정 조회수 prior.
        #  명시 슬롯 있으면 텍스트가 결정, 없으면 인기도 prior 허용)
        s4 = 0.0
        if parsed.get("visual") and r.get("is_visual"):
            s4 += W["S4_VISUAL_BONUS"]
        fam = parsed.get("family") or {}
        has_slot = (fam.get("value") or (parsed.get("dims_explicit") or [])
                    or (parsed.get("metric") or []) or parsed.get("channel")
                    or (parsed.get("concepts") or []))
        if not has_slot:
            try:
                s4 += min(0.05, math.log10(int(str(r.get("조회수") or 0)) + 1) / 120)
            except Exception:
                pass
        # 컬럼정보 없는 보고서는 답변 순위에서 감점 (참고용으로는 허용).
        # 단 BM25·Vector 양쪽 1위 일치면 면제 (텍스트 확정 신호).
        if r.get("columns_missing") and rid != consensus_rid:
            s4 -= W["S4_NO_COLUMNS_PENALTY"]
            log.append({"stage": 4, "reason": "컬럼정보 없음 감점"})
        s4 = max(-W["S4_CAP"], min(W["S4_CAP"], s4))
        score += s4
        log.append({"stage": 4, "reason": f"soft {s4:+.2f}"})

        # S5 (RRF 폴백. 동점군 내 텍스트 변별용이라 coverage 스텝보다 작게)
        if rrf_scores and rid in rrf_scores:
            score += W.get("S5_W", 4.0) * rrf_scores[rid]
            log.append({"stage": 5, "reason": "rrf 가산"})

        # 동점용 specificity (S2 보조): doc dims 적을수록 +0.1 한도... 설계상 tie-break용이므로
        # 순위 계산 후 별도 처리. 여기서는 기록만.
        out.append((rid, score, log))

    out.sort(key=lambda x: -x[1])
    # specificity tie-break: 상위 20위 내 동점군(차 < 1e-9)에서 dims 적은 쪽 +0.1
    by_id = {r["report_id"]: r for r in _cat()}
    scored = out[:20]
    i = 0
    while i < len(scored):
        j = i + 1
        while j < len(scored) and abs(scored[j][1] - scored[i][1]) < 1e-9:
            j += 1
        if j - i > 1:
            grp = sorted(scored[i:j], key=lambda x: len(by_id[x[0]].get("dims") or []))
            scored[i:j] = [(rid, s + (W["SPEC_PENALTY"] if k == 0 else 0),
                            log + ([{"stage": 2, "reason": "specificity +0.1"}] if k == 0 else []))
                           for k, (rid, s, log) in enumerate(grp)]
        i = j
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


if __name__ == "__main__":
    import sys

    # CLI 단독 실행 전용 (앱에서는 __main__ 이 아니다).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from slot_parser import parse
    for q in sys.argv[1:] or ["지역별 계약 현황", "벤처나라 지역별 계약 현황", "수의계약 실적"]:
        p = parse(q)
        top = solve(p, top_k=5)
        print(f"Q: {q} -> {p['dims']}/{p['family']}")
        for rid, s, log in top:
            print(f"  {rid} {s:.3f} {[l['stage'] for l in log]}")
