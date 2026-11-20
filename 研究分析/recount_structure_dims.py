# -*- coding: utf-8 -*-
import glob
import io
import json
import os
import sys
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
NAIVE = ROOT + "/publish/02_结构化结果与过程/中间产物/ablation_naive"
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"

def keys_of(o, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            keys_of(v, acc)
    elif isinstance(o, list):
        for v in o:
            keys_of(v, acc)
    return acc

def leaves(o):
    if isinstance(o, dict):
        return sum(leaves(v) for v in o.values())
    if isinstance(o, list):
        return sum(leaves(v) for v in o)
    if o is None or o == "" or o == [] or o == {}:
        return 0
    return 1

def anchors(o):
    if isinstance(o, dict):
        n = 1 if o.get("anchor") else 0
        return n + sum(anchors(v) for k, v in o.items() if k != "anchor")
    if isinstance(o, list):
        return sum(anchors(v) for v in o)
    return 0

def markers(o):
    n = 0
    if isinstance(o, dict):
        if "marker_name" in o and ("raw_value" in o or "value" in o):
            n += 1
        for v in o.values():
            n += markers(v)
    elif isinstance(o, list):
        for v in o:
            n += markers(v)
    return n

def longstr(o):
    n = 0
    if isinstance(o, dict):
        for v in o.values():
            n += longstr(v)
    elif isinstance(o, list):
        for v in o:
            n += longstr(v)
    elif isinstance(o, str) and len(o) > 80:
        n += 1
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

def stat(name, prods, rids):
    ks = set()
    leaf = anc = mk = lb = 0
    top_missing = Counter()
    TOP = ["chief_complaint", "present_illness", "past_history", "physical_exam",
           "auxiliary_exam", "diagnoses", "treatment", "orders"]
    n = 0
    for rid in rids:
        p = prods.get(rid)
        if p is None:
            continue
        n += 1
        keys_of(p, ks)
        leaf += leaves(p)
        anc += anchors(p)
        mk += markers(p)
        lb += longstr(p)
        for t in TOP:
            if not p.get(t):
                top_missing[t] += 1
    return {
        "产物": name, "记录数": n,
        "唯一字段名": len(ks),
        "非空叶子总": leaf, "非空叶子每条": round(leaf / n, 1) if n else 0,
        "anchor总": anc, "anchor每条": round(anc / n, 1) if n else 0,
        "IHC规范marker总": mk, "marker每条": round(mk / n, 2) if n else 0,
        "长串(>80字)每条": round(lb / n, 1) if n else 0,
        "板块缺失": dict(top_missing),
    }

def main():
    rids = []
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            rids.append(json.loads(line)["record_id"])
    rids = sorted(rids)
    wf = load_wf(rids)
    sets = [("完整工作流(终态tar)", wf), ("裸LLM", load_dir(NAIVE)),
            ("L1无标准化", load_dir(L1)), ("L2无分板块", load_dir(L2))]
    rows = [stat(nm, pr, [r for r in rids if r in pr]) for nm, pr in sets]

    hdr = ["产物", "记录数", "唯一字段名", "非空叶子每条", "anchor每条", "marker每条", "长串每条"]
    print("%-22s %6s %10s %12s %10s %10s %9s" % tuple(hdr))
    for r in rows:
        print("%-22s %6d %10d %12.1f %10.1f %10.2f %9.1f" % (
            r["产物"], r["记录数"], r["唯一字段名"], r["非空叶子每条"],
            r["anchor每条"], r["marker每条"], r["长串(>80字)每条"]))
    print()
    for r in rows:
        if r["板块缺失"]:
            print("%-22s 板块缺失: %s" % (r["产物"], r["板块缺失"]))
        else:
            print("%-22s 板块缺失: 无" % r["产物"])

    out = ROOT + "/publish/02_结构化结果与过程/qa_reports/六维结构指标重算.json"
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"样本": "100 条测试子集", "结果": rows}, f, ensure_ascii=False, indent=1)
    print("\nwrote", out)

if __name__ == "__main__":
    main()
