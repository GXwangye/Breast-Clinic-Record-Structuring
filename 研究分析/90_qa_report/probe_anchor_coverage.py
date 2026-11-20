# -*- coding: utf-8 -*-
import json
import io
import os
import re
import sys
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC锚点覆盖.json"
FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]

def norm(s):
    s = str(s or "").translate(str.maketrans("０１２３４５６７８９％＋－（）", "0123456789%+-()"))
    return re.sub(r"[\s,，、;；。．\.]+", "", s)

def main():
    inp = {}
    for line in io.open(INPUT, encoding="utf-8"):
        r = json.loads(line)
        inp[r["record_id"]] = r

    pan_n = anc_n = quote_hit = 0
    mk_total = mk_in_anchored = mk_in_quote = 0
    quote_len = []
    no_anchor_keys = Counter()
    ex = []

    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid not in inp:
            continue
        ih = (rec.get("auxiliary_exam") or {}).get("ihc")
        if not isinstance(ih, dict):
            continue
        for pan in (ih.get("panels") or []):
            if not isinstance(pan, dict):
                continue
            pan_n += 1
            ms = [m for m in (pan.get("markers") or []) if isinstance(m, dict)]
            mk_total += len(ms)
            an = pan.get("anchor")
            if not (isinstance(an, dict) and an.get("quote")):
                no_anchor_keys[tuple(sorted(pan.keys()))] += 1
                continue
            anc_n += 1
            q = norm(an["quote"])
            quote_len.append(len(an["quote"]))
            fld = an.get("field")
            if fld:
                src = norm((inp[rid]["fields"] or {}).get(fld) or "")
            else:
                src = norm(" ".join(str((inp[rid]["fields"] or {}).get(k) or "") for k in FIELDS))
            if q and q in src:
                quote_hit += 1
            elif len(ex) < 5:
                ex.append({"record_id": rid, "field": fld, "quote": an["quote"][:80]})
            mk_in_anchored += len(ms)
            for m in ms:
                nm = norm(m.get("marker_name"))
                if nm and nm in q:
                    mk_in_quote += 1

    ql = sorted(quote_len)
    res = {
        "样本": "100 条测试子集",
        "口径": "panel 级 anchor 存在性与逐字命中；quote 长度分布；marker 名称是否落在自身 panel 的 quote 内",
        "panel总数": pan_n,
        "panel带anchor": anc_n,
        "panel无anchor": pan_n - anc_n,
        "quote逐字命中原文": quote_hit,
        "quote命中率": round(100.0 * quote_hit / anc_n, 1) if anc_n else None,
        "quote长度": {
            "最小": ql[0] if ql else None,
            "中位": ql[len(ql) // 2] if ql else None,
            "最大": ql[-1] if ql else None,
            "超200字条数": sum(1 for x in ql if x > 200),
        },
        "marker总数": mk_total,
        "marker属于带anchor面板": mk_in_anchored,
        "marker名称在quote内": mk_in_quote,
        "可核比例_占锚定面板": round(100.0 * mk_in_quote / mk_in_anchored, 1) if mk_in_anchored else None,
        "可核比例_占全部marker": round(100.0 * mk_in_quote / mk_total, 1) if mk_total else None,
        "无anchor面板的键组合": {"|".join(k): v for k, v in no_anchor_keys.most_common()},
        "quote未命中例": ex,
        "结论": "anchor 为定位锚而非证据全文：逐字可靠，但覆盖不足以保证逐 marker 可核",
    }
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    for k, v in res.items():
        if k in ("quote未命中例", "无anchor面板的键组合"):
            continue
        print("%-22s %s" % (k, v))
    print("无anchor面板键组合:", res["无anchor面板的键组合"])
    print("->", OUT)

if __name__ == "__main__":
    main()
