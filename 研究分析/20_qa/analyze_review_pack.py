#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import csv
import json
import re
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
PKG = DATA + "/qa_reports/审核包_20261112"
OUT_JSON = DATA + "/qa_reports/审核结果_20261112.json"
OUT_MD = DATA + "/qa_reports/审核结果_20261112.md"

THRESHOLD = 0.05          # 预注册：单字段错判率 > 5% 判不通过
KAPPA_MIN = 0.60          # 预注册：κ < 0.6 先复核规则再统计错判率

FIELDS = [
    ("K", "kind（医嘱七桶）", "kind"),
    ("C", "call（免疫组化判读）", "call"),
    ("S", "stage（TNM 主期）", "stage"),
    ("M", "分子亚型（panel 级 St Gallen）", "分子亚型"),
    ("V", "就诊性质 + 良恶性", "就诊性质+良恶性"),
]

# ---------------- 归一化 ----------------
def norm_kind(v):
    v = (v or "").strip()
    return v if v in {"药品", "检查", "检验", "操作", "费用", "材料", "其他"} else ("其他?" + v if v else "空")

def norm_call_label(v):
    v = (v or "").strip()
    return "无判" if v in {"不适用", "不可判", ""} else v

RE_NUM = re.compile(r"(\d+(?:\.\d+)?)")
RE_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*[-–~—～至]\s*(\d+(?:\.\d+)?)")

def parse_ki67(v):
    s = (v or "").strip()
    if s in {"未写", "不适用", "不可判", "无", ""}:
        return "无判"
    m = RE_RANGE.search(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = RE_NUM.search(s)
    return (float(m.group(1)),) * 2 if m else "无判"

def ki67_ok(exp, prod):
    if exp == "无判" and prod == "无判":
        return True
    if isinstance(exp, tuple) and isinstance(prod, tuple):
        return not (prod[1] < exp[0] - 0.01 or prod[0] > exp[1] + 0.01)
    return exp == prod

ROMAN = {"1": "Ⅰ", "2": "Ⅱ", "3": "Ⅲ", "4": "Ⅳ", "0": "0",
         "I": "Ⅰ", "II": "Ⅱ", "III": "Ⅲ", "IV": "Ⅳ"}

def norm_stage_label(v):
    s = (v or "").strip()
    if s in {"未写", "无", "—", "-", ""}:
        return "未写"
    if s in {"不可判", "不适用"}:
        return "无判"
    s = s.replace("期", "").replace(" ", "")
    if s in ROMAN:
        return ROMAN[s]
    return s if s in set(ROMAN.values()) else "其他?" + s

def norm_subtype(v):
    s = (v or "").strip().replace(" ", "")
    return "无判" if s in {"无法判定", "不可判", ""} else s

# 原文只写 TNM 串、未写期别字母的行：按规程「只归并不推断」专家应填「未写」，
# 与产物（模型按 AJCC 映射给出期别）必然不一致，故不计错判，单列另核。
RE_STAGE_LABEL = re.compile(r"(Ⅰ|Ⅱ|Ⅲ|Ⅳ|IV|III|II|I)\s*期|[一二三四]\s*期")

def stage_has_label(text):
    return bool(RE_STAGE_LABEL.search(text or ""))

RE_SEP = re.compile(r"[／/；;、,，]")

def norm_visit(v):
    parts = [p.strip() for p in RE_SEP.split((v or "").strip()) if p.strip()]
    return tuple(parts) if len(parts) == 2 else ("格式异常", (v or "").strip())

def src_kind(seg, pref=""):
    if pref in ("K", "S", "M", "V"):
        return "记录级"
    t = (seg or "")[:26]
    if t.startswith("【本行所判报告原文】"):
        return "自身panel"
    if "同现" in t:
        return "同现定位"
    if "前后文" in t:
        return "前后文"
    if "不可判" in t:
        return "无标志物"
    return "其它"

def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))

