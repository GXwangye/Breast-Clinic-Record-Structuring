# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import re

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"

# ---- 契约闭集（与 v7 契约第十二节逐字一致；改这里必须先改契约） ----
CLOSED = {
    "report_type": ["超声", "钼靶", "X线", "CT", "MRI", "核医学", "分子与基因检测",
                    "骨密度", "病理", "心电", "造影", "内镜", "其他"],
    "body_site": ["乳腺", "甲状腺", "甲状旁腺", "胸壁", "肝", "胆", "胰", "脾", "肾",
                  "肺", "纵隔", "胸膜腔", "骨", "盆腔",
                  "腋窝淋巴结", "颈部淋巴结", "锁骨上淋巴结", "内乳淋巴结", "其他"],
    "laterality": ["左", "右", "双", "未指明"],
}

# ---- 规则层：顺序即优先级，**不得随意调换** ----
# 几条顺序的由来（踩过才这么排）：
#   · 病理在影像之前：`病理报告`／`冰冻切片报告` 带「报告」字样，先被影像通用规则抓走就没救了；
#   · 分子在 CT 之前：`CTC检测` 含 `CT`，CT 规则若在前会把它判成 CT 检查；
#   · 核医学在 CT 之前：`PET/CT` 是核医学检查，不是普通 CT；
#   · 超声在造影之前：`脏器声学造影` 是超声造影，不是 X 线血管造影。
RULES = {
    "report_type": [
        ("病理", r"病理|path|冰冻|冷冻|frozen|切片|细胞学|cytolog|组织学|石蜡|"
                 r"免疫组化|IHC|特殊染色|special.?stain|histo|"
                 r"标本|specimen|(?<!液体)活检|biopsy|切除|resection|根治术|mastect|excis|"
                 r"穿刺|gross|surgical|石蜡|细胞块|细胞涂片|cyt|涂片|特染|"
                 r"前哨淋巴结(?!超声|显像)|淋巴结活检|"
                 r"淋巴结清扫|淋巴结评估|sentinel|"
                 r"会诊|consult|术中诊断|快速诊断|切缘|margin|石蜡切片|冰残"),
        ("核医学", r"核医学|核素|显像|骨扫描|bone.?scan|ECT|SPECT|PET|nuclear|同位素|^BS$"),
        ("分子与基因检测", r"FISH|原位杂交|基因|分子|molecular|oncotype|CTC|液体活检|"
                          r"扩增|分型|gene|genetic|amplification|hybridization|循环肿瘤细胞"),
        ("钼靶", r"钼靶|钼钯|钼铑|mammo|^MG$|乳腺X"),
        ("MRI", r"磁共振|核磁|MRI|\bMR\b|MR平扫|DWI|磁共振"),
        ("CT", r"CT|计算机断层|平扫|增强|断层|plain_scan"),
        ("超声", r"超声|彩超|B超|ultrasound|ultrasonograph|sonograph|\bUS\b|多普勒|"
                 r"弹性成像|CDFI|声学造影|浅表器官|echo|ult|US_|elastograph|doppler"),
        # 骨密度必须在 X线 之前：`双能X线骨密度` 含「X线」，X线规则在前就被它抢走。
        # 骨密度是独立模态（DEXA／双能 X 线），乳腺门诊用它监测内分泌治疗期间的骨量。
        ("骨密度", r"骨密度|骨量|DEXA|DXA|bone.?densit|bone_density"),
        ("X线", r"X线|X光|DR|摄片|胸片|正位|侧位|平片|^XR$|x_?ray"),
        ("心电", r"心电|ECG|ekg"),
        ("造影", r"造影|DSA|血管造影"),
        ("内镜", r"内镜|乳管镜|胃镜|肠镜|支气管镜|腔镜"),
        ("其他", r"影像|imaging|检查|报告|描述|结论|所见|findings|impression|conclusion|"
                 r"诊断|评估|复查|随访|常规|综合|发现|背景|参考|结构|其他|补充|区域|"
                 r"病灶|主报告|primary|main|single|routine|exam|structur|"
                 r"放射|diagnostic|finding|description|mass|cyst|"
                 r"结节|病变|BI-RADS|分级|分类|术前|术后|本院|未见异常|对比|incidental|"
                 r"查体|手术|普通|淋巴结|lymph_node|背部|皮下|软组织|soft_tissue|"
                 r"breast|乳腺|low_echo|DXA|final_diagnosis|secondary|surgery|^2$|^$"),
    ],
    "body_site": [
        ("内乳淋巴结", r"内乳|乳内"),
        ("锁骨上淋巴结", r"锁骨上"),
        ("腋窝淋巴结", r"腋窝|腋下|腋部|腋窝下|前哨|腋"),
        ("颈部淋巴结", r"颈部|颈淋巴结|锁骨下|(?<!宫)颈(?!椎)"),
        ("胸壁", r"胸壁|胸肌|胸大肌"),
        ("甲状旁腺", r"甲状旁腺"),
        ("甲状腺", r"甲状腺"),
        ("乳腺", r"乳腺|乳房|breast|mammary|乳头|腺体|乳"),
        ("纵隔", r"纵隔"),
        ("胸膜腔", r"胸腔|胸膜"),
        ("肺", r"肺"),
        ("骨", r"脊柱|椎体|椎|肋骨|骨盆|骶骨|股骨|肩胛|颅骨|骨骼|全身骨|^骨"),
        ("肝", r"肝"),
        ("胆", r"胆"),
        ("胰", r"胰"),
        ("脾", r"脾"),
        ("肾", r"肾"),
        ("盆腔", r"盆腔|附件|宫颈|子宫|卵巢|膀胱"),
    ],
    "laterality": [
        ("双", r"^双|双侧|两边|两侧"),
        ("左", r"^左|左侧|指左"),
        ("右", r"^右|右侧|指右"),
        ("未指明", r"未指明|不明|不详|未标注|全身|多发|多处|中线|^1$"),
    ],
}

