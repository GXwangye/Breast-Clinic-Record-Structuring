#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import io
import csv
import json
import random
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"

sys.path.insert(0, os.path.join(ROOT, "研究分析", "20_qa"))
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "00_core"))
sys.path.insert(0, os.path.join(ROOT, "研究分析", "90_qa_report"))
sys.path.insert(0, os.path.join(ROOT, "研究分析"))

# 复用 v1 的取证与判据函数——项目内判据只留一份，v2 不写副本
from build_review_pack import (                       # noqa: E402
    iter_records, TAR, s, items_of, tnm_entries, ctx_sentence,
    panel_evidence, marker_key, marker_window, TARGET_MARKERS, collect, read_source,
    rows_stage, rows_sub, rows_visit, visit_kind, malig_kind,
    norm_stage, classify_subtype,
)

OUT_DIR = DATA + "/qa_reports/审核包_20261113"

SEED = 20261113          # 预注册
N_KIND = 100             # 记录数
N_CALL = 100             # 记录数
N_STAGE = 60
N_SUB = 100
N_VISIT = 100

N_KIND_REP = 25          # 富集：条目名含「报告」
N_CALL_ISH = 20          # 富集：HER2 带 ISH／扩增文字
N_CALL_RANGE = 15        # 富集：Ki-67 带区间

MAX_K_ITEMS = 8          # 单块最多列出的条目数（超出按首次出现截断，防单块过长）

def scan_pools():
    pool = {k: [] for k in ("K", "C", "S", "M", "V", "K_rep", "C_ish", "C_range")}
    n = 0
    for fn, rec in iter_records(TAR):
        rid = s(rec.get("record_id")) or fn.rsplit(".", 1)[0]
        n += 1
        pool["V"].append(rid)

        its = [it for it in items_of(rec.get("orders")) if s(it.get("kind"))]
        if its:
            pool["K"].append(rid)
            if any("报告" in s(it.get("name")) for it in its):
                pool["K_rep"].append(rid)

        ihc = (rec.get("auxiliary_exam") or {}).get("ihc") or {}
        panels = [p for p in (ihc.get("panels") or []) if isinstance(p, dict)]
        hit = ish = rng = False
        for p in panels:
            for m in p.get("markers") or []:
                if not isinstance(m, dict):
                    continue
                nm = s(m.get("marker_name")).upper().replace("-", "").replace(" ", "")
                if nm not in TARGET_MARKERS:
                    continue
                if not (s(m.get("call")) or m.get("percent") is not None):
                    continue
                hit = True
                raw = s(m.get("raw_value"))
                if nm == "HER2" and any(t in raw for t in ("扩增", "FISH", "ISH", "原位杂交")):
                    ish = True
                if nm in ("KI67", "KI-67", "KI 67") and ("-" in raw or "~" in raw
                                                         or "～" in raw or "—" in raw):
                    rng = True
        if hit:
            pool["C"].append(rid)
        if ish:
            pool["C_ish"].append(rid)
        if rng:
            pool["C_range"].append(rid)

        has_stage = False
        for d in items_of(rec.get("diagnoses")):
            for e in tnm_entries(d):
                if norm_stage(e.get("stage")):
                    has_stage = True
        if has_stage:
            pool["S"].append(rid)

        if any(classify_subtype(p)[0] for p in panels):
            pool["M"].append(rid)
    return pool, n

def draw(pool, k, rng, exclude=()):
    ids = sorted(set(pool) - set(exclude))
    return rng.sample(ids, min(k, len(ids)))

def pick_panel(panels):
    best, bn = None, -1
    for p in panels:
        n = sum(1 for m in p["markers"] if marker_key(m["name"]) in TARGET_MARKERS
                and (m["call"] or m["percent"] is not None))
        if n > bn:
            best, bn = p, n
    return best if bn > 0 else None

