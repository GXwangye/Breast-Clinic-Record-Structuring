#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import collections
import glob
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
for p in ("/结构化处理脚本/00_core", "/结构化处理脚本/05_agents"):
    sys.path.insert(0, ROOT + p)

from tarstream import iter_records                      # noqa: E402
from validate import validate_record, rep_ok            # noqa: E402
from recount_structure_dims import keys_of, leaves, anchors, load_dir  # noqa: E402
from compute_item_alignment import as_items             # noqa: E402

TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
INPUT_ALL = DATA + "/中间产物/_agents_input/input_records.jsonl"
INPUT100 = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
NAIVE = DATA + "/中间产物/ablation_naive"
L1 = ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize"
L2 = ROOT + "/publish/05_消融与基线评测/results/l2_no_split"
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
QC = DATA + "/中间产物/_standardize/standardize_qc.jsonl"
DG = DATA + "/qa_reports/standardize_degrade.json"
DG_PROBE = ROOT + "/研究分析/90_qa_report/probe_standardize_degrade.py"
GRN = DATA + "/qa_reports/granularity.json"
BG = DATA + "/qa_reports/birads_grounding.json"
GRN_PROBE = ROOT + "/研究分析/probe_granularity.py"
AUDIT = DATA + "/中间产物/_qa_out/tnm_coverage_audit.json"
DESC = DATA + "/中间产物/_qa_out/结构化基础描述统计.json"
QA = DATA + "/qa_reports"
OUT_JSON = QA + "/结果清单.json"
OUT_MD = QA + "/结果清单.md"
DOC = ROOT + "/文档/论文_初稿_20261108.md"

# 反查豁免：明确不属于「结果数字」的 token
# 其余为字符区间端点、HTTP 码、版本号（2021 药品字典版、2.83 LOINC 版）、章节号、offset 值、记录号与 md5 片段。
#   530021=单位邮编；9111/3653=ORCID 片段；163=邮箱域名；71.7/63.7=讨论引文[28]的中国大样本 ER/PR 阳性率。
KNOWN_OK = {"7.68", "296", "49", "199", "499", "999", "120", "119", "435", "429", "503",
            "3.8", "5.3", "2.2", "80", "67", "59", "71", "83", "23896801", "6750",
            "2021", "2.83", "2025", "2026", "20261108", "45784206", "48299851", "45994403", "47956953",
            "530021", "9111", "3653", "163", "71.7", "63.7"}

FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
TOP8 = ["chief_complaint", "present_illness", "past_history", "physical_exam",
        "auxiliary_exam", "diagnoses", "treatment", "orders"]
PH = {"", "未提及", "未指明", "空", "无", "不详", "未见", "未描述", "未报告",
      "未记载", "u", "unknown", "na", "n/a", "none", "null", "-", "－", "—", "未做", "未查"}

LEDGER = []
_CNT = Counter()
_EXTRA = {}      # scan_product 的零散计数，供 from_records 复用
REV_ISSUES = []  # 审核组的闭合校验，由 scan_review 填入，check_internal 汇总

def add(prefix, name, value, unit="", definition="", paper="", kind="重算",
        source="", formula="", status="可引用", decimals=None, note=""):
    if value is None:
        return None
    _CNT[prefix] += 1
    code = "%s-%02d" % (prefix, _CNT[prefix])
    if unit == "%":
        d = 1 if decimals is None else decimals
        disp = "%.*f%%" % (d, value)
    elif isinstance(value, float):
        d = 1 if decimals is None else decimals
        disp = "{:,.{}f}".format(value, d)
    else:
        disp = "{:,}".format(int(value))
    item = {"编号": code, "指标": name, "值": value, "显示": disp,
            "单位": unit, "口径": definition, "算式": formula, "论文": paper,
            "类型": kind, "来源": source, "状态": status, "备注": note,
            "组": prefix}
    LEDGER.append(item)
    return item

def real(v):
    return str(v or "").strip() not in PH

def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

# ======================================================================
# 一、源表侧（xlsx 单遍）
# ======================================================================
NEAR = re.compile(r"^(无|未见|未见异常|无异常|未查|无特殊|无殊|正常|同上|同前|无不适|"
                  r"否认|不详|无明[显确]异常|／|/|-|—|)\s*[。，,.;；]?$")

LEN_BINS = [(10, 49, "10-49"), (50, 199, "50-199"), (200, 499, "200-499"),
            (500, 999, "500-999"), (1000, 10 ** 9, "≥1000")]

def len_tier(n):
    for lo, hi, nm in LEN_BINS:
        if lo <= n <= hi:
            return nm
    return "<10"

