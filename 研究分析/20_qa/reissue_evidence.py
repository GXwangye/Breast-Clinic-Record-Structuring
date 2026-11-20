#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import csv
import json
import os
import random
import re
import sys

import openpyxl

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QA = DATA + "/qa_reports"
OLD_PACK = QA + "/审核包_20261113"
OUT_DIR = QA + "/审核包_20261113_补取证"
SRC_XLSX = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"

SEED = 20261113

# 源表列位（与 build_review_pack.py 一致，实查所得）
C_TYPE, C_ID = 0, 1
C_COMPLAINT, C_PI, C_PAST, C_AUX, C_PE = 8, 9, 10, 11, 12
C_DX = list(range(13, 26))
C_NEXT = 28

FIELD_LABEL = {"pi": "现病史", "aux": "辅助检查", "past": "既往史", "pe": "查体",
               "dx": "诊断"}
FIELD_MAX = 3000                      # 单字段上限；超长以首个标志物为中心取窗

_MARK_ANY = re.compile(
    r"(?<![A-Z])ER(?![A-Z])|(?<![A-Z])PR(?![A-Z0-9])"
    r"|HER-?\s?2(?!\d)|CerbB[-\s]?2|c-erbB[-\s]?2|ERBB2|KI[-\s]?67", re.I)

def read_targets():
    d = json.load(open(QA + "/审核结果_20261113.json", encoding="utf-8"))
    mapping = {"C·免疫组化判读": ("C", "call（免疫组化判读）"),
               "M·分子亚型": ("M", "分子亚型（panel 级 St Gallen）")}
    out = []
    for key, (pref, fname) in mapping.items():
        for x in d["_明细"][key]["判不了但产物给值"]:
            out.append({"前缀": pref, "字段": fname, "样本号": x["样本号"],
                        "记录ID": x["记录ID"], "待判对象": str(x.get("待判对象") or ""),
                        "产物值": str(x["产物"]),
                        "A": x.get("A_raw") or x.get("A"), "B": x.get("B_raw") or x.get("B")})
    return out

def read_source(ids):
    want = set(ids)
    out = {}
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = ["" if x is None else str(x) for x in row]
        rid = v[C_ID].strip()
        if rid in want and rid not in out:
            out[rid] = {"pi": v[C_PI], "aux": v[C_AUX], "past": v[C_PAST], "pe": v[C_PE],
                        "dx": [v[i] for i in C_DX if v[i].strip()]}
        if len(out) == len(want):
            break
    wb.close()
    return out

