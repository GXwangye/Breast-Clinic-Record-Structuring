#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import io
import csv
import json
import random
import re
import html
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "00_core"))
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "05_agents"))
sys.path.insert(0, os.path.join(ROOT, "研究分析", "10_analysis"))
sys.path.insert(0, os.path.join(ROOT, "研究分析"))

from tarstream import iter_records                                    # noqa: E402
from probe_granularity import (norm_stage, dx_group, dx_group_visit, visit_kind,
                               BREAST_SET, BENIGN_SET)                # noqa: E402
from describe_structured_cohort import classify_subtype               # noqa: E402

TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
SRC_XLSX = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
OUT_DIR = DATA + "/qa_reports/审核包_20261112"

SEED = 20261112          # 预注册，写进 _抽样参数.json
N_KIND = 200
N_CALL = 200
N_STAGE = 100
N_SUB = 100
N_VISIT = 100

# 源表列位（实查，不凭记忆）
C_TYPE, C_ID = 0, 1
C_COMPLAINT, C_PI, C_PAST, C_AUX, C_PE = 8, 9, 10, 11, 12
C_DX = list(range(13, 26))          # 诊断1..13
C_NEXT = 28
C_ORD = 30                          # 医嘱（拼接长文本）
C_ORD_ITEMS = list(range(31, 91))   # 医嘱1..60（逐条）

TARGET_MARKERS = ("ER", "PR", "HER2", "KI67")

# 标志物名归一：产物写 HER2，原文常写 HER-2／CerbB-2／c-erbB-2／ERBB2。
# 已抽中记录内 26 个 HER2 判定对象因此整行被跳过（C 从 922 行缩到 892 行）。
MARKER_ALIAS = {
    "HER2": ("HER2", "HER-2", "CERBB-2", "CERBB2", "C-ERBB-2", "ERBB2", "HER2/NEU", "HER-2/NEU"),
    "ER": ("ER",),
    "PR": ("PR",),
    "KI67": ("KI67", "KI-67", "KI 67"),
}
# ER/PR 用词边界，否则 EGFR／ERBB2／PRL 会误命中
_RE_ER = re.compile(r"(?<![A-Z])ER(?![A-Z])", re.I)
_RE_PR = re.compile(r"(?<![A-Z])PR(?![A-Z0-9])", re.I)
_MARK_PAT = {
    "ER": _RE_ER, "PR": _RE_PR,
    "HER2": re.compile(r"(?:HER-?\s?2(?!\d)|CerbB[-\s]?2|c-erbB[-\s]?2|ERBB2)", re.I),
    "KI67": re.compile(r"KI[-\s]?67", re.I),
}
# 名后是否带取值（百分数／正负号／强弱／阴阳）；缺这条会放过「片段停在标志物名之后」的行
_HAS_VAL = re.compile(r"[0-9０-９]|[-+＋－]|阳性|阴性|弱|强|阴|阳")
PLACEHOLDER = {"", "无", "-", "—", "未提及", "未指明", "不详", "未查", "none", "null", "{}"}

def _flat(t):
    return re.sub(r"[\s\-]", "", s(t).upper())

def marker_key(name):
    k = re.sub(r"[\s\-]", "", s(name).upper())
    if k in ("HER2", "CERBB2", "ERBB2", "HER2NEU", "HERNEU"):
        return "HER2"
    if k in ("ER", "ESR"):
        return "ER"
    if k in ("PR", "PGR"):
        return "PR"
    if k in ("KI67", "MIB1"):
        return "KI67"
    return ""

def marker_spans(text, key):
    return [m.start() for m in _MARK_PAT[key].finditer(s(text))] if key in _MARK_PAT else []

def has_marker(text, key):
    return bool(marker_spans(text, key))

def marker_has_value(text, key, span=30):
    t = s(text)
    return any(_HAS_VAL.search(t[i:i + span]) for i in marker_spans(t, key))

def marker_window(text, key, before=220, after=380):
    t = s(text)
    sp = marker_spans(t, key)
    if not sp:
        return ""
    i = sp[-1]
    return t[max(0, i - before):i + after]

def s(v):
    if v is None:
        return ""
    return html.unescape(str(v)).strip()

