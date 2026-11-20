#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import re
import json
import time
import hashlib
import collections

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本/00_core"))
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本/05_agents"))

from tarstream import iter_records                      # noqa: E402
from validate import validate_record, rep_ok            # noqa: E402
# BI-RADS「有无分级值」与「分级归一」的判据只有一处定义，在 研究分析/probe_granularity。
from probe_granularity import has_grade, norm_birads     # noqa: E402

TAR = os.path.join(DATA, "中间产物/_packed/assembled_v7_final.tar")
LEDGER = os.path.join(DATA, "中间产物/_qa_out/clean_enum_ledger.jsonl")
OUT = os.path.join(DATA, "qa_reports/终态数字.json")
GRAN = os.path.join(DATA, "qa_reports/granularity.json")            # 90_qa_report 产出：BI-RADS 记录级四格
GROUND = os.path.join(DATA, "qa_reports/birads_grounding.json")     # 90_qa_report 产出：值级回指

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_YM = re.compile(r"^\d{4}-\d{2}$")          # 月精度：有效信息，非脏值
RE_Y = re.compile(r"^\d{4}$")                 # 仅年
RE_NOPAD = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")  # 未补零

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return ""
    return str(v).strip()

def shape_of(lab):
    ks = set(lab.keys())
    if "section_seq" in ks:
        return "report_group+section_seq"
    if "panel" in ks:
        return "panel"
    if "panel_name" in ks:
        return "panel_name"
    if "name" in ks and "value" in ks:
        return "扁平name+value"
    if "results" in ks:
        return "仅results"
    return "无名称键"

def md5_of(path, limit=None):
    h = hashlib.md5()
    n = 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
            n += len(b)
            if limit and n >= limit:
                break
    return h.hexdigest()