def rows_kind_block(rids, det, src, ans, layer_of):
    rows = []
    for bi, rid in enumerate(rids, 1):
        sr = src.get(rid) or {}
        ord_text = s(sr.get("ord_text")) or ""
        ord_items = [s(x) for x in (sr.get("ord_items") or []) if s(x)]
        body = ord_text or "；".join(ord_items)
        if not body:
            body = " ".join([s(sr.get("pi")), s(sr.get("aux")), s(sr.get("past"))])
        head = ("【本记录医嘱原文】" + body[:1500]) if body else "（原文医嘱列为空）"

        seen, items = set(), []
        for nm, kind, sf in det[rid]["orders"]:
            key = nm.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append((nm, kind))
        items = items[:MAX_K_ITEMS]
        blk = "K-%04d" % bi
        lay = layer_of.get(rid, "主层")
        for i, (nm, kind) in enumerate(items):
            ctx = head if i == 0 else "（同块，见首行）"
            loc = ctx_sentence(body, nm, cap=80) if nm else ""
            sn = "K-%04d-%02d" % (bi, i + 1)
            rows.append({
                "块号": blk, "样本号": sn, "层": lay, "字段": "kind（医嘱七桶）",
                "记录ID": rid, "待判对象": nm, "原文片段": ctx,
                "定位": loc or "（该名称未在原文直接出现）",
                "待判问题": "该条目属哪一桶？填：药品／检查／检验／操作／费用／材料／其他",
                "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": sn, "块号": blk, "层": lay, "字段": "kind",
                        "记录ID": rid, "待判对象": nm, "产物值": kind,
                        "对齐": "原文未匹配" if not loc else "已对齐"})
    return rows

def rows_call_block(rids, det, src, ans, layer_of):
    order = ["ER", "PR", "HER2", "KI67"]
    rows = []
    bi = 0
    for rid in rids:
        p = pick_panel(det[rid]["panels"])
        if p is None:
            continue
        bi += 1
        blk = "C-%04d" % bi
        lay = layer_of.get(rid, "主层")
        # 块首原文：panel 自己的 quote 优先，缺则按标志物同现定位
        ctx0 = ""
        for key in order:
            for m in p["markers"]:
                if marker_key(m["name"]) == key:
                    ctx0 = panel_evidence(p, src.get(rid) or {}, m["name"], key)
                    break
            if ctx0 and not ctx0.startswith("【全文未出现"):
                break
        ctx0 = ctx0 or "（本份报告无可定位原文）"
        sel, seen = [], set()
        for key in order:
            for m in p["markers"]:
                if marker_key(m["name"]) != key or key in seen:
                    continue
                if not (m["call"] or m["percent"] is not None):
                    continue
                seen.add(key)
                sel.append((key, m))
        for i, (key, m) in enumerate(sel):
            sn = "C-%04d-%02d" % (bi, i + 1)
            # 定位列必须给出本标志物在原文中的上下文：块首原文可到 700 字，
            # 只写「本块第 x 项」等于让专家自己在长文里找（预审 C-0001 即如此）。
            loc = marker_window(ctx0, key, before=40, after=90).replace("\n", " ") \
                or ("本块第 %d 项" % (i + 1))
            q = ("原文给出的 Ki-67 数值是多少？填百分数（如 30%）；原文写区间照抄（如 40%-60%）；"
                 "未写数值填「未写」") if key == "KI67" else \
                "按 ASCO-CAP 该标志物应判为？填：阳性／阴性／不确定／不适用"
            rows.append({
                "块号": blk, "样本号": sn, "层": lay, "字段": "call（免疫组化判读）",
                "记录ID": rid, "待判对象": m["name"],
                "原文片段": ctx0 if i == 0 else "（同块，见首行）",
                "定位": "…" + loc + "…",
                "待判问题": q, "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": sn, "块号": blk, "层": lay, "字段": "call",
                        "记录ID": rid, "待判对象": m["name"],
                        "产物值": ("percent=%s" % m["percent"]) if key == "KI67" else m["call"],
                        "对齐": "已对齐"})
    return rows

def stamp_block(rows, prefix, det):
    out = []
    for i, r in enumerate(rows, 1):
        r = dict(r)
        r["块号"] = "%s-%04d" % (prefix, i)
        r["层"] = "主层"
        r["定位"] = "—"
        out.append(r)
    return out

COLS = ["块号", "样本号", "层", "字段", "记录ID", "待判对象", "原文片段", "定位",
        "待判问题", "判定", "错因", "备注"]

def write_csv(path, rows):
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

