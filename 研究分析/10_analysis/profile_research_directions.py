# -*- coding: utf-8 -*-
import collections
import json
import os
import re
import sys
import time

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
import normalize_enum as N  # noqa: E402  闭集唯一来源，避免两处各写一份

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
OUT = DATA + "/中间产物/_qa_out/研究方向可及性画像_20261107.json"

N_REC = 80718
N_PAT = 25081

# ---------------- 影像/病理模态归一（顺序敏感：先病理，再影像） ----------------
MOD_RULES = [
    ("病理", re.compile(r"病理|活检|细胞学|冰冻")),
    ("骨显像", re.compile(r"骨显像|骨扫描|ECT|核医学")),
    ("MRI", re.compile(r"磁共振|MRI|MR[（(]|MR平扫", re.I)),
    ("钼靶", re.compile(r"钼靶|X线摄影|X线|乳腺摄影|MG")),
    ("CT", re.compile(r"CT", re.I)),
    ("超声", re.compile(r"超声|彩超|B超|ultrasound", re.I)),
    ("检验", re.compile(r"血常规|肝功能|肾功能|血脂|电解质|凝血|肿瘤标志物|性激素|心肌酶")),
]

# 关键词定位器（只用于定位，不作结论）
KW = {
    "新辅助": re.compile(r"新辅助|术前化疗|术前新辅助"),
    "前哨淋巴结": re.compile(r"前哨淋巴结|哨兵淋巴结|SLNB"),
    "腋窝清扫": re.compile(r"腋窝淋巴结清扫|腋窝清扫|腋清|ALND"),
    "保乳": re.compile(r"保乳"),
    "重建": re.compile(r"重建|再造"),
    "pCR": re.compile(r"pCR|病理完全缓解"),
    "曲妥珠": re.compile(r"曲妥珠|赫赛汀|帕妥珠|双靶|TDM1|恩美曲妥珠"),
    "内分泌药": re.compile(r"他莫昔芬|托瑞米芬|来曲唑|阿那曲唑|依西美坦|氟维司群|戈舍瑞林|亮丙瑞林"),
    "化疗药": re.compile(r"紫杉|多西他赛|白蛋白紫杉醇|环磷酰胺|表柔比星|吡柔比星|卡铂|顺铂|氟尿嘧啶|长春瑞滨|吉西他滨|卡培他滨"),
    "放疗": re.compile(r"放疗|放射治疗|调强|照射"),
    "骨转移": re.compile(r"骨转移|骨继发"),
    "肺转移": re.compile(r"肺转移|肺继发"),
    "肝转移": re.compile(r"肝转移|肝继发"),
    "脑转移": re.compile(r"脑转移|颅内继发|脑继发"),
    "淋巴结转移": re.compile(r"淋巴结转移|淋巴结继发"),
    "复发": re.compile(r"复发"),
    "BI-RADS": re.compile(r"BI-?RADS", re.I),
    "切缘": re.compile(r"切缘"),
    "脉管癌栓": re.compile(r"脉管癌栓|脉管侵犯|淋巴管侵犯"),
    "组织学分级": re.compile(r"II?I?级|分级"),
}

RE_BIRADS = re.compile(r"BI-?RADS\s*(?:分类)?\s*[:：]?\s*([0-6])\s*类?", re.I)
RE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:cm|CM|厘米)")
RE_LN_POS = re.compile(r"(\d+)\s*/\s*(\d+)\s*枚?")          # 3/15枚
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

def _s(v):
    return "" if v is None else str(v).strip()

def its(rec, key, sub="items"):
    x = rec.get(key)
    if isinstance(x, dict):
        x = x.get(sub)
    return x if isinstance(x, list) else []

def modality_of(*fields):
    blob = " ".join(_s(f) for f in fields if f)
    for name, pat in MOD_RULES:
        if pat.search(blob):
            return name
    return "其他"

