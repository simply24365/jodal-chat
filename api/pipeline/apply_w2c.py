#!/usr/bin/env python3
"""W2c 확정 반영: LLM verdict → 최종 카탈로그. 원본 불변 (w2b/audit 읽기만).

규칙 (사용자 승인):
  - 기각 3건 → 휴리스틱으로 revert:
      00599/순위 -> dimension (순위 합산 무의미)
      00282/시스템유형 -> flag, 00288/구분 -> flag (구분·유형=flag 방침)
  - 격리: 샘플에 'PROCESSING REQUEST' 포함 컬럼 → quarantine:true (검색 제외, 판정은 유지)
  - 그 외 변경 227건 수용. needs_review: confidence < 0.7 표기만.

출력:
  - data/catalog/report_catalog_w2c.json (columns[].category = 최종값 + w2c 감사 필드)
  - data/catalog/w2c_decisions.json (결정 로그)
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2b.json"
AUDIT_PATH = BASE_DIR / "data" / "catalog" / "w2c_audit.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
LOG_PATH = BASE_DIR / "data" / "catalog" / "w2c_decisions.json"

REVERTS = {
    ("00599", "순위"): "dimension",
    ("00282", "시스템유형"): "flag",
    ("00288", "구분"): "flag",
}


def main():
    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    decisions = {"reverts": [], "quarantined": [], "needs_review": []}
    n_change = n_accept = 0

    out = []
    for r in catalog:
        rec = dict(r)
        if r.get("columns_missing") or not r.get("columns"):
            rec["w2c_status"] = "no_columns"
            out.append(rec)
            continue
        cols = []
        for c in r["columns"]:
            c2 = dict(c)
            j = audit.get(r["report_id"], {}).get(c["name"])
            if not j:
                c2["w2c"] = {"status": "missing"}
                cols.append(c2)
                continue
            prev, verdict = c["category"], j["verdict"]
            changed = prev != verdict
            n_change += changed
            key = (r["report_id"], c["name"])
            if key in REVERTS:
                final, reverted = REVERTS[key], True
                decisions["reverts"].append(
                    {"report_id": r["report_id"], "name": c["name"],
                     "prev": prev, "verdict": verdict, "final": final})
            else:
                final, reverted = verdict, False
                n_accept += changed
            quar = any("PROCESSING REQUEST" in str(s) for s in (c.get("sample_values") or []))
            if quar:
                decisions["quarantined"].append(
                    {"report_id": r["report_id"], "name": c["name"], "final": final})
            conf = j.get("confidence", 0)
            try:
                conf_f = float(conf)
            except Exception:
                conf_f = 0
            if conf_f < 0.7:
                decisions["needs_review"].append(
                    {"report_id": r["report_id"], "name": c["name"],
                     "final": final, "confidence": conf})
            c2["category"] = final
            c2["is_metric"] = (final == "metric")
            c2["w2c"] = {"prev": prev, "verdict": verdict, "final": final,
                         "confidence": conf, "reason": j.get("reason", ""),
                         "via": j.get("via", "api"), "batch": j.get("batch"),
                         "reverted": reverted, "quarantine": quar,
                         "needs_review": conf_f < 0.7}
            cols.append(c2)
        # 요약 재계산 (최종값 기준, 격리분 제외)
        live = [c for c in cols if not c["w2c"].get("quarantine")]
        rec["columns"] = cols
        rec["metrics"] = [c["name"] for c in live if c["category"] == "metric"]
        rec["dimensions"] = [c["name"] for c in live if c["category"] == "dimension"]
        rec["identifiers"] = [c["name"] for c in live if c["category"] == "identifier"]
        rec["dates"] = [c["name"] for c in live if c["category"] == "date"]
        rec["total_columns"] = len(live)
        rec["w2c_status"] = "applied"
        out.append(rec)

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    decisions["summary"] = {"changed": n_change, "accepted": n_accept,
                            "reverted": len(decisions["reverts"]),
                            "quarantined": len(decisions["quarantined"]),
                            "needs_review": len(decisions["needs_review"])}
    LOG_PATH.write_text(json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[W2c-apply] changed={n_change} accepted={n_accept} "
          f"reverted={len(decisions['reverts'])} quarantined={len(decisions['quarantined'])} "
          f"needs_review={len(decisions['needs_review'])}")
    print(f"  -> {OUT_PATH}\n  -> {LOG_PATH}")
    assert n_change == 230 and n_accept == 227, decisions["summary"]


if __name__ == "__main__":
    main()