def items_of(v):
    if isinstance(v, dict):
        v = v.get("items")
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []

def tnm_entries(d):
    t = d.get("tnm")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, dict)]

def sentences(text):
    return [x.strip() for x in re.split(r"[。；;\n\r]", s(text)) if x.strip()]

def snip(text, kw, width=140):
    t = s(text)
    if not t or not kw:
        return ""
    m = re.search(re.escape(kw), t, re.I)
    if not m:
        return ""
    a = max(0, m.start() - width)
    return t[a:m.end() + width].replace("\n", " ")

def ctx_sentence(body, kw, cap=400):
    for sent in sentences(body):
        if re.search(re.escape(kw), sent, re.I):
            return sent[:cap]
    return snip(body, kw, 200)

# ---------------------------------------------------------------- 第一遍：候选池
def scan_candidates():
    pool = {"K": [], "C": [], "S": [], "M": [], "V": []}
    n = 0
    for fn, rec in iter_records(TAR):
        rid = s(rec.get("record_id")) or fn.rsplit(".", 1)[0]
        n += 1
        pool["V"].append(rid)                       # 全库均可审就诊性质

        if any(s(it.get("kind")) for it in items_of(rec.get("orders"))):
            pool["K"].append(rid)

        ihc = (rec.get("auxiliary_exam") or {}).get("ihc") or {}
        panels = [p for p in (ihc.get("panels") or []) if isinstance(p, dict)]
        hit = False
        for p in panels:
            for m in p.get("markers") or []:
                if not isinstance(m, dict):
                    continue
                nm = s(m.get("marker_name")).upper().replace("-", "").replace(" ", "")
                if nm in TARGET_MARKERS and (s(m.get("call")) or m.get("percent") is not None):
                    hit = True
        if hit:
            pool["C"].append(rid)

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

def draw(pool, k, rng):
    ids = sorted(set(pool))
    return rng.sample(ids, min(k, len(ids)))

# ---------------------------------------------------------------- 第二遍：样本明细
def collect(ids):
    want = set(ids)
    det = {}
    for fn, rec in iter_records(TAR):
        rid = s(rec.get("record_id")) or fn.rsplit(".", 1)[0]
        if rid not in want:
            continue
        ihc = (rec.get("auxiliary_exam") or {}).get("ihc") or {}
        panels = [p for p in (ihc.get("panels") or []) if isinstance(p, dict)]
        det[rid] = {
            "orders": [(s(it.get("name")), s(it.get("kind")), s(it.get("source_field")))
                       for it in items_of(rec.get("orders")) if s(it.get("name"))],
            "panels": [{
                "quote": s(p.get("anchor", {}).get("quote")) if isinstance(p.get("anchor"), dict) else "",
                "markers": [{
                    "name": s(m.get("marker_name")),
                    "call": s(m.get("call")),
                    "status": s(m.get("status")),
                    "percent": m.get("percent"),
                    "score": s(m.get("score")),
                    "raw": s(m.get("raw_value")),
                } for m in (p.get("markers") or []) if isinstance(m, dict)],
                "subtype": classify_subtype(p),
            } for p in panels],
            "dx": [{
                "name": s(d.get("name")),
                "icd": s(d.get("icd_yb_code")),
                "stages": [s(e.get("stage")) for e in tnm_entries(d)],
                "stage_src": [s(e.get("source_field")) for e in tnm_entries(d)],
            } for d in items_of(rec.get("diagnoses"))],
        }
    return det

def read_source(ids):
    import openpyxl
    want = set(ids)
    out = {}
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = ["" if x is None else str(x) for x in row]
        rid = v[C_ID].strip()
        if rid not in want:
            continue
        out[rid] = {
            "type": v[C_TYPE], "complaint": v[C_COMPLAINT], "pi": v[C_PI],
            "past": v[C_PAST], "aux": v[C_AUX], "pe": v[C_PE],
            "dx": [v[i] for i in C_DX if v[i].strip()],
            "next": v[C_NEXT],
            "ord_text": v[C_ORD],
            "ord_items": [v[i] for i in C_ORD_ITEMS if v[i].strip()],
        }
    wb.close()
    return out

