# -*- coding: utf-8 -*-
import argparse
import collections
import io
import json
import os
import re
import sys
import tarfile
import time

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
from tarstream import iter_records, count  # noqa: E402
import normalize_enum as N  # noqa: E402

PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_tnm.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final_aux.tar"
LEDGER_DEFAULT = DATA + "/中间产物/_qa_out/aux_normalize_ledger.jsonl"

DATE_RE = re.compile(r"^\d{4}(-\d{1,2}(-\d{1,2})?)?$")
# 「没写日期」的几种写法。注意 `unknown` 之类英文占位也要算进来——
# 它们与「未指明」同义，留着会让下游把占位当真实日期解析。
DATE_PH = {"未指明", "未提及", "未标注", "未注明", "未填", "未写", "空", "无", "不详",
           "未知", "不明", "none", "null", "unknown", "-", "—", "/", "∅", "n/a", "na"}
# 契约 §3.5.1 的泛称：归「其他」后按 exam_name 回填模态。
GENERIC_RT = re.compile(r"^(影像|影像学检查|影像检查|检查|检查报告|报告|描述|结论|所见|"
                        r"其他|其他检查|辅助检查|combined|imaging)$")
SPECIMEN_STAMP = "site_not_applicable"
# 多部位混写（`乳腺+腋窝`、`乳腺及腋窝`、`乳腺/腋窝`）：分隔符任取其一。
MULTI_RE = re.compile(r"[+＋]|及|和|与|/|／")

def _s(v):
    return "" if v is None else str(v).strip()

def _obj(x):
    return x if isinstance(x, dict) else {}

def _lst(x):
    return x if isinstance(x, list) else []

# ---------------------------------------------------------------- A. impressions
def _wrap_imp(text, rep_anchor):
    o = {"text": text, "site_hint": "", "side_hint": "", "grade_raw": ""}
    if rep_anchor:
        # 报告级 anchor 指整段，不是该条结论的精确定位——标清范围，不冒充条目级
        o["anchor"] = dict(rep_anchor)
        o["anchor_scope"] = "report_level"
    return o

def _normalize_imp_obj(o, stat):
    txt = str(o.get("text") or "").strip()
    if txt:
        return o
    # 同义键：模型换了名但存的就是结论原文，改名即可，不重新生成
    for k in ("impression_text", "impression", "text_raw", "conclusion", "impression_raw"):
        v = str(o.get(k) or "").strip()
        if v:
            o["text"] = v
            o["text_from"] = k
            stat["imp_text_renamed"] += 1
            return o
    # text 空：只剩 qc / anchor 这类标记的，等于没有内容，删除
    keys = [k for k in o if k != "text" and o.get(k) not in ("", None, [], {})]
    if not keys or set(keys) <= {"qc", "anchor", "anchor_scope", "qc_notes"}:
        stat["imp_empty_dropped"] += 1
        return None
    # 还有别的内容键（site_hint/value/findings…）：不猜它该不该当结论，打标留待核
    o["text_key_variant"] = ",".join(sorted(keys))
    stat["imp_text_variant"] += 1
    return o

def fix_impressions(rep, stat, ledger, rid):
    imps = rep.get("impressions")
    rep_anchor = _obj(rep.get("anchor"))

    # 整段结论被写成单个字符串（不是数组）：先补数组再包对象
    if isinstance(imps, str):
        if not imps.strip():
            rep["impressions"] = []
            stat["imp_empty_dropped"] += 1
            return
        rep["impressions"] = [_wrap_imp(imps, rep_anchor)]
        rep["impressions_wrapped"] = True
        stat["imp_wrapped"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "impression_wrap_single",
                                 "text": imps[:120]}, ensure_ascii=False) + "\n")
        return

    if not isinstance(imps, list) or not imps:
        return

    out = []
    changed = False
    for i in imps:
        if isinstance(i, dict):
            before = stat["imp_text_renamed"] + stat["imp_empty_dropped"] + stat["imp_text_variant"]
            o = _normalize_imp_obj(i, stat)
            after = stat["imp_text_renamed"] + stat["imp_empty_dropped"] + stat["imp_text_variant"]
            if o is None:
                changed = True
                continue
            if after != before:
                changed = True
            out.append(o)
        elif i is None:
            stat["imp_null_dropped"] += 1
            changed = True
        else:
            if not str(i).strip():
                # 空串不承载信息，包成对象只会造出一个空壳
                stat["imp_empty_dropped"] += 1
                changed = True
                continue
            out.append(_wrap_imp(str(i), rep_anchor))
            stat["imp_wrapped"] += 1
            changed = True
            ledger.write(json.dumps({"rid": rid, "op": "impression_wrap",
                                     "text": str(i)[:120]}, ensure_ascii=False) + "\n")
    if changed:
        rep["impressions"] = out
        rep["impressions_wrapped"] = True

