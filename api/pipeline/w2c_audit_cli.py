#!/usr/bin/env python3
"""W2c 심사 — gemini CLI (`gemini -p`) 경유 배치 심판. 신규 파일, 기존 감사결과와 병합.

동작:
  - data/catalog/report_catalog_w2b.json 에서 10개씩 배치 (w2c_audit.py 와 동일 순서/키)
  - w2c_audit.json 에 없는 컬럼만 심사 (멱등: 완료분 스킵)
  - 배치당: gemini -p 프롬프트 → stdout 에서 JSON 배열 추출 → 검증
    → 빠진 이름은 repair 프롬프트 1회 → 그래도 안 되면 배치 실패 기록 후 다음
  - 결과는 기존 w2c_audit.json / w2c_progress.json 에 바로 병합 (스키마 호환)

사용:
  python3 tools/w2c_audit_cli.py --dry-run            # 미완 배치 1개만 (검증용)
  python3 tools/w2c_audit_cli.py                      # 남은 것 전부
  python3 tools/w2c_audit_cli.py --only-failed        # 실패 배치만
  python3 tools/w2c_audit_cli.py --limit-batches 3    # 앞에서 3배치만
  GEMINI_CLI_MODEL=gemini-3.1-flash-lite python3 tools/w2c_audit_cli.py --dry-run
"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2b.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "w2c_audit.json"
PROG_PATH = BASE_DIR / "data" / "catalog" / "w2c_progress.json"
PROG_VERSION = 2
BATCH = 10
VERDICTS = {"metric", "dimension", "identifier", "date", "flag"}
VIA = "gemini-cli"

PROMPT = """조달데이터허브 보고서 출력 컬럼의 분류를 판정하라. 각 컬럼에 대해 verdict 하나를 고르라.

- metric: 합산·평균·순위 계산 대상인 양(금액/수량/건수 등). 번호·차수·순번·코드는 metric이 아니다.
- identifier: 행 식별용 코드·번호 (계약번호, 사업자등록번호, 차수 등).
- date: 일자·년월·기한.
- flag: 여부·구분·유형·상태.
- dimension: 그 외 필터·그룹 기준 (기관명, 품명, 지역 등). 숫자 모양이어도 코드·순번이면 dimension.

반드시 JSON 배열만 출력하라 (설명 금지, 코드펜스 금지):
[{"name":"컬럼명","verdict":"metric","confidence":0.9,"reason":"이유 한 줄"}]
배열 길이는 반드시 {n}개, 입력 순서 그대로. 생략·병합 금지.

대상 컬럼 (현재 분류·샘플 참고용, 정답 아님):
{items}"""

REPAIR_PROMPT = """앞 요청에서 빠진 컬럼만 판정하라. 반드시 JSON 배열만 출력 (설명·코드펜스 금지).
형식: [{"name":"컬럼명","verdict":"metric","confidence":0.9,"reason":"이유 한 줄"}]
배열 길이는 반드시 {n}개, 아래 순서 그대로. 생략·병합 금지.