# ---------------------------------------------------------------- 行组装
def rows_kind(rids, det, src, ans):
    rows = []
    for rid in rids:
        sr = src.get(rid) or {}
        body = " ".join([sr.get("pi", ""), sr.get("aux", ""), sr.get("past", ""),
                         sr.get("next", ""), sr.get("ord_text", "")] +
                        sr.get("ord_items", []))
        for nm, kind, sf in det[rid]["orders"]:
            ctx = ctx_sentence(body, nm) if nm else ""
            if not ctx:
                # 名称被模型改写或多条合并时，名称无法在原文定位；给该记录医嘱原文兜底，
                # 让专家按原文判断该条目应归哪桶，而不是无据可判
                fallback = s(sr.get("ord_text")) or "；".join(sr.get("ord_items", []))
                ctx = ("【该名称未在原文直接出现，下列为本记录医嘱原文】" +
                       (fallback[:700] if fallback else "（原文医嘱列为空）"))
            sn = "K-%04d" % (len(rows) + 1)
            rows.append({
                "样本号": sn, "字段": "kind（医嘱七桶）", "记录ID": rid,
                "待判对象": nm, "原文片段": ctx,
                "待判问题": "该条目属哪一桶？填：药品／检查／检验／操作／费用／材料／其他",
                "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": sn, "字段": "kind", "记录ID": rid,
                        "待判对象": nm, "产物值": kind,
                        "对齐": "原文未匹配" if ctx.startswith("【该名称未在原文直接出现") else "已对齐"})
    return rows

def panel_evidence(p, sr, name, key):
    q = s(p.get("quote"))
    names = [s(m.get("name")) for m in p.get("markers") if s(m.get("name"))]
    if q and marker_has_value(q, key):
        return "【本行所判报告原文】" + q

    # 同现定位：候选句须含本行标志物**且名后有取值**，再比同 panel 其余标志物的同现数。
    # 三个字段都搜——IHC 写在现病史的记录占 53.9%，只搜辅助检查会两头落空。
    pool = [s(sr.get("aux")), s(sr.get("pi")), s(sr.get("past"))]
    best, score = "", 0
    for block in pool:
        sents = sentences(block)
        for i, sent in enumerate(sents):
            if not marker_has_value(sent, key):
                continue
            k = sum(1 for n in names if _flat(n) and _flat(n) in _flat(sent))
            if k > score:
                nxt = sents[i + 1] if i + 1 < len(sents) else ""
                best, score = (sent + "；" + nxt if nxt else sent), k
    if best:
        return "【本行所判报告原文（按标志物同现定位）】" + best[:700]

    # 兜底：以本标志物为中心取前后文，不用「整段前 N 字」（会切在别处）
    for block in pool:
        win = marker_window(block, key)
        if win:
            return "【本行所判报告原文（本标志物前后文）】" + win
    return "【全文未出现该标志物名，本行请直接填「不可判」】"

def rows_call(rids, det, src, ans):
    head, tail = [], []
    for rid in rids:
        sr = src.get(rid) or {}
        body = " ".join([s(sr.get("pi")), s(sr.get("aux")), s(sr.get("past"))])
        for p in det[rid]["panels"]:
            for m in p["markers"]:
                key = marker_key(m["name"])
                if key not in TARGET_MARKERS:
                    continue
                if not (m["call"] or m["percent"] is not None):
                    continue
                if ctx_sentence(body, m["name"]) or s(p.get("quote")):
                    head.append((rid, p, m, key))
                elif has_marker(body, key):
                    tail.append((rid, p, m, key))
    rows = []
    for group, note in ((head, "已对齐"), (tail, "已对齐（名称写作别名）")):
        for rid, p, m, key in group:
            ctx = panel_evidence(p, src.get(rid) or {}, m["name"], key)
            if key == "KI67":
                q = "原文给出的 Ki67 数值是多少？填百分数（如 30%），未写数值填「未写」"
            else:
                q = "按 ASCO-CAP 该标志物应判为？填：阳性／阴性／不确定／不适用"
            sn = "C-%04d" % (len(rows) + 1)
            rows.append({
                "样本号": sn, "字段": "call（免疫组化判读）", "记录ID": rid,
                "待判对象": m["name"], "原文片段": ctx, "待判问题": q,
                "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": sn, "字段": "call", "记录ID": rid,
                        "待判对象": m["name"],
                        "产物值": ("percent=%s" % m["percent"]) if key == "KI67" else m["call"],
                        "对齐": note})
    return rows