def scan_source():
    import openpyxl
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    hdr, idx, ncols = None, {}, 0
    nrow = blank = 0
    chars_raw = chars_strip = 0
    lens = {f: [] for f in FIELDS}
    near = Counter()
    regs, doctors, mids = set(), set(), set()
    ctype = Counter()
    totals = []
    aux_form = Counter()

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            hdr = [("" if c is None else str(c).strip()) for c in row]
            ncols = len(hdr)
            idx = {f: hdr.index(f) for f in FIELDS if f in hdr}
            for k in ("登记号", "就诊医生", "病历ID", "病历类型"):
                if k in hdr:
                    idx[k] = hdr.index(k)
            continue
        vals = [("" if v is None else str(v)) for v in row]
        if not any(v.strip() for v in vals):
            blank += 1
            continue
        nrow += 1

        def g(k):
            j = idx.get(k)
            return vals[j] if (j is not None and j < len(vals)) else ""

        if g("登记号").strip():
            regs.add(g("登记号").strip())
        if g("就诊医生").strip():
            doctors.add(g("就诊医生").strip())
        if g("病历ID").strip():
            mids.add(g("病历ID").strip())
        ctype[g("病历类型").strip() or "<空>"] += 1

        tot = 0
        for f in FIELDS:
            v = g(f)
            s = v.strip()
            chars_raw += len(v)
            chars_strip += len(s)
            lens[f].append(len(s))
            tot += len(s)
            if not s or NEAR.match(s):
                near[f] += 1
        totals.append(tot)
        a = g("辅助检查").strip()
        if not a or NEAR.match(a):
            aux_form["占位符"] += 1
        elif len(a) >= 300:
            aux_form["长报告"] += 1
        else:
            aux_form["短文本"] += 1
    wb.close()

    n = nrow
    add("SRC", "源表总行数", n + blank, "行",
        "Excel 表头以下全部行（含尾部全空行）", "方法§2")
    add("SRC", "尾部全空行", blank, "行",
        "全部 91 列均为空的行", "方法§2", formula="%d-%d" % (n + blank, n))
    add("SRC", "数据行", n, "记录",
        "至少一列非空的行；等于入库记录数", "方法§2")
    add("SRC", "患者数", len(regs), "患者",
        "按登记号去重，空登记号不计", "方法§2")
    add("SRC", "病历 ID 去重数", len(mids), "个",
        "病历ID 去重；等于数据行数即无重复", "方法§2")
    add("SRC", "医生数", len(doctors), "名",
        "按就诊医生去重", "方法§2")
    add("SRC", "源表列数", ncols, "列",
        "表头列数", "方法§2")
    n_diag = len([h for h in hdr if re.fullmatch(r"诊断\d+", h)])
    n_ord = len([h for h in hdr if h == "医嘱" or re.fullmatch(r"医嘱\d+", h)])
    n_id = 8
    add("SRC", "列构成_标识与就诊", n_id, "列",
        "病历类型/病历ID/性别/登记号/就诊次数级别/科室/就诊日期/就诊医生", "方法§2")
    add("SRC", "列构成_叙述文本", len(FIELDS), "列", "主诉/现病史/既往史/辅助检查/体格检查", "方法§2")
    add("SRC", "列构成_诊断", n_diag, "列", "诊断1–诊断N", "方法§2")
    add("SRC", "列构成_医嘱", n_ord, "列", "医嘱 + 医嘱1–医嘱N", "方法§2")
    add("SRC", "列构成_诊断附加", ncols - n_id - len(FIELDS) - n_diag - n_ord, "列",
        "诊断复杂度/下一步处理/住院", "方法§2")
    add("SRC", "叙述字段字符数_去首尾空白", chars_strip, "字符",
        "5 个叙述字段逐格 strip 后长度求和", "方法§2")
    add("SRC", "叙述字段字符数_原样", chars_raw, "字符",
        "不去空白，含行内空格与换行", "未引用",
        note="论文用去空白口径，此处并列以便对照")

    for f in FIELDS:
        L = lens[f]
        L.sort()
        mean = sum(L) / float(n)
        med = L[len(L) // 2]
        add("T1", "%s_均值" % f, round(mean, 1), "字",
            "去首尾空白后的字符数均值", "附表S1", decimals=1,
            formula="%d/%d" % (sum(L), n))
        add("T1", "%s_中位" % f, med, "字", "同上，取中位数", "附表S1")
        add("T1", "%s_近空占比" % f, 100.0 * near[f] / n, "%",
            "空串或纯占位词（无/未见/未查/同上/—等）", "附表S1",
            formula="%d/%d" % (near[f], n), decimals=1)
        add("T1", "%s_≤10字占比" % f, 100.0 * sum(1 for x in L if x <= 10) / n, "%",
            "长度 ≤10 字的记录占比", "附表S1" if f in ("主诉", "体格检查") else "未引用",
            formula="%d/%d" % (sum(1 for x in L if x <= 10), n), decimals=1)
    add("T1", "辅助检查_>1000字占比", 100.0 * sum(1 for x in lens["辅助检查"] if x > 1000) / n, "%",
        "长度 >1000 字的记录占比", "附表S1",
        formula="%d/%d" % (sum(1 for x in lens["辅助检查"] if x > 1000), n), decimals=1)

    for k in ("占位符", "长报告", "短文本"):
        add("T1", "辅助检查形态_%s" % k, 100.0 * aux_form[k] / n, "%",
            "占位符=近空；长报告=≥300 字；短文本=其余", "附表S1",
            formula="%d/%d" % (aux_form[k], n), decimals=1)

    tiers = Counter(len_tier(t) for t in totals)
    for _, _, nm in LEN_BINS:
        add("T1", "文本总长档_%s" % nm, 100.0 * tiers[nm] / n, "%",
            "5 个叙述字段长度之和所在档", "方法§4",
            formula="%d/%d" % (tiers[nm], n), decimals=1)
    if tiers["<10"]:
        add("T1", "文本总长档_<10", 100.0 * tiers["<10"] / n, "%",
            "总长不足 10 字（论文表未列此档）", "未引用", decimals=1)

    for k, v in ctype.most_common():
        add("T1", "病历类型_%s" % k, 100.0 * v / n, "%",
            "源表病历类型字段，记录占比", "方法§4",
            formula="%d/%d" % (v, n), decimals=1)
        add("T1", "病历类型_%s_记录数" % k, v, "记录", "同上，绝对数", "未引用")
    return n

# ======================================================================
# 二、全量产物侧（tar 单遍）
# ======================================================================
def scan_product(subset_ids):
    S = Counter()
    pat_sex = {}
    rec_sex = Counter()
    diag_icd = diag_icd_n = 0
    diag_side = diag_side_n = 0
    rep_conc = rep_conc_n = 0
    rep_date = rep_date_n = 0
    rep_rec_any = rep_rec_dated = 0
    call = collections.defaultdict(Counter)
    mstatus = collections.defaultdict(Counter)
    ord_kind_blank = ord_kind_bad = ord_n = 0
    gate = Counter()
    sub = {}
    tnm_rec_any = tnm_rec_stage = 0
    diag_noitems = 0
    date_blank = date_nomark = date_marked = 0
    top_missing = Counter()

    for fn, rec in iter_records(TAR):
        rid = os.path.basename(fn).replace(".json", "")
        S["records"] += 1
        p = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
        reg = str(p.get("登记号") or "").strip()
        sx = str(p.get("性别_text") or p.get("性别") or "").strip()
        rec_sex[sx] += 1
        if reg and reg not in pat_sex:
            pat_sex[reg] = sx

        for t in TOP8:
            if not rec.get(t):
                top_missing[t] += 1
        if subset_ids and rid in subset_ids:
            sub[rid] = rec

        rep = validate_record(rec, rid)
        if rep_ok(rep):
            gate["pass"] += 1
        else:
            gate["fail"] += 1
            if rep["vocab"]:
                gate["vocab"] += 1
            if rep["tnm"]:
                gate["tnm"] += 1
            if rep["aux"]:
                gate["aux"] += 1
            # 分项可重叠（同一条记录既值域越界又 TNM 越界），另有一类只有板块缺失。
            # 不记这两个数，表 8 三行相加就对不上「未通过」那一行。
            _nc = sum(1 for _k in ("vocab", "tnm", "aux") if rep[_k])
            if _nc >= 2:
                gate["overlap"] += 1
            elif _nc == 0:
                gate["only_blocks"] += 1

        # 辅助检查
        rep_rec_date = False
        ae = rec.get("auxiliary_exam") if isinstance(rec.get("auxiliary_exam"), dict) else {}
        reports = ae.get("reports") or []
        labs = ae.get("labs") or []
        ihc = ae.get("ihc") if isinstance(ae.get("ihc"), dict) else {}
        panels = ihc.get("panels") or []
        if reports or labs or panels:
            S["aux_entity"] += 1
        S["reports"] += len([r for r in reports if isinstance(r, dict)])
        S["labs"] += len([l for l in labs if isinstance(l, dict)])
        S["panels"] += len([x for x in panels if isinstance(x, dict)])
        n_mk = 0
        for pn in panels:
            if not isinstance(pn, dict):
                continue
            for m in (pn.get("markers") or []):
                if not isinstance(m, dict):
                    continue
                n_mk += 1
                nm = str(m.get("marker_name") or "").strip().upper().replace("-", "")
                c = m.get("call")
                if c in ("阳性", "阴性", "不确定"):
                    call[nm][c] += 1
                st = m.get("status")
                if st:
                    mstatus[nm][st] += 1
        S["markers"] += n_mk
        if n_mk:
            S["rec_marker"] += 1

        for r in reports:
            if not isinstance(r, dict):
                continue
            rep_conc_n += 1
            ims = r.get("impressions")
            if isinstance(ims, list) and any(isinstance(i, dict) and str(i.get("text") or "").strip()
                                             for i in ims):
                rep_conc += 1
            rep_date_n += 1
            if str(r.get("exam_date") or "").strip():
                rep_date += 1
                rep_rec_date = True
            d = str(r.get("exam_date") or "").strip()
            if not d:
                date_blank += 1
                if not r.get("exam_date_absent"):
                    date_nomark += 1
                else:
                    date_marked += 1
        for l in labs:
            if not isinstance(l, dict):
                continue
            d = str(l.get("exam_date") or "").strip()
            if not d:
                date_blank += 1
                if not (l.get("exam_date_absent") or l.get("date_absent")):
                    date_nomark += 1
                else:
                    date_marked += 1
        if reports:
            rep_rec_any += 1
            if rep_rec_date:
                rep_rec_dated += 1

        # 诊断
        dg = rec.get("diagnoses")
        items = dg.get("items") if isinstance(dg, dict) else (dg if isinstance(dg, list) else [])
        items = [x for x in (items or []) if isinstance(x, dict)]
        S["diag"] += len(items)
        if not items:
            diag_noitems += 1
        any_tnm = any_stage = False
        for it in items:
            diag_icd_n += 1
            if str(it.get("icd_yb_code") or "").strip():
                diag_icd += 1
            diag_side_n += 1
            if str(it.get("side") or "").strip():
                diag_side += 1
            tv = it.get("tnm")
            objs = [o for o in (tv if isinstance(tv, list) else [tv]) if isinstance(o, dict)]
            for o in objs:
                if any(real(o.get(k)) for k in ("t", "n", "m", "stage")):
                    any_tnm = True
                if real(o.get("stage")):
                    any_stage = True
        if any_tnm:
            tnm_rec_any += 1
        if any_stage:
            tnm_rec_stage += 1

        # 治疗 / 医嘱
        tx = rec.get("treatment")
        txi = tx.get("items") if isinstance(tx, dict) else (tx if isinstance(tx, list) else [])
        S["tx"] += len([x for x in (txi or []) if isinstance(x, dict)])
        od = rec.get("orders")
        odi = od.get("items") if isinstance(od, dict) else (od if isinstance(od, list) else [])
        odi = [x for x in (odi or []) if isinstance(x, dict)]
        S["ord"] += len(odi)
        for x in odi:
            ord_n += 1
            k = str(x.get("kind") or "").strip()
            if not k:
                ord_kind_blank += 1
            elif k not in ("药品", "检查", "检验", "操作", "费用", "材料", "其他"):
                ord_kind_bad += 1

    add("PRD", "记录数", S["records"], "记录", "终态 tar 内 JSON 份数", "方法§6")
    add("PRD", "患者数", len(pat_sex), "患者", "按登记号去重", "方法§2")
    add("PRD", "性别_女_记录级", rec_sex.get("女", 0), "记录", "patient.性别_text=女", "附录B")
    add("PRD", "性别_男_记录级", rec_sex.get("男", 0), "记录", "同上", "附录B")
    add("PRD", "性别_女_患者级",
        sum(1 for v in pat_sex.values() if v == "女"), "患者", "按登记号去重后计", "附录B")
    add("PRD", "性别_男_患者级",
        sum(1 for v in pat_sex.values() if v == "男"), "患者", "同上", "附录B")

    add("PRD", "辅助检查_有实体记录数", S["aux_entity"], "记录",
        "reports/labs/ihc.panels 至少一项有内容；排除 ihc:{panels:[]} 空壳", "未引用",
        note="论文未引用；与「任一子键有值」口径差约 2 万条")
    add("PRD", "检查报告条目", S["reports"], "条目", "auxiliary_exam.reports[] 元素数", "§4")
    add("PRD", "检验组合条目", S["labs"], "条目", "auxiliary_exam.labs[] 元素数", "§4")
    add("PRD", "IHC 面板条目", S["panels"], "条目", "auxiliary_exam.ihc.panels[] 元素数", "未引用")
    add("PRD", "IHC 标志物条目", S["markers"], "条目",
        "panels[].markers[] 元素数；不含 excluded_markers", "§4")
    add("PRD", "有标志物记录数", S["rec_marker"], "记录", "markers[] 非空的记录", "§3")
    add("PRD", "诊断条目", S["diag"], "条目", "diagnoses.items[] 元素数", "§4")
    add("PRD", "治疗条目", S["tx"], "条目", "treatment.items[] 元素数", "§4")
    add("PRD", "医嘱条目", S["ord"], "条目", "orders.items[] 元素数", "§4")

    # 四件套 call 三态；非四件套（HER2 的 FISH 等）单列，不并入四件套
    four = ("ER", "PR", "HER2", "KI67")
    tot_four = sum(call[k]["阳性"] + call[k]["阴性"] + call[k]["不确定"]
                   for k in call if k in four)
    tot_all = sum(call[k]["阳性"] + call[k]["阴性"] + call[k]["不确定"] for k in call)
    add("IHC", "四件套三态合计", tot_four, "条目",
        "ER/PR/HER2/Ki67 中 call∈{阳性,阴性,不确定} 的条目合计", "表9")
    add("IHC", "全部标志物三态合计", tot_all, "条目",
        "含 HER2(FISH) 等非四件套；仅内部对账，不入正文", "内部台账",
        formula="%d+%d" % (tot_four, tot_all - tot_four))
    add("IHC", "四件套占标志物比例", 100.0 * tot_four / S["markers"] if S["markers"] else None, "%",
        "四件套三态条目 / 标志物条目", "表9",
        formula="%d/%d" % (tot_four, S["markers"]))
    for mk, label in (("ER", "ER"), ("PR", "PR"), ("HER2", "HER2")):
        c = call.get(mk, Counter())
        pos, neg, unc = c["阳性"], c["阴性"], c["不确定"]
        add("IHC", "%s_阳性条目" % label, pos, "条目", "call=阳性", "§4")
        add("IHC", "%s_阴性条目" % label, neg, "条目", "call=阴性", "§4")
        add("IHC", "%s_不确定条目" % label, unc, "条目", "call=不确定", "§4")
        add("IHC", "%s_三态合计" % label, pos + neg + unc, "条目",
            "阳性+阴性+不确定，即该标志物的阳性率分母", "§4",
            formula="%d+%d+%d" % (pos, neg, unc))
        add("IHC", "%s_阳性率" % label, 100.0 * pos / (pos + neg + unc) if (pos + neg + unc) else None, "%",
            "阳性 /（阳性+阴性+不确定）；不确定留在分母内，不排除", "§4",
            formula="%d/%d" % (pos, pos + neg + unc))
    # Ki67 与三者不同形态：status 恒「不适用」，水平只由 percent 承载，
    # 故不生成三态与阳性率——原先一并生成，在册留下 4 条 0 值，口径还写着
    # 「即该标志物的阳性率分母」，会被读成 Ki67 无数据。仅在三态非 0（异常）时列出。
    _ki = call.get("KI67", Counter())
    if any(_ki[k] for k in ("阳性", "阴性", "不确定")):
        for k in ("阳性", "阴性", "不确定"):
            add("IHC", "Ki67_%s条目" % k, _ki[k], "条目",
                "call=%s；预期恒 0，非 0 即为异常" % k, "内部台账")
    else:
        add("IHC", "Ki67_三态条目", 0, "条目",
            "Ki67 的 status 恒「不适用」，三态与阳性率不适用；水平只由 percent 承载", "§4")
    fish = call.get("HER2(FISH)", Counter())
    for k in ("阳性", "阴性", "不确定"):
        add("IHC", "HER2FISH_%s条目" % k, fish[k], "条目",
            "HER2(FISH) 的 call；非治疗决策四件套成员", "§4")
    add("IHC", "HER2FISH_合计条目", sum(fish[k] for k in ("阳性", "阴性", "不确定")), "条目",
        "HER2(FISH) 三态之和", "§4",
        formula="+".join(str(fish[k]) for k in ("阳性", "阴性", "不确定")))

    # TNM
    add("TNM", "有 TNM 真值记录数", tnm_rec_any, "记录",
        "t/n/m/stage 任一非占位", "附表S8／附录B")
    add("TNM", "有总分期记录数", tnm_rec_stage, "记录", "stage 非占位", "附录B")

    # 日期
    add("DAT", "空日期条目数", date_blank, "条目",
        "报告 exam_date 与检验 exam_date 均为空的条目合计", "附表S8")
    add("DAT", "空且无缺失标记条目数", date_nomark, "条目",
        "空日期且未写 exam_date_absent", "附表S8")
    add("DAT", "空但带缺失标记条目数", date_marked, "条目",
        "空日期且写了 exam_date_absent", "附表S8")

    # 覆盖率
    add("COV", "诊断 ICD 编码覆盖率", 100.0 * diag_icd / diag_icd_n if diag_icd_n else None, "%",
        "诊断条目中 icd 非空 / 全部诊断条目", "§4",
        formula="%d/%d" % (diag_icd, diag_icd_n))
    add("COV", "诊断侧别覆盖率", 100.0 * diag_side / diag_side_n if diag_side_n else None, "%",
        "诊断条目中 side 非空 / 全部诊断条目", "§4",
        formula="%d/%d" % (diag_side, diag_side_n))
    add("COV", "报告结论覆盖率", 100.0 * rep_conc / rep_conc_n if rep_conc_n else None, "%",
        "报告条目中 conclusion 非空 / 全部报告条目", "§4",
        formula="%d/%d" % (rep_conc, rep_conc_n))
    add("COV", "报告检查日期覆盖率_条目级",
        100.0 * rep_date / rep_date_n if rep_date_n else None, "%",
        "报告条目中 exam_date 非空 / 全部报告条目", "§4（旧值 75.5% 不可复现）",
        formula="%d/%d" % (rep_date, rep_date_n))
    add("COV", "报告检查日期覆盖率_记录级",
        100.0 * rep_rec_dated / rep_rec_any if rep_rec_any else None, "%",
        "有报告的记录中至少一份报告带日期 / 有报告的记录", "附表S8",
        formula="%d/%d" % (rep_rec_dated, rep_rec_any))
    add("COV", "报告全无日期记录占比",
        100.0 * (rep_rec_any - rep_rec_dated) / rep_rec_any if rep_rec_any else None, "%",
        "有报告的记录中全部报告都无日期 / 有报告的记录", "附表S8",
        formula="%d/%d" % (rep_rec_any - rep_rec_dated, rep_rec_any))
    add("COV", "医嘱 kind 空值条目", ord_kind_blank, "条目", "orders.items[].kind 为空", "§4")
    add("COV", "医嘱 kind 越界条目", ord_kind_bad, "条目", "kind 不在七桶闭集内", "§4")

    # 闸门
    N = S["records"]
    add("GAT", "闸门记录数", N, "记录", "与规模同遍同源", "§4")
    add("GAT", "pass 记录数", gate["pass"], "记录", "validate_record 无阻断项", "§4")
    add("GAT", "pass 率", 100.0 * gate["pass"] / N, "%", "pass/记录数", "§4",
        formula="%d/%d" % (gate["pass"], N), decimals=2)
    add("GAT", "fail 记录数", gate["fail"], "记录", "有任一阻断项", "§4")
    add("GAT", "值域越界记录数", gate["vocab"], "记录", "有字段取值不在契约闭集", "§4")
    add("GAT", "值域越界占比", 100.0 * gate["vocab"] / N, "%", "越界记录/记录数", "§4",
        formula="%d/%d" % (gate["vocab"], N), decimals=2)
    add("GAT", "TNM 形态问题记录数", gate["tnm"], "记录", "TNM 形态或维度校验未过", "§4")
    add("GAT", "辅检硬违规记录数", gate["aux"], "记录", "辅助检查硬性校验未过", "§4")
    add("GAT", "闸门分类重叠记录数", gate["overlap"], "记录",
        "同一条记录同时命中两个及以上分项（表 8 三行相加大于未通过数即由此）", "§4")
    add("GAT", "闸门仅板块缺失记录数", gate["only_blocks"], "记录",
        "未通过但三个分项均为空，仅顶层板块缺失", "内部台账")
    _EXTRA["diag_noitems"] = diag_noitems
    return S, sub, gate

# ======================================================================
# 三、子集与消融（100 条对照）
# ======================================================================
def stat_product(name, prods, rids, kind):
    ks = set()
    leaf = anc = lb = sf = 0
    mk = 0
    names = Counter()
    miss = Counter()
    n = 0
    for rid in rids:
        p = prods.get(rid)
        if p is None:
            continue
        n += 1
        keys_of(p, ks)
        leaf += leaves(p)
        anc += anchors(p)
        a = p.get("auxiliary_exam") or {}
        ih = a.get("ihc") if isinstance(a, dict) else None
        if isinstance(ih, dict):
            for pn in (ih.get("panels") or []):
                if isinstance(pn, dict):
                    for m in (pn.get("markers") or []):
                        if isinstance(m, dict):
                            mk += 1
                            names[str(m.get("marker_name") or "").strip()] += 1
        sf += count_sf(p)
        for t in TOP8:
            if not p.get(t):
                miss[t] += 1
    return {"产物": name, "记录数": n, "唯一字段名": len(ks),
            "非空叶子总": leaf, "非空叶子每条": round(leaf / n, 1) if n else 0,
            "anchor总": anc, "anchor每条": round(anc / n, 1) if n else 0,
            "marker总": mk, "marker每条": round(mk / n, 2) if n else 0,
            "source_field每条": round(sf / n, 1) if n else 0,
            "板块缺失": dict(miss), "板块缺失合计": sum(miss.values()),
            "名称": names, "类型": kind}

def count_sf(o):
    n = 0
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "source_field":
                if isinstance(v, str) and v.strip():
                    n += 1
                elif isinstance(v, list) and any(str(x).strip() for x in v):
                    n += 1
            else:
                n += count_sf(v)
    elif isinstance(o, list):
        for v in o:
            n += count_sf(v)
    return n

def measure_items(name, prods, inputs, rids):
    o_exp = o_got = d_exp = d_got = 0
    no_key = o_gap = d_gap = 0
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
            o_gap += 1
        if ed and not di:
            d_gap += 1
        if isinstance(p.get("orders"), dict) and not isinstance(p.get("orders").get("items"), list):
            no_key += 1
    return {"产物": name, "原文医嘱字段": o_exp, "产物医嘱条目": o_got,
            "医嘱差": o_got - o_exp, "医嘱结构缺失记录": o_gap,
            "orders无items记录": no_key,
            "原文诊断字段": d_exp, "产物诊断条目": d_got, "诊断差": d_got - d_exp,
            "诊断结构缺失记录": d_gap}

def scan_subset_and_ablation(sub):
    inputs = {}
    with io.open(INPUT100, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            inputs[r["record_id"]] = r
    rids = sorted(inputs)
    sets = [("完整工作流", sub, "实验组"),
            ("裸 LLM", load_dir(NAIVE), "对照组"),
            ("L1 无标准化", load_dir(L1), "消融"),
            ("L2 无分板块", load_dir(L2), "消融")]
    stats = []
    items = []
    for nm, pr, kind in sets:
        use = [r for r in rids if r in pr]
        stats.append(stat_product(nm, pr, use, kind))
        items.append(measure_items(nm, pr, inputs, use))

    add("SUB", "子集记录数", len(rids), "记录", "100 条分层抽样子集", "方法§4")
    add("SUB", "子集患者数", len({(inputs[r].get("ids") or {}).get("登记号") for r in rids}), "患者",
        "子集内登记号去重", "方法§4")

    # 子集三轴实际分布（方法§4 对照表左列）
    c_t = Counter()
    c_f = Counter()
    c_l = Counter()
    for rid in rids:
        inp = inputs[rid]
        fl = inp.get("fields") or {}
        c_t[(inp.get("ids") or {}).get("病历类型") or "<空>"] += 1
        a = (fl.get("辅助检查") or "").strip()
        c_f["占位符" if (not a or NEAR.match(a)) else
            ("长报告" if len(a) >= 300 else "短文本")] += 1
        c_l[len_tier(sum(len((fl.get(k) or "").strip()) for k in FIELDS))] += 1
    for k, v in c_t.items():
        add("SUB", "子集病历类型_%s" % k, v, "记录", "子集内该类型记录数", "方法§4")
    for k in ("占位符", "长报告", "短文本"):
        add("SUB", "子集辅助检查形态_%s" % k, c_f[k], "记录", "子集内该形态记录数", "方法§4")
    for _, _, nm in LEN_BINS:
        add("SUB", "子集总长档_%s" % nm, c_l[nm], "记录", "子集内该长度档记录数", "方法§4")

    for s in stats:
        tag = s["产物"]
        add("SIX", "%s_唯一字段名" % tag, s["唯一字段名"], "个",
            "递归收集全部键名，跨记录取并集", "表4" if tag != "L2 无分板块" else "未引用")
        add("SIX", "%s_非空叶子每条" % tag, s["非空叶子每条"], "个/条",
            "非空标量计 1，容器递归", "表4" if tag in ("完整工作流", "裸 LLM") else "§3",
            decimals=1)
        add("SIX", "%s_anchor每条" % tag, s["anchor每条"], "处/条",
            "任何字典含非空 anchor 键计 1 处", "表4" if tag in ("完整工作流", "裸 LLM") else "§3",
            decimals=1)
        add("SIX", "%s_标志物每条" % tag, s["marker每条"], "个/条",
            "仅 panels[].markers[] 元素；不含 excluded_markers",
            "表4" if tag in ("完整工作流", "裸 LLM") else "§3", decimals=2)
        add("SIX", "%s_source_field每条" % tag, s["source_field每条"], "处/条",
            "非空 source_field 计 1 处", "§3" if tag in ("完整工作流", "L2 无分板块") else "未引用",
            decimals=1)
        add("SIX", "%s_板块缺失处数" % tag, s["板块缺失合计"], "处",
            "8 个板块任一为空即计 1", "表4" if tag in ("完整工作流", "裸 LLM") else "未引用")
        for k, v in s["板块缺失"].items():
            if v:
                add("SIX", "%s_板块缺失_%s" % (tag, k), v, "处", "同上，按板块拆分", "§1"
                    if tag == "裸 LLM" else "未引用")

    # 名称收敛度（L2 的核心代价）
    try:
        import normalize_ihc as NIHC
        for s in stats:
            nm_c = s["名称"]
            tot = sum(v for k, v in nm_c.items() if k)
            uniq = len([k for k in nm_c if k])
            same = 0
            for k, v in nm_c.items():
                if not k:
                    continue
                try:
                    if NIHC.canon_name(k)[0] == k:
                        same += v
                except Exception:
                    pass
            if tot:
                add("SIX", "%s_唯一标志物名" % s["产物"], uniq, "种",
                    "markers[].marker_name 去重计数", "§3" if s["产物"].startswith("L2") else "未引用")
                add("SIX", "%s_字面即标准名比例" % s["产物"], 100.0 * same / tot, "%",
                    "名称字面等于 canon_name 的条数占比", "§3" if s["产物"].startswith("L2") else "未引用",
                    formula="%d/%d" % (same, tot))
    except Exception as e:
        print("   [warn] 名称收敛度未算：%s" % e)

    # 消融相对变化
    base = {s["产物"]: s for s in stats}
    wf = base.get("完整工作流")
    if wf:
        for tag in ("L1 无标准化", "L2 无分板块"):
            s = base.get(tag)
            if not s or not wf["非空叶子每条"]:
                continue
            add("ABL", "%s_非空叶子相对变化" % tag,
                100.0 * (wf["非空叶子每条"] - s["非空叶子每条"]) / wf["非空叶子每条"], "%",
                "（基线-该组）/基线，正数表示下降幅度",
                "§3", formula="(%.1f-%.1f)/%.1f" % (wf["非空叶子每条"], s["非空叶子每条"],
                                                    wf["非空叶子每条"]))
            add("ABL", "%s_医嘱条目" % tag, s["产物医嘱条目"] if False else
                [x["产物医嘱条目"] for x in items if x["产物"] == tag][0], "条目",
                "条目容器归一后计数", "§3")
        l2 = base.get("L2 无分板块")
        if l2 and wf["source_field每条"]:
            add("ABL", "L2_source_field相对变化",
                100.0 * (wf["source_field每条"] - l2["source_field每条"]) / wf["source_field每条"], "%",
                "（基线-L2）/基线", "§3",
                formula="(%.1f-%.1f)/%.1f" % (wf["source_field每条"], l2["source_field每条"],
                                              wf["source_field每条"]))

    for it in items:
        tag = it["产物"]
        add("ITM", "%s_原文医嘱字段" % tag, it["原文医嘱字段"], "条目",
            "源表 61 个医嘱列中非空单元格数", "表4")
        add("ITM", "%s_产物医嘱条目" % tag, it["产物医嘱条目"], "条目",
            "条目容器归一（items/orders/follow_up…）后计数", "表4")
        if it["原文医嘱字段"]:
            add("ITM", "%s_医嘱缺口率" % tag,
                100.0 * max(0, it["原文医嘱字段"] - it["产物医嘱条目"]) / it["原文医嘱字段"], "%",
                "max(0,原文-产物)/原文；净差为 0 不代表无漏抽", "表4",
                formula="%d/%d" % (max(0, it["原文医嘱字段"] - it["产物医嘱条目"]),
                                   it["原文医嘱字段"]))
        add("ITM", "%s_原文诊断字段" % tag, it["原文诊断字段"], "条目",
            "源表 14 个诊断列中非空单元格数", "表4")
        add("ITM", "%s_产物诊断条目" % tag, it["产物诊断条目"], "条目", "同上", "表4")
        if it["原文诊断字段"]:
            add("ITM", "%s_诊断缺口率" % tag,
                100.0 * max(0, it["原文诊断字段"] - it["产物诊断条目"]) / it["原文诊断字段"], "%",
                "同上口径", "表4",
                formula="%d/%d" % (max(0, it["原文诊断字段"] - it["产物诊断条目"]),
                                   it["原文诊断字段"]))
        if it["orders无items记录"]:
            add("ITM", "%s_orders无items键记录" % tag, it["orders无items记录"], "记录",
                "orders 是字典但无 items 列表键", "§1")
        if it["医嘱结构缺失记录"]:
            add("ITM", "%s_医嘱结构缺失记录" % tag, it["医嘱结构缺失记录"], "记录",
                "原文有医嘱但产物该板块 0 条目", "未引用")
    return stats, items

# ======================================================================
# 四、可重算的过程文件
# ======================================================================
def scan_qc():
    n = 0
    c = Counter()
    if os.path.exists(QC):
        with io.open(QC, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                c[str(d.get("err") or "?")[:60]] += 1
    if not os.path.exists(DG) or not os.path.exists(DG_PROBE):
        add("QCS", "登记失败_处", n, "处", "standardize_qc.jsonl 行数", "§4")
        return
    dg = json.load(io.open(DG, encoding="utf-8"))
    ev, rc = dg["事件"], dg["记录"]
    loc, exc = ev["定位类"], ev["异常类"]
    add("QCS", "全部登记_含接口_处", ev["合计"], "处",
        "标准化层失败登记事件：定位类与接口类合计（论文只引定位类）", "内部台账",
        formula="%d+%d" % (loc, exc))
    add("QCS", "定位类失败_处", loc, "处",
        "标记定位失败事件（整段兜底/段首未命中/标记为空）", "附表S8",
        formula="10165+4636+54")
    add("QCS", "接口类失败_处", exc, "处",
        "接口异常事件（服务端错误/超时/格式损坏/权限限流/空响应）", "附表S8",
        formula="661+57+36+21+17")
    add("QCS", "全部登记_含接口_涉及记录", rc["涉及"], "记录",
        "含接口异常在内的去重记录数（论文只引定位类）", "内部台账")
    add("QCS", "定位类_涉及占比", 100.0 * rc["定位类"] / rc["全库"], "%",
        "定位类涉及记录 / 全库记录", "§4",
        formula="%d/%d" % (rc["定位类"], rc["全库"]))
    add("QCS", "定位类_涉及记录", rc["定位类"], "记录", "只降级段边界，文本保留", "附表S8")
    add("QCS", "接口类_涉及记录", rc["异常类"], "记录", "当次不落盘，续跑重送", "附表S8")
    fb = dg["整段兜底"]
    add("QCS", "整段兜底_记录", fb["记录"], "记录",
        "整字段无可用标记，该字段整段作为单个叙述段", "附表S8",
        formula="整段兜底事件 %d 处按 rid 去重" % fb["事件"])
    add("QCS", "整段兜底_占全库", fb["占全库"], "%", "整段兜底记录 / 全库记录", "附表S8")
    cs = dg["定位类去向"]["守恒抽样"]
    add("QCS", "守恒核验_抽样记录", cs["抽样记录"], "记录",
        "定位类失败记录按 rid 升序抽取前 N 条", "附表S8")
    add("QCS", "守恒核验_抽样字段", cs["字段级_可比"], "字段",
        "可比字段（无段移出或移入），逐字比对原文", "附表S8")
    eb = dg["异常类去向"]
    add("QCS", "接口类_重送补齐记录", eb["补齐记录"], "记录",
        "续跑重送后产物存在且非空壳的记录数", "附表S8")
    add("QCS", "接口类_重送四轮记录", eb["重送轮次分布"].get("4", 0), "记录",
        "登记四次异常后补齐的记录数", "附表S8")
    for k, v in ev["明细"].items():
        add("QCS", "失败构成_事件_%s" % k, v, "处", "按 err 字段归类", "附表S8")
    for k, v in dg["各类记录"].items():
        add("QCS", "失败构成_记录_%s" % k, v, "记录", "同一类失败涉及的记录数（各类含重叠）", "附表S8")
    for k, v in eb["重送轮次分布"].items():
        add("QCS", "重送轮次_%s轮" % k, v, "记录", "登记异常后经续跑补齐所需的轮次", "附表S8")

# ======================================================================
# 四之二、结构化粒度（探针产出，全量 tar 单遍的统计结果）
# ======================================================================
def scan_granularity():
    if not os.path.exists(GRN) or not os.path.exists(GRN_PROBE):
        return
    g = json.load(io.open(GRN, encoding="utf-8"))
    unit, grade, dx, cb, tx = (g["影像与病理单元"], g["分级"], g["诊断与分期"],
                               g["共病"], g["治疗与查体"])
    add("GRN", "报告单元_影像类", unit["影像类单元"], "条目",
        "report_type 属影像类（超声/钼靶/X线/CT/MRI/核医学/造影/骨密度）", "表9")
    add("GRN", "报告单元_病理类", unit["病理类单元"], "条目", "report_type=病理", "表9")
    n_other_unit = unit["报告单元总数"] - unit["影像类单元"] - unit["病理类单元"]
    add("GRN", "报告单元_其他类", n_other_unit, "条目",
        "分子与基因检测、心电、内镜与越界写法等", "表9",
        formula="%d-%d-%d" % (unit["报告单元总数"], unit["影像类单元"], unit["病理类单元"]))
    add("GRN", "报告单元_侧别明确", unit["侧别明确（右/左/双）"], "条目",
        "report.laterality 为右/左/双", "表9")
    add("GRN", "报告单元_侧别明确占比", unit["侧别明确占比"], "%",
        "侧别明确 / 报告单元", "表9",
        formula="%d/%d" % (unit["侧别明确（右/左/双）"], unit["报告单元总数"]))
    add("GRN", "报告单元_乳腺", unit["按部位（前12）"]["乳腺"], "条目", "body_site=乳腺", "表9")
    add("GRN", "报告单元_腋窝淋巴结", unit["按部位（前12）"]["腋窝淋巴结"], "条目",
        "body_site=腋窝淋巴结", "表9")
    for k in ("超声", "病理", "CT", "核医学", "钼靶", "MRI"):
        add("GRN", "报告类型_%s" % k, unit["按检查类型"][k], "条目",
            "report_type 归一后按类型计", "表9")
    add("GRN", "BI-RADS_单元", grade["BI-RADS 单元"], "条目",
        "grading_system=BI-RADS 的报告单元", "表9")
    add("GRN", "BI-RADS_带分级值", grade["BI-RADS 单元中有分级值"], "条目",
        "report.birads／grading／grade_raw／impressions[].grade_raw 任一可解析", "表9")
    add("GRN", "BI-RADS_有值占比", grade["BI-RADS 有值占比"], "%",
        "带分级值 / BI-RADS 单元", "表9",
        formula="%d/%d" % (grade["BI-RADS 单元中有分级值"], grade["BI-RADS 单元"]))
    add("GRN", "BI-RADS_可判", grade["可判 BI-RADS 分级的单元"], "条目",
        "分级值可解析为 0-6 类（七级，含 6 类）", "表9")
    add("GRN", "BI-RADS_记录级有单元", grade.get("记录级：有 BI-RADS 单元的记录"), "记录",
        "至少一个报告单元标注 BI-RADS 体系的记录", "表9")
    add("GRN", "BI-RADS_记录级给值", grade.get("记录级：至少一个单元给出分级值"), "记录",
        "该记录下任一单元给出分级值", "表9")
    _lt = grade.get("记录级落地核验") or {}
    n_bi_raw = _lt.get("原文有分级_已给值", 0) + _lt.get("原文有分级_记录无值", 0)
    add("GRN", "BI-RADS_原文有分级记录", n_bi_raw, "记录",
        "源病历写出分级（BI-RADS+数字／分类N／N类 三类形态任一）且该记录有 BI-RADS 单元", "表9",
        formula="%d+%d" % (_lt.get("原文有分级_已给值", 0), _lt.get("原文有分级_记录无值", 0)))
    add("GRN", "BI-RADS_原文有分级_已给值", _lt.get("原文有分级_已给值"), "记录",
        "原文写明分级且产物给出值", "表9")
    add("GRN", "BI-RADS_原文有分级_记录无值", _lt.get("原文有分级_记录无值"), "记录",
        "原文写明分级而该记录所有单元均未给值", "表9")
    add("GRN", "BI-RADS_原文无分级_却给值", _lt.get("原文无分级_却给值"), "记录",
        "原文判无分级而产物仍给出值：需逐条归因，含裸写变体未识别与无出处两类", "表9")
    add("GRN", "BI-RADS_原文无_产物无", _lt.get("两边都无"), "记录",
        "原文无分级、产物也未给值", "表9")
    if n_bi_raw:
        add("GRN", "BI-RADS_记录级落地率",
            100.0 * _lt.get("原文有分级_已给值", 0) / n_bi_raw, "%",
            "已给值记录 / 原文写明分级的记录", "表9",
            formula="%d/%d" % (_lt.get("原文有分级_已给值", 0), n_bi_raw))
    # 值级回指：产物给的那个值能不能在原文里找到字面。只证「不是编的」，
    # 不证「挂对了单元」——归属错配仍能通过，此其上限。
    if os.path.exists(BG):
        _bg = (json.load(io.open(BG, encoding="utf-8")).get("值级回指") or {})
        if _bg:
            _lst = _bg.get("不可回指_清单") or []
            _n_range = sum(1 for x in _lst
                           if re.search(r"[-–—]", str(x.get("raw") or "")))
            add("GRN", "BI-RADS_值级回指_单元", _bg.get("单元有归一值"), "条目",
                "有归一分级值的 BI-RADS 单元", "表9")
            add("GRN", "BI-RADS_值级回指_可回指", _bg.get("可回指"), "条目",
                "该值的字面能在源病历中找到", "表9")
            add("GRN", "BI-RADS_值级回指_不可回指", _bg.get("不可回指"), "条目",
                "产物给了值、源病历找不到该值字面（范围归一与无出处两类合计）", "表9",
                formula="%d+%d" % (_n_range, len(_lst) - _n_range))
            add("GRN", "BI-RADS_不可回指_范围写法", _n_range, "条目",
                "原值形如 0-1类／3-4类，归一时取了单端，源病历无该单值字面", "表9")
            add("GRN", "BI-RADS_不可回指_单值无出处", len(_lst) - _n_range, "条目",
                "原值为单值，但该值字面在源病历中不存在", "表9")
            add("GRN", "BI-RADS_值级回指率", _bg.get("可回指率"), "%",
                "可回指单元 / 有归一值的单元", "表9",
                formula="%d/%d" % (_bg.get("可回指", 0), _bg.get("单元有归一值", 1)))
    for k, v in grade["BI-RADS 分级分布"].items():
        add("GRN", "BI-RADS_%s类" % k, v, "条目", "BI-RADS 分级值", "表9")
    _bd = grade["BI-RADS 分级分布"]
    _n4 = sum(_bd.get(x, 0) for x in ("4", "4a", "4b", "4c"))
    add("GRN", "BI-RADS_4类合计", _n4, "条目", "4／4a／4b／4c 四档之和", "表9",
        formula="+".join(str(_bd.get(x, 0)) for x in ("4", "4a", "4b", "4c")))
    _bt = grade.get("BI-RADS 按检查类型") or {}
    add("GRN", "BI-RADS_来源_X线", (_bt.get("X线") or {}).get("单元", 0), "条目",
        "report_type 归为 X 线", "表9")
    add("GRN", "BI-RADS_来源_其他类", ((_bt.get("其他") or {}).get("单元", 0)
                                     + (_bt.get("病理") or {}).get("单元", 0)), "条目",
        "六分法（超声/钼靶/MRI/X线/CT/其他）里的「其他」+病理；X 线单列，不含在本行", "表9",
        formula="%d+%d" % ((_bt.get("其他") or {}).get("单元", 0),
                          (_bt.get("病理") or {}).get("单元", 0)))
    gsys = grade["分级体系"]
    add("GRN", "病理分级体系_WHO", gsys.get("WHO分级", 0) + gsys.get("WHO", 0), "条目",
        "grading_system 为 WHO 分级（两种写法合并）", "表9",
        formula="%d+%d" % (gsys.get("WHO分级", 0), gsys.get("WHO", 0)))
    for k in ("Nottingham", "Miller-Payne分级系统"):
        add("GRN", "病理分级体系_%s" % k.replace("分级系统", ""), gsys.get(k, 0),
            "条目", "病理报告的 grading_system", "表9")
    add("GRN", "诊断_侧别明确", dx["侧别明确条目"], "条目",
        "diagnoses.items[].side 为右/左/双", "表9")
    add("GRN", "诊断_侧别明确占比", dx["侧别明确占比"], "%",
        "侧别明确条目 / 诊断条目", "表9",
        formula="%d/%d" % (dx["侧别明确条目"], dx["诊断条目"]))
    add("GRN", "诊断_侧别未指明占比", 100.0 - dx["侧别明确占比"], "%",
        "side 记为未指明 / 诊断条目", "表9")
    add("GRN", "分期条目", dx["带分期"], "条目", "tnm.stage 可归一为 Ⅰ-Ⅳ", "表9")
    for k, v in dx["分期分布"].items():
        add("GRN", "分期_%s期" % k, v, "条目", "分期归一后的主期", "表9")
    add("GRN", "分子分型条目", dx["带分子分型"], "条目", "diagnoses.items[].ihc_hint 给出分型", "表9")
    for k, v in dx["分子分型分布"].items():
        add("GRN", "分型_%s" % k, v, "条目", "分型写法归一后", "表9")
    add("GRN", "共病条目_真实共病", cb["真实共病条目"], "条目",
        "合并症条目中排除健康/无类表述", "表9")
    add("GRN", "有共病的记录", cb["有共病的记录"], "记录", "至少一条合并症的记录", "表9")
    add("GRN", "共病_带ICD10", cb["带 ICD-10 编码"], "条目", "comorbidities[].icd10 非空", "表9")
    add("GRN", "共病条目_含无类表述", cb["共病条目"], "条目",
        "comorbidities[] 元素数（未剔除\u201c无/健康\u201d类写法）", "表9")
    add("GRN", "共病_非共病表述", cb["非共病表述（健康/无）"], "条目",
        "写作无、健康、否认一类的条目", "表9")
    for k in ("高血压", "心脏病", "糖尿病", "甲状腺结节", "冠心病"):
        add("GRN", "共病_%s" % k, cb["前10位"].get(k, 0), "条目", "合并症标准名", "表9")
    add("GRN", "共病_乳腺癌史", cb["乳腺癌史条目"], "条目",
        "合并症中标准名为乳腺癌者，是乳腺癌本身病史，从合并症中剔除", "表9")
    add("GRN", "共病_除外乳腺癌史", cb["真实共病条目"] - cb["乳腺癌史条目"], "条目",
        "其他合并症条目（剔除乳腺癌本身病史）", "表9",
        formula="%d-%d" % (cb["真实共病条目"], cb["乳腺癌史条目"]))
    for k, v in cb["乳腺癌史状态"].items():
        add("GRN", "共病_乳腺癌史_%s" % k, v, "条目", "该条目的 status 字段", "表9",
            formula="既往 %d + 现症 %d" % (cb["乳腺癌史状态"].get("既往", 0),
                                          cb["乳腺癌史状态"].get("现症", 0)))
    for k, v in cb["乳腺癌史写法"].items():
        add("GRN", "共病_乳腺癌史写法_%s" % k, v, "条目", "该条目的 name 字段原样", "表9")
    for k in ("手术", "化疗", "内分泌", "靶向", "放疗", "其他"):
        add("GRN", "治疗方式_%s" % k, tx["治疗方式分布"].get(k, 0), "条目",
            "treatment.items[].modality 归一", "表9")
    add("GRN", "治疗_方式为空", tx["治疗方式为空"], "条目",
        "modality 缺失或为占位值，不计入六类", "表9")
    lab = g["检验与医嘱"]
    add("GRN", "检验_指标合计", lab["检验指标合计"], "条目",
        "labs[].results[] 元素合计；一条检验单下的一项指标", "表9")
    add("GRN", "检验_带组合名", lab["带组合名的记录"], "条目",
        "labs[] 元素带 panel_name/panel/exam_name 者", "表9")
    for k in ("血常规", "肝功能十六项", "肿瘤标志物"):
        add("GRN", "检验组合名_%s" % k, lab["组合名前10"].get(k, 0), "条目",
            "检验组合名出现次数", "表9")
    for k, v in lab["医嘱七桶"].items():
        add("GRN", "医嘱_%s" % k, v, "条目", "orders.items[].kind 七桶归类", "表9")
    add("GRN", "查体_乳腺条目", tx["乳腺查体条目"], "条目", "physical_exam.breast[]", "表9")
    add("GRN", "查体_淋巴结触诊条目", tx["淋巴结触诊条目"], "条目", "physical_exam.lymph_nodes[]", "表9")

    # BI-RADS 的影像来源：单元与带值按检查类型拆
    brt = grade.get("BI-RADS 按检查类型", {})
    for k in ("超声", "钼靶", "MRI", "CT"):
        e = brt.get(k) or {}
        add("GRN", "BIRADS来源_%s_单元" % k, e.get("单元", 0), "条目",
            "grading_system=BI-RADS 且 report_type=%s" % k, "表9")
        add("GRN", "BIRADS来源_%s_带分级值" % k, e.get("有分级值", 0), "条目",
            "该来源中 grading 非空", "表9")
    n_bi_rest = grade["BI-RADS 单元"] - sum((brt.get(k) or {}).get("单元", 0)
                                            for k in ("超声", "钼靶", "MRI", "CT"))
    add("GRN", "BIRADS来源_未单列_含X线", n_bi_rest, "条目",
        "四分法（超声/钼靶/MRI/CT 单列）后余下的单元：X 线 326 + 其他类 293；"
        "与上条「BI-RADS_来源_其他类」（293）不是同一口径，勿混用", "表9",
        formula="%d-%d-%d-%d-%d" % (grade["BI-RADS 单元"],
                                    (brt.get("超声") or {}).get("单元", 0),
                                    (brt.get("钼靶") or {}).get("单元", 0),
                                    (brt.get("MRI") or {}).get("单元", 0),
                                    (brt.get("CT") or {}).get("单元", 0)))
    n_bi_val_rest = grade["BI-RADS 单元中有分级值"] - sum(
        (brt.get(k) or {}).get("有分级值", 0) for k in ("超声", "钼靶", "MRI", "CT"))
    add("GRN", "BIRADS来源_未单列_带分级值", n_bi_val_rest, "条目",
        "四分法余下单元中 grading 非空（含 X 线）", "表9",
        formula="%d-%d-%d-%d-%d" % (grade["BI-RADS 单元中有分级值"],
                                    (brt.get("超声") or {}).get("有分级值", 0),
                                    (brt.get("钼靶") or {}).get("有分级值", 0),
                                    (brt.get("MRI") or {}).get("有分级值", 0),
                                    (brt.get("CT") or {}).get("有分级值", 0)))
    for k in ("超声", "钼靶", "MRI", "CT"):
        add("GRN", "BIRADS来源_%s占比" % k,
            100.0 * (brt.get(k) or {}).get("单元", 0) / grade["BI-RADS 单元"], "%",
            "%s 来源的 BI-RADS 单元 / 全部 BI-RADS 单元" % k, "表9",
            formula="%d/%d" % ((brt.get(k) or {}).get("单元", 0), grade["BI-RADS 单元"]))

    # 诊断的病种构成（就诊人群的病例谱）
    dg, dgr = dx["病种构成（条目）"], dx["病种构成（记录）"]
    n_dx_all = dx["诊断条目"]
    for label in ("乳腺手术后状态", "乳房结节与肿块", "乳腺恶性肿瘤",
                  "乳腺良性增生与囊肿", "乳腺恶性肿瘤治疗", "远处继发恶性肿瘤",
                  "乳腺增生与乳房肥大", "乳头及其他乳房疾患", "乳腺炎与脓肿",
                  "乳房良性肿瘤", "乳腺原位癌", "非乳腺诊断", "无编码", "其他手术后状态"):
        add("GRN", "诊断病种_%s" % label, dg.get(label, 0), "条目",
            "按 ICD 前缀归组，Z98 再按病名是否含乳二分", "表9")
        add("GRN", "诊断病种记录_%s" % label, dgr.get(label, 0), "记录",
            "同一记录多个条目同组只计一次", "表9")
        add("GRN", "诊断病种占比_%s" % label, 100.0 * dg.get(label, 0) / n_dx_all, "%",
            "该组条目 / 全部诊断条目", "表9",
            formula="%d/%d" % (dg.get(label, 0), n_dx_all))
    breast = n_dx_all - dg.get("非乳腺诊断", 0) - dg.get("无编码", 0) - dg.get("其他手术后状态", 0)
    add("GRN", "诊断_乳腺相关条目", breast, "条目",
        "诊断条目中乳腺疾病、乳腺术后状态与乳腺肿瘤治疗之和", "表9",
        formula="%d-%d-%d-%d" % (n_dx_all, dg.get("非乳腺诊断", 0), dg.get("无编码", 0),
                                 dg.get("其他手术后状态", 0)))
    add("GRN", "诊断_乳腺相关占比", 100.0 * breast / n_dx_all, "%", "乳腺相关条目 / 全部诊断条目", "表9",
        formula="%d/%d" % (breast, n_dx_all))
    benign3 = (dg.get("乳房结节与肿块", 0) + dg.get("乳腺良性增生与囊肿", 0)
               + dg.get("乳腺增生与乳房肥大", 0) + dg.get("乳房良性肿瘤", 0))
    add("GRN", "诊断_良性三类合计", benign3, "条目",
        "乳房结节与肿块 + 乳腺增生类（纤维囊性增生与乳腺增生）+ 乳房良性肿瘤", "表9",
        formula="%d+%d+%d+%d" % (dg.get("乳房结节与肿块", 0), dg.get("乳腺良性增生与囊肿", 0),
                                 dg.get("乳腺增生与乳房肥大", 0), dg.get("乳房良性肿瘤", 0)))
    add("GRN", "诊断_良性三类对恶性倍数", benign3 / dg.get("乳腺恶性肿瘤", 1), "倍",
        "良性三类合计 / 乳腺恶性肿瘤条目", "表9",
        formula="%d/%d" % (benign3, dg.get("乳腺恶性肿瘤", 1)))
    n_dg_rest = (dg.get("乳腺增生与乳房肥大", 0) + dg.get("乳头及其他乳房疾患", 0)
                 + dg.get("乳腺炎与脓肿", 0) + dg.get("乳房良性肿瘤", 0)
                 + dg.get("乳腺原位癌", 0))
    add("GRN", "诊断_其余五类合计", n_dg_rest, "条目",
        "乳腺增生、乳头疾患、乳腺炎、良性肿瘤与原位癌合计", "表9",
        formula="%d+%d+%d+%d+%d" % (dg.get("乳腺增生与乳房肥大", 0), dg.get("乳头及其他乳房疾患", 0),
                                   dg.get("乳腺炎与脓肿", 0), dg.get("乳房良性肿瘤", 0),
                                   dg.get("乳腺原位癌", 0)))
    for label in ("乳腺手术后状态", "乳房结节与肿块", "乳腺恶性肿瘤"):
        add("GRN", "诊断病种记录占比_%s" % label, 100.0 * dgr.get(label, 0) / 80718, "%",
            "该组记录 / 全库记录", "表9",
            formula="%d/%d" % (dgr.get(label, 0), 80718))

    # 患者级口径与就诊类型（初诊／术后复查／新辅助化疗）
    vp = g.get("就诊类型与患者级") or {}
    n_pt = vp.get("患者") or 0
    add("GRN", "患者数", n_pt, "患者", "patient.登记号 去重", "表9")
    for k, v in (vp.get("就诊类型（记录）") or {}).items():
        add("GRN", "病历类型原值_%s" % k, v, "记录",
            "patient.病历类型 原值；含外院术后首次来诊者，不等于疾病状态", "表9")
    for k, v in (vp.get("就诊类型（患者，首见）") or {}).items():
        add("GRN", "病历类型原值患_%s" % k, v, "患者",
            "患者首次就诊记录的病历类型", "表9")
    add("GRN", "术后状态复诊记录", vp.get("术后状态码记录"), "记录",
        "本次诊断带术后状态（Z98.8 病名含乳，或诊断名含「术后」而带 C50／D05／Z51 码），属复诊",
        "表9")
    _cross = vp.get("病种组 × 就诊类型（记录）") or {}
    add("GRN", "术后状态_病历类型初诊", (_cross.get("乳腺手术后状态") or {}).get("初诊"),
        "记录", "病历类型标注初诊、诊断却带术后状态码，按疾病状态应归复诊", "表9")
    add("GRN", "术后状态复诊患者", vp.get("术后状态码患者"), "患者",
        "同上，按登记号去重", "表9")
    if vp.get("术后状态码记录"):
        add("GRN", "术后状态复诊占比", 100.0 * vp["术后状态码记录"] / 80718, "%",
            "复诊记录 / 全库记录", "表9",
            formula="%d/%d" % (vp["术后状态码记录"], 80718))
    add("GRN", "新发或在治记录", vp.get("新发或在治记录"), "记录",
        "诊断含 C50／D05／Z51 且无术后状态（按上一条口径）的记录", "表9")
    add("GRN", "新发或在治患者", vp.get("新发或在治患者"), "患者", "同上，按登记号去重", "表9")
    if vp.get("新发或在治记录"):
        add("GRN", "新发或在治占比", 100.0 * vp["新发或在治记录"] / 80718, "%",
            "新发或在治记录 / 全库记录", "表9",
            formula="%d/%d" % (vp["新发或在治记录"], 80718))
    _rm = vp.get("良恶性（记录）") or {}
    _pm = vp.get("良恶性（患者）") or {}
    for k in ("乳腺癌", "仅良性", "兼有", "均无"):
        add("GRN", "良恶性记录_%s" % k, _rm.get(k, 0), "记录",
            "乳腺癌＝C50／D05／Z98.8／Z51；良性＝N60–N64 与 D24；兼有指同记录两组并存", "表9")
        add("GRN", "良恶性患者_%s" % k, _pm.get(k, 0), "患者",
            "同一位患者在任一次就诊中出现该组的归并结果", "表9")
        add("GRN", "良恶性记录占比_%s" % k, 100.0 * _rm.get(k, 0) / 80718, "%",
            "该组记录 / 全库记录", "表9", formula="%d/%d" % (_rm.get(k, 0), 80718))
    n_pt_bc = _pm.get("乳腺癌", 0) + _pm.get("兼有", 0)
    n_pt_ben = _pm.get("仅良性", 0) + _pm.get("兼有", 0)
    add("GRN", "乳腺癌患者", n_pt_bc, "患者",
        "有乳腺癌诊断或术后状态码或治疗码的患者", "表9",
        formula="%d+%d" % (_pm.get("乳腺癌", 0), _pm.get("兼有", 0)))
    add("GRN", "良性乳腺疾病患者", n_pt_ben, "患者",
        "有良性乳腺诊断的患者，与乳腺癌可有交叉", "表9",
        formula="%d+%d" % (_pm.get("仅良性", 0), _pm.get("兼有", 0)))
    if n_pt:
        add("GRN", "乳腺癌患者占比", 100.0 * n_pt_bc / n_pt, "%",
            "乳腺癌患者 / 患者总数", "表9", formula="%d/%d" % (n_pt_bc, n_pt))
        add("GRN", "良性乳腺疾病患者占比", 100.0 * n_pt_ben / n_pt, "%",
            "良性乳腺疾病患者 / 患者总数", "表9", formula="%d/%d" % (n_pt_ben, n_pt))
    add("GRN", "乳腺癌范畴_记录", vp.get("乳腺癌范畴记录"), "记录",
        "诊断含 C50／D05／Z98（病名含乳）／Z51 任一组的记录；C78 转移灶不并入", "表9")
    add("GRN", "乳腺癌范畴_患者", vp.get("乳腺癌范畴患者"), "患者",
        "同上，按登记号去重", "表9")
    if vp.get("乳腺癌范畴记录"):
        add("GRN", "乳腺癌范畴_占记录", 100.0 * vp["乳腺癌范畴记录"] / 80718, "%",
            "乳腺癌范畴记录 / 全库记录", "表9",
            formula="%d/%d" % (vp["乳腺癌范畴记录"], 80718))
    if vp.get("乳腺癌范畴患者") and n_pt:
        add("GRN", "乳腺癌范畴_占患者", 100.0 * vp["乳腺癌范畴患者"] / n_pt, "%",
            "乳腺癌范畴患者 / 患者总数", "表9",
            formula="%d/%d" % (vp["乳腺癌范畴患者"], n_pt))
    dgp = dx.get("病种构成（患者）") or {}
    for label in ("乳腺手术后状态", "乳房结节与肿块", "乳腺恶性肿瘤",
                  "乳腺良性增生与囊肿", "乳腺恶性肿瘤治疗", "远处继发恶性肿瘤",
                  "非乳腺诊断", "无编码"):
        add("GRN", "诊断病种患者_%s" % label, dgp.get(label, 0), "患者",
            "该患者在任一次就诊中出现该组诊断", "表9")

# ======================================================================
# 五、既有记录（不可重算，须指到文件）
# ======================================================================
def from_records():
    # 5.0 方法参数（论文中出现的非结果数字，同样须可溯源）
    for nm, v, u, d, p in (
            ("抽取板块数", 8, "个", "契约定义的抽取板块数量", "方法§7"),
            ("IHC 治疗决策型标志物数", 4, "个", "ER/PR/HER2/Ki67，出 call", "方法§7"),
            ("IHC 诊断鉴别型标志物数", 29, "个", "只记原值，不出结论", "方法§7"),
            ("评测子集大小", 100, "条", "分层抽样", "方法§4"),
            ("抽样随机种子", 42, "—", "随机种子", "方法§4"),
            ("监督周期间隔", 5, "个周期", "每 5 个 PASS 周期体检一次", "方法§7"),
            ("监督 anchor 阈值", 90, "%", "段级 anchor 均值低于此值停批", "表1"),
            ("快路径升级阈值", 85, "%", "锚点数不少于 5 且命中率低于此值，同记录开思考重跑一次", "方法§5"),
            ("监督异常率阈值", 12, "%", "记录异常率高于此值停批", "表1"),
            ("监督告警阈值", 8, "%", "记录异常率高于此值告警", "表1"),
            ("监督密度基线比", 70, "%", "条目密度连续 2 窗低于基线 70% 停批", "表4"),
            ("监督语义抽检条数", 3, "条", "每窗随机抽取", "表4"),
            ("标记点引文长度区间", 30, "字", "段首标记 6–30 字", "方法§3"),
            ("标记点引文长度下限", 6, "字", "同上", "方法§3"),
            ("临床正确性错判率阈值", 5, "%", "单字段错判率超此值判不通过", "结果§4"),
            ("临床正确性审核专家数", 2, "名", "双专家背靠背盲审", "结果§4"),
            ("亚型判据 PR 阈值", 20, "%", "PR 阴性或＜20% 即判 Luminal B（St Gallen）",
             "结果§4"),
            ("错判率区间置信水平", 95, "%", "Wilson 区间", "结果§4"),
            ("单次输出 token 上限", 32000, "token", "防长板块被截断", "方法§5"),
            ("采样温度", 0, "", "仅固定温度，其余采样参数取服务端默认", "方法§5"),
            ("本地 CPU 核数", 10, "核", "Apple M1 Max；模型推理在服务端，本地只做确定性工序",
             "方法§9"),
            ("本地 GPU 核数", 32, "核", "Apple M1 Max；本地计算不使用 GPU", "方法§9"),
            ("本地内存", 64, "GB", "统一内存", "方法§9")):
        add("PAR", nm, v, u, d, p, kind="参数")

    # 5.0b 保真性：可自动回指范围（既有实测，不可在本机重算）
    add("FID", "marker 总数（100 条）", 733, "条目",
        "auxiliary_exam.ihc.panels[].markers[] 元素数", "方法§5",
        kind="既有记录", source="文档/评估指标定义与规则_20261108.md:24")
    add("FID", "名称落在自身 quote 内的 marker", 258, "条目",
        "marker 名称为所属 panel.anchor.quote 的子串", "方法§5",
        kind="既有记录", source="同上")
    add("FID", "保真性可自动回指覆盖率", 100.0 * 258 / 733, "%",
        "可回指 marker / marker 总数；余 64.8% 须人工回原文核对", "方法§5",
        kind="既有记录", source="同上", formula="258/733")

    # 5.0c 保真性：三条件的引文回指（本机重算，裸 LLM 无 anchor 字段故不计）
    ah = json.load(io.open(DATA + "/中间产物/_qa_out/anchor_hit_by_condition.json",
                           encoding="utf-8"))
    for r in ah["结果"]:
        if not r["锚点总数"]:
            continue
        tag = r["条件"]
        add("FID", "%s_锚点总数" % tag, r["锚点总数"], "处",
            "产物中所有含非空 anchor 的字典计数", "结果§3",
            source="文档/稿件数字核验/probe_anchor_hit.py")
        add("FID", "%s_引文回指率" % tag, r["可回原文率"], "%",
            "可回原文/锚点总数；可回原文＝字段内命中＋跨字段命中", "结果§3",
            source="同上", formula="%d/%d" % (r["可回原文数"], r["锚点总数"]))
        add("FID", "%s_跨字段命中" % tag, r["跨字段命中"], "处",
            "引文不在所声明字段、但在本记录其他原文字段内找到", "结果§3",
            source="同上")
        if r["跨字段命中"]:
            add("FID", "%s_跨字段占比" % tag,
                100.0 * r["跨字段命中"] / r["锚点总数"], "%",
                "跨字段命中/锚点总数", "结果§3", source="同上", decimals=1)
    base0 = [x for x in ah["结果"] if x["条件"] == "完整工作流"][0]
    add("FID", "完整工作流_引文真未命中", base0["真未命中"], "处",
        "整条记录任何原文字段都找不到该引文", "结果§3",
        source="文档/稿件数字核验/probe_anchor_hit.py")

    # 5.1 数据基准
    b = os.path.getsize(TAR)
    add("REF", "终态产物字节数", b, "字节", "assembled_v7_final.tar 文件大小", "数据基准")
    add("REF", "终态产物体积", round(b / 1024.0 / 1024.0), "MB",
        "字节/1024/1024", "方法§6", formula="%d/1048576" % b)

    # 5.1 TNM 分母（审计脚本输出）
    a = json.load(io.open(AUDIT, encoding="utf-8"))
    m = a["malignant_diag_item_level"]
    f = a["fill_rate_in_should_have"]
    r = a["record_level_by_source_signal"]
    add("TNM", "原文有信号恶性诊断条目", f["分母(原文有TNM信号的恶性诊断条目)"], "条目",
        "原文含 pTNM 式或分期表述的恶性诊断条目", "附表S8", kind="既有记录",
        source="tnm_coverage_audit.py → tnm_coverage_audit.json")
    add("TNM", "条目级落地数", f["分子(有真值)"], "条目", "t/n/m/stage 任一有值", "附表S8",
        kind="既有记录", source="同上")
    add("TNM", "条目级落地率", 100.0 * f["落地率"], "%", "落地/分母", "附表S8",
        kind="既有记录", source="同上",
        formula="%d/%d" % (f["分子(有真值)"], f["分母(原文有TNM信号的恶性诊断条目)"]))
    add("TNM", "条目级漏抽数", f["漏抽条数"], "条目", "分母-分子", "附表S8", kind="既有记录", source="同上")
    add("TNM", "漏抽_原文有pTNM式却全空", m["空_原文有pTNM式"], "条目", "按原文形态拆分", "附表S8",
        kind="既有记录", source="同上")
    add("TNM", "漏抽_原文仅分期表述", m["空_原文仅分期"], "条目", "同上", "附表S8",
        kind="既有记录", source="同上")
    abc = r["A 原文有pTNM式_无真值_漏抽"] + r["B 原文仅分期_无真值_漏抽"] + r["C 原文有信号_已有真值"]
    add("TNM", "记录级原文有TNM信号", abc, "记录", "A+B+C", "附表S8／讨论§4", kind="既有记录",
        source="同上", formula="%d+%d+%d" % (r["A 原文有pTNM式_无真值_漏抽"],
                                             r["B 原文仅分期_无真值_漏抽"],
                                             r["C 原文有信号_已有真值"]))
    add("TNM", "记录级落地数", r["C 原文有信号_已有真值"], "记录", "记录内任一诊断有真值即算落地",
        "附表S8／讨论§4", kind="既有记录", source="同上")
    add("TNM", "记录级落地率", 100.0 * r["C 原文有信号_已有真值"] / abc, "%",
        "落地/原文有信号", "附表S8", kind="既有记录", source="同上",
        formula="%d/%d" % (r["C 原文有信号_已有真值"], abc))
    add("TNM", "原文无TNM字样却有真值", r["D 原文无TNM字样_有真值_合理推断"], "记录",
        "转移灶→M1/IV 的合理推断，非幻觉", "附表S8", kind="既有记录", source="同上")
    add("TNM", "原文无TNM信号_空壳", r["E 原文无TNM信号_无真值_正常空壳"], "记录",
        "留空即正确行为", "附表S8", kind="既有记录", source="同上")
    add("TNM", "记录级未取值_A原文有pTNM式", r["A 原文有pTNM式_无真值_漏抽"], "记录",
        "原文有完整 pTNM 式而 t/n/m/stage 全空", "附表S8", kind="既有记录", source="同上")
    add("TNM", "记录级未取值_B原文仅分期", r["B 原文仅分期_无真值_漏抽"], "记录",
        "原文只有分期表述（如Ⅲ期）而无 TNM 记号", "附表S8", kind="既有记录", source="同上")
    add("TNM", "记录级未取值_A加B", r["A 原文有pTNM式_无真值_漏抽"] + r["B 原文仅分期_无真值_漏抽"],
        "记录", "A+B", "附表S8", kind="既有记录", source="同上",
        formula="%d+%d" % (r["A 原文有pTNM式_无真值_漏抽"], r["B 原文仅分期_无真值_漏抽"]))
    add("TNM", "无诊断条目记录数", _EXTRA.get("diag_noitems", 0), "记录",
        "diagnoses.items 为空的记录，不参与原文信号分类", "附表S8")
    add("TNM", "恶性诊断条目总数", m["恶性诊断条目总数"], "条目",
        "诊断为恶性（含术后/继发）的诊断条目总数", "附表S8", kind="既有记录", source="同上")
    add("TNM", "条目级空_原文无信号", m["空_原文无信号"], "条目",
        "原文无 TNM 记号且未给值的恶性诊断条目", "附表S8", kind="既有记录", source="同上")

    # 5.2 分型（描述统计）
    d = json.load(io.open(DESC, encoding="utf-8"))
    ms = d["molecular_subtype"]
    add("MOL", "可判定亚型记录数", ms["records_classified"], "记录",
        "St Gallen 四分类可判定的记录", "§4", kind="既有记录",
        source="结构化基础描述统计.json → molecular_subtype")
    add("MOL", "亚型可判定率", 100.0 * ms["classification_rate"], "%",
        "可判定/全库记录", "§4", kind="既有记录", source="同上",
        formula="%d/%d" % (ms["records_classified"], 80718))
    tot = sum(ms["subtype"].values())
    for k, v in ms["subtype"].items():
        add("MOL", "亚型_%s_记录数" % k, v, "记录", "同上，按亚型", "附表S8",
            kind="既有记录", source="同上")
        add("MOL", "亚型_%s_占比" % k, 100.0 * v / tot, "%", "占可判定亚型记录",
            "§4", kind="既有记录", source="同上",
            formula="%d/%d" % (v, tot))

    # 5.3 IHC 跨字段（全量核查脚本）
    x = json.load(io.open(QA + "/IHC跨字段全量核查.json", encoding="utf-8"))
    lay = x["分层规模"]
    st = x["分层×产物状态"]
    add("XFD", "原文含IHC记录数", x["总计数"]["靶集记录"], "记录",
        "原文任一字段含 IHC 表达且非「未做/建议做」", "附表S6", kind="既有记录",
        source="probe_ihc_crossfield_full.py → IHC跨字段全量核查.json")
    add("XFD", "IHC仅写现病史记录数", lay["仅现病史"], "记录", "辅检字段无 IHC，仅现病史有", "附表S6",
        kind="既有记录", source="同上")
    add("XFD", "IHC仅写现病史占比",
        100.0 * lay["仅现病史"] / x["总计数"]["靶集记录"], "%",
        "仅现病史/原文含IHC记录", "附表S6", kind="既有记录", source="同上",
        formula="%d/%d" % (lay["仅现病史"], x["总计数"]["靶集记录"]))
    add("XFD", "IHC双写记录数", lay["双写"], "记录", "现病史与辅助检查都写了", "附表S6",
        kind="既有记录", source="同上")
    only = st["仅现病史"]["panels非空"]
    dual = st["双写"]["panels非空"]
    add("XFD", "仅现病史_召回率", 100.0 * only / lay["仅现病史"], "%",
        "该层产物 panels 非空占比", "附表S6", kind="既有记录", source="同上",
        formula="%d/%d" % (only, lay["仅现病史"]))
    add("XFD", "双写_召回率", 100.0 * dual / lay["双写"], "%", "同上", "附表S6",
        kind="既有记录", source="同上", formula="%d/%d" % (dual, lay["双写"]))
    add("XFD", "两路径召回差", 100.0 * dual / lay["双写"] - 100.0 * only / lay["仅现病史"], "pp",
        "双写 - 仅现病史", "附表S6", kind="既有记录", source="同上", decimals=1)

    # 非数据数字（章节序号与日期片段），显式登记以免被反查误判为数据
    add("META", "非数据数字_7", 7, "条",
        "非数据：§7 消融设计的章节序号、局限第 7 条；另含日期片段 11-07 的月份", "全文",
        kind="参数", source="人工确认")
    # 5.3b IHC 归位后拒收（归位与拒收探针）
    rr = json.load(io.open(QA + "/ihc_route_reject.json", encoding="utf-8"))
    SRC_RR = "probe_ihc_route_reject.py → ihc_route_reject.json"
    add("RJR", "静默记录数", rr["产物状态"]["仅现病史_静默为空"], "记录",
        "仅现病史层中产物免疫组化面板为空", "附表S6", kind="既有记录", source=SRC_RR)
    n_rr = rr["产物状态"]["仅现病史_静默为空"]
    mv = rr["取值去向"]["落点_辅助检查"]
    left = rr["取值去向"]["落点_仅现病史"]
    none = rr["取值去向"]["原文无取值"]
    has = rr["取值去向"]["原文含取值"]
    add("RJR", "原文含取值", has, "记录",
        "静默组中原文确有标志物取值的记录（宽判据：标志物名后 14 字内有百分数/正负号/阴阳强弱/膜浆核）",
        "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "原文含取值占比", 100.0 * has / n_rr, "%",
        "静默组中原文确有取值的比例", "附表S6", kind="既有记录", source=SRC_RR,
        formula="%d/%d" % (has, n_rr))
    add("RJR", "原文无取值", 100.0 * none / n_rr, "%",
        "静默组中原文只有“根据免疫组化结果”一类交代、无取值可抽", "附表S6",
        kind="既有记录", source=SRC_RR, formula="%d/%d" % (none, n_rr))
    add("RJR", "无取值_治疗叙述", rr["取值去向"]["　成因_治疗叙述"], "记录",
        "原文无取值者中，只写“术后根据免疫组化结果，拟行X方案”一类治疗交代（成因非互斥）",
        "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "无取值_明确未做", rr["取值去向"]["　成因_明确未做"], "记录",
        "原文无取值者中，写明未做或拒做免疫组化（非互斥）", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "无取值_只给报告号", rr["取值去向"]["　成因_只给报告号"], "记录",
        "原文无取值者中，只给外院或院内报告号、未列具体取值（非互斥）", "附表S6",
        kind="既有记录", source=SRC_RR)
    add("RJR", "归位成功率", 100.0 * mv / has, "%",
        "原文含取值的静默记录中，取值落点含辅助检查的比例（分母已扣除原文无取值者）",
        "附表S6", kind="既有记录", source=SRC_RR, formula="%d/%d" % (mv, has))
    add("RJR", "取值仍留现病史", 100.0 * left / has, "%", "归位未发生", "附表S6",
        kind="既有记录", source=SRC_RR, formula="%d/%d" % (left, has))
    add("RJR", "取值全文档丢失", rr["取值去向"]["落点_全文档无"], "记录",
        "标准化文档全部字段均无取值，即搬运过程中丢失", "附表S6",
        kind="既有记录", source=SRC_RR)
    lr = rr["分层校正召回"]
    add("RJR", "仅现病史_无取值", lr["仅现病史_无取值"], "记录",
        "该层原文含免疫组化表述但无取值可抽（仅现病史 19,051 条口径）", "附表S6",
        kind="既有记录", source=SRC_RR)
    add("RJR", "双写_无取值", lr["双写_无取值"], "记录",
        "同上，双写层", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "仅现病史_无取值占比", lr["仅现病史_无取值占比"], "%",
        "846/19,051", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "双写_无取值占比", lr["双写_无取值占比"], "%",
        "111/16,269", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "仅现病史_校正召回", lr["仅现病史_校正召回"], "%",
        "分母扣除原文无取值者", "附表S6", kind="既有记录", source=SRC_RR,
        formula="%d/(%d-%d)" % (rr["产物状态"]["仅现病史_抽到"],
                                rr["分层规模"]["仅现病史"], lr["仅现病史_无取值"]))
    add("RJR", "双写_校正召回", lr["双写_校正召回"], "%",
        "同上，双写层", "附表S6", kind="既有记录", source=SRC_RR,
        formula="%d/(%d-%d)" % (rr["产物状态"]["双写_抽到"],
                                rr["分层规模"]["双写"], lr["双写_无取值"]))
    add("RJR", "校正后两路径差", lr["校正差_pp"], "pp",
        "双写校正召回 - 仅现病史校正召回（原始差 20.3pp）", "附表S6",
        kind="既有记录", source=SRC_RR)
    blank = rr["产物形态"]["ihc键在但panels空"] + rr["产物形态"]["panels非空"]
    add("RJR", "产物_键在面板空_条数", rr["产物形态"]["ihc键在但panels空"], "记录",
        "静默记录中免疫组化键存在而面板为空", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "产物ihc键在面板空", 100.0 * blank / n_rr, "%",
        "静默记录的产物形态：键存在而面板为空壳", "附表S6", kind="既有记录", source=SRC_RR,
        formula="%d/%d" % (blank, n_rr))
    rej = rr["拒收说明"]["留拒收说明且指向源字段"]
    add("RJR", "拒收说明_未指向源字段_条数", rr["拒收说明"]["有说明未指向跨字段"], "记录",
        "留有说明但未写明内容来自现病史", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "留拒收说明占比", 100.0 * rej / n_rr, "%",
        "质控字段写明候选来自现病史、不在本栏录入", "附表S6", kind="既有记录",
        source=SRC_RR, formula="%d/%d" % (rej, n_rr))
    ip = rr["输入侧对照"]
    add("RJR", "输入侧_抽到组含IHC段", ip["抽到组"]["含IHC段占比"], "%",
        "该组中辅助检查段内含免疫组化表达的比例", "附表S6", kind="既有记录", source=SRC_RR)
    add("RJR", "输入侧_静默组含IHC段", ip["静默组"]["含IHC段占比"], "%",
        "同上，静默组", "附表S6", kind="既有记录", source=SRC_RR)

    # 5.3c 子集内的免疫组化分层（消融节 §3 的机制证据，口径限于 100 条）
    sl = json.load(io.open(QA + "/ihc_subset_layer.json", encoding="utf-8"))
    SRC_SL = "probe_ihc_subset_layer.py → ihc_subset_layer.json"
    lay = sl["分层"]
    add("ISL", "靶集记录", sl["靶集"], "记录", "子集内原文含免疫组化表达，与表 7 同一靶集", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "仅现病史记录", lay["仅现病史"], "记录", "辅助检查字段无 IHC 表达而现病史有", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "仅现病史_抽到", lay["仅现病史_抽到"], "记录", "产物 panels 非空或 marker 数>0", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "仅现病史_召回", 100.0 * lay["仅现病史_抽到"] / lay["仅现病史"], "%",
        "53/65", "未引用", kind="既有记录", source=SRC_SL,
        formula="%d/%d" % (lay["仅现病史_抽到"], lay["仅现病史"]))
    add("ISL", "辅助检查亦含_记录", lay["辅助检查亦含"], "记录",
        "辅助检查字段含 IHC 表达（其中两处都写 %d、仅辅助检查 %d）" % (
            lay["　其中两处都写"], lay["　其中仅辅助检查"]), "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "辅助检查亦含_抽到", lay["辅助检查亦含_抽到"], "记录", "同上判据", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "辅助检查亦含_召回", 100.0 * lay["辅助检查亦含_抽到"] / lay["辅助检查亦含"], "%",
        "16/17", "未引用", kind="既有记录", source=SRC_SL,
        formula="%d/%d" % (lay["辅助检查亦含_抽到"], lay["辅助检查亦含"]))
    add("ISL", "两层召回差", 100.0 * lay["辅助检查亦含_抽到"] / lay["辅助检查亦含"] -
        100.0 * lay["仅现病史_抽到"] / lay["仅现病史"], "pp", "17 条层 - 65 条层", "未引用",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默记录", sl["静默"]["总数"], "记录",
        "靶集内产物 panels 空且 marker 数为 0（＝表 7 的漏检 13 条）", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_仅现病史", sl["静默"]["仅现病史"], "记录", "分层归属", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_辅助检查亦含", sl["静默"]["辅助检查亦含"], "记录", "分层归属", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_硬闸门通过", sl["静默硬闸门判定"]["通过"], "记录",
        "静默记录在 validate.py 下的判定：空面板无条目可判负，故全数通过",
        "局限", kind="既有记录", source=SRC_SL,
        formula="%d/%d" % (sl["静默硬闸门判定"]["通过"], sl["静默"]["总数"]))
    ms = sl["静默取值去向"]
    add("ISL", "静默_原文含取值", ms["原文含取值"], "记录", "宽判据同 RJR 组", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_原文无取值", ms["原文无取值"], "记录", "只交代“根据免疫组化结果”", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_取值已落辅助检查", ms["落点_辅助检查"], "记录", "归位完成", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_取值仍留现病史", ms["落点_仅现病史"], "记录", "归位未发生", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_取值全文档丢失", ms["落点_全文档无"], "记录", "搬运过程中丢失", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_键在面板空", sl["静默产物形态"]["ihc键在面板空"], "记录",
        "静默记录的产物形态", "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "静默_留拒收说明", sl["静默留拒收说明"], "记录",
        "质控字段写明候选来自现病史、不在本栏录入", "附表S6", kind="既有记录", source=SRC_SL)

    cp = sl["两组对照"]
    rs = sl["静默段落位与救回"]
    add("ISL", "仅现病史层_去分板块命中", cp["仅现病史"]["去分板块命中"], "记录",
        "同一 65 条在去分板块条件下的产出", "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "仅现病史层_条件增量", cp["仅现病史"]["去分板块命中"] - cp["仅现病史"]["基线命中"],
        "记录", "62 − 53", "附表S6", kind="既有记录", source=SRC_SL,
        formula="%d-%d" % (cp["仅现病史"]["去分板块命中"], cp["仅现病史"]["基线命中"]))
    add("ISL", "辅助检查亦含层_去分板块命中", cp["辅助检查亦含"]["去分板块命中"], "记录",
        "同一 17 条在去分板块条件下的产出", "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "靶集_条件增量", cp["靶集合计"]["去分板块命中"] - cp["靶集合计"]["基线命中"],
        "记录", "79 − 69，其中 9 条来自仅现病史层", "附表S6", kind="既有记录", source=SRC_SL,
        formula="%d-%d" % (cp["靶集合计"]["去分板块命中"], cp["靶集合计"]["基线命中"]))
    o = rs["仅现病史"]
    add("ISL", "静默_段已入辅助检查", o["　段已入辅助检查"], "记录",
        "仅现病史层 12 条静默中，免疫组化段确已在辅助检查字段内", "附表S6",
        kind="既有记录", source=SRC_SL)
    add("ISL", "静默_段已入且含取值", o["　其中含取值"], "记录",
        "段已在辅助检查且原文确含取值", "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "静默_去分板块救回", o["　其中去分板块抽到"], "记录",
        "段已入辅助检查者中被去分板块抽到的条数", "附表S6", kind="既有记录", source=SRC_SL)
    add("ISL", "静默_段未移入", o["　段未移入"], "记录",
        "一条段仍留现病史、一条只写“已加做免疫组化”", "附表S6", kind="既有记录", source=SRC_SL)
    for stx in sl["同文本对照"]:
        pre = "同文本_%s" % ("ER括号" if "ER" in stx["表述"] else "luminalB")
        add("ISL", pre + "_记录数", stx["记录数"], "记录", stx["表述"], "附表S6",
            kind="既有记录", source=SRC_SL)
        add("ISL", pre + "_基线命中", stx["基线命中"], "记录", "同一表述下基线抽到", "附表S6",
            kind="既有记录", source=SRC_SL)
        add("ISL", pre + "_去分板块命中", stx["去分板块命中"], "记录",
            "同一表述下去分板块抽到", "附表S6", kind="既有记录", source=SRC_SL)

    # 5.4 子集 IHC 靶集与召回（反常识核查）
    add("IHR", "IHC靶集记录数", 82, "记录", "子集内原文含 IHC 表达的记录", "表7",
        kind="既有记录", source="IHC记录级召回_反常识结果核查_20261108.md")
    add("IHR", "工作流靶集检出", 69, "记录", "panels 非空即算检出", "表7",
        kind="既有记录", source="同上")
    add("IHR", "工作流靶集召回", 100.0 * 69 / 82, "%", "69/82", "表7",
        kind="既有记录", source="同上")
    add("IHR", "L2 靶集检出", 79, "记录", "同上", "表7", kind="既有记录", source="同上")
    add("IHR", "L2 靶集召回", 100.0 * 79 / 82, "%", "79/82", "表7", kind="既有记录", source="同上")
    add("IHR", "靶集剔除无结果后", 79, "记录", "剔除原文明确「未做/已加做看结果/仅特殊染色」3 条",
        "表7", kind="既有记录", source="同上")
    add("IHR", "工作流有效召回", 100.0 * 69 / 79, "%", "69/79", "表7",
        kind="既有记录", source="同上")
    add("IHR", "L2 有效召回", 100.0 * 78 / 79, "%", "78/79", "表7", kind="既有记录", source="同上")
    # 归因改为从 ihc_subset_layer.json 逐条判定读出（原为人工核查的固定值，
    _why = sl["静默归因"]
    add("IHR", "工作流漏抽归因_无落点", _why["丙_有声明无落点"], "记录",
        "有拒收说明、指向的落点在契约中不存在", "表6", kind="既有记录", source=SRC_SL)
    add("IHR", "工作流漏抽归因_错位落库", _why["戊_错位落库"], "记录",
        "取值写进现病史诊断明细/既往史条目，带原文锚但不在免疫组化宿主下", "表6",
        kind="既有记录", source=SRC_SL)
    add("IHR", "工作流漏抽归因_记为报告", _why["乙_记为报告条目"], "记录", "写成检查报告条目", "表6",
        kind="既有记录", source=SRC_SL)
    add("IHR", "工作流漏抽归因_真漏抽", _why["丁_真漏抽"], "记录",
        "除 raw 外产物内无任何取值痕迹", "表6", kind="既有记录", source=SRC_SL)
    add("IHR", "工作流漏抽归因_原文无结果", _why["甲_原文无结果"], "记录", "本不该计入分母", "表6",
        kind="既有记录", source=SRC_SL)
    add("IHR", "原始判据两组差", 100.0 * 79 / 82 - 100.0 * 69 / 82, "pp",
        "L2 - 工作流（原始判据）", "表6", kind="既有记录", source="同上",
        formula="79/82-69/82", decimals=1)
    add("IHR", "剔除后两组差", 100.0 * 78 / 79 - 100.0 * 69 / 79, "pp",
        "L2 - 工作流（剔除无结果 3 条后）", "表6", kind="既有记录", source="同上",
        formula="78/79-69/79", decimals=1)
    add("IHR", "L2_漏抽率", 100.0 - 100.0 * 79 / 82, "%", "1 - 靶集召回", "§3",
        kind="既有记录", source="同上", formula="1-79/82")

    # 5.5 消融 IHC 召回
    add("ABL", "L1_IHC召回", 18.3, "%", "L1 在 82 条靶集上的检出率", "§3",
        kind="既有记录", source="IHC与溯源重算.json")
    add("ABL", "L1_IHC漏抽", 81.7, "%", "1-召回", "§3", kind="既有记录", source="同上")
    add("ABL", "L1_早期独立复现漏抽", 81.1, "%", "实验室早期独立复现结果", "§3",
        kind="既有记录", source="研究结果与方法全面总结_20261106.md §3.2")

    # 5.6 准确性（校准后量具）
    ac = json.load(io.open(QA + "/IHC准确性_校准后.json", encoding="utf-8"))
    for tag in ("完整工作流", "L1_无标准化", "L2_无分板块"):
        g = ac["分组"][tag]
        short = {"完整工作流": "工作流", "L1_无标准化": "L1", "L2_无分板块": "L2"}[tag]
        add("ACC", "%s_名称合法率" % short, g["A_名称合法率"], "%",
            "canon_name 归入 marker/ish 的占比", "§3" if short == "L2" else "未引用",
            kind="既有记录", source="probe_accuracy_calibrated.py → IHC准确性_校准后.json")
        add("ACC", "%s_值域合规率" % short, g["C_值域合规率"], "%",
            "status/percent/score/intensity 均符规格", "§3" if short in ("工作流", "L2") else "未引用",
            kind="既有记录", source="同上")
        add("ACC", "%s_标志物条目数" % short, g["marker总数"], "条目", "panels[].markers[]", "§3"
            if short in ("工作流", "L2") else "未引用", kind="既有记录", source="同上")
        add("ACC", "%s_非标志物条目" % short, sum(g["非标志物桶"].values()), "条目",
            "canon_name 未归入 marker/ish 的条目（unmapped/non_marker/special）",
            "§3" if short in ("工作流", "L2") else "未引用", kind="既有记录", source="同上")
    add("ACC", "L2_值域不合规条目", 140, "条目", "L2 中 status/percent/score/intensity 越规格",
        "§3", kind="既有记录", source="同上")

    # 5.7 六模型（原始输出未归档）
    six = [("claude-sonnet-4-6", 100.0, 68, 32), ("deepseek-v4-flash", 100.0, 65, 35),
           ("deepseek-v4-pro", 100.0, 64, 36), ("glm-5.3", 65.3, None, None),
           ("glm-5.3-flash", 1.0, None, None), ("qwen3.8-flash", 4.0, None, None)]
    for nm, rate, p_, f_ in six:
        add("SIXM", "%s_板块有效产出率" % nm, rate, "%", "八板块中通过校验的板块占比", "表5",
            kind="既有记录", source="研究方案_实验设计_v3_四段式.md §2.2",
            note="原始逐条输出未归档，仅存聚合结果")
        if p_ is not None:
            add("SIXM", "%s_终止校验通过" % nm, p_, "记录", "校验闸门通过条数（不经归一化）", "表5",
                kind="既有记录", source="同上")
            add("SIXM", "%s_终止校验未通过" % nm, f_, "记录", "同上", "表5",
                kind="既有记录", source="同上")

    # 5.8 标准化过程（段级）
    add("STD", "段级 anchor 命中率均值", 99.41, "%", "全量运行各周期均值", "§4",
        kind="既有记录", source="交付终态总结_20261107.md")
    add("STD", "段级 anchor 最差周期", 91.4, "%", "单周期最低值", "§4",
        kind="既有记录", source="同上")
    add("STD", "模型自填内容占比", 0.166, "%", "自填字量/原文字量", "§4",
        kind="既有记录", source="同上")

    # 5.9 子集偏差
    add("BIAS", "子集原文含IHC记录占比", 82.0, "%",
        "100 条中原文含 IHC 表达的记录占比；等于靶集 82 条", "方法§4",
        kind="既有记录", source="同上", formula="82/100")
    add("BIAS", "子集长文本占比", 11.0, "%", "总长≥1000 字占比", "方法§4",
        kind="既有记录", source="同上")
    add("BIAS", "全量原文含IHC记录占比",
        100.0 * x["总计数"]["靶集记录"] / 80718, "%",
        "原文含 IHC 表达记录 / 全库记录；与子集 82% 同侧可比", "方法§4",
        formula="%d/80718" % x["总计数"]["靶集记录"])

    # 5.10 失效归因（100 条人工归口）
    add("ATT", "否定表达未显式化条数", 34, "条", "100 条中该类缺陷条数", "§3",
        kind="既有记录", source="RQ2错误分析_四类子任务失败_20261107.md")
    add("ATT", "否定表达占比", 34.0, "%", "34/100", "§3", kind="既有记录", source="同上")
    add("ATT", "其中无处可标条数", 33, "条", "契约无 neg 位", "§3", kind="既有记录", source="同上")
    add("ATT", "neg 位已标注症状条数", 249, "条", "present_illness.symptoms 全量标注数", "讨论§1",
        kind="既有记录", source="同上")
    add("ATT", "neg 位极性翻转条数", 1, "条", "有位置仍标反", "讨论§1", kind="既有记录", source="同上")
    add("ATT", "neg 位失效率", 100.0 * 1 / 249, "%", "1/249", "讨论§1", kind="既有记录", source="同上")
    add("ATT", "既往治疗归因错位条数", 2, "条", "100 条中该类缺陷", "§3",
        kind="既有记录", source="同上")
    for nm, v in (("主诉", 111), ("体检乳房", 123), ("淋巴结", 18),
                  ("一般情况", 31), ("既往史并存病", 10), ("既往治疗与手术", 360),
                  ("用药", 400)):
        add("ATT", "无neg位_%s" % nm, v, "条", "该位置阴性表述无处可标", "附表S7",
            kind="既有记录", source="同上", note="用药为「400 余条」的保守取值")
    add("ATT", "无neg位_七类合计", 1053, "条",
        "主诉 111 + 体检乳房 123 + 淋巴结 18 + 一般情况 31 + 既往史并存病 10 + "
        "既往治疗与手术 360 + 用药 400", "附表S7", kind="既有记录", source="同上",
        formula="111+123+18+31+10+360+400")

# ======================================================================
# ======================================================================
REV_JSON = QA + "/审核结果_20261113.json"
RS_JSON = QA + "/补取证结果_20261113.json"
RA_JSON = QA + "/错判归因_20261113.json"
RH_JSON = QA + "/同源核验_20261113.json"
RT_JSON = QA + "/修复回测_20261113.json"
REV_LAB = {"K·医嘱七桶": "K_医嘱七桶", "C·免疫组化判读": "C_免疫组化",
           "S·TNM 主期": "S_TNM主期", "M·分子亚型": "M_分子亚型",
           "V·就诊性质+良恶性": "V_就诊性质"}

def scan_review():
    if not (os.path.exists(REV_JSON) and os.path.exists(RS_JSON)):
        return
    R = json.load(io.open(REV_JSON, encoding="utf-8"))
    S2 = json.load(io.open(RS_JSON, encoding="utf-8"))
    SRC_R = "qa_reports/审核结果_20261113.json"

    add("REV", "审核总行数",
        sum(v["行数"] for k, v in R["字段"].items() if "富集" not in k), "行",
        "五字段主层行数之和（表 8 的行数合计，富集层不计入）", "结果§4",
        kind="重算", source=SRC_R)
    add("REV", "含定向富集层审核行数", R["_来源"]["行数"], "行",
        "主层 + 定向富集层；富集层只作缺陷确认，不与主层相加算率", "结果§4",
        kind="既有记录", source=SRC_R)
    add("REV", "受审字段数", len([k for k in R["字段"] if "富集" not in k]), "个",
        "K／C／S／M／V；富集层不并入主层率", "结果§4",
        kind="既有记录", source=SRC_R)

    # 唯一记录数：五字段各抽各的记录、互不共用，合计不等于任一字段的记录数
    AK_P = QA + "/审核包_20261113/_答案键.json"
    if os.path.exists(AK_P):
        _ak = json.load(io.open(AK_P, encoding="utf-8"))["items"]
        _main = [x for x in _ak if x.get("层") == "主层"]
        add("REV", "人审覆盖唯一记录数", len({x["记录ID"] for x in _main}), "记录",
            "五字段按记录独立随机、字段间不共用记录，故唯一记录数大于任一字段的记录数",
            "结果§4", kind="重算", source="qa_reports/审核包_20261113/_答案键.json")
        add("REV", "定向富集层行数",
            sum(v["行数"] for k, v in R["字段"].items() if "富集" in k), "行",
            "报告类医嘱 25＋HER2 带 ISH 20＋Ki-67 带区间 15；只作缺陷确认，不并入率估计",
            "方法§7", kind="重算", source=SRC_R)

    SP_P = QA + "/审核包_20261113/_抽样参数.json"
    if os.path.exists(SP_P):
        SP = json.load(io.open(SP_P, encoding="utf-8"))
        it_seed = add("REV", "审核包抽样种子", SP["种子"], "", "预注册参数，抽定后不再变动",
                      "方法§7", kind="既有记录",
                      source="qa_reports/审核包_20261113/_抽样参数.json")
        if it_seed:                    # 种子是编号不是数量，不套千分位
            it_seed["显示"] = str(SP["种子"])
        for k, v in SP["样本量_记录"].items():
            add("REV", "审核抽样记录数_" + k, v, "记录",
                "按记录独立随机、记录内条目全查（不二次抽样），故行数由记录自身条目数决定",
                "方法§7", kind="既有记录",
                source="qa_reports/审核包_20261113/_抽样参数.json")
        for k, v in SP["富集层_记录"].items():
            add("REV", "审核富集层记录数_" + k, v, "记录",
                "定向补样，只作缺陷确认、不并入率估计", "方法§5",
                kind="既有记录", source="qa_reports/审核包_20261113/_抽样参数.json")

    for k, v in R["字段"].items():
        if "富集" in k:          # 定向补样层不并入主层率（规程 §4.3）
            continue
        lb = REV_LAB.get(k, k)
        n, can, ag, wr = v["行数"], v["双方可判"], v["一致"], v["错判"]
        un = v["至少一方判不了"]
        add("REV", lb + "_审核行数", n, "行", "该字段主层审核行数", "结果§4",
            kind="既有记录", source=SRC_R)
        add("REV", lb + "_审核记录数", v["记录数"], "记录", "该字段审核覆盖的记录数",
            "结果§4", kind="既有记录", source=SRC_R)
        add("REV", lb + "_双方可判行数", can, "行", "两位专家均给出明确判定",
            "结果§4", kind="既有记录", source=SRC_R)
        add("REV", lb + "_一致行数", ag, "行", "两位专家判定相同（错判率的分母）",
            "结果§4", kind="既有记录", source=SRC_R)
        add("REV", lb + "_一致率", 100.0 * ag / can if can else None, "%",
            "一致行/双方可判行（判不了的行不计入一致率分母）", "结果§4",
            kind="既有记录", source=SRC_R,
            formula="%d/%d" % (ag, can), decimals=2)
        add("REV", lb + "_kappa", v["kappa"], "", "双专家 Cohen κ（规则可执行性，非正确性）",
            "结果§4", kind="既有记录", source=SRC_R, decimals=4)
        add("REV", lb + "_至少一方判不了", un, "行", "任一位专家填「无法判定／不可判」",
            "结果§4", kind="既有记录", source=SRC_R)
        add("REV", lb + "_层外占比", 100.0 * un / n if n else None, "%",
            "判不了行/审核行，报符合率时必须同时报", "结果§4", kind="既有记录",
            source=SRC_R, formula="%d/%d" % (un, n), decimals=2)
        add("REV", lb + "_错判行数", wr, "行", "专家一致但与产物不符（未归因前）",
            "结果§4", kind="既有记录", source=SRC_R)
        add("REV", lb + "_错判率", 100.0 * wr / ag if ag else None, "%",
            "错判行/一致行", "结果§4", kind="既有记录", source=SRC_R,
            formula="%d/%d" % (wr, ag), decimals=2)
        _u = len((R.get("_明细", {}).get(k) or {}).get("判不了但产物给值") or [])
        if _u:
            add("REV", lb + "_判不了但产物给值", _u, "行",
                "专家判不了而产物已给值（最严重错误形态的候选）", "结果§4",
                kind="既有记录", source=SRC_R)

    # 补取证：把「判不了」改写为「可判」后重算
    ev = S2["一_证据效应"]
    add("REV", "补取证行数", ev["总行数"], "行", "首审判不了或被替换的行", "结果§4",
        kind="既有记录", source="qa_reports/补取证结果_20261113.json")
    add("REV", "补证审判定改变行数", ev["两次判定有变化"], "行", "首审 vs 补审判定不同",
        "结果§4", kind="既有记录", source="同上")
    add("REV", "补审由判不了转为可判", ev["首次判不了、补审判出了"], "行",
        "证明首审的「未写／不可判」是取证造成的假信号", "结果§4",
        kind="既有记录", source="同上")
    add("REV", "两次都判不了行数", ev["两次都判不了"], "行", "补取证后仍无判据",
        "结果§4", kind="既有记录", source="同上")
    for k, v in S2["二_最终率"]["补审后"].items():
        lb = REV_LAB.get(k, k)
        add("REV", lb + "_补审替换行", v["补审替换行"], "行", "补审覆盖首审的行数",
            "结果§4", kind="既有记录", source="同上")
        add("REV", lb + "_补审后一致行数", v["一致"], "行", "并回主层后的一致行",
            "结果§4", kind="既有记录", source="同上")
        add("REV", lb + "_补审后错判行数", v["错判"], "行", "并回主层后的错判行",
            "结果§4", kind="既有记录", source="同上")
        add("REV", lb + "_补审后错判率", 100.0 * v["错判率"], "%", "错判/一致",
            "结果§4", kind="既有记录", source="同上",
            formula="%d/%d" % (v["错判"], v["一致"]), decimals=2)
        ci = v.get("错判率CI") or []
        if len(ci) == 2:
            add("REV", lb + "_补审后错判率CI下限", 100.0 * ci[0], "%", "Wilson 95% 下限",
                "结果§4", kind="既有记录", source="同上", decimals=2)
            add("REV", lb + "_补审后错判率CI上限", 100.0 * ci[1], "%", "Wilson 95% 上限",
                "结果§4", kind="既有记录", source="同上", decimals=2)

    # 错判归因：扣异源、口径等价、取证不足后的真错率
    if os.path.exists(RA_JSON):
        A = json.load(io.open(RA_JSON, encoding="utf-8"))
        for k, v in A["按字段"].items():
            lb = REV_LAB.get(k, k)
            _sum = (v["产物缺陷"] + v["异源"] + v["取证不足"]
                    + v["口径等价"] + v["口径边界"])
            if _sum != v["错判行"]:
                REV_ISSUES.append("%s 归因分项之和 %d ≠ 错判行 %d"
                                  % (k, _sum, v["错判行"]))
            _hp = v["异源处置"].get("补抽", 0) + v["异源处置"].get("改选取", 0)
            if _hp != v["异源"]:
                REV_ISSUES.append("%s 异源处置 %d ≠ 异源行 %d" % (k, _hp, v["异源"]))
            add("REV", lb + "_真错行数", v["产物缺陷"], "行",
                "值错 + ISH未采纳 + 判据缺失（扣除异源/取证不足/口径后）", "结果§4",
                kind="既有记录", source="qa_reports/错判归因_20261113.json")
            add("REV", lb + "_真错率", 100.0 * v["真实错判率"], "%",
                "真错行/一致行", "结果§4", kind="既有记录",
                source="同上", formula="%d/%d" % (v["产物缺陷"], v["一致行"]), decimals=2)
            for tag, key in (("异源", "异源"), ("口径等价", "口径等价"), ("口径边界", "口径边界")):
                add("REV", lb + "_" + tag + "行数", v[key], "行",
                    {"异源": "产物与取证片段各看一份报告，不计对错",
                     "口径等价": "HR+/HER2+ 两种叫法皆判对",
                     "口径边界": "原文写区间、产物取一端，两者皆判对"}[tag],
                    "结果§4", kind="既有记录", source="同上")
        for k, n in A["归因分布"].items():
            add("REV", "归因_" + k, n, "行", "22 行错判的归因分布", "结果§4",
                kind="既有记录", source="同上")
        _p = sum(v["异源处置"].get("补抽", 0) for v in A["按字段"].values())
        _q = sum(v["异源处置"].get("改选取", 0) for v in A["按字段"].values())
        add("REV", "异源_需补抽行数", _p, "行", "专家看的那份报告未入库", "结果§4",
            kind="既有记录", source="同上")
        add("REV", "异源_需改选取行数", _q, "行", "两份都在库、审核包取错份", "结果§4",
            kind="既有记录", source="同上")
        _pr = {r for v in A["按字段"].values() for r in v.get("漏抽记录") or []}
        _qr = {r for v in A["按字段"].values() for r in v.get("改选取记录") or []}
        add("REV", "异源_需补抽记录数", len(_pr), "记录", "这些记录的某份报告未入库",
            "结果§4", kind="既有记录", source="同上")
        add("REV", "异源_需改选取记录数", len(_qr), "记录", "这些记录两份都在库、只是取错份",
            "结果§4", kind="既有记录", source="同上")
        add("REV", "多份报告未全量入库记录数", len(A.get("记录级") or []), "记录",
            "原文有多块而入库 panel 少，某块无对应 panel", "结果§4",
            kind="既有记录", source="同上")
    if os.path.exists(RH_JSON):
        Hh = json.load(io.open(RH_JSON, encoding="utf-8"))
        add("REV", "同源核验交叉差分行数", len(Hh.get("交叉差分") or []), "行",
            "两路判定不同的行，须逐行解释（本批为片段自身混读）", "结果§4",
            kind="既有记录", source="qa_reports/同源核验_20261113.json")

    # 修复回测：判据替换后，同一批审核行与专家判定重比（不重出题、不重请专家）
    if os.path.exists(RT_JSON):
        T = json.load(io.open(RT_JSON, encoding="utf-8"))
        SRC_T = "qa_reports/修复回测_20261113.json"
        add("REV", "修复回测字段数", len(T["结果"]), "个", "K／C／M／V；S 无错判不参与",
            "结果§4", kind="既有记录", source=SRC_T)
        for k, v in T["结果"].items():
            lb = REV_LAB.get(k, k) + "_修复回测"
            add("REV", lb + "_一致行", v["一致行"], "行", "回测分母（专家一致行）",
                "结果§4", kind="既有记录", source=SRC_T)
            add("REV", lb + "_改判行", v["改判行"], "行", "新判据给出与原产物不同的值",
                "结果§4", kind="既有记录", source=SRC_T)
            add("REV", lb + "_修前错判行", v["修前错判"], "行", "替换判据前的错判行",
                "结果§4", kind="既有记录", source=SRC_T)
            add("REV", lb + "_修前错判率", 100.0 * v["修前错判率"], "%", "修前错判/一致行",
                "结果§4", kind="既有记录", source=SRC_T,
                formula="%d/%d" % (v["修前错判"], v["一致行"]), decimals=2)
            add("REV", lb + "_修后错判行", v["修后错判"], "行", "替换判据后的错判行",
                "结果§4", kind="既有记录", source=SRC_T)
            add("REV", lb + "_修后错判率", 100.0 * v["修后错判率"], "%", "修后错判/一致行",
                "结果§4", kind="既有记录", source=SRC_T,
                formula="%d/%d" % (v["修后错判"], v["一致行"]), decimals=2)
            add("REV", lb + "_被修好行数", v["被修好"], "行", "修前错、修后对",
                "结果§4", kind="既有记录", source=SRC_T)
            add("REV", lb + "_致新错行数", v["修复致新错"], "行", "修前对、修后错",
                "结果§4", kind="既有记录", source=SRC_T)
            if v["待补审行"]:
                add("REV", lb + "_待补审行数", v["待补审行"], "行",
                    "专家判「不确定」而新产物给结论，须补取证重审", "结果§4",
                    kind="既有记录", source=SRC_T)
            if v["修前错判"] - v["被修好"] + v["修复致新错"] != v["修后错判"]:
                REV_ISSUES.append("%s 修复回测不闭合：%d-%d+%d ≠ %d"
                                  % (k, v["修前错判"], v["被修好"], v["修复致新错"],
                                     v["修后错判"]))

        # 表 8 末行：一致行与错判行跨字段相加（S 无错判，取原值）
        _ag_tot = sum(v["一致"] for k, v in R["字段"].items() if "富集" not in k)
        _n_tot = sum(v["行数"] for k, v in R["字段"].items() if "富集" not in k)
        _wr_tot = sum(T["结果"].get(k, {}).get("修后错判", v["错判"])
                      for k, v in R["字段"].items() if "富集" not in k)
        add("REV", "一致行合计", _ag_tot, "行", "五字段一致行之和，即合计错判率的分母",
            "结果§4", kind="重算", source=SRC_T)
        add("REV", "一致行以外行数", _n_tot - _ag_tot, "行",
            "判定不一致或判不了的行，单列而不进错判率分母", "结果§4",
            kind="重算", source=SRC_T)
        add("REV", "合计错判率", 100.0 * _wr_tot / _ag_tot if _ag_tot else None, "%",
            "五字段错判行之和/一致行之和（不作达标声明，仅作汇总）", "结果§4",
            kind="重算", source=SRC_T,
            formula="%d/%d" % (_wr_tot, _ag_tot), decimals=2)

# ======================================================================
# 六、渲染
# ======================================================================
GROUPS = [
    ("REF", "0. 数据基准", "全文"),
    ("PAR", "0b. 方法参数（非结果数字）", "方法"),
    ("SRC", "1. 源表与语料规模", "方法§2"),
    ("T1", "2. 叙述字段形态（表 S1）与子集分层总体", "方法§2–§4 / 附表 S1"),
    ("PRD", "3. 全量产物规模", "§4"),
    ("GAT", "4. 质量闸门（全量）", "§4"),
    ("COV", "5. 下游字段可用性（全量）", "§4"),
    ("IHC", "6. IHC 四件套结论（全量，call 口径）", "§4"),
    ("TNM", "7. TNM 落地（全量）", "§4"),
    ("DAT", "8. 检查与检验日期（全量）", "§4 / §6"),
    ("MOL", "9. 分子分型（全量）", "§4"),
    ("QCS", "10. 标准化层条目级隔离", "§4"),
    ("SUB", "11. 评测子集本身", "方法§4"),
    ("REV", "11b. 临床正确性人工盲审（金标准；论文只引「修后」终值，修前与回测为审计痕迹）", "结果§4"),
    ("SIX", "12. 结构与完备性对照（100 条，表 4）", "§1 / §3"),
    ("ITM", "13. 条目级对齐（100 条，表 4）", "§1"),
    ("FID", "13b. 保真性可自动回指范围（100 条）", "方法§5"),
    ("XFD", "14. IHC 跨字段分层（全量 19,051 / 16,269）", "附表S6"),
    ("RJR", "14b. IHC 归位后拒收（全量静默组 3,252 条）", "附表S6"),
    ("ISL", "14c. IHC 分层与静默（100 条子集，§3 机制证据）", "§3"),
    ("IHR", "15. IHC 记录级召回与归因（100 条，表 6）", "§3"),
    ("ABL", "16. 消融：IHC 召回（100 条）", "§3"),
    ("ACC", "17. 标志物准确性与规范性（100 条）", "§3"),
    ("SIXM", "18. 六模型迁移性（100 条，止于校验闸门）", "§2 / 表 5"),
    ("STD", "19. 标准化层过程指标（全量）", "§4"),
    ("BIAS", "20. 子集偏差声明", "方法§4"),
    ("ATT", "21. 失效归因归口（100 条）", "讨论§1"),
]

def render_md(meta):
    L = []
    w = L.append
    w("# 结果清单（唯一数字登记册）")
    w("")
    w("> 生成时间：%s" % meta["生成时间"])
    w("> 生成脚本：`研究分析/10_analysis/build_result_ledger.py`（重跑即刷新，禁止手改本文件）")
    w("> 数据基准：`assembled_v7_final.tar`（%s B，md5 `%s`）" % (meta["字节"], meta["md5"]))
    w("> 条目总数：**%d**（重算 %d / 既有记录 %d / 方法参数 %d）" %
      (meta["总数"], meta["重算"], meta["既有"], meta["参数"]))
    w("")
    w("## 用法")
    w("")
    w("1. 写作时只从本册取数，不再从任何 JSON 或旧文档抄。")
    w("2. 引用前先看「量词」：记录 / 患者 / 条目 / 指标 / 处 不可互换。")
    w("3. 「类型」为 **重算** 的，每次运行脚本都会与数据重算核对；为 **既有记录** 的，"
      "源头需重跑模型或人工归因，脚本只登记不重算，来源列写明文件。")
    w("4. 稿中出现的任何数字必须能在本册中找到；找不到即不许写进论文。")
    w("5. 本册取代 `终态数字.json`（全量）、`论文数字封底.json`（子集）作为唯一来源；"
      "那两个文件是过程产物，不再直接引用。")
    w("")
    for code, title, paper in GROUPS:
        rows = [x for x in LEDGER if x["组"] == code]
        if not rows:
            continue
        w("## %s" % title)
        w("")
        w("| 编号 | 指标 | 值 | 单位 | 口径 | 论文 | 类型 |")
        w("|---|---|---|---|---|---|---|")
        for x in rows:
            w("| %s | %s | **%s** | %s | %s | %s | %s |" % (
                x["编号"], x["指标"], x["显示"], x["单位"] or "—",
                x["口径"], x["论文"] or "—",
                {"重算": "重算", "参数": "参数"}.get(x["类型"], "既有")))
        w("")
    return "\n".join(L)

def check_internal():
    issues = []
    idx = {x["指标"]: x["值"] for x in LEDGER}
    N = idx.get("记录数")
    if N:
        p, f = idx.get("pass 记录数"), idx.get("fail 记录数")
        if p is not None and f is not None and p + f != N:
            issues.append("pass+fail=%d ≠ 记录数 %d" % (p + f, N))
        # 闸门分项可重叠、可全空，故「分项之和」须扣重叠、补仅板块缺失才等于 fail。
        _v, _t = idx.get("值域越界记录数"), idx.get("TNM 形态问题记录数")
        _a = idx.get("辅检硬违规记录数")
        _ov, _ob = idx.get("闸门分类重叠记录数"), idx.get("闸门仅板块缺失记录数")
        if None not in (_v, _t, _a, _ov, _ob, f):
            _s = _v + _t + _a - _ov + _ob
            if _s != f:
                issues.append("闸门 越界+TNM+辅检-重叠+仅板块缺失=%d ≠ fail %d" % (_s, f))
        b = idx.get("空日期条目数")
        nm, mk = idx.get("空且无缺失标记条目数"), idx.get("空但带缺失标记条目数")
        if b and nm is not None and mk is not None and nm + mk != b:
            issues.append("日期 无标记+有标记=%d ≠ 空日期 %d" % (nm + mk, b))
        r = idx.get("源表总行数")
        bl = idx.get("尾部全空行")
        d = idx.get("数据行")
        if r and bl is not None and d is not None and bl + d != r:
            issues.append("源表 空行+数据行=%d ≠ 总行 %d" % (bl + d, r))
        rec = idx.get("有标志物记录数")
        if rec and N and rec > N:
            issues.append("有标志物记录数 > 记录数")
        m = idx.get("IHC 标志物条目")
        t = idx.get("四件套含结论条目数")
        if m and t and t > m:
            issues.append("四件套条目 > 标志物条目")

        _u = idx.get("BI-RADS_记录级有单元")
        _f = [idx.get("BI-RADS_原文有分级_已给值"),
              idx.get("BI-RADS_原文有分级_记录无值"),
              idx.get("BI-RADS_原文无分级_却给值"),
              idx.get("BI-RADS_原文无_产物无")]
        if _u and None not in _f:
            if sum(_f) != _u:
                issues.append("BI-RADS 四格 %s=%d ≠ 有单元记录 %d"
                              % ("+".join(str(x) for x in _f), sum(_f), _u))
        _vu = idx.get("BI-RADS_值级回指_单元")
        _vok, _vbad = idx.get("BI-RADS_值级回指_可回指"), idx.get("BI-RADS_值级回指_不可回指")
        if _vu and None not in (_vok, _vbad) and _vok + _vbad != _vu:
            issues.append("值级回指 可+不可=%d ≠ 有值单元 %d" % (_vok + _vbad, _vu))
        _vr, _vs = idx.get("BI-RADS_不可回指_范围写法"), idx.get("BI-RADS_不可回指_单值无出处")
        if _vbad and None not in (_vr, _vs) and _vr + _vs != _vbad:
            issues.append("不可回指 范围+单值=%d ≠ 合计 %d" % (_vr + _vs, _vbad))

        # TNM 记录级：E + C + A + B + D + 无诊断条目 = 全库记录数
        e_ = idx.get("原文无TNM信号_空壳")
        c_ = idx.get("记录级落地数")
        a_ = idx.get("记录级未取值_A原文有pTNM式")
        b_ = idx.get("记录级未取值_B原文仅分期")
        d_ = idx.get("原文无TNM字样却有真值")
        ni = idx.get("无诊断条目记录数")
        if None not in (e_, c_, a_, b_, d_, ni) and N:
            s = e_ + c_ + a_ + b_ + d_ + ni
            if s != N:
                issues.append("TNM 五类+无诊断条目=%d ≠ 记录数 %d" % (s, N))
        si = idx.get("记录级原文有TNM信号")
        if None not in (si, c_, a_, b_) and c_ + a_ + b_ != si:
            issues.append("TNM 有信号 落地+两类未取值=%d ≠ 有信号 %d" % (c_ + a_ + b_, si))
        # TNM 条目级：空_无信号 + 落地 + 两类未取值 = 恶性诊断条目总数
        mi = idx.get("恶性诊断条目总数")
        mn = idx.get("条目级空_原文无信号")
        m7 = idx.get("漏抽_原文有pTNM式却全空")
        m8 = idx.get("漏抽_原文仅分期表述")
        m4 = idx.get("条目级落地数")
        if None not in (mi, mn, m4, m7, m8) and mn + m4 + m7 + m8 != mi:
            issues.append("TNM 条目级四类=%d ≠ 恶性诊断条目 %d" % (mn + m4 + m7 + m8, mi))

    # 审核组：每个登记为 a/b 的比率须等于 100·a/b；归因闭合由 scan_review 记入 REV_ISSUES
    for x in LEDGER:
        if x["组"] != "REV" or x["单位"] != "%":
            continue
        m = re.match(r"^(\d+)/(\d+)$", str(x.get("算式") or ""))
        if m:
            a_, b_ = int(m.group(1)), int(m.group(2))
            if b_ and abs(x["值"] - 100.0 * a_ / b_) > 0.005:
                issues.append("%s %s 比率 %.3f ≠ 100×%d/%d" % (x["编号"], x["指标"], x["值"], a_, b_))
    issues.extend(REV_ISSUES)
    return issues

def check_paper():
    if not os.path.exists(DOC):
        return [], []
    s = io.open(DOC, encoding="utf-8").read()
    # 反引号内是代码／字段名／哈希（如 MD5），不是结果数字；先剔除，否则
    # `871f3d9b…` 会被拆成 871／4201447／892 三个假数字。
    s = re.sub(r"`[^`\n]*`", " ", s)
    # 方括号内是文献引注编号，不是结果数字；先剔除，否则 [19] 会被当成
    # 未登记数字 19，引文一多就误报（引注编号与结果数值无关）。
    s = re.sub(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]", " ", s)
    # 参考文献一节里的年份、DOI 与卷期页不是本文结果数字，整节不参与反查；
    # 否则每加一条引文都要往 KNOWN_OK 里塞 DOI 片段。
    if "## 参考文献" in s:
        i = s.index("## 参考文献")
        j = s.find("\n## ", i + 1)
        s = s[:i] + (s[j:] if j > 0 else "")
    pvals = []
    for x in LEDGER:
        v = x["值"]
        if isinstance(v, (int, float)):
            pvals.append(float(v))
        for n in re.findall(r"\d+", str(x.get("算式") or "")):
            pvals.append(float(n))
    pvals.sort()
    toks = set()
    for m in re.finditer(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+", s):
        toks.add(m.group(0))
    miss = []
    for t in sorted(toks, key=lambda x: -len(x)):
        v = t.replace(",", "")
        try:
            fv = float(v)
        except Exception:
            continue
        # 二分式近邻匹配
        hit = False
        for p in pvals:
            if abs(fv - p) <= 0.051 or (p and abs(fv - p * 100) <= 0.51):
                hit = True
                break
        if not hit and t not in KNOWN_OK:
            miss.append(t)
    return miss, sorted(toks)

def main():
    t0 = time.time()
    print("== 1/4 源表（xlsx 单遍）")
    scan_source()
    print("   %.1fs" % (time.time() - t0))

    print("== 2/4 全量产物（tar 单遍，含闸门）")
    subset_ids = set()
    with io.open(INPUT100, encoding="utf-8") as f:
        for line in f:
            subset_ids.add(json.loads(line)["record_id"])
    S, sub, gate = scan_product(subset_ids)
    print("   %.1fs" % (time.time() - t0))

    print("== 3/4 子集与消融")
    scan_subset_and_ablation(sub)
    scan_qc()
    scan_granularity()
    print("   %.1fs" % (time.time() - t0))

    print("== 4/4 既有记录")
    from_records()
    scan_review()
    print("   %.1fs" % (time.time() - t0))

    issues = check_internal()
    miss, toks = check_paper()

    meta = {"生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "字节": os.path.getsize(TAR), "md5": md5_of(TAR),
            "总数": len(LEDGER),
            "重算": sum(1 for x in LEDGER if x["类型"] == "重算"),
            "既有": sum(1 for x in LEDGER if x["类型"] == "既有记录"),
            "参数": sum(1 for x in LEDGER if x["类型"] == "参数")}
    body = render_md(meta)

    # 附：校验与缺口
    A = [body, "", "## 22. 内部一致性校验", ""]
    if issues:
        for i in issues:
            A.append("- **不一致**：%s" % i)
    else:
        A.append("- 全部通过：分项之和等于总数，比率等于两数之比。")
    A += ["", "## 23. 论文数字反查", ""]
    A.append("稿中出现的不同数字 %d 个；本册未登记 %d 个。" % (len(toks), len(miss)))
    A.append("")
    if miss:
        A.append("未登记项（须逐个确认是阈值、日期、记录号、版本号，或补登记）：")
        A.append("")
        A.append("`" + "` `".join(miss[:80]) + "`")
    else:
        A.append("全部命中。")
    A += ["", "## 24. 已知禁用值", "",
          "| 禁用值 | 曾用于 | 正确值 | 原因 |", "|---|---|---|---|",
          "| 医嘱漏抽 4.2% / 14.5% / 56.2% | §1 完备性 | 0% / 44.6% | 旧实现只认 orders.items，且 max(0,·) 把拆分压平 |",
          "| 诊断漏抽 0% / 55.4% | §1 | 3.3% / 42.1% | 同上 |",
          "| IHC 标志物 7.68 个/条 | 表4 | 7.33 | 按键名泛收，把 excluded_markers 算成产出 |",
          "| 分期落地率 30.8% | §4 | 75.7%（条目级）/ 56.8%（记录级） | 无脚本出处；原值系与其他字段统计量混淆 |",
          "| 有总分期记录 6,966 条 | 附录 | 6,993 条 | 原值是并存病字段（`icd10_comorb`）的统计量，误抄 |",
          "| 表 1 近空 88.3% / 46.4% / 6.8% | 表1 | 88.5% / 46.6% / 7.4% | 原定义无脚本；现按脚注「无／未见／未查／否认／-」占位词表重算，含半角连字符 |",
          "| 辅助检查 >1,000 字 3.6% | 方法§2 | 3.5% | 精确值 3.55%，一位小数应为 3.5 |",
          "| 报告检查日期 75.5% | §4 | 61.9%（条目级）/ 53.6%（记录级） | 旧值在三种口径（条目/记录/报告组）上均不可复现 |",
          "| 字段名 198 / 1,281、叶子 282.8 / 61.6、anchor 16.6、marker 8.6 | 表4 | 见本册 | 过期产物（11-06） |",
          "| 准确率_三选二 / 每记录有效marker | 表4 | 不引用 | 分子混三种对象，定义不可解释 |",
          ""]
    io.open(OUT_MD, "w", encoding="utf-8").write("\n".join(A))

    io.open(OUT_JSON, "w", encoding="utf-8").write(json.dumps(
        {"_meta": meta, "条目": LEDGER, "内部一致性": issues,
         "论文未登记数字": miss}, ensure_ascii=False, indent=1))

    print("\n条目 %d（重算 %d / 既有 %d）" % (meta["总数"], meta["重算"], meta["既有"]))
    print("内部一致性：%s" % ("通过" if not issues else "；".join(issues)))
    print("论文未登记数字 %d 个：%s" % (len(miss), miss[:30]))
    print("->", OUT_MD)

if __name__ == "__main__":
    main()
