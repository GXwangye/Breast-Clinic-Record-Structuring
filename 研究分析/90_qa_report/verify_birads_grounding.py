# -*- coding: utf-8 -*-
import argparse
import collections
import io
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")

from tarstream import iter_records          # noqa: E402
import probe_granularity as pg              # noqa: E402

DATA = ROOT + "/publish/02_结构化结果与过程"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
OUT = DATA + "/qa_reports/birads_grounding.json"

RE_OLD = re.compile(r"BI[\s\-–—]*RADS[^0-9]{0,12}?[0-5]", re.I)
RE_ROMAN = re.compile(r"[ⅠⅡⅢⅣⅤⅥ]")

def s(v):
    return "" if v is None else str(v).strip()

def scan_struct():
    has_unit, has_val = set(), set()
    units = []
    for fn, rec in iter_records(TAR):
        rid = fn.rsplit(".", 1)[0]
        ax = rec.get("auxiliary_exam")
        reps = ax.get("reports") if isinstance(ax, dict) else None
        if not isinstance(reps, list):
            continue
        for rp in reps:
            if not isinstance(rp, dict):
                continue
            # 口径与 probe_granularity 完全一致：只去连字符，不去空格。
            # 「BI RADS」4 个单元（3 条记录）因此不计入——这是 normalize_aux_enum
            # 的漏，应在归一层修，探针不替它兜底（见清单 GRN 待办）。
            gs = s(rp.get("grading_system")).upper().replace("-", "")
            if not gs.startswith("BIRADS"):
                continue
            has_unit.add(rid)
            if pg.has_grade(rp):
                has_val.add(rid)
            # 候选须与 probe_granularity 的调用完全一致：birads 优先（回填后它是主落点，
            g = pg.norm_birads(rp.get("grading_system"),
                               rp.get("birads") or rp.get("grading"),
                               rp.get("impressions"))
            if g:
                raw = s(rp.get("birads")) or s(rp.get("grading"))
                units.append((rid, s(rp.get("report_type")), g, raw))
    return has_unit, has_val, units

ROMAN_MAP = {"1": "Ⅰ", "2": "Ⅱ", "3": "Ⅲ", "5": "Ⅴ", "6": "Ⅵ"}

def value_in_text(g, text):
    t = text.upper().replace("－", "-").replace("—", "-")
    if g in ("4a", "4b", "4c"):
        return re.search(r"4\s*-?\s*" + g[1].upper() + r"(?![0-9A-Z])", t) is not None
    if g == "4":
        return re.search(r"4\s*[类级]", t) is not None \
            or re.search(r"分类\s*为?\s*4(?![0-9ABC])", t) is not None \
            or re.search(r"BI[\s\-]*RA[D]?S?[^0-9Ⅰ-Ⅵ]{0,16}4(?![0-9ABC])", t) is not None
    pats = [r"(?<![\d.])" + g + r"\s*[类级]",
            r"分类\s*为?\s*" + g + r"(?![\d.])",
            r"BI[\s\-]*RA[D]?S?[^0-9Ⅰ-Ⅵ]{0,16}" + g + r"(?![\d.])",
            r"RAD[^0-9Ⅰ-Ⅵ]{0,16}" + g + r"(?![\d.])"]
    if g in ROMAN_MAP:
        pats.append(ROMAN_MAP[g] + r"\s*[级类]")
    return any(re.search(p, t) for p in pats)

def scan_source():
    import openpyxl
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    hit_old, hit_new, body = set(), set(), {}
    j = None
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            hdr = [("" if c is None else str(c).strip()) for c in row]
            j = hdr.index("病历ID") if "病历ID" in hdr else None
            continue
        vals = [("" if v is None else str(v)) for v in row]
        if j is None or j >= len(vals):
            continue
        mid = vals[j].strip()
        if not mid:
            continue
        text = " ".join(vals)
        body[mid] = text
        if RE_OLD.search(text):
            hit_old.add(mid)
        if pg.birads_in_text(text):
            hit_new.add(mid)
    return hit_old, hit_new, body

