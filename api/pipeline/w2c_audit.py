#!/usr/bin/env python3
"""W2c: 출력컬럼 전수 심사 (LLM 배치 심판, 20개/호출, JSON 검증, 재개 지원).

입력(읽기만): data/catalog/report_catalog_w2b.json (113종, 1122컬럼)
출력: data/catalog/w2c_audit.json ({rid:name: {verdict, confidence, reason, batch}})
진행: data/catalog/w2c_progress.json — 배치 단위 성공 기록, 재실행 시 스킵
LLM: llm_chain.chat (gemini 우선, 레이트리밋+폴백 내장)

verdict 종류: metric | dimension | identifier | date | flag
검증: JSON 파싱 → 건수 일치 → 이름 일치 → verdict enum. 실패 시 1회 재시도 후 배치 실패 기록.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_chain import chat, LLMExhausted  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2b.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "w2c_audit.json"
PROG_PATH = BASE_DIR / "data" / "catalog" / "w2c_progress.json"
PROG_VERSION = 2
BATCH = 10
VERDICTS = {"metric", "dimension", "identifier", "date", "flag"}

PROMPT = """조달데이터허브 보고서 출력 컬럼의 분류를 판정하라. 각 컬럼에 대해 verdict 하나를 고르라.

- metric: 합산·평균·순위 계산 대상인 양(금액/수량/건수 등). 번호·차수·순번·코드는 metric이 아니다.
- identifier: 행 식별용 코드·번호 (계약번호, 사업자등록번호, 차수 등).
- date: 일자·년월·기한.
- flag: 여부·구분·유형·상태.
- dimension: 그 외 필터·그룹 기준 (기관명, 품명, 지역 등). 숫자 모양이어도 코드·순번이면 dimension.

반드시 JSON 배열만 출력하라 (설명 금지, 코드펜스 금지):
[{"name":"컬럼명","verdict":"metric","confidence":0.9,"reason":"이유 한 줄"}]
배열 길이는 반드시 {n}개, 입력 순서 그대로. 생략·병합 금지. 마지막 원소 뒤에
{"count":{n}} 을 추가하지 말고 배열만 출력하라.

대상 컬럼 (현재 분류·샘플 참고용, 정답 아님):
{items}"""


REPAIR_PROMPT = """앞 요청에서 빠진 컬럼만 판정하라. 반드시 JSON 배열만 출력 (설명·코드펜스 금지).
형식: [{"name":"컬럼명","verdict":"metric","confidence":0.9,"reason":"이유 한 줄"}]
배열 길이는 반드시 {n}개, 아래 순서 그대로. 생략·병합 금지.

