#!/usr/bin/env python3
"""LLM-as-judge 평가 체계 (설계 확정: MT-Bench / τ-bench / inspect_ai 리서치 근거).

3개 하위커맨드:

  run      자연 질의셋(data/eval/natural_queries.json)을 실행 서버에 던져
           runs/<label>.ndjson 을 만든다. 툴콜 로그(tool_name/arguments/result)와
           검색 상위 결과(retrieved_top)를 함께 기록한다.
             uv run python pipeline/eval_judge.py run --url http://localhost:8321 \
                 --out runs/head.ndjson --commit HEAD

  judge    NDJSON을 루브릭 5기준으로 채점한다. judge 모델은 xkiro 재사용
           (사용자 결정 — self-preference 편향은 리포트에서 명시한다).
             uv run python pipeline/eval_judge.py judge --in runs/head.ndjson

  compare  두 run을 같은 judge로 채점해 기준별 Δ, hit@1×coverage 교차표,
           회귀/개선 케이스를 evidence와 함께 출력한다.
             uv run python pipeline/eval_judge.py compare runs/head.ndjson runs/prev.ndjson

루브릭 (모두 높을수록 좋다):
  goldcoverage        binary. gold 보고서를 답이 실제 전달하면 1.
                      gold=[] (도메인밖)은 근거 없는 보고서 제시 없이
                      '없음/불가'로 정리하면 1.
  groundedness        binary. 답의 수치·ID·링크가 툴 결과에 근거하면 1.
  toolappropriateness 1-5.   필요한 조회를 적절한 툴로 수행했는지.
  hygieneresidue      binary. 1=잔여물 없음(미래형 약속 종료·나레이션·think 태그·
                      끊긴 링크 줄). 정규식 통과 케이스도 judge가 재검사한다.
  koreanquality       binary. 자연스러운 한국어면 1.

채점 규칙: 온도 0, JSON 강제 출력(response_format json_object),
파싱 실패 1회 재시도 → unscored. flagged(기준 간 모순) 케이스만 3회 다수결.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EVAL_QUERIES = BASE_DIR / "data" / "eval" / "natural_queries.json"


def _load_env(path: Path) -> None:
    """api/.env 자체 로딩 — uv 자동로딩·bash sourcing 의존을 제거한다.

    Windows(uv run)와 Oracle Linux(make dev / bash sourcing) 어디서든
    동일하게 키가 주입되도록 하는 크로스플랫폼 안전장치.
    우선순위: 이미 설정된 환경변수(셸) > .env. BOM·CRLF·export 접두어·따옴표 허용.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if val:
            os.environ.setdefault(key, val)


_load_env(BASE_DIR / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_chain  # noqa: E402

CRITERIA = ["goldcoverage", "groundedness", "toolappropriateness",
            "hygieneresidue", "koreanquality"]
BINARY = {"goldcoverage", "groundedness", "hygieneresidue", "koreanquality"}
TOOL_RESULT_MAX_CHARS = 1200
MAX_TOOL_CALLS = 6
NDJSON_TOP = 5  # retrieved_top 길이 (hit@1/hit@5 계산용)


def _read_ndjson(path: Path) -> list[dict]:
    # utf-8-sig: Windows 도구가 붙인 BOM 허용
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]


# ---------------------------------------------------------------- run 모드

def _extract_report_ids(tool_result: str) -> list[str]:
    """툴 결과에서 report_id 후보를 등장 순서대로 뽑는다 (검색 candidates 우선)."""
    try:
        data = json.loads(tool_result)
        cands = data.get("candidates") if isinstance(data, dict) else None
        if isinstance(cands, list):
            ids = [c.get("report_id") for c in cands if isinstance(c, dict)]
            ids = [i for i in ids if isinstance(i, str)]
            if ids:
                return ids
    except Exception:
        pass
    return re.findall(r"\b\d{5}\b", tool_result)


