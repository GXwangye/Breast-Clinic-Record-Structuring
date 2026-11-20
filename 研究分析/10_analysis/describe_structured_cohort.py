#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys, os, json
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本/00_core"))
from tarstream import iter_records

SRC_TAR = os.path.join(
    ROOT, "publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar")
OUT = os.path.join(DATA, "中间产物/_qa_out/结构化基础描述统计.json")

def norm_marker(name):
    n = (name or "").upper()
    n = n.replace("-", "").replace("_", "").replace("/", "").replace(" ", "")
    return n

HER2_ALIASES = {"HER2", "HER2NEU", "CERBB2", "CERBB2", "CERBB2", "NEU"}

def marker_of(panel, target):
    for m in (panel.get("markers") or []):
        if not isinstance(m, dict):
            continue
        nm = norm_marker(m.get("marker_name"))
        if target == "Ki67":
            if nm in ("KI67", "KI-67", "MIB1", "MIB-1"):
                return m
        elif target == "HER2":
            if nm in HER2_ALIASES:
                return m
        else:
            if nm == target:
                return m
    return None

def call_of(m):
    if not m:
        return None
    c = m.get("call")
    if c in ("阳性", "阴性", "不确定", "不适用"):
        return c
    st = m.get("status")
    if st in ("阳性", "阴性", "不确定", "不适用"):
        return st
    return None

def classify_subtype(panel):
    er = call_of(marker_of(panel, "ER"))
    pr = call_of(marker_of(panel, "PR"))
    her2 = marker_of(panel, "HER2")
    her2_call = call_of(her2)
    ki67 = marker_of(panel, "Ki67")
    # HER2 阳性判据：call=阳性，或 score=3+，或 raw 含 3+
    her2_pos = her2_call == "阳性"
    if her2 and her2.get("score") in ("3+", "3"):
        her2_pos = True
    if her2 and (her2.get("raw_value") or "").strip() in ("3+", "3"):
        her2_pos = True

    # Ki67 高增殖判据
    ki67_high = False
    if ki67:
        band = ki67.get("ki67_band")
        if band == "高增殖":
            ki67_high = True
        pct = ki67.get("percent")
        if isinstance(pct, (int, float)) and pct > 20:
            ki67_high = True

    # 未做 ER/PR 则无法分型
    if er is None and pr is None:
        return None, None

    er_pos = er == "阳性"
    pr_pos = pr == "阳性"
    # ER+/PR 低且 Ki-67 不高的记录被一律判成 Luminal A（人工盲审 4/88 行即此因）。
    pr_pct = (marker_of(panel, "PR") or {}).get("percent")
    pr_low = pr == "阴性" or (isinstance(pr_pct, (int, float)) and pr_pct < 20)

    if her2_pos:
        return "HER2阳性型", "HER2 阳性"
    if er_pos or pr_pos:
        if pr_low:
            return "Luminal B", "HR+/PR 阴性或＜20%"
        if ki67_high:
            return "Luminal B", "HR+/HER2-/Ki67高"
        return "Luminal A", "HR+/HER2-/Ki67低"
    # HR 双阴 + HER2 阴/未测
    if her2_call in ("阴性", "不确定", None):
        return "三阴性", "HR-/HER2-"
    return None, None

def dim_label(v, kind):
    s = str(v or "").strip()
    if not s:
        return "未明确"
    if s.lower() == "x":
        return kind + "x"
    if kind == "T" and s.lower() == "is":
        return "Tis"
    return kind + s.upper()

def stage_label(t):
    s = str(t.get("stage_norm") or "").strip()
    if s:
        return s
    cn = str(t.get("stage_raw_cn") or "").strip()
    if cn:
        return cn
    return "未明确"

