#!/usr/bin/env python3
"""P2a: 쿼리 슬롯 파서 (결정적, LLM 불사용).

출력:
  {"family": {"value"|None, "confidence": "hard|strong|weak|none"},
   "dims": [...], "excluded_dims": [...],
   "channel": str|None, "item_scope": "118"|"all"|None,
   "metric": [...], "visual": bool}
규칙:
  - 정규화: 소문자화·공백 제거 복사본에서 매칭 (원문 별도 보관 불필요).
  - dims: dim_vocab + 별/기준/... 접미 strip + DIM_EXTRA 별칭.
  - 부정(말고/빼고/제외/아니고/말구): 부정사 앞 dims → excluded(감점용, 탈락 아님).
  - family hard: family명 substring. strong: concept alias 확정 매치.
    weak/LLM은 여기서 안 함 (별도 단계) -> weak 미사용, none으로.
  - channel/item_scope/visual: 키워드 exact.
  - metric: concepts 7종 label/alias substring.
"""
import json
import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = API_ROOT / "data"
DIM_VOCAB = json.loads((BASE_DIR / "dim_vocab.json").read_text(encoding="utf-8"))["dims"]
CONCEPTS = json.loads((BASE_DIR / "catalog" / "concepts.json").read_text(encoding="utf-8"))["concepts"]
_fam_names = None  # 지연 로드 (w2d 카탈로그)
_fam_size = {}
_fam_aliases = None


def _fam_alias():
    global _fam_aliases
    if _fam_aliases is None:
        _fam_aliases = json.loads((BASE_DIR / "family_overrides.json").read_text(encoding="utf-8")).get("aliases", {})
    return _fam_aliases


def _families():
    global _fam_names
    if _fam_names is None:
        cat = json.loads((BASE_DIR / "catalog" / "report_catalog_w2d.json").read_text(encoding="utf-8"))
        from collections import Counter
        cnt = Counter(r["family"] for r in cat if r.get("family"))
        _fam_size.update(cnt)
        _fam_names = sorted(cnt, key=lambda f: (cnt[f], -len(f)))
    return _fam_names


SUFFIXES = ["별로 보여줘", "별로", "기준으로", "기준", "으로 나눠서", "로 나눠서",
            "나눠서", "나누어서", "으로", "로", "별"]
EXPLICIT_MARKERS = ["별", "기준"]  # dims 명시 표지. 없으면 S0 탈락 미적용(S2 점수만)
NEG_WORDS = ["말고", "빼고", "제외", "아니고", "말구"]
# 흔한 표현 → dims (보수적: 애매하면 매칭 안 함. "기관"→수요기관만 예외, 도메인 빈도 근거)
DIM_EXTRA = {"시도별": "지역", "시도": "지역", "권역별": "지역", "권역": "지역",
             "시군구": "지역", "기관별": "수요기관", "기관": "수요기관"}
CHANNELS = ["나라장터", "벤처나라", "혁신장터", "자체전자조달시스템"]
VISUAL_WORDS = ["시각화", "차트", "그래프"]


def _norm(s):
    return re.sub(r"\s+", "", str(s or "").lower())


def _strip_suffix(tok):
    for suf in sorted(SUFFIXES, key=len, reverse=True):
        if tok.endswith(suf) and len(tok) > len(suf):
            return tok[: -len(suf)]
    return tok


def _match_dims(text):
    """정규화 텍스트에서 dims 매칭. (토큰, 명시여부) 반환. 순서 유지, 중복 제거.
    규칙: 토큰==dims, DIM_EXTRA, 또는 토큰이 dims로 시작(prefix).
    접미 포함(사회적약자기업→기업) 금지, 가문명 토큰(지역제한)은 dims에서 제외.
    명시 = 원본 토큰에 별/기준 포함 (S0 탈락은 명시 dims에만 적용)."""
    fams = {_norm(f) for f in _families()}
    found = []
    for m in re.finditer(r"[가-힣a-z0-9]+", text.lower()):
        raw = m.group(0)
        core = _strip_suffix(raw)
        if not core or core in fams:
            continue
        explicit = any(mk in raw for mk in EXPLICIT_MARKERS)
        hit = None
        if core in DIM_VOCAB:
            hit = core
        elif core in DIM_EXTRA:
            hit = DIM_EXTRA[core]
        else:
            for d in DIM_VOCAB:
                if core.startswith(d):
                    hit = d
                    break
        if hit and hit not in [h for h, _ in found]:
            found.append((hit, explicit))
    return found


def parse(q):
    nq = _norm(q)
    # 부정 분리: 부정사 앞=제외 구간, 뒤=요구 구간
    neg_idx, neg_hit = len(nq), None
    for w in NEG_WORDS:
        i = nq.find(w)
        if 0 <= i < neg_idx:
            neg_idx, neg_hit = i, w
    if neg_hit is None:
        pos_text, neg_text = q, ""
    else:
        cut = q.lower().find(neg_hit)
        neg_text, pos_text = q[:cut], q[cut + len(neg_hit):]

    pos_hits = _match_dims(pos_text)
    dims = [h for h, _ in pos_hits]
    dims_explicit = [h for h, e in pos_hits if e]
    excluded = [h for h, _ in _match_dims(neg_text) if h not in dims]

    # family: 44 가문명 hard 매칭만. concept 매칭은 family가 아니라 별도 축.
    # (LLM 정규화=weak 단계는 별도. 여기서 strong 찍지 않음.)
    family, conf = None, "none"
    # family hard: 작은 가문(특이도) 우선. overrides 별칭도 동일 취급.
    for fam in _families():
        names = [fam] + list(_fam_alias().get(fam, []))
        if any(_norm(nm) and _norm(nm) in nq for nm in names):
            family, conf = fam, "hard"
            break

    metric = []
    for c in CONCEPTS:
        if c.get("type") != "metric":
            continue
        terms = [c.get("label")] + list(c.get("aliases") or [])
        if any(t and _norm(t) in nq for t in terms if t):
            if c["label"] not in metric:
                metric.append(c["label"])

    channel = next((c for c in CHANNELS if c in q), None)
    if "118개" in q:
        item_scope = "118"
    elif "전체 품명" in q or "전체품명" in q:
        item_scope = "all"
    else:
        item_scope = None
    visual = any(w in q for w in VISUAL_WORDS)

    return {"family": {"value": family, "confidence": conf},
            "dims": dims, "dims_explicit": dims_explicit, "excluded_dims": excluded,
            "channel": channel, "item_scope": item_scope,
            "metric": metric, "visual": visual,
            "concepts": [c["id"] for c in CONCEPTS
                         if any(t and _norm(t) in nq for t in ([c.get("label")] + list(c.get("aliases") or [])) if t)]}


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:] or ["지역 말고 기관별로 보여줘", "벤처나라 지역별 계약 현황",
                              "수의계약 실적", "MAS 실적 보여줘", "118개 품명 기준 순위"]:
        print(q, "->", json.dumps(parse(q), ensure_ascii=False))
