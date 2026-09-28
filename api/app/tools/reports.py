"""물품분류(품명 8자리 / 세부품명 10자리) 4툴 — goods.g2b.go.kr live.

    resolve_items / item_children / item_detail / item_products

mcp/ Cloudflare Worker 의 goodsPost()/parseClsRows()/parseProductRows()/rankItems() 이식.

중요: 업스트림은 **무UCTURE한 HTML 테이블**이다(공공데이터 개방형 API 아님). 그래서 정규식
파싱에 의존하고, 셀 인덱스 계약이 이 파일 안에 묶여 있다:

    분류검색 행: [0]=8자리품명 [1]=품명 [2]=영문 [3]="8자리 NN"(또는 10자리)
                 [4]=세부품명 [5]=세부영문 [6]=보기
    품목행:     [0]=10자리 [1]=8자리품명 [2]=품명 [3]=품목(식별번호)

업스트림이 열을 바꾸면 여기가 깨진다. 그때는 2026-09-23 시점의 HTML 에 맞춰
CELLS_* 계약을 갱신할 것.

mcp/ 와 달리 Worker 엣지가 아니라 Oracle Linux 박스에서 직접 호출하므로,
네트워크 위치가 바뀌지 않는다(goods.g2b.go.kr 이 Oracle IP 를 막는다면
CORS/차단 문제는 여기로 온다 — Worker 경유가 필요하면 그때 다시 고려).
"""

from __future__ import annotations

import html as htmllib
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..utils import setup_logger

logger = setup_logger("custom_chat.reports")

GOODS_BASE = "https://goods.g2b.go.kr:8053"
GOODS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
ITEM_TAXONOMY_VER = "goods.g2b.go.kr-live|20260923"
TIMEOUT_SEC = 20
MAX_TOP_K = 30

CLS_PATH = "/search/classificationSearch.do"
PROD_PATH = "/search/classificationSearchProductListView.do"

RESOLVE_ITEMS_DESC = (
    "물품 표현(예: 의자, 레미콘, 노트북)을 조달 물품분류 번호로 변환. 품명 8자리 + "
    "세부품명 10자리 후보를 score 순으로 반환. 보고서 조건 선택값에 쓸 정확한 번호를 "
    "찾을 때 사용. 목록 제공자가 아니라 랭킹된 후보만 준다 — 번호를 지어내지 말 것."
)
ITEM_CHILDREN_DESC = (
    "분류 번호 prefix(2·4·6·8자리)의 하위 품명을 1단계 나열. '43' 같은 큰 묶음부터 "
    "점점 좁혀 내려오며 어느 번호가 유효한지 확인. 8자리를 주면 그 품명의 세부품명(10자리) "
    "자식을 직접 반환."
)
ITEM_DETAIL_DESC = (
    "분류 번호 1건의 확정 검증 (정규명·계층경로·해설·형제수). 코드 박기 전 최종 확인용. "
    "존재하지 않는 번호는 에러 — 지어내기 방지. 품목(식별번호)은 미지원."
)
ITEM_PRODUCTS_DESC = (
    "품목(식별번호) 단건 조회 전용. 세부품명 10자리 코드의 등록 품목 상위 10건. 검색 불가."
)


class GoodsError(RuntimeError):
    """업스트림 실패. -32000(내부) 성격."""


class GoodsInputError(ValueError):
    """입력 오류. -32602 성격."""


# ---------- HTML 파싱 ----------

_TR_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.I)
_TD_RE = re.compile(r"<td[^>]*>([\s\S]*?)</td>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CODE8_RE = re.compile(r"^\d{8}$")
_CODE10_RE = re.compile(r"^\d{10}$")
_M8SUB_RE = re.compile(r"(\d{8})\s*(\d{2})")
_SUB_RE = re.compile(r"^(\d{2})\s+(.*)$")


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s)).strip()


def _unescape(s: str) -> str:
    return htmllib.unescape(s)


def _rows(html: str) -> list[list[str]]:
    out = []
    for tr in _TR_RE.findall(html or ""):
        cells = [_unescape(_strip_tags(td)) for td in _TD_RE.findall(tr)]
        if cells:
            out.append(cells)
    return out