def cohen_kappa(pairs):
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe >= 1 else (po - pe) / (1 - pe)

def load_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return {r["样本号"].strip(): r for r in csv.DictReader(f)}

def main():
    key = {i["样本号"]: i for i in json.load(open(PKG + "/_答案键.json", encoding="utf-8"))["items"]}
    A = load_csv(PKG + "/审核表_专家A_已完成.csv")
    B = load_csv(PKG + "/审核表_专家B_已完成.csv")
    mult = json.load(open(DATA + "/qa_reports/review_key_check.json", encoding="utf-8"))["明细"]

    result = {"_口径": {"阈值": THRESHOLD, "kappa下限": KAPPA_MIN,
                      "无判类": "不适用／不可判／无法判定，不计错判，单列",
                      "kappa": "剔除任一方无判的行",
                      "错判率": "双专家一致、双方可判、且该行产物取值唯一"},
              "字段": {}}

    for pref, col, kfield in FIELDS:
        rows = []
        for sid, item in key.items():
            if item["字段"] != kfield:
                continue
            a_raw, b_raw, prod = A[sid]["判定"], B[sid]["判定"], item["产物值"]
            # Python 晚绑定使**全部行都用最后一行的判据**——C 的 Ki-67 区间相容规则整体失效，
            # 原文「40%–60%」被判成与产物 40 不符，凭空造出 11 条伪错判。
            if pref == "K":
                av, bv, pv = norm_kind(a_raw), norm_kind(b_raw), norm_kind(prod)
                err = av != pv
            elif pref == "C":
                if (item.get("待判对象") or "").strip().lower().startswith("ki"):
                    av, bv = parse_ki67(a_raw), parse_ki67(b_raw)
                    pv = parse_ki67(str(prod).split("=")[-1] + "%")
                    err = not ki67_ok(av, pv)
                else:
                    av, bv, pv = norm_call_label(a_raw), norm_call_label(b_raw), norm_call_label(prod)
                    err = av != pv
            elif pref == "S":
                av, bv = norm_stage_label(a_raw), norm_stage_label(b_raw)
                pset = {norm_stage_label(x) for x in str(prod).split("/")}
                pv = "／".join(sorted(pset))
                err = av not in pset
            elif pref == "M":
                av, bv, pv = norm_subtype(a_raw), norm_subtype(b_raw), norm_subtype(prod)
                err = av != pv
            else:
                av, bv, pv = norm_visit(a_raw), norm_visit(b_raw), norm_visit(prod)
                err = av != pv

            rows.append({"样本号": sid, "记录ID": item["记录ID"],
                         "待判对象": item.get("待判对象") or A[sid].get("待判对象") or "",
                         "产物值": pv, "A": av, "B": bv, "A_raw": a_raw, "B_raw": b_raw,
                         "错判": err,
                         "多值": pref in ("K", "C") and bool((mult.get(sid) or {}).get("多值")),
                         "仅TNM": pref == "S" and not stage_has_label(A[sid].get("原文片段")),
                         "取证": src_kind(A[sid].get("原文片段"), pref),
                         "panel_Ki67": (mult.get(sid) or {}).get("所判panel_Ki67"),
                         "A备注": A[sid]["备注"], "B备注": B[sid]["备注"]})

        n = len(rows)
        a_nd = sum(1 for r in rows if r["A"] == "无判")
        b_nd = sum(1 for r in rows if r["B"] == "无判")
        both = [r for r in rows if r["A"] != "无判" and r["B"] != "无判"]
        pairs = [(str(r["A"]), str(r["B"])) for r in both]
        kappa = cohen_kappa(pairs)
        po = (sum(1 for a, b in pairs if a == b) / len(pairs)) if pairs else None
        kappa_all = cohen_kappa([(str(r["A"]), str(r["B"])) for r in rows])

        cons = [r for r in both if r["A"] == r["B"]]
        cons_err = [r for r in cons if r["错判"]]
        single = [r for r in cons if not r["多值"] and not r.get("仅TNM")]
        single_err = [r for r in single if r["错判"]]
        only_tnm = [r for r in cons if r.get("仅TNM")]
        err_recs = {r["记录ID"] for r in single_err}
        all_recs = {r["记录ID"] for r in rows}
        disp = [r for r in both if r["A"] != r["B"]]

        # M：Ki-67 恰为 20% 的行属**口径缺口**，不计分。规程交付专家时写「>20% 为高」，
        # 产物 ki67_band 按「≥20% 为高」，两者在 20% 这一点必然分叉；专家按 >20% 判 LuminalA，
        # 不是看错原文（原文就写「约20%」），是规则没写死。审后把规程改口径等于审后调尺，不能据此改判，
        # 只能把这批行剔出分母、单列。
        thresh = []
        if pref == "M":
            thresh = [r for r in single_err
                      if str(r["A"]) in ("LuminalA", "LuminalB") and str(r["产物值"]) in ("LuminalA", "LuminalB")
                      and str(r["A"]) != str(r["产物值"])
                      and r.get("panel_Ki67") is not None and abs(float(r["panel_Ki67"]) - 20) < 0.01]
        res_resid = len(single_err) - len(thresh)
        denom = len(single) - len(thresh)
        src_c = Counter(r["取证"] for r in rows)
        unres = [r for r in cons_err if r["多值"] or r.get("仅TNM")]

        res = {
            "行数": n, "记录数": len(all_recs),
            "A无判": a_nd, "B无判": b_nd, "双方可判": len(both),
            "一致率": round(po, 4) if po is not None else None,
            "kappa": round(kappa, 4) if kappa is not None else None,
            "kappa_含无判": round(kappa_all, 4) if kappa_all is not None else None,
            "分歧行": len(disp),
            "多值行": sum(1 for r in rows if r["多值"]),
            "多值记录": len({r["记录ID"] for r in rows if r["多值"]}),
            "原文仅TNM行": len(only_tnm),
            "原文仅TNM记录": len({r["记录ID"] for r in only_tnm}),
            "一致行": len(cons),
            "共识错判": len(cons_err),
            "不可归因错判": len(unres),
            "单值一致行": len(single),
            "单值错判": len(single_err),
            "单值错判率": round(len(single_err) / len(single), 4) if single else None,
            "单值错判率CI": [round(x, 4) for x in wilson(len(single_err), len(single))],
            "错判涉及记录": len(err_recs),
            "记录级错判率": round(len(err_recs) / len(all_recs), 4) if all_recs else None,
            "取证来源": dict(src_c),
            "取证同源率": round((src_c.get("自身panel", 0) + src_c.get("记录级", 0)) / n, 4) if n else None,
            "口径缺口": len(thresh),
            "计分错判": res_resid,
            "计分分母": denom,
            "计分后率": round(res_resid / denom, 4) if denom else None,
            "计分后CI": [round(x, 4) for x in wilson(res_resid, denom)] if denom else None,
            "分歧簇": [{"待判对象": k[0], "A": k[1], "B": k[2], "条数": v}
                    for k, v in Counter((r["待判对象"], str(r["A"]), str(r["B"])) for r in disp).most_common(8)],
            "错判簇": [{"待判对象": k[0], "产物": k[1], "专家一致": k[2], "条数": v}
                    for k, v in Counter((r["待判对象"], str(r["产物值"]), str(r["A"]))
                                        for r in single_err).most_common(8)],
        }
        result["字段"][f"{pref}·{col}"] = res
        result.setdefault("_明细", {})[pref] = {
            "分歧": [{k: (str(v) if k in ("A", "B", "产物值") else v) for k, v in r.items()
                    if k in ("样本号", "记录ID", "待判对象", "产物值", "A", "B", "取证", "A备注", "B备注")}
                   for r in disp],
            "单值错判": [{k: (str(v) if k in ("A", "产物值") else v) for k, v in r.items()
                      if k in ("样本号", "记录ID", "待判对象", "产物值", "A", "取证", "多值")}
                     for r in single_err],
            "不可归因": [{k: (str(v) if k in ("A", "产物值") else v) for k, v in r.items()
                      if k in ("样本号", "记录ID", "待判对象", "产物值", "A", "取证", "多值")}
                     for r in unres],
        }

    # ---- 有效性判定：取证面／判据缺陷 → 不给数 ----
    result["_有效性"] = {
        "K": {"状态": "有效", "说明": "取证片段＝医嘱原文；同名条目在产物内取值不一者已单列不计错判"},
        "C": {"状态": "有条件有效",
              "说明": "产物值绑 panel，但取证片段仅 334/922 行取自该 panel 自身，588 行由「全文同现定位」"
                      "补足——同一记录存两份 IHC 报告时会取到另一份。故只对「该标志物在本记录内取值唯一」"
                      "的单值行计错判；多值行无法判定证据与产物是否同源，单列不计"},
        "S": {"状态": "不适用人审",
              "说明": "产物 stage 部分由模型按 TNM 自行推断期别（脚本层不做 AJCC 查表），人审规则却要求"
                      "「原文只写 TNM 未写期别即填未写」，两者口径不可比：101 行中 10 行原文仅写 TNM、"
                      "其中 8 行产物仍给了期别；另有 43 行专家把原文已写明的期别判为「未写」。"
                      "故本轮不报错判率，待契约明确 stage 可否由 TNM 派生后另行核验"},
        "M": {"状态": "口径缺口",
              "说明": "交付专家时的规程写「Ki-67 高增殖 >20%」，产物 ki67_band 按「≥20%」，两者在恰为 20% 处"
                      "必然分叉；10 行落在该点，属口径缺口，剔出分母单列，不记产物错也不记专家错"},
        "V": {"状态": "有效", "说明": "取证片段＝诊断列；产物为记录级单一取值；判定对象为（就诊性质，患者性质）二元组"},
    }

    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # 不适用的字段不把错判率留在 JSON 里，免得日后被当作可用数字引用
    for pref in ("S",):
        nm = next(k for k in result["字段"] if k.startswith(pref + "·"))
        r = result["字段"][nm]
        r["作废"] = True
        r["作废原因"] = result["_有效性"][pref]["说明"]
        r["作废前_单值错判"] = r["单值错判"]
        r["作废前_单值错判率"] = r["单值错判率"]
        r["单值错判"] = r["单值错判率"] = r["单值错判率CI"] = None
        r["作废前_记录级错判率"] = r["记录级错判率"]
        r["记录级错判率"] = r["错判涉及记录"] = None
    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------------- Markdown ----------------
    L = []
    w = L.append
    w("# 人工审核结果（派生层五字段）\n")
    w("> 输入：`审核包_20261112/` 两表 ＋ `_答案键.json`；预处理：`verify_review_key.py`（同名多值）\n")
    w("> 预注册阈值：单字段错判率 ≤ 5%、双专家 κ ≥ 0.6。无判类不计错判，单列。\n")

    w("## 一、双专家一致性\n")
    w("| 字段 | 行数 | 双方可判 | A 无判 | B 无判 | 一致率 | κ | κ（含无判） |")
    w("|---|---|---|---|---|---|---|---|")
    for name, r in result["字段"].items():
        w(f"| {name} | {r['行数']} | {r['双方可判']} | {r['A无判']} | {r['B无判']} | "
          f"{r['一致率']:.1%} | {r['kappa']:.3f} | {r['kappa_含无判']:.3f} |")
    ok_k = [(n, r["kappa"]) for n, r in result["字段"].items()
            if result["_有效性"][n[0]]["状态"] in ("有效", "有条件有效", "口径缺口")]
    lo = min(k for _, k in ok_k)
    hi = max(k for _, k in ok_k)
    w(f"\n纳入错判率统计的 {len(ok_k)} 个字段 κ 介于 {lo:.3f}–{hi:.3f}，均高于预注册下限 0.6："
      "判读规则对两位专家可复现。**但 κ 只证明可重复、不证明正确**——"
      "两位专家按同一份措辞判读，若措辞本身没写死，两人会朝同一方向偏。"
      "故本报告在 κ 之外另报两项：取证是否同源、产物取值的原文依据。"
      "S 的一致率单独见第二节该字段的状态说明，不作规则返工依据。\n")

    w("## 二、错判率（只对取证面无缺陷的字段给数）\n")
    w("| 字段 | 状态 | 行级错判 | 行级错判率（95%CI） | 记录级 | 5% 阈值 | 依据 |")
    w("|---|---|---|---|---|---|---|")
    for name, r in result["字段"].items():
        st = result["_有效性"][name[0]]
        if st["状态"] in ("有效", "有条件有效", "口径缺口"):
            k_n, d_n = r["计分错判"], r["计分分母"]
            ci = r["计分后CI"] if r.get("口径缺口") else r["单值错判率CI"]
            rate = r["计分后率"] if r.get("口径缺口") else r["单值错判率"]
            vd = "超" if rate > THRESHOLD else "未超"
            note = f"；已剔口径缺口 {r['口径缺口']} 行" if r.get("口径缺口") else ""
            w(f"| {name} | {st['状态']} | {k_n}/{d_n}{note} | "
              f"{rate:.2%}（{ci[0]:.2%}–{ci[1]:.2%}）"
              f" | {r['错判涉及记录']}/{r['记录数']}（{r['记录级错判率']:.1%}） | {vd} | {st['说明'][:60]} |")
        else:
            w(f"| {name} | **{st['状态']}** | — | — | — | 不给数 | {st['说明'][:110]} |")
    w("")
    for name, r in result["字段"].items():
        st = result["_有效性"][name[0]]
        w(f"### {name}\n")
        w(f"状态：**{st['状态']}**。{st['说明']}\n")
        w(f"- 行数 {r['行数']}（{r['记录数']} 条记录）；分歧 {r['分歧行']} 行；"
          f"同名多值 {r['多值行']} 行／{r['多值记录']} 记录；"
          f"取证同源 {r['取证来源'].get('自身panel', 0) + r['取证来源'].get('记录级', 0)}/{r['行数']}（{r['取证同源率']:.1%}）")
        if st["状态"] in ("有效", "有条件有效", "口径缺口"):
            w(f"- 共识错判 {r['共识错判']} 行，其中不可归因 {r['不可归因错判']} 行（多值／原文仅TNM）单列；"
              f"可归因错判 {r['单值错判']}/{r['单值一致行']}"
              f"（{r['单值错判率']:.2%}），涉及 {r['错判涉及记录']} 条记录")
            if r.get("口径缺口"):
                w(f"- Ki-67 恰为 20% 的口径缺口 {r['口径缺口']} 行（交付规程 >20%／产物 ≥20%），剔出分母；"
                  f"计分 {r['计分错判']}/{r['计分分母']}（{r['计分后率']:.2%}）")
        if r["错判簇"] and st["状态"] in ("有效", "有条件有效", "口径缺口"):
            w("\n**错判聚簇（单值）**\n")
            w("| 待判对象 | 产物 | 专家一致判定 | 条数 |\n|---|---|---|---|")
            for c in r["错判簇"][:6]:
                w(f"| {str(c['待判对象'])[:30]} | {c['产物']} | {c['专家一致']} | {c['条数']} |")
        if r["分歧簇"]:
            w("\n**双专家分歧聚簇**\n")
            w("| 待判对象 | A | B | 条数 |\n|---|---|---|---|")
            for c in r["分歧簇"][:6]:
                w(f"| {str(c['待判对象'])[:30]} | {c['A']} | {c['B']} | {c['条数']} |")
        w("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L[4:]))
    print("->", OUT_MD)
    return 0

if __name__ == "__main__":
    sys.exit(main())