# ---------------------------------------------------------------- B. exam_date
def fix_exam_date(node, stat, ledger, rid, kind):
    v = _s(node.get("exam_date"))
    if not v:
        return
    if DATE_RE.match(v):
        return
    if v in DATE_PH:
        node["exam_date"] = ""
        node["exam_date_absent"] = True
        stat["date_placeholder"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "date_placeholder_clear", kind: v},
                                ensure_ascii=False) + "\n")
        return
    node["exam_date_raw"] = v
    node["exam_date"] = ""
    stat["date_malformed"] += 1
    ledger.write(json.dumps({"rid": rid, "op": "date_malformed_clear", kind: v},
                            ensure_ascii=False) + "\n")

# ---------------------------------------------------------------- C. report_type
def fix_report_type(rep, stat, ledger, rid):
    raw = _s(rep.get("report_type"))
    exam_name = _s(rep.get("exam_name")) + " " + _s(rep.get("exam_name_alt"))

    if raw in N.CLOSED["report_type"]:
        rep["report_type"] = raw  # 写回 strip 后的值（原值可能带首尾空格）
        stat["rt_exact"] += 1
        return

    target, how, _side = N.rule_map("report_type", raw)

    if how == "leak":
        # 契约 §3.5.1：检验类出现在本字段即错位，不得塞「其他」
        rep["enum_leak"] = "lab_in_reports"
        rep["report_type_text"] = raw
        stat["rt_leak"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "rt_leak_keep", "raw": raw},
                                ensure_ascii=False) + "\n")
        return

    if how == "empty":
        # 原文没写：按 exam_name 回填模态，填不出才落「其他」
        _rt_by_exam(rep, raw, "fill_empty", "fill_empty_other", stat, ledger, rid, exam_name)
        return

    if how in ("rule", "exact") and target:
        rep["report_type"] = target
        rep["report_type_src"] = "rule"
        rep["report_type_text"] = raw
        stat["rt_rule"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "rt_rule", "raw": raw, "to": target},
                                ensure_ascii=False) + "\n")
        return

    if how == "other_generic":
        # 泛称（影像／检查／报告）：归「其他」后按 exam_name 回填模态（契约 §3.5.1）
        _rt_by_exam(rep, raw, "fill_other", "fill_other_other", stat, ledger, rid, exam_name)
        return

    # other_unmatched：规则一条都没认出（长尾写法）→ 其他，原值留痕待审
    rep["report_type"] = "其他"
    rep["report_type_src"] = "other_unmatched"
    rep["report_type_text"] = raw
    stat["rt_other"] += 1
    ledger.write(json.dumps({"rid": rid, "op": "rt_other", "raw": raw},
                            ensure_ascii=False) + "\n")

def _rt_by_exam(rep, raw, src_ok, src_fail, stat, ledger, rid, exam_name):
    t2, how2, _ = N.rule_map("report_type", exam_name)
    if t2 and t2 != "其他" and how2 in ("rule", "exact"):
        rep["report_type"] = t2
        rep["report_type_src"] = src_ok
        stat["rt_" + src_ok] += 1
        ledger.write(json.dumps({"rid": rid, "op": "rt_" + src_ok, "raw": raw,
                                 "exam_name": exam_name[:60], "to": t2},
                                ensure_ascii=False) + "\n")
    else:
        rep["report_type"] = "其他"
        rep["report_type_src"] = src_fail
        stat["rt_" + src_fail] += 1
    rep["report_type_text"] = raw

