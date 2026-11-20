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
from tarstream import iter_records, count  # noqa: E402

PACKED = DATA + "/中间产物/_packed"
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_aux.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final_birads.tar"
OUTD = DATA + "/中间产物/_qa_out"

# ---- 契约字段名（§3.5.1）----
F_BIRADS = "birads"        # report 层：BI-RADS 原样表述
F_GRADE_RAW = "grade_raw"  # impressions 层：该条自带的分级表述

# ---- 同义键：模型自造键名，只在这几类里取值，绝不把体系字段当值 ----
RE_KEY = re.compile(r"^(bi.?rads|grad)", re.I)
KEY_SKIP = {
    "grading_system", "grade_system", "grading_system_value", "grading_system_raw",
    "grade_system_value", "system", "category_system",
    "grading_note", "grading_basis", "grading_reason", "grading_meaning",
    "grading_interpretation", "grading_recommendation", "grading_scope",
    "grading_source", "grading_reference", "grading_text", "grading_label",
    "grading_desc", "grading_description", "grading_summary",
}

UNSPEC = {"", "未提及", "未指明", "未标注", "未明示", "未使用", "未填", "空", "none", "null",
          "无", "-", "—", "/", "∅", "n/a", "na", "unknown", "不适用", "不详", "未知"}

# 分级值域：0–6 七级，4 拆 a/b/c
GRADE_ORDER = {"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "4a": 5, "4b": 6, "4c": 7, "5": 8, "6": 9}
# 范围写法（契约 §3.5.1：`3-4a类` 不压成单一值，原样保留；排序与组内最高取高端）
RE_TX_RANGE = re.compile(
    r"([0-6])\s*([abcABC])?\s*[-–~—～至]\s*([0-6])\s*([abcABC])?\s*(?=[类级])")
RE_TX_BI = re.compile(r"BI[\s\-–—]*RADS[^\d]{0,12}?([0-6])\s*([abcABC])?", re.I)
RE_TX_BARE = re.compile(r"(?<![\d.])([0-6])\s*类")
RE_TX_CLASS = re.compile(r"分类\s*([0-6])\s*([abcABC])?")
RE_ALREADY_FMT = re.compile(r"^[0-6]([abcABC])?类$")

def fmt_grade(g, raw=""):
    r = _s(raw)
    if re.search(r"[-–~—～至]", r) and re.match(r"^[0-6]\s*[abc]?\s*[-–~—～至]", r, re.I):
        return r if r.endswith("类") else r + "类"
    if RE_ALREADY_FMT.match(r):
        return r
    return g + "类"

def text_candidates(txt):
    out = []
    for m in RE_TX_RANGE.finditer(txt):
        hi, sub2 = m.group(3), (m.group(4) or "").lower()
        g = hi + sub2 if hi == "4" and sub2 else hi
        if g in GRADE_ORDER:
            out.append((m.group(0).strip() + "类", g, m.group(0)))
    rest = RE_TX_RANGE.sub(lambda m: " " * len(m.group(0)), txt) if out else txt
    for m in RE_TX_BI.finditer(rest):
        d, sub = m.group(1), (m.group(2) or "").lower()
        g = d + sub if d == "4" and sub else d
        if g in GRADE_ORDER:
            out.append((g + "类", g, m.group(0)))
    if not out:
        for m in RE_TX_CLASS.finditer(rest):
            d, sub = m.group(1), (m.group(2) or "").lower()
            g = d + sub if d == "4" and sub else d
            if g in GRADE_ORDER:
                out.append((g + "类", g, m.group(0)))
        if not out:
            for m in RE_TX_BARE.finditer(rest):
                g = m.group(1)
                if g in GRADE_ORDER:
                    out.append((g + "类", g, m.group(0)))
    return out

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()

def blank(v):
    return _s(v).lower() in UNSPEC

def parse_grade(raw):
    t = _s(raw).upper().replace("类", "").replace("级", "")
    m = re.search(r"(?<![\d.])([0-6])\s*([ABC])?", t)
    if not m:
        return None
    d, sub = m.group(1), (m.group(2) or "").lower()
    v = d + sub if d == "4" and sub else d
    return v if v in GRADE_ORDER else None

def is_birads_unit(rp):
    gs = _s(rp.get("grading_system")).upper().replace("-", "").replace(" ", "")
    return gs.startswith("BIRADS")

def unit_key_candidates(rp, stat):
    out = []
    for k, v in rp.items():
        kl = k.lower()
        if kl in KEY_SKIP or kl == F_BIRADS or not RE_KEY.search(kl):
            continue
        if isinstance(v, (str, int, float)) and not blank(v):
            g = parse_grade(v)
            if g:
                out.append((g, fmt_grade(g, v), "键:" + k, _s(v)))
                stat["src_key_" + kl] += 1
    return out

def imp_text(im):
    return _s(im.get("text"))

def imp_key_candidates(im, stat):
    out = []
    for k, v in im.items():
        kl = k.lower()
        if kl in KEY_SKIP or kl == F_GRADE_RAW or not RE_KEY.search(kl):
            continue
        if isinstance(v, (str, int, float)) and not blank(v):
            g = parse_grade(v)
            if g:
                out.append((g, fmt_grade(g, v), "键:" + k, _s(v)))
                stat["src_impkey_" + kl] += 1
    return out

def backfill_record(rec, stat, ledger):
    ax = rec.get("auxiliary_exam")
    if not isinstance(ax, dict):
        stat["skip_no_aux"] += 1
        return
    reps = ax.get("reports")
    if not isinstance(reps, list):
        stat["skip_no_reports"] += 1
        return

    rid = rec.get("_rid")
    for rp in reps:
        if not isinstance(rp, dict) or not is_birads_unit(rp):
            continue
        stat["unit"] += 1

        # ---- 1. 逐条结论：补 grade_raw ----
        imps = rp.get("impressions")
        imps = imps if isinstance(imps, list) else []
        best = []          # 组内各条的分级
        for im in imps:
            if not isinstance(im, dict):
                continue
            gv = im.get(F_GRADE_RAW)
            if not blank(gv):
                g = parse_grade(gv)
                if g:
                    best.append((g, _s(gv), "契约已有", _s(gv)))
                else:
                    stat["skip_grade_raw_unparsable"] += 1
                continue

            pick = None
            for cand in imp_key_candidates(im, stat):
                pick = cand
                break
            if pick is None:
                for val, g, raw in text_candidates(imp_text(im)):
                    pick = (g, val, "结论文本", raw)
                    break
            if pick is None:
                stat["skip_no_grade_in_imp"] += 1
                continue

            im[F_GRADE_RAW] = pick[1]
            best.append(pick)
            stat["grade_raw_filled"] += 1
            stat["by_src_" + pick[2].split(":")[0]] += 1
            ledger.write(json.dumps({
                "record_id": rid,
                "report_id": _s(rp.get("report_id")) or None,
                "report_type": _s(rp.get("report_type")),
                "body_site": _s(rp.get("body_site")),
                "laterality": _s(rp.get("laterality")),
                "field": "impressions[].grade_raw",
                "written": pick[1],
                "grade": pick[0],
                "source": pick[2],
                "quote": pick[3] or imp_text(im)[:80],
            }, ensure_ascii=False) + "\n")

        # ---- 2. 单元级：补 birads（组内最高）----
        if not blank(rp.get(F_BIRADS)):
            stat["skip_birads_filled"] += 1
            continue
        if not best:
            for cand in unit_key_candidates(rp, stat):
                best.append(cand)
                break
        if not best:
            stat["skip_no_grade_in_unit"] += 1
            continue

        top = max(best, key=lambda x: GRADE_ORDER.get(x[0], -1))
        distinct = {b[0] for b in best}
        rp[F_BIRADS] = top[1]
        stat["birads_filled"] += 1
        stat["birads_multi_distinct"] += (1 if len(distinct) > 1 else 0)
        ledger.write(json.dumps({
            "record_id": rid,
            "report_id": _s(rp.get("report_id")) or None,
            "report_type": _s(rp.get("report_type")),
            "field": "report.birads",
            "written": top[1],
            "grade": top[0],
            "source": top[2],
            "quote": top[3],
            "unit_distinct_grades": sorted(distinct, key=lambda x: GRADE_ORDER.get(x, -1)),
        }, ensure_ascii=False) + "\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份（抽样试跑）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出 tar")
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--out-tar", default=OUT_TAR_DEFAULT, metavar="PATH")
    a = ap.parse_args()

    if not os.path.exists(a.src_tar):
        raise SystemExit("缺输入包 %s" % a.src_tar)
    n_src = count(a.src_tar)
    print("输入：%s（%d 份）" % (os.path.basename(a.src_tar), n_src))

    if not a.dry_run:
        if os.path.exists(a.out_tar) and not a.force:
            raise SystemExit("输出 tar 已存在，加 --force 覆盖：%s" % a.out_tar)
        os.makedirs(os.path.dirname(a.out_tar) or ".", exist_ok=True)
        os.makedirs(OUTD, exist_ok=True)

    stat = collections.defaultdict(int)
    ledger_path = os.path.join(OUTD, "birads_backfill_ledger.jsonl")
    tf_out = tarfile.open(a.out_tar, "w") if (a.out_tar and not a.dry_run) else None
    ledger_file = None if a.dry_run else open(ledger_path, "w", encoding="utf-8")
    ledger = ledger_file if ledger_file is not None else io.StringIO()

    def _emit(fn, payload):
        ti = tarfile.TarInfo("./" + fn)
        ti.size = len(payload)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tf_out.addfile(ti, io.BytesIO(payload))

    n_rec = 0
    t1 = time.time()
    for fn, rec in iter_records(a.src_tar, limit=a.limit):
        n_rec += 1
        rec["_rid"] = fn.split("/")[-1].replace(".json", "")
        backfill_record(rec, stat, ledger)
        rec.pop("_rid", None)
        if not a.dry_run:
            _emit(fn, json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))

    el = time.time() - t1
    if ledger_file is not None:
        ledger_file.close()
    if tf_out is not None:
        tf_out.close()

    print("\n记录 %d / BI-RADS 单元 %d / 补 grade_raw %d 条 / 补 birads %d 个单元（组内多分级 %d）/ 用时 %.1fs"
          % (n_rec, stat["unit"], stat["grade_raw_filled"], stat["birads_filled"],
             stat["birads_multi_distinct"], el))
    srcs = {k[7:]: v for k, v in stat.items() if k.startswith("by_src_")}
    print("补值来源：%s" % dict(sorted(srcs.items(), key=lambda x: -x[1])))
    keyd = {k[7:]: v for k, v in stat.items() if k.startswith("src_key_") or k.startswith("src_impkey_")}
    print("取值的同义键：%s" % dict(sorted(keyd.items(), key=lambda x: -x[1])[:15]))
    skips = {k[5:]: v for k, v in stat.items() if k.startswith("skip_")}
    print("跳过：%s" % dict(sorted(skips.items(), key=lambda x: -x[1])))

    if not a.dry_run:
        summary = {
            "generated_by": "05_agents/backfill_birads_from_text.py",
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": a.src_tar, "out": a.out_tar,
            "n_record": n_rec,
            "n_unit": stat["unit"],
            "n_grade_raw_filled": stat["grade_raw_filled"],
            "n_birads_filled": stat["birads_filled"],
            "n_unit_multi_grade": stat["birads_multi_distinct"],
            "by_source": dict(sorted(srcs.items(), key=lambda x: -x[1])),
            "synonym_keys": dict(sorted(keyd.items(), key=lambda x: -x[1])),
            "skip_detail": dict(sorted(skips.items(), key=lambda x: -x[1])),
            "ledger": ledger_path,
            "rule": ("仅 BI-RADS 单元；grade_raw 空则取该条同义键或结论文本中的分级原样表述；"
                     "birads 空则取组内最高原样表述；值域 0-6 七级；"
                     "不覆盖已有值、不删自造键、不新增 birads_grade（须先改契约）、D 层不填"),
            "elapsed_sec": round(el, 1),
        }
        fp = os.path.join(OUTD, "birads_backfill_summary.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("✓ tar %s  %.1f MB" % (a.out_tar, os.path.getsize(a.out_tar) / 1048576))
        print("✓ 摘要 %s" % fp)
        print("✓ 账本 %s" % ledger_path)
    else:
        print("\n（dry-run，未落盘）")

if __name__ == "__main__":
    main()