def rows_stage(rids, det, src, ans):
    rows = []
    for rid in rids:
        sr = src.get(rid) or {}
        parts = [("诊断列", "；".join(sr.get("dx", [])) or "（空）"),
                 ("现病史", s(sr.get("pi")) or "（空）"),
                 ("既往史", s(sr.get("past")) or "（空）"),
                 ("体格检查", s(sr.get("pe")) or "（空）"),
                 ("辅助检查", s(sr.get("aux"))[:3000] or "（空）")]
        ctx = " ｜ ".join("%s：%s" % (k, v) for k, v in parts)
        for d in det[rid]["dx"]:
            st = [x for x in d["stages"] if norm_stage(x)]
            if not st:
                continue
            sn = "S-%04d" % (len(rows) + 1)
            rows.append({
                "样本号": sn, "字段": "stage（TNM 主期）", "记录ID": rid,
                "待判对象": "诊断：%s" % (d["name"] or "（未给名）"),
                "原文片段": ctx,
                "待判问题": "原文写明的 TNM 主期应为？填：0／Ⅰ／Ⅱ／Ⅲ／Ⅳ；原文未写分期填「未写」",
                "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": sn, "字段": "stage", "记录ID": rid,
                        "产物值": "/".join([norm_stage(x) or "" for x in st]),
                        "对齐": "已对齐"})
    return rows

def rows_sub(rids, det, src, ans):
    rows = []
    for rid in rids:
        sr = src.get(rid) or {}
        body = " ".join([sr.get("pi", ""), sr.get("aux", ""), sr.get("past", "")])
        pick = None
        for p in det[rid]["panels"]:
            if p["subtype"][0]:
                pick = p
                break
        if pick is None:
            continue
        ctx = pick["quote"]
        if not ctx:
            names = [x["name"] for x in pick["markers"]][:4]
            ctx = "；".join([c for c in (ctx_sentence(body, n) for n in names) if c][:3])
        if not ctx:
            continue
        sn = "M-%04d" % (len(rows) + 1)
        rows.append({
            "样本号": sn, "字段": "分子亚型（panel 级 St Gallen）", "记录ID": rid,
            "待判对象": "该份 IHC 报告（本行只判此份，勿与本记录其他报告合并）",
            "原文片段": ctx,
            "待判问题": "按 St Gallen 应归为？填：Luminal A／Luminal B／HER2 阳性型／三阴性／无法判定",
            "判定": "", "错因": "", "备注": "",
        })
        ans.append({"样本号": sn, "字段": "分子亚型", "记录ID": rid,
                    "产物值": pick["subtype"][0], "对齐": "已对齐"})
    return rows

def malig_kind(groups):
    mal = bool(groups & BREAST_SET)
    ben = bool(groups & BENIGN_SET)
    if mal and ben:
        return "兼有"
    if mal:
        return "乳腺癌"
    if ben:
        return "仅良性"
    return "均无"

def rows_visit(rids, det, src, ans):
    rows = []
    for rid in rids:
        sr = src.get(rid) or {}
        ctx = "；".join(sr.get("dx", [])) or "（原文诊断列为空）"
        groups = {dx_group_visit(d["icd"], d["name"]) for d in det[rid]["dx"]}
        sn = "V-%04d" % (len(rows) + 1)
        rows.append({
            "样本号": sn, "字段": "就诊性质 + 良恶性", "记录ID": rid,
            "待判对象": "本次诊断",
            "原文片段": ctx,
            "待判问题": "两问：①就诊性质填 复诊／新发或在治／仅良性／无乳腺诊断；"
                        "②患者性质填 乳腺癌／仅良性／兼有／均无",
            "判定": "", "错因": "", "备注": "",
        })
        ans.append({"样本号": sn, "字段": "就诊性质+良恶性", "记录ID": rid,
                    "产物值": "%s／%s" % (visit_kind(groups), malig_kind(groups)),
                    "对齐": "已对齐"})
    return rows

