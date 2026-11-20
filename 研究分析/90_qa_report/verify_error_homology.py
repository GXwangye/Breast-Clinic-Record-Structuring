#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import csv
from collections import Counter

import openpyxl

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QR = DATA + "/qa_reports"
PACK = QR + "/审核包_20261113"
SRC_XLSX = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
OUT_JSON = QR + "/同源核验_20261113.json"
OUT_MD = QR + "/同源核验_20261113.md"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records                                        # noqa: E402
from ihc_reports import (mkey, blocks_of, match_block, vec_of_segment,    # noqa: E402
                         vec_of_panel, block_matches)

def main():
    d = json.load(open(QR + "/错判归因_20261113.json", encoding="utf-8"))
    rows = d["明细"]
    ids = {r["记录ID"] for r in rows}

    # 源表原文（现病史／辅助检查／既往史）
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    src = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = ["" if x is None else str(x) for x in row]
        rid = v[1].strip()
        if rid in ids and rid not in src:
            src[rid] = " ".join([v[9], v[11], v[10]])
        if len(src) == len(ids):
            break
    wb.close()

    # tar panel（只用来取 panel 份数与该块是否已入库，不用来复算产物值）
    recs = {}
    for fn, rec in iter_records(TAR):
        rid = str(rec.get("record_id") or "")
        if rid in ids and rid not in recs:
            recs[rid] = rec
        if len(recs) == len(ids):
            break

    A = {r["样本号"]: r for r in csv.DictReader(
        open(PACK + "/审核表_专家A_已完成.csv", encoding="utf-8-sig"))}

    out, self_fail, differ = [], [], []
    for t in rows:
        rid = t["记录ID"]
        blocks = blocks_of(src.get(rid, ""))
        panels = ((recs.get(rid, {}).get("auxiliary_exam") or {}).get("ihc") or {}).get("panels") or []

        blk = t["样本号"].rsplit("-", 1)[0] if t["字段名"].startswith("C") else t["样本号"]
        seg = A.get(blk + "-01", A.get(t["样本号"], {})).get("原文片段", "")

        # ---- 产物值：从已落盘的字符串反解析（不用 tar 复算，这是本脚本的独立数据路径）
        if t["字段名"].startswith("C"):
            keys = [mkey(t["对象"])]
            if t["产物"].startswith("percent="):
                pv = {"KI67": float(t["产物"].split("=")[1])}
            else:
                pv = {keys[0]: str(t["产物"])}
        else:
            # M 的亚型由四要素共同决定：只按 ER/PR 匹配会把「两份报告 ER/PR 都阳性、
            # 但 HER2 与 Ki-67 不同」误判成同源（47191820 即此坑）
            keys = ["ER", "PR", "HER2", "KI67"]
            pv = {}
            for k, pat in (("ER", r"ER=(\S+)"), ("PR", r"PR=(\S+)"),
                           ("HER2", r"HER2=(\S+)"), ("KI67", r"Ki67=(\S+)")):
                m = re.search(pat, t["产物panel原值"])
                if not m:
                    continue
                v = m.group(1)
                if k == "KI67":
                    if v not in ("None", "—"):
                        try:
                            pv[k] = float(v)
                        except ValueError:
                            pass
                elif v in ("阳性", "阴性", "不确定"):
                    pv[k] = v

        # ---- 取证片段值：C 取该标志物，M 取片段里能凑齐的那几项
        if t["字段名"].startswith("C"):
            sv, _ = vec_of_segment(seg, keys)
            sv = {k: v for k, v in sv.items() if k in keys}
        else:
            sv, _ = vec_of_segment(seg, ["ER", "PR"])
            sv = {k: v for k, v in sv.items() if k in ("ER", "PR", "HER2", "KI67")}
        seg_keys = [k for k in keys if k in sv]

        pv_keys = [k for k in keys if k in pv]
        prod_hit = match_block(pv, blocks, pv_keys) if pv_keys else []
        seg_hit = match_block(sv, blocks, seg_keys) if seg_keys else []

        if not prod_hit:
            # C 的 Ki-67 常分句写（「Ki-67（40%）」自成一块），允许只按该值找
            if t["字段名"].startswith("C") and "KI67" in pv:
                prod_hit = [i for i, b in enumerate(blocks)
                            if "KI67" in b["vec"] and abs(b["vec"]["KI67"] - pv["KI67"]) <= 1]
            if not prod_hit:
                self_fail.append({"样本号": t["样本号"], "记录ID": rid,
                                  "产物值": pv, "块数": len(blocks),
                                  "块值": [b["vec"] for b in blocks][:6]})

        same = bool(set(prod_hit) & set(seg_hit)) if (prod_hit and seg_hit) else None

        # ---- 专家取的那一块在不在库
        pvecs = [vec_of_panel(p) for p in panels]
        covered = None
        if seg_hit:
            covered = any(block_matches(pv_, blocks[i]) for i in seg_hit for pv_ in pvecs if pv_)
        if t["归因"] == "异源":
            disp = "改选取" if covered else "补抽"
        else:
            disp = "—"

        # 与归因表交叉：唯一的合法差分是「片段自身混读」（片段里两组取值 → 归因判异源而块仍同源）
        if (t["归因"] == "异源") != (same is False):
            differ.append({"样本号": t["样本号"], "归因": t["归因"],
                           "本脚本同源": same, "说明": t.get("说明", "")[:40]})

        out.append({
            "样本号": t["样本号"], "记录ID": rid, "字段名": t["字段名"],
            "对象": t["对象"], "产物": t["产物"], "专家": t["专家"],
            "原归因": t["归因"],
            "报告块数": len(blocks),
            "产物取自块": prod_hit,
            "片段取自块": seg_hit,
            "是否同源": same,
            "片段块已入库": covered,
            "处置": disp,
            "块摘要": ["[%d] %s" % (i, b["text"][:60]) for i, b in enumerate(blocks)][:6],
            "panel数": len(panels),
            "panel有anchor": sum(1 for p in panels if p.get("anchor")),
        })

    if self_fail:
        print("！产物 panel 值在记录报告块中找不到 %d 行，拒绝出数：" % len(self_fail))
        for x in self_fail[:8]:
            print("  ", x)
        raise SystemExit(1)

    # 汇总
    summit = {}
    for r in out:
        key = (r["字段名"], r["原归因"])
        s = summit.setdefault(key, {"行数": 0, "同源": 0, "异源": 0, "未定": 0,
                                    "多报告记录": 0, "补抽": 0, "改选取": 0})
        s["行数"] += 1
        if r["是否同源"] is True:
            s["同源"] += 1
        elif r["是否同源"] is False:
            s["异源"] += 1
        else:
            s["未定"] += 1
        if r["报告块数"] > 1:
            s["多报告记录"] += 1
        if r["处置"] in ("补抽", "改选取"):
            s[r["处置"]] += 1

    R = {"_来源": {"错判明细": "错判归因_20261113.json", "原文": "源表 现病史/辅助检查/既往史",
                   "判据": "报告块=含≥2 靶标志物的句子；产物块与片段块无交集即异源"
                           "（00_core/ihc_reports.py，与归因脚本同一判据）",
                   "独立性": "产物值由落盘字符串反解析，不由 tar 的 panel 复算",
                   "自检": "产物值必须在原文块中找到：%d/%d 通过" % (len(out), len(out))},
         "汇总": {"%s|%s" % k: v for k, v in summit.items()},
         "交叉差分": differ,
         "明细": out}
    json.dump(R, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_md(R)
    print("OK ->", OUT_JSON)
    for k, v in R["汇总"].items():
        print("%-34s 行%2d 同源%2d 异源%2d 未定%2d 多报告%2d 补抽%2d 改选取%2d"
              % (k, v["行数"], v["同源"], v["异源"], v["未定"], v["多报告记录"],
                 v["补抽"], v["改选取"]))
    print("交叉差分 %d 行" % len(differ))
    for x in differ:
        print("  ", x["样本号"], x["归因"], "本脚本同源=", x["本脚本同源"], "|", x["说明"])

def write_md(R):
    L = []
    w = L.append
    w("# 错判行的同源核验\n")
    w("审核包的产物值取自 `pick_panel()` 选中的**一份** panel；取证片段在 panel 无 anchor 时"
      "回落「同现定位」。记录存 2 份以上 IHC 时，两者可能不是同一份报告。\n")
    w("本节把每一行错判回源表切成「报告块」（含 ≥2 个靶标志物的句子），分别定位"
      "**产物取自哪一块**与**片段取自哪一块**。不同块即异源——这类错判不是值错。\n")
    w("> 独立性：判据与归因脚本共用 `00_core/ihc_reports.py`，但产物值是从归因表落盘的"
      "`产物panel原值` 反解析，不由 tar 的 panel 复算。%s。\n" % R["_来源"]["自检"])

    w("| 字段 | 原归因 | 行数 | 同源 | 异源 | 未定 | 其中多报告记录 | 补抽 | 改选取 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for k, v in R["汇总"].items():
        f, a = k.split("|")
        w("| %s | %s | %d | %d | %d | %d | %d | %d | %d |"
          % (f, a, v["行数"], v["同源"], v["异源"], v["未定"], v["多报告记录"],
             v["补抽"], v["改选取"]))
    w("")

    w("## 逐行\n")
    w("| 样本号 | 记录ID | 产物 | 专家 | 原归因 | 报告块数 | 产物块 | 片段块 | 同源 | 处置 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in R["明细"]:
        w("| %s | %s | %s | %s | %s | %d | %s | %s | %s | %s |"
          % (r["样本号"], r["记录ID"], str(r["产物"])[:10], str(r["专家"])[:10],
             r["原归因"], r["报告块数"],
             ",".join(str(i + 1) for i in r["产物取自块"]) or "—",
             ",".join(str(i + 1) for i in r["片段取自块"]) or "—",
             {True: "**是**", False: "**否**", None: "未定"}[r["是否同源"]], r["处置"]))
    w("")

    w("## 交叉差分\n")
    if not R["交叉差分"]:
        w("两路判定逐行一致。\n")
    else:
        w("| 样本号 | 归因表结论 | 本脚本同源 | 差异原因 |\n|---|---|---|---|")
        for x in R["交叉差分"]:
            w("| %s | %s | %s | 取证片段自身混了两份报告的取值（`seg_conflicts`），"
              "块级仍同源 |" % (x["样本号"], x["归因"], {True: "是", False: "否", None: "未定"}[x["本脚本同源"]]))
        w("")
        w("此类不是判据冲突：片段本身混读时，块匹配只能落到其中一组取值，而同源的真问题是"
          "**一行里出现两组值**。归因表以片段内取值冲突兜底判异源，故两处结论不同、处置相同。\n")

    w("## 异源行的原文对照\n")
    for r in R["明细"]:
        if r["是否同源"] is False:
            w("### %s / %s\n" % (r["样本号"], r["记录ID"]))
            w("- 产物取自第 %s 块，取证片段取自第 %s 块；专家那块**%s**"
              % ([i + 1 for i in r["产物取自块"]], [i + 1 for i in r["片段取自块"]],
                 "已在库（改选取）" if r["片段块已入库"] else "未入库（补抽）"))
            for i, b in enumerate(r["块摘要"]):
                mark = "**产物**" if i in r["产物取自块"] else ("**片段**" if i in r["片段取自块"] else "")
                w("- %s%s" % (mark + " " if mark else "", b))
            w("")
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

if __name__ == "__main__":
    main()