def main():
    dry = "--dry-run" in sys.argv
    rng = random.Random(SEED)
    pool, n_rec = scan_pools()
    print("记录 %d；候选池 %s" % (n_rec, {k: len(set(v)) for k, v in pool.items()}))

    ids = {
        "K": draw(pool["K"], N_KIND, rng),
        "C": draw(pool["C"], N_CALL, rng),
        "S": draw(pool["S"], N_STAGE, rng),
        "M": draw(pool["M"], N_SUB, rng),
        "V": draw(pool["V"], N_VISIT, rng),
    }
    # 富集层：只从主层未抽中的记录里取，保证是新增信息
    ech = {
        "K_rep": draw(pool["K_rep"], N_KIND_REP, rng, exclude=ids["K"]),
        "C_ish": draw(pool["C_ish"], N_CALL_ISH, rng, exclude=ids["C"]),
        "C_range": draw(pool["C_range"], N_CALL_RANGE, rng, exclude=ids["C"] + pool["C_ish"]),
    }
    layer_of = {}
    for rid in ids["K"] + ids["C"]:
        layer_of[rid] = "主层"
    for rid in ech["K_rep"]:
        layer_of[rid] = "富集"
    for rid in ech["C_ish"] + ech["C_range"]:
        layer_of[rid] = "富集"

    all_ids = sorted({x for v in list(ids.values()) + list(ech.values()) for x in v})
    print("抽中记录 %d；主层 %s；富集 %s" % (
        len(all_ids), {k: len(v) for k, v in ids.items()},
        {k: len(v) for k, v in ech.items()}))
    if dry:
        return

    det = collect(all_ids)
    src = read_source(all_ids)
    print("明细 %d 条，原文命中 %d 条" % (len(det), len(src)))
    miss = [x for x in all_ids if x not in src]
    if miss:
        print("⚠ 有明细无原文：", miss[:5], "共", len(miss))

    ans, rows = [], []
    rows += rows_kind_block(ids["K"] + ech["K_rep"], det, src, ans, layer_of)
    rows += rows_call_block(ids["C"] + ech["C_ish"] + ech["C_range"], det, src, ans, layer_of)
    rows += stamp_block(rows_stage(ids["S"], det, src, ans), "S", det)
    rows += stamp_block(rows_sub(ids["M"], det, src, ans), "M", det)
    rows += stamp_block(rows_visit(ids["V"], det, src, ans), "V", det)

    for a in ans:
        if "块号" not in a:
            a["块号"] = a["样本号"].rsplit("-", 1)[0]
        a.setdefault("层", "主层")

    blocks = len({r["块号"] for r in rows})
    print("块数 %d，行数 %d（%s）" % (blocks, len(rows), dict(Counter(r["字段"] for r in rows))))
    print("层分布:", dict(Counter(r["层"] for r in rows)))

    os.makedirs(OUT_DIR, exist_ok=True)
    write_csv(os.path.join(OUT_DIR, "审核表_专家A.csv"), rows)

    # 必须按**块**打乱，不能按行：块内其余行写的是「（同块，见首行）」，
    byf = defaultdict(lambda: defaultdict(list))
    for r in rows:
        byf[r["字段"]][r["块号"]].append(r)
    rng2 = random.Random(SEED + 1)
    shuf = []
    for k in byf:
        blks = list(byf[k].keys())
        rng2.shuffle(blks)
        for b in blks:
            shuf += byf[k][b]
    write_csv(os.path.join(OUT_DIR, "审核表_专家B.csv"), shuf)

    with io.open(os.path.join(OUT_DIR, "_答案键.json"), "w", encoding="utf-8") as f:
        json.dump({"_说明": "审核期间不交付专家；用于事后比对与 kappa 计算",
                   "items": ans}, f, ensure_ascii=False, indent=1)

    with io.open(os.path.join(OUT_DIR, "_抽样参数.json"), "w", encoding="utf-8") as f:
        json.dump({
            "_说明": "抽样为预注册参数，审核开始后不得变动",
            "版本": "v2 块式",
            "种子": SEED,
            "样本量_记录": {"K_kind": N_KIND, "C_call": N_CALL, "S_stage": N_STAGE,
                        "M_亚型": N_SUB, "V_就诊性质": N_VISIT},
            "富集层_记录": {"K_报告类": len(ech["K_rep"]), "C_HER2_ISH": len(ech["C_ish"]),
                        "C_Ki67_区间": len(ech["C_range"])},
            "候选池": {k: len(set(v)) for k, v in pool.items()},
            "抽中记录": len(all_ids),
            "块数": blocks,
            "行数": len(rows),
            "对齐情况": dict(Counter(a["对齐"] for a in ans)),
            "组织方式": "块 = 记录（C 为 记录×panel）；原文只在块首行给出，其余行写「（同块，见首行）」",
            "富集层口径": "标 层=富集，不参与主层率估计，只作缺陷确认",
            "产物": "assembled_v7_final.tar",
            "v1_对照": "审核包_20261112（预审）：698 记录／2,678 行／片段重复率 K 90%、C 78%",
        }, f, ensure_ascii=False, indent=1)
    print("OK ->", OUT_DIR)

if __name__ == "__main__":
    main()
