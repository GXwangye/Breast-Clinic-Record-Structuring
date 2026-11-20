#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
from collections import Counter, defaultdict
from datetime import date
import openpyxl

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
OUT = ROOT + "/权威数据/原始数据基本面_20261027.json"

wb = openpyxl.load_workbook(SRC, read_only=True)
ws = wb[wb.sheetnames[0]]
it = ws.iter_rows(min_row=1, values_only=True)
hdr = [("" if c is None else str(c).strip()) for c in next(it)]
ix = {h: i for i, h in enumerate(hdr)}
i_rid, i_pid, i_dt, i_typ, i_dept = ix["病历ID"], ix["登记号"], ix["就诊日期"], ix["病历类型"], ix["医生所属科室"]
F = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
fi = [ix[f] for f in F]

field_chars = Counter()
rows = 0
recs = []                      # (pid, date, type, dept, total, aux)
pt_days = defaultdict(set)
pt_dates = defaultdict(list)
pt_span_ok_aux = defaultdict(int)  # 含长影像报告的记录数
visitday_recs = Counter()

for row in it:
    rid = "" if row[i_rid] is None else str(row[i_rid]).strip()
    if not rid:
        continue
    rows += 1
    pid = "" if row[i_pid] is None else str(row[i_pid]).strip()
    dt = ("" if row[i_dt] is None else str(row[i_dt]).strip())[:10]
    typ = "" if row[i_typ] is None else str(row[i_typ]).strip()
    dept = "" if row[i_dept] is None else str(row[i_dept]).strip()
    lens = {}
    for f, c in zip(F, fi):
        v = "" if row[c] is None else str(row[c]).strip()
        lens[f] = len(v)
        field_chars[f] += len(v)
    pt_days[pid].add(dt)
    pt_dates[pid].append(dt)
    if lens["辅助检查"] >= 200:
        pt_span_ok_aux[pid] += 1
    visitday_recs[(pid, dt)] += 1
    recs.append({
        "pid": pid, "dt": dt, "typ": typ, "dept": dept,
        "total": sum(lens.values()), "aux": lens["辅助检查"],
    })

# ---------- 1. 信息承载 ----------
total_chars = sum(field_chars.values())
info_share = {f: {"chars": field_chars[f], "share_pct": round(100 * field_chars[f] / total_chars, 2)} for f in F}

# ---------- 2. 就诊日口径 ----------
vd_dist = Counter(visitday_recs.values())
n_visitdays = len(visitday_recs)
vd_multi = sum(1 for v in visitday_recs.values() if v > 1)
vd_multi_rec = sum(v for v in visitday_recs.values() if v > 1)

# 每患者就诊日数分布
def bucket(n):
    if n == 1: return "1"
    if n == 2: return "2"
    if n == 3: return "3"
    if n <= 5: return "4-5"
    if n <= 10: return "6-10"
    if n <= 20: return "11-20"
    if n <= 30: return "21-30"
    return ">30"

day_bucket = Counter(bucket(len(v)) for v in pt_days.values())

# ---------- 3. 纵向可观察性 ----------
spans, spans_ok = [], []
for pid, ds in pt_dates.items():
    ds = sorted(x for x in ds if x)
    if not ds:
        continue
    try:
        a = date(*map(int, ds[0].split("-")))
        b = date(*map(int, ds[-1].split("-")))
    except Exception:
        continue
    spans.append((b - a).days)
spans.sort()
person_years = round(sum(spans) / 365.25, 1)
n_pat = len(pt_days)
def q(a, p):
    return a[min(len(a) - 1, int(p * len(a)))]
longi = {
    "patients": n_pat,
    "person_years": person_years,
    "span_days_median": q(spans, .5), "span_p25": q(spans, .25), "span_p75": q(spans, .75),
    "span_max": spans[-1],
    "ge90d": sum(1 for s in spans if s >= 90),
    "ge180d": sum(1 for s in spans if s >= 180),
    "ge365d": sum(1 for s in spans if s >= 365),
    "span_eq0": sum(1 for s in spans if s == 0),
}
longi["ge90d_pct"] = round(100 * longi["ge90d"] / n_pat, 1)
longi["ge180d_pct"] = round(100 * longi["ge180d"] / n_pat, 1)
longi["ge365d_pct"] = round(100 * longi["ge365d"] / n_pat, 1)

# 可做纵向配对：>=2 就诊日 且 至少 2 次含长影像报告(辅助检查>=200字)
pair_aux = sum(1 for pid in pt_days if len(pt_days[pid]) >= 2 and pt_span_ok_aux.get(pid, 0) >= 2)

# ---------- 4. 长文本（影像报告）可得性：仅按长度，不按术语 ----------
has_long_aux = sum(1 for pid, n in pt_span_ok_aux.items() if n >= 1)

# ---------- 5. 记录长度 ----------
tot = sorted(r["total"] for r in recs)
aux = sorted(r["aux"] for r in recs)
rec_shape = {
    "total_median": q(tot, .5), "total_p90": q(tot, .9), "total_max": tot[-1],
    "aux_median": q(aux, .5), "aux_p90": q(aux, .9),
    "aux_ge200": sum(1 for r in recs if r["aux"] >= 200),
    "aux_lt50": sum(1 for r in recs if r["aux"] < 50),
}
rec_shape["aux_ge200_pct"] = round(100 * rec_shape["aux_ge200"] / rows, 1)
rec_shape["aux_lt50_pct"] = round(100 * rec_shape["aux_lt50"] / rows, 1)

res = {
    "meta": {"source": SRC, "records": rows, "patients": n_pat, "visit_days": n_visitdays,
             "total_chars": total_chars},
    "info_share": info_share,
    "visitday": {"n_visitdays": n_visitdays, "multi_record_days": vd_multi,
                 "records_in_multi_days": vd_multi_rec,
                 "extra_records": vd_multi_rec - vd_multi,
                 "extra_pct": round(100 * (vd_multi_rec - vd_multi) / rows, 2),
                 "per_day_dist": dict(sorted(vd_dist.items())),
                 "patient_day_bucket": dict(day_bucket)},
    "longitudinal": dict(longi, pair_with_2long_aux=pair_aux),
    "long_text": {"patient_with_long_aux": has_long_aux,
                  "patient_with_long_aux_pct": round(100 * has_long_aux / n_pat, 1)},
    "record_shape": rec_shape,
    "note": "不含任何关键词/术语层面的内容统计（术语未统一，命中率不可引用）",
}

with open(OUT, "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)

print("records", rows, "patients", n_pat, "visitdays", n_visitdays)
print("info_share", json.dumps(info_share, ensure_ascii=False))
print("visitday", json.dumps(res["visitday"], ensure_ascii=False))
print("longi", json.dumps(res["longitudinal"], ensure_ascii=False))
print("long_text", json.dumps(res["long_text"], ensure_ascii=False))
print("rec_shape", json.dumps(rec_shape, ensure_ascii=False))
print("written", OUT)
