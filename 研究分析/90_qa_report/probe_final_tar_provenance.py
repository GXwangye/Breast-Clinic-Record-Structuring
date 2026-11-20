# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
DEFAULT_TAR = PACKED + "/assembled_v7_final.tar"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

DIRTY_NATURE = {"（-）", "(-)", "（-)", "-", "阴性", "未触及明显肿大", "（）", "()"}
KIND7 = {"药品", "检查", "检验", "操作", "材料", "费用", "其他"}

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return ""
    return str(v).strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", default=DEFAULT_TAR)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    C = collections.Counter()
    n_rec = 0

    for name, rec in iter_records(a.tar, limit=a.limit):
        n_rec += 1

        # ---------- 1. 派生 IHC call ----------
        ae = rec.get("auxiliary_exam") or {}
        ihc = ae.get("ihc") or ae.get("ihc_panels") or {}
        panels = ihc.get("panels") if isinstance(ihc, dict) else None
        if panels is None:
            panels = ihc if isinstance(ihc, list) else []
        for p in (panels or []):
            if not isinstance(p, dict):
                continue
            for mk in (p.get("markers") or []):
                if not isinstance(mk, dict):
                    continue
                C["marker_total"] += 1
                if _s(mk.get("call")):
                    C["marker_with_call"] += 1
                if _s(mk.get("call_src")):
                    C["marker_call_src"] += 1
                if mk.get("call_conflict"):
                    C["marker_call_conflict"] += 1
                if _s(mk.get("ki67_band")):
                    C["marker_ki67_band"] += 1

        # ---------- 2. 辅检形态/枚举归一 ----------
        rep_groups = (ae.get("reports") or [])
        for rep in rep_groups:
            if not isinstance(rep, dict):
                continue
            C["report_total"] += 1
            if rep.get("impressions_wrapped"):
                C["rep_impressions_wrapped"] += 1
            if rep.get("exam_date_absent"):
                C["rep_exam_date_absent"] += 1
            if _s(rep.get("exam_date_raw")):
                C["rep_exam_date_raw"] += 1
            if _s(rep.get("enum_leak")):
                C["rep_enum_leak"] += 1
            if rep.get("site_absent"):
                C["rep_site_absent"] += 1
            if _s(rep.get("body_site_text")):
                C["rep_body_site_text"] += 1
            if _s(rep.get("report_type_src")):
                C["rep_report_type_src"] += 1
            for imp in (rep.get("impressions") or []):
                C["impression_total"] += 1
                if isinstance(imp, dict):
                    C["impression_obj"] += 1
                else:
                    C["impression_bare_str"] += 1

        for lab in (ae.get("labs") or []):
            if not isinstance(lab, dict):
                continue
            C["lab_total"] += 1
            if _s(lab.get("lab_shape_from")):
                C["lab_shape_from"] += 1
            if _s(lab.get("panel_name")):
                C["lab_has_panel_name"] += 1
            if isinstance(lab.get("results"), list):
                C["lab_has_results"] += 1

        # ---------- 3. 契约字段派生 ----------
        pt = rec.get("patient") or {}
        if _s(pt.get("性别_text")):
            C["patient_sex_text"] += 1

        dgs = rec.get("diagnoses")
        items = dgs.get("items") if isinstance(dgs, dict) else dgs
        for dg in (items or []):
            if not isinstance(dg, dict):
                continue
            C["diag_total"] += 1
            tnm = dg.get("tnm") or {}
            if not isinstance(tnm, dict) or not tnm:
                continue
            C["diag_tnm_obj"] += 1
            if _s(tnm.get("stage_system")):
                C["tnm_stage_system"] += 1
            if _s(tnm.get("stage_raw_cn")):
                C["tnm_stage_raw_cn"] += 1
            for k in ("prefix_norm", "t_norm", "n_norm", "m_norm", "stage_norm"):
                if _s(tnm.get(k)):
                    C["tnm_" + k] += 1
            if tnm.get("derived_qc"):
                C["tnm_derived_qc"] += 1

        # ---------- 4. orders.kind 七桶归一（normalize_order_kind） ----------
        meta = rec.get("meta") or {}
        if meta.get("order_kind_normalized") is True:
            C["meta_order_kind_normalized"] += 1
        for it in ((rec.get("orders") or {}).get("items") or []):
            if not isinstance(it, dict):
                continue
            C["order_item_total"] += 1
            kd = _s(it.get("kind"))
            if kd in KIND7:
                C["order_kind_in_bucket"] += 1
            elif kd:
                C["order_kind_outside_bucket"] += 1
            else:
                C["order_kind_empty"] += 1

        # ---------- 5. 值域写法归一（clean_enum_values） ----------
        pe = rec.get("physical_exam") or {}
        st = pe.get("structured") or {}
        for ln in (st.get("lymph_nodes") or []):
            if not isinstance(ln, dict):
                continue
            C["lymph_node_total"] += 1
            nat = _s(ln.get("nature"))
            if nat:
                C["lymph_nature_filled"] += 1
                if nat in DIRTY_NATURE:
                    C["lymph_nature_dirty_left"] += 1
                if nat == "未见异常":
                    C["lymph_nature_normal"] += 1

    out = {"tar": a.tar, "records": n_rec, "counts": dict(sorted(C.items()))}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json_out:
        os.makedirs(os.path.dirname(a.json_out) or ".", exist_ok=True)
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\nwrote " + a.json_out)

if __name__ == "__main__":
    main()