빠진 컬럼:
{items}"""


def run_gemini(prompt, timeout, model=None):
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd[1:1] = ["-m", model]
    # CLI가 GOOGLE_* 키를 우선 참조할 수 있어 GEMINI_API_KEY와 동기화 (혼합키 오인증 방지)
    env = dict(os.environ)
    if env.get("GEMINI_API_KEY"):
        env["GOOGLE_GENERATIVE_AI_API_KEY"] = env["GEMINI_API_KEY"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "gemini CLI 타임아웃"
    out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    if r.returncode != 0 and not r.stdout.strip():
        return None, f"exit={r.returncode} {out.strip()[:200]}"
    return out, None


def extract_array(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()
    m = re.search(r"\[.*\]", t, re.DOTALL)
    if not m:
        raise ValueError(f"JSON 배열 없음: {t[:150]}")
    arr = json.loads(m.group(0))
    if not isinstance(arr, list) or not arr:
        raise ValueError("빈 배열/비배열 응답")
    for a in arr:
        if not isinstance(a, dict) or not a.get("name"):
            raise ValueError(f"형식 오류: {str(a)[:80]}")
        if a.get("verdict") not in VERDICTS:
            raise ValueError(f"verdict 범위 밖: {a}")
        a.setdefault("confidence", 0.0)
        a.setdefault("reason", "")
    return arr


def judge_items(items, timeout, model, verbose=True):
    """(합격분, 빠진이름[]) 또는 전체실패 시 (None, err)."""
    names = [c["name"] for c in items]
    detail = "\n".join(
        f'- {c["name"]} (현재:{c["category"]}, 샘플:{c.get("sample_values") or []})'
        for c in items)

    def attempt(template, targets):
        prompt = template.replace("{n}", str(len(targets))).replace(
            "{items}",
            "\n".join(
                f'- {c["name"]} (현재:{c["category"]}, 샘플:{c.get("sample_values") or []})'
                for c in targets))
        if verbose:
            print(f"  gemini -p 요청 ({len(targets)}개)", flush=True)
        out, err = run_gemini(prompt, timeout, model)
        if out is None:
            return None, err
        try:
            arr = extract_array(out)
        except Exception as e:
            return None, f"파싱 실패: {e}"
        by_name = {a["name"]: a for a in arr}
        extra = [k for k in by_name if k not in names]
        if extra:
            return None, f"요청 외 이름 포함: {extra[:3]}"
        got = [by_name[n] for n in names if n in by_name]
        missing = [n for n in names if n not in by_name]
        return got, missing

    last = ""
    for _ in range(2):
        got, missing = attempt(PROMPT, items) if True else (None, None)
        if got is None:
            last = missing  # err 문자열
            print(f"  {last}", flush=True)
            continue
        if not missing:
            return got, []
        print(f"  빠진 {len(missing)}건 repair: {missing[:5]}", flush=True)
        sub = [c for c in items if c["name"] in missing]
        extra, still = attempt(REPAIR_PROMPT, sub)
        if extra is None:
            return got, missing
        return got + extra, still
    return None, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="미완 배치 1개만 처리")
    ap.add_argument("--limit-batches", type=int, default=None)
    ap.add_argument("--only-failed", action="store_true")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    model = os.environ.get("GEMINI_CLI_MODEL")

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
    print(f"[W2c-cli] 컬럼 {len(cols)}개 → 배치 {len(batches)}개 ({BATCH}개씩)"
          + (f" model={model}" if model else " model=cli-default"))

    prog = json.loads(PROG_PATH.read_text(encoding="utf-8")) if PROG_PATH.exists() else {}
    audit = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    if prog.get("_version") != PROG_VERSION:
        prog["_version"] = PROG_VERSION

    def incomplete(i):
        b = batches[i]
        return [c for c in b if c["name"] not in audit.get(c["rid"], {})]

    idx = list(range(len(batches)))
    if a.limit_batches is not None:
        idx = idx[:a.limit_batches]
    if a.only_failed:
        idx = [i for i in idx
               if prog.get(f"batch_{i:03d}", {}).get("ok") is False or incomplete(i)]
    idx = [i for i in idx if incomplete(i)]
    if a.dry_run:
        idx = idx[:1]
    print(f"[W2c-cli] 대상 {len(idx)}배치" + (" (dry-run 1)" if a.dry_run else ""))
    if not idx:
        print("[W2c-cli] 할 일 없음 (전부 완료)")
        return

    n_call = n_fail = 0
    for n, i in enumerate(idx):
        b = batches[i]
        key = f"batch_{i:03d}"
        todo = incomplete(i)
        print(f"[{n+1}/{len(idx)}] {key} ({todo[0]['rid']}:{todo[0]['name']} …) "
              f"신규 {len(todo)}/{len(b)}", flush=True)
        arr, missing = judge_items(todo, a.timeout, model)
        if arr is None:
            prog[key] = {"ok": False, "error": missing, "via": VIA}
            n_fail += 1
        else:
            got = {j["name"]: j for j in arr}
            for c in todo:
                if c["name"] in got:
                    j = got[c["name"]]
                    audit.setdefault(c["rid"], {})[c["name"]] = {
                        "verdict": j["verdict"], "confidence": j["confidence"],
                        "reason": j["reason"], "prev": c["category"],
                        "batch": key, "via": VIA}
            if missing:
                prog[key] = {"ok": False,
                             "error": f"부분 성공, 잔여 {len(missing)}: {missing[:5]}",
                             "via": VIA}
                n_fail += 1
            else:
                prog[key] = {"ok": True, "n": len(todo), "via": VIA}
            n_call += 1
        PROG_PATH.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(v) for v in audit.values())
    print(f"[W2c-cli] calls={n_call} failed={n_fail} verdicts={total}/{len(cols)}")


if __name__ == "__main__":
    main()
