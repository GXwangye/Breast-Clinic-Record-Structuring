# -*- coding: utf-8 -*-
import argparse
import glob
import io
import json
import os
import re
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

# 条目容器候选键：不同产物把条目放在不同键下
ORDER_KEYS = ("items", "orders", "list", "follow_up", "drugs", "exams", "labs", "other")
DX_KEYS = ("items", "list", "diagnoses")

def load_dir(d):
    out = {}
    for fn in glob.glob(os.path.join(d, "*.json")):
        try:
            with io.open(fn, encoding="utf-8") as f:
                out[os.path.basename(fn)[:-5]] = json.load(f)
        except Exception:
            pass
    return out

def load_input():
    out = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out[r["record_id"]] = r
    return out

def load_wf(rids):
    out = {}
    want = set(rids)
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in want:
            out[rid] = rec
            if len(out) == len(want):
                break
    return out

def as_items(section, keys):
    if isinstance(section, list):
        return section
    if not isinstance(section, dict):
        return []
    got = []
    for k in keys:
        v = section.get(k)
        if isinstance(v, list):
            got.extend(v)
        elif isinstance(v, dict):
            got.append(v)
    return got

def all_text(obj, buf):
    if isinstance(obj, dict):
        for v in obj.values():
            all_text(v, buf)
    elif isinstance(obj, list):
        for v in obj:
            all_text(v, buf)
    elif isinstance(obj, str):
        buf.append(obj)

def norm(s):
    return re.sub(r"[\s，,。;；、\/\|（）()\[\]【】:：·\-—_]+", "", s or "")

SPLIT = re.compile(r"[\d]+[\.、）\)]\s*|[;；\n]")

def pieces(cell):
    out = []
    for p in SPLIT.split(cell or ""):
        p = norm(p)
        if len(p) >= 3:
            out.append(p)
    return out or ([norm(cell)] if len(norm(cell)) >= 3 else [])

def coverage(exp_cells, prod_text, keys):
    blob = norm(prod_text)
    exp_n = hit = 0
    for cell in exp_cells:
        for pc in pieces(cell):
            exp_n += 1
            if pc in blob:
                hit += 1
            else:
                # 长片段允许 80% 前缀命中（原文常带附加语）
                if len(pc) > 8 and pc[: int(len(pc) * 0.8)] in blob:
                    hit += 1
    return exp_n, hit

def count_old(section, keys):
    if isinstance(section, dict) and isinstance(section.get("items"), list):
        return len(section["items"])
    return 0