def run_server(url: str, query: str, web_search: bool, timeout: int) -> dict:
    body = {"message": query, "stream": False, "web_search": web_search}
    req = urllib.request.Request(
        url.rstrip("/") + "/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d, (time.monotonic() - t0) * 1000


def cmd_run(a: argparse.Namespace) -> None:
    queries = json.loads(EVAL_QUERIES.read_text(encoding="utf-8"))["queries"]
    if a.limit:
        queries = queries[: a.limit]
    commit = a.commit
    if not commit:
        import subprocess
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=BASE_DIR, capture_output=True,
                                text=True).stdout.strip() or "unknown"
    out = BASE_DIR / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] {len(queries)}개 질의 → {a.url} (commit={commit}, out={out})")
    with out.open("w", encoding="utf-8") as f:
        for i, item in enumerate(queries, 1):
            rec = {
                "query_id": f"n{i:02d}",
                "query": item["q"],
                "commit": commit,
                "gold": item.get("gold") or [],
                "gold_any": item.get("gold_any"),
                "type": item.get("type", "?"),
                "final_answer": None,
                "tool_calls": [],
                "retrieved_top": [],
                "latency_ms": None,
                "error": None,
            }
            try:
                d, ms = run_server(a.url, item["q"], a.web_search, a.timeout)
                rec["latency_ms"] = round(ms)
                rec["final_answer"] = d.get("answer")
                calls = d.get("tool_calls") or []
                for c in calls[:MAX_TOOL_CALLS]:
                    rec["tool_calls"].append({
                        "tool_name": c.get("tool_name"),
                        "tool_arguments": c.get("tool_arguments"),
                        "tool_result": (c.get("tool_result") or "")[:TOOL_RESULT_MAX_CHARS],
                    })
                for c in calls:
                    rec["retrieved_top"] = _extract_report_ids(c.get("tool_result") or "")
                    if rec["retrieved_top"]:
                        break
                rec["retrieved_top"] = rec["retrieved_top"][:NDJSON_TOP]
            except Exception as e:  # 서버 다운·타임아웃도 레코드로 남긴다
                rec["error"] = str(e)[:200]
                print(f"  [{rec['query_id']}] ERROR {rec['error']}")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ans = (rec["final_answer"] or "")[:40].replace("\n", " ")
            print(f"  [{rec['query_id']}] {rec['latency_ms']}ms tool={len(rec['tool_calls'])} "
                  f"top={rec['retrieved_top'][:2]} ans={ans!r}")
    print(f"[run] 완료 → {out}")


# ---------------------------------------------------------------- judge 모드

JUDGE_SYSTEM = (
    "너는 한국어 검색-챗봇(RAG) 답변 품질 심사관이다. 제시된 근거만 보고 엄격하게 판정하고, "
    "출력은 지정된 JSON 하나뿐이다. 설명·접두어·코드펜스 없이 JSON만 출력한다."
)

JUDGE_PROMPT = """[질의]
{query}

[정답 정보]
gold 보고서 ID: {gold}
gold 조건(gold_any): {gold_any}
질의 유형: {qtype}
(주의: gold가 비어 있으면 이 질의는 조달데이터허브 도메인밖/답 없는 질문이다. \
이때는 근거 없는 보고서를 지어내지 않고 '없음/불가'로 정리하는 답이 정답이다.)

[툴 호출 기록]
{tool_calls}

[검색 상위 결과 report_id (순서대로)]
{retrieved_top}

[최종 답변]
{answer}

[채점 기준 — 모두 값이 클수록 좋다]
1. goldcoverage (0/1): gold ID가 있으면, 답변이 그 보고서를(ID·이름·내용으로) 실제 전달하면 1. \
gold가 비어 있으면, 도메인밖 질문에 근거 없는 보고서를 제시하지 않고 정중히 거절/무매치 처리하면 1. \
gold_any 조건이 있으면, 제시된 보고서가 그 조건을 만족하면 1.
2. groundedness (0/1): 답변의 수치·report_id·링크가 [툴 호출 기록]에 근거하면 1. \
툴 결과에 없는 수치·ID·링크를 만들어냈으면 0.
3. toolappropriateness (1~5): 5=필요한 조회를 정확한 툴·인자로 수행, 3=수행했지만 불필요/중복 호출 포함, \
1=조회 없이 추측했거나 전혀 엉뚱한 툴 사용.
4. hygieneresidue (0/1): 1=잔여물 없음. 0=미래형 약속으로 턴 종료("조회해드릴게요"), \
내독 나레이션 잔여, <think> 태그, 링크만 남은 끊긴 줄 등 위생 문제가 보인다.
5. koreanquality (0/1): 1=자연스러운 한국어. 0=기계적/어색/다른 언어 섞임.

출력 JSON (키 그대로):
{{"goldcoverage": 0, "groundedness": 0, "toolappropriateness": 3, "hygieneresidue": 1, "koreanquality": 1, "evidence": "판단 근거 1-2문장"}}"""