def parse_cls_rows(html: str) -> list[dict[str, str]]:
    """분류검색 행 → [{code8,name8,eng8,code10,subno,name10,eng10}]"""
    out = []
    for cells in _rows(html):
        if len(cells) < 6:
            continue
        if not _CODE8_RE.match(cells[0]):
            continue
        m8sub = _M8SUB_RE.search(cells[3] or "")
        sub = _SUB_RE.match(cells[4] or "")
        subno = m8sub.group(2) if m8sub else (sub.group(1) if sub else "")
        out.append(
            {
                "code8": cells[0],
                "name8": cells[1] or "",
                "eng8": cells[2] or "",
                "code10": (
                    m8sub.group(1) + m8sub.group(2) if m8sub else (cells[3] or "")
                ),
                "subno": subno,
                "name10": sub.group(2) if sub else (cells[4] or ""),
                "eng10": cells[5] or "",
            }
        )
    return out


def parse_product_rows(html: str) -> list[dict[str, str]]:
    """품목행 → [{code10,id8,name8,item}]"""
    out = []
    for cells in _rows(html):
        if len(cells) < 4:
            continue
        if not _CODE10_RE.match(cells[0]) or not _CODE8_RE.match(cells[1]):
            continue
        out.append(
            {
                "code10": cells[0],
                "id8": cells[1],
                "name8": cells[2] or "",
                "item": cells[3] or "",
            }
        )
    return out


# ---------- 업스트림 ----------

def goods_post(path: str, params: dict[str, str]) -> str:
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        GOODS_BASE + path,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": GOODS_UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:  # noqa: S310
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:120]
        raise GoodsError(f"goods upstream HTTP {e.code} {detail}") from e
    except Exception as e:
        raise GoodsError(f"goods upstream fail: {str(e)[:120]}") from e


def _norm_q(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "").strip())


