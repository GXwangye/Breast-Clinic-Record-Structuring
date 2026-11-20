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
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"

IHC_KW = re.compile(r"免疫组化|免疫组织化学")
MK = re.compile(r"(ER|PR|HER-?2?|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3)"
                r"\s*(\(|:|\s*\d|\s*[+-])", re.I)
FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
# 明确「本次没有 IHC 结果」的措辞
NO_RESULT = re.compile(r"免疫组化未做|未行免疫组化|建议(做|行)?免疫组化|加做免疫组化|待免疫组化|免疫组化协诊|"
                       r"免疫组化结果未(出|归|回)|已送免疫组化")

def has_ihc(text):
    return bool(IHC_KW.search(text) or len(MK.findall(text)) >= 2)

def panels_of(rec):
    out = []
    for path in ("ihc", "auxiliary_exam"):
        o = rec.get(path)
        if isinstance(o, dict):
            ih = o if path == "ihc" else o.get("ihc")
            if isinstance(ih, dict):
                for p in (ih.get("panels") or []):
                    if isinstance(p, dict):
                        out.append(p)
    return out

def marker_list(rec):
    mk = []
    for p in panels_of(rec):
        for m in (p.get("markers") or []):
            if isinstance(m, dict):
                mk.append(m)
    return mk

def mname(m):
    for k in ("name", "marker", "marker_name", "symbol"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

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

def aux_reports(rec):
    ae = rec.get("auxiliary_exam")
    if not isinstance(ae, dict):
        return 0, ""
    n, txt = 0, []
    for rg in (ae.get("report_groups") or []):
        if isinstance(rg, dict):
            for r in (rg.get("reports") or []):
                if isinstance(r, dict) and IHC_KW.search(str(r.get("exam_name") or "")):
                    n += 1
                    txt.append(json.dumps(r, ensure_ascii=False)[:300])
    for r in (ae.get("reports") or []):
        if isinstance(r, dict) and IHC_KW.search(str(r.get("exam_name") or "")):
            n += 1
            txt.append(json.dumps(r, ensure_ascii=False)[:300])
    return n, " | ".join(txt)

def excluded(rec):
    out = []
    stack = [rec]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "excluded_text" and isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            out.append(str(it.get("text") or "")[:160])
                        elif isinstance(it, str):
                            out.append(it[:160])
                else:
                    stack.append(v)
        elif isinstance(o, list):
            stack.extend(o)
    return out

def notes(rec):
    out = []
    stack = [rec]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            v = o.get("note")
            if isinstance(v, str) and v.strip():
                out.append(v[:200])
            stack.extend(x for k, x in o.items() if k != "note")
        elif isinstance(o, list):
            stack.extend(o)
    return out

def main():
    inputs = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            inputs[r["record_id"]] = r
    rids = sorted(inputs)
    wf = load_wf(rids)
    l2, l1 = load_dir(L2), load_dir(L1)

    rows = []
    for rid in rids:
        fields = inputs[rid].get("fields") or {}
        hits = [k for k in FIELDS if has_ihc(fields.get(k) or "")]
        if not hits:
            continue
        src = " ".join(fields.get(k) or "" for k in FIELDS)
        no_result = bool(NO_RESULT.search(src))
        w = wf.get(rid, {})
        n_rep, rep_txt = aux_reports(w)
        exc = excluded(w)
        nts = notes(w)
        mk_w = marker_list(w)
        mk_g = marker_list(l2.get(rid, {}))
        rows.append({
            "record_id": rid,
            "IHC字段": hits,
            "原文似无IHC结果": no_result,
            "工作流panels": len(mk_w),
            "工作流报告条目": n_rep,
            "工作流excluded": len(exc),
            "L2markers": len(mk_g),
            "L1markers": len(marker_list(l1.get(rid, {}))),
            "工作流marker名": [mname(m) for m in mk_w][:10],
            "L2marker名": [mname(m) for m in mk_g][:10],
            "excluded示例": exc[:2],
            "note示例": [n for n in nts if "免疫组化" in n or "IHC" in n][:2],
            "报告示例": rep_txt[:300],
        })

    # 分类（只对工作流 marker==0 的漏抽记录）
    def cls(r):
        if r["原文似无IHC结果"]:
            return "甲 原文无IHC结果（分母假阳）"
        if r["工作流报告条目"] > 0:
            return "乙 记为检查报告而非panels"
        if r["工作流excluded"] > 0:
            return "丙 判为非辅检内容，仅在excluded_text留痕"
        return "丁 产物内无该信息（真漏抽）"

    miss = [r for r in rows if r["工作流panels"] == 0]
    l2miss = [r for r in rows if r["L2markers"] == 0]
    from collections import Counter
    cnt = Counter(cls(r) for r in miss)
    cnt_l2 = Counter(cls(r) for r in l2miss)

    out = {
        "靶集": len(rows),
        "工作流漏抽": {"n": len(miss), "分类": dict(cnt), "明细": miss},
        "L2漏抽": {"n": len(l2miss), "分类": dict(cnt_l2), "明细": l2miss},
        "原文无IHC结果的记录数": sum(1 for r in rows if r["原文似无IHC结果"]),
        "全靶集明细": rows,
    }
    p = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC反常识归因.json"
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("靶集 %d，其中原文明确无 IHC 结果 %d 条" % (len(rows), out["原文无IHC结果的记录数"]))
    print("\n工作流漏抽 %d 条分类：" % len(miss))
    for k, v in cnt.most_common():
        print("   %-32s %d" % (k, v))
    print("\nL2 漏抽 %d 条分类：" % len(l2miss))
    for k, v in cnt_l2.most_common():
        print("   %-32s %d" % (k, v))
    print("\n--- 工作流漏抽逐条 ---")
    for r in miss:
        print("%s [%s] 原文无结果=%s 报告%d excluded%d L2mk%d" % (
            r["record_id"], cls(r)[:2], r["原文似无IHC结果"], r["工作流报告条目"],
            r["工作流excluded"], r["L2markers"]))
        if r["note示例"]:
            print("    note:", r["note示例"][0][:110])
    print("\nwrote", p)

if __name__ == "__main__":
    main()
