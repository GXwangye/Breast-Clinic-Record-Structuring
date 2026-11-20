#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records                                     # noqa: E402

TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
OUT = DATA + "/qa_reports/granularity.json"
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
#   ① BI-RADS + 数字：BI-RADS分类3类；容拼写残缺 BI-RAD / BI-RAS；容罗马数字 Ⅳa
#   ② 「分类N」省略「类」字：分类4a、分类4A。
#   ③ 裸写「N类」：右乳肿物5类、外院超声示右乳肿物5类
# 不含「为N」「是N」这类：只多认出 4 条，却把「为4个结节」之类误纳的风险带上，不值。
RE_BI_NUM = re.compile(
    r"BI[\s\-–—]*RA[D]?S?[^0-9Ⅰ-Ⅻ]{0,16}?(?:[0-6]|Ⅳ|Ⅵ|Ⅴ|Ⅲ|Ⅱ|Ⅰ)", re.I)
RE_BI_CLASS = re.compile(r"分类\s*[0-6]\s*[abcABC]?")
RE_BI_BARE = re.compile(r"(?<![\d.])([0-6])\s*[abcABC]?\s*类")

def birads_in_text(text):
    return bool(RE_BI_NUM.search(text) or RE_BI_CLASS.search(text)
                or RE_BI_BARE.search(text))

BLANK = {"", "none", "null", "无", "未提及", "未指明", "未知", "不适用", "不详", "—", "-"}
N_TOTAL = 80718

# 报告类型闭集（契约 3.5.1）；越界写法单列，不并入"其他"
RT_SET = ("超声", "钼靶", "X线", "CT", "MRI", "核医学", "分子与基因检测",
          "骨密度", "病理", "心电", "造影", "内镜", "其他")
# 影像类（用于分影像/病理两组）
IMG_SET = ("超声", "钼靶", "X线", "CT", "MRI", "核医学", "造影", "骨密度")

TNM_PLACEHOLDER = {"", "none", "未提及", "未指明", "无", "不详"}

def dct(v):
    return v if isinstance(v, dict) else {}

def lst(v):
    return v if isinstance(v, list) else []

def items_of(v):
    if isinstance(v, dict):
        return [x for x in lst(v.get("items")) if isinstance(x, dict)]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []

def s(v):
    return "" if v is None else str(v).strip()

def blank(v):
    return s(v).lower() in BLANK