빠진 컬럼:
{items}"""


def parse_json_array(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    arr = json.loads(t)
    assert isinstance(arr, list) and arr, "빈 배열/비배열 응답"
    for a in arr:
        assert isinstance(a, dict) and a.get("name"), f"형식 오류: {str(a)[:80]}"
        assert a.get("verdict") in VERDICTS, f"verdict 범위 밖: {a}"
        a.setdefault("confidence", 0.0)
        a.setdefault("reason", "")
    return arr


def validate(text, batch):
    """(합격분 배열, 빠진 이름 목록) 반환. 파싱 자체 실패 시 예외."""
    arr = parse_json_array(text)
    want = [c["name"] for c in batch]
    by_name = {a["name"]: a for a in arr}
    missing = [n for n in want if n not in by_name]
    extra = [a["name"] for a in arr if a["name"] not in want]
    if extra:
        raise ValueError(f"요청 외 이름 포함: {extra[:3]}")
    return [by_name[n] for n in want if n in by_name], missing


def judge(batch, verbose=True):
    """(합격분 배열, 빠진 이름[]) 또는 전체 실패 시 (None, err문자열)."""
    arr, missing_or_err = _judge_with(PROMPT, batch, verbose)
    if arr is None:
        return None, missing_or_err
    if missing_or_err:
        print(f"  빠진 {len(missing_or_err)}건 repair: {missing_or_err[:5]}")
        extra, still = repair(batch, missing_or_err, verbose)
        if extra is None:
            # repair 호출 자체 실패 → 합격분은 저장, 나머지는 미완료
            return arr, missing_or_err
        arr = arr + extra
        missing_or_err = still
    return arr, missing_or_err


def _judge_with(template, batch, verbose=True):
    """성공 시 (합격분 배열, 빠진 이름[]) — 빠짐없으면 missing=[]. 전체 실패 시 (None, err)."""
    items = "\n".join(
        f'- {c["name"]} (현재:{c["category"]}, 샘플:{c.get("sample_values") or []})'
        for c in batch)
    prompt = template.replace("{n}", str(len(batch))).replace("{items}", items)
    last = ""
    for attempt in range(2):
        try:
            text = chat([{"role": "user", "content": prompt}],
                        max_tokens=4096, temperature=0, verbose=verbose,
                        json_mode=True)
        except LLMExhausted as e:
            return None, f"LLM 소진: {e}"
        try:
            arr, missing = validate(text, batch)
            return arr, missing
        except Exception as e:
            last = f"검증 실패(시도{attempt+1}): {str(e)[:150]}"
            print(f"  {last}")
    return None, last


def repair(batch, missing, verbose=True):
    """빠진 이름만 재질의 → (추가분 배열, 빠진 이름[]) 또는 (None, err)."""
    sub = [c for c in batch if c["name"] in missing]
    if not sub:
        return [], []
    return _judge_with(REPAIR_PROMPT, sub, verbose)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-batches", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only-failed", action="store_true",
                    help="실패 배치만 재시도 (성공분은 손대지 않음)")
    a = ap.parse_args()

    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    cols = []
    for r in catalog:
        if r.get("columns_missing"):
            continue
        for c in r.get("columns") or []:
            cols.append({"rid": r["report_id"], "name": c["name"],
                         "category": c["category"],
                         "sample_values": c.get("sample_values") or []})
    batches = [cols[i:i + BATCH] for i in range(0, len(cols), BATCH)]
    print(f"[W2c] 컬럼 {len(cols)}개 → 배치 {len(batches)}개 ({BATCH}개씩)")

    prog = json.loads(PROG_PATH.read_text(encoding="utf-8")) if PROG_PATH.exists() else {}
    audit = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    if a.force:
        prog, audit = {}, {}
    if prog.get("_version") != PROG_VERSION:
        # v1(20개씩) → v2(10개씩): 구 배치키는 무효, audit 실존 기준으로 재유도
        prog = {"_version": PROG_VERSION}
        print("[W2c] progress v1 감지 → v2로 마이그레이션 (audit 실존분은 스킵됨)")

    def batch_complete(key, b):
        """감사 결과가 전부 있으면 진짜 완료 (audit이 ground truth, 배치 크기 무관)."""
        if a.force:
            return False
        return all(c["name"] in audit.get(c["rid"], {}) for c in b)

    idx = (range(len(batches)) if a.limit_batches is None
           else range(min(a.limit_batches, len(batches))))
    if a.only_failed:
        idx = [i for i in idx if not batch_complete(f"batch_{i:03d}", batches[i])]
        print(f"[W2c] only-failed: 대상 {len(idx)}배치")
    n_call = n_skip = n_fail = 0
    total = len(idx)
    for n, i in enumerate(idx):
        b = batches[i]
        key = f"batch_{i:03d}"
        if batch_complete(key, b):
            n_skip += 1
            continue
        # 부분 완료 배치: 빠진 것만 심사 (audit 실존분은 재호출 안 함)
        todo = [c for c in b if c["name"] not in audit.get(c["rid"], {})]
        print(f"[{n+1}/{total}] {key} ({todo[0]['rid']}:{todo[0]['name']} …) "
              f"심사 중 (신규 {len(todo)}/{len(b)})")
        arr, missing = judge(todo)
        if arr is None:
            prog[key] = {"ok": False, "error": missing}
            n_fail += 1
        else:
            got = {j["name"]: j for j in arr}
            for c in todo:
                if c["name"] in got:
                    j = got[c["name"]]
                    audit.setdefault(c["rid"], {})[c["name"]] = {
                        "verdict": j["verdict"], "confidence": j["confidence"],
                        "reason": j["reason"], "prev": c["category"], "batch": key}
            if missing:
                prog[key] = {"ok": False, "error": f"부분 성공, 잔여 {len(missing)}: {missing[:5]}"}
                n_fail += 1
            else:
                prog[key] = {"ok": True, "n": len(todo)}
            n_call += 1
        PROG_PATH.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    n_verdicts = sum(len(v) for v in audit.values())
    print(f"[W2c] calls={n_call} skipped={n_skip} failed_batches={n_fail} "
          f"verdicts={n_verdicts}/{len(cols)}")


if __name__ == "__main__":
    main()
