# -*- coding: utf-8 -*-
import io
import json
import os
import random
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/研究分析/90_qa_report")
from tarstream import iter_records  # noqa: E402
from probe_ihc_crossfield_full import (  # noqa: E402
    has_ihc, has_value, marker_count, aux_state, NO_RESULT,
)

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/02_结构化结果与过程/中间产物/_agents_input/input_records.jsonl"
STD = ROOT + "/publish/02_结构化结果与过程/中间产物/standardized_v1"
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/ihc_route_reject.json"

# 与 crossfield 探针的 VAL 同构：标志物名 + 括号/冒号 + 取值符号（窄，仅用于产物侧旁证）
VAL = re.compile(
    r"(ER|PR|HER-?2|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3|P63|Calponin|"
    r"P120|CK19|CK7|CK20|EMA|Vimentin|D2-40|CD34|P16|GATA-?3)\s*[*＊#]?\s*[（(：:]\s*([^）)]{0,24})[）)]",
    re.I)
VAL_CH = re.compile(r"[+\-−%％0-9约强弱阴阳膜浆核]")

# 取值判据（宽）：原文字常把取值写成「ER 90%+」而不带括号，窄判据会漏。
# 定义：标志物名后 14 字内出现百分数 / 加号减号 / 阴阳强弱 / 膜浆核定位。
# 分母与分子同用此判据，避免一侧宽一侧窄造成系统性偏差。
MARK = (r"(ER|PR|HER-?2|Ki-?67|C-?erbB-?2|P53|P63|P16|AR|E-?cadherin|CK5/6|CK7|CK19|CK20|"
        r"EGFR|TOP2A|GATA-?3|Calponin|P120|EMA|Vimentin|D2-40|CD34|CK)")
VAL_WIDE = re.compile(
    MARK + r"[^。；;\n]{0,14}?([0-9]{1,3}\s*[%％]|[+\-−]{1,3}|阴性|阳性|弱|强|膜|浆|核)", re.I)
# 拒收说明：指向源字段或"不属本栏"的措辞
REJ = re.compile(r"现病史|跨字段|非本|不属|其他模块|既往史|路由|不应|不能在|污染")
REJ_CTX = re.compile(r"免疫组化|IHC|ER|PR|HER")

# 原文无取值的成因（非互斥，一条可同时命中多类）
WHY_TREAT = re.compile(r"拟行|拟予|建议|方案")
WHY_NOIHC = re.compile(r"(未|无|拒绝|不同意|未行|无法)[^。；\n]{0,12}免疫组化")
WHY_REPORT = re.compile(r"免疫组化[^。；\n]{0,20}(结果|报告)\s*(参见|见|详)")
SAMPLE = 400
SEED = 42

def seg_has_value(t):
    return any(VAL_CH.search(m.group(2) or "") for m in VAL.finditer(t or ""))

def seg_texts(std_doc, field):
    f = (std_doc.get("fields") or {}).get(field) or {}
    return [s.get("text") or "" for s in (f.get("segments") or []) if isinstance(s, dict)]

def has_val_wide(t):
    return bool(VAL_WIDE.search(t or ""))

def val_wide_hits(t):
    return [m.group(0)[:30] for m in VAL_WIDE.finditer(t or "")]

def landing_fields(std_doc):
    out = set()
    for fname, fv in (std_doc.get("fields") or {}).items():
        for s in ((fv or {}).get("segments") or []):
            if isinstance(s, dict) and has_val_wide(s.get("text") or ""):
                out.add(fname)
                break
    return out

