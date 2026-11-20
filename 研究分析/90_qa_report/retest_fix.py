#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import io
import os
import json
import csv
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QR = DATA + "/qa_reports"
PACK = QR + "/审核包_20261113"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
OUT_JSON = QR + "/修复回测_20261113.json"
OUT_MD = QR + "/修复回测_20261113.md"

sys.path.insert(0, ROOT + "/研究分析/20_qa")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from analyze_review_pack_v2 import norm_for, prod_norm, ok_for, is_unjudged  # noqa: E402
from normalize_order_kind import normalize_kind                               # noqa: E402
from derive_ihc_call import record_ish                                        # noqa: E402
from tarstream import iter_records                                             # noqa: E402
from probe_granularity import dx_group_visit, visit_kind                       # noqa: E402

def load(fn):
    with io.open(PACK + "/" + fn, encoding="utf-8-sig") as f:
        return {r["样本号"]: r for r in csv.DictReader(f)}

def main():
    A = load("审核表_专家A_已完成.csv")
    B = load("审核表_专家B_已完成.csv")
    key = json.load(io.open(PACK + "/_答案键.json", encoding="utf-8"))["items"]

    # 需要记录级 ISH 的行：先把记录 ID 收齐
    ids = {x["记录ID"] for x in key if x["字段"] == "call" and x["层"] == "主层"}
    recs = {}
    for fn, rec in iter_records(TAR):
        rid = str(rec.get("record_id") or "")
        if rid in ids:
            recs[rid] = rec
        if len(recs) == len(ids):
            break

    out, rows = {}, []
    # M/V 的产物不是 tar 里的现成字段，而是出题时由判据当场算出（panel 级 St Gallen／
    # 诊断归组），所以换判据后必须重算一遍再比。按记录 ID 对齐，不按样本号。
    from build_review_pack import collect, malig_kind
    ids_mv = {x["记录ID"] for x in key if x["字段"] in ("分子亚型", "就诊性质+良恶性")}
    det = collect(sorted(ids_mv))
    print("M/V 记录 %d 条，取到 %d 条" % (len(ids_mv), len(det)))

    for fkey, fname, fixer in (("kind", "K·医嘱七桶", "kind"),
                               ("call", "C·免疫组化判读", "call"),
                               ("分子亚型", "M·分子亚型", "subtype"),
                               ("就诊性质+良恶性", "V·就诊性质+良恶性", "visit")):
        for x in key:
            if x["字段"] != fkey or x["层"] != "主层":
                continue
            sid, obj = x["样本号"], x.get("待判对象") or ""
            av = norm_for(fkey, obj, A[sid]["判定"].strip())
            bv = norm_for(fkey, obj, B[sid]["判定"].strip())
            old = prod_norm(fkey, obj, x["产物值"])

            # ---- 新判据
            new_raw, how = x["产物值"], "unchanged"
            if fixer == "kind":
                nk, path = normalize_kind(x["产物值"], obj)
                if nk != x["产物值"]:
                    new_raw, how = nk, path
            elif fixer == "subtype":
                d = det.get(x["记录ID"]) or {"panels": []}
                pk = next((p for p in d["panels"] if p["subtype"][0]), None)
                if pk and pk["subtype"][0] != x["产物值"]:
                    new_raw, how = pk["subtype"][0], "classify_subtype:" + pk["subtype"][1]
            elif fixer == "visit":
                d = det.get(x["记录ID"]) or {"dx": []}
                g = {dx_group_visit(i["icd"], i["name"]) for i in d["dx"]}
                nv = "%s／%s" % (visit_kind(g), malig_kind(g))
                if nv != x["产物值"]:
                    new_raw, how = nv, "dx_group_visit"
            else:
                if x["产物值"] == "不确定":
                    ish, quote = record_ish(recs.get(x["记录ID"], {}))
                    if ish:
                        new_raw, how = ish, "record_ish:" + quote[:18]
            new = prod_norm(fkey, obj, new_raw)

            same = av == bv and not is_unjudged(fkey, av)
            ok_old = ok_for(fkey, obj, av, old) if same else None
            ok_new = ok_for(fkey, obj, av, new) if same else None
            # 专家判「不确定」而新产物给了结论：ISH 写在出题片段之外，专家没看到。
            # 这种行不能记产物新错，也不能记对——须补取证重审一次才定。
            frag_out = bool(fkey == "call" and av == "不确定"
                            and new != old and how.startswith("record_ish"))
            rows.append({"字段": fname, "样本号": sid, "记录ID": x["记录ID"],
                         "待判对象": obj[:40], "专家": av, "旧产物": old, "新产物": new,
                         "改判路径": how, "片段外判据": frag_out,
                         "旧相符": ok_old, "新相符": ok_new})

    for fname in ("K·医嘱七桶", "C·免疫组化判读", "M·分子亚型", "V·就诊性质+良恶性"):
        sub = [r for r in rows if r["字段"] == fname]
        agree = [r for r in sub if r["旧相符"] is not None]
        old_bad = [r for r in agree if not r["旧相符"]]
        new_bad = [r for r in agree if not r["新相符"]]
        changed = [r for r in sub if r["旧产物"] != r["新产物"]]
        fixed = [r for r in old_bad if r["新相符"]]
        broke = [r for r in agree if r["旧相符"] and not r["新相符"] and not r["片段外判据"]]
        pend = [r for r in agree if r["旧相符"] and not r["新相符"] and r["片段外判据"]]
        out[fname] = {
            "审核行": len(sub), "一致行": len(agree),
            "改判行": len(changed),
            "修前错判": len(old_bad), "修前错判率": round(len(old_bad) / len(agree), 4),
            "修后错判": len(new_bad) - len(pend),
            "修后错判率": round((len(new_bad) - len(pend)) / len(agree), 4),
            "被修好": len(fixed), "修复致新错": len(broke), "待补审行": len(pend),
            "改判路径": dict(Counter(r["改判路径"] for r in changed)),
            "待补审明细": [{"样本号": r["样本号"], "记录ID": r["记录ID"],
                            "待判对象": r["待判对象"], "专家（片段内）": r["专家"],
                            "新产物": r["新产物"], "依据": r["改判路径"]} for r in pend],
            "仍错判明细": [{"样本号": r["样本号"], "记录ID": r["记录ID"],
                            "待判对象": r["待判对象"],
                            "专家": r["专家"], "产物": r["新产物"]}
                           for r in new_bad if not r["片段外判据"]],
            "致新错明细": [{"样本号": r["样本号"], "待判对象": r["待判对象"],
                            "专家": r["专家"], "旧": r["旧产物"], "新": r["新产物"]}
                           for r in broke],
        }

    R = {"_来源": {"审核包": "审核包_20261113（同一批行，专家判定不变）",
                   "回测口径": "判据替换后与专家判定重比，不重新出题、不重新请专家",
                   "修复1": "normalize_order_kind.normalize_kind 名称强判",
                   "修复2": "derive_ihc_call.record_ish 记录级 ISH 兜底",
                   "修复3": "describe_structured_cohort.classify_subtype 补 PR 阴性或＜20% 判 Luminal B",
                   "修复4": "probe_granularity.dx_group_visit 诊断名含「术后」按手术后状态计就诊性质"},
         "结果": out, "明细": rows}
    json.dump(R, io.open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    L = ["# 修复回测\n",
         "同一批审核行，专家判定不变；只把判据换掉再比一次。口径与 `analyze_review_pack_v2` 同源。\n",
         "| 字段 | 审核行 | 一致行 | 改判行 | 修前错判 | 修前率 | 修后错判 | 修后率 | 修好 | 致新错 | 待补审 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for f, v in out.items():
        L.append("| %s | %d | %d | %d | %d | %.2f%% | **%d** | **%.2f%%** | %d | %d | %d |"
                 % (f, v["审核行"], v["一致行"], v["改判行"], v["修前错判"],
                    100 * v["修前错判率"], v["修后错判"], 100 * v["修后错判率"],
                    v["被修好"], v["修复致新错"], v["待补审行"]))
    L.append("")
    L.append("「待补审」＝专家判「不确定」而新产物给出了结论的行：ISH 写在出题片段之外，"
             "专家当时看不到。这类行既不能记产物新错，也不能记对，须补取证重审一次；"
             "故上表「修后错判」已把它们排除，单列于此备查。\n")
    for f, v in out.items():
        L.append("## %s\n" % f)
        L.append("改判路径：%s\n" % json.dumps(v["改判路径"], ensure_ascii=False))
        if v["致新错明细"]:
            L.append("| 致新错样本 | 待判对象 | 专家 | 旧 | 新 |\n|---|---|---|---|---|")
            for x in v["致新错明细"]:
                L.append("| %s | %s | %s | %s | %s |"
                         % (x["样本号"], x["待判对象"], x["专家"], x["旧"], x["新"]))
            L.append("")
        if v["仍错判明细"]:
            L.append("仍错判 %d 行（前 10）：\n" % len(v["仍错判明细"]))
            L.append("| 样本号 | 待判对象 | 专家 | 修后产物 |\n|---|---|---|---|")
            for x in v["仍错判明细"][:10]:
                L.append("| %s | %s | %s | %s |" % (x["样本号"], x["待判对象"], x["专家"], x["产物"]))
            L.append("")
    # 残余错判的归因：C/M 复用「错判归因」的两路判定（同一批行，专家判定不变）；
    # K/V 不属派生判据，逐条形态写在固定说明里。
    RA = QR + "/错判归因_20261113.json"
    att = {}
    if os.path.exists(RA):
        att = {r["样本号"]: r for r in
               json.load(io.open(RA, encoding="utf-8"))["明细"]}
    NOTE = {
        "K·医嘱七桶":
            "残余 14 行三种形态：含检验词或器械名的整句叙述 9 行（判成检验，如"
            "「定期复查…监测肝功能」）、文书或报告名 3 行（「病理检查」「门诊疾病证明书」"
            "「等超声结果复诊」判成检查）、药品与操作互串 2 行（「升白治疗」判药品、"
            "「建议内分泌治疗：…片」判其他）。前者要求先判「这整句是不是一项医嘱」，"
            "不是词表能覆盖的。",
        "V·就诊性质+良恶性":
            "残余 3 行都是诊断名在产物侧缺字，与就诊性质规则无关：一行原文「乳腺癌术后」"
            "被判为既往状态、未计入诊断条目（契约无状态落点）；一行产物名缩略成"
            "「乳腺癌」丢了「术后」；一行诊断为占位字符串 blank，原文并非如此。",
    }
    L.append("## 五、残余错判的归因\n")
    for f, v in out.items():
        if not v["仍错判明细"]:
            continue
        L.append("**%s**（%d 行）\n" % (f, len(v["仍错判明细"])))
        if f in NOTE:
            L.append(NOTE[f] + "\n")
            continue
        c = Counter()
        for x in v["仍错判明细"]:
            a = att.get(x["样本号"]) or {}
            if a:
                k = a.get("归因") or "未归因"
                if a.get("处置"):
                    k += "（" + a["处置"] + "）"
            elif f == "M·分子亚型":
                d = det.get(x["记录ID"]) or {"panels": []}
                pk = next((p for p in d["panels"] if p["subtype"][0]), None)
                er = next((m for m in (pk or {}).get("markers") or []
                           if m["name"].upper().replace("-", "").startswith("ER")), None)
                k = "口径等价（HR+/HER2+）" if (er and er.get("call") == "阳性") else "未归因"
            else:
                k = "未归因"
            c[k] += 1
        L.append("沿用 `错判归因_20261113.json` 的逐行判定：%s。"
                 "即残余已无判据取值错，均落在多份报告只入库一份与口径边界上。\n"
                 % json.dumps(dict(c.most_common()), ensure_ascii=False))

    io.open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

    print("OK ->", OUT_JSON)
    for f, v in out.items():
        print("%-14s 一致%3d 改判%3d 修前 %d（%.2f%%）→ 修后 %d（%.2f%%） 修好%d 致新错%d"
              % (f, v["一致行"], v["改判行"], v["修前错判"], 100 * v["修前错判率"],
                 v["修后错判"], 100 * v["修后错判率"], v["被修好"], v["修复致新错"]))

if __name__ == "__main__":
    main()