def text_pool(rec):
    parts = []
    for k in ("chief_complaint", "present_illness", "past_history"):
        v = rec.get(k)
        if isinstance(v, dict) and isinstance(v.get("raw"), str):
            parts.append(v["raw"])
    for t in its(rec, "treatment"):
        if isinstance(t, dict):
            parts.append(_s(t.get("text")))
    for d in its(rec, "diagnoses"):
        if isinstance(d, dict):
            parts.append(_s(d.get("name")))
    for o in its(rec, "orders"):
        if isinstance(o, dict):
            parts.append(_s(o.get("name")))
    ae = rec.get("auxiliary_exam")
    if isinstance(ae, dict):
        for r in (ae.get("reports") or []):
            if not isinstance(r, dict):
                continue
            for x in (r.get("impressions") or []):
                parts.append(_s(x.get("text")) if isinstance(x, dict) else _s(x))
    return "\n".join(p for p in parts if p)

def main():
    t0 = time.time()
    C = collections.Counter()
    imp_kind = collections.Counter()
    rt_out = collections.Counter()
    bs_out = collections.Counter()
    lab_no_panel = 0
    lab_shape = collections.Counter()   # 键形态：契约要求只有 panel_name+results
    lab_uniq = set()                    # 唯一指标值 =(记录,日期,名,值,单位)
    mod_cnt = collections.Counter()
    mod_rec = collections.Counter()
    birads = collections.Counter()
    kw = collections.Counter()
    # 方向专用
    metastasis = collections.Counter()
    endo_drug = collections.Counter()
    tgt_drug = collections.Counter()
    chemo_drug = collections.Counter()
    ln_report_site = collections.Counter()
    order_dur = collections.Counter()
    lab_items = collections.Counter()
    RE_LIVER = re.compile(r"肝酶|转氨酶|肝功能")
    RE_ABN = re.compile(r"升高|异常|损伤")
    diag_liver = collections.Counter()
    rec_liver = 0
    diag_osteo = collections.Counter()
    rec_osteo = 0
    ihc_marker = collections.Counter()
    subtype = collections.Counter()
    pat_dates = collections.defaultdict(list)
    pat_exam_dates = collections.defaultdict(list)
    date_quality = collections.Counter()
    new_adj = 0
    new_adj_pcr_like = 0
    yptnm_rec = 0
    yptnm_valued = 0
    ypt0n0m0 = 0
    RE_PCR = re.compile(r"未见癌|未见癌细胞|未见残留|未见肿瘤残留|pCR|病理完全缓解")
    pat_img = collections.Counter()
    paired_preop_img_with_path = 0
    rec_with_img_and_path = 0
    rec_with_ln_img_and_path = 0
    ln_pos_pairs = collections.Counter()

    ER_POS = {"阳性", "阳"}
    for fn, rec in iter_records(TAR):
        # ---- 患者/就诊 ----
        p = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
        reg = _s(p.get("登记号"))
        v = rec.get("visit") if isinstance(rec.get("visit"), dict) else {}
        d = _s(v.get("就诊日期"))[:10]
        if reg and RE_DATE.match(d):
            pat_dates[reg].append(d)

        # ---- 影像 / 病理报告 ----
        ae = rec.get("auxiliary_exam") if isinstance(rec.get("auxiliary_exam"), dict) else {}
        img_dates, path_dates, ln_img = [], [], False
        rec_mods = set()
        path_texts = []
        for r in (ae.get("reports") or []):
            if not isinstance(r, dict):
                continue
            m = modality_of(r.get("exam_name"), r.get("report_type"))
            C["reports"] += 1
            mod_cnt[m] += 1
            rec_mods.add(m)
            ed = _s(r.get("exam_date"))[:10]
            # exam_date 质量:有效日期 / 占位词 / 空 / 其他非日期写法
            if RE_DATE.match(ed):
                C["reports_dated"] += 1
                date_quality["有效日期"] += 1
                if reg:
                    pat_exam_dates[reg].append(ed)
            elif not ed:
                date_quality["空"] += 1
            elif re.search(r"未指明|未提及|未标注|未注明|未知|不详|不明|缺失|待补充|无日期", ed):
                date_quality["占位词"] += 1
            else:
                date_quality["相对/异常写法"] += 1
            if m == "病理":
                if RE_DATE.match(ed):
                    path_dates.append(ed)
                for x in (r.get("impressions") or []):
                    path_texts.append(_s(x.get("text")) if isinstance(x, dict) else _s(x))
            elif m in ("超声", "CT", "钼靶", "MRI", "骨显像"):
                if RE_DATE.match(ed):
                    img_dates.append(ed)
            site = _s(r.get("body_site"))
            if re.search(r"腋窝|前哨|淋巴结", site):
                ln_report_site[site] += 1
                if m != "病理":
                    ln_img = True
            _rt = _s(r.get("report_type"))
            if _rt not in N.CLOSED["report_type"]:
                rt_out[_rt[:18]] += 1
            _bs = _s(r.get("body_site"))
            if _bs and _bs not in N.CLOSED["body_site"]:
                bs_out[_bs[:18]] += 1
            if _s(r.get("grading_system")).upper().startswith("BI"):
                C["reports_birads_flag"] += 1
            for x in (r.get("impressions") or []):
                if isinstance(x, dict):
                    imp_kind["dict"] += 1
                    txt = _s(x.get("text"))
                else:
                    imp_kind[type(x).__name__] += 1
                    txt = _s(x)
                if not txt:
                    continue
                C["impressions"] += 1
                mb = RE_BIRADS.search(txt)
                if mb:
                    birads[mb.group(1)] += 1
                if RE_SIZE.search(txt):
                    C["impressions_with_size"] += 1
                mp = RE_LN_POS.search(txt)
                if mp and "淋巴结" in txt:
                    ln_pos_pairs["有枚数比"] += 1

        for m in rec_mods:
            mod_rec[m] += 1

        has_img = bool(img_dates) or any(m in ("超声", "CT", "钼靶", "MRI") for m in rec_mods)
        has_path = "病理" in rec_mods
        if has_img and has_path:
            rec_with_img_and_path += 1
            if img_dates and path_dates and min(img_dates) < max(path_dates):
                paired_preop_img_with_path += 1
        if has_path and ln_img:
            rec_with_ln_img_and_path += 1

        # ---- 检验 ----
        # 契约 3.5.2：labs[] 是「检验组合级」，`panel_name` + `results[]`。
        # 实测此处有三种形态并存（`panel`／扁平 `name+value`／`report_group+section_seq`），
        # 且两套形态各自覆盖不同指标（合并会丢 1.6 万个指标），故**不合并、不去重**，
        # 只把形态分布如实计数，供报告交代口径。条目数 != 份数，报告不得写成「张单」。
        rec_labs = ae.get("labs") or []
        if rec_labs:
            C["rec_with_labs"] += 1
        for l in rec_labs:
            if not isinstance(l, dict):
                continue
            C["labs_entry"] += 1
            ks = set(l.keys())
            if "section_seq" in ks:
                lab_shape["section_seq逐项"] += 1
            elif "panel" in ks:
                lab_shape["panel"] += 1
            elif "panel_name" in ks:
                lab_shape["panel_name"] += 1
            elif "name" in ks and "value" in ks:
                lab_shape["扁平name+value"] += 1
            else:
                lab_shape["无名称键"] += 1
            rs = l.get("results") or []
            if rs:
                C["labs_with_results"] += 1
            elif _s(l.get("value")):
                rs = [l]          # 扁平条目自身就是一个指标
            if not (_s(l.get("panel_name")) or _s(l.get("panel"))
                    or _s(l.get("name")) or l.get("results")):
                lab_no_panel += 1
            for r in rs:
                if isinstance(r, dict):
                    lab_items[_s(r.get("name"))[:18]] += 1
                    if _s(r.get("value")):
                        C["lab_values"] += 1
                        lab_uniq.add((fn, _s(l.get("exam_date")), _s(r.get("name")),
                                      _s(r.get("value")), _s(r.get("unit"))))
                        # 带编码只在「有值的指标」内计数（口径与终态数字.json 一致）
                        if _s(r.get("code")):
                            C["lab_coded"] += 1

        # ---- IHC / 分子分型 ----
        ihc = ae.get("ihc") if isinstance(ae.get("ihc"), dict) else {}
        panels = ihc.get("panels") or []
        if panels:
            C["rec_with_ihc"] += 1
        er = pr = her2 = ki = None
        for pn in panels:
            if not isinstance(pn, dict):
                continue
            for mk in (pn.get("markers") or []):
                if not isinstance(mk, dict):
                    continue
                nm = _s(mk.get("marker_name")).upper().replace("-", "").replace("_", "")
                ihc_marker[nm] += 1
                if nm in ("ER", "PR") and mk.get("call"):
                    if nm == "ER":
                        er = mk.get("call")
                    else:
                        pr = mk.get("call")
                elif nm in ("HER2", "CERBB2") and mk.get("call"):
                    her2 = mk.get("call")
                elif nm in ("KI67", "MIB1"):
                    ki = mk.get("call") or mk.get("percent")
        if er and her2:
            if _s(her2) in ("阳性", "阳"):
                subtype["HER2+"] += 1
            elif _s(er) in ("阴性", "阴"):
                subtype["三阴（ER-/HER2-）"] += 1
            else:
                subtype["Luminal（ER+/HER2-）"] += 1

        # ---- 治疗 / 医嘱 ----
        for t in its(rec, "treatment"):
            if isinstance(t, dict):
                C["treat:" + _s(t.get("modality"))] += 1
        for o in its(rec, "orders"):
            if not isinstance(o, dict):
                continue
            nm = _s(o.get("name"))
            if o.get("duration_days") not in (None, "", 0):
                order_dur["有"] += 1
            else:
                order_dur["无"] += 1
            if KW["内分泌药"].search(nm):
                endo_drug[nm[:16]] += 1
            if KW["曲妥珠"].search(nm):
                tgt_drug[nm[:16]] += 1
            if KW["化疗药"].search(nm):
                chemo_drug[nm[:16]] += 1

        # ---- 诊断 / 转移 / ypTNM ----
        has_y = False
        y_valued = False
        y_pcr = False
        has_liver = False
        has_osteo = False
        for dd in its(rec, "diagnoses"):
            if not isinstance(dd, dict):
                continue
            nm = _s(dd.get("name"))
            if RE_LIVER.search(nm) and RE_ABN.search(nm):
                has_liver = True
                diag_liver["肝功能异常" if "肝功能异常" in nm else "肝酶或转氨酶升高"] += 1
            if "骨质疏松" in nm:
                has_osteo = True
                diag_osteo["骨质疏松" if nm.strip() == "骨质疏松" else "其他骨质疏松写法"] += 1
            for k in ("骨转移", "肺转移", "肝转移", "脑转移", "淋巴结转移"):
                if KW[k].search(nm):
                    metastasis[k + ":诊断"] += 1
            if "继发恶性肿瘤" in nm:
                metastasis["继发恶性肿瘤(诊断名)"] += 1
            tv = dd.get("tnm")
            for o in (tv if isinstance(tv, list) else [tv]):
                if not isinstance(o, dict):
                    continue
                if _s(o.get("prefix_norm") or o.get("prefix")).lower() != "y":
                    continue
                has_y = True
                tt = _s(o.get("t_norm")).upper()
                nn = _s(o.get("n_norm")).upper()
                mm = _s(o.get("m_norm")).upper()
                if tt or nn or mm:
                    y_valued = True
                if tt in ("0", "IS") and nn == "0" and mm in ("", "0"):
                    y_pcr = True
        yptnm_rec += 1 if has_y else 0
        yptnm_valued += 1 if y_valued else 0
        ypt0n0m0 += 1 if y_pcr else 0
        rec_liver += 1 if has_liver else 0
        rec_osteo += 1 if has_osteo else 0

        # ---- 关键词定位 ----
        txt = text_pool(rec)
        for k, pat in KW.items():
            if pat.search(txt):
                kw[k] += 1
        # 新辅助人群的病理反应替代判据：病理结论「未见癌/未见残留/pCR」
        # 注：这是定位级判据，不是疗效结论；「未见癌」在良性病变中同样出现，
        # 故只在明确写「新辅助」的记录内统计。
        if KW["新辅助"].search(txt):
            new_adj += 1
            if any(RE_PCR.search(t) for t in path_texts if t):
                new_adj_pcr_like += 1

    # ---- 患者级纵向 ----
    visits = collections.Counter(len(v) for v in pat_dates.values())
    multi = sum(1 for v in pat_dates.values() if len(v) > 1)
    spans = []
    for v in pat_dates.values():
        if len(v) > 1:
            ds = sorted(v)
            spans.append(int(ds[-1][:4]) - int(ds[0][:4]))
    span_dist = collections.Counter(spans)

    # 检查日期跨度（含既往检查，故可回溯到多年前）——影像纵向能力的真实口径
    ex_spans = []
    all_ex = []
    for v in pat_exam_dates.values():
        all_ex.extend(v)
        if len(v) > 1:
            ds = sorted(v)
            ex_spans.append(int(ds[-1][:4]) - int(ds[0][:4]))
    ex_span_dist = collections.Counter(ex_spans)
    ex_multi = sum(1 for v in pat_exam_dates.values() if len(v) > 1)
    ex_years = collections.Counter(d[:4] for d in all_ex)
    ym_all = sorted({d[:7] for v in pat_dates.values() for d in v})

    result = {
        "generated_by": "研究分析/10_analysis/profile_research_directions.py",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": TAR,
        "denominator": {"records": N_REC, "patients": N_PAT},
        "D1_分子分型": {
            "有IHC记录": C["rec_with_ihc"],
            "亚型可判": dict(subtype.most_common()),
            "亚型可判合计": sum(subtype.values()),
            "标志物出现次数top": dict(ihc_marker.most_common(12)),
        },
        "D2_淋巴结": {
            "影像淋巴结部位报告": dict(ln_report_site.most_common(8)),
            "病理含枚数比结论": ln_pos_pairs.get("有枚数比", 0),
            "影像淋巴结+病理同记录": rec_with_ln_img_and_path,
            "kw_前哨": kw["前哨淋巴结"],
            "kw_腋窝清扫": kw["腋窝清扫"],
        },
        "D3_新辅助": {
            "kw_新辅助": kw["新辅助"],
            "kw_pCR": kw["pCR"],
            "kw_化疗药": kw["化疗药"],
            "新辅助人群中病理反应判据命中": new_adj_pcr_like,
            "ypTNM记录(prefix=y)": yptnm_rec,
            "ypTNM有实质T/N/M值": yptnm_valued,
            "可精确识别的ypT0/isN0M0": ypt0n0m0,
            "说明": "「未见癌/未见残留/pCR」是定位级判据，非疗效结论；仅在明确写新辅助的记录内统计。"
                    "回查2,040条「仅y前缀、T/N/M全空」记录：1,759条原文无三段式TNM串、"
                    "268条原文串前缀非y、仅13条原文含y前缀串——属原文未记录，非漏抽"
                    "（「全空」按当前产物为2,050条=2,409−359；回查样本为2,040条）",
        },
        "D4_手术方式": {
            "kw_保乳": kw["保乳"],
            "kw_重建": kw["重建"],
            "治疗条目_手术": C["treat:手术"],
            "kw_切缘": kw["切缘"],
            "kw_脉管癌栓": kw["脉管癌栓"],
        },
        "D5_复发转移": {
            "kw_骨转移": kw["骨转移"],
            "kw_肺转移": kw["肺转移"],
            "kw_肝转移": kw["肝转移"],
            "kw_脑转移": kw["脑转移"],
            "kw_淋巴结转移": kw["淋巴结转移"],
            "kw_复发": kw["复发"],
            "诊断名": dict(metastasis.most_common(8)),
        },
        "D6_影像": {
            "报告总数": C["reports"],
            "带检查日期": C["reports_dated"],
            "按模态": dict(mod_cnt.most_common(9)),
            "含该模态的记录数": dict(mod_rec.most_common(9)),
            "BI-RADS标记报告": C["reports_birads_flag"],
            "结论条数": C["impressions"],
            "结论结构": dict(imp_kind),
            "结论含BI-RADS分级": dict(sorted(birads.items())),
            "BI-RADS_4_5_6合计": sum(v for k, v in birads.items() if str(k) in ("4", "5", "6")),
            "结论含尺寸(cm)": C["impressions_with_size"],
            "影像+病理同记录": rec_with_img_and_path,
            "其中影像日期早于病理": paired_preop_img_with_path,
        },
        "D7_内分泌": {
            "kw_内分泌药": kw["内分泌药"],
            "药名top": dict(endo_drug.most_common(10)),
            "医嘱有疗程天数": order_dur["有"],
            "医嘱总数": order_dur["有"] + order_dur["无"],
        },
        "D8_靶向": {
            "kw_曲妥珠类": kw["曲妥珠"],
            "药名top": dict(tgt_drug.most_common(10)),
        },
        "D9_不良反应与骨健康": {
            "kw_组织学分级": kw["组织学分级"],
            "诊断条目_肝酶或转氨酶升高": diag_liver["肝酶或转氨酶升高"],
            "诊断条目_肝功能异常": diag_liver["肝功能异常"],
            "诊断记录_肝酶或转氨酶异常": rec_liver,
            "诊断条目_骨质疏松": diag_osteo["骨质疏松"],
            "诊断条目_其他骨质疏松写法": diag_osteo["其他骨质疏松写法"],
            "诊断条目_骨质疏松合计": sum(diag_osteo.values()),
            "诊断条目_肝酶转氨酶合计": sum(diag_liver.values()),
            "诊断记录_骨质疏松": rec_osteo,
            "检验项目top": dict(lab_items.most_common(15)),
            "含检验记录": C["rec_with_labs"],
            "检验组合条目": C["labs_entry"],
            "检验条目有results": C["labs_with_results"],
            "检验值条数": C["lab_values"],
            "检验唯一指标值": len(lab_uniq),
            "检验带编码": C["lab_coded"],
            "检验键形态": dict(lab_shape),
            "说明": "`检验组合条目` 是 labs[] 条目数，不是份数；`含检验记录` 才是记录覆盖面。"
                    "契约 3.5.2 只认 `panel_name+results`，实测 `panel`／扁平 `name+value`／"
                    "`report_group+section_seq` 三种旧形态仍在，且各形态覆盖的指标互不包含"
                    "（合并丢 1.6 万个指标），故按不去重铁律原样保留，仅登记形态分布。",
        },
        "D10_纵向随访": {
            "患者数": len(pat_dates),
            "就诊次数分布": {str(k): v for k, v in sorted(visits.items())[:12]},
            "最大就诊次数": max(visits) if visits else 0,
            "≥2次就诊患者": multi,
            "≥2次占比": round(multi / len(pat_dates), 4) if pat_dates else None,
            "就诊跨年跨度分布": {str(k): v for k, v in sorted(span_dist.items())},
            "就诊年月范围": [ym_all[0], ym_all[-1]] if ym_all else None,
            "有多次检查患者": ex_multi,
            "检查跨年跨度分布": {str(k): v for k, v in sorted(ex_span_dist.items())},
            "检查年份分布": {k: v for k, v in sorted(ex_years.items())},
            "说明": "就诊窗口仅 13 个月（横断面）；但既往检查全部录入，检查日期可回溯到多年前，"
                    "故纵向能力以「检查跨年跨度」为准",
        },
        "structure_issues": {
            "契约合规（归一后实测）": {
                "impressions裸字符串": imp_kind.get("str", 0),
                "impressions对象": imp_kind.get("dict", 0),
                "report_type闭集外": sum(rt_out.values()),
                "body_site闭集外": sum(bs_out.values()),
                "labs无组合名与明细": lab_no_panel,
            },
            "report_type闭集外写法": dict(rt_out.most_common(10)),
            "exam_date质量": dict(date_quality),
        },
        "elapsed_sec": None,
    }
    result["elapsed_sec"] = round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print("\n✓ %s（%.1fs）" % (OUT, result["elapsed_sec"]))

if __name__ == "__main__":
    main()