# ---------------------------------------------------------------- D. body_site
def fix_body_site(rep, stat, ledger, rid):
    raw = _s(rep.get("body_site"))
    if raw in N.CLOSED["body_site"]:
        rep["body_site"] = raw  # 写回 strip 后的值
        stat["bs_exact"] += 1
        # 闭集内也可能仍混着侧别（`左乳`不在闭集，故此处无需剥）
        return

    # 多部位值（`乳腺+腋窝`／`乳腺及腋窝`）：契约要求 body_site 单值，
    # 取原文第一个部位（原文顺序即主次），原值留痕。拆条属结构层，本脚本不搬单元。
    # 标本类不参与拆分（`乳腺切缘` 是标本不是多部位）。
    probe = raw
    if raw and MULTI_RE.search(raw) and not re.search(N.SPECIMEN_RE, raw):
        first = MULTI_RE.split(raw)[0].strip(" 　,，、:：")
        if first and first != raw:
            probe = first
            rep["body_site_multi"] = raw
            stat["bs_multi"] += 1

    target, how, side = N.rule_map("body_site", probe)

    # 剥出的侧别优先填进 laterality（仅当 laterality 空或未指明）
    if side:
        cur = _s(rep.get("laterality"))
        if cur in ("", "未指明"):
            rep["laterality"] = side
            rep["laterality_source"] = "原文明确"
            stat["lat_from_site"] += 1

    if how == "empty":
        rep["body_site"] = ""
        rep["site_absent"] = True
        stat["bs_absent"] += 1
        return

    if how == "specimen":
        rep["body_site"] = ""
        rep["body_site_text"] = raw
        rep[SPECIMEN_STAMP] = True
        stat["bs_specimen"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "bs_specimen_blank", "raw": raw},
                                ensure_ascii=False) + "\n")
        return

    if how == "specimen_organ":
        # `乳腺切缘`／`左乳切缘`：标本里写得出器官，保留器官（丢了「乳腺」就只剩无归属的「切缘」）
        rep["body_site"] = target
        rep["body_site_text"] = raw
        rep["specimen_organ"] = True
        stat["bs_specimen_organ"] += 1
        return

    if how == "lat_only":
        rep["body_site"] = ""
        rep["body_site_text"] = raw
        rep["site_absent"] = True
        stat["bs_lat_only"] += 1
        return

    if how in ("rule", "exact"):
        rep["body_site"] = target
        rep["body_site_text"] = raw
        rep["body_site_src"] = "rule"
        stat["bs_rule"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "bs_rule", "raw": raw, "to": target},
                                ensure_ascii=False) + "\n")
        return

    if how == "other_organ":
        rep["body_site"] = "其他"
        rep["body_site_text"] = raw
        rep["body_site_src"] = "other_organ"
        stat["bs_other"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "bs_other_organ", "raw": raw},
                                ensure_ascii=False) + "\n")
        return

    rep["body_site"] = "其他"
    rep["body_site_text"] = raw
    rep["body_site_src"] = "unmatched"
    stat["bs_other"] += 1
    ledger.write(json.dumps({"rid": rid, "op": "bs_unmatched", "raw": raw},
                            ensure_ascii=False) + "\n")

# ---------------------------------------------------------------- D2. laterality
def fix_laterality(rep, stat, ledger, rid):
    raw = _s(rep.get("laterality"))
    if not raw:
        rep["laterality"] = "未指明"
        rep["laterality_source"] = rep.get("laterality_source") or "缺省未指明"
        stat["lat_absent"] += 1
        return
    if raw in N.CLOSED["laterality"]:
        rep["laterality"] = raw  # 写回 strip 后的值
        stat["lat_exact"] += 1
        return
    target, how, _ = N.rule_map("laterality", raw)
    if target:
        rep["laterality"] = target
        rep["laterality_text"] = raw
        stat["lat_rule"] += 1
    else:
        rep["laterality"] = "未指明"
        rep["laterality_text"] = raw
        stat["lat_todo"] += 1
        ledger.write(json.dumps({"rid": rid, "op": "lat_pending", "raw": raw},
                                ensure_ascii=False) + "\n")

# ---------------------------------------------------------------- E. labs 形态
def fix_labs(lab, stat, ledger, rid):
    if not isinstance(lab, dict):
        return
    changed = False

    # ① 组合名键统一到 panel_name（只补不改）
    if not _s(lab.get("panel_name")):
        src_key = None
        for k in ("report_type", "lab_name", "exam_name"):
            if _s(lab.get(k)):
                src_key = k
                break
        if src_key:
            lab["panel_name"] = _s(lab.get(src_key))
            lab["panel_name_from"] = src_key
            stat["lab_panel_filled"] += 1
            changed = True

    # ② 明细键统一到 results[]（items → results）
    if not _lst(lab.get("results")) and _lst(lab.get("items")):
        lab["results"] = lab["items"]
        lab["results_from"] = "items"
        stat["lab_results_from_items"] += 1
        changed = True

    # ③ 扁平单指标 → 包成 results[0]（原键全部保留，只加视图）
    if not _lst(lab.get("results")) and _s(lab.get("name")):
        item = {}
        for k in ("code", "name", "value", "unit", "flag", "qualitative", "ref"):
            if k in lab:
                item[k] = lab[k]
        if item:
            lab["results"] = [item]
            lab["lab_shape_from"] = "flat_name_value"
            stat["lab_packed_flat"] += 1
            changed = True

    if changed:
        ledger.write(json.dumps({"rid": rid, "op": "lab_shape",
                                 "panel": _s(lab.get("panel_name"))[:40],
                                 "from": lab.get("lab_shape_from") or lab.get("panel_name_from") or ""},
                                ensure_ascii=False) + "\n")

    # ④ 有结构但没有任何名称/明细 → 空壳，打标不删（原文只写「正常」）
    has_name = bool(_s(lab.get("panel_name")))
    has_res = bool(_lst(lab.get("results")))
    if not has_name and not has_res:
        lab["lab_empty"] = True
        stat["lab_empty_shell"] += 1

