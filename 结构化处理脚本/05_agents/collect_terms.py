# -*- coding: utf-8 -*-

import argparse
import collections
import glob
import json
import os
import re
import time
from multiprocessing import Pool

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
SRC = DATA + "/中间产物/assembled_v7"
OUT = DATA + "/中间产物/_norm"

LOINC_RE = re.compile(r"^\d{1,6}-\d$")

# 剂型后缀：kind 缺字段（82,380 条）时兜底识别药名，避免 drug 域漏抽约四成。
DRUG_FORM_RE = re.compile(
    r"(片|胶囊|注射液|注射用|颗粒|口服液|合剂|软膏|乳膏|凝胶|散|丸|滴眼液|"
    r"喷雾剂|气雾剂|贴剂|栓|洗剂|酊|溶液|混悬液|乳|糖浆|滴剂|擦剂|膜|植入剂|微球)$"
    r"|^注射用|^灭菌注射用水")

DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "rad_exam",
           "rad_enum", "drug"]

DRUG_KINDS = {"药品", "西药", "中成药", "中草药"}
SURG_MODALITIES = {"手术", "手术治疗"}

SAMPLE_PER_TERM = 3          # 每个术语保留几条来源记录号（便于回查原文）

def _blank():
    return {d: {} for d in DOMAINS}

def _slot(M, domain, literal):
    t = M[domain].get(literal)
    if t is None:
        t = M[domain][literal] = [0, [], 0, None, {}]
    return t

def _scalar(x):
    if isinstance(x, str):
        return x.strip() or None
    if isinstance(x, (int, float)):
        return str(x)
    return None

def _add(M, domain, literal, rid, code=None, level=None):
    if not isinstance(literal, str):
        return
    s = literal.strip()
    if not s:
        return
    code = _scalar(code)
    level = _scalar(level)
    t = _slot(M, domain, s)
    t[0] += 1
    if len(t[1]) < SAMPLE_PER_TERM:
        t[1].append(rid)
    if code:
        t[2] += 1
        if t[3] is None:
            t[3] = code
    if level:
        t[4][level] = t[4].get(level, 0) + 1

def _dic(x):
    return x if isinstance(x, dict) else {}

def _lst(x):
    return x if isinstance(x, list) else []

def _walk_dx(M, d, rid):
    for it in _lst(_dic(d.get("diagnoses")).get("items")):
        if isinstance(it, dict):
            _add(M, "icd10_dx", it.get("name"), rid,
                 code=it.get("icd_yb_code"), level=it.get("icd_level"))

def _walk_past(M, d, rid):
    st = _dic(_dic(d.get("past_history")).get("structured"))
    for it in _lst(st.get("comorbidities")):
        if isinstance(it, dict):
            _add(M, "icd10_comorb", it.get("name"), rid, code=it.get("icd_yb_code"))
        elif isinstance(it, str):
            _add(M, "icd10_comorb", it, rid)
    for it in _lst(st.get("surgeries")):
        if isinstance(it, dict):
            _add(M, "icd9_surgery", it.get("name") or it.get("text"), rid,
                 code=it.get("icd9_code") or it.get("code"))
        elif isinstance(it, str):
            _add(M, "icd9_surgery", it, rid)

def _walk_treat(M, d, rid):
    for it in _lst(_dic(d.get("treatment")).get("items")):
        if isinstance(it, dict) and _scalar(it.get("modality")) in SURG_MODALITIES:
            _add(M, "icd9_surgery", it.get("text"), rid)

def _walk_labs(M, d, rid):
    for lab in _lst(_dic(d.get("auxiliary_exam")).get("labs")):
        if not isinstance(lab, dict):
            continue
        for r in _lst(lab.get("results")):
            if isinstance(r, dict):
                _add(M, "loinc_lab", r.get("name"), rid,
                     code=r.get("code") or r.get("loinc_code"))