def main():
    n_rec = 0
    sex_rec = Counter()          # 记录级性别
    type_rec = Counter()         # 病历类型
    dept_rec = Counter()         # 科室
    patients = {}                # 登记号 -> {sex, 首类型, 记录数}
    # 性别编码映射（源表 HIS 编码：1=男, 2=女）
    SEX_MAP = {"1": "男", "2": "女"}
    # 诊断
    diag_name = Counter()        # 诊断名（原始）
    diag_icd = Counter()         # ICD 码
    diag_side = Counter()        # 侧别
    tnm_t = Counter(); tnm_n = Counter(); tnm_m = Counter(); tnm_stage = Counter()
    stage_sys = Counter()        # 分期体系（契约 §3.6 锚定 AJCC 8th）
    diag_total = 0
    # IHC 四件套（按 marker 条目统计 call）
    ihc_call = defaultdict(Counter)   # target -> call -> count
    ihc_ki67_band = Counter()
    ihc_her2_score = Counter()
    # 分子分型（按 panel，但一记录可能多 panel，取该记录任一可分型的 panel）
    subtype_counter = Counter()
    panel_with_target = 0
    rec_with_subtype = 0

    for rid, rec in iter_records(SRC_TAR):
        n_rec += 1
        # ---- 患者/记录基线 ----
        patient = rec.get("patient") or {}
        # 性别：优先读产物派生字段 性别_text（derive_contract_fields.py，契约 §11.1）；
        # 缺失时才退回本地码表，两级都判不出即「未知」，不猜。
        sex = patient.get("性别_text") or SEX_MAP.get(str(patient.get("性别") or ""), "")
        sex = sex or "未知"
        ctype = patient.get("病历类型") or patient.get("病历类型") or ""
        reg = patient.get("登记号") or patient.get("reg_no") or rid
        visit = rec.get("visit") or {}
        dept = visit.get("医生所属科室") or ""
        sex_rec[sex] += 1
        if ctype:
            type_rec[ctype] += 1
        if dept:
            dept_rec[dept] += 1
        p = patients.setdefault(reg, {"sex": sex, "n": 0})
        p["n"] += 1

        # ---- 诊断 ----
        _dx = rec.get("diagnoses") or {}
        diags = _dx.get("items") if isinstance(_dx, dict) else _dx
        if not isinstance(diags, list):
            diags = []
        for d in diags:
            if not isinstance(d, dict):
                continue
            diag_total += 1
            nm = d.get("name") or ""
            if nm:
                diag_name[nm] += 1
            icd = d.get("icd_yb_code") or ""
            if icd:
                diag_icd[icd] += 1
            side = d.get("side") or "未指明"
            diag_side[side] += 1
            # TNM：直接读产物归一字段（前缀/维度/分期全部在 derive_contract_fields.py
            # 归一完毕），此处不再做第二套归一。
            tnms = d.get("tnm")
            if isinstance(tnms, dict):
                tnms = [tnms]
            elif not isinstance(tnms, list):
                tnms = []
            for tnm in tnms:
                if not isinstance(tnm, dict):
                    continue
                if tnm.get("t_norm"): tnm_t[dim_label(tnm["t_norm"], "T")] += 1
                if tnm.get("n_norm"): tnm_n[dim_label(tnm["n_norm"], "N")] += 1
                if tnm.get("m_norm"): tnm_m[dim_label(tnm["m_norm"], "M")] += 1
                if tnm.get("stage_norm") or tnm.get("stage_raw_cn"):
                    tnm_stage[stage_label(tnm)] += 1
                if tnm.get("stage_system"): stage_sys[tnm["stage_system"]] += 1

        # ---- IHC ----
        _ae = rec.get("auxiliary_exam") or {}
        ihc = _ae.get("ihc") if isinstance(_ae, dict) else None
        ihc = ihc if isinstance(ihc, dict) else {}
        panels = ihc.get("panels") if isinstance(ihc, dict) else []
        if not isinstance(panels, list):
            panels = []
        rec_subtype = None
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            for m in (panel.get("markers") or []):
                if not isinstance(m, dict):
                    continue
                nm = norm_marker(m.get("marker_name"))
                c = call_of(m)
                if nm in ("ER", "PR", "HER2") or nm in HER2_ALIASES or nm in ("KI67", "MIB1", "MIB-1"):
                    # 归一到四件套名
                    if nm in HER2_ALIASES:
                        tgt = "HER2"
                    elif nm in ("KI67", "MIB1", "MIB-1"):
                        tgt = "Ki67"
                    else:
                        tgt = nm
                    if c:
                        ihc_call[tgt][c] += 1
                    if tgt == "Ki67" and m.get("ki67_band"):
                        ihc_ki67_band[m["ki67_band"]] += 1
                    if tgt == "HER2" and m.get("score"):
                        ihc_her2_score[str(m["score"])] += 1
            # 分子分型：panel 含 ER/PR 任一即可尝试
            if rec_subtype is None:
                st, basis = classify_subtype(panel)
                if st:
                    rec_subtype = st
                    panel_with_target += 1
        if rec_subtype:
            subtype_counter[rec_subtype] += 1
            rec_with_subtype += 1

    # ---- 患者级统计 ----
    n_pat = len(patients)
    sex_pat = Counter(p["sex"] for p in patients.values())
    freq = Counter(p["n"] for p in patients.values())

    res = {
        "src": SRC_TAR,
        "record_level": {
            "records": n_rec,
            "sex": dict(sex_rec.most_common()),
            "case_type": dict(type_rec.most_common()),
            "dept": dict(dept_rec.most_common(10)),
        },
        "patient_level": {
            "patients": n_pat,
            "sex": dict(sex_pat.most_common()),
            "visit_freq_median": None,  # 下方计算
            "visit_freq": {str(k): v for k, v in sorted(freq.items())},
        },
        "diagnosis": {
            "total_entries": diag_total,
            "top_names": dict(diag_name.most_common(30)),
            "top_icd": dict(diag_icd.most_common(30)),
            "side": dict(diag_side.most_common()),
            "tnm": {"t": dict(tnm_t.most_common()), "n": dict(tnm_n.most_common()),
                    "m": dict(tnm_m.most_common()), "stage": dict(tnm_stage.most_common()),
                    "stage_system": dict(stage_sys.most_common())},
        },
        "ihc": {
            "call_by_marker": {k: dict(v.most_common()) for k, v in ihc_call.items()},
            "ki67_band": dict(ihc_ki67_band.most_common()),
            "her2_score": dict(ihc_her2_score.most_common()),
        },
        "molecular_subtype": {
            "subtype": dict(subtype_counter.most_common()),
            "records_classified": rec_with_subtype,
            "classification_rate": round(rec_with_subtype / n_rec, 4),
        },
    }
    # 中位就诊次数
    freqs = sorted(patients.values(), key=lambda p: p["n"])
    if freqs:
        mid = len(freqs) // 2
        res["patient_level"]["visit_freq_median"] = freqs[mid]["n"] if len(freqs) % 2 else \
            (freqs[mid - 1]["n"] + freqs[mid]["n"]) / 2

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("OK ->", OUT)
    print("records", n_rec, "patients", n_pat)
    print("diag entries", diag_total, "| subtype classified", rec_with_subtype)

if __name__ == "__main__":
    main()
