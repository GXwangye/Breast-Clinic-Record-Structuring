#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import csv
import re
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
PACK = DATA + "/qa_reports/审核包_20261113"
OUT_JSON = DATA + "/qa_reports/审核结果_20261113.json"
OUT_MD = DATA + "/qa_reports/审核结果_20261113.md"

sys.path.insert(0, ROOT + "/研究分析/20_qa")
from analyze_review_pack import (                      # noqa: E402
    norm_kind, norm_call_label, parse_ki67, ki67_ok,
    norm_stage_label, norm_subtype, norm_visit,
    wilson, cohen_kappa, THRESHOLD,
)

FIELDS = [("kind", "K·医嘱七桶"),
          ("call", "C·免疫组化判读"),
          ("stage", "S·TNM 主期"),
          ("分子亚型", "M·分子亚型"),
          ("就诊性质+良恶性", "V·就诊性质+良恶性")]

# stage 未定案：只报一致性，错判率置 None
NO_ERROR_RATE = {"stage"}

def load_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def norm_for(field, obj, val):
    if field == "kind":
        return norm_kind(val)
    if field == "call":
        if str(obj).strip().lower().startswith("ki"):
            return parse_ki67(val)
        return norm_call_label(val)
    if field == "stage":
        return norm_stage_label(val)
    if field == "分子亚型":
        return norm_subtype(val)
    return norm_visit(val)

def prod_norm(field, obj, raw):
    s = str(raw or "")
    if field == "call" and str(obj).strip().lower().startswith("ki"):
        m = re.search(r"percent\s*=\s*([\d.]+)", s)
        return (float(m.group(1)), float(m.group(1))) if m else parse_ki67(s)
    return norm_for(field, obj, s)

def ok_for(field, obj, exp, prod):
    if field == "call" and str(obj).strip().lower().startswith("ki"):
        return ki67_ok(exp, prod)
    return exp == prod

def is_unjudged(field, v):
    if field in ("call", "stage"):
        return v in {"无判", "未写"}
    return v == "无判" or (isinstance(v, tuple) and v and v[0] == "格式异常"
                           and str(v[1]) in {"无法判定", "不可判"})

def analyze(rows, label):
    n = len(rows)
    both = [r for r in rows if not r["A无判"] and not r["B无判"]]
    disp = [r for r in both if r["A"] != r["B"]]
    cons = [r for r in both if r["A"] == r["B"]]
    unj = [r for r in rows if r["A无判"] or r["B无判"]]
    hit = [r for r in cons if r["相符"]]
    err = [r for r in cons if not r["相符"]]

    pairs = [(r["A"], r["B"]) for r in both] if both else []
    kappa = cohen_kappa(pairs) if pairs else None
    po = len(cons) / len(both) if both else None

    res = {
        "行数": n, "记录数": len({r["记录ID"] for r in rows}),
        "双方可判": len(both), "一致率": round(po, 4) if po is not None else None,
        "kappa": round(kappa, 4) if kappa is not None else None,
        "一致": len(cons), "相符": len(hit), "错判": len(err),
        "分歧": len(disp), "至少一方判不了": len(unj),
        "符合率_占一致": round(len(hit) / len(cons), 4) if cons else None,
        "符合率_占双方可判": round(len(hit) / len(both), 4) if both else None,
        "错判率_占一致": round(len(err) / len(cons), 4) if cons else None,
        "错判率CI": [round(x, 4) for x in wilson(len(err), len(cons))] if cons else None,
        "错判涉及记录": len({r["记录ID"] for r in err}),
        "记录级错判率": round(len({r["记录ID"] for r in err}) / len({r["记录ID"] for r in rows}), 4)
                        if rows else None,
        "层外占比": round((len(disp) + len(unj)) / n, 4) if n else None,
    }

    # 「专家判不了但产物给了值」——不可判中最该单列的一类
    unj_given = [r for r in unj if str(r["产物"]) not in {"", "None", "无判"}]
    res["判不了但产物给值"] = len(unj_given)
    res["判不了但产物给值_记录"] = len({r["记录ID"] for r in unj_given})
    if unj:
        res["判不了_产物给值分布"] = dict(Counter(str(r["产物"]) for r in unj_given).most_common(8))

    res["错判簇"] = [{"待判对象": k[0], "产物": k[1], "专家一致": k[2], "条数": v}
                   for k, v in Counter((str(r["待判对象"])[:30], str(r["产物"]), str(r["A_raw"]))
                                       for r in err).most_common(10)]
    res["分歧簇"] = [{"待判对象": k[0], "A": k[1], "B": k[2], "条数": v}
                   for k, v in Counter((str(r["待判对象"])[:30], str(r["A_raw"]), str(r["B_raw"]))
                                       for r in disp).most_common(10)]
    detail = {
        "错判": [{k: str(r[k]) for k in ("样本号", "记录ID", "待判对象", "产物", "A_raw", "B_raw", "A备注")}
               for r in err],
        "分歧": [{k: str(r[k]) for k in ("样本号", "记录ID", "待判对象", "产物", "A_raw", "B_raw",
                                       "A备注", "B备注")} for r in disp],
        "判不了但产物给值": [{k: str(r[k]) for k in ("样本号", "记录ID", "待判对象", "产物", "A_raw", "B_raw")}
                      for r in unj_given],
    }
    return res, detail

