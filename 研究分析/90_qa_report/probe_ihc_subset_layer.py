# -*- coding: utf-8 -*-
import io
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/研究分析/90_qa_report")
from tarstream import iter_records  # noqa: E402
from probe_ihc_crossfield_full import has_ihc, marker_count, aux_state  # noqa: E402
from probe_ihc_route_reject import (  # noqa: E402
    REJ, REJ_CTX, blob_of, has_val_wide, landing_fields,
)

IHC_KW = re.compile(r"免疫组化|IHC|ER|PR|HER", re.I)

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT100 = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
STD = ROOT + "/publish/02_结构化结果与过程/中间产物/standardized_v1"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/ihc_subset_layer.json"

# 段级判据（比 has_ihc 宽）：MK 只认半角括号，全角"ER（中-强+，约90%）"会漏，
# 故段级另按"标志物名 + 全/半角括号"计数。靶集仍用 has_ihc，口径不变。
SEG_IHC = re.compile(r"(ER|PR|HER-?2|Ki-?67|P53|AR|CK5/6|EGFR|GATA-?3|P63|Calponin|"
                     r"P120|CK19|E-?ca(dherin)?|EMA|Vimentin)\s*[*＊#]?\s*[（(：:]", re.I)

def seg_in_aux(x):
    d = json.load(io.open(os.path.join(STD, x + ".json"), encoding="utf-8"))
    for s in (((d.get("fields") or {}).get("辅助检查") or {}).get("segments") or []):
        t = str(s.get("text") or "")
        if s.get("kind") == "ihc" or has_ihc(t) or len(SEG_IHC.findall(t)) >= 2:
            return True
    return False

def l2_hit(x):
    p = os.path.join(L2, x + ".json")
    if not os.path.exists(p):
        return None
    return marker_count(json.load(io.open(p, encoding="utf-8"))) > 0

def structured_blob(rec):
    out = []
    for k, v in (rec or {}).items():
        if not isinstance(v, dict):
            if k not in ("raw",):
                out.append(str(v))
            continue
        for kk, vv in v.items():
            if kk == "raw":
                continue
            out.append(json.dumps(vv, ensure_ascii=False))
    return " ".join(out)

