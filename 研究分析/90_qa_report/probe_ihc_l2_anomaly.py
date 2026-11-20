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
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"

IHC_KW = re.compile(r"免疫组化|免疫组织化学")
MK = re.compile(r"(ER|PR|HER-?2?|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3)"
                r"\s*(\(|:|\s*\d|\s*[+-])", re.I)
FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
# 规范名（29 个诊断鉴别型 + 4 个治疗决策型）的简表，用于判"非规范名"
CANON = {"ER", "PR", "HER2", "KI67", "AR", "P53", "EGFR", "CK5/6", "KI-67", "HER-2",
         "E-CADHERIN", "TOP2A", "GATA3", "CK14", "CK5", "CK8/18", "P63", "SMMHC",
         "CD31", "D2-40", "CD34", "VIMENTIN", "CD68", "S100", "SYN", "CGA", "CD56",
         "TTF-1", "NAPSIN", "GCDFP-15", "MAMAGLOBIN", "SOX10", "P120", "B-CATENIN",
         "CYCLIND1", "BCL2", "P53", "MLH1", "MSH2", "MSH6", "PMS2", "PD-L1", "CD3",
         "CD20", "DESMIN", "MYOGENIN", "MYOD1", "CALPONIN", "SMA", "STAT6"}

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
            elif isinstance(m, str):
                mk.append({"name": m, "_str": True})
    return mk

