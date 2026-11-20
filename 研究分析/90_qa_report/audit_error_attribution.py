#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import csv
import re
from collections import Counter

import openpyxl

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QR = DATA + "/qa_reports"
PACK = QR + "/审核包_20261113"
SRC_XLSX = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
OUT_JSON = QR + "/错判归因_20261113.json"
OUT_MD = QR + "/错判归因_20261113.md"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records                                      # noqa: E402
from ihc_reports import (mkey, blocks_of, match_block, vec_of_segment,  # noqa: E402
                         vec_of_text, vec_of_panel, polarity, seg_conflicts,
                         block_matches)

SUB_PAIR = {"LuminalB", "HER2阳性型"}

def sub_norm(v):
    return re.sub(r"\s+", "", str(v or "")).replace("HER-2", "HER2").replace("her2", "HER2")

def marker_in(panel, key):
    for m in (panel.get("markers") or []):
        if mkey(m.get("marker_name")) == key:
            return m
    return None

def mval(m, key):
    if not m:
        return None
    if key == "KI67":
        return m.get("percent")
    return m.get("call") or m.get("status")

def pick_panel(panels):
    best, bn = None, -1
    for p in panels:
        n = sum(1 for m in (p.get("markers") or [])
                if mkey(m.get("marker_name")) in ("ER", "PR", "HER2", "KI67")
                and (m.get("call") or m.get("percent") is not None))
        if n > bn:
            best, bn = p, n
    return best if bn > 0 else None

def _block_covered(blocks, idxs, pvecs):
    if not idxs:
        return None
    for i in idxs:
        for pv in pvecs:
            if pv and block_matches(pv, blocks[i]):
                return True
    return False

def _missing_blocks(blocks, pvecs):
    return [i + 1 for i in range(len(blocks)) if not _block_covered(blocks, [i], pvecs)]

