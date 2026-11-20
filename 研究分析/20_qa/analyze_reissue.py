#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import csv
import re
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QR = DATA + "/qa_reports"
PACK1 = QR + "/审核包_20261113"            # 正式审（第一次）
PACK2 = QR + "/审核包_20261113_补取证"       # 补取证（第二次）
OUT_JSON = QR + "/补取证结果_20261113.json"
OUT_MD = QR + "/补取证结果_20261113.md"

sys.path.insert(0, ROOT + "/研究分析/20_qa")
from analyze_review_pack_v2 import (           # noqa: E402
    norm_for, prod_norm, ok_for, is_unjudged, load_csv,
)
from analyze_review_pack import wilson, cohen_kappa, THRESHOLD   # noqa: E402

# 补取证包的答案键用短名 C／M（生成脚本按板块缩写写），正式包用契约字段名。
# 两包要能对齐才能把补判结果并回主层——这里显式映射，不做隐式猜测。
FIELD_ALIAS = {"C": "call", "M": "分子亚型", "K": "kind", "S": "stage", "V": "就诊性质+良恶性"}

def canon_field(f):
    return FIELD_ALIAS.get(f, f)

def judge(fkey, obj, a_raw, b_raw, prod_raw):
    av, bv = norm_for(fkey, obj, a_raw), norm_for(fkey, obj, b_raw)
    pv = prod_norm(fkey, obj, prod_raw)
    a_nd, b_nd = is_unjudged(fkey, av), is_unjudged(fkey, bv)
    both = not a_nd and not b_nd
    same = both and av == bv
    return {
        "A": av, "B": bv, "prod": pv, "A无判": a_nd, "B无判": b_nd,
        "一致": same, "相符": (ok_for(fkey, obj, av, pv) if same else None),
    }