def norm_stage(v):
    t = s(v).upper().replace("期", "").replace(" ", "")
    if not t or t.lower() in TNM_PLACEHOLDER:
        return None
    m = re.search(r"(Ⅰ|Ⅱ|Ⅲ|Ⅳ|I{1,3}V?|IV)", t)
    if not m:
        m2 = re.search(r"[1-4]", t)
        return {1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ"}.get(int(m2.group())) if m2 else None
    g = m.group()
    return {"I": "Ⅰ", "II": "Ⅱ", "III": "Ⅲ", "IV": "Ⅳ",
            "Ⅰ": "Ⅰ", "Ⅱ": "Ⅱ", "Ⅲ": "Ⅲ", "Ⅳ": "Ⅳ"}.get(g)

GRADE_ORDER = {"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "4a": 5, "4b": 6, "4c": 7, "5": 8, "6": 9}

def grade_val(v):
    t = s(v).upper().replace("类", "").replace("级", "")
    m = re.search(r"(?<![\d.])([0-6])\s*([ABC])?", t)
    if not m:
        return None
    d, sub = m.group(1), (m.group(2) or "").lower()
    g = d + sub if d == "4" and sub else d
    return g if g in GRADE_ORDER else None

def norm_birads(gs, grading, imps):
    cand = []
    if s(gs).upper().replace("-", "").startswith("BIRADS"):
        cand.append(s(grading))
        for im in lst(imps):
            if isinstance(im, dict):
                cand.append(s(im.get("grade_raw")))
                cand.append(s(im.get("grading")))
    for c in cand:
        g = grade_val(c)
        if g:
            return g
    return None

def has_grade(rp):
    gs = s(rp.get("grading_system"))
    if not blank(rp.get("birads")) and grade_val(rp.get("birads")):
        return True
    if not blank(rp.get("grading")) and grade_val(rp.get("grading")):
        return True
    if not blank(rp.get("grade_raw")) and grade_val(rp.get("grade_raw")):
        return True
    for im in lst(rp.get("impressions")):
        if isinstance(im, dict):
            for k in ("grade_raw", "grading", "birads"):
                if not blank(im.get(k)) and grade_val(im.get(k)):
                    return True
    return False

def scan_birads_source():
    if not os.path.exists(SRC):
        return set()
    import openpyxl
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    j = None
    out = set()
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            hdr = [("" if c is None else str(c).strip()) for c in row]
            j = hdr.index("病历ID") if "病历ID" in hdr else None
            continue
        vals = [("" if v is None else str(v)) for v in row]
        if j is None or j >= len(vals):
            continue
        mid = vals[j].strip()
        if mid and birads_in_text(" ".join(vals)):
            out.add(mid)
    return out

def norm_modality(v):
    t = s(v)
    for k in ("手术", "化疗", "内分泌", "靶向", "放疗"):
        if k in t:
            return k
    return "其他" if t and t not in BLANK else None

def norm_subtype(v):
    t = s(v).upper().replace(" ", "")
    if blank(v):
        return None
    if "三阴" in t or "TNBC" in t:
        return "三阴"
    if "LUMINALA" in t.replace("，", "") or ("LUMINAL" in t and "B" not in t):
        return "Luminal A"
    if "LUMINALB" in t or ("LUMINAL" in t and "B" in t):
        return "Luminal B"
    if "HER2" in t or "HER-2" in t:
        return "HER2 阳性"
    return "其他写法"

DX_GROUPS = (
    ("乳腺恶性肿瘤", ("C50",)),
    ("乳腺原位癌", ("D05",)),
    ("乳房良性肿瘤", ("D24",)),
    ("乳房结节与肿块", ("N63",)),
    ("乳腺良性增生与囊肿", ("N60",)),
    ("乳腺增生与乳房肥大", ("N62",)),
    ("乳腺炎与脓肿", ("N61",)),
    ("乳头及其他乳房疾患", ("N64",)),
    ("乳腺恶性肿瘤治疗", ("Z51",)),
    ("远处继发恶性肿瘤", ("C78", "C79")),
)

def dx_group(code, name):
    c = s(code).upper()
    if c.startswith("Z98"):
        return "乳腺手术后状态" if "乳" in s(name) else "其他手术后状态"
    for label, pres in DX_GROUPS:
        if any(c.startswith(x) for x in pres):
            return label
    if not c:
        return "无编码"
    return "非乳腺诊断"

# 就诊性质用分组：诊断名写明「术后」的乳腺诊断按手术后状态计。
POSTOP_NAME = re.compile(r"(术后|手术后|根治术)")

def dx_group_visit(code, name):
    g = dx_group(code, name)
    if g in ("乳腺恶性肿瘤", "乳腺恶性肿瘤治疗", "乳腺原位癌") and POSTOP_NAME.search(s(name)):
        return "乳腺手术后状态"
    return g

def visit_kind(groups):
    if "乳腺手术后状态" in groups:
        return "复诊"
    if groups & {"乳腺恶性肿瘤", "乳腺原位癌", "乳腺恶性肿瘤治疗"}:
        return "新发或在治"
    if groups & BENIGN_SET:
        return "仅良性"
    return "无乳腺诊断"

# 乳腺癌范畴：恶性肿瘤、原位癌、术后状态、在治。远处继发灶（C78/C79）原发部位
# 未逐一溯源，单列不并入。
BREAST_SET = {"乳腺恶性肿瘤", "乳腺原位癌", "乳腺手术后状态", "乳腺恶性肿瘤治疗"}
BENIGN_SET = {"乳房结节与肿块", "乳腺良性增生与囊肿", "乳腺增生与乳房肥大",
              "乳腺炎与脓肿", "乳头及其他乳房疾患", "乳房良性肿瘤"}

def main():
    n = 0
    RAW_BI = scan_birads_source()
    rt = Counter()          # 报告类型
    site = Counter()        # 部位
    lat = Counter()         # 侧别
    gsys = Counter()        # 分级体系
    birads = Counter()      # BI-RADS 分级值
    rt_cn = Counter()       # 报告类型记录数（同一记录只计一次）
    site_cn = Counter()
    n_unit = n_img = n_path = n_date = n_gsys = n_birads = 0
    stage = Counter()
    subtype = Counter()
    top_comorb = Counter()
    modality = Counter()
    n_dx = n_dx_icd = n_dx_side = n_dx_tnm = n_dx_stage = n_dx_sub = 0
    n_comorb = n_comorb_icd = n_rec_comorb = 0
    n_tx = n_breast = n_ln = 0
    n_tx_nomod = 0
    n_tx_bad = 0
    n_dx_bad = 0
    side_val = Counter()
    n_side_explicit = 0
    n_bi_unit = n_bi_val = 0
    n_rec_bi_unit = n_rec_bi_fld = 0
    bi_src = Counter()             # BI-RADS 落地：原文是否写明分级 × 记录是否给出值
    stage_bad = 0
    comorb_neg = 0
    COMORB_NEG = ("健康", "无", "否认", "未见异常", "无特殊", "无异常")
    n_rec_report = n_rec_birads = n_rec_stage = 0
    n_lab = n_lab_res = n_lab_panel = n_lab_empty = 0
    panel = Counter()
    n_ord = 0
    kind = Counter()
    callv = {}
    n_call = 0
    bi_rt = {}                 # 检查类型 -> [单元, 有分级值, 可判]
    dxg = Counter()            # 诊断构成（条目）
    dxg_rec = Counter()        # 诊断构成（记录，组内去重）
    dis = Counter()            # 病名
    n_rec_bc = n_rec_z98 = 0
    bc_status = Counter()      # 共病中的乳腺癌史：既往/现症
    bc_names = Counter()
    n_bc_comorb = 0
    vtype_rec = Counter()          # 就诊类型（病历类型）记录数
    vtype_cross = Counter()        # (病种组, 就诊类型) 记录数
    pt_vtype = {}                  # 患者 -> 就诊类型（首见）
    pt_groups = defaultdict(set)   # 患者 -> 病种组集合（跨就诊并集）
    pt_all = set()
    pt_breast = set()
    n_rec_breast = 0
    rec_mix = Counter()                # 良恶性（记录级四档）
    pt_mix = defaultdict(set)          # 患者 -> 四档集合
    n_postop_rec = 0
    n_new_rec = 0
    pt_postop = set()
    pt_new = set()

    for name, rec in iter_records(TAR):
        n += 1
        pa = dct(rec.get("patient"))
        pid = s(pa.get("登记号")) or name
        vtype = s(pa.get("病历类型")) or "未标"
        vtype_rec[vtype] += 1
        pt_vtype.setdefault(pid, vtype)
        pt_all.add(pid)
        ax = dct(rec.get("auxiliary_exam"))
        reps = lst(ax.get("reports"))
        if reps:
            n_rec_report += 1
        has_bi = has_stage = has_bc = has_z98 = False
        rec_bi_unit = rec_bi_fld = False
        grp_in_rec = set()
        grp_visit = set()
        for rp in reps:
            if not isinstance(rp, dict):
                continue
            n_unit += 1
            t = s(rp.get("report_type"))
            if t in RT_SET:
                rt[t] += 1
                rt_cn[t] = rt_cn.get(t, 0) + 1
                if t in IMG_SET:
                    n_img += 1
                if t == "病理":
                    n_path += 1
            else:
                rt["越界写法"] += 1
            bs = s(rp.get("body_site")) or "未指明"
            site[bs] += 1
            lt = s(rp.get("laterality")) or "未指明"
            lat[lt] += 1
            if lt in ("右", "左", "双"):
                n_side_explicit += 1
            if not blank(rp.get("exam_date")):
                n_date += 1
            gs = s(rp.get("grading_system"))
            if gs and gs.lower() not in BLANK:
                n_gsys += 1
                gsys["BI-RADS" if gs.upper().replace("-", "").startswith("BIRADS")
                     else gs.upper() if "WHO" in gs.upper() else gs] += 1
            is_bi = gs.upper().replace("-", "").startswith("BIRADS")
            if is_bi:
                n_bi_unit += 1
                rec_bi_unit = True
                if has_grade(rp):
                    n_bi_val += 1
                    rec_bi_fld = True
            b = norm_birads(gs, rp.get("birads") or rp.get("grading"), rp.get("impressions"))
            if is_bi:
                e = bi_rt.setdefault(t or "未标类型", [0, 0, 0])
                e[0] += 1
                if has_grade(rp):
                    e[1] += 1
                if b:
                    e[2] += 1
            if b:
                n_birads += 1
                birads[b] += 1
                has_bi = True

        for it in items_of(rec.get("diagnoses")):
            if not isinstance(it, dict):
                n_dx_bad += 1
                continue
            n_dx += 1
            _code = s(it.get("icd_yb_code"))
            _nm = s(it.get("name"))
            _g = dx_group(_code, _nm)
            dxg[_g] += 1
            grp_in_rec.add(_g)
            grp_visit.add(dx_group_visit(_code, _nm))
            if _nm:
                dis[_nm] += 1
            if _code.startswith(("C50", "D05")):
                has_bc = True
            if _code.startswith("Z98"):
                has_z98 = True
            if not blank(it.get("icd_yb_code")):
                n_dx_icd += 1
            if not blank(it.get("side")) or not blank(it.get("laterality")):
                n_dx_side += 1
            sv = s(it.get("side")) or s(it.get("laterality")) or "未填"
            side_val[sv] += 1
            tnm_f = it.get("tnm")
            tnm_list = tnm_f if isinstance(tnm_f, list) else ([tnm_f] if isinstance(tnm_f, dict) else [])
            hit_tnm = False
            for tnm in tnm_list:
                if not isinstance(tnm, dict):
                    continue
                tv = {k: s(tnm.get(k)).lower() for k in ("t", "n", "m", "stage")}
                if any(v not in TNM_PLACEHOLDER for v in tv.values()):
                    hit_tnm = True
                stx = norm_stage(tnm.get("stage"))
                if stx:
                    n_dx_stage += 1
                    stage[stx] += 1
                    has_stage = True
                    break
            if hit_tnm:
                n_dx_tnm += 1
            st_any = any(s(x.get("stage")) not in ("", "None", "未提及", "未指明", "不详")
                         for x in tnm_list if isinstance(x, dict))
            if st_any and not has_stage:
                stage_bad += 1
            sb = norm_subtype(it.get("ihc_hint"))
            if sb:
                n_dx_sub += 1
                subtype[sb] += 1

        for _g in grp_in_rec:
            dxg_rec[_g] += 1
            vtype_cross[(_g, vtype)] += 1
        pt_groups[pid] |= grp_in_rec
        if grp_in_rec & BREAST_SET:
            n_rec_breast += 1
            pt_breast.add(pid)

        _mal = bool(grp_in_rec & BREAST_SET)
        _ben = bool(grp_in_rec & BENIGN_SET)
        _k = ("乳腺癌" if _mal and not _ben else "仅良性" if _ben and not _mal
              else "兼有" if _mal and _ben else "均无")
        rec_mix[_k] += 1
        pt_mix[pid].add(_k)
        _vk = visit_kind(grp_visit)
        if _vk == "复诊":
            n_postop_rec += 1
            pt_postop.add(pid)
        elif _vk == "新发或在治":
            n_new_rec += 1
            pt_new.add(pid)

        if rec_bi_unit:
            n_rec_bi_unit += 1
            if rec_bi_fld:
                n_rec_bi_fld += 1
            _raw = s(rec.get("record_id")) in RAW_BI
            if _raw and rec_bi_fld:
                bi_src["原文有分级_已给值"] += 1
            elif _raw:
                bi_src["原文有分级_记录无值"] += 1
            elif rec_bi_fld:
                bi_src["原文无分级_却给值"] += 1
            else:
                bi_src["两边都无"] += 1
        if has_bc:
            n_rec_bc += 1
        if has_z98:
            n_rec_z98 += 1

        ph = dct(dct(rec.get("past_history")).get("structured"))
        cbs = lst(ph.get("comorbidities"))
        if cbs:
            n_rec_comorb += 1
        for cb in cbs:
            if not isinstance(cb, dict):
                continue
            n_comorb += 1
            if not blank(cb.get("icd10")):
                n_comorb_icd += 1
            nm = s(cb.get("std")) or s(cb.get("name"))
            if nm == "乳腺癌":
                n_bc_comorb += 1
                bc_status[s(cb.get("status")) or "未标"] += 1
                bc_names[s(cb.get("name"))] += 1
            if nm:
                if any(k in nm for k in COMORB_NEG) and len(nm) <= 8:
                    comorb_neg += 1
                else:
                    top_comorb[nm] += 1

        for e in lst(ax.get("labs")):
            if not isinstance(e, dict):
                continue
            n_lab += 1
            rs = [x for x in lst(e.get("results")) if isinstance(x, dict)]
            n_lab_res += len(rs)
            if not rs:
                n_lab_empty += 1
            pn = s(e.get("panel_name")) or s(e.get("panel")) or s(e.get("exam_name"))
            if pn:
                n_lab_panel += 1
                panel[pn] += 1

        for od in lst(dct(rec.get("orders")).get("items")):
            if not isinstance(od, dict):
                continue
            n_ord += 1
            kk = s(od.get("kind"))
            kind[kk if kk else "缺失"] += 1

        for pn_ in lst(dct(ax.get("ihc")).get("panels")):
            if not isinstance(pn_, dict):
                continue
            for m in lst(pn_.get("markers")):
                if not isinstance(m, dict):
                    continue
                nm = s(m.get("marker_name")).upper().replace("-", "")
                cv = s(m.get("call"))
                if cv in ("阳性", "阴性", "不确定"):
                    callv.setdefault(nm, Counter())[cv] += 1
                    n_call += 1

        for t in items_of(rec.get("treatment")):
            if not isinstance(t, dict):
                n_tx_bad += 1
                continue
            n_tx += 1
            md = norm_modality(t.get("modality"))
            if md:
                modality[md] += 1
            else:
                n_tx_nomod += 1

        pe = dct(dct(rec.get("physical_exam")).get("structured"))
        n_breast += len(lst(pe.get("breast")))
        n_ln += len(lst(pe.get("lymph_nodes")))
        if has_bi:
            n_rec_birads += 1
        if has_stage:
            n_rec_stage += 1

    _pt_mix = Counter()
    for _ks in pt_mix.values():
        _m = ("乳腺癌" in _ks) or ("兼有" in _ks)
        _b = ("仅良性" in _ks) or ("兼有" in _ks)
        if _m and _b:
            _pt_mix["兼有"] += 1
        elif _m:
            _pt_mix["乳腺癌"] += 1
        elif _b:
            _pt_mix["仅良性"] += 1
        else:
            _pt_mix["均无"] += 1

    res = {
        "记录": n,
        "影像与病理单元": {
            "报告单元总数": n_unit,
            "影像类单元": n_img,
            "病理类单元": n_path,
            "带检查日期": n_date,
            "有报告的记录": n_rec_report,
            "侧别明确（右/左/双）": n_side_explicit,
            "侧别明确占比": 100.0 * n_side_explicit / n_unit if n_unit else None,
            "按检查类型": dict(rt.most_common()),
            "按部位（前12）": dict(site.most_common(12)),
            "按侧别": dict(lat.most_common()),
        },
        "分级": {
            "带分级体系标注的单元": n_gsys,
            "BI-RADS 单元": n_bi_unit,
            "BI-RADS 单元中有分级值": n_bi_val,
            "BI-RADS 有值占比": 100.0 * n_bi_val / n_bi_unit if n_bi_unit else None,
            "分级体系": dict(gsys.most_common(6)),
            "可判 BI-RADS 分级的单元": n_birads,
            "BI-RADS 分级分布": dict(sorted(birads.items())),
            "可判 BI-RADS 的记录": n_rec_birads,
            "记录级：有 BI-RADS 单元的记录": n_rec_bi_unit,
            "记录级：至少一个单元给出分级值": n_rec_bi_fld,
            "记录级落地核验": dict(bi_src.most_common()),
            "BI-RADS 按检查类型": {k: {"单元": v[0], "有分级值": v[1], "可判": v[2]}
                                  for k, v in sorted(bi_rt.items(), key=lambda x: -x[1][0])},
        },
        "诊断与分期": {
            "非对象条目": n_dx_bad,
            "诊断条目": n_dx,
            "带 ICD 编码": n_dx_icd,
            "带侧别": n_dx_side,
            "侧别明确条目": sum(v for k, v in side_val.items() if k in ("右", "左", "双")),
            "侧别明确占比": 100.0 * sum(v for k, v in side_val.items() if k in ("右", "左", "双")) / n_dx if n_dx else None,
            "带 TNM 任一真值": n_dx_tnm,
            "侧别取值分布": dict(side_val.most_common(6)),
            "带分期": n_dx_stage,
            "分期分布": dict(sorted(stage.items())),
            "分期写法未归类": stage_bad,
            "带分子分型": n_dx_sub,
            "分子分型分布": dict(subtype.most_common()),
            "可判分期的记录": n_rec_stage,
            "有乳腺癌诊断的记录": n_rec_bc,
            "有手术后状态诊断的记录": n_rec_z98,
            "病种构成（条目）": dict(dxg.most_common()),
            "病种构成（记录）": {k: v for k, v in dxg_rec.most_common()},
            "病种构成（患者）": {g: sum(1 for _p, _s in pt_groups.items() if g in _s)
                            for g, _ in dxg.most_common()},
            "病名前10": dict(dis.most_common(10)),
        },
        "就诊类型与患者级": {
            "记录": n,
            "患者": len(pt_all),
            "就诊类型（记录）": dict(vtype_rec.most_common()),
            "就诊类型（患者，首见）": dict(Counter(pt_vtype.values()).most_common()),
            "乳腺癌范畴记录": n_rec_breast,
            "乳腺癌范畴患者": len(pt_breast),
            "良恶性（记录）": dict(rec_mix.most_common()),
            "良恶性（患者）": dict(_pt_mix.most_common()),
            "术后状态码记录": n_postop_rec,
            "术后状态码患者": len(pt_postop),
            "新发或在治记录": n_new_rec,
            "新发或在治患者": len(pt_new),
            "病种组 × 就诊类型（记录）": {
                g: {v: vtype_cross.get((g, v), 0) for v in vtype_rec}
                for g, _ in dxg_rec.most_common()},
        },
        "共病": {
            "共病条目": n_comorb,
            "有共病的记录": n_rec_comorb,
            "带 ICD-10 编码": n_comorb_icd,
            "非共病表述（健康/无）": comorb_neg,
            "真实共病条目": n_comorb - comorb_neg,
            "前10位": dict(top_comorb.most_common(10)),
            "乳腺癌史条目": n_bc_comorb,
            "乳腺癌史状态": dict(bc_status.most_common()),
            "乳腺癌史写法": dict(bc_names.most_common(6)),
        },
        "检验与医嘱": {
            "检验记录条目": n_lab,
            "检验指标合计": n_lab_res,
            "带组合名的记录": n_lab_panel,
            "组合名唯一数": len(panel),
            "组合名前10": dict(panel.most_common(10)),
            "无指标的记录": n_lab_empty,
            "医嘱条目": n_ord,
            "医嘱七桶": dict(kind.most_common()),
        },
        "四件套三态": {
            "合计_全部标志物": n_call,
            "分标志物": {k: dict(v) for k, v in sorted(callv.items())},
            "合计_四件套": sum(sum(c.values()) for k, c in callv.items()
                              if k in ("ER", "PR", "HER2", "KI67")),
        },
        "治疗与查体": {
            "治疗条目": n_tx,
            "治疗方式分布": dict(modality.most_common()),
            "治疗方式为空": n_tx_nomod,
            "非对象条目": n_tx_bad,
            "乳腺查体条目": n_breast,
            "淋巴结触诊条目": n_ln,
        },
    }
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:3000])
    print("\n写入", OUT)

if __name__ == "__main__":
    main()