def measure(name, prods, inputs, rids):
    old_exp = old_got = 0          # 口径A
    b_exp = b_got = 0              # 口径B
    c_exp = c_hit = 0              # 口径C
    c_dx_exp = c_dx_hit = 0
    miss_rec = 0                   # 有原文医嘱但产物 0 条目的记录数
    struct_missing = 0             # 产物 orders 无 items 键的记录数

    for rid in rids:
        p = prods.get(rid)
        if p is None:
            continue
        inp = inputs.get(rid, {})
        exp_orders = [v for _, v in (inp.get("orders") or []) if v and v.strip()]
        exp_dx = [v for _, v in (inp.get("diagnoses") or []) if v and v.strip()]

        # A 旧口径
        old_exp += len(exp_orders)
        old_got += count_old(p.get("orders"), ORDER_KEYS)

        # B 结构归一
        its = as_items(p.get("orders"), ORDER_KEYS)
        b_exp += len(exp_orders)
        b_got += len(its)

        # C 内容覆盖
        buf = []
        all_text(p, buf)
        blob = "\n".join(buf)
        e, h = coverage(exp_orders, blob, ORDER_KEYS)
        c_exp += e
        c_hit += h
        e2, h2 = coverage(exp_dx, blob, DX_KEYS)
        c_dx_exp += e2
        c_dx_hit += h2

        if exp_orders and not its:
            miss_rec += 1
        if isinstance(p.get("orders"), dict) and not isinstance(p.get("orders").get("items"), list):
            struct_missing += 1

    def rate(miss, tot):
        return 100.0 * miss / tot if tot else 0.0

    return {
        "产物": name,
        "记录数": len(rids),
        "A_旧口径_条目数差": {
            "原文条目": old_exp, "产物items条目": old_got,
            "漏抽率%": round(rate(max(0, old_exp - old_got), old_exp), 2),
        },
        "B_结构归一口径": {
            "原文条目": b_exp, "产物条目": b_got,
            "漏抽率%": round(rate(max(0, b_exp - b_got), b_exp), 2),
            "orders无items键记录": struct_missing,
            "有原文医嘱但0条目记录": miss_rec,
        },
        "C_内容覆盖口径": {
            "原文医嘱成分": c_exp, "命中": c_hit,
            "漏抽率%": round(rate(c_exp - c_hit, c_exp), 2),
            "诊断成分": c_dx_exp, "诊断命中": c_dx_hit,
            "诊断漏抽率%": round(rate(c_dx_exp - c_dx_hit, c_dx_exp), 2),
        },
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    inputs = load_input()
    rids = sorted(inputs)
    print("原文记录 %d 条" % len(rids))

    naive, l1, l2 = load_dir(NAIVE), load_dir(L1), load_dir(L2)
    wf = load_wf(rids)
    print("产物：wf(终态tar) %d / naive %d / L1 %d / L2 %d"
          % (len(wf), len(naive), len(l1), len(l2)))

    # anchor 现状核查（终态 v7.1 起产物不附 anchor）
    n_anchor = sum(1 for r in wf.values() if json.dumps(r, ensure_ascii=False).count('"anchor"'))
    print("终态产物含 anchor 字段的记录数：%d / %d" % (n_anchor, len(wf)))

    rows = []
    for nm, pr in (("完整工作流(终态tar)", wf), ("裸LLM", naive), ("L1无标准化", l1), ("L2无分板块", l2)):
        shared = [r for r in rids if r in pr]
        rows.append(measure(nm, pr, inputs, shared))

    for r in rows:
        print("\n===== %s（%d 条共有）=====" % (r["产物"], r["记录数"]))
        print("  A 旧口径(只认 items):  原文 %5d 条 → 产物 %5d 条  漏抽 %6.2f%%"
              % (r["A_旧口径_条目数差"]["原文条目"], r["A_旧口径_条目数差"]["产物items条目"],
                 r["A_旧口径_条目数差"]["漏抽率%"]))
        b = r["B_结构归一口径"]
        print("  B 结构归一(条目数):    原文 %5d 条 → 产物 %5d 条  漏抽 %6.2f%%  [无items键 %d 条 / 有原文但0条目 %d 条]"
              % (b["原文条目"], b["产物条目"], b["漏抽率%"], b["orders无items键记录"], b["有原文医嘱但0条目记录"]))
        c = r["C_内容覆盖口径"]
        print("  C 内容覆盖(真漏抽):    医嘱成分 %5d → 命中 %5d  漏抽 %6.2f%%  |  诊断成分 %4d → 命中 %4d 漏抽 %6.2f%%"
              % (c["原文医嘱成分"], c["命中"], c["漏抽率%"], c["诊断成分"], c["诊断命中"], c["诊断漏抽率%"]))

    if a.json_out:
        out = {"样本100条": True, "口径说明": {
            "A": "复刻 eval_naive_vs_wf.py：只比 orders.items 条数，结构缺失与切分粒度均影响结果",
            "B": "把各产物条目容器归一后比条数，消除结构缺失伪影，仍受切分粒度影响",
            "C": "原文每个单元格切成成分，检查是否出现在产物全部文本中；不受条数影响，是真漏抽",
        }, "结果": rows}
        with io.open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\nwrote", a.json_out)

if __name__ == "__main__":
    main()