def main():
    ids, fields = set(), {}
    with io.open(INPUT100, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            ids.add(r["record_id"])
            fields[r["record_id"]] = r.get("fields") or {}

    only = [x for x in ids
            if not has_ihc(fields[x].get("辅助检查") or "")
            and has_ihc(fields[x].get("现病史") or "")]
    # 与 crossfield 探针同一定义：辅助检查字段含 IHC 表达即入此层（含两处都写与只在辅助检查写）
    aux_has = [x for x in ids if has_ihc(fields[x].get("辅助检查") or "")]
    both = [x for x in aux_has if has_ihc(fields[x].get("现病史") or "")]
    aux_only = [x for x in aux_has if not has_ihc(fields[x].get("现病史") or "")]
    dual = aux_has

    rec = {}
    for name, r in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in ids:
            rec[rid] = r

    def hit(rid):
        r = rec.get(rid) or {}
        return aux_state(r) == "panels非空" or marker_count(r) > 0

    silent = [x for x in ids
              if (not has_ihc(fields[x].get("辅助检查") or "")
                  and has_ihc(fields[x].get("现病史") or "")
                  or has_ihc(fields[x].get("辅助检查") or ""))
              and not hit(x)]

    miss = {"原文含取值": 0, "原文无取值": 0,
            "落点_辅助检查": 0, "落点_仅现病史": 0, "落点_全文档无": 0}
    form = {"ihc键在面板空": 0, "ihc键缺失": 0}
    rej = 0
    why = {"甲_原文无结果": 0, "乙_记为报告条目": 0, "丙_有声明无落点": 0,
           "戊_错位落库": 0, "丁_真漏抽": 0}
    detail = []
    for rid in silent:
        f = fields[rid]
        txt = (f.get("现病史") or "") + " " + (f.get("辅助检查") or "")
        p = os.path.join(STD, rid + ".json")
        lands = landing_fields(json.load(io.open(p, encoding="utf-8"))) if os.path.exists(p) else set()
        if not has_val_wide(txt):
            miss["原文无取值"] += 1
        else:
            miss["原文含取值"] += 1
            if "辅助检查" in lands:
                miss["落点_辅助检查"] += 1
            elif "现病史" in lands:
                miss["落点_仅现病史"] += 1
            else:
                miss["落点_全文档无"] += 1

        r = rec.get(rid) or {}
        ae = r.get("auxiliary_exam") if isinstance(r.get("auxiliary_exam"), dict) else {}
        ih = ae.get("ihc") if isinstance(ae.get("ihc"), dict) else None
        if ih is None:
            form["ihc键缺失"] += 1
        else:
            form["ihc键在面板空"] += 1
        blob = blob_of((ih or {}).get("qc")) + " " + blob_of(ae.get("excluded_text")) + \
            " " + blob_of(r.get("qc"))
        has_note = bool(REJ.search(blob) and REJ_CTX.search(blob))
        if has_note:
            rej += 1

        # 归因（互斥，按甲→乙→丙→戊→丁 优先级）
        rep = [x for x in (ae.get("reports") or [])
               if isinstance(x, dict) and IHC_KW.search(str(x.get("exam_name") or ""))]
        # 产物痕迹：除 raw 外，他处是否已出现标志物取值（错位落库，非漏抽）
        trace = has_val_wide(structured_blob(r))
        if not has_val_wide(txt):
            k = "甲_原文无结果"
        elif rep:
            k = "乙_记为报告条目"
        elif has_note:
            k = "丙_有声明无落点"
        elif trace:
            k = "戊_错位落库"
        else:
            k = "丁_真漏抽"
        why[k] += 1
        detail.append({"rid": rid, "归因": k, "落点": sorted(lands), "产物痕迹": trace,
                       "层": "仅现病史" if rid in set(only) else "辅助检查亦含",
                       "段已入辅助检查": seg_in_aux(rid), "去分板块命中": l2_hit(rid),
                       "原文": txt.replace("\n", " ").strip()[:200]})

    # 两组对照：同一批记录分别看基线与去分板块的产出
    cmp_layer = {
        "仅现病史": {"条数": len(only), "基线命中": sum(1 for x in only if hit(x)),
                     "去分板块命中": sum(1 for x in only if l2_hit(x))},
        "辅助检查亦含": {"条数": len(aux_has), "基线命中": sum(1 for x in aux_has if hit(x)),
                         "去分板块命中": sum(1 for x in aux_has if l2_hit(x))},
        "靶集合计": {"条数": len(only) + len(aux_has),
                     "基线命中": sum(1 for x in only + aux_has if hit(x)),
                     "去分板块命中": sum(1 for x in only + aux_has if l2_hit(x))},
    }
    # 静默记录中"段已入辅助检查"者，去分板块救回多少（按层拆，与 ¶203 的 65 条口径对齐）
    def _rescue(sub):
        mv = [d for d in sub if d["段已入辅助检查"]]
        return {
            "静默": len(sub),
            "　段已入辅助检查": len(mv),
            "　其中含取值": sum(1 for d in mv if has_val_wide(d["原文"])),
            "　其中去分板块抽到": sum(1 for d in mv if d["去分板块命中"]),
            "　段未移入": len(sub) - len(mv),
        }
    rescue = {"仅现病史": _rescue([d for d in detail if d["层"] == "仅现病史"]),
              "全靶集": _rescue(detail)}
    # 同文本对照：同一句免疫组化表述在不同记录上的结局
    texts = [("ER（80%+，中等）", re.compile(r"ER（80%\+，中等）")),
             ("luminal B 型 + ki-67", re.compile(r"luminal\s*[aAbB]\s*型", re.I))]
    same_text = []
    for name, pat in texts:
        g = [x for x in ids
             if pat.search((fields[x].get("现病史") or "") + " " + (fields[x].get("辅助检查") or ""))]
        same_text.append({"表述": name, "记录数": len(g),
                          "基线命中": sum(1 for x in g if hit(x)),
                          "去分板块命中": sum(1 for x in g if l2_hit(x)),
                          "记录": sorted(g)})

    # 硬闸门判定：静默记录是否被 validate.py 判负。
    # 用途是把「完备性不进闸门视野」落成可复现的数，而不是只在正文里断言。
    sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
    import validate as _V  # noqa: E402
    gate = {"通过": 0, "判负": 0}
    for rid in silent:
        if _V.rep_ok(_V.validate_record(rec.get(rid) or {}, rid)):
            gate["通过"] += 1
        else:
            gate["判负"] += 1

    out = {
        "口径": ("100 条抽样子集内，与表 6 同一靶集（原文含免疫组化表达的 82 条）。"
                 "字段层按原文划分：仅现病史＝辅助检查字段无 IHC 表达而现病史有；"
                 "双写＝两字段都有。静默＝产物 panels 空且 marker 数为 0。"
                 "取值判据与 probe_ihc_route_reject 同一宽判据，落点扫标准化文档全部字段。"),
        "子集记录": len(ids),
        "靶集": len(only) + len(dual),
        "分层": {
            "仅现病史": len(only), "仅现病史_抽到": sum(1 for x in only if hit(x)),
            "辅助检查亦含": len(aux_has), "辅助检查亦含_抽到": sum(1 for x in aux_has if hit(x)),
            "　其中两处都写": len(both),
            "　其中仅辅助检查": len(aux_only),
        },
        "静默": {
            "总数": len(silent),
            "仅现病史": sum(1 for x in silent if x in set(only)),
            "辅助检查亦含": sum(1 for x in silent if x in set(aux_has)),
        },
        "静默取值去向": miss,
        "静默产物形态": form,
        "静默留拒收说明": rej,
        "静默归因": why,
        "两组对照": cmp_layer,
        "静默段落位与救回": rescue,
        "静默硬闸门判定": gate,
        "同文本对照": same_text,
        "静默明细": detail,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("wrote", OUT)

if __name__ == "__main__":
    main()
