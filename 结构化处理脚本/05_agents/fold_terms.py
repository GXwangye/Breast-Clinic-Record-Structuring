# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import re
import unicodedata

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"

DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "drug"]

# 确定等价替换（顺序有意义：长串先替）
FOLD = [
    ("乳腺癌癌", "乳腺癌"),
    ("乳房", "乳腺"),
    ("乳癌", "乳腺癌"),
    ("手术后", "术后"),
    ("彩超", "超声"),
    ("b超", "超声"),
    ("超声检查", "超声"),
    ("钼铑", "钼靶"),
]

SIDE_HEAD = re.compile(r"^(双侧|左侧|右侧|左侧|右侧|单侧|左|右|双)")
SIDE_TAIL = re.compile(r"(双侧|左侧|右侧|单侧)$")

PUNCT = re.compile(r"[\s（）()\[\]【】、,，.。;；:：\"'“”‘’·\-—_/\\|]+")

# 元话语：括号整段是注释/缺失说明，剥掉不影响语义
META_PAREN = re.compile(r"[（(][^（()）]*(?:未提及|未记录|不详|见|报告|日期|具体|待查|略|同上)[^（()）]*[）)]")
# 尾部时长：「术后20余天」「3年」「2月」
DUR = re.compile(r"(?:术后)?\d+\s*(?:余)?\s*(?:天|日|周|月|年|小时)(?:余)?$")

def squash(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = PUNCT.sub("", s)
    return s

def fold(s):
    stripped = []
    t = unicodedata.normalize("NFKC", s or "")
    t = t.strip()

    m = SIDE_HEAD.match(t)
    lat = "未指明"
    if m:
        w = m.group(1)
        lat = "双" if "双" in w else ("左" if "左" in w else "右")

    t2 = META_PAREN.sub(lambda mo: "", t)
    if t2 != t:
        stripped.append("paren")
        t = t2

    t2 = DUR.sub("", t)
    if t2 != t:
        stripped.append("duration")
        t = t2

    t = PUNCT.sub("", unicodedata.normalize("NFKC", t).lower())
    m = SIDE_HEAD.match(t)
    if m:
        if lat == "未指明":
            w = m.group(1)
            lat = "双" if "双" in w else ("左" if "左" in w else "右")
        t = t[len(m.group(1)):]
    t = SIDE_TAIL.sub("", t)

    for a, b in FOLD:
        t = t.replace(a, b)
    if not t:
        t = squash(s)
    return t, lat, stripped

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 个最大块")
    a = ap.parse_args()

    doms = [a.domain] if a.domain else DOMAINS
    summary = {}

    for dom in doms:
        p = os.path.join(NORM, "terms_%s.jsonl" % dom)
        if not os.path.exists(p):
            print("跳过 %s：缺 terms_%s.jsonl" % (dom, dom))
            continue
        rows = [json.loads(l) for l in open(p, encoding="utf-8")]
        out = []
        blocks = collections.defaultdict(list)
        for r in rows:
            k, lat, stripped = fold(r["literal"])
            rec = {"key": k, "literal": r["literal"], "count": r.get("count", 0),
                   "laterality": lat, "stripped": stripped,
                   "records": (r.get("records") or [])[:3]}
            out.append(rec)
            blocks[k].append(rec)

        op = os.path.join(NORM, "fold_%s.jsonl" % dom)
        with open(op, "w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        occ = sum(r["count"] for r in out)
        multi = {k: v for k, v in blocks.items() if len(v) > 1}
        summary[dom] = {
            "literals": len(rows), "blocks": len(blocks),
            "compress": round(1.0 * len(blocks) / len(rows), 4),
            "occurrences": occ,
            "multi_blocks": len(multi),
            "laterality": dict(collections.Counter(r["laterality"] for r in out)),
            "stripped": dict(collections.Counter(x for r in out for x in r["stripped"])),
        }
        print("%-13s 写法 %6d → 块 %6d（%.1f%%）  多写法块 %5d  出现 %7d"
              % (dom, len(rows), len(blocks), 100.0 * len(blocks) / len(rows),
                 len(multi), occ))

        if a.show:
            top = sorted(blocks.items(), key=lambda kv: -sum(x["count"] for x in kv[1]))[:a.show]
            for k, v in top:
                print("   [%s] %d 变体 / %d 次  %s"
                      % (k[:22], len(v), sum(x["count"] for x in v),
                         " | ".join(x["literal"][:16] for x in v[:5])))

    with open(os.path.join(NORM, "fold_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("\n汇总写入 fold_summary.json")

if __name__ == "__main__":
    main()
