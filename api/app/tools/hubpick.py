"""hubpick — 조달데이터허브 전용 보조 5툴.

    form_guide / concept_reports / value_lookup / family_map / catalog_health

mcp/ Cloudflare Worker 의 hubpick.js(349줄) 이식. 에셋 6파일은
data/hubpick/ 로 옮겨 왔다(mcp/public/hubpick/ → 동일 파일).

mcp/ 와 달리 assets 바인딩 없이 그냥 디스크에서 json 로 읽는다. 로더는
여전히 lazy + 프로세스 상주(모듈 전역 캐시).

★ 이식 시 반드시 유지할 것 — docs/MIGRATION_NOTES.md §1:
value_lookup 의 (1) report_id 없음 모드에서 **"값 없음"을 -32602 로 throw 하면 안 된다.**
MCP 클라이언트가 Tool execution failed 로 닫아버려 모델이 아래 가이드를 읽지 못하고
다음 툴로 진행하지 못한 채 포기한다. 200 + 빈 결과 + 대체 경로 가이드로 돌려준다.
(worker.js 는 mcp/ 삭제 직전 미커밋으로 이게 고쳐졌다.)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..utils import setup_logger

logger = setup_logger("custom_chat.hubpick")

API_ROOT = Path(__file__).resolve().parents[2]
HUB_DIR = API_ROOT / "data" / "hubpick"
HUBPICK_VERSION = "w2d-20260919|hubpick-1"

_FILES = (
    "form_guide.json",
    "names.json",
    "family_index.json",
    "concept_index.json",
    "value_topk.json",
    "health.json",
)


class HubInputError(ValueError):
    """입력 오류 (-32602)."""


_hub: dict[str, Any] | None = None


def load_hub() -> dict[str, Any]:
    """에셋 6파일 로드 + 프로세스 상주. mcp/ 의 loadHub() 이식."""
    global _hub
    if _hub is None:
        raw = {}
        for name in _FILES:
            p = HUB_DIR / name
            if not p.exists():
                raise RuntimeError(
                    f"hubpick 아티팩트 없음: {p} — pipeline/export_hubpick_index.py 로 생성"
                )
            raw[name] = json.loads(p.read_text(encoding="utf-8"))
        _hub = {
            "forms": raw["form_guide.json"],
            "names": raw["names.json"],
            "families": (raw["family_index.json"] or {}).get("families", {}),
            "concepts": raw["concept_index.json"],
            "values": raw["value_topk.json"],
            "health": raw["health.json"],
        }
    return _hub


# ---------- 툴 설명 ----------

FORM_GUIDE_DESC = (
    "보고서 입력폼의 조건 목록(조건명 + 선택지 개수). "
    "get_report_detail 조건명만으론 부족할 때 사용. "
    "**반환된 조건을 표로 옮겨 사용자에게 보여주지 말 것.** "
    "조회에 꼭 필요한 조건 2~3개만 골라 '무엇을 바꿔야 수치가 나오는지' 한 줄씩 안내하고, "
    "나머지는 기본값이라고 묶어 말할 것. "
    "선택지 값이 필요할 때만 include_options=true로 다시 부를 것. "
    "131건 전량 조회 불가, 1건씩."
)
CONCEPT_REPORTS_DESC = (
    "개념별 보고서 목록: 여성기업·계약금액 등 개념 ID로 관련 보고서 직접 조회. "
    "search_reports 문장검색에서 빠질 때 사용. 개념 9종: org_client, vendor, "
    "contract_amount, contract_count, item, region, date, cert_product, female_biz."
)
VALUE_LOOKUP_DESC = (
    "조건 선택값에서 특정 값(기관명·업체명 등)을 찾는다. 두 가지 방식. "
    "(1) report_id 없이 호출 → 131건 전량에서 그 값이 조건 옵션으로 들어있는 "
    "'보고서 목록'을 찾는다. '○○공사 계약 실적 몇 건' 처럼 어떤 보고서를 쓸지 "
    "모를 때 이것부터 쓸 것. "
    "(2) report_id + cond 지정 → 그 보고서에서 그 값을 고를 수 있는지 + 상위값 표본. "
    "기관명·업체명은 보고서 이름에 안 나오므로 search_reports로는 절대 안 잡힌다."
)
FAMILY_MAP_DESC = (
    "보고서 그룹(family)의 구성 보고서 목록. 검색 후보가 여러 개로 갈릴 때 "
    "'같은 가족의 다른 버전'을 확인하는 용도."
)
CATALOG_HEALTH_DESC = "카탈로그/인덱스 상태(건수·버전). 장애 진단용."


# ---------- 공통 ----------

def _norm(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "").strip())


def _clamp_top_k(v: Any, default: int = 10, cap: int = 30) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(1, min(cap, n))


def _resolve_rid(hub: dict, report_id: Any, report_name: Any) -> str:
    """mcp/ 의 hubResolveRid() 이식."""
    rid = str(report_id or "").strip()
    if rid:
        if rid in hub["forms"]:
            return rid
        raise HubInputError(f"unknown report_id: {rid[:20]}")
    q = _norm(report_name)
    if not q:
        raise HubInputError("report_id or report_name required")
    for id_, n in hub["names"].items():
        if _norm(n) == q:
            return id_
    hits = [(id_, n) for id_, n in hub["names"].items() if (s := _norm(n)) and (s in q or q in s)]
    if len(hits) == 1:
        return hits[0][0]
    if hits:
        cand = " / ".join(f"{i} {n}" for i, n in hits[:5])
        raise HubInputError(f"ambiguous report_name, candidates: {cand}")
    raise HubInputError(f"unknown report_name: {str(report_name)[:40]}")


def _public_conds(conds: list, include_options: bool) -> list[dict]:
    """조건명 + 선택지 개수. 선택지 값은 기본 숨김.

    ui/sem/required 는 화면 구현용 내부 코드이고 값이 없다(required_unknown).
    그대로 주면 LLM 이 `yearmonth`·`enum_multi` 를 사람용 설명으로 번역하거나
    조건표를 그대로 노출한다(실측). 선택지 값도 있으면 6~8행 표를 그대로 뱉는다.
    """
    out = []
    for c in conds or []:
        opts, seen = [], set()
        for v in c.get("opts") or []:
            s = str(v)
            if s in seen:
                continue
            seen.add(s)
            opts.append(s)
        row = {"name": c.get("name"), "option_count": len(opts)}
        if include_options:
            row["opts"] = opts
        out.append(row)
    return out


# ---------- 툴 ----------

def form_guide(
    report_id: Any = None,
    report_name: Any = None,
    include_options: bool = False,
) -> dict:
    hub = load_hub()
    rid = _resolve_rid(hub, report_id, report_name)
    inc = include_options is True
    res = {
        "report_id": rid,
        "name": hub["names"].get(rid) or rid,
        "conds": _public_conds(hub["forms"].get(rid), inc),
    }
    if not inc:
        res["note"] = (
            "선택지 값은 숨겼다. 사용자가 '무엇을 고를 수 있냐'고 물으면 include_options=true로 "
            "다시 불러라. 이 목록을 표로 옮겨 사용자에게 보여주지 말고, 조회에 꼭 필요한 조건 "
            "2~3개만 골라 안내할 것."
        )
    res["hubpick_ver"] = HUBPICK_VERSION
    return res


def concept_reports(concept_id: str) -> dict:
    hub = load_hub()
    cid = str(concept_id or "").strip()
    if not cid:
        raise HubInputError("concept_id required")
    lst = (hub["concepts"].get("concepts") or [])
    found = next((c for c in lst if c.get("id") == cid), None)
    if found is None:
        ids = ", ".join(c.get("id", "") for c in lst)
        raise HubInputError(f"unknown concept_id: {cid[:30]}. available: {ids}")
    return {
        "concept_id": cid,
        "label": found.get("label"),
        "definition": found.get("definition"),
        "aliases": found.get("aliases") or [],
        "reports": found.get("reports") or [],
        "hubpick_ver": HUBPICK_VERSION,
    }


_AGENCY_COND_RE = re.compile(r"(수요기관|공고기관|발주기관|조달기관|주관기관|수요기관명)")

# 조사·어미는 잘라 어절만 본다. mcp/ 의 _INTENT_STOP 이식.
_INTENT_STOP = {
    "알", "고", "싶", "다", "이", "요", "을", "를", "은", "는", "이야", "야",
    "건", "수", "중", "개", "만", "좀", "더", "그", "저", "것", "어떻", "때문",
}
_SPLIT_RE = re.compile(r"[\s,.\n!?~()]+")
_KEEP_RE = re.compile(r"[^0-9A-Za-z가-힣]")


def _intent_score(name: str, intent: Any) -> int:
    """기관 1개가 60~70개 보고서에 동시에 걸린다. 그중 정답을 가르는 유일한 신호는
    '질문의 나머지 부분'이다. ('조폐공사 계약 총 몇건' → 계약/실적 이 이름에 있는
    보고서를 앞세운다)"""
    if not intent:
        return 0
    norm = _norm(intent)
    target = _norm(name)
    score = 0
    for raw in _SPLIT_RE.split(str(intent)):
        t = _KEEP_RE.sub("", raw)
        if len(t) < 2 or t in _INTENT_STOP:
            continue
        if t in target:
            score += 2 if len(t) >= 3 else 1
    if not score and len(norm) >= 3 and target[:3] and norm[:3] in target:
        score = 1
    return score


def _scan_form_options(hub: dict, query: str, top_k: int, intent: Any) -> dict:
    """form_guide.json 의 opts 를 131건 전수 스캔 (표본 value_topk 는 19건뿐이라 부족).

    "○○공사 계약 실적 몇 건" → 그 기관이 조건 옵션으로 들어있는 보고서를 돌려준다.
    정렬이 중요하다: 00262(수요기관별 계약납품요구 실적통계) 같은 정답 보고서는
    수요기관 조건이 popup이라 정적 옵션이 없고 통계대상시스템 경유로만 걸린다.
    """
    q = _norm(query)
    found = []
    for rid, conds in (hub["forms"] or {}).items():
        name = hub["names"].get(rid) or rid
        for c in conds or []:
            opts = c.get("opts") or []
            if not opts:
                continue
            exact = [v for v in opts if _norm(v) == q]
            contains = [v for v in opts if _norm(v) != q and q in _norm(v)]
            if not exact and not contains:
                continue
            found.append(
                {
                    "report_id": rid,
                    "name": name,
                    "cond": c.get("name"),
                    "match": "exact" if exact else "contains",
                    # 직접 기관 선택 조건이면 '기관 고르기'만으로 바로 좁힌다.
                    "direct_filter": bool(_AGENCY_COND_RE.search(c.get("name") or "")),
                    # 건수/금액 세는 질문이면 통계형(시각화 아님)이 먼저다.
                    "stat_type": "chart" if "시각화" in name else "table",
                    "intent_hit": _intent_score(name, intent),
                    "_values": exact or contains,
                }
            )
    found.sort(
        key=lambda a: (
            0 if a["match"] == "exact" else 1,
            -a["intent_hit"],
            0 if a["direct_filter"] else 1,
            0 if a["stat_type"] == "table" else 1,
            a["report_id"],
        )
    )
    slim = [
        {**{k: v for k, v in r.items() if k != "_values"}, "values": r["_values"][:4]}
        for r in found[:top_k]
    ]
    return {"total": len(found), "reports": slim}


def value_lookup(
    query: str,
    report_id: Any = None,
    cond: Any = None,
    intent: Any = None,
    top_k: Any = 10,
) -> dict:
    hub = load_hub()
    q = _norm(query)
    if not q:
        raise HubInputError("query required")
    top_k = _clamp_top_k(top_k)
    rid_input = str(report_id or "").strip()

    # (1) report_id 없음 → 131건 전수에서 "이 값을 고를 수 있는 보고서"를 찾는다.
    if not rid_input:
        # 기관 1개가 60~70개에 동시에 걸린다. 자르면 정답이 밀리므로 넓게 준다.
        scan = _scan_form_options(hub, query, max(top_k, 40), intent)
        if not scan["total"]:
            # ★ throw 하면 안 된다 — 200 + 빈 결과 + 대체 경로로 돌린다.
            # (docs/MIGRATION_NOTES.md §1, mcp/ 의 미커밋 수정)
            return {
                "mode": "scan_forms",
                "query": query,
                "matched_reports": 0,
                "returned": 0,
                "reports": [],
                "note": f'131건 보고서의 입력폼 정적 옵션 어디에도 "{q[:30]}" 가 없다. '
                "기관명·업체명은 보고서 이름에도 없고, 조건 선택값(정적 옵션)에 있을 때만 "
                "여기서 잡힌다. 팝업이 자유입력인 조건이라 애초에 목록에 안 뜨는 경우다. "
                '대체 경로: (1) 더 짧은 형태로 재검색 (예: "한국전력기술" → "한국전력"). '
                "(2) search_reports 로 보고서명부터 찾은 뒤 "
                "form_guide(include_options=true)로 그 조건을 고를 수 있는지 확인. "
                "(3) 그래도 안 되면 이 기관/업체를 집계하는 보고서가 허브에 없는 것.",
                "hubpick_ver": HUBPICK_VERSION,
            }
        return {
            "mode": "scan_forms",
            "query": query,
            "matched_reports": scan["total"],
            "returned": len(scan["reports"]),
            "reports": scan["reports"],
            "note": "이 값을 조건으로 골라 조회할 수 있는 보고서 목록이다. "
            "여기서는 링크를 주지 않으므로, 고른 보고서의 열기 링크가 필요하면 "
            "그 report_id 로 get_report_detail 을 한 번 더 불러라.",
            "hubpick_ver": HUBPICK_VERSION,
        }

    # (2) report_id + cond 지정 → 그 보고서에서 고를 수 있는지 + 상위값 표본.
    rid = _resolve_rid(hub, rid_input, None)
    cond_s = str(cond or "").strip()
    if not cond_s:
        avail = ", ".join(c.get("name", "") for c in (hub["forms"].get(rid) or [])[:8])
        raise HubInputError(f"cond required when report_id is given (사용 가능: {avail})")

    cols = hub["values"].get(rid) or {}
    col = cols.get(cond_s)
    if col is None:
        # 표본이 없는 조건이어도 form_guide opts 로는 전수 확인이 가능하다.
        form_opts = [
            v
            for c in (hub["forms"].get(rid) or [])
            if c.get("name") == cond_s
            for v in (c.get("opts") or [])
        ]
        hit = [v for v in form_opts if q in _norm(v)]
        if hit:
            return {
                "report_id": rid,
                "name": hub["names"].get(rid) or rid,
                "cond": cond_s,
                "query": query,
                "source": "form_guide_opts",
                "total_count": len(hit),
                "candidates": [
                    {"value": str(v), "match": "contains"} for v in hit[:top_k]
                ],
                "note": "표본(value_topk) 밖이지만 입력폼 정적 옵션에는 있다. 존재는 확정.",
                "hubpick_ver": HUBPICK_VERSION,
            }
        avail = ", ".join(list(cols)[:12]) or "none"
        raise HubInputError(f"no sample for cond: {cond_s[:30]} (available: {avail})")

    rows = []
    for n, v in col.get("top") or []:
        sv = _norm(v)
        m = "exact" if sv == q else ("contains" if q in sv else None)
        if m:
            rows.append({"value": str(v), "hits": n, "match": m})
    rows.sort(key=lambda r: (0 if r["match"] == "exact" else 1, -r["hits"]))
    return {
        "report_id": rid,
        "cond": cond_s,
        "query": query,
        "sample_n": col.get("n"),
        "sample_distinct": col.get("distinct"),
        "sample_note": "표본 상위값(n≈5000). 없음≠부재.",
        "total_count": len(rows),
        "candidates": rows[:top_k],
        "hubpick_ver": HUBPICK_VERSION,
    }


def family_map(family: str) -> dict:
    hub = load_hub()
    fam = _norm(family)
    if not fam:
        raise HubInputError("family required")
    names = list(hub["families"])
    hit = next((n for n in names if _norm(n) == fam), None) or next(
        (n for n in names if _norm(n) in fam or fam in _norm(n)), None
    )
    if hit is None:
        raise HubInputError(f"unknown family: {str(family)[:30]}")
    return {"family": hit, "reports": hub["families"][hit], "hubpick_ver": HUBPICK_VERSION}


def catalog_health() -> dict:
    return load_hub()["health"]