# 「没写」不等于「其他」——三态显式铁律。留空并落 qc，不送 LLM，也不塞进闭集。
EMPTY_RE = r"^(∅|-|—|/|无|未提及|未指明|未填|空)$"

# body_site 专有：**标本名不是部位名**。切缘／血清／外周血／涂片描述的是「从哪取的、
# 取的什么东西」，不是「哪一处的影像所见」。归「其他」等于宣称「这是一处未命名的部位」，
# 是造错；正确做法是置空（site_not_applicable）并把原词存 body_site_text。
SPECIMEN_RE = (r"切缘|基底|血清|外周血|全血|血浆|血液|^血$|骨髓|标本|蜡块|组织块|"
               r"穿刺组织|小组织|细胞学|涂片|切片|石蜡|冰冻|离心|上清|"
               r"分泌物|胸水|腹水|尿液|粪便|拭子|积液")

# 侧别混进部位（`左腋`／`右前哨`／`左`）：先剥侧别，剩下的才是部位。
# 纯侧别值（`左`）剥完为空 —— 部位真的没写，留空而不是拿「其他」顶上。
LAT_PREFIX_RE = re.compile(r"^(双侧|左侧|右侧|左|右|双)")

# 明显不属于影像检查、应回路由到别的板块的值（不是枚举清洗问题，是错位）
# 检验类：v7 有独立 `labs[]`，出现在 reports[].report_type 里就是字段错位，
# 必须路由回去，**不能塞「其他」**（塞了就永久分不清「真是别的检查」与「检验写错端口」）。
LEAK = {
    "report_type": r"实验室|laborator|^lab|lab_|\blab\b|检验|化验|血常规|blood|生化|"
                   r"肝功能|肾功能|肾功|电解质|血脂|心肌|标志物|肿瘤标志物|激素|甲功|"
                   r"甲状腺功能|CA1|CA2|CEA|糖基|感染|免疫学|微生物|细菌|培养|分泌物|"
                   r"尿常规|凝血|血象|血气|维生素|糖化血红蛋白|marker|血糖|尿液|肾小球|肌酐|"
                   r"biochem|血液学|电解质|血气分析",
}