def cause_of(text):
    if not text:
        return "原文缺行"
    if not pg.birads_in_text(text):
        return "原文确无分级（待人工核）"
    if RE_ROMAN.search(text):
        return "BI-RADS+罗马数字"
    if pg.RE_BI_CLASS.search(text) and not pg.RE_BI_NUM.search(text):
        return "分类N（省略「类」字）"
    if pg.RE_BI_BARE.search(text) and not pg.RE_BI_NUM.search(text):
        return "裸写N类"
    return "BI-RADS+拼写残缺／汉字间隔"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="打印 N 条「原文确无分级」样例")
    a = ap.parse_args()

    for p in (TAR, SRC):
        if not os.path.exists(p):
            raise SystemExit("缺文件 %s" % p)

    has_unit, has_val, units = scan_struct()
    old, new, body = scan_source()

    U, A = has_unit, has_val & has_unit

    def four(B):
        return {
            "原文有_已给值": len(A & B),
            "原文有_记录无值": len((U - A) & B),
            "原文无_却给值": len(A - B),
            "两边都无": len(U - A - B),
        }

    f_old, f_new = four(old), four(new)

    miss = A - old
    cause = collections.Counter()
    detail = collections.defaultdict(list)
    for rid in miss:
        k = cause_of(body.get(rid, ""))
        cause[k] += 1
        detail[k].append(rid)

    print("记录级：有 BI-RADS 单元 %d ；给出可解析分级 %d" % (len(U), len(A)))
    print()
    print("%-22s %10s %10s" % ("四格", "旧判据", "新判据"))
    for k in ("原文有_已给值", "原文有_记录无值", "原文无_却给值", "两边都无"):
        print("%-22s %10d %10d" % (k, f_old[k], f_new[k]))
    print()
    print("旧判据漏掉的「产物给值」记录 %d 条，归因：" % len(miss))
    for k, v in cause.most_common():
        print("  %-28s %6d  (%.1f%%)" % (k, v, 100.0 * v / max(1, len(miss))))
    print()
    # ---- 值级回指：产物给的那个值，原文里找不找得到字面 ----
    v_ok, v_bad = [], []
    for rid, rtype, g, raw in units:
        (v_ok if value_in_text(g, body.get(rid, "")) else v_bad).append((rid, rtype, g, raw))
    nt = max(1, len(units))
    print()
    print("值级回指：有归一值的 BI-RADS 单元 %d 个" % len(units))
    print("  可回指 %d（%.2f%%）  不可回指 %d（%.2f%%）"
          % (len(v_ok), 100.0 * len(v_ok) / nt, len(v_bad), 100.0 * len(v_bad) / nt))
    by_g = collections.Counter(x[2] for x in v_bad)
    if by_g:
        print("  不可回指按分级：%s"
              % "、".join("%s 类 %d" % (k, v) for k, v in by_g.most_common()))
        print("  不可回指样例（前 6）：%s"
              % "；".join("%s[%s]=%s 原值「%s」" % (x[0], x[1], x[2], x[3]) for x in v_bad[:6]))

    for tag, f in (("旧", f_old), ("新", f_new)):
        den = f["原文有_已给值"] + f["原文有_记录无值"]
        print("%s判据记录级落地率：%d/%d = %.2f%%"
              % (tag, f["原文有_已给值"], den, 100.0 * f["原文有_已给值"] / max(1, den)))

    if a.show:
        key = "原文确无分级（待人工核）"
        pick = detail[key][:a.show]
        print()
        print("「%s」%d 条中前 %d 条：" % (key, cause[key], len(pick)))
        for rid in pick:
            print("  %s | %s" % (rid, body.get(rid, "")[:120].replace("\n", " ")))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "记录级_旧判据": f_old,
            "记录级_新判据": f_new,
            "旧判据_漏掉给值记录": len(miss),
            "归因": dict(cause),
            "值级回指": {
                "单元有归一值": len(units),
                "可回指": len(v_ok),
                "不可回指": len(v_bad),
                "可回指率": 100.0 * len(v_ok) / nt,
                "不可回指_按分级": dict(by_g),
                "不可回指_清单": [{"record_id": x[0], "report_type": x[1],
                                   "grade": x[2], "raw": x[3]} for x in v_bad],
            },
            "样例": {k: v[:50] for k, v in detail.items()},
            "判据": {
                "旧": RE_OLD.pattern,
                "新_BI": pg.RE_BI_NUM.pattern,
                "新_分类": pg.RE_BI_CLASS.pattern,
                "新_裸写": pg.RE_BI_BARE.pattern,
                "唯一来源": "probe_granularity.birads_in_text（本文件不另建副本）",
            },
        }, f, ensure_ascii=False, indent=1)
    print()
    print("✓ %s" % OUT)

if __name__ == "__main__":
    main()
