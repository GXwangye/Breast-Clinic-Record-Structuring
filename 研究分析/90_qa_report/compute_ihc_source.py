# -*- coding: utf-8 -*-
import glob
import io
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
NAIVE = ROOT + "/publish/02_结构化结果与过程/中间产物/ablation_naive"
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"

# IHC 靶集判据（与 remediate_scope_index / remediate_aux 同源）
IHC_KW = re.compile(r"免疫组化|免疫组织化学")
MK = re.compile(r"(ER|PR|HER-?2?|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3)\s*(\(|:|\s*\d|\s*[+-])", re.I)

def has_ihc(text):
    return bool(IHC_KW.search(text) or len(MK.findall(text)) >= 2)

def marker_count(rec):
    n = 0
    ihc = rec.get("ihc")
    if isinstance(ihc, dict):
        for p in (ihc.get("panels") or []):
            if isinstance(p, dict):
                n += len(p.get("markers") or [])
    ae = rec.get("auxiliary_exam")
    if isinstance(ae, dict):
        ih = ae.get("ihc")
        if isinstance(ih, dict):
            for p in (ih.get("panels") or []):
                if isinstance(p, dict):
                    n += len(p.get("markers") or [])
    return n

def source_fields(rec):
    n = 0
    stack = [rec]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if o.get("source_field") not in (None, "", [], {}):
                n += 1
            stack.extend(v for k, v in o.items() if k != "source_field")
        elif isinstance(o, list):
            stack.extend(o)
    return n

def load_dir(d):
    out = {}
    for fn in glob.glob(os.path.join(d, "*.json")):
        try:
            with io.open(fn, encoding="utf-8") as f:
                out[os.path.basename(fn)[:-5]] = json.load(f)
        except Exception:
            pass
    return out

def load_wf(rids):
    want, out = set(rids), {}
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in want:
            out[rid] = rec
            if len(out) == len(want):
                break
    return out

def main():
    inputs = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            inputs[r["record_id"]] = r
    rids = sorted(inputs)

    # 原文 IHC 记录
    ihc_rid = set()
    for rid, r in inputs.items():
        txt = json.dumps(r.get("fields") or {}, ensure_ascii=False)
        if has_ihc(txt):
            ihc_rid.add(rid)
    print("原文含 IHC 的记录：%d / %d" % (len(ihc_rid), len(rids)))

    sets = [("完整工作流(终态tar)", load_wf(rids)), ("裸LLM", load_dir(NAIVE)),
            ("L1无标准化", load_dir(L1)), ("L2无分板块", load_dir(L2))]
    rows = []
    for nm, prods in sets:
        shared = [r for r in rids if r in prods]
        sub = [r for r in shared if r in ihc_rid]
        ok = sum(1 for r in sub if marker_count(prods[r]) > 0)
        sf = sum(source_fields(prods[r]) for r in shared)
        mk = sum(marker_count(prods[r]) for r in shared)
        rows.append({
            "产物": nm, "记录数": len(shared),
            "原文IHC记录": len(sub), "产物有marker记录": ok,
            "IHC记录级召回%": round(100.0 * ok / len(sub), 1) if sub else 0.0,
            "IHC漏抽%": round(100.0 * (len(sub) - ok) / len(sub), 1) if sub else 0.0,
            "source_field总": sf,
            "source_field每条": round(sf / len(shared), 1) if shared else 0.0,
            "marker总": mk, "marker每条": round(mk / len(shared), 2) if shared else 0.0,
        })

    print("%-22s %6s %8s %8s %9s %9s" % ("产物", "记录", "原文IHC", "有marker", "IHC漏抽%", "sf/条"))
    for r in rows:
        print("%-22s %6d %8d %8d %9.1f %9.1f" % (
            r["产物"], r["记录数"], r["原文IHC记录"], r["产物有marker记录"],
            r["IHC漏抽%"], r["source_field每条"]))

    out = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC与溯源重算.json"
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"口径": "IHC 为记录级（原文命中靶集 vs 产物 marker 非空）；source_field 为条目级非空计数",
                   "样本": "100 条", "结果": rows}, f, ensure_ascii=False, indent=1)
    print("\nwrote", out)

if __name__ == "__main__":
    main()