def _strip_lat(v):
    m = LAT_PREFIX_RE.match(v)
    if not m:
        return None, v
    side = {"双侧": "双", "双": "双", "左侧": "左", "左": "左", "右侧": "右", "右": "右"}[m.group(1)]
    return side, v[m.end():].strip(" 　,，、:-—")

def rule_map(field, val):
    v = (val or "").strip()
    if v in CLOSED[field]:          # 闭集内先认，否则 laterality 的「未指明」会被当空值
        return v, "exact", None
    if not v or re.search(EMPTY_RE, v):
        return None, "empty", None
    if LEAK.get(field) and re.search(LEAK[field], v, re.I):
        return None, "leak", None      # 错位内容：留给路由，不做枚举归一

    side = None
    if field == "body_site":
        # ① 标本名先判：`切缘`「血清」这类带部位字的（`基底切缘`）不能被后面的规则抢走
        if re.search(SPECIMEN_RE, v):
            s0, rest0 = _strip_lat(v)
            core0 = rest0 if s0 is not None else v
            # 标本里**仍写得出器官**的保留器官（`乳腺切缘`／`左乳切缘` 是乳腺的切缘，
            # 丢了「乳腺」就只剩一个没有归属的「切缘」）—— 与用户「尽可能保留信息」
            # 的总原则一致；器官写不出来的（切缘／血清／外周血）才留空。
            for target, pat in RULES["body_site"]:
                if re.search(pat, core0, re.I):
                    return target, "specimen_organ", s0
            return None, "specimen", s0
        # ② 剥侧别：`左腋` → 腋；`左` → 空
        side, rest = _strip_lat(v)
        if side is not None:
            if not rest:
                return None, "lat_only", side
            v = rest

    for target, pat in RULES[field]:
        if re.search(pat, v, re.I):
            # 「其他」这条规则是**泛称**（影像／检查／报告／描述），与下面的
            # `other_unmatched`（规则一个都没认出来）分开记：两者的处置完全不同，
            # 前者是正常收口，后者是长尾待审。
            if target == "其他" and field == "report_type":
                return target, "other_generic", side
            return target, "rule", side

    # body_site 兜底：写了具体器官/部位但不在闭集（胸部／腹部／主动脉／门静脉／宫颈…）
    if field == "body_site":
        return "其他", "other_organ", side
    # report_type 兜底：闭集里本来就有「其他」，认不出来的归它（否则长尾永远挂着待判）；
    # 但单独记 `other_unmatched`，长尾清单仍可逐条审，不被泛称淹没。
    if field == "report_type":
        return "其他", "other_unmatched", side
    return None, "todo", side