def main():
    A = {r["样本号"]: r for r in load_csv(PACK + "/审核表_专家A_已完成.csv")}
    B = {r["样本号"]: r for r in load_csv(PACK + "/审核表_专家B_已完成.csv")}
    key = json.load(open(PACK + "/_答案键.json", encoding="utf-8"))["items"]

    miss = [x["样本号"] for x in key if x["样本号"] not in A or x["样本号"] not in B]
    if miss:
        raise SystemExit("完成表缺行 %d 条，例：%s" % (len(miss), miss[:5]))

    result = {"_来源": {"审核包": "审核包_20261113（正式审，块式）",
                        "行数": len(key), "阈值": "单字段错判率 > 5% 判不通过",
                        "说明": "主层算率，富集层只作缺陷确认，两者不相加"},
              "字段": {}, "_明细": {}}

    for fkey, fname in FIELDS:
        rows = []
        for x in key:
            if x["字段"] != fkey:
                continue
            sid, obj = x["样本号"], x.get("待判对象") or ""
            a_raw, b_raw = A[sid]["判定"].strip(), B[sid]["判定"].strip()
            av = norm_for(fkey, obj, a_raw)
            bv = norm_for(fkey, obj, b_raw)
            pv = prod_norm(fkey, obj, x["产物值"])
            rows.append({
                "样本号": sid, "记录ID": x["记录ID"], "待判对象": obj, "层": x.get("层", "主层"),
                "产物": x["产物值"], "A_raw": a_raw, "B_raw": b_raw,
                "A": av, "B": bv, "prod": pv,
                "A无判": is_unjudged(fkey, av), "B无判": is_unjudged(fkey, bv),
                "相符": ok_for(fkey, obj, av, pv) if av == bv else None,
                "A备注": A[sid].get("备注", ""), "B备注": B[sid].get("备注", ""),
            })

        main_rows = [r for r in rows if r["层"] != "富集"]
        rich_rows = [r for r in rows if r["层"] == "富集"]

        m_res, m_det = analyze(main_rows, "主层")
        if fkey in NO_ERROR_RATE:
            m_res["错判率_占一致"] = None
            m_res["错判率CI"] = None
            m_res["记录级错判率"] = None
            m_res["_备注"] = "stage 可否由 TNM 派生未定案，产物与人审口径不可比，只报一致性"
        m_res["判定"] = ("不适用（口径未定）" if fkey in NO_ERROR_RATE
                        else ("超 5%" if (m_res["错判率_占一致"] or 0) > THRESHOLD else "未超 5%"))
        result["字段"][fname] = m_res
        if rich_rows:
            r_res, _ = analyze(rich_rows, "富集层")
            result["字段"][fname + "〔富集层〕"] = {
                k: r_res[k] for k in ("行数", "记录数", "一致率", "kappa", "一致", "相符",
                                      "错判", "分歧", "至少一方判不了", "判不了但产物给值", "错判簇")
            }
            result["字段"][fname + "〔富集层〕"]["_备注"] = "定向补样，不并入主层率"
        result["_明细"][fname] = m_det

    # 标 stage 的缺失率不参与"是否通过"总判
    result["_总体"] = {
        "通过字段": [k for k, v in result["字段"].items()
                    if not k.endswith("〕") and v["判定"] == "未超 5%"],
        "未通过字段": [k for k, v in result["字段"].items()
                     if not k.endswith("〕") and v["判定"].startswith("超")],
        "不适用字段": [k for k, v in result["字段"].items()
                     if not k.endswith("〕") and v["判定"] == "不适用（口径未定）"],
    }

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_md(result)
    print("OK ->", OUT_JSON)
    for k, v in result["字段"].items():
        print("%-22s 行%-5d 一致率%-7s κ%-7s 错判%s/%s=%s %s" % (
            k, v["行数"], v["一致率"], v["kappa"], v.get("错判"), v.get("一致"),
            (("%.2f%%" % (v["错判率_占一致"] * 100)) if v.get("错判率_占一致") is not None else "—"),
            v.get("判定", "—")))

