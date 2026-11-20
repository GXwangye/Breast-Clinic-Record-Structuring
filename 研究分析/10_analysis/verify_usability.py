# -*- coding: utf-8 -*-
import json
import os
import sys
import time
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

SRC_TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
OUT = DATA + "/中间产物/_qa_out/可用性验证_20261107.json"

NARRATIVE = ("chief_complaint", "present_illness", "past_history", "physical_exam")

def items_of(blk):
    if isinstance(blk, list):
        return blk
    if isinstance(blk, dict):
        v = blk.get("items")
        return v if isinstance(v, list) else []
    return []

def walk_anchors(node, acc):
    if isinstance(node, dict):
        a = node.get("anchor")
        if isinstance(a, dict) and "quote" in a:
            acc.append(a)
        for v in node.values():
            walk_anchors(v, acc)
    elif isinstance(node, list):
        for v in node:
            walk_anchors(v, acc)

def blk_nonempty(blk):
    if isinstance(blk, dict):
        return bool(blk.get("raw") or blk.get("structured"))
    return bool(blk)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    n = 0
    # A. 覆盖度计数
    cov = {k: 0 for k in (
        "records", "chief_complaint", "present_illness", "past_history",
        "physical_exam", "auxiliary_exam", "diagnoses", "treatment",
        "orders", "follow_up")}
    # B. 关键字段完整性
    diag_total = diag_icd = diag_tnm = diag_stage = diag_side = 0
    report_total = report_date = report_impression = 0
    lab_total = lab_value = lab_unit = 0
    lab_item_total = lab_item_value = lab_item_unit = 0
    ihc_marker = ihc_call = ihc_band = 0
    # C. anchor
    anchor_total = anchor_hit = anchor_skip = 0
    # D. qc / 一致性
    qc_any = 0
    qc_missing = qc_low_conf = qc_conflicts = 0
    parse_err = 0
    # 报告单元按器官侧别、诊断 side 取值
    side_vals = Counter()

    t0 = time.time()
    for rid, rec in iter_records(SRC_TAR, limit=a.limit):
        n += 1
        try:
            cov["records"] += 1
            for k in NARRATIVE:
                if blk_nonempty(rec.get(k)):
                    cov[k] += 1
            ae = rec.get("auxiliary_exam") or {}
            if ae.get("reports") or ae.get("labs") or (ae.get("ihc") or {}).get("panels"):
                cov["auxiliary_exam"] += 1
            if items_of(rec.get("diagnoses")):
                cov["diagnoses"] += 1
            if items_of(rec.get("treatment")):
                cov["treatment"] += 1
            if items_of(rec.get("orders")):
                cov["orders"] += 1
            if items_of(rec.get("follow_up")):
                cov["follow_up"] += 1

            # 诊断：ICD / TNM / stage / side
            for it in items_of(rec.get("diagnoses")):
                if not isinstance(it, dict):
                    continue
                diag_total += 1
                if it.get("icd_yb_code"):
                    diag_icd += 1
                tnm = it.get("tnm") or {}
                if tnm.get("t") or tnm.get("n") or tnm.get("m"):
                    diag_tnm += 1
                if tnm.get("stage"):
                    diag_stage += 1
                sd = it.get("side") or it.get("laterality")
                if sd:
                    diag_side += 1
                    side_vals[sd] += 1

            # 报告单元
            for rp in (ae.get("reports") or []):
                if not isinstance(rp, dict):
                    continue
                report_total += 1
                if rp.get("exam_date"):
                    report_date += 1
                if rp.get("impression") or rp.get("impressions"):
                    report_impression += 1

            # 检验指标。
            # 注意：labs[] 有三种形态（`panel_name+results`／`panel+results`／扁平 `name+value`），
            # 归一后数值落在 `results[].value`，条目顶层 `value` 多数已不存在。
            # 因此「可用性」必须按 results 中心判定；只数顶层 `value` 会得到 62% 的假低值。
            for lb in (ae.get("labs") or []):
                if not isinstance(lb, dict):
                    continue
                lab_total += 1
                items = [x for x in (lb.get("results") or []) if isinstance(x, dict)]
                if not items and lb.get("value"):
                    items = [lb]      # 扁平形态：条目自身即一个指标
                lab_item_total += len(items)
                if any(str(x.get("value") or "").strip() for x in items):
                    lab_value += 1
                if any(str(x.get("unit") or "").strip() for x in items):
                    lab_unit += 1
                lab_item_value += sum(1 for x in items if str(x.get("value") or "").strip())
                lab_item_unit += sum(1 for x in items if str(x.get("unit") or "").strip())

            # IHC 四件套 call
            ihc = ae.get("ihc") or {}
            for p in (ihc.get("panels") or []) if isinstance(ihc.get("panels"), list) else []:
                if not isinstance(p, dict):
                    continue
                for m in (p.get("markers") or []):
                    if not isinstance(m, dict):
                        continue
                    ihc_marker += 1
                    if m.get("call"):
                        ihc_call += 1
                    if m.get("ki67_band"):
                        ihc_band += 1

            # anchor 回指（用记录内 narrative 原文做自洽复验）
            # field 中文名 → narrative 板块 raw 原文映射；「诊断列/医嘱/None」无 raw 原文，单列不纳入分母
            FIELD2RAW = {
                "主诉": "chief_complaint", "现病史": "present_illness",
                "既往史": "past_history", "体格检查": "physical_exam",
                "辅助检查": "auxiliary_exam",
            }
            rawmap = {}
            for k in NARRATIVE:
                blk = rec.get(k) or {}
                if isinstance(blk, dict):
                    rawmap[k] = blk.get("raw") or ""
            # 辅助检查 raw：v7.1「彻底去原文」后产物不带 report_raw（已随 v8 淘汰），
            # 产物内可回指的只有报告单条目上的 raw 字段；无则记空（自洽复验口径见文档 §三）
            ae_raw = ""
            if isinstance(ae, dict):
                parts = []
                for rp in (ae.get("reports") or []):
                    if isinstance(rp, dict):
                        parts.append(rp.get("raw") or "")
                ae_raw = "".join(parts)
            rawmap["auxiliary_exam"] = ae_raw or "".join(rawmap.values())

            acc = []
            walk_anchors(rec, acc)
            for an in acc:
                q = an.get("quote") or ""
                fld = an.get("field") or ""
                if fld in FIELD2RAW:
                    anchor_total += 1
                    src = rawmap.get(FIELD2RAW[fld]) or ""
                    if q and (q in src or q in "".join(rawmap.values())):
                        anchor_hit += 1
                else:
                    anchor_skip += 1

            # qc
            q = rec.get("qc") or {}
            anyf = False
            for key, cnt in (("missing", 0), ("low_conf", 0), ("conflicts", 0)):
                v = q.get(key) or []
                if isinstance(v, list):
                    if key == "missing":
                        qc_missing += len(v)
                    elif key == "low_conf":
                        qc_low_conf += len(v)
                    else:
                        qc_conflicts += len(v)
                    if v:
                        anyf = True
            if anyf:
                qc_any += 1
        except Exception:
            parse_err += 1
            continue

    res = {
        "src": SRC_TAR,
        "scanned": n,
        "coverage": {k: v for k, v in cov.items() if k != "records"},
        "coverage_rate": {k: round(v / n, 4) for k, v in cov.items() if k != "records"},
        "diagnosis": {
            "total": diag_total,
            "icd_rate": round(diag_icd / diag_total, 4) if diag_total else None,
            "tnm_rate": round(diag_tnm / diag_total, 4) if diag_total else None,
            "stage_rate": round(diag_stage / diag_total, 4) if diag_total else None,
            "side_rate": round(diag_side / diag_total, 4) if diag_total else None,
            "side_dist": dict(side_vals.most_common(6)),
        },
        "report": {
            "total": report_total,
            "exam_date_rate": round(report_date / report_total, 4) if report_total else None,
            "impression_rate": round(report_impression / report_total, 4) if report_total else None,
        },
        "lab": {
            "total": lab_total,
            "value_rate": round(lab_value / lab_total, 4) if lab_total else None,
            "unit_rate": round(lab_unit / lab_total, 4) if lab_total else None,
            "note": "value_rate/unit_rate 为**条目级**：该检验条目是否至少有一个带数值/带单位的指标。",
            "items": {
                "total": lab_item_total,
                "value": lab_item_value,
                "unit": lab_item_unit,
                "value_rate": round(lab_item_value / lab_item_total, 4) if lab_item_total else None,
                "unit_rate": round(lab_item_unit / lab_item_total, 4) if lab_item_total else None,
            },
        },
        "ihc": {
            "marker_total": ihc_marker,
            "call_count": ihc_call,
            "call_rate": round(ihc_call / ihc_marker, 4) if ihc_marker else None,
            "ki67_band_count": ihc_band,
        },
        "anchor": {
            "total": anchor_total,
            "hit": anchor_hit,
            "hit_rate": round(anchor_hit / anchor_total, 4) if anchor_total else None,
            "skip_no_raw": anchor_skip,
        },
        "qc": {
            "records_with_any_flag": qc_any,
            "missing": qc_missing,
            "low_conf": qc_low_conf,
            "conflicts": qc_conflicts,
        },
        "parse_err": parse_err,
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