def do_backfill():
    p = os.path.join(NORM, "terms_rad_enum.jsonl")
    if not os.path.exists(p):
        raise SystemExit("缺 %s（先跑 collect_terms.py）" % p)

    fill_targets = {t for t in CLOSED["report_type"] if t != "其他"}
    fill, stats, conflict, unfill = {}, collections.Counter(), [], collections.Counter()
    hit, miss_exam, miss_mode = collections.Counter(), collections.Counter(), collections.Counter()
    before, after = collections.Counter(), collections.Counter()
    total = 0

    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            name = (r.get("literal") or "").strip()
            levels = r.get("levels") or {}
            if not levels:
                continue
            cand, cand_how, _ = rule_map("report_type", name)
            cand_ok = bool(cand) and cand in fill_targets

            known, rows = collections.Counter(), []
            for combo, n in levels.items():
                rt = (combo or "").split("|")[0]
                t, how, _ = rule_map("report_type", rt or "∅")
                total += n
                before[t or "（留空）"] += n
                if t in fill_targets:
                    kind = None
                elif t is None and how == "empty":
                    kind = "empty"
                elif t == "其他":
                    kind = "other"
                elif t is None and how == "leak":
                    kind = "leak"
                else:
                    kind = "unknown"
                rows.append((rt, n, kind))
                if t in fill_targets:
                    known[t] += n

            mode, mode_n = (known.most_common(1)[0] if known else (None, 0))
            known_tot = sum(known.values())
            ratio = (mode_n / known_tot) if known_tot else 0.0

            for rt, n, kind in rows:
                if kind is None:
                    stats["已明确"] += n
                    after[rule_map("report_type", rt or "∅")[0]] += n
                    continue
                if kind == "leak":
                    stats["原值即错位"] += n
                    after["（错位待路由）"] += n
                    continue
                base = "empty" if kind == "empty" else "other"
                stats["待回填_" + base] += n
                if not cand_ok:
                    stats["exam判不出_" + base] += n
                    unfill[(base, cand_how, (rt or "∅")[:24])] += n
                    miss_exam[(name, rt, n)] += 1
                    # 字面判不出、但同项目名多数有明确模态：只记账不回填
                    if mode and known_tot >= 5 and ratio >= 0.9:
                        stats["仅众数可判_" + base] += n
                        miss_mode[(name, mode, n)] += 1
                    after["其他"] += n
                    continue
                if mode and mode != cand and known_tot >= 3 and ratio >= 0.8:
                    stats["冲突_" + base] += n
                    conflict.append({"exam_name": name, "exam_says": cand,
                                     "field_mode": mode, "mode_ratio": round(ratio, 3),
                                     "report_type": rt or "∅", "kind": base, "n": n})
                    after["其他"] += n
                    continue

                key = name + "\x00" + (rt or "∅")
                prev = fill.get(key)
                if prev:
                    prev["n"] += n
                else:
                    fill[key] = {"target": cand, "kind": base, "n": n,
                                 "basis": "exam_literal", "agree": (mode == cand),
                                 "exam_mode": mode,
                                 "exam_mode_ratio": round(ratio, 3)}
                stats["可回填_" + base] += n
                hit[(name, cand)] += n
                after[cand] += n

    out = {
        "generated_by": "05_agents/normalize_enum.py --backfill",
        "field": "report_type",
        "key": "exam_name\\x00report_type原文",
        "closed": CLOSED["report_type"],
        "rules": {
            "basis": "exam_name 字面经 rule_map 判出具体模态",
            "no_fill_if_exam_unknown": True,
            "no_fill_if_exam_leak": True,
            "conflict_guard": "同 exam_name 下多数字段值占比≥0.8 且样本≥3 时与字面冲突则不回填",
            "kinds": {"other": "原文写了「其他」类泛称，回填是翻案",
                      "empty": "原文没写，回填是补值（apply 须标 fill_empty）"},
        },
        "total_rows": total,
        "stats": dict(stats),
        "distribution_before": dict(before.most_common()),
        "distribution_after": dict(after.most_common()),
        "by_exam": {k[0]: {"target": k[1], "n": v} for k, v in hit.most_common()},
        "fill": fill,
        "conflicts": sorted(conflict, key=lambda x: -x["n"]),
        "unfillable": [{"kind": k[0], "why": k[1], "report_type": k[2], "n": v}
                       for k, v in unfill.most_common(60)],
    }
    fp = os.path.join(NORM, "report_backfill.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("报告单元 %d（exam_name 缺失的 %d 条无法回填）"
          % (total, 270356 - total))
    print("%-22s %8s" % ("待回填（其他）", stats["待回填_other"]))
    print("%-22s %8s" % ("  可回填", stats["可回填_other"]))
    print("%-22s %8s" % ("待回填（原文空）", stats["待回填_empty"]))
    print("%-22s %8s" % ("  可回填", stats["可回填_empty"]))
    print("%-22s %8s" % ("exam 判不出", stats["exam判不出_other"] + stats["exam判不出_empty"]))
    print("%-22s %8s" % ("  其中仅众数可判", stats["仅众数可判_other"] + stats["仅众数可判_empty"]))
    print("%-22s %8s" % ("与字段多数冲突", stats["冲突_other"] + stats["冲突_empty"]))
    print("\n回填后 report_type 分布：")
    for k, v in after.most_common():
        print("  %-14s %7d  %6.2f%%" % (k, v, 100.0 * v / total if total else 0))
    print("\n→ 中间产物/_norm/report_backfill.json（%d 条行级规则）" % len(fill))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="按 exam_name 回填 report_type（行级查找表）")
    ap.add_argument("--show", default="", help="打印某字段前 N 个未判项")
    ap.add_argument("--n", type=int, default=30)
    a = ap.parse_args()

    if a.backfill:
        return do_backfill()

    probe_p = os.path.join(NORM, "structure_probe.json")
    if not os.path.exists(probe_p):
        raise SystemExit("缺 %s（先跑 collect_terms.py）" % probe_p)
    probe = json.load(open(probe_p, encoding="utf-8"))

    out = {}
    print("%-14s %8s %10s %9s %9s %7s %6s %6s %7s"
          % ("字段", "变体数", "总出现", "闭集内", "规则可定", "标本空", "留空", "错位", "需 LLM"))
    for field in CLOSED:
        vals = probe.get(field) or {}
        if not vals:
            print("%-14s  （无数据）" % field)
            continue
        mapping, todo, blank = {}, {}, {}
        stat = collections.Counter()
        for v, n in vals.items():
            t, how, side = rule_map(field, v)
            stat[how] += 1
            if t:
                ent = {"target": t, "how": how, "count": n}
                if side:
                    ent["laterality"] = side
                mapping[v] = ent
            elif how in ("specimen", "lat_only"):
                blank[v] = {"reason": how, "count": n}
            elif how == "todo":
                todo[v] = n
        leakv = {v: n for v, n in vals.items() if rule_map(field, v)[1] == "leak"}
        tot = sum(vals.values())
        out[field] = {"closed": CLOSED[field], "mapping": mapping,
                      "todo": todo, "leak": leakv, "blank": blank,
                      "variants": len(vals), "occurrences": tot}
        print("%-14s %8d %10d %9d %9d %7d %6d %6d %7d"
              % (field, len(vals), tot, stat["exact"],
                 stat["rule"] + stat["other_generic"] + stat["other_unmatched"],
                 stat["specimen"] + stat["lat_only"], stat["empty"],
                 stat["leak"], stat["todo"]))
        if blank:
            n_sp = sum(x["count"] for x in blank.values() if x["reason"] == "specimen")
            n_lo = sum(x["count"] for x in blank.values() if x["reason"] == "lat_only")
            print("               └ 留空：标本类 %d 次 / 纯侧别 %d 次（原文进 body_site_text）"
                  % (n_sp, n_lo))
        n_um = sum(m["count"] for m in mapping.values() if m["how"] == "other_unmatched")
        if n_um:
            um = sorted(((v, m["count"]) for v, m in mapping.items()
                         if m["how"] == "other_unmatched"), key=lambda x: -x[1])
            print("               └ 归「其他」但规则未识别：%d 种 / %d 次（长尾待审，例：%s）"
                  % (len(um), n_um, [x[0] for x in um[:4]]))
        if leakv:
            print("               └ 错位（应路由到别的板块，不做枚举归一）：%d 种 / %d 次，例：%s"
                  % (len(leakv), sum(leakv.values()), list(leakv)[:3]))
        if a.show == field:
            print("\n  未判项（按次数降序，前 %d）：" % a.n)
            for v, n in sorted(todo.items(), key=lambda kv: -kv[1])[:a.n]:
                print("    %-38s %d" % (v[:38], n))

    with open(os.path.join(NORM, "enum_plan.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n→ 中间产物/_norm/enum_plan.json")

if __name__ == "__main__":
    main()