def mname(m):
    for k in ("name", "marker", "marker_name", "symbol"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def mval(m):
    for k in ("value", "result", "raw", "raw_value", "interpretation"):
        v = m.get(k)
        if v not in (None, "", [], {}):
            return v
    return None

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

    wf = load_wf(rids)
    l2 = load_dir(L2)
    l1 = load_dir(L1)

    ihc_rid, field_hit = [], {}
    for rid in rids:
        r = inputs[rid]
        fields = r.get("fields") or {}
        hits = [k for k in FIELDS if has_ihc(fields.get(k) or "")]
        if hits:
            ihc_rid.append(rid)
            field_hit[rid] = hits

    # 每条记录：两侧 marker 数、命中字段
    detail = []
    for rid in ihc_rid:
        w = wf.get(rid, {})
        g = l2.get(rid, {})
        wm, gm = marker_list(w), marker_list(g)
        detail.append({
            "record_id": rid,
            "IHC所在字段": field_hit[rid],
            "辅检字段是否载IHC": "辅助检查" in field_hit[rid],
            "工作流marker": len(wm),
            "L2marker": len(gm),
            "L1marker": len(marker_list(l1.get(rid, {}))),
        })

    # 分组：工作流漏 / L2 漏 / 两者都有
    wf_miss = [d for d in detail if d["工作流marker"] == 0]
    l2_miss = [d for d in detail if d["L2marker"] == 0]
    both = [d for d in detail if d["工作流marker"] > 0 and d["L2marker"] > 0]

    def rate(rows, key):
        return round(100.0 * sum(1 for d in rows if d[key] > 0) / len(rows), 1) if rows else 0.0

    # 假设 B：工作流的漏抽是否只发生在「IHC 不在辅助检查字段」的记录
    wf_miss_where = {}
    for d in wf_miss:
        k = "IHC仅在非辅助检查字段" if not d["辅检字段是否载IHC"] else "辅助检查字段本身载IHC"
        wf_miss_where[k] = wf_miss_where.get(k, 0) + 1
    # 分层：仅非辅检字段 vs 辅检含 IHC
    strata = {}
    for tag, cond in (("IHC仅在非辅助检查字段", lambda d: not d["辅检字段是否载IHC"]),
                      ("辅助检查字段含IHC", lambda d: d["辅检字段是否载IHC"])):
        rows = [d for d in detail if cond(d)]
        strata[tag] = {
            "记录数": len(rows),
            "工作流有marker": sum(1 for d in rows if d["工作流marker"] > 0),
            "L2有marker": sum(1 for d in rows if d["L2marker"] > 0),
            "工作流检出率%": rate(rows, "工作流marker"),
            "L2检出率%": rate(rows, "L2marker"),
        }

    # 假设 C：L2 的 marker 质量（规范化率、空值率、重复率）
    def quality(prod, tag):
        n_mk = n_named = n_valued = n_canon = 0
        dup_recs = 0
        names_all = []
        for rid in ihc_rid:
            mk = marker_list(prod.get(rid, {}))
            n_mk += len(mk)
            ns = [mname(m) for m in mk]
            ns_nonempty = [x for x in ns if x]
            n_named += sum(1 for m in mk if mname(m))
            n_valued += sum(1 for m in mk if mval(m) is not None)
            n_canon += sum(1 for x in ns_nonempty if x.upper().replace(" ", "") in CANON)
            if len(ns_nonempty) != len(set(x.upper() for x in ns_nonempty)):
                dup_recs += 1
            names_all.extend(ns_nonempty)
        return {
            "产物": tag, "marker总": n_mk,
            "有名称%": round(100.0 * n_named / n_mk, 1) if n_mk else 0.0,
            "有价值%": round(100.0 * n_valued / n_mk, 1) if n_mk else 0.0,
            "规范名%": round(100.0 * n_canon / n_mk, 1) if n_mk else 0.0,
            "记录内重名记录数": dup_recs,
            "唯一名称数": len(set(x.upper() for x in names_all)),
        }

    q_wf, q_l2 = quality(wf, "完整工作流"), quality(l2, "L2无分板块")

    # 工作流漏、L2 有的记录：逐条给出 L2 的 marker 名，用于人工判是否真抽对
    recovered = []
    for d in wf_miss:
        rid = d["record_id"]
        mk = marker_list(l2.get(rid, {}))
        src = inputs[rid]["fields"]
        src_txt = " ".join((src.get(k) or "") for k in FIELDS)
        m = re.search(r"免疫组化|免疫组织化学", src_txt)
        snippet = src_txt[max(0, m.start() - 40): m.start() + 160] if m else src_txt[:200]
        recovered.append({
            "record_id": rid,
            "IHC所在字段": d["IHC所在字段"],
            "L2marker数": len(mk),
            "L2marker名": [mname(x) for x in mk][:12],
            "L2marker值示例": [mval(x) for x in mk][:6],
            "原文片段": snippet,
        })

    # L2 漏、工作流有的记录
    l2_only_miss = [{
        "record_id": d["record_id"], "IHC所在字段": d["IHC所在字段"],
        "工作流marker数": d["工作流marker"],
        "工作流marker名": [mname(x) for x in marker_list(wf.get(d["record_id"], {}))][:12],
    } for d in l2_miss if d["工作流marker"] > 0]

    out = {
        "靶集": {"原文IHC记录": len(ihc_rid), "判据": "免疫组化字样，或≥2个标志物名+括号/值"},
        "汇总": {
            "工作流漏抽记录": len(wf_miss), "L2漏抽记录": len(l2_miss),
            "工作流检出率%": rate(detail, "工作流marker"),
            "L2检出率%": rate(detail, "L2marker"),
            "L1检出率%": rate(detail, "L1marker"),
            "工作流漏抽位置分布": wf_miss_where,
        },
        "分层_按IHC是否写在辅助检查字段": strata,
        "marker质量对照": [q_wf, q_l2],
        "工作流漏而L2有的记录": recovered,
        "L2漏而工作流有的记录": l2_only_miss,
        "逐条": detail,
    }
    p = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC反常识核查.json"
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("靶集", len(ihc_rid))
    print("工作流检出 %s%%   L2 %s%%   L1 %s%%" % (
        rate(detail, "工作流marker"), rate(detail, "L2marker"), rate(detail, "L1marker")))
    print("工作流漏 %d 条，位置分布 %s" % (len(wf_miss), wf_miss_where))
    print("\n分层（按 IHC 是否写在辅助检查字段）：")
    for k, v in strata.items():
        print("  %-24s n=%-3d 工作流 %5.1f%%  L2 %5.1f%%" % (k, v["记录数"], v["工作流检出率%"], v["L2检出率%"]))
    print("\nmarker 质量：")
    for q in (q_wf, q_l2):
        print("  %-10s 总%-5d 有名%-6s 有值%-6s 规范名%-6s 重名记录%d 唯一名%d" % (
            q["产物"], q["marker总"], q["有名称%"], q["有价值%"], q["规范名%"],
            q["记录内重名记录数"], q["唯一名称数"]))
    print("\nwrote", p)

if __name__ == "__main__":
    main()