def _clamp_top_k(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 10
    return max(1, min(MAX_TOP_K, n))


def _rank_items(rows: list[dict[str, str]], q: str) -> list[tuple[dict, int]]:
    """정확(0) > 접두(1) > 포함(2) > 기본(3) > 불일치(4). mcp/ rankItems() 이식."""
    nq = _norm_q(q)
    scored = []
    for r in rows:
        if r["name10"] == nq or r["name8"] == nq:
            rank = 0
        elif (r["name10"] or "").startswith(nq) or (r["name8"] or "").startswith(nq):
            rank = 1
        elif nq and (nq in (r["name10"] or "") or nq in (r["name8"] or "")):
            rank = 2
        else:
            rank = 4
        scored.append((r, rank))
    scored.sort(key=lambda x: (x[1], x[0]["code10"]))
    return scored


# ---------- 툴 ----------

def resolve_items(query: str, level: str = "all", top_k: Any = 10) -> dict:
    q = str(query or "").strip()
    if not q:
        raise GoodsInputError("query required")
    level = str(level or "all")
    if level not in ("all", "품명", "세부품명"):
        raise GoodsInputError("level must be all|품명|세부품명")
    top_k = _clamp_top_k(top_k)
    nq = _norm_q(q)

    if re.fullmatch(r"\d{2,10}", nq):
        # 숫자 조회면 이름검색 스킵 — 번호 직접 조회 1건으로 충분.
        rows = parse_cls_rows(
            goods_post(CLS_PATH, {"searchGoodsClsfcDetailNo": nq, "pageNumber": "1", "pageSize": "30"})
        )
    else:
        by_name = goods_post(
            CLS_PATH, {"searchGoodsClsfcNm": q, "pageNumber": "1", "pageSize": "30"}
        )
        by_detail = goods_post(
            CLS_PATH,
            {"searchGoodsClsfcDetailNm": q, "pageNumber": "1", "pageSize": "30"},
        )
        rows = [*parse_cls_rows(by_name), *parse_cls_rows(by_detail)]

    seen: set[str] = set()
    deduped = []
    for r in rows:
        k = r["code10"] + "|" + r["name10"]
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    rows = deduped

    if level == "품명":
        s8: set[str] = set()
        rows = [r for r in rows if not (r["code8"] in s8 or s8.add(r["code8"]))]

    total = len(rows)
    ranked = _rank_items(rows, q)[:top_k]
    candidates = [
        {
            "code": r["code10"],
            "level": "세부품명",
            "name": r["name10"],
            "parent": r["code8"],
            "parent_name": r["name8"],
            "path": _path4(r["code8"]),
            "match": m,
        }
        for r, m in ranked
    ]
    if level == "품명":
        # 품명 단위로 접고 하위 분류명은 접두 경로만 남긴다.
        uniq: dict[str, dict] = {}
        for c in candidates:
            uniq.setdefault(c["parent"], c)
        candidates = [
            {
                "code": c["parent"],
                "level": "품명",
                "name": c["parent_name"],
                "path": c["path"],
                "match": c["match"],
            }
            for c in uniq.values()
        ][:top_k]
    return {
        "query": q,
        "level": level,
        "total_count": total,
        "candidates": candidates,
        "taxonomy_ver": ITEM_TAXONOMY_VER,
    }


def _path4(code8: str) -> str:
    return f"{code8[:2]} > {code8[:4]} > {code8[:6]} > {code8}"


def item_children(code: str) -> dict:
    code = _norm_q(code)
    if not re.fullmatch(r"\d{2}|\d{4}|\d{6}|\d{8}", code):
        raise GoodsInputError("code must be 2·4·6·8-digit prefix")
    rows = parse_cls_rows(
        goods_post(CLS_PATH, {"searchGoodsClsfcDetailNo": code, "pageNumber": "1", "pageSize": "100"})
    )
    rows = [r for r in rows if r["code8"].startswith(code)]

    by8: dict[str, dict] = {}
    for r in rows:
        e = by8.setdefault(
            r["code8"],
            {"code": r["code8"], "level": "품명", "name": r["name8"], "child_count": 0, "sample": []},
        )
        e["child_count"] += 1
        if len(e["sample"]) < 3:
            e["sample"].append({"code": r["code10"], "name": r["name10"]})

    if len(code) == 8:
        subs = [
            {"code": r["code10"], "level": "세부품명", "name": r["name10"], "child_count": 0}
            for r in rows
            if r["code8"] == code
        ]
        if not subs and not by8:
            raise GoodsInputError(f"unknown code prefix: {code}")
        return {"code": code, "children": subs, "taxonomy_ver": ITEM_TAXONOMY_VER}

    children = list(by8.values())
    if not children:
        raise GoodsInputError(f"unknown code prefix: {code}")
    return {"code": code, "children": children, "taxonomy_ver": ITEM_TAXONOMY_VER}


def item_detail(code: str) -> dict:
    code = _norm_q(code)
    if not (_CODE8_RE.match(code) or _CODE10_RE.match(code)):
        raise GoodsInputError("code must be 8-digit 품명 or 10-digit 세부품명")
    rows = parse_cls_rows(
        goods_post(CLS_PATH, {"searchGoodsClsfcDetailNo": code, "pageNumber": "1", "pageSize": "30"})
    )
    ten = _CODE10_RE.match(code) is not None
    sibs = [
        r
        for r in rows
        if (r["code8"] == code[:8] if ten else r["code8"].startswith(code[:6]))
    ]
    hit = next((r for r in rows if (r["code10"] if ten else r["code8"]) == code), None)
    if hit is None:
        raise GoodsInputError(f"unknown code: {code}")
    code8 = hit["code8"]
    return {
        "code": code,
        "level": "세부품명" if ten else "품명",
        "name": hit["name10"] if ten else hit["name8"],
        "parent": code8 if ten else code[:6],
        "path": (
            f"{code8[:2]} > {code8[:4]} > {code8[:6]} > {code8} {hit['name8']}"
            if ten
            else f"{code[:2]} > {code[:4]} > {code[:6]}"
        ),
        "eng": hit["eng10"] if ten else hit["eng8"],
        "sibling_count": max(0, len(sibs) - 1),
        "taxonomy_ver": ITEM_TAXONOMY_VER,
    }


def item_products(code: str = None, code10: str = None) -> dict:
    """품목(식별번호) 단건 조회 전용 — 검색 불가."""
    code = _norm_q(code10 or code)
    if not _CODE10_RE.match(code):
        raise GoodsInputError("code10 must be 10-digit 세부품명 번호")
    rows = parse_product_rows(
        goods_post(
            PROD_PATH, {"searchGoodsClsfcNo": code, "pageNumber": "1", "pageSize": "10"}
        )
    )
    return {
        "code10": code,
        "total_count": "10+" if rows else "0",
        "items": rows[:10],
        "taxonomy_ver": ITEM_TAXONOMY_VER,
    }