COLS = ["样本号", "字段", "记录ID", "待判对象", "原文片段", "待判问题", "判定", "错因", "备注"]

def write_csv(path, rows):
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def main():
    dry = "--dry-run" in sys.argv
    rng = random.Random(SEED)
    pool, n_rec = scan_candidates()
    print("记录 %d；候选池 %s" % (n_rec, {k: len(set(v)) for k, v in pool.items()}))

    ids = {}
    ids["K"] = draw(pool["K"], N_KIND, rng)
    ids["C"] = draw(pool["C"], N_CALL, rng)
    ids["S"] = draw(pool["S"], N_STAGE, rng)
    ids["M"] = draw(pool["M"], N_SUB, rng)
    ids["V"] = draw(pool["V"], N_VISIT, rng)
    all_ids = sorted({x for v in ids.values() for x in v})
    print("抽中记录 %d；分字段 %s" % (len(all_ids), {k: len(v) for k, v in ids.items()}))
    if dry:
        return

    det = collect(all_ids)
    src = read_source(all_ids)
    print("明细 %d 条，原文命中 %d 条" % (len(det), len(src)))
    miss = [x for x in all_ids if x not in src]
    if miss:
        print("⚠ 有明细无原文：", miss[:5], "共", len(miss))

    ans = []
    rows = []
    rows += rows_kind(ids["K"], det, src, ans)
    rows += rows_call(ids["C"], det, src, ans)
    rows += rows_stage(ids["S"], det, src, ans)
    rows += rows_sub(ids["M"], det, src, ans)
    rows += rows_visit(ids["V"], det, src, ans)
    print("行数 %d（%s）" % (len(rows), dict(Counter(r["字段"] for r in rows))))

    os.makedirs(OUT_DIR, exist_ok=True)
    # 专家 A：原序；专家 B：同样本、行序打乱（按字段内打乱，保持字段成块便于分批）
    write_csv(os.path.join(OUT_DIR, "审核表_专家A.csv"), rows)
    by_field = {}
    for r in rows:
        by_field.setdefault(r["字段"], []).append(r)
    shuf = []
    rng2 = random.Random(SEED + 1)
    for k in by_field:
        blk = list(by_field[k])
        rng2.shuffle(blk)
        shuf += blk
    write_csv(os.path.join(OUT_DIR, "审核表_专家B.csv"), shuf)

    with io.open(os.path.join(OUT_DIR, "_答案键.json"), "w", encoding="utf-8") as f:
        json.dump({"_说明": "审核期间不交付专家；用于事后比对与 kappa 计算",
                   "items": ans}, f, ensure_ascii=False, indent=1)

    aligned = Counter(a["对齐"] for a in ans)
    with io.open(os.path.join(OUT_DIR, "_抽样参数.json"), "w", encoding="utf-8") as f:
        json.dump({
            "_说明": "抽样为预注册参数，审核开始后不得变动",
            "种子": SEED,
            "样本量": {"K_kind": N_KIND, "C_call": N_CALL, "S_stage": N_STAGE,
                       "M_亚型": N_SUB, "V_就诊性质": N_VISIT},
            "候选池": {k: len(set(v)) for k, v in pool.items()},
            "抽中记录": len(all_ids),
            "行数": len(rows),
            "对齐情况": dict(aligned),
            "产物": "assembled_v7_final.tar",
            "字段名实查": {
                "kind": "orders.items[].kind",
                "call": "auxiliary_exam.ihc.panels[].markers[].call（不是 call_status）",
                "stage": "diagnoses.items[].tnm.stage（dict 或 list 两形态）",
                "亚型": "未落产物，记录级由 describe_structured_cohort.classify_subtype 推导",
                "就诊性质": "未落产物，由 probe_granularity.dx_group 归类后判定",
            },
        }, f, ensure_ascii=False, indent=1)
    print("OK ->", OUT_DIR)
    print("对齐", dict(aligned))

if __name__ == "__main__":
    main()
