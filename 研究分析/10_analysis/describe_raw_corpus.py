import os
import json
import re
import time
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
OUT = ROOT + "/权威数据/原始数据描述性统计_20261027.json"

FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
FLAGS = {
    "超声": r"超声|B超|彩超",
    "钼靶/MG": r"钼靶|乳腺X|MG\b",
    "MRI": r"MRI|磁共振|核磁",
    "病理": r"病理",
    "免疫组化/IHC": r"免疫组化|IHC|ER|PR|HER2|Ki-?67",
    "化疗": r"化疗",
    "放疗": r"放疗|放射治疗",
    "内分泌治疗": r"内分泌治疗|他莫昔芬|托瑞米芬|来曲唑|阿那曲唑|依西美坦|戈舍瑞林",
    "靶向治疗": r"靶向|曲妥珠|帕妥珠|赫赛汀|吡咯替尼",
    "手术": r"手术|切除|根治|活检|穿刺",
    "新辅助": r"新辅助",
    "BI-RADS": r"BI-?RADS",
    "TNM/分期": r"TNM|分期",
    "月经婚育史": r"月经|绝经|婚育|G\dP\d",
}
NEG_RX = re.compile(r"^(无|未见|未见异常|无异常|未查|无特殊|／|-|—|/|无不适|同上|否认)[。，,.;；]?$")
AGE_RX = re.compile(r"(\d{1,3})\s*岁")
TIERS = [(0, 0, "0"), (1, 9, "1-9"), (10, 49, "10-49"), (50, 199, "50-199"),
         (200, 499, "200-499"), (500, 999, "500-999"), (1000, 10 ** 9, ">=1000")]

def s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()

def q(arr, p):
    if not arr:
        return 0
    a = sorted(arr)
    return a[min(int(round(p * (len(a) - 1))), len(a) - 1)]

def tier_of(L):
    for lo, hi, name in TIERS:
        if lo <= L <= hi:
            return name
    return ">=1000"