def _peek(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _birads_block(S, dist, rec_unit, rec_val):
    unit, val = S["bi_unit"], S["bi_val"]
    blk = {
        "_口径": "单元 = auxiliary_exam.reports[] 中 grading_system 以 BI-RADS 开头者；"
                 "给值 = report.birads／report.grading／impressions[].grade_raw 任一非空"
                 "（判据 import 自 研究分析/probe_granularity.has_grade，不建副本）",
        "单元": unit,
        "给值": val,
        "给值占比": round(100.0 * val / unit, 2) if unit else None,
        "有单元记录数": len(rec_unit),
        "给值记录数": len(rec_val),
        "分级分布": dict(sorted(dist.items())),
    }
    g = _peek(GRAN)
    if g and isinstance(g.get("分级"), dict):
        lt = g["分级"].get("记录级落地核验") or {}
        raw = (lt.get("原文有分级_已给值") or 0) + (lt.get("原文有分级_记录无值") or 0)
        blk["记录级"] = {
            "_来源": "研究分析/probe_granularity.py → granularity.json（按源表原文判有无分级）",
            "原文写明分级的记录": raw,
            "原文有分级_已给值": lt.get("原文有分级_已给值"),
            "原文有分级_记录无值": lt.get("原文有分级_记录无值"),
            "原文无分级_却给值": lt.get("原文无分级_却给值"),
            "两边都无": lt.get("两边都无"),
            "记录级召回": round(100.0 * (lt.get("原文有分级_已给值") or 0) / raw, 2) if raw else None,
        }
    b = _peek(GROUND)
    if b and isinstance(b.get("值级回指"), dict):
        vi = b["值级回指"]
        blk["值级回指"] = {
            "_来源": "研究分析/90_qa_report/verify_birads_grounding.py → birads_grounding.json（产物值能否在源表找到字面）",
            "有归一值的单元": vi.get("单元有归一值"),
            "可回指": vi.get("可回指"),
            "不可回指": vi.get("不可回指"),
            "可回指率": round(vi.get("可回指率") or 0, 2),
        }
    return blk

def main():
    S = collections.Counter()
    pat = collections.defaultdict(lambda: {"sex": "", "reg": ""})
    rec_type = collections.Counter()
    dept = collections.Counter()
    tx_mod = collections.Counter()

    pat_dates = collections.defaultdict(set)       # 登记号 -> 就诊日期
    pat_exam_dates = collections.defaultdict(set)  # 登记号 -> 检查日期（不同日期）
    pat_exam_list = collections.defaultdict(list)  # 登记号 -> 检查报告份数（含同日多份）

    lab_shape = collections.Counter()
    lab_item_names = collections.Counter()
    lab_uniq = set()
    tnm_prefix = collections.Counter()
    ln_nature = collections.Counter()

    bi_dist = collections.Counter()
    rec_bi_unit = set()
    rec_bi_val = set()

    gate = collections.Counter()
    gate_tnm = collections.Counter()
    gate_vocab = collections.Counter()
    gate_vocab_rec = collections.Counter()
    gate_aux = collections.Counter()

    for fn, rec in iter_records(TAR):
        rid = os.path.basename(fn).replace(".json", "")

        # ---------- 闸门（与其他计数同遍同源） ----------
        rep = validate_record(rec, rid)
        gate["records"] += 1
        if rep["aux_pending"]:
            gate["aux_pending_rec"] += 1
            gate["aux_pending_item"] += len(rep["aux_pending"])
        if rep_ok(rep):
            gate["pass"] += 1
        else:
            gate["fail"] += 1
            if rep["blocks_missing"]:
                gate["blocks_missing"] += 1
            if rep["vocab"]:
                gate["vocab"] += 1
                seen = set()
                for it in rep["vocab"]:
                    k = _s(it.get("key"))
                    gate_vocab[k] += 1          # 处数
                    seen.add(k)
                for k in seen:
                    gate_vocab_rec[k] += 1      # 记录数（同字段一处或多处都算 1）
            if rep["tnm"]:
                gate["tnm"] += 1
                for it in rep["tnm"]:
                    gate_tnm[(it.get("at", "").rsplit(".", 1)[-1], it.get("error"))] += 1
            if rep["aux"]:
                gate["aux"] += 1
                for it in rep["aux"]:
                    k = it.get("at", "").split("]")[-1].lstrip(".") or it.get("at", "").rsplit(".", 1)[-1]
                    gate_aux[(k, it.get("error"))] += 1

        # ---------- 规模 ----------
        p = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
        v = rec.get("visit") if isinstance(rec.get("visit"), dict) else {}
        reg = _s(p.get("登记号"))
        S["records"] += 1
        if reg:
            if not pat[reg]["sex"]:
                pat[reg]["sex"] = _s(p.get("性别_text")) or _s(p.get("性别"))
        rec_type[_s(p.get("病历类型")) or "<空>"] += 1
        dept[_s(v.get("医生所属科室")) or "<空>"] += 1
        d = _s(v.get("就诊日期"))[:10]
        if reg and RE_DATE.match(d):
            pat_dates[reg].add(d)

        # ---------- 四个叙述板块 ----------
        for key in ("chief_complaint", "present_illness", "past_history", "physical_exam"):
            b = rec.get(key)
            if isinstance(b, dict) and any(b.get(k) for k in ("raw", "text", "structured")):
                S["has_" + key] += 1
            elif isinstance(b, str) and b.strip():
                S["has_" + key] += 1

        # ---------- 体格检查（触诊淋巴结） ----------
        pe = rec.get("physical_exam") if isinstance(rec.get("physical_exam"), dict) else {}
        st = pe.get("structured") if isinstance(pe.get("structured"), dict) else {}
        ln = st.get("lymph_nodes")
        ln_items = ln if isinstance(ln, list) else ([ln] if isinstance(ln, dict) else [])
        ln_nonempty = [x for x in ln_items if isinstance(x, dict) and any(x.values())]
        if ln_nonempty:
            S["ln_has"] += 1
        for it in ln_nonempty:
            S["ln_items"] += 1
            ln_nature[_s(it.get("nature")) or "<空>"] += 1

        # ---------- 辅助检查 ----------
        ae = rec.get("auxiliary_exam") if isinstance(rec.get("auxiliary_exam"), dict) else {}
        if ae:
            S["aux_key"] += 1
            if any(ae.values()):
                S["aux_any"] += 1
            ihc = ae.get("ihc") if isinstance(ae.get("ihc"), dict) else {}
            panels = ihc.get("panels") or []
            reports = ae.get("reports") or []
            labs = ae.get("labs") or []
            if reports or labs or panels:
                S["aux_entity"] += 1
            if panels:
                S["rec_with_ihc"] += 1

            for r in reports:
                if not isinstance(r, dict):
                    continue
                S["reports"] += 1
                # ---------- BI-RADS 单元（体系字段前缀判据，与 90_qa_report 同源） ----------
                if _s(r.get("grading_system")).upper().replace("-", "").startswith("BIRADS"):
                    S["bi_unit"] += 1
                    rec_bi_unit.add(rid)
                    if has_grade(r):
                        S["bi_val"] += 1
                        rec_bi_val.add(rid)
                    g = norm_birads(r.get("grading_system"),
                                    r.get("birads") or r.get("grading"),
                                    r.get("impressions"))
                    if g:
                        bi_dist[g] += 1
                ed = _s(r.get("exam_date"))[:10]
                if r.get("exam_date_raw"):
                    S["date_blanked"] += 1       # 原文是「外院/待补充」等，已置空并留原值
                if not ed:
                    S["exam_date_absent"] += 1
                    if not r.get("exam_date_absent"):
                        S["date_empty_nomark"] += 1
                elif RE_DATE.match(ed):
                    S["reports_dated"] += 1
                    if reg:
                        pat_exam_dates[reg].add(ed)
                        pat_exam_list[reg].append(ed)
                elif RE_YM.match(ed):
                    S["date_ym"] += 1
                elif RE_NOPAD.match(ed):
                    S["date_nopad"] += 1
                elif RE_Y.match(ed):
                    S["date_y"] += 1
                else:
                    S["date_other"] += 1

            if labs:
                S["rec_with_labs"] += 1
            sigs = collections.Counter()
            for l in labs:
                if not isinstance(l, dict):
                    continue
                S["labs_entries"] += 1
                lab_shape[shape_of(l)] += 1
                rs = [x for x in (l.get("results") or []) if isinstance(x, dict)]
                if rs:
                    S["labs_with_results"] += 1
                # 组合名 = panel_name / panel；扁平形态的 name 是**指标名**，不是组合名，
                # 不能拿它充数（否则「无组合名」会被低估十倍）。
                if "panel_name" in l:
                    S["labs_has_pn_key"] += 1
                if "panel" in l:
                    S["labs_has_p_key"] += 1
                if not (_s(l.get("panel_name")) or _s(l.get("panel"))):
                    S["labs_no_panel"] += 1
                    if not rs and not _s(l.get("name")):
                        S["labs_no_panel_no_detail"] += 1
                if l.get("panel_name_from"):
                    S["labs_panel_name_from"] += 1
                ld = _s(l.get("exam_date"))[:10]
                if l.get("exam_date_raw") or l.get("date_raw"):
                    S["lab_date_blanked"] += 1
                if not ld:
                    S["lab_date_absent"] += 1
                    if not (l.get("exam_date_absent") or l.get("date_absent")):
                        S["lab_date_empty_nomark"] += 1
                elif RE_DATE.match(ld):
                    S["lab_date_full"] += 1
                elif RE_YM.match(ld):
                    S["lab_date_ym"] += 1
                elif RE_NOPAD.match(ld):
                    S["lab_date_nopad"] += 1
                else:
                    S["lab_date_other"] += 1

                flat = rs if rs else [l]
                sig = []
                for r in flat:
                    nm = _s(r.get("name"))
                    val = _s(r.get("value"))
                    if nm:
                        lab_item_names[nm[:18]] += 1
                    if _s(r.get("code")):
                        S["lab_coded_all"] += 1
                    if not val:
                        continue
                    S["lab_values"] += 1
                    if _s(r.get("unit")):
                        S["lab_values_unit"] += 1
                    if _s(r.get("code")):
                        S["lab_values_coded"] += 1
                    lab_uniq.add((rid, _s(l.get("exam_date"))[:10], nm, val, _s(r.get("unit"))))
                    sig.append((nm, val))
                if sig:
                    sigs[tuple(sig)] += 1
            excess = sum(c - 1 for c in sigs.values() if c > 1)
            if excess:
                S["lab_dup_rec"] += 1
                S["lab_dup_excess"] += excess

            for pn in panels:
                if not isinstance(pn, dict):
                    continue
                S["ihc_panels"] += 1
                S["ihc_markers"] += len([m for m in (pn.get("markers") or []) if isinstance(m, dict)])

        # ---------- 诊断 ----------
        dg = rec.get("diagnoses")
        if isinstance(dg, dict):
            S["diag_key"] += 1
            items = dg.get("items") or []
            if any(dg.values()):
                S["diag_any"] += 1
        else:
            items = dg if isinstance(dg, list) else []
            if items:
                S["diag_key"] += 1
                S["diag_any"] += 1
        if items:
            S["diag_items_rec"] += 1
        r_obj = r_val = r_stage = r_y = r_y_val = False   # 记录级 TNM 标志（跨诊断条目取并）
        r_dirty = r_clean = False
        r_y_dirty = False
        r_mal = False
        for x in items:
            if not isinstance(x, dict):
                continue
            S["diag_items"] += 1
            nm = _s(x.get("name"))
            if "癌" in nm or "恶性" in nm:
                r_mal = True
            tv = x.get("tnm")
            objs = [o for o in (tv if isinstance(tv, list) else [tv]) if isinstance(o, dict)]
            if objs:
                S["tnm_obj_marks"] += 1
                r_obj = True
            for o in objs:
                bad_fields = set()
                for q in (o.get("derived_qc") or []):
                    if isinstance(q, dict) and q.get("field") in ("t", "n", "m"):
                        bad_fields.add(q.get("field"))
                vals = {k: _s(o.get(k)) for k in ("t", "n", "m")}
                vals = {k: v for k, v in vals.items() if v}
                has_val = bool(vals)
                if has_val:
                    S["tnm_valued_marks"] += 1
                    r_val = True
                    if bad_fields & set(vals):
                        S["tnm_dirty_marks"] += 1
                        r_dirty = True
                    else:
                        S["tnm_clean_marks"] += 1
                        r_clean = True
                if _s(o.get("stage")):
                    S["tnm_staged_marks"] += 1
                    r_stage = True
                pfx = _s(o.get("prefix"))
                if pfx:
                    tnm_prefix[pfx] += 1
                    if pfx.lower().startswith("y"):   # yp 也是新辅助后，一并算作 y 前缀
                        S["y_marks"] += 1
                        r_y = True
                        if has_val:
                            if bad_fields & set(vals):
                                S["y_marks_dirty"] += 1
                                r_y_dirty = True
                            else:
                                S["y_marks_val"] += 1
                                r_y_val = True
                        else:
                            S["y_marks_blank"] += 1
        if r_mal:
            S["malignant_rec"] += 1
        if r_obj:
            S["tnm_rec_obj"] += 1
        if r_val:
            S["tnm_rec_val"] += 1
        if r_dirty:
            S["tnm_rec_dirty"] += 1
        if r_clean:
            S["tnm_rec_clean"] += 1
        if r_stage:
            S["tnm_rec_stage"] += 1
        if r_y:
            S["y_rec"] += 1
            if r_y_val:
                S["y_rec_val"] += 1
            elif r_y_dirty:
                S["y_rec_dirty"] += 1
            else:
                S["y_rec_blank"] += 1

        # ---------- 治疗 / 医嘱 ----------
        tx = rec.get("treatment")
        if isinstance(tx, dict):
            S["tx_key"] += 1
            if any(tx.values()):
                S["tx_any"] += 1
            tx_items = tx.get("items") or []
        else:
            tx_items = tx if isinstance(tx, list) else []
            if tx_items:
                S["tx_key"] += 1
                S["tx_any"] += 1
        if tx_items:
            S["tx_items_rec"] += 1
        for x in tx_items:
            if isinstance(x, dict):
                S["tx_items"] += 1
                tx_mod[_s(x.get("modality")) or "<空>"] += 1

        od = rec.get("orders")
        if isinstance(od, dict):
            S["ord_key"] += 1
            if any(od.values()):
                S["ord_any"] += 1
            ord_items = od.get("items") or []
        else:
            ord_items = od if isinstance(od, list) else []
            if ord_items:
                S["ord_key"] += 1
                S["ord_any"] += 1
        if ord_items:
            S["ord_items_rec"] += 1
        S["ord_items"] += len([x for x in ord_items if isinstance(x, dict)])

    # ---------- 派生：纵向随访 ----------
    ex_spans = []
    for dates in pat_exam_dates.values():
        if len(dates) > 1:
            ds = sorted(dates)
            ex_spans.append(int(ds[-1][:4]) - int(ds[0][:4]))
    span_dist = collections.Counter(ex_spans)
    multi_exam = sum(1 for v in pat_exam_dates.values() if len(v) > 1)
    multi_exam_reports = sum(1 for v in pat_exam_list.values() if len(v) > 1)

    # ---------- 派生：清洗账本 ----------
    clean = collections.Counter()
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                at = _s(it.get("at"))
                clean[at.rsplit(".", 1)[-1].split("[")[0]] += 1

    N = S["records"] or 1
    out = {
        "_meta": {
            "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "生成脚本": "研究分析/10_analysis/build_final_numbers.py",
            "唯一数据源": TAR,
            "数据源字节数": os.path.getsize(TAR),
            "数据源md5": md5_of(TAR),
            "用途": "终态 tar 的快照式指标卡（规模／覆盖／条目／日期／闸门）；供快速查阅与交叉核对",
            "口径约定": {
                "记录": "一条门诊就诊（patient.登记号 + visit.病历ID）",
                "患者": "按 patient.登记号 去重",
                "条目": "某板块数组的一个元素（诊断项 / 医嘱项 / 报告 / 检验组合 / 标志物…）",
                "指标": "检验 results[] 的一个元素",
                "板块_有实体": "reports / labs / ihc.panels 至少一项有内容",
                "板块_任一子键": "板块下任一子键非空；会把 ihc:{\"panels\":[]} 空壳算作非空，仅作对照",
            },
            "引用铁律": [
                "写「张/份」之前先确认是哪个量词：检验条目不等于检验份数",
                "本次重跑的闸门数字与其他计数同遍同源，可与本文件交叉核对",
            ],
        },
        "规模": {
            "_口径": "记录 / 患者",
            "记录数": S["records"],
            "患者数": len(pat),
            "性别": dict(collections.Counter(v["sex"] for v in pat.values()).most_common()),
            "病历类型_记录数": dict(rec_type.most_common()),
            "科室_记录数": dict(dept.most_common()),
        },
        "板块覆盖": {
            "_口径": "记录数；叙述板块按 raw/text/structured 任一有值",
            "主诉": S["has_chief_complaint"],
            "现病史": S["has_present_illness"],
            "既往史": S["has_past_history"],
            "体格检查": S["has_physical_exam"],
            "辅助检查_有实体": S["aux_entity"],
            "辅助检查_任一子键": S["aux_any"],
            "诊断_有items": S["diag_items_rec"],
            "诊断_任一子键": S["diag_any"],
            "医嘱_有items": S["ord_items_rec"],
            "医嘱_任一子键": S["ord_any"],
            "治疗_有items": S["tx_items_rec"],
            "治疗_任一子键": S["tx_any"],
            "_提示": "辅助检查务必用「有实体」口径；任一子键会把 2 万余条 ihc:{\"panels\":[]} 空壳算进来",
        },
        "实体条目": {
            "_口径": "条目数（不是记录数）",
            "诊断条目": S["diag_items"],
            "恶性诊断记录数_名含癌或恶性": S["malignant_rec"],
            "医嘱条目": S["ord_items"],
            "治疗条目": S["tx_items"],
            "检查报告条目": S["reports"],
            "检验组合条目": S["labs_entries"],
            "IHC面板条目": S["ihc_panels"],
            "IHC标志物条目": S["ihc_markers"],
            "淋巴结_触诊条目": S["ln_items"],
        },
        "BI-RADS": _birads_block(S, bi_dist, rec_bi_unit, rec_bi_val),
        "检验": {
            "_口径": "混合：先看键名里的量词",
            "含检验记录数": S["rec_with_labs"],
            "检验组合条目数": S["labs_entries"],
            "条目有results数": S["labs_with_results"],
            "指标值条数": S["lab_values"],
            "指标有单位数": S["lab_values_unit"],
            "指标带标准编码数_有值内": S["lab_values_coded"],
            "指标带标准编码数_含无值": S["lab_coded_all"],
            "唯一指标值数_去重": len(lab_uniq),
            "键形态分布": dict(lab_shape.most_common()),
            "无组合名条目数": S["labs_no_panel"],
            "无组合名且有指标条目数": S["labs_no_panel"] - S["labs_no_panel_no_detail"],
            "无组合名且无明细条目数": S["labs_no_panel_no_detail"],
            "有panel_name键条目数": S["labs_has_pn_key"],
            "有panel键条目数": S["labs_has_p_key"],
            "_无组合名说明": "「无组合名」以**值**判空（panel_name 与 panel 均为空）。按键存在算会少 614 条"
                        "（有键但值为空），故与早期报告的 41,870 差 614，以本文件为准",
            "panel_name_view条目数": S["labs_panel_name_from"],
            "同记录内内容重复条目数_多余": S["lab_dup_excess"],
            "同记录内内容重复记录数": S["lab_dup_rec"],
            "检验项目top": dict(lab_item_names.most_common(15)),
            "_铁律": "五形态并存且指标互不包含 → 禁合并去重；按指标聚合须遍历全部形态",
        },
        "IHC": {
            "_口径": "混合：记录 / 条目",
            "有IHC记录数": S["rec_with_ihc"],
            "IHC面板条目": S["ihc_panels"],
            "标志物条目": S["ihc_markers"],
        },
        "TNM": {
            "_口径": "键名以「条目」/「记录」结尾区分量词；「合法」= 已剔除 derived_qc 标记的值域外脏值",
            "有tnm对象记录数": S["tnm_rec_obj"],
            "有tnm对象诊断条目数": S["tnm_obj_marks"],
            "有非空t或n或m记录数": S["tnm_rec_val"],
            "其中t或n或m全部合法记录数": S["tnm_rec_clean"],
            "其中含值域外脏值记录数": S["tnm_rec_dirty"],
            "有非空t或n或m诊断条目数": S["tnm_valued_marks"],
            "其中合法诊断条目数": S["tnm_clean_marks"],
            "含脏值诊断条目数": S["tnm_dirty_marks"],
            "有stage记录数": S["tnm_rec_stage"],
            "有stage诊断条目数": S["tnm_staged_marks"],
            "前缀分布_条目级": dict(tnm_prefix.most_common()),
            "y前缀记录数": S["y_rec"],
            "y前缀有合法t或n或m记录数": S["y_rec_val"],
            "y前缀仅含脏值记录数": S["y_rec_dirty"],
            "y前缀全空记录数": S["y_rec_blank"],
            "y前缀条目数": S["y_marks"],
            "y前缀有合法值条目数": S["y_marks_val"],
            "y前缀含脏值条目数": S["y_marks_dirty"],
            "_y说明": "「全空」= 有 y 前缀但 t/n/m 全空；回查 2,040 条样本得 1,759 条原文无三段式 TNM 串，属原文未记录而非漏抽",
            "_脏值说明": "t/n/m 的数值 0 / 400 / 300 与空容器 {} 是抽取层假填充（0 也出现在「肝损伤」「肝酶升高」等非肿瘤诊断上），"
                     "derived_qc 已逐条标记值域外，不得计入有效 TNM",
        },
        "体格检查": {
            "_口径": "记录 / 条目",
            "触诊淋巴结有值记录数": S["ln_has"],
            "触诊淋巴结有值占比": round(S["ln_has"] / N, 4),
            "淋巴结触诊条目数": S["ln_items"],
            "nature分布_条目级": dict(ln_nature.most_common(10)),
        },
        "检验检查日期": {
            "_口径": "条目级（报告条目 / 检验条目）；「月精度/仅年/未补零」是有效信息，非脏值",
            "报告_完整日期": S["reports_dated"],
            "报告_无日期": S["exam_date_absent"],
            "报告_月精度YYYY-MM": S["date_ym"],
            "报告_仅年": S["date_y"],
            "报告_未补零": S["date_nopad"],
            "报告_其他写法": S["date_other"],
            "报告_置空且存exam_date_raw": S["date_blanked"],
            "报告_空且无exam_date_absent标记": S["date_empty_nomark"],
            "检验_完整日期": S["lab_date_full"],
            "检验_无日期": S["lab_date_absent"],
            "检验_月精度YYYY-MM": S["lab_date_ym"],
            "检验_未补零": S["lab_date_nopad"],
            "检验_其他写法": S["lab_date_other"],
            "检验_置空且存exam_date_raw": S["lab_date_blanked"],
            "检验_空且无absent标记": S["lab_date_empty_nomark"],
            "空日期合计条目数": S["exam_date_absent"] + S["lab_date_absent"],
            "空且无标记合计条目数": S["date_empty_nomark"] + S["lab_date_empty_nomark"],
            "有exam_date_absent标记合计条目数": (S["exam_date_absent"] - S["date_empty_nomark"])
                                          + (S["lab_date_absent"] - S["lab_date_empty_nomark"]),
            "日期精度不足合计条目数": S["date_ym"] + S["date_y"] + S["date_nopad"] + S["date_other"]
                                 + S["lab_date_ym"] + S["lab_date_nopad"] + S["lab_date_other"],
            "月精度合计条目数": S["date_ym"] + S["lab_date_ym"],
            "_提示": "「置空且存 raw」的原文多为「外院/待补充/复查」，已置空并保留原值；月精度等按原样保留，不要当畸形值清空",
        },
        "纵向随访": {
            "_口径": "患者级",
            "有检查日期的患者数": len(pat_exam_dates),
            "≥2个不同检查日期患者数": multi_exam,
            "≥2份检查报告患者数_含同日多份": multi_exam_reports,
            "_差异说明": "同日多份报告会造成后者显著大于前者（约多 4 千余人），纵向分析应用「不同日期」口径",
            "检查跨年跨度分布_患者数": {str(k): v for k, v in sorted(span_dist.items()) if k <= 100},
            "跨≥3年患者数": sum(v for k, v in span_dist.items() if 3 <= k <= 100),
            "跨≥5年患者数": sum(v for k, v in span_dist.items() if 5 <= k <= 100),
            "跨度异常超100年患者数": sum(v for k, v in span_dist.items() if k > 100),
        },
        "质量闸门": {
            "_口径": "记录级；与 validate.py --tar 同源（复用 validate_record）",
            "记录数": gate["records"],
            "pass": gate["pass"],
            "fail": gate["fail"],
            "pass率": round(gate["pass"] / N, 4),
            "板块缺失": gate["blocks_missing"],
            "值域越界": gate["vocab"],
            "TNM问题": gate["tnm"],
            "辅检硬违规": gate["aux"],
            "辅检待处置_项": gate["aux_pending_item"],
            "辅检待处置_记录": gate["aux_pending_rec"],
            "值域越界_按字段记录数": dict(gate_vocab_rec.most_common()),
            "值域越界_按字段处数": dict(gate_vocab.most_common()),
            "TNM问题_按字段": {("%s|%s" % k): v for k, v in gate_tnm.most_common(12)},
            "辅检问题_按位置": {("%s|%s" % k): v for k, v in gate_aux.most_common(12)},
            "_注": "辅检待处置=原文缺失类，不计失败",
        },
        "清洗归一": {
            "_口径": "改动处数（一记录可多处）",
            "改动处数合计": sum(clean.values()),
            "分项": dict(clean.most_common()),
            "_账本": LEDGER,
        },
    }

    # TNM 记录级补算（条目级已在遍历中累加，这里按记录口径另给）
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("已写出：%s" % OUT)
    print("记录 %d | 患者 %d | pass %d (%.2f%%) | fail %d"
          % (S["records"], len(pat), gate["pass"], 100 * gate["pass"] / N, gate["fail"]))
    print("实体条目：诊断 %d / 医嘱 %d / 治疗 %d / 报告 %d / 检验组合 %d / IHC面板 %d"
          % (S["diag_items"], S["ord_items"], S["tx_items"], S["reports"],
             S["labs_entries"], S["ihc_panels"]))

if __name__ == "__main__":
    main()
