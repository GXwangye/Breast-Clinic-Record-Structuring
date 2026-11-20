# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
INPUT = DATA + "/中间产物/_agents_input/input_records.jsonl"
DEFECT_JSON = DATA + "/qa_reports/两条缺陷_实证.json"

RE_FULL = re.compile(r"(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")
RE_YM = re.compile(r"(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*月?")
RE_MD = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日")

KW = ("超声", "B超", "彩超", "病理", "CT", "MRI", "磁共振", "钼靶", "X线",
      "心电", "核医学", "骨显像", "PET", "血常规", "生化", "肿瘤标志物")

def core_terms(exam_name):
    t = [k for k in KW if k in exam_name]
    if not t and len(exam_name) >= 2:
        t = [exam_name[:2]]
    return t

def near_date(text, terms, win=40):
    for m in RE_FULL.finditer(text):
        seg = text[max(0, m.start() - win): m.end() + win]
        if any(t in seg for t in terms):
            return m.group(0), seg
    for m in RE_MD.finditer(text):
        seg = text[max(0, m.start() - win): m.end() + win]
        if any(t in seg for t in terms):
            return m.group(0), seg
    return None, ""

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return ""
    return str(v).strip()

def classify(text):
    if RE_FULL.search(text) or RE_MD.search(text):
        return "原文有完整日期"
    if RE_YM.search(text):
        return "仅年月或仅年"
    return "原文未载明"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--input", default=INPUT)
    ap.add_argument("--defect-json", default=DEFECT_JSON)
    ap.add_argument("--json-out", default=DATA + "/qa_reports/空日期_回原文核对.json")
    ap.add_argument("--show", type=int, default=12)
    a = ap.parse_args()

    with open(a.defect_json, encoding="utf-8") as f:
        d = json.load(f)
    samples = d["缺陷一_空日期无标记"]["抽样明细"][: a.sample]
    want = {x["record_id"] for x in samples}
    print("抽样 %d 条，涉及 record_id %d 个" % (len(samples), len(want)))

    fields_by_rid = {}
    with open(a.input, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            rid = _s(obj.get("record_id"))
            if rid in want:
                fields_by_rid[rid] = obj.get("fields") or {}
    print("命中原文 %d 个 record_id" % len(fields_by_rid))

    import collections
    C = collections.Counter()
    detail = []
    for s in samples:
        rid = s["record_id"]
        flds = fields_by_rid.get(rid)
        if not flds:
            C["原文缺失"] += 1
            continue
        sf = s.get("source_field") or ""
        if sf and sf in flds:
            text = _s(flds.get(sf))
            scope = "source_field"
        else:
            text = "\n".join(_s(v) for v in flds.values())
            scope = "全部字段"
        verdict = classify(text)
        C[verdict] += 1
        C["scope_" + scope] += 1

        # 二级判据：日期是否与检查名同句（漏抽置信度）
        en = s.get("exam_name") or s.get("exam_name_alt") or ""
        hit, seg = near_date(text, core_terms(en)) if en else (None, "")
        if hit:
            C["日期与检查同句_疑似漏抽"] += 1
        elif verdict == "原文有完整日期" or verdict == "仅年月或仅年":
            C["有日期但不同句_无法归因"] += 1

        if len(detail) < a.show:
            m = RE_FULL.search(text) or RE_MD.search(text) or RE_YM.search(text)
            if m:
                b = max(0, m.start() - 60)
                detail.append({
                    "record_id": rid,
                    "exam_name": s.get("exam_name"),
                    "source_field": sf,
                    "scope": scope,
                    "verdict": verdict,
                    "同日句": bool(hit),
                    "hit": m.group(0),
                    "context": text[b:m.end() + 60].replace("\n", " "),
                })

    out = {
        "sample_size": len(samples),
        "原文命中": len(fields_by_rid),
        "分类": {k.replace("scope_", "搜索范围_"): v for k, v in C.items()},
        "疑似漏抽_日期与检查同句": C["日期与检查同句_疑似漏抽"],
        "有日期但不同句_无法归因": C["有日期但不同句_无法归因"],
        "命中片段": detail,
    }
    os.makedirs(os.path.dirname(a.json_out), exist_ok=True)
    with open(a.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n== 分类 ==")
    for k in ("原文有完整日期", "仅年月或仅年", "原文未载明", "原文缺失"):
        if C[k]:
            print("  %-12s %4d  (%.1f%%)" % (k, C[k], 100.0 * C[k] / max(1, len(samples))))
    print("  ⭐ 日期与检查同句（疑似漏抽） %d (%.1f%%)"
          % (C["日期与检查同句_疑似漏抽"], 100.0 * C["日期与检查同句_疑似漏抽"] / max(1, len(samples))))
    print("     有日期但不同句（无法归因） %d (%.1f%%)"
          % (C["有日期但不同句_无法归因"], 100.0 * C["有日期但不同句_无法归因"] / max(1, len(samples))))
    print("  搜索范围:", {k[6:]: v for k, v in C.items() if k.startswith("scope_")})
    print("\n== 命中片段（前 %d 条）==" % len(detail))
    for x in detail:
        print("  [%s] %s / %s" % (x["record_id"], x["exam_name"][:18], x["verdict"]))
        print("      hit=%r  %s" % (x["hit"], x["context"][:150]))
    print("\nwrote " + a.json_out)

if __name__ == "__main__":
    main()