def _walk_reports(M, d, rid):
    ax = _dic(d.get("auxiliary_exam"))
    for rp in _lst(ax.get("reports")):
        if not isinstance(rp, dict):
            continue
        _add(M, "rad_exam", rp.get("exam_name"), rid, code=rp.get("loinc_code"))
        _add(M, "rad_exam", rp.get("exam_name_alt"), rid, code=rp.get("loinc_code"))
        # 枚举归一用的上下文：同一个项目名在各记录里被标成什么 report_type / body_site。
        # 多数派就是规则层的直接判据，不必调 LLM。
        combo = "%s|%s" % (_scalar(rp.get("report_type")) or "∅",
                           _scalar(rp.get("body_site")) or "∅")
        _add(M, "rad_enum", rp.get("exam_name"), rid, level=combo)
    for g in _lst(ax.get("report_groups")):
        if not isinstance(g, dict):
            continue
        for rp in _lst(g.get("reports")):
            if isinstance(rp, dict):
                _add(M, "rad_exam", rp.get("exam_name"), rid, code=rp.get("loinc_code"))
                combo = "%s|%s" % (_scalar(rp.get("report_type")) or "∅",
                                   _scalar(rp.get("body_site")) or "∅")
                _add(M, "rad_enum", rp.get("exam_name"), rid, level=combo)

def _walk_orders(M, d, rid):
    for it in _lst(_dic(d.get("orders")).get("items")):
        if not isinstance(it, dict):
            continue
        nm = it.get("name") or it.get("drug_name")
        kind = _scalar(it.get("kind"))
        if kind in DRUG_KINDS:
            _add(M, "drug", nm, rid)
        elif isinstance(nm, str) and DRUG_FORM_RE.search(nm.strip()):
            _add(M, "drug", nm, rid)
            _probe(M, "drug_backfill", str(kind or "∅")[:12])

# ---- 结构诊断：同一次扫描顺手统计，避免再扫 8 万文件的第二遍 ----
PROBE_KEYS = ["orders_kind", "report_type", "lab_code_shape", "dx_level", "lab_code_empty"]

def _probe(M, key, val):
    p = M.setdefault("__probe__", {})
    p[key] = p.get(key, {})
    p[key][val] = p[key].get(val, 0) + 1

def _walk_probe(M, d):
    for it in _lst(_dic(d.get("orders")).get("items")):
        if isinstance(it, dict):
            _probe(M, "orders_kind", str(it.get("kind") or "∅")[:20])
    ax = _dic(d.get("auxiliary_exam"))
    reps = list(_lst(ax.get("reports")))
    for g in _lst(ax.get("report_groups")):
        if isinstance(g, dict):
            reps.extend(_lst(g.get("reports")))
    for rp in reps:
        if isinstance(rp, dict):
            # 不截断：这些就是 B 类枚举要清洗的**全部变体清单**，截断会漏掉长写法
            _probe(M, "report_type", str(rp.get("report_type") or "∅"))
            _probe(M, "body_site", str(rp.get("body_site") or "∅"))
            _probe(M, "laterality", str(rp.get("laterality") or "∅"))
    for lab in _lst(ax.get("labs")):
        if not isinstance(lab, dict):
            continue
        for r in _lst(lab.get("results")):
            if not isinstance(r, dict):
                continue
            c = _scalar(r.get("code")) or ""
            if not c:
                _probe(M, "lab_code_empty", "空")
            elif LOINC_RE.match(c):
                _probe(M, "lab_code_shape", "LOINC形")
            else:
                _probe(M, "lab_code_shape", "本地缩写")
    for it in _lst(_dic(d.get("diagnoses")).get("items")):
        if isinstance(it, dict):
            _probe(M, "dx_level", str(it.get("icd_level") or "∅")[:12])

def scan_one(args):
    paths, = args
    M = _blank()
    ok = 0
    bad = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            bad.append(os.path.basename(p)[:-5])
            continue
        rid = os.path.basename(p)[:-5]
        try:
            _walk_dx(M, d, rid)
            _walk_past(M, d, rid)
            _walk_treat(M, d, rid)
            _walk_labs(M, d, rid)
            _walk_reports(M, d, rid)
            _walk_orders(M, d, rid)
            _walk_probe(M, d)
        except Exception:
            bad.append(rid)
            continue
        ok += 1
    return ok, bad, M