def main():
    A1 = {r["样本号"]: r for r in load_csv(PACK1 + "/审核表_专家A_已完成.csv")}
    B1 = {r["样本号"]: r for r in load_csv(PACK1 + "/审核表_专家B_已完成.csv")}
    A2 = {r["样本号"]: r for r in load_csv(PACK2 + "/审核表_专家A_已完成.csv")}
    B2 = {r["样本号"]: r for r in load_csv(PACK2 + "/审核表_专家B_已完成.csv")}
    key2 = json.load(open(PACK2 + "/_答案键.json", encoding="utf-8"))["items"]
    key1 = {x["样本号"]: x for x in
            json.load(open(PACK1 + "/_答案键.json", encoding="utf-8"))["items"]}

    miss = [x["样本号"] for x in key2
            if x["样本号"] not in A2 or x["样本号"] not in B2
            or x["样本号"] not in A1 or x["样本号"] not in B1]
    if miss:
        raise SystemExit("缺行 %d：%s" % (len(miss), miss[:5]))

    rows = []
    for x in key2:
        sid, fkey, obj = x["样本号"], canon_field(x["字段"]), x.get("待判对象") or ""
        prod = x["产物值"]
        r1 = judge(fkey, obj, A1[sid]["判定"].strip(), B1[sid]["判定"].strip(), prod)
        r2 = judge(fkey, obj, A2[sid]["判定"].strip(), B2[sid]["判定"].strip(), prod)
        rows.append({
            "样本号": sid, "字段": fkey, "记录ID": x["记录ID"],
            "待判对象": obj, "产物": prod,
            "A1_raw": A1[sid]["判定"].strip(), "B1_raw": B1[sid]["判定"].strip(),
            "A2_raw": A2[sid]["判定"].strip(), "B2_raw": B2[sid]["判定"].strip(),
            "A2备注": A2[sid].get("备注", ""), "B2备注": B2[sid].get("备注", ""),
            "一": r1, "二": r2,
        })

    def sumup(sub):
        n = len(sub)
        # 第一次：可判＝双方都给了取值；补审：同
        def pack(which):
            both = [r for r in sub if not r[which]["A无判"] and not r[which]["B无判"]]
            cons = [r for r in both if r[which]["一致"]]
            err = [r for r in cons if not r[which]["相符"]]
            unj = [r for r in sub if r[which]["A无判"] or r[which]["B无判"]]
            pairs = [(r[which]["A"], r[which]["B"]) for r in both]
            return {
                "行数": n, "双方可判": len(both), "至少一方判不了": len(unj),
                "一致": len(cons), "分歧": len(both) - len(cons),
                "错判": len(err), "相符": len(cons) - len(err),
                "错判率": round(len(err) / len(cons), 4) if cons else None,
                "错判率CI": [round(v, 4) for v in wilson(len(err), len(cons))] if cons else None,
                "一致率": round(len(cons) / len(both), 4) if both else None,
                "kappa": (round(cohen_kappa(pairs), 4) if pairs else None),
            }
        p1, p2 = pack("一"), pack("二")
        return {"第一次": p1, "补审": p2}

    # ---- 一、证据效应：同一行两次判定是否变化 ----
    chg = [r for r in rows if (r["一"]["A"], r["一"]["B"]) != (r["二"]["A"], r["二"]["B"])]
    # 只要一方变了就算变
    from_unjudged = [r for r in chg
                     if (r["一"]["A无判"] or r["一"]["B无判"])
                     and not (r["二"]["A无判"] or r["二"]["B无判"])]
    still_unj = [r for r in rows
                 if (r["一"]["A无判"] or r["一"]["B无判"])
                 and (r["二"]["A无判"] or r["二"]["B无判"])]

    # ---- 二、最终率：补判结果替换正式审对应行，重算主层 ----
    final = {}
    for fkey, fname in (("call", "C·免疫组化判读"), ("分子亚型", "M·分子亚型")):
        # 正式审主层该字段全部行
        main_rows = [x for x in key1.values()
                     if x["字段"] == fkey and x.get("层", "主层") != "富集"]
        reissued = {r["样本号"]: r for r in rows if r["字段"] == fkey}
        recalc = []
        for x in main_rows:
            sid, obj, prod = x["样本号"], x.get("待判对象") or "", x["产物值"]
            if sid in reissued:                      # 用补审结果
                rr = reissued[sid]["二"]
                recalc.append({"sid": sid, "src": "补审", "rec": x["记录ID"],
                               "一致": rr["一致"], "相符": rr["相符"],
                               "A无判": rr["A无判"], "B无判": rr["B无判"],
                               "A": rr["A"], "B": rr["B"], "A_raw": reissued[sid]["A2_raw"],
                               "B_raw": reissued[sid]["B2_raw"], "产物": prod,
                               "obj": obj})
            else:                                    # 沿用正式审判定
                rr = judge(fkey, obj, A1[sid]["判定"].strip(), B1[sid]["判定"].strip(), prod)
                recalc.append({"sid": sid, "src": "首审", "rec": x["记录ID"],
                               "一致": rr["一致"], "相符": rr["相符"],
                               "A无判": rr["A无判"], "B无判": rr["B无判"],
                               "A": rr["A"], "B": rr["B"], "A_raw": A1[sid]["判定"].strip(),
                               "B_raw": B1[sid]["判定"].strip(), "产物": prod, "obj": obj})
        both = [r for r in recalc if not r["A无判"] and not r["B无判"]]
        cons = [r for r in both if r["一致"]]
        err = [r for r in cons if not r["相符"]]
        unj = [r for r in recalc if r["A无判"] or r["B无判"]]
        n = len(recalc)
        final[fname] = {
            "行数": n, "补审替换行": len(reissued),
            "双方可判": len(both), "至少一方判不了": len(unj),
            "层外占比": round((len(both) - len(cons) + len(unj)) / n, 4) if n else None,
            "一致": len(cons), "分歧": len(both) - len(cons),
            "错判": len(err), "相符": len(cons) - len(err),
            "错判率": round(len(err) / len(cons), 4) if cons else None,
            "错判率CI": [round(v, 4) for v in wilson(len(err), len(cons))] if cons else None,
            "判定": "超 5%" if (len(err) / len(cons) if cons else 0) > THRESHOLD else "未超 5%",
            "错判簇": [{"样本号": k[0], "对象": k[1], "产物": k[2], "专家一致": k[3], "条数": v}
                     for k, v in Counter((r["sid"][:4], str(r["obj"])[:10], str(r["产物"]),
                                          str(r["A_raw"])) for r in err).most_common(10)],
            "错判明细": [{k: str(r[k]) for k in ("sid", "rec", "obj", "产物", "A_raw", "B_raw", "src")}
                       for r in err],
        }

    # 正式审对应口径（未补审）作对照
    base = {}
    for fkey, fname in (("call", "C·免疫组化判读"), ("分子亚型", "M·分子亚型")):
        main_rows = [x for x in key1.values()
                     if x["字段"] == fkey and x.get("层", "主层") != "富集"]
        recalc = []
        for x in main_rows:
            rr = judge(fkey, x.get("待判对象") or "", A1[x["样本号"]]["判定"].strip(),
                       B1[x["样本号"]]["判定"].strip(), x["产物值"])
            recalc.append(rr)
        both = [r for r in recalc if not r["A无判"] and not r["B无判"]]
        cons = [r for r in both if r["一致"]]
        err = [r for r in cons if not r["相符"]]
        base[fname] = {"双方可判": len(both), "一致": len(cons), "错判": len(err),
                       "错判率": round(len(err) / len(cons), 4) if cons else None,
                       "至少一方判不了": len(recalc) - len(both)}

    out = {
        "_来源": {"补取证包": "审核包_20261113_补取证（57 行 / 41 块）",
                  "首次": "审核包_20261113（正式审主层同一批行）",
                  "口径": "归一函数与 analyze_review_pack_v2 同源；错判＝两位专家一致但与产物不符",
                  "阈值": "单字段错判率 > 5% 判不通过"},
        "一_证据效应": {
            "总行数": len(rows),
            "两次判定有变化": len(chg),
            "首次判不了、补审判出了": len(from_unjudged),
            "两次都判不了": len(still_unj),
            "两次都判不了_明细": [{"样本号": r["样本号"], "记录ID": r["记录ID"],
                              "对象": r["待判对象"], "产物": r["产物"],
                              "A": r["A2_raw"], "B": r["B2_raw"],
                              "A备注": r["A2备注"][:120], "B备注": r["B2备注"][:120]}
                             for r in still_unj],
            "按字段": {f: sumup([r for r in rows if r["字段"] == f]) for f in ("call", "分子亚型")},
        },
        "二_最终率": {"补审前": base, "补审后": final},
        "_明细": [{"样本号": r["样本号"], "字段": r["字段"], "记录ID": r["记录ID"],
                   "对象": r["待判对象"], "产物": str(r["产物"]),
                   "首审A": r["A1_raw"], "首审B": r["B1_raw"],
                   "补审A": r["A2_raw"], "补审B": r["B2_raw"],
                   "补审A备注": r["A2备注"], "补审B备注": r["B2备注"]} for r in rows],
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_md(out)
    print("OK ->", OUT_JSON)
    e = out["一_证据效应"]
    print("证据效应：%d 行中 %d 行判定变化，其中首次判不了→补审判出 %d 行；两次都判不了 %d 行"
          % (e["总行数"], e["两次判定有变化"], e["首次判不了、补审判出了"], e["两次都判不了"]))
    for f, v in out["二_最终率"]["补审后"].items():
        b = out["二_最终率"]["补审前"][f]
        print("%-16s 补审前 %s/%s=%s → 补审后 %s/%s=%s %s"
              % (f, b["错判"], b["一致"], ("%.2f%%" % (b["错判率"] * 100)) if b["错判率"] is not None else "—",
                 v["错判"], v["一致"], ("%.2f%%" % (v["错判率"] * 100)) if v["错判率"] is not None else "—",
                 v["判定"]))

def write_md(R):
    L = []
    w = L.append
    e = R["一_证据效应"]
    w("# 补取证结果（审核包_20261113_补取证，57 行）\n")
    w("同一批行审了两次：**首审**片段只覆盖 IHC 串的前半段（标志物清单句或 quote 一句），"
      "**补审**按字段整段给原文、`定位` 逐处列出该标志物位置。\n")
    w("目的：① 判断首审的「未写／不可判」是不是取证造成的假信号；② 把补判结果并回主层，"
      "重算 C 与 M 的最终错判率。\n")

    w("## 一、证据效应（同一行两次判定）\n")
    w("| 项 | 行数 |")
    w("|---|---|")
    w("| 补取证总行数 | %d |" % e["总行数"])
    w("| 两次判定有变化 | **%d（%.0f%%）** |"
      % (e["两次判定有变化"], 100 * e["两次判定有变化"] / e["总行数"]))
    w("| 首审判不了 → 补审判出了取值 | **%d（%.0f%%）** |"
      % (e["首次判不了、补审判出了"], 100 * e["首次判不了、补审判出了"] / e["总行数"]))
    w("| 两次都判不了 | %d |" % e["两次都判不了"])
    w("")
    for f, v in e["按字段"].items():
        n1, n2 = v["第一次"], v["补审"]
        w("### %s\n" % ("C·免疫组化判读" if f == "call" else "M·分子亚型"))
        w("| 项 | 首审 | 补审 |")
        w("|---|---|---|")
        w("| 双方可判 | %d | %d |" % (n1["双方可判"], n2["双方可判"]))
        w("| 至少一方判不了 | %d | **%d** |" % (n1["至少一方判不了"], n2["至少一方判不了"]))
        w("| 两人一致 | %d | %d |" % (n1["一致"], n2["一致"]))
        w("| 其中与产物相符 | %d | %d |" % (n1["相符"], n2["相符"]))
        w("| 其中与产物不符 | %d | %d |" % (n1["错判"], n2["错判"]))
        w("| 一致率 | %s | %s |" % (n1["一致率"], n2["一致率"]))
        w("| κ | %s | %s |" % (n1["kappa"], n2["kappa"]))
        w("")

    if e["两次都判不了_明细"]:
        w("### 两次都判不了的 %d 行\n" % e["两次都判不了"])
        w("| 样本号 | 记录ID | 对象 | 产物 | 补审 A | 补审 B |")
        w("|---|---|---|---|---|---|")
        for x in e["两次都判不了_明细"]:
            w("| %s | %s | %s | %s | %s | %s |"
              % (x["样本号"], x["记录ID"], x["对象"], x["产物"], x["A"][:22], x["B"][:22]))
        w("")
        w("两位专家在拿到整段原文后**仍判不了**，这类才属「原文本身不足以判定」，"
          "应与「取证不足」分开记。\n")

    w("## 二、最终错判率（补审结果并回主层）\n")
    w("| 字段 | 补审替换行 | 双方可判 | 一致 | 错判 | 错判率（95%CI） | 首审同口径 | 判定 |")
    w("|---|---|---|---|---|---|---|---|")
    for f, v in R["二_最终率"]["补审后"].items():
        b = R["二_最终率"]["补审前"][f]
        ci = v["错判率CI"]
        w("| %s | %d | %d | %d | **%d** | **%.2f%%**（%.2f–%.2f） | %.2f%% | %s |"
          % (f, v["补审替换行"], v["双方可判"], v["一致"], v["错判"],
             (v["错判率"] or 0) * 100, ci[0] * 100, ci[1] * 100,
             (b["错判率"] or 0) * 100, v["判定"]))
    w("")
    w("首审同口径的分母是「判得了的那部分」，补审把所有行都纳进来了，"
      "两者的差就是取证不足造成的偏倚。\n")

    for f, v in R["二_最终率"]["补审后"].items():
        if not v["错判明细"]:
            continue
        w("### %s 的 %d 行错判\n" % (f, v["错判"]))
        w("| 样本号 | 记录ID | 对象 | 产物 | 专家一致判定 | 判定来源 |")
        w("|---|---|---|---|---|---|")
        for x in v["错判明细"]:
            w("| %s | %s | %s | %s | %s | %s |"
              % (x["sid"], x["rec"], x["obj"], x["产物"], x["A_raw"], x["src"]))
        w("")
    w("## 三、错判归因与处置\n")
    w("上表的错判行不能直接当「产物错」——同一记录若存两份以上 IHC，审核包只给了单份 panel "
      "的产物值，却可能给了另一份的原文。逐行归因见 `错判归因_20261113.md`"
      "（脚本 `研究分析/90_qa_report/audit_error_attribution.py`），交叉核验见 `同源核验_20261113.md`。\n")
    attr_f = QR + "/错判归因_20261113.json"
    if os.path.exists(attr_f):
        AT = json.load(open(attr_f, encoding="utf-8"))
        w("| 字段 | 一致行 | 原报错判 | 产物缺陷（真错） | 真实错判率 | 异源 | 口径等价／边界 | 判定 |")
        w("|---|---|---|---|---|---|---|---|")
        for f, v in AT["按字段"].items():
            r = v["真实错判率"] or 0
            w("| %s | %d | %d（%.2f%%） | **%d** | **%.2f%%** | %d | %d | %s |"
              % (f, v["一致行"], v["错判行"], (v["原报错判率"] or 0) * 100,
                 v["产物缺陷"], r * 100, v["异源"], v["口径等价"] + v["口径边界"],
                 "超 5%" if r > 0.05 else "未超 5%"))
        w("")
        w("处置按缺陷所在层分派：\n")
        w("- **判据缺失 / ISH未采纳** → 派生层，改 `classify_subtype` 与 ISH 入库")
        w("- **异源 · 改选取** → 两份都在库、审核包取错份，改 `pick_panel` 选取规则")
        w("- **异源 · 补抽** → 专家看的那份报告未入库，回抽取层补 panel 后重算")
        w("- **口径等价** → `HR+/HER2+` 归 `Luminal B` 或 `HER2 阳性型` 两者皆判对，不计错")
        w("- **口径边界** → 原文写区间、产物取一端，两者皆判对，不计错\n")
    else:
        w("（未找到 `错判归因_20261113.json`，先跑 `研究分析/90_qa_report/audit_error_attribution.py`）\n")
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

if __name__ == "__main__":
    main()