# ---------------------------------------------------------------- 记录级
def normalize_record(rec, stat, ledger, rid):
    aux = rec.get("auxiliary_exam")
    if not isinstance(aux, dict):
        return
    for rep in _lst(aux.get("reports")):
        if not isinstance(rep, dict):
            continue
        stat["reports"] += 1
        fix_impressions(rep, stat, ledger, rid)
        fix_exam_date(rep, stat, ledger, rid, "report")
        fix_report_type(rep, stat, ledger, rid)
        fix_body_site(rep, stat, ledger, rid)
        fix_laterality(rep, stat, ledger, rid)
    for lab in _lst(aux.get("labs")):
        if not isinstance(lab, dict):
            continue
        stat["labs"] += 1
        fix_exam_date(lab, stat, ledger, rid, "lab")
        fix_labs(lab, stat, ledger, rid)

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT)
    ap.add_argument("--out-tar", default=OUT_TAR_DEFAULT)
    ap.add_argument("--ledger", default=LEDGER_DEFAULT, help="变更明细 jsonl（dry-run 也写）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="只统计不落大盘")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出")
    a = ap.parse_args()

    if not os.path.exists(a.src_tar):
        sys.exit("源包不存在：" + a.src_tar)
    if a.out_tar and os.path.exists(a.out_tar) and not a.force and not a.dry_run:
        sys.exit("输出已存在，加 --force 覆盖：" + a.out_tar)

    os.makedirs(os.path.dirname(a.ledger), exist_ok=True)
    ledger = open(a.ledger, "w", encoding="utf-8") if a.ledger else io.StringIO()

    stat = collections.Counter()
    tf_out = None
    if not a.dry_run:
        tf_out = tarfile.open(a.out_tar, "w")

    t0 = time.time()
    n_rec = 0
    for fn, rec in iter_records(a.src_tar, limit=a.limit):
        n_rec += 1
        rid = os.path.basename(fn).replace(".json", "")
        before = json.dumps(rec, ensure_ascii=False, sort_keys=True) if a.dry_run else None
        normalize_record(rec, stat, ledger, rid)
        if a.dry_run:
            if json.dumps(rec, ensure_ascii=False, sort_keys=True) != before:
                stat["rec_changed"] += 1
            continue
        payload = json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8")
        ti = tarfile.TarInfo("./" + os.path.basename(fn))
        ti.size = len(payload)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tf_out.addfile(ti, io.BytesIO(payload))

    if tf_out:
        tf_out.close()
    if a.ledger:
        ledger.close()

    el = time.time() - t0
    summary = collections.OrderedDict()
    summary["src"] = a.src_tar
    summary["out"] = a.out_tar if not a.dry_run else "(dry-run)"
    summary["records"] = n_rec
    summary["records_changed"] = stat["rec_changed"] if a.dry_run else "n/a"
    summary["reports"] = stat["reports"]
    summary["labs"] = stat["labs"]
    summary["impressions_wrapped"] = stat["imp_wrapped"]
    summary["exam_date"] = {"placeholder_cleared": stat["date_placeholder"],
                            "malformed_cleared": stat["date_malformed"]}
    summary["report_type"] = {"exact": stat["rt_exact"],
                              "fill_empty": stat["rt_fill_empty"],
                              "fill_empty_other": stat["rt_fill_empty_other"],
                              "fill_other": stat["rt_fill_other"],
                              "rule": stat["rt_rule"],
                              "rule_other": stat["rt_other"],
                              "leak_kept": stat["rt_leak"]}
    summary["body_site"] = {"exact": stat["bs_exact"],
                            "rule": stat["bs_rule"],
                            "other": stat["bs_other"],
                            "specimen_blank": stat["bs_specimen"],
                            "specimen_keep_organ": stat["bs_specimen_organ"],
                            "multi_kept_first": stat["bs_multi"],
                            "lat_only_blank": stat["bs_lat_only"],
                            "absent": stat["bs_absent"]}
    summary["laterality"] = {"exact": stat["lat_exact"], "rule": stat["lat_rule"],
                             "absent_filled": stat["lat_absent"],
                             "from_site": stat["lat_from_site"],
                             "pending": stat["lat_todo"]}
    summary["labs_shape"] = {"panel_filled": stat["lab_panel_filled"],
                             "results_from_items": stat["lab_results_from_items"],
                             "flat_packed": stat["lab_packed_flat"],
                             "empty_shell": stat["lab_empty_shell"]}
    summary["elapsed_s"] = round(el, 1)

    with open(DATA + "/中间产物/_qa_out/aux_normalize_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