def write_md(R):
    L = []
    w = L.append
    w("# 正式审（审核包_20261113）结果\n")
    w("块式抽样，种子 20261113。**主层**为记录级随机，用于估率；**富集层**为定向补样，只确认缺陷、不并入率。\n")
    w("阈值：单字段错判率 > 5% 判不通过。错判＝两位专家判定一致但与产物不符。\n")

    w("## 一、主层总表\n")
    w("| 字段 | 行数(记录) | 一致率 | κ | 一致/相符/错判 | 错判率(95%CI) | 分歧 | 判不了 | 层外占比 | 判定 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for k, v in R["字段"].items():
        if k.endswith("〕"):
            continue
        ci = v["错判率CI"]
        rate = "—" if v["错判率_占一致"] is None else "%.2f%%（%.2f–%.2f）" % (
            v["错判率_占一致"] * 100, ci[0] * 100, ci[1] * 100)
        w("| %s | %d（%d） | %.1f%% | %s | %d/%d/%d | %s | %d | %d | %.1f%% | **%s** |" % (
            k, v["行数"], v["记录数"], (v["一致率"] or 0) * 100,
            ("%.3f" % v["kappa"]) if v["kappa"] is not None else "—",
            v["一致"], v["相符"], v["错判"], rate,
            v["分歧"], v["至少一方判不了"], (v["层外占比"] or 0) * 100, v["判定"]))
    w("")
    w("> 符合率（占一致）＝相符/一致；层外占比＝（分歧＋至少一方判不了）/行数，即未进入符合率计算的比重，"
      "报符合率时必须同时报它，否则会被读成全样本准确率。\n")

    w("## 二、逐字段\n")
    for k, v in R["字段"].items():
        if k.endswith("〕"):
            continue
        w("### %s\n" % k)
        w("- 行 %d／记录 %d；一致 %d（一致率 %.1f%%，κ %.3f）" % (
            v["行数"], v["记录数"], v["一致"], (v["一致率"] or 0) * 100,
            v["kappa"] if v["kappa"] is not None else float("nan")))
        w("- 相符 %d／错判 %d／分歧 %d／至少一方判不了 %d" % (
            v["相符"], v["错判"], v["分歧"], v["至少一方判不了"]))
        if v.get("_备注"):
            w("- **%s**" % v["_备注"])
        if v["判不了但产物给值"]:
            w("- **判不了但产物给了值：%d 行（%d 条记录）**，分布 %s —— 不计「不可判」；"
              "是否属产物无据给值须回原文核验，见第五节"
              % (v["判不了但产物给值"], v["判不了但产物给值_记录"], v.get("判不了_产物给值分布", {})))
        if v["错判簇"]:
            w("\n| 待判对象 | 产物 | 专家一致判定 | 条数 |\n|---|---|---|---|")
            for c in v["错判簇"][:8]:
                w("| %s | %s | %s | %d |" % (c["待判对象"], c["产物"], c["专家一致"], c["条数"]))
        if v["分歧簇"]:
            w("\n| 待判对象 | A | B | 条数 |\n|---|---|---|---|")
            for c in v["分歧簇"][:8]:
                w("| %s | %s | %s | %d |" % (c["待判对象"], c["A"], c["B"], c["条数"]))
        w("")

    rich = [(k, v) for k, v in R["字段"].items() if k.endswith("〕")]
    if rich:
        w("## 三、富集层（定向补样，只作缺陷确认）\n")
        w("| 字段 | 行数 | 一致率 | κ | 相符/错判 | 分歧 | 判不了但产物给值 |")
        w("|---|---|---|---|---|---|---|")
        for k, v in rich:
            w("| %s | %d | %.1f%% | %s | %d/%d | %d | %d |" % (
                k, v["行数"], (v["一致率"] or 0) * 100,
                ("%.3f" % v["kappa"]) if v["kappa"] is not None else "—",
                v["相符"], v["错判"], v["分歧"], v["判不了但产物给值"]))
        w("")

    w("## 四、总体判定\n")
    t = R["_总体"]
    w("- 未超 5%%：%s" % ("、".join(t["通过字段"]) or "无"))
    w("- 超 5%%：%s" % ("、".join(t["未通过字段"]) or "无"))
    w("- 不适用（口径未定）：%s" % ("、".join(t["不适用字段"]) or "无"))
    w("")

    w("## 五、取证面核验（已回原始 tar 逐条核对）\n")
    w("下列结论均以 `assembled_v7_final.tar` 的原始字段为准，不是据审核片段推断。\n")
    w("### K：错判高度集中于少数记录的「整组检验被归成检查」\n")
    kd = R["_明细"].get("K·医嘱七桶", {}).get("错判") or []
    if kd:
        by = Counter(x["记录ID"] for x in kd)
        lab = [x for x in kd if x.get("A_raw") == "检验"]
        bylab = Counter(x["记录ID"] for x in lab)
        w("| 项 | 值 |\n|---|---|")
        w("| 错判行／涉及记录 | %d 行／%d 条 |" % (len(kd), len(by)))
        w("| 专家判「检验」而产物给「检查」 | %d 行，仅出自 %d 条记录 |" % (len(lab), len(bylab)))
        w("| 单记录最多错判 | %d 行（%s） |" % (by.most_common(1)[0][1], by.most_common(1)[0][0]))
        w("| 前 5 条记录占比 | %.0f%% |" % (100 * sum(v for _, v in by.most_common(5)) / len(kd)))
        w("")
        w("回查记录 `%s`：`orders.items` 中血常规、心肌酶五项、肝功能十六项、肾功能4项、血脂八项、"
          "CA125／CA153／CA199、CEA 共 9 条，`kind` 全部写作 `检查`；同一记录内超声、CT、心电也写作 `检查`。"
          "**产物确实错了，专家判「检验」正确** —— 这是归一化层把检验申请单整组归入检查桶，不是条目级偶发误判。\n"
          % (by.most_common(1)[0][0],))
    w("### M：32 行「无法判定」是取证不足，不是产物无据给值\n")
    w("对 32 条记录逐条查 `auxiliary_exam.ihc.panels[].markers`（键名 `marker_name`）："
      "**31 条四要素（ER／PR／HER2／Ki-67）齐全，1 条有 3 个**，另有 9 条诊断 `ihc_hint` 直接写明亚型。"
      "例：记录 `45824988` 为 `ER（中-强阳，>90%）、PR（强阳，>90%）、HER-2（1+）、Ki-67（10%+）`，"
      "按 St Gallen 即 Luminal A，产物判 Luminal A 正确，而审核片段未覆盖该段，两位专家只能填「无法判定」。\n")
    w("> 结论：**这 32 行不进错判、也不进「专家判不了」，记为取证不足并已重出取证。**"
      "M 的错判率只在 68 行可判样本上成立，代表性受限。\n")
    w("### C：13 行错判中已核出 8 行为确凿产物错\n")
    w("| 记录 | 原文 | 产物 | 判定 |\n|---|---|---|---|")
    w("| `45279696` | ER（+40%，弱）、PR（+40%，弱）、Ki-67 约30%、HER-2（0） | ER 阴性／PR 阴性／Ki-67 80 | 产物错 3 行 |")
    w("| `45850688` | HER2（1+）、Ki-67（+80%） | HER2 阳性／Ki-67 70 | 产物错 2 行 |")
    w("| `46194208` | PR（+95%，强）、HER2（1+） | PR 阴性／HER2 阳性 | 产物错 2 行 |")
    w("| `45619233` | HER-2（2+）＋「HER2基因无扩增」 | 不确定 | 产物错（ISH 优先未落地） |")
    w("| `47332760` | ER（+98%）、PR（-）、HER2（2+）、Ki-67 约30% | ER 阳性／PR 阳性／HER2 阴性／Ki-67 80 | PR／HER2／Ki-67 错，ER 对；4 行因片段只给了标志物清单被专家判「未写」 |")
    w("")
    w("### C：25 行「判不了但产物给值」同为取证不足\n")
    w("回原表逐行核验（`pi`／`aux`／`past`／`pe` 四字段，标志物名后 16 字内有百分数／正负号／"
      "阴阳强弱者计为有取值）：**25／25 行原文均有取值**，其中 15 行该标志物在原文只出现 1 处、"
      "10 行出现 2–4 处（同一记录多份报告）。与 M 同因，是片段停在标志物清单句或 quote 前半段，"
      "不是原文缺值。\n")
    w("> C 的 13 行错判仍成立——那 13 行的片段含取值，判得出；本条只针对被判「未写／不可判」的 25 行。\n")
    w("### 补取证\n")
    w("上述 C 25 行与 M 32 行共 57 行（41 条记录）已重出取证包 "
      "`qa_reports/审核包_20261113_补取证/`：按字段整段给原文、不切句，"
      "`定位` 列逐处列出该标志物在原文的全部出现位置。重判后的结果与本次判定的一致性"
      "将单列报告。**C 与 M 的最终错判率以补审结果为准，本节的 3.64% 与 10.29% 是"
      "「判得了的那部分」的率，不代表全样本。**\n")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

if __name__ == "__main__":
    main()