def main():
    A = {r["样本号"]: r for r in csv.DictReader(
        open(PACK + "/审核表_专家A_已完成.csv", encoding="utf-8-sig"))}
    key = {x["样本号"]: x for x in json.load(
        open(PACK + "/_答案键.json", encoding="utf-8"))["items"]}
    res = json.load(open(QR + "/补取证结果_20261113.json", encoding="utf-8"))

    targets = []
    for fname in ("C·免疫组化判读", "M·分子亚型"):
        for x in res["二_最终率"]["补审后"][fname]["错判明细"]:
            targets.append({"字段名": fname, "样本号": x["sid"], "记录ID": x["rec"],
                            "对象": x["obj"], "产物": x["产物"], "专家": x["A_raw"],
                            "来源": x["src"]})

    ids = {t["记录ID"] for t in targets}

    # 源表原文（现病史／辅助检查／既往史）
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    src = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = ["" if x is None else str(x) for x in row]
        rid = v[1].strip()
        if rid in ids and rid not in src:
            src[rid] = "；".join([v[9], v[11], v[10]])
        if len(src) == len(ids):
            break
    wb.close()

    recs = {}
    for fn, rec in iter_records(TAR):
        rid = str(rec.get("record_id") or "")
        if rid in ids and rid not in recs:
            recs[rid] = rec
        if len(recs) == len(ids):
            break

    selfcheck = []
    for t in targets:
        rec = recs.get(t["记录ID"])
        panels = ((rec.get("auxiliary_exam") or {}).get("ihc") or {}).get("panels") or [] \
            if rec else []
        p = pick_panel(panels)
        t["panel数"] = len(panels)
        blk = t["样本号"].rsplit("-", 1)[0] if t["字段名"].startswith("C") else t["样本号"]
        seg = A.get(blk + "-01", A.get(t["样本号"], {})).get("原文片段", "")
        t["取证片段"] = seg[:30].replace("\n", " ")
        t["原文块数"] = len(blocks_of(src.get(t["记录ID"], "")))

        # ---- 产物值向量（自检：必须等于答案键）
        if t["字段名"].startswith("C"):
            k = mkey(t["对象"])
            pm = marker_in(p, k) if p else None
            pv = mval(pm, k)
            t["产物panel原值"] = ("call=%s pct=%s score=%s raw=%s"
                                 % (pm.get("call") or pm.get("status"), pm.get("percent"),
                                    pm.get("score"), str(pm.get("raw_value"))[:16])) if pm else "—"
            expect = str(key[t["样本号"]]["产物值"])
            got = ("percent=%s" % pv) if k == "KI67" and pv is not None else str(pv)
            selfcheck.append((t["样本号"], got == expect, got, expect))
            keys = [k]
            pvec = {k: pv} if pv is not None else {}
        else:
            pvec = vec_of_panel(p) if p else {}
            t["产物panel原值"] = " ".join("%s=%s" % (k, pvec.get(k)) for k in ("ER", "PR", "HER2", "KI67"))
            expect = str(key[t["样本号"]]["产物值"])
            # M 的记录级亚型由四要素反推，不逐字比；只验 panel 存在
            selfcheck.append((t["样本号"], pvec != {}, str(pvec), expect))
            keys = [k for k in ("ER", "PR", "HER2", "KI67") if k in pvec]

        # ---- 取证片段值向量
        sk = keys if not t["字段名"].startswith("M") else ["ER", "PR"]
        svec, _ = vec_of_segment(seg, sk)
        svec = {k: v for k, v in svec.items() if k in keys}
        sk = [k for k in keys if k in svec]

        blocks = blocks_of(src.get(t["记录ID"], ""))
        ph = match_block(pvec, blocks, [k for k in keys if k in pvec])
        sh = match_block(svec, blocks, sk) if svec else []
        t["产物取自块"], t["片段取自块"] = ph, sh
        same = bool(set(ph) & set(sh)) if (ph and sh) else None
        t["同源"] = same
        t["片段取值"] = ["%s=%s" % (k, v) for k, v in svec.items()]

        # 专家看的那一块，产物里有没有对应的 panel（有=两份都入库，只是审核包取了一份）
        pvecs = [vec_of_panel(q) for q in panels]
        t["片段块已入库"] = _block_covered(blocks, sh, pvecs)
        t["漏抽块"] = _missing_blocks(blocks, pvecs)

        if not pvec or not ph:
            t["归因"], t["说明"] = "取证不足", "产物取值在原文找不到对应报告块"
        elif sh and not (set(ph) & set(sh)):
            t["归因"] = "异源"
            t["说明"] = ("产物取块 %s、片段取块 %s；原文 %d 块、入库 %d panel"
                        % ([i + 1 for i in ph], [i + 1 for i in sh],
                           len(blocks), len(panels)))
        elif t["字段名"].startswith("C") and len(seg_conflicts(seg, key_of(t))) > 1:
            # 片段自己就混了两份报告的取值（如「HER-2*（2+）；HER-2*（3+）」）
            t["归因"] = "异源"
            t["说明"] = "取证片段含 %d 组不同取值，专家取了一组、产物取另一组" \
                        % len(seg_conflicts(seg, key_of(t)))
        else:
            src_text = src.get(t["记录ID"], "")
            if t["字段名"].startswith("C"):
                k0 = keys[0]
                pvv, svv = pvec.get(k0), svec.get(k0)
                if k0 == "HER2" and str(pvv) == "不确定" \
                        and re.search(r"基因[^。；]{0,6}(无扩增|扩增)", src_text):
                    t["归因"], t["说明"] = "ISH未采纳", "原文写明基因扩增／无扩增，产物 HER2 仍记不确定"
                elif pvv is not None and svv is not None and str(pvv) != str(svv):
                    if k0 == "HER2" and "/" in str(svv):
                        t["归因"], t["说明"] = "口径边界", "原文 HER2 写区间，产物取下限"
                    else:
                        t["归因"], t["说明"] = "值错", "%s 取值与原文不同" % k0
                else:
                    t["归因"] = "口径边界"
                    t["说明"] = "产物值与原文一致，专家判读更严／更松"
            else:
                prm = marker_in(p, "PR") if p else None
                prpct = prm.get("percent") if prm else None
                want = expect_subtype(pvec, prpct)
                got = sub_norm(t["产物"])
                if {got, sub_norm(t["专家"])} == SUB_PAIR \
                        and (pvec.get("ER") == "阳性" or pvec.get("PR") == "阳性"):
                    t["归因"], t["说明"] = "口径等价", "HR+/HER2+ 的两种叫法都判对"
                elif want and got != sub_norm(want):
                    if pvec.get("PR") == "阴性" or _pr_low(p):
                        t["归因"], t["说明"] = "判据缺失", "PR 阴性或 <20%，产物仍判 Luminal A"
                    else:
                        t["归因"], t["说明"] = "值错", "亚型与四要素不符（应为 %s）" % want
                else:
                    t["归因"] = "口径边界"
                    t["说明"] = "产物亚型与四要素一致，专家判读不同"

        # 异源行的处置：专家取的那一块在不在库，决定是「补抽」还是「改选取」
        if t["归因"] == "异源":
            cov = t["片段块已入库"]
            if cov is True:
                t["处置"] = "改选取"
            elif cov is False or t["漏抽块"]:
                t["处置"] = "补抽"
            else:
                t["处置"] = "待查"

    bad = [x for x in selfcheck if not x[1]]
    if bad:
        print("！pick_panel 复刻与答案键不符 %d 行，拒绝出数：" % len(bad))
        for x in bad[:6]:
            print("  ", x[0], "复刻=", x[2], "答案键=", x[3])
        raise SystemExit(1)

    out = {
        "_来源": {"错判行": "补取证结果_20261113.json 的 C 与 M 最终错判",
                  "panel选取": "复刻 build_review_pack_v2.pick_panel",
                  "原文": "源表 现病史／辅助检查／既往史",
                  "判据": "报告块匹配（00_core/ihc_reports.py）；产物块与片段块无交集即异源",
                  "自检": "产物值 == 答案键产物值：%d/%d 通过"
                          % (sum(1 for x in selfcheck if x[1]), len(selfcheck))},
        "归因分布": dict(Counter(t["归因"] for t in targets)),
        "按字段": {}, "明细": targets,
    }
    final_rate = res["二_最终率"]["补审后"]

    # 记录级：原文有几块、入库几份 panel、哪几块没对应 panel
    recmap = {}
    for t in targets:
        r = recmap.setdefault(t["记录ID"], {"记录ID": t["记录ID"], "原文块数": t["原文块数"],
                                           "panel数": t["panel数"], "漏抽块": t["漏抽块"],
                                           "涉及样本": [], "归因": []})
        r["涉及样本"].append(t["样本号"])
        r["归因"].append(t["归因"])
    out["记录级"] = [r for r in recmap.values() if r["漏抽块"]]
    for r in out["记录级"]:
        r["缺失份数"] = len(r["漏抽块"])
        r["归因"] = sorted(set(r["归因"]))

    for fn_ in ("C·免疫组化判读", "M·分子亚型"):
        sub = [t for t in targets if t["字段名"] == fn_]
        defect = [t for t in sub if t["归因"] in ("值错", "ISH未采纳", "判据缺失")]
        equiv = [t for t in sub if t["归因"] == "口径等价"]
        edge = [t for t in sub if t["归因"] == "口径边界"]
        unjudge = [t for t in sub if t["归因"] in ("异源", "取证不足")]
        hetero = [t for t in sub if t["归因"] == "异源"]
        denom = final_rate[fn_]["一致"]
        out["按字段"][fn_] = {
            "错判行": len(sub),
            "归因": dict(Counter(t["归因"] for t in sub)),
            "产物缺陷": len(defect),
            "异源": len(hetero),
            "异源处置": dict(Counter(t.get("处置", "—") for t in hetero)),
            "漏抽记录": sorted({t["记录ID"] for t in hetero if t.get("处置") == "补抽"}),
            "改选取记录": sorted({t["记录ID"] for t in hetero if t.get("处置") == "改选取"}),
            "取证不足": len([t for t in sub if t["归因"] == "取证不足"]),
            "口径等价": len(equiv),
            "口径边界": len(edge),
            "一致行": denom,
            "真实错判率": round(len(defect) / denom, 4) if denom else None,
            "原报错判率": final_rate[fn_]["错判率"],
        }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_md(out)
    print("OK ->", OUT_JSON)
    print("归因分布：", out["归因分布"])
    for f, v in out["按字段"].items():
        print("%-16s 错判 %2d | %s" % (f, v["错判行"], v["归因"]))
        print("                 产物缺陷 %d/%d=%.2f%%（原报 %.2f%%）｜异源 %d｜取证不足 %d｜口径等价 %d｜口径边界 %d"
              % (v["产物缺陷"], v["一致行"], (v["真实错判率"] or 0) * 100,
                 (v["原报错判率"] or 0) * 100, v["异源"], v["取证不足"], v["口径等价"], v["口径边界"]))