def _fmt_tool_calls(calls: list[dict]) -> str:
    if not calls:
        return "(툴 호출 없음)"
    lines = []
    for c in calls:
        args = json.dumps(c.get("tool_arguments"), ensure_ascii=False)
        lines.append(f"- {c.get('tool_name')} args={args}\n  result={c.get('tool_result')}")
    return "\n".join(lines)


def _build_judge_messages(rec: dict) -> list[dict]:
    user = JUDGE_PROMPT.format(
        query=rec["query"],
        gold=json.dumps(rec.get("gold") or [], ensure_ascii=False),
        gold_any=json.dumps(rec.get("gold_any"), ensure_ascii=False),
        qtype=rec.get("type", "?"),
        tool_calls=_fmt_tool_calls(rec.get("tool_calls") or []),
        retrieved_top=", ".join(rec.get("retrieved_top") or []) or "(없음)",
        answer=(rec.get("final_answer") or "(빈 답변)")[:4000],
    )
    return [{"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user}]


def _parse_scores(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    for k in CRITERIA:
        v = d.get(k)
        if not isinstance(v, int):
            return None
        if k in BINARY and v not in (0, 1):
            return None
        if k == "toolappropriateness" and not 1 <= v <= 5:
            return None
    if not isinstance(d.get("evidence"), str) or not d["evidence"].strip():
        d["evidence"] = ""
    return d


def _is_flagged(s: dict) -> bool:
    """기준 간 모순 → 다수결 재검사 대상."""
    if s["goldcoverage"] == 1 and s["groundedness"] == 0:
        return True
    if s["goldcoverage"] == 1 and s["toolappropriateness"] <= 2:
        return True
    if s["goldcoverage"] == 1 and s["hygieneresidue"] == 0:
        return True
    return False


def _judge_once(rec: dict, verbose: bool = False) -> dict | None:
    msgs = _build_judge_messages(rec)
    for attempt in (1, 2):  # 파싱 실패 1회 재시도 → unscored
        try:
            text = llm_chain.chat(msgs, max_tokens=400, temperature=0,
                                  verbose=verbose, json_mode=True)
        except llm_chain.LLMExhausted as e:
            print(f"  [judge] LLMExhausted: {e}")
            return None
        scores = _parse_scores(text)
        if scores is not None:
            return scores
        print(f"  [judge] 파싱 실패 (시도 {attempt}) — {text[:120]!r}")
    return None


def _judge_record(rec: dict) -> dict:
    votes = []
    s = _judge_once(rec)
    if s is not None:
        votes.append(s)
        if _is_flagged(s):
            print(f"  [judge] flagged({rec['query_id']}) → 3회 다수결")
            extra = [x for x in (_judge_once(rec) for _ in range(2)) if x is not None]
            votes.extend(extra)
    if not votes:
        return {"query_id": rec["query_id"], "scores": None, "votes": [],
                "flagged": False, "evidence": "unscored"}
    by_crit: dict[str, list[int]] = {k: [v[k] for v in votes] for k in CRITERIA}
    final = {k: Counter(v).most_common(1)[0][0] for k, v in by_crit.items()}
    evidence = next((v["evidence"] for v in votes
                     if all(v[k] == final[k] for k in CRITERIA)), votes[0]["evidence"])
    return {
        "query_id": rec["query_id"],
        "scores": final,
        "votes": [{k: v[k] for k in CRITERIA} for v in votes],
        "flagged": len(votes) > 1,
        "evidence": evidence,
    }


def _judge_model_name() -> str:
    provs = llm_chain._providers()
    return ", ".join(f"{p['name']}:{p['model']}" for p in provs[:2]) or "none"


def cmd_judge(a: argparse.Namespace) -> None:
    src = BASE_DIR / a.input
    recs = _read_ndjson(src)
    if a.limit:
        recs = recs[: a.limit]
    out = BASE_DIR / a.output if a.output else src.with_suffix(".judged.json")
    print(f"[judge] {len(recs)}개 레코드 채점 — judge={_judge_model_name()}")
    judged = []
    for rec in recs:
        if rec.get("error") and not rec.get("final_answer"):
            print(f"  [{rec['query_id']}] 실행 실패 레코드 → unscored")
            judged.append({"query_id": rec["query_id"], "scores": None,
                           "votes": [], "flagged": False,
                           "evidence": f"run error: {rec['error']}"})
            continue
        print(f"  [{rec['query_id']}] 채점 중…")
        judged.append(_judge_record(rec))
    summary = _summarize(recs, judged)
    result = {
        "meta": {
            "source": str(src), "judge_model": _judge_model_name(),
            "rubric": {k: ("binary" if k in BINARY else "1-5") for k in CRITERIA},
            "note": "judge=xkiro 재사용 (self-preference 편향 감수)",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        "summary": summary,
        "records": judged,
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[judge] 완료 → {out}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def _summarize(recs: list[dict], judged: list[dict]) -> dict:
    by_id = {r["query_id"]: r for r in recs}
    summary: dict = {"n": len(recs), "unscored": 0, "flagged": 0}
    for c in CRITERIA:
        summary[c] = None
    hits1 = hits5 = n_gold = 0
    crosstab = Counter()
    for j in judged:
        rec = by_id.get(j["query_id"], {})
        if j["scores"] is None:
            summary["unscored"] += 1
            continue
        if j["flagged"]:
            summary["flagged"] += 1
        for c in CRITERIA:
            norm = j["scores"][c] / 5 if c == "toolappropriateness" else j["scores"][c]
            summary[c] = (summary[c] or 0) + norm
        gold = rec.get("gold") or []
        if gold:
            n_gold += 1
            top = rec.get("retrieved_top") or []
            h1 = bool(set(top[:1]) & set(gold))
            h5 = bool(set(top[:NDJSON_TOP]) & set(gold))
            hits1 += h1
            hits5 += h5
            cov = j["scores"]["goldcoverage"]
            key = f"hit1={int(h1)},cov={cov}"
            crosstab[key] += 1
        elif rec.get("gold_any"):
            pass  # gold_any는 교차표에서 제외 (조건 판정은 judge 몫)
    if len(judged) > summary["unscored"]:
        for c in CRITERIA:
            if summary[c] is not None:
                summary[c] = round(summary[c] / (len(judged) - summary["unscored"]), 3)
        summary["total"] = round(sum(summary[c] or 0 for c in CRITERIA) / len(CRITERIA), 3)
    summary["hit@1"] = round(hits1 / n_gold, 3) if n_gold else None
    summary["hit@5"] = round(hits5 / n_gold, 3) if n_gold else None
    summary["crosstab_hit1_x_coverage"] = dict(crosstab)
    return summary


# ---------------------------------------------------------------- compare 모드

def _ensure_judged(path: Path) -> dict:
    judged_path = path.with_suffix(".judged.json")
    if judged_path.exists():
        print(f"[compare] 기존 채점 재사용: {judged_path}")
        return json.loads(judged_path.read_text(encoding="utf-8"))
    print(f"[compare] 채점 수행: {path} — judge={_judge_model_name()}")
    recs = _read_ndjson(path)
    judged = []
    for rec in recs:
        if rec.get("error") and not rec.get("final_answer"):
            judged.append({"query_id": rec["query_id"], "scores": None, "votes": [],
                           "flagged": False, "evidence": f"run error: {rec['error']}"})
            continue
        print(f"  [{rec['query_id']}] 채점 중…")
        judged.append(_judge_record(rec))
    result = {
        "meta": {"source": str(path), "judge_model": _judge_model_name(),
                 "note": "judge=xkiro 재사용 (self-preference 편향 감수)",
                 "ts": datetime.now(timezone.utc).isoformat()},
        "summary": _summarize(recs, judged),
        "records": judged,
    }
    judged_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[compare] 채점 저장 → {judged_path}")
    return result


def cmd_compare(a: argparse.Namespace) -> None:
    head_path, prev_path = BASE_DIR / a.head, BASE_DIR / a.prev
    head_rec = _read_ndjson(head_path)
    prev_rec = _read_ndjson(prev_path)
    head = _ensure_judged(head_path)
    prev = _ensure_judged(prev_path)
    if head["meta"]["judge_model"] != prev["meta"]["judge_model"]:
        print("[compare] 경고: 양쪽 judge 모델이 다르다 — judge 혼입 주의")

    # 기준별 Δ (같은 query_id끼리)
    h_scores = {j["query_id"]: j["scores"] for j in head["records"] if j["scores"]}
    p_scores = {j["query_id"]: j["scores"] for j in prev["records"] if j["scores"]}
    common = sorted(set(h_scores) & set(p_scores))
    delta = {}
    for c in CRITERIA:
        def norm(s):
            return s[c] / 5 if c == "toolappropriateness" else s[c]
        dh = sum(norm(h_scores[q]) for q in common) / len(common) if common else 0
        dp = sum(norm(p_scores[q]) for q in common) / len(common) if common else 0
        delta[c] = round(dh - dp, 3)
    total_h = head["summary"].get("total")
    total_p = prev["summary"].get("total")
    total_d = round((total_h or 0) - (total_p or 0), 3)
    subtle = abs(total_d) <= 0.03

    reg, imp = [], []
    for q in common:
        h, p = h_scores[q], p_scores[q]
        for c in CRITERIA:
            nh = h[c] / 5 if c == "toolappropriateness" else h[c]
            np_ = p[c] / 5 if c == "toolappropriateness" else p[c]
            if nh < np_:
                reg.append((q, c, p[c], h[c]))
            elif nh > np_:
                imp.append((q, c, p[c], h[c]))

    def _evidence_of(jrecs, q):
        for j in jrecs:
            if j["query_id"] == q:
                return j.get("evidence", "")
        return ""

    report = {
        "head": {"source": head["meta"]["source"], "summary": head["summary"]},
        "prev": {"source": prev["meta"]["source"], "summary": prev["summary"]},
        "n_common_scored": len(common),
        "delta_by_criterion": delta,
        "total": {"head": total_h, "prev": total_p, "delta": total_d,
                  "verdict": "유의 미묘 (±0.03 이내)" if subtle else "유의미한 변화"},
        "regressions": [
            {"query_id": q, "criterion": c, "prev": pv, "head": hv,
             "evidence": _evidence_of(head["records"], q)}
            for q, c, pv, hv in reg],
        "improvements": [
            {"query_id": q, "criterion": c, "prev": pv, "head": hv,
             "evidence": _evidence_of(head["records"], q)}
            for q, c, pv, hv in imp],
        "flagged_recheck": [
            {"query_id": j["query_id"], "evidence": j.get("evidence", "")}
            for j in head["records"] + prev["records"] if j.get("flagged")],
        "note": "judge는 HEAD 버전 하나만 사용해 양쪽 채점 (judge 혼입 방지)",
    }
    out = BASE_DIR / a.output if a.output else head_path.with_suffix(".compare.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[compare] 완료 → {out}")
    print(json.dumps({k: report[k] for k in
                      ("delta_by_criterion", "total", "n_common_scored")},
                     ensure_ascii=False, indent=1))
    if subtle and report["flagged_recheck"]:
        print(f"[compare] 총점 Δ가 미묘하므로 flagged {len(report['flagged_recheck'])}건 다수결 결과를 재확인하라.")


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM-as-judge 평가 체계")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="자연 질의셋 실행 → NDJSON")
    r.add_argument("--url", default="http://localhost:8321")
    r.add_argument("--out", default="runs/head.ndjson")
    r.add_argument("--commit", default=None, help="기본: 현재 git short hash")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--timeout", type=int, default=240)
    r.add_argument("--web-search", action="store_true",
                   help="웹검색 툴 포함 (기본: jodal 도메인 툴만)")

    j = sub.add_parser("judge", help="NDJSON 채점")
    j.add_argument("--input", "--in", dest="input", default="runs/head.ndjson")
    j.add_argument("--output", default=None)
    j.add_argument("--limit", type=int, default=0)

    c = sub.add_parser("compare", help="두 run 비교 리포트")
    c.add_argument("head", help="runs/head.ndjson")
    c.add_argument("prev", help="runs/prev.ndjson")
    c.add_argument("--output", default=None)

    a = ap.parse_args()
    {"run": cmd_run, "judge": cmd_judge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
