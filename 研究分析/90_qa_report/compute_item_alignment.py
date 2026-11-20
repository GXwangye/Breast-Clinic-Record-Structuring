# -*- coding: utf-8 -*-
import glob
import io
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
NAIVE = ROOT + "/publish/02_结构化结果与过程/中间产物/ablation_naive"
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"
KEYS = ("items", "orders", "list", "follow_up", "drugs", "exams", "labs", "other")

def as_items(sec):
    if isinstance(sec, list):
        return sec
    if not isinstance(sec, dict):
        return []
    got = []
    for k in KEYS:
        v = sec.get(k)
        if isinstance(v, list):
            got.extend(v)
        elif isinstance(v, dict):
            got.append(v)
    return got

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

def measure(nm, prods, inputs, rids):
    o_exp = o_got = d_exp = d_got = 0
    o_struct_gap = d_struct_gap = 0     # 有原文但产物该板块 0 条目
    no_items_key = 0                    # orders 无 items 键的记录数
    for rid in rids:
        p = prods.get(rid)
        if p is None:
            continue
        inp = inputs.get(rid, {})
        eo = [v for _, v in (inp.get("orders") or []) if v and v.strip()]
        ed = [v for _, v in (inp.get("diagnoses") or []) if v and v.strip()]
        oi = as_items(p.get("orders"))
        di = as_items(p.get("diagnoses"))
        o_exp += len(eo)
        o_got += len(oi)
        d_exp += len(ed)
        d_got += len(di)
        if eo and not oi:
            o_struct_gap += 1
        if ed and not di:
            d_struct_gap += 1
        if isinstance(p.get("orders"), dict) and not isinstance(p.get("orders").get("items"), list):
            no_items_key += 1
    n = len(rids)
    return {
        "产物": nm, "记录数": n,
        "医嘱": {"原文非空字段": o_exp, "产物条目": o_got,
                 "差": o_got - o_exp,
                 "有原文但0条目记录": o_struct_gap,
                 "orders无items键记录": no_items_key},
        "诊断": {"原文非空字段": d_exp, "产物条目": d_got,
                 "差": d_got - d_exp,
                 "有原文但0条目记录": d_struct_gap},
    }

def main():
    inputs = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            inputs[r["record_id"]] = r
    rids = sorted(inputs)
    wf = load_wf(rids)
    sets = [("完整工作流(终态tar)", wf), ("裸LLM", load_dir(NAIVE)),
            ("L1无标准化", load_dir(L1)), ("L2无分板块", load_dir(L2))]
    rows = []
    for nm, pr in sets:
        rows.append(measure(nm, pr, inputs, [r for r in rids if r in pr]))

    print("%-22s %8s %8s %6s %10s | %6s %6s %6s" %
          ("产物", "医嘱原文", "医嘱条目", "差值", "医嘱0条目", "诊断原文", "诊断条目", "差值"))
    for r in rows:
        o, d = r["医嘱"], r["诊断"]
        print("%-22s %8d %8d %6d %10d | %6d %6d %6d" %
              (r["产物"], o["原文非空字段"], o["产物条目"], o["差"],
               o["有原文但0条目记录"], d["原文非空字段"], d["产物条目"], d["差"]))

    out = ROOT + "/publish/02_结构化结果与过程/qa_reports/条目级对齐重算.json"
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"口径": "条目级对齐：原文非空字段数 vs 产物条目数，双向报差，不做 max(0) 截断",
                   "样本": "100 条测试子集", "结果": rows}, f, ensure_ascii=False, indent=1)
    print("\nwrote", out)

if __name__ == "__main__":
    main()