def scan():
    import openpyxl
    t0 = time.time()
    wb = openpyxl.load_workbook(SRC, read_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(min_row=1, values_only=True)
    hdr = [s(c) for c in next(it)]
    ix = {h: i for i, h in enumerate(hdr)}
    ci = [ix[f] for f in FIELDS]
    i_rid, i_pid, i_sex, i_date = ix["病历ID"], ix["登记号"], ix["性别"], ix["就诊日期"]
    i_type, i_level, i_dept, i_doc = ix["病历类型"], ix["就诊次数级别"], ix["医生所属科室"], ix["就诊医生"]
    i_cx, i_next, i_zy, i_dx1 = ix["诊断复杂度"], ix["下一步处理"], ix["住院"], ix["诊断1"]
    i_dx = [ix["诊断%d" % k] for k in range(1, 15) if "诊断%d" % k in ix]
    i_ord = [ix["医嘱"]] + [ix["医嘱%d" % k] for k in range(1, 61) if "医嘱%d" % k in ix]
    flag_re = {k: re.compile(v) for k, v in FLAGS.items()}

    agg = dict(lens=defaultdict(list), tot=[], age=[], ndx=[], nord=[])
    cnt = dict(sex=Counter(), typ=Counter(), lvl=Counter(), dept=Counter(), doc=Counter(),
               cx=Counter(), next=Counter(), zy=Counter(), ym=Counter(), year=Counter(),
               dxn=Counter(), ordn=Counter(), dx1=Counter(), ordtop=Counter(), cat=Counter(),
               flags=Counter(), neg=Counter(), ticket=Counter(), tot_tier=Counter())
    field_tier = {f: Counter() for f in FIELDS}
    pids = Counter()
    rids = set()
    blank = []
    age_hit = 0
    n = 0
    for row in it:
        n += 1
        rid = s(row[i_rid])
        texts = [s(row[c]) for c in ci]
        if not rid and not any(texts):
            blank.append(n)
            continue
        rids.add(rid)
        pids[s(row[i_pid])] += 1
        cnt["sex"][s(row[i_sex])] += 1
        cnt["typ"][s(row[i_type])] += 1
        cnt["lvl"][s(row[i_level])] += 1
        cnt["dept"][s(row[i_dept])] += 1
        cnt["doc"][s(row[i_doc])] += 1
        d = s(row[i_date])
        if len(d) >= 10:
            cnt["ym"][d[:7]] += 1
            cnt["year"][d[:4]] += 1
        total = 0
        for f, t in zip(FIELDS, texts):
            L = len(t)
            total += L
            if L <= 10:
                cnt["ticket"][f] += 1
            if NEG_RX.match(t):
                cnt["neg"][f] += 1
            agg["lens"][f].append(L)
            field_tier[f][tier_of(L)] += 1
        agg["tot"].append(total)
        cnt["tot_tier"][tier_of(total)] += 1
        blob = "\n".join(texts)
        for k, rx in flag_re.items():
            if rx.search(blob):
                cnt["flags"][k] += 1
        dxs = [s(row[c]) for c in i_dx if s(row[c])]
        agg["ndx"].append(len(dxs))
        cnt["dxn"][len(dxs)] += 1
        d1 = s(row[i_dx1])
        if d1:
            cnt["dx1"][d1] += 1
        if "癌" in d1 or "恶性" in d1:
            cnt["cat"]["恶性/癌"] += 1
        elif "结节" in d1:
            cnt["cat"]["结节"] += 1
        elif "增生" in d1 or "囊性" in d1:
            cnt["cat"]["增生/囊性"] += 1
        elif "纤维腺瘤" in d1 or "纤维瘤" in d1:
            cnt["cat"]["良性肿瘤"] += 1
        elif "炎" in d1:
            cnt["cat"]["炎症"] += 1
        elif not d1:
            cnt["cat"]["(无诊断)"] += 1
        else:
            cnt["cat"]["其他"] += 1
        cnt["cx"][s(row[i_cx])] += 1
        cnt["next"][s(row[i_next])] += 1
        cnt["zy"][s(row[i_zy])] += 1
        ords = []
        for j, c in enumerate(i_ord):
            t = s(row[c])
            if not t or j == 0:
                continue
            ords.append(t)
            cnt["ordtop"][t] += 1
        agg["nord"].append(len(ords))
        cnt["ordn"][len(ords)] += 1
        m = None
        for t in texts[:2]:
            for mm in AGE_RX.finditer(t):
                a = int(mm.group(1))
                if 10 <= a <= 100:
                    m = a
                    break
            if m:
                break
        if m:
            age_hit += 1
            agg["age"].append(m)

    valid = n - len(blank)
    cx_bad = sum(v for k, v in cnt["cx"].items() if not re.fullmatch(r"level_\d", k))
    nx_bad = sum(1 for k, v in cnt["next"].items() if re.fullmatch(r"\d{10,}", k) for _ in range(v))
    vpp = Counter(pids.values())
    res = {
        "meta": {
            "source_file": "数据源/乳腺疾病门诊记录_20240915.xlsx",
            "sheet": "Sheet1",
            "raw_rows_with_header": n,
            "raw_data_rows": n,
            "blank_rows": len(blank),
            "blank_rows_contiguous": (max(blank) - min(blank) + 1) == len(blank) if blank else True,
            "blank_first_row": min(blank) if blank else None,
            "blank_last_row": max(blank) if blank else None,
            "valid_records": valid,
            "unique_record_id": len(rids),
            "unique_patients": len(pids),
            "unique_doctors": len([k for k in cnt["doc"] if k]),
            "n_columns": len(hdr),
            "columns": hdr,
            "elapsed_s": round(time.time() - t0, 1),
        },
        "time": {"date_min": min(cnt["ym"]) if cnt["ym"] else "", "date_max": max(cnt["ym"]) if cnt["ym"] else "",
                 "year": dict(sorted(cnt["year"].items())), "year_month": dict(sorted(cnt["ym"].items()))},
        "demographics": {"sex": dict(cnt["sex"]), "visits_per_patient": {
            "median": q(list(pids.values()), .5), "p90": q(list(pids.values()), .9),
            "max": max(pids.values()) if pids else 0,
            "single_visit_pct": round(100 * sum(1 for v in pids.values() if v == 1) / max(len(pids), 1), 1),
            "ge10_visits_pct": round(100 * sum(1 for v in pids.values() if v >= 10) / max(len(pids), 1), 1)}},
        "encounter": {"record_type": dict(cnt["typ"]), "visit_level": dict(cnt["lvl"]),
                      "department": dict(cnt["dept"]), "doctor_top10": dict(cnt["doc"].most_common(10)),
                      "hospitalization": dict(cnt["zy"])},
        "text_length": {
            "total_chars": sum(agg["tot"]),
            "record_total": {"mean": round(sum(agg["tot"]) / valid, 1), "p25": q(agg["tot"], .25),
                             "median": q(agg["tot"], .5), "p75": q(agg["tot"], .75), "p90": q(agg["tot"], .9),
                             "p99": q(agg["tot"], .99), "max": max(agg["tot"])},
            "record_total_tiers": {k: [v, round(100 * v / valid, 1)] for k, v in cnt["tot_tier"].items()},
            "by_field": {f: {"mean": round(sum(v) / len(v), 1), "p25": q(v, .25), "median": q(v, .5),
                             "p75": q(v, .75), "p90": q(v, .9), "p99": q(v, .99), "max": max(v),
                             "le10_pct": round(100 * cnt["ticket"][f] / valid, 1),
                             "near_empty_pct": round(100 * cnt["neg"][f] / valid, 1),
                             "tiers": {k: round(100 * n2 / valid, 1) for k, n2 in field_tier[f].items()}}
                         for f, v in agg["lens"].items()},
        },
        "diagnosis": {"n_per_record": dict(sorted(cnt["dxn"].items())),
                      "median_per_record": q(agg["ndx"], .5), "mean_per_record": round(sum(agg["ndx"]) / valid, 2),
                      "unique_dx1_texts": len(cnt["dx1"]),
                      "dx1_top25": dict(cnt["dx1"].most_common(25)),
                      "dx1_coarse_category": dict(cnt["cat"].most_common())},
        "orders": {"total_items": sum(agg["nord"]), "median_per_record": q(agg["nord"], .5),
                   "mean_per_record": round(sum(agg["nord"]) / valid, 2),
                   "n_per_record": {k: v for k, v in sorted(cnt["ordn"].items()) if k <= 20},
                   "unique_order_texts": len(cnt["ordtop"]),
                   "top25": dict(cnt["ordtop"].most_common(25))},
        "content_flags": dict(cnt["flags"].most_common()),
        "age_extractable": {"n": age_hit, "pct": round(100 * age_hit / valid, 3),
                            "median": q(agg["age"], .5) if agg["age"] else None},
        "data_quality": {
            "blank_tail_rows": len(blank),
            "diagnosis_complexity_nonconforming_rows": cx_bad,
            "next_step_serial_rows": nx_bad,
            "next_step_note": "下一步处理列存在 10 位以上纯数字串（导出串号），不可直接使用",
        },
    }
    return res

if __name__ == "__main__":
    r = scan()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    m = r["meta"]
    print("valid=%d patients=%d doctors=%d blank=%d elapsed=%ss -> %s"
          % (m["valid_records"], m["unique_patients"], m["unique_doctors"],
             m["blank_rows"], m["elapsed_s"], OUT))