def clip(text):
    t = (text or "").strip()
    if len(t) <= FIELD_MAX:
        return t
    m = _MARK_ANY.search(t)
    if not m:
        return t[:FIELD_MAX] + "……（以下略）"
    a = max(0, m.start() - FIELD_MAX // 3)
    b = min(len(t), a + FIELD_MAX)
    head = "……" if a > 0 else ""
    tail = "……（以下略）" if b < len(t) else ""
    return head + t[a:b] + tail

def evidence(src):
    parts = []
    for f in ("pi", "aux", "past", "pe"):
        t = clip(src.get(f, ""))
        if t and t not in ("无", "-"):
            parts.append("【%s】%s" % (FIELD_LABEL[f], t))
    dx = [x for x in (src.get("dx") or []) if x.strip()]
    if dx:
        parts.append("【诊断】" + "；".join(dx))
    return "\n".join(parts) if parts else "（原文各字段均为空）"

Q_CALL = "按 ASCO-CAP 该标志物应判为？填：阳性／阴性／不确定／不适用"
Q_KI67 = ("原文给出的 Ki-67 数值是多少？填百分数（如 30%）；原文写区间照抄"
          "（如 40%-60%）；未写数值填「未写」")
Q_SUB = "按 St Gallen 应归为？填：Luminal A／Luminal B／HER2 阳性型／三阴性／无法判定"

_MARK_ONE = {
    "ER": re.compile(r"(?<![A-Z])ER(?![A-Z])", re.I),
    "PR": re.compile(r"(?<![A-Z])PR(?![A-Z0-9])", re.I),
    "HER2": re.compile(r"(?:HER-?\s?2(?!\d)|CerbB[-\s]?2|c-erbB[-\s]?2|ERBB2)", re.I),
    "KI67": re.compile(r"KI[-\s]?67", re.I),
}

def mark_key(obj):
    o = (obj or "").upper().replace(" ", "").replace("-", "")
    if o.startswith("KI"):
        return "KI67"
    if "HER2" in o or "CERBB" in o:
        return "HER2"
    return o if o in ("ER", "PR") else None

def locator(obj, pref, ev):
    if pref == "M":
        return "本记录全部 IHC 报告（产物为记录级单一亚型）"
    k = mark_key(obj)
    if not k:
        return "该标志物：%s" % obj
    hits = []
    for m in _MARK_ONE[k].finditer(ev or ""):
        a = max(0, m.start() - 24)
        ctx = ev[a:m.end() + 34].replace("\n", " ").strip()
        if ctx not in hits:
            hits.append(ctx)
    if not hits:
        return "原文未见该标志物"
    if len(hits) == 1:
        return "原文 1 处：" + hits[0]
    return "原文 %d 处：① %s" % (len(hits), "　② ".join(h[:90] for h in hits[:4]))

def build_rows():
    T = read_targets()
    src = read_source({x["记录ID"] for x in T})

    # 一条记录一块；块内先 C 后 M，各自按原样本号升序
    by_rec = {}
    for x in sorted(T, key=lambda y: (y["前缀"] != "C", y["样本号"])):
        by_rec.setdefault(x["记录ID"], []).append(x)

    rows, ans = [], []
    for bi, rid in enumerate(sorted(by_rec), 1):
        blk = "Z-%04d" % bi
        ev = evidence(src.get(rid) or {})
        for i, x in enumerate(by_rec[rid]):
            pref, obj = x["前缀"], x["待判对象"]
            if pref == "M":
                q = Q_SUB
            elif obj.upper().replace(" ", "").replace("-", "").startswith("KI"):
                q = Q_KI67
            else:
                q = Q_CALL
            rows.append({
                "块号": blk, "样本号": x["样本号"], "层": "补取证",
                "字段": x["字段"], "记录ID": rid,
                "待判对象": "该记录全部 IHC 报告（整段原文见首行）" if pref == "M" else obj,
                "原文片段": ev if i == 0 else "（同块，见首行）",
                "定位": locator(obj, pref, ev),
                "待判问题": q, "判定": "", "错因": "", "备注": "",
            })
            ans.append({"样本号": x["样本号"], "字段": pref, "记录ID": rid,
                        "待判对象": x["待判对象"], "产物值": x["产物值"],
                        "上次A": x["A"], "上次B": x["B"], "块号": blk})
    return rows, ans

COLS = ["块号", "样本号", "层", "字段", "记录ID", "待判对象",
        "原文片段", "定位", "待判问题", "判定", "错因", "备注"]

def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=COLS)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in COLS})

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows, ans = build_rows()

    # A：样本号原序；B：按块打乱、块内保序（块式阅读，跨块才换原文）
    write_csv(os.path.join(OUT_DIR, "审核表_专家A.csv"), rows)
    blocks = {}
    for r in rows:
        blocks.setdefault(r["块号"], []).append(r)
    order = list(blocks)
    random.Random(SEED).shuffle(order)
    shuffled = [r for b in order for r in blocks[b]]
    write_csv(os.path.join(OUT_DIR, "审核表_专家B.csv"), shuffled)

    json.dump({"seed": SEED, "items": ans},
              open(os.path.join(OUT_DIR, "_答案键.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({
        "生成脚本": "研究分析/20_qa/reissue_evidence.py",
        "来源": "审核结果_20261113.json 的「判不了但产物给值」明细",
        "行数": len(rows), "记录数": len(blocks),
        "字段行数": {"C": sum(1 for x in ans if x["字段"] == "C"),
                     "M": sum(1 for x in ans if x["字段"] == "M")},
        "取证方式": "按字段（现病史/辅助检查/既往史/查体/诊断）整段给出，不切句、不选句",
        "单字段上限": FIELD_MAX,
        "行序": "A＝样本号原序；B＝按块打乱、块内保序",
        "seed": SEED,
    }, open(os.path.join(OUT_DIR, "_补取证参数.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    print("行 %d｜块 %d｜C %d｜M %d" % (
        len(rows), len(blocks),
        sum(1 for x in ans if x["字段"] == "C"),
        sum(1 for x in ans if x["字段"] == "M")))
    chars = sum(len(r["原文片段"]) for r in rows if not r["原文片段"].startswith("（同块"))
    print("原文总字数 %.1f 万｜块均 %d 字" % (chars / 1e4, chars / max(1, len(blocks))))

if __name__ == "__main__":
    main()