def _pr_low(p):
    m = marker_in(p, "PR")
    return bool(m) and isinstance(m.get("percent"), (int, float)) and m["percent"] < 20

def key_of(t):
    return mkey(t["对象"]) if t["字段名"].startswith("C") else "HER2"

def expect_subtype(vec, pr_pct=None):
    er, pr, h2, ki = vec.get("ER"), vec.get("PR"), vec.get("HER2"), vec.get("KI67")
    if er is None and pr is None:
        return None
    if h2 == "阳性":
        return "HER2阳性型" if (er == "阳性" or pr == "阳性") else "三阴性"
    if er == "阳性" or pr == "阳性":
        pr_low = pr == "阴性" or (isinstance(pr_pct, (int, float)) and pr_pct < 20)
        if pr_low or (isinstance(ki, (int, float)) and ki > 20):
            return "Luminal B"
        return "Luminal A"
    if h2 in ("阴性", "不确定", None):
        return "三阴性"
    return None

def write_md(R):
    L = []
    w = L.append
    w("# 错判归因（C 与 M）\n")
    w("把「两位专家一致但与产物不符」逐行拆开。不靠推测：回源表取 现病史／辅助检查／既往史 原文，"
      "切成「报告块」，分别定位**产物取自哪一块**与**取证片段取自哪一块**。\n")
    w("| 归因 | 判据 | 是否计产物缺陷 |\n|---|---|---|")
    w("| 异源 | 产物块与片段块无交集：同一记录两份 IHC，各自看了一份 | **不计**，缺陷在「多份报告只入库一份」 |")
    w("| 取证不足 | 产物无该标志物／片段未含完整报告块 | 不计 |")
    w("| 口径等价 | HR+/HER2+ 判 Luminal B 或 HER2 阳性型 | 不计 |")
    w("| 口径边界 | 原文写区间（1-2+、40%-60%），产物取一端 | 不计 |")
    w("| ISH未采纳 | 原文写基因扩增／无扩增，产物 HER2 仍记不确定 | **计**（派生层） |")
    w("| 判据缺失 | PR 阴性或 <20%，产物仍判 Luminal A | **计**（派生层） |")
    w("| 值错 | 同一份报告内取值不同 | **计**（取值层） |\n")
    w("> 自检：%s。\n" % R["_来源"]["自检"])

    for f, v in R["按字段"].items():
        sub = [t for t in R["明细"] if t["字段名"] == f]
        w("## %s（错判 %d 行）\n" % (f, v["错判行"]))
        w("| 归因 | 条数 |\n|---|---|")
        for k, n in v["归因"].items():
            w("| %s | %d |" % (k, n))
        w("")
        w("| 样本号 | 记录ID | 对象 | 产物 | 专家 | 产物 panel 原值 | 片段取值 | 块/panel | 归因 | 处置 |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for t in sub:
            w("| %s | %s | %s | %s | %s | %s | %s | %d/%d | **%s**%s | %s |"
              % (t["样本号"], t["记录ID"], t["对象"] or "（记录级）", str(t["产物"])[:12],
                 str(t["专家"])[:10], t["产物panel原值"][:34],
                 "；".join(t["片段取值"])[:24] or "—", t["原文块数"], t["panel数"],
                 t["归因"], ("（%s）" % t["说明"][:44]) if t.get("说明") else "",
                 t.get("处置", "—")))
        w("")

    w("## 三、扣除取证与异源后的真实率\n")
    w("| 字段 | 一致行 | 原报错判 | 异源 | 取证不足 | 口径等价 | 口径边界 | **产物缺陷** | **真实率** | 判定 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for f, v in R["按字段"].items():
        r = v["真实错判率"]
        w("| %s | %d | %d（%.2f%%） | %d | %d | %d | %d | **%d** | **%.2f%%** | %s |"
          % (f, v["一致行"], v["错判行"], (v["原报错判率"] or 0) * 100,
             v["异源"], v["取证不足"], v["口径等价"], v["口径边界"],
             v["产物缺陷"], (r or 0) * 100,
             "超 5%" if (r or 0) > 0.05 else "未超 5%"))
    w("")
    w("「异源」不是产物取值错，而是**同一记录的两份 IHC 只入库了一份**：产物取 A 报告、"
      "专家看 B 报告。这类缺陷的修法是补抽报告、按报告拆 panel，不是改取值。\n")

    w("## 四、多份报告未全量入库（记录级）\n")
    w("判据：块由原文切出，panel 由产物给出；某块在所有 panel 里找不到"
      "取值一致者（HER2 一侧为「不确定」不计冲突，因 ISH 可升级／降级），即该份报告没入库。\n")
    w("| 记录ID | 原文块数 | 入库 panel | 未入库块 | 缺失份数 | 涉及归因 |")
    w("|---|---|---|---|---|---|")
    for r in R["记录级"]:
        w("| %s | %d | %d | %s | %d | %s |"
          % (r["记录ID"], r["原文块数"], r["panel数"],
             "、".join(str(x) for x in r["漏抽块"]), r["缺失份数"], "、".join(r["归因"])))
    w("")
    _n = len(R["记录级"])
    _h = sum(1 for r in R["记录级"] if "异源" in r["归因"])
    w("%d 条记录中 %d 条正是本轮错判所在；其余为未影响判读的旧病理（如 `45835356` 6 年前院外报告）。\n"
      % (_n, _h))

    w("## 五、处置\n")
    w("- **值错** → 取值层缺陷，回抽取层修。")
    w("- **ISH未采纳** → 派生层缺陷：ISH 文字（基因扩增／无扩增）要随 marker 入库。")
    w("- **判据缺失** → 派生层缺陷：`classify_subtype` 缺「PR 阴性或 <20% 即 Luminal B」分支。")
    w("- **异源 · 改选取**（%d 行）→ 两份都在库，只是审核包 `pick_panel` 取了另一份；"
      "改选取规则（按报告逐份出题，而非每记录取一份）。"
      % sum(v["异源处置"].get("改选取", 0) for v in R["按字段"].values()))
    w("- **异源 · 补抽**（%d 行）→ 专家看的那份报告未入库，回抽取层补 panel 后重算。"
      % sum(v["异源处置"].get("补抽", 0) for v in R["按字段"].values()))
    w("- **口径等价 / 口径边界** → 两者都判对，产物无需改。\n")
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

if __name__ == "__main__":
    main()