def blob_of(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(str(v) for v in obj.values() if isinstance(v, str))
    if isinstance(obj, list):
        return " ".join(blob_of(x) for x in obj)
    return ""

def main():
    src = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            src[r["record_id"]] = r.get("fields") or {}

    only_pi, dual = set(), set()
    nv_only = nv_dual = 0
    for rid, f in src.items():
        aux = f.get("辅助检查") or ""
        others = [f.get(k) or "" for k in ("现病史", "既往史", "体格检查", "主诉")]
        aux_has = has_ihc(aux)
        other_hit = any(has_ihc(v) for v in others)
        if not aux_has and not other_hit:
            continue
        full = " ".join([aux] + others)
        if NO_RESULT.search(full) and not has_value(full):
            continue
        txt = (f.get("现病史") or "") + " " + aux
        if aux_has:
            dual.add(rid)
            if not has_val_wide(txt):
                nv_dual += 1
        elif has_ihc(f.get("现病史") or ""):
            only_pi.add(rid)
            if not has_val_wide(txt):
                nv_only += 1

    silent, filled, filled_dual = [], [], 0
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in only_pi:
            st = aux_state(rec)
            if st == "panels非空" or marker_count(rec) > 0:
                filled.append((rid, st))
            elif st == "静默为空":
                silent.append(rid)
        elif rid in dual:
            if aux_state(rec) == "panels非空" or marker_count(rec) > 0:
                filled_dual += 1

    random.seed(SEED)
    s_sil = random.sample(sorted(silent), min(SAMPLE, len(silent)))
    s_fill = random.sample(sorted([r for r, _ in filled]), min(SAMPLE, len(filled)))

    # 全量普查静默组：取值去向 + 产物形态 + 拒收说明
    miss = {"原文含取值": 0, "原文无取值": 0,
            "　成因_治疗叙述": 0, "　成因_明确未做": 0, "　成因_只给报告号": 0,
            "落点_辅助检查": 0, "　其中仅辅助检查": 0, "　其中兼现病史": 0,
            "落点_仅现病史": 0, "落点_仅其他字段": 0, "落点_全文档无": 0}
    prod = {"ihc键在但panels空": 0, "ihc键缺失": 0, "panels非空": 0}
    rej = {"留拒收说明且指向源字段": 0, "有说明未指向跨字段": 0, "无任何说明": 0}
    ctrl_pos, ctrl_neg = [], []

    rids_prod = {}
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in set(silent):
            rids_prod[rid] = rec

    for rid in silent:
        p = os.path.join(STD, rid + ".json")
        src_txt = (src.get(rid, {}).get("现病史") or "") + " " + (src.get(rid, {}).get("辅助检查") or "")
        src_has = has_val_wide(src_txt)
        lands = set()
        if os.path.exists(p):
            d = json.load(io.open(p, encoding="utf-8"))
            lands = landing_fields(d)
        if not src_has:
            miss["原文无取值"] += 1
            if WHY_TREAT.search(src_txt):
                miss["　成因_治疗叙述"] += 1
            if WHY_NOIHC.search(src_txt):
                miss["　成因_明确未做"] += 1
            if WHY_REPORT.search(src_txt):
                miss["　成因_只给报告号"] += 1
            if len(ctrl_neg) < 5:
                ctrl_neg.append({"rid": rid, "原文片段": src_txt.replace("\n", " ")[:120]})
        else:
            miss["原文含取值"] += 1
            if len(ctrl_pos) < 5:
                ctrl_pos.append({"rid": rid, "命中": val_wide_hits(src_txt)[:4],
                                 "落点": sorted(lands)})
            if "辅助检查" in lands:
                miss["落点_辅助检查"] += 1
                if lands == {"辅助检查"}:
                    miss["　其中仅辅助检查"] += 1
                else:
                    miss["　其中兼现病史"] += 1
            elif "现病史" in lands:
                miss["落点_仅现病史"] += 1
            elif lands:
                miss["落点_仅其他字段"] += 1
            else:
                miss["落点_全文档无"] += 1

        rec = rids_prod.get(rid) or {}
        ae = rec.get("auxiliary_exam") if isinstance(rec.get("auxiliary_exam"), dict) else {}
        ih = ae.get("ihc") if isinstance(ae.get("ihc"), dict) else None
        if ih is None:
            prod["ihc键缺失"] += 1
        elif [x for x in (ih.get("panels") or []) if isinstance(x, dict)]:
            prod["panels非空"] += 1
        else:
            prod["ihc键在但panels空"] += 1
        blob = blob_of((ih or {}).get("qc")) + " " + blob_of(ae.get("excluded_text")) + \
            " " + blob_of(rec.get("qc"))
        if REJ.search(blob) and REJ_CTX.search(blob):
            rej["留拒收说明且指向源字段"] += 1
        elif blob.strip():
            rej["有说明未指向跨字段"] += 1
        else:
            rej["无任何说明"] += 1

    def input_profile(rids, tag):
        hit = 0
        lens = []
        nseg = []
        n_ok = 0
        for rid in rids:
            p = os.path.join(STD, rid + ".json")
            if not os.path.exists(p):
                continue
            n_ok += 1
            d = json.load(io.open(p, encoding="utf-8"))
            segs = seg_texts(d, "辅助检查")
            nseg.append(len(segs))
            ih = [t for t in segs if has_ihc(t)]
            if ih:
                hit += 1
                lens.extend(len(t) for t in ih)
        lens.sort()
        nseg.sort()

        def med(x):
            return x[len(x) // 2] if x else 0
        return {
            "样本": n_ok,
            "辅助检查含IHC段": hit,
            "含IHC段占比": round(hit / n_ok * 100, 1) if n_ok else 0,
            "IHC段长度中位": med(lens),
            "辅助检查段数中位": med(nseg),
        }

    a = miss["原文含取值"]
    miss["归位成功率"] = round(miss["落点_辅助检查"] / a * 100, 1) if a else 0

    # 分层召回校正：分母扣除"原文含免疫组化表述但无取值可抽"者（两组同法）
    den_only = len(only_pi) - nv_only
    den_dual = len(dual) - nv_dual
    r_raw_only = 100.0 * len(filled) / len(only_pi) if only_pi else 0
    r_raw_dual = 100.0 * filled_dual / len(dual) if dual else 0
    r_cor_only = 100.0 * len(filled) / den_only if den_only else 0
    r_cor_dual = 100.0 * filled_dual / den_dual if den_dual else 0
    layered = {
        "仅现病史_无取值": nv_only,
        "仅现病史_无取值占比": round(100.0 * nv_only / len(only_pi), 1) if only_pi else 0,
        "双写_无取值": nv_dual,
        "双写_无取值占比": round(100.0 * nv_dual / len(dual), 1) if dual else 0,
        "仅现病史_原始召回": round(r_raw_only, 1),
        "双写_原始召回": round(r_raw_dual, 1),
        "仅现病史_校正召回": round(r_cor_only, 1),
        "双写_校正召回": round(r_cor_dual, 1),
        "原始差_pp": round(r_raw_dual - r_raw_only, 1),
        "校正差_pp": round(r_cor_dual - r_cor_only, 1),
    }

    out = {
        "口径": ("靶集与产物状态分类复用 probe_ihc_crossfield_full；仅现病史层＝原文仅现病史含 IHC 表达"
                 "且排除'未做/建议做'。取值去向、产物形态、拒收说明为全量普查，不抽样。"
                 "输入侧对照为两组各随机抽样 400 条（种子 42），非全量，只作组间形态对照。"
                 "取值去向的分母限定为原文确含取值的记录"
                 "（宽判据 VAL_WIDE：标志物名后 14 字内出现百分数/正负号/阴阳强弱/膜浆核），"
                 "落点扫描标准化文档全部字段，不只辅助检查与现病史。分层校正召回的分母，"
                 "按同一宽判据扣除该层原文无取值者，两组同法。"),
        "分层规模": {"仅现病史": len(only_pi), "双写": len(dual)},
        "产物状态": {"仅现病史_抽到": len(filled), "双写_抽到": filled_dual,
                     "仅现病史_静默为空": len(silent)},
        "取值去向": miss,
        "分层校正召回": layered,
        "判据正控": {"原文含取值样本": ctrl_pos, "原文无取值样本": ctrl_neg},
        "产物形态": prod,
        "拒收说明": rej,
        "输入侧对照": {
            "抽到组": input_profile(s_fill, "抽到"),
            "静默组": input_profile(s_sil, "静默"),
        },
        "样例": s_sil[:10],
        "随机种子": SEED,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("wrote", OUT)

if __name__ == "__main__":
    main()