def merge(dst, src):
    for dom in DOMAINS:
        for lit, rec in src[dom].items():
            t = _slot(dst, dom, lit)
            t[0] += rec[0]
            if len(t[1]) < SAMPLE_PER_TERM:
                t[1].extend(rec[1][:SAMPLE_PER_TERM - len(t[1])])
            t[2] += rec[2]
            if t[3] is None:
                t[3] = rec[3]
            for k, v in rec[4].items():
                t[4][k] = t[4].get(k, 0) + v
    sp = src.get("__probe__") or {}
    dp = dst.setdefault("__probe__", {})
    for key, cnt in sp.items():
        d0 = dp.setdefault(key, {})
        for k, v in cnt.items():
            d0[k] = d0.get(k, 0) + v

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    files = sorted(glob.glob(SRC + "/*.json"))
    if a.limit:
        files = files[:a.limit]
    print("files = %d  workers = %d" % (len(files), a.workers), flush=True)

    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    # 切成比 workers 多几倍的片，避免某片特别慢拖尾
    nchunk = max(a.workers * 4, 8)
    size = (len(files) + nchunk - 1) // nchunk
    chunks = [(files[i:i + size],) for i in range(0, len(files), size)]

    M = _blank()
    ok = 0
    dirty = []
    if a.workers <= 1:
        for i, c in enumerate(chunks, 1):
            n, bad, part = scan_one(c)
            ok += n
            dirty.extend(bad)
            merge(M, part)
            print("…chunk %d/%d  ok=%d  dirty=%d  %.0fs"
                  % (i, len(chunks), ok, len(dirty), time.time() - t0), flush=True)
    else:
        with Pool(a.workers) as pool:
            for i, (n, bad, part) in enumerate(pool.imap_unordered(scan_one, chunks), 1):
                ok += n
                dirty.extend(bad)
                merge(M, part)
                print("…chunk %d/%d  ok=%d  dirty=%d  %.0fs"
                      % (i, len(chunks), ok, len(dirty), time.time() - t0), flush=True)

    print("\n===== 汇总 =====")
    for dom in DOMAINS:
        items = sorted(M[dom].items(), key=lambda kv: -kv[1][0])
        p = os.path.join(a.out, "terms_%s.jsonl" % dom)
        with open(p, "w", encoding="utf-8") as f:
            for lit, rec in items:
                f.write(json.dumps({
                    "literal": lit, "count": rec[0], "records": rec[1],
                    "with_code": rec[2], "code_sample": rec[3],
                    "levels": rec[4],
                }, ensure_ascii=False) + "\n")
        occ = sum(r[0] for r in M[dom].values())
        wc = sum(r[2] for r in M[dom].values())
        print("  %-12s 唯一 %6d  出现 %8d  已带码 %7d (%5.1f%%)  -> %s"
              % (dom, len(items), occ, wc, 100.0 * wc / occ if occ else 0,
                 os.path.basename(p)))
    print("  收录文件 %d，用时 %.0fs" % (ok, time.time() - t0))
    if dirty:
        dp = os.path.join(a.out, "dirty_records.txt")
        with open(dp, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(set(dirty))))
        print("  !! 结构异常跳过 %d 条（去重 %d），清单 %s"
              % (len(dirty), len(set(dirty)), os.path.basename(dp)))

    probe = M.get("__probe__") or {}
    if probe:
        pp = os.path.join(a.out, "structure_probe.json")
        with open(pp, "w", encoding="utf-8") as f:
            json.dump({k: dict(sorted(v.items(), key=lambda kv: -kv[1]))
                       for k, v in probe.items()}, f, ensure_ascii=False, indent=1)
        print("\n===== 结构诊断 -> %s =====" % os.path.basename(pp))
        for k, v in probe.items():
            top = sorted(v.items(), key=lambda kv: -kv[1])[:8]
            print("  %-16s %s" % (k, top))

if __name__ == "__main__":
    main()
