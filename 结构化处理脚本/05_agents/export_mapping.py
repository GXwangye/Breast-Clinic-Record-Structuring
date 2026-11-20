# -*- coding: utf-8 -*-

import argparse
import collections
import csv
import difflib
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"
QA_OUT = DATA + "/中间产物/_qa_out"
OUT_DIR = NORM + "/映射字典"
ERR_DIR = NORM + "/错误记录"

DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "drug"]
DOMAIN_CN = {"icd10_dx": "诊断（ICD-10）", "icd10_comorb": "合并症（ICD-10）",
             "icd9_surgery": "手术（ICD-9）", "loinc_lab": "检验（LOINC）",
             "drug": "药品", "enum:report_type": "检查类型枚举",
             "enum:body_site": "部位枚举", "enum:laterality": "侧别枚举"}
REV = "v1"

# 错误类型 → 后续动作（写死，防"记录了但没人知道要干嘛"）
ACTION = {
    "gate_reject": "重判：进 retry 队列重跑该块",
    "code_rejected": "码置空，canonical 保留；同时进字典缺口账",
    "judge_diff": "组内并错：拆组重判",
    "cross_merge": "该合没合：进第 5 步二次归并",
    "not_processed": "漏跑：补跑该块",
    "enum_leak": "路由修正：检验回 labs[]，心电图单列；不在枚举层硬塞",
    "enum_todo": "交 LLM 归入闭集（枚举专用批）",
    "enum_other": "长尾待审：已归「其他」，规则没认出来；逐条看是否需要补规则或补闭集值",
    "dict_gap": "非错误：字典缺口，供知识库迭代参考",
    "abbr_unverified": "复核：纯拉丁写法跳过了字面锚定与跨类检查，须逐条确认",
    "core_added": "复核：canonical 新增了核心词（多为同义扩展），确认即放过",
    "noise_literal": "无需处理：原文本身就是占位（其他/冒号/无），没有可归的内容",
}

#
# 背景：icd9_surgery 有 29% 的块写法里混着病理结果，整段压进一个术式标签会丢掉
# `前哨淋巴结6枚，均未见癌（0/6）` 这类临床信息。原文不删（literal 列全量保留），
# 这里再把**结果分句**单列一列，让标签之外仍有通道。
#
# 只抽分句、不改字、不解释 —— 捏成结构化字段（淋巴结阳性数/TNM）是 v7 结构化层的职责，
# 归一层硬捏会造出无法回溯的数字。
# 强标记：出现其一才认为这条写法**确实带了结果**。
# 「淋巴结/前哨/结节」不在此列 —— 它们只用来定位分句，否则 `右乳4b结节mmt旋切活检术`
# 「冰冻/病理」也不能裸用：`…+术中冰冻+输液港置入术` 里「术中冰冻」是手术项目名，
# 不是结果，必须带「示/病理」才算。
RESULT_STRONG = re.compile(
    r"(未见癌|见癌|癌累及|癌转移|转移|枚|浸润|脉管|癌栓|切缘|分级|级别|直径|大小|"
    r"\d+\.?\d*\s*(?:cm|mm)|ER|PR|HER2|Ki-?67|免疫组化|"
    r"(?:术后|术中)?病理(?:示|[：:])|冰冻(?:示|结果)|"
    r"阳性|阴性|反应性增生|不典型)")
RESULT_WEAK = re.compile(r"(淋巴结|前哨|结节)")
SPLIT_R = re.compile(r"[，,；;。\n]")

def result_text(lit):
    if not lit or not RESULT_STRONG.search(lit):
        return None
    # 指标名形态（`淋巴结化疗反应阴性数`、`前哨淋巴结转移数`、`转移比例`、`组织学分级`）
    # 是**检验项目名**，不是某次检查的结果值 —— 它们含「淋巴结/转移/阴性/分级」这些词，
    # 会被下面的分句规则整条捞出来（loinc 域 815 条就是这么误判的）。
    bare = lit.replace(" ", "")
    if len(bare) <= 12 and re.fullmatch(r".{0,12}(数|比例|计数|大小|直径|分级|级别)", bare):
        return None
    parts = [p.strip() for p in SPLIT_R.split(lit) if p.strip()]
    hit = []
    for p in parts:
        if not (RESULT_STRONG.search(p) or RESULT_WEAK.search(p)):
            continue
        if re.fullmatch(r"[\d\-./年月日\s]+", p):     # 纯日期段
            continue
        hit.append(p)
    if not hit:
        return None
    return "；".join(hit)[:300]

def _load(p):
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

def _w_csv(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if r.get(h) is None else r.get(h) for h in header])
    os.replace(tmp, path)

# ---------------- 一、装配映射 ----------------

def load_overrides(p):
    ov = {}
    for r in _load(p) if p else []:
        dom = r.get("domain")
        if dom not in DOMAINS:
            continue
        loc = r.get("literal") or r.get("key")
        if not loc:
            continue
        ov[(dom, loc)] = r
    return ov

def build(overrides):
    mapping, errors = [], []
    stat = collections.defaultdict(collections.Counter)

    for dom in DOMAINS:
        fold = _load(os.path.join(NORM, "fold_%s.jsonl" % dom))
        canon = {r["key"]: r for r in _load(os.path.join(NORM, "canon_%s.jsonl" % dom))}
        # 步 5 归并结果（同义词收敛），只含被改写的块
        canon5 = {r["key"]: r for r in
                  _load(os.path.join(NORM, "step5", "canon5_%s.jsonl" % dom))}
        if not fold:
            stat[dom]["skip"] += 1
            continue
        stat[dom]["literals"] = len(fold)
        stat[dom]["blocks"] = len(canon)

        for fr in fold:
            lit, key, n = fr.get("literal"), fr.get("key"), fr.get("count") or 0
            blk = canon.get(key)
            row = {
                "domain": dom, "literal": lit, "block_key": key,
                "canonical": None, "code": None, "kb_name": None,
                "status": "not_processed", "n": n,
                "laterality": fr.get("laterality"),
                "stripped": ",".join(fr.get("stripped") or []),
                "display": None, "n_group": None, "n_variants": None,
                "source": None, "reason": None, "rev": REV,
                "sample_records": ",".join((fr.get("records") or [])[:2]),
                "base_status": None,
                "result_text": result_text(lit), "has_result": 0,
            }
            row["has_result"] = 1 if row["result_text"] else 0

            # ① 取 LLM 判定
            if blk is not None and blk.get("canonical"):
                row.update({
                    "canonical": blk["canonical"], "code": blk.get("code"),
                    "kb_name": blk.get("kb_name"),
                    "status": blk.get("match_status") or "canonical_only",
                    "display": blk.get("display"),
                    "n_group": blk.get("n"), "n_variants": blk.get("n_variants"),
                    "source": "llm", "reason": blk.get("reason"),
                })
            elif blk is not None:
                row["status"] = "rejected"
                row["reason"] = blk.get("gate") or blk.get("reason")
                row["display"] = blk.get("display")
                row["n_group"] = blk.get("n")
            row["base_status"] = row["status"]

            # ①b 步 5 归并（同义词收敛）：只改标签，不动 status，靠 source 区分
            s5 = canon5.get(key)
            if s5 and s5.get("canonical") and row["canonical"] \
                    and s5["canonical"] != row["canonical"]:
                row["reason"] = ("步5归并 %s→%s" % (s5.get("from"), s5["canonical"]))[:40]
                row["canonical"] = s5["canonical"]
                if s5.get("code"):
                    row["code"], row["kb_name"] = s5["code"], s5.get("kb_name")
                row["source"] = "step5_merge"
                stat[dom]["step5"] += 1

            # ② 修正覆盖（按 literal 优先，退而按 block key）
            ov = overrides.get((dom, lit)) or overrides.get((dom, key))
            if ov:
                if ov.get("canonical"):
                    row["canonical"] = ov["canonical"]
                if "code" in ov:
                    row["code"] = ov["code"]
                row["status"] = "overridden"
                row["source"] = "override"
                row["reason"] = (ov.get("note") or "")[:40]
                stat[dom]["override"] += 1

            # ③ 按最终状态记错
            if row["status"] == "not_processed":
                errors.append(_err("not_processed", dom, lit, key, None, None,
                                   "折叠块无 canonical 记录（未跑或批失败）", n))
                stat[dom]["not_processed"] += 1
            elif row["status"] == "rejected" and blk is not None and blk.get("noise"):
                errors.append(_err("noise_literal", dom, lit, key, None, None,
                                   "原文为噪声占位", n))
                stat[dom]["noise_literal"] += 1
            elif row["status"] == "rejected":
                errors.append(_err("gate_reject", dom, lit, key,
                                   blk.get("raw_canonical"), blk.get("raw_code"),
                                   blk.get("gate") or blk.get("reason") or "", n))
                stat[dom]["gate_reject"] += 1
            else:
                # 码被拒：仅当最终仍未拿到码（override 若已补码则不再记）
                if blk is not None and blk.get("code_rejected") and not row["code"]:
                    errors.append(_err("code_rejected", dom, lit, key,
                                       row["canonical"], blk["code_rejected"],
                                       "码在字典中不存在，已置空（canonical 保留）", n))
                    stat[dom]["code_rejected"] += 1
                if not row["code"]:
                    errors.append(_err("dict_gap", dom, lit, key, row["canonical"], None,
                                       "字典无对应码，canonical 由 LLM 造词", n))
                    stat[dom]["dict_gap"] += 1
                # 纯拉丁写法（AST/ALT/WBC/MMT）跳过了锚定与跨类检查，单列复核
                if blk is not None and blk.get("abbr"):
                    errors.append(_err("abbr_unverified", dom, lit, key, row["canonical"],
                                       row["code"],
                                       "纯拉丁写法：字面锚定与跨类检查不适用，靠 LLM 判断", n))
                    stat[dom]["abbr_unverified"] += 1
                # canonical 新增核心词但未作废（多为同义扩展），单列供复核
                if blk is not None and blk.get("core_added"):
                    errors.append(_err("core_added", dom, lit, key, row["canonical"],
                                       row["code"],
                                       "新增核心词：%s" % "、".join(blk["core_added"]), n))
                    stat[dom]["core_added"] += 1
                stat[dom]["mapped"] += 1

            mapping.append(row)

    return mapping, errors, stat

def _err(kind, dom, lit, key, canon, code, detail, n):
    return {"kind": kind, "domain": dom, "literal": lit, "block_key": key,
            "canonical": canon, "code": code, "detail": detail or "",
            "n": n, "action": ACTION.get(kind, "")}

def _quals(s):
    return {w for w in ("左", "右", "双侧", "单侧", "男性", "女性") if w in (s or "")}

def _flat(s):
    return (s or "").replace("乳房", "乳腺").replace(" ", "").replace("　", "")

def _pick_code(canon, codes):
    cq = _quals(canon)
    base = _flat(canon)

    def rank(kv):
        (code, name), n = kv
        q = _quals(name)
        sim = difflib.SequenceMatcher(None, base, _flat(name)).ratio()
        side = 0 if not q else (1 if "单侧" in q else 2)
        return (0 if name == canon else 1,
                0 if q <= cq else 1,
                round(-sim, 4), side, -n, code)
    return sorted(codes.items(), key=rank)[0][0]

def build_canonical(mapping):
    g = collections.defaultdict(lambda: {"n_total": 0, "lits": [], "blocks": set(),
                                         "codes": collections.Counter()})
    for r in mapping:
        c = r.get("canonical")
        if not c:
            continue
        x = g[(r["domain"], c)]
        x["n_total"] += r.get("n") or 0
        x["lits"].append((r["literal"], r.get("n") or 0))
        x["blocks"].add(r["block_key"])
        if r.get("code"):
            x["codes"][(r["code"], r.get("kb_name") or "")] += 1

    rows = []
    for (dom, c), x in g.items():
        codes = x["codes"]
        if not codes:
            status, code, kbname, alt = "absent", None, None, ""
        else:
            code, kbname = _pick_code(c, codes)
            if len(codes) == 1:
                status, alt = "unified", ""
            else:
                status = "picked"
                alt = " | ".join(
                    ("%s（%s）" % (cd, nm)) if nm else cd
                    for (cd, nm), _ in sorted(codes.items(),
                                              key=lambda kv: (-kv[1], kv[0][0]))
                    if (cd, nm) != (code, kbname))
        top = sorted(x["lits"], key=lambda kv: -kv[1])[:5]
        rows.append({
            "domain": dom, "canonical": c,
            "n_total": x["n_total"], "n_literals": len(x["lits"]),
            "n_blocks": len(x["blocks"]),
            "code": code, "kb_name": kbname,
            "code_status": status, "code_n": len(codes), "code_alt": alt,
            "top_literals": " | ".join("%s(%d)" % (a, b) for a, b in top),
            "rev": REV,
        })
    rows.sort(key=lambda r: (r["domain"], -r["n_total"]))
    return rows

# ---------------- 二、质检类错误 ----------------

def collect_enum_errors():
    p = os.path.join(NORM, "enum_plan.json")
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for field, x in d.items():
        dom = "enum:%s" % field
        closed = "/".join(x.get("closed") or [])
        for v, n in (x.get("leak") or {}).items():
            out.append(_err("enum_leak", dom, v, None, None, None,
                            "字段错位：%s 里出现非影像值（属路由问题，不做枚举归一）" % field, n))
        for v, n in (x.get("todo") or {}).items():
            out.append(_err("enum_todo", dom, v, None, None, None,
                            "规则层判不了，待归入闭集 %s" % closed, n))
        # 归「其他」但规则一条都没认出来的长尾：不是错误，但要看一眼是不是闭集缺值
        for v, m in (x.get("mapping") or {}).items():
            if m.get("how") == "other_unmatched":
                out.append(_err("enum_other", dom, v, None, None, None,
                                "无规则命中，已归「其他」；确认是否属先前列举的闭集 %s" % closed,
                                m.get("count")))
    return out

def collect_judge_diff():
    out = []
    for d in _load(os.path.join(QA_OUT, "canon_judge.jsonl")):
        if d.get("judge_same"):
            continue
        out.append({
            "kind": "judge_diff", "domain": d.get("domain"), "literal": d.get("b"),
            "block_key": None, "canonical": d.get("canonical"), "code": None,
            "detail": "复核判异：%s | %s（%s）"
                      % (d.get("a"), d.get("b"), d.get("why") or ""),
            "n": None, "action": ACTION["judge_diff"]})
    return out

def collect_cross_merge(canon_rows, top=3000, th=0.85):
    keyed = sorted(canon_rows, key=lambda r: -r["n_total"])[:top]
    out = []
    for i, a in enumerate(keyed):
        ca = a["canonical"]
        for b in keyed[i + 1:i + 60]:
            cb = b["canonical"]
            if a["domain"] != b["domain"]:
                continue
            if abs(len(ca) - len(cb)) > 4:
                continue
            r = difflib.SequenceMatcher(None, ca, cb).ratio()
            if r >= th:
                out.append({
                    "kind": "cross_merge", "domain": a["domain"], "literal": ca,
                    "block_key": None, "canonical": cb, "code": None,
                    "detail": "相似度 %.2f：%s | %s（%d 次 vs %d 次）"
                              % (r, ca, cb, a["n_total"], b["n_total"]),
                    "n": a["n_total"], "action": ACTION["cross_merge"]})
    out.sort(key=lambda x: -x["n"] if x["n"] else 0)
    return out

# ---------------- 三、落盘 ----------------

def write_sqlite(path, mapping, canons, errors):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    c = con.cursor()
    c.executescript("""
    CREATE TABLE mapping (
      domain TEXT, literal TEXT, block_key TEXT, canonical TEXT,
      code TEXT, kb_name TEXT, status TEXT, base_status TEXT, n INTEGER,
      laterality TEXT, stripped TEXT, display TEXT,
      n_group INTEGER, n_variants INTEGER, source TEXT, reason TEXT,
      sample_records TEXT, rev TEXT,
      result_text TEXT, has_result INTEGER);
    CREATE TABLE canonical (
      domain TEXT, canonical TEXT, n_total INTEGER, n_literals INTEGER,
      n_blocks INTEGER, code TEXT, kb_name TEXT, code_status TEXT,
      top_literals TEXT, rev TEXT, code_n INTEGER, code_alt TEXT);
    CREATE TABLE error (
      kind TEXT, domain TEXT, literal TEXT, block_key TEXT, canonical TEXT,
      code TEXT, detail TEXT, n INTEGER, action TEXT);
    CREATE TABLE meta (k TEXT, v TEXT);
    """)
    c.executemany("INSERT INTO mapping VALUES (%s)" % ",".join("?" * 20),
                  [tuple(r[k] for k in ("domain", "literal", "block_key", "canonical",
                                        "code", "kb_name", "status", "base_status", "n",
                                        "laterality", "stripped", "display", "n_group",
                                        "n_variants", "source", "reason",
                                        "sample_records", "rev",
                                        "result_text", "has_result"))
                   for r in mapping])
    c.executemany("INSERT INTO canonical VALUES (%s)" % ",".join("?" * 12),
                  [tuple(r[k] for k in ("domain", "canonical", "n_total", "n_literals",
                                        "n_blocks", "code", "kb_name", "code_status",
                                        "top_literals", "rev", "code_n", "code_alt"))
                   for r in canons])
    c.executemany("INSERT INTO error VALUES (%s)" % ",".join("?" * 9),
                  [tuple(r[k] for k in ("kind", "domain", "literal", "block_key",
                                        "canonical", "code", "detail", "n", "action"))
                   for r in errors])
    c.executemany("INSERT INTO meta VALUES (?,?)", [
        ("rev", REV),
        ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("n_mapping", str(len(mapping))),
        ("n_canonical", str(len(canons))),
        ("n_error", str(len(errors))),
        ("note", "归一的终点是同质标签收敛；字典码仅为附加信息，code 为空不代表归一失败"),
    ])
    c.executescript("""
    CREATE INDEX ix_map_lit ON mapping(literal);
    CREATE INDEX ix_map_canon ON mapping(domain, canonical);
    CREATE INDEX ix_map_status ON mapping(status);
    CREATE INDEX ix_canon_lbl ON canonical(canonical);
    CREATE INDEX ix_err_kind ON error(kind);
    """)
    con.commit()
    con.close()

def write_readme(path, stat, n_lit, n_can, n_err):
    lines = []
    A = lines.append
    A("# 归一化映射字典 %s\n" % REV)
    A("生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    A("## 这是什么\n")
    A("三张表，用途不同，**不要混用**：\n")
    A("| 表 | 粒度 | 谁用 | 怎么用 |")
    A("|---|---|---|---|")
    A("| `mapping` | 一个写法一行 | 回刷脚本 | `WHERE domain=? AND literal=?` 直接查，O(1) |")
    A("| `canonical` | 一个规范标签一行 | 统计/分析 | 按标签计频，避免同一标签被写法拆散 |")
    A("| `error` | 一条问题一行 | 迭代 | 按 `kind` 筛，每类带 `action` |")
    A("")
    A("**`code` 为空不代表归一失败。** 归一的终点是同质标签收敛成一个 `canonical`；"
      "字典码只是附加信息，字典没有的术语照样归，码留空。\n")
    A("**同标签多码不是错误。** 我们手上有多个字典（ICD-10/ICD-9/LOINC/药品多批次），"
      "同一个中文标签在不同字典里各有一个入口是常态。目的既然只是**统一中文表述**，"
      "码就只锚一个：名字与标签逐字相同的优先，其次取多数块选中的，再按码定序；"
      "其余候选只记个数与码串（`code_n`/`code_alt`），不进错误记录。\n")
    A("**术式要保留，结果也要保留。** 手术域有 29% 的写法里混着病理结果，例如"
      "`前哨淋巴结6枚，均未见癌（0/6）` —— 压成一个术式标签会把这些信息压没。"
      "原文在 `literal` 列全量保留，`result_text` 再把结果分句单列出来。"
      "**它不做解释、不捏成字段**：淋巴结阳性数、TNM、分级属于结构化层的事，"
      "归一层硬捏会造出无法回溯的数字。\n")
    A("## mapping 字段\n")
    A("| 字段 | 含义 |")
    A("|---|---|")
    for k, v in [
        ("domain", "五个域之一：icd10_dx / icd10_comorb / icd9_surgery / loinc_lab / drug"),
        ("literal", "**原写法，逐字，一个字节不改**（这是唯一真相）"),
        ("block_key", "折叠块键（步 2 产物，写法折叠后归一到的键）"),
        ("canonical", "规范标签（LLM 判定）"),
        ("code / kb_name", "字典码与码对应名；可空"),
        ("status", "canonical_with_code / canonical_only / rejected / not_processed / overridden"),
        ("base_status", "覆盖前的原始状态（看 override 救回了什么）"),
        ("n", "该写法出现次数"),
        ("laterality", "折叠时剥离的侧别（左/右/双/未指明），侧别不参与标签"),
        ("stripped", "剥离内容标记（paren=括号元话语，duration=时长）"),
        ("display", "该折叠块内出现最多的写法（数量优势的代表词）"),
        ("n_group / n_variants", "所在折叠块的总出现次数 / 写法数"),
        ("source", "llm（模型判定）/ step5_merge（同义词收敛）/ override（修正清单覆盖）"),
        ("sample_records", "该写法的示例登记号（回溯用）"),
        ("result_text", "写法里夹带的**病理/检查结果分句**，逐字用「；」连接；无结果则为空"),
        ("has_result", "1 = 该写法带结果信息（术式名之外还有结果，需要保留）"),
    ]:
        A("| `%s` | %s |" % (k, v))
    A("")
    A("## canonical 字段\n")
    A("| 字段 | 含义 |")
    A("|---|---|")
    A("| `code_status` | `unified` 一码 ／ `picked` 多码已按规则取一 ／ `absent` 字典无码 |")
    A("| `code_n` | 该标签下出现过的候选码个数（>1 即「多字典各有入口」，正常现象） |")
    A("| `code_alt` | 未被选中的其余候选码（只备查，不参与回刷） |")
    A("")
    A("## error 的 kind 与动作\n")
    A("| kind | 含义 | 动作 | 是否算错误 |")
    A("|---|---|---|---|")
    D = {"gate_reject": ("硬闸门作废（空/未锚定/跨类改写）", "重判", "是"),
         "code_rejected": ("LLM 给的码在字典中不存在", "码置空，canonical 保留", "是"),
         "judge_diff": ("LLM 独立复核判定组内并错", "拆组重判", "是"),
         "cross_merge": ("两个标签高度相似（该合没合）", "进二次归并", "是（待判）"),
         "not_processed": ("折叠块无 canonical 记录", "补跑", "是"),
         "enum_leak": ("枚举字段被填成别的模块的值", "路由修正", "是"),
         "enum_todo": ("枚举变体规则层判不了", "交 LLM 归闭集", "否（待判）"),
         "enum_other": ("枚举值无规则命中，已归「其他」",
                        "逐条看：补规则／补闭集值／确认属其他", "否（长尾待审）"),
         "abbr_unverified": ("纯拉丁写法（AST/ALT/WBC/MMT）：锚定与跨类检查不适用",
                             "逐条复核或写 override", "是（须复核）"),
         "core_added": ("canonical 新增核心词（术/切除/炎/囊肿…），未作废",
                        "复核：多为同义扩展，确认即放过", "是（须复核）"),
         "noise_literal": ("原文本身是占位（其他/冒号/无）", "无需处理", "**否**"),
         "dict_gap": ("字典无对应码", "知识库迭代参考", "**否**")}
    for k, v in ACTION.items():
        A("| `%s` | %s | %s | %s |" % (k, v, D[k][1], D[k][2]))
    A("")
    A("## 三个典型查询\n")
    A("```sql")
    A("-- ① 回刷一条记录用哪个标签、哪个码")
    A("SELECT canonical, code FROM mapping WHERE domain='icd10_dx' AND literal='乳癌术后';")
    A("")
    A("-- ② 某个标签下都收了哪些写法（看归并对不对）")
    A("SELECT literal, n FROM mapping WHERE domain='icd9_surgery'")
    A("  AND canonical='乳腺癌改良根治术' ORDER BY n DESC;")
    A("")
    A("-- ③ 字典缺口清单（哪些概念字典里根本没有，供知识库迭代）")
    A("SELECT canonical, n_total FROM canonical")
    A("  WHERE code_status='absent' ORDER BY n_total DESC LIMIT 50;")
    A("```")
    A("")
    A("## 怎么改进而不用重跑 LLM\n")
    A("写一份 `overrides.jsonl`，每行一条修正：\n")
    A("```json")
    A('{"domain":"icd10_dx","literal":"乳癌术后","canonical":"乳腺癌术后","code":null,"note":"人工确认"}')
    A("```")
    A("然后 `python 结构化处理脚本/05_agents/export_mapping.py --override 中间产物/_norm/overrides.jsonl`。")
    A("映射字典每次从「fold + canon + override」重新装配，**修正不会被重跑冲掉**。\n")
    A("两个细节：\n")
    A("- 按 `block_key` 覆盖 = 整个折叠块都按这条判（同块所有写法一起生效）；按 `literal` 只影响一条写法。")
    A("- 错误记录按**最终状态**生成：`base_status` 是 `not_processed`、被 override 救回的条目，"
      "不会再留在 `error` 表里（否则错误账会越迭代越脏）。")
    A("")
    A("## 本次规模\n")
    A("| 项 | 数 |")
    A("|---|---:|")
    A("| mapping（写法级） | %d |" % n_lit)
    A("| canonical（标签级） | %d |" % n_can)
    A("| error（问题条目） | %d |" % n_err)
    A("")
    A("| 域 | 写法 | 已映射 | 闸门作废 | 未跑 | 无码 | 修正 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for d in DOMAINS:
        s = stat.get(d) or {}
        A("| %s | %d | %d | %d | %d | %d | %d |"
          % (d, s.get("literals", 0), s.get("mapped", 0), s.get("gate_reject", 0),
             s.get("not_processed", 0), s.get("dict_gap", 0), s.get("override", 0)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def write_err_summary(path, errors, stat):
    groups = collections.OrderedDict(
        (k, []) for k in ("gate_reject", "judge_diff",
                          "code_rejected", "not_processed", "enum_leak", "enum_todo",
                          "abbr_unverified", "core_added", "cross_merge", "dict_gap",
                          "noise_literal"))
    for e in errors:
        groups.setdefault(e["kind"], []).append(e)

    L = []
    A = L.append
    A("# 归一化错误记录汇总 %s\n" % REV)
    A("生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    A("> 这份给**人**看，用来决定下一轮改什么；机器筛请查 `error` 表或 `error_%s.csv`。\n" % REV)
    A("> 注意：`dict_gap` **不是错误**，它记的是「字典里没有这个概念」，是知识库迭代的输入。\n")
    A("## 总览\n")
    A("| kind | 条数 | 涉及出现次数 | 动作 |")
    A("|---|---:|---:|---|")
    for k, v in groups.items():
        if not v:
            continue
        occ = sum(x.get("n") or 0 for x in v)
        A("| `%s` | %d | %d | %s |" % (k, len(v), occ, ACTION.get(k, "")))
    A("")
    A("## 各域状况\n")
    A("| 域 | 写法 | 已映射 | 闸门作废 | 未跑 | 无码 | 修正 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for d in DOMAINS:
        s = stat.get(d) or {}
        if not s.get("literals"):
            continue
        A("| %s | %d | %d | %d | %d | %d | %d |"
          % (DOMAIN_CN.get(d, d), s.get("literals", 0), s.get("mapped", 0),
             s.get("gate_reject", 0), s.get("not_processed", 0),
             s.get("dict_gap", 0), s.get("override", 0)))
    A("")
    A("> 枚举层（`report_type` / `body_site` / `laterality`）不在上表：它走契约闭集、"
      "不参与术语归一。它的问题见下方 `enum_leak` / `enum_todo` 两节。\n")

    TITLE = {"gate_reject": "硬闸门作废（需重判）",
             "judge_diff": "组内并错（需拆组重判）",
             "code_rejected": "码不存在（已置空，canonical 可用）",
             "not_processed": "漏跑（需补跑）",
             "enum_leak": "枚举层字段错位（路由问题）",
             "enum_todo": "枚举变体待判（非错误）",
             "abbr_unverified": "纯拉丁写法（跳过字面锚定，须复核）",
             "core_added": "canonical 新增核心词（多为同义扩展，须复核）",
             "noise_literal": "原文即占位（非错误）",
             "cross_merge": "该合没合（进二次归并）",
             "dict_gap": "字典缺口（非错误，供知识库迭代）"}
    for k, v in groups.items():
        if not v:
            continue
        A("## %s — `%s`\n" % (TITLE.get(k, k), k))
        A("共 **%d** 条，涉及 %d 次出现。动作：%s\n"
          % (len(v), sum(x.get("n") or 0 for x in v), ACTION.get(k, "")))
        rows = sorted(v, key=lambda x: -(x.get("n") or 0))[:15]
        A("| 域 | 写法 | 标签 | 次数 | 说明 |")
        A("|---|---|---|---:|---|")
        for x in rows:
            A("| %s | %s | %s | %s | %s |"
              % (DOMAIN_CN.get(x.get("domain"), x.get("domain") or ""),
                 (x.get("literal") or "-")[:30],
                 (x.get("canonical") or "-")[:24],
                 x.get("n") if x.get("n") is not None else "-",
                 (x.get("detail") or "").replace("|", "/")[:60]))
        if len(v) > 15:
            A("\n> 其余 %d 条见 `error_%s.csv`。" % (len(v) - 15, REV))
        A("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

def main():
    global REV, ERR_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--rev", default=REV)
    ap.add_argument("--override", default="", help="修正清单 jsonl（可选）")
    ap.add_argument("--cross-top", type=int, default=3000, help="跨组合并扫描的标签数上限")
    ap.add_argument("--sql", default="", help="建库后执行一条查询并打印")
    a = ap.parse_args()

    REV = a.rev
    # 错误记录跟 --out 走（默认与「映射字典」平级），否则换目录跑仍会写回正式目录
    ERR_DIR = os.path.join(os.path.dirname(os.path.abspath(a.out)), "错误记录")
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(ERR_DIR, exist_ok=True)

    mapping, errors, stat = build(load_overrides(a.override) if a.override else {})
    if not mapping:
        raise SystemExit("没有任何 fold_*.jsonl，先跑 fold_terms.py")

    canons = build_canonical(mapping)
    errors += collect_judge_diff()
    errors += collect_enum_errors()
    errors += collect_cross_merge(canons, top=a.cross_top)
    errors.sort(key=lambda r: (r["kind"], r["domain"] or "", -(r["n"] or 0)))

    db = os.path.join(a.out, "mapping_%s.sqlite" % REV)
    write_sqlite(db, mapping, canons, errors)

    _w_csv(os.path.join(a.out, "mapping_literal.csv"),
           ["domain", "literal", "canonical", "code", "kb_name", "status", "base_status",
            "n", "laterality", "display", "n_group", "n_variants", "source", "reason",
            "block_key", "stripped", "sample_records", "rev",
            "result_text", "has_result"], mapping)
    _w_csv(os.path.join(a.out, "mapping_canonical.csv"),
           ["domain", "canonical", "n_total", "n_literals", "n_blocks",
            "code", "kb_name", "code_status", "code_n", "code_alt",
            "top_literals", "rev"], canons)
    _w_csv(os.path.join(a.out, "error_%s.csv" % REV),
           ["kind", "domain", "literal", "canonical", "code", "detail", "n",
            "action", "block_key"], errors)

    ep = os.path.join(ERR_DIR, "error_%s.jsonl" % REV)
    with open(ep, "w", encoding="utf-8") as f:
        for r in errors:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    write_readme(os.path.join(a.out, "映射字典说明.md"), stat,
                 len(mapping), len(canons), len(errors))
    err_md = os.path.join(ERR_DIR, "错误汇总_%s.md" % REV)
    write_err_summary(err_md, errors, stat)

    # ---- 控制台摘要 ----
    print("=" * 66)
    print("%-13s %8s %8s %8s %8s %8s" % ("域", "写法", "已映射", "闸门作废", "未跑", "无码"))
    for d in DOMAINS:
        s = stat.get(d) or {}
        if not s.get("literals"):
            print("%-13s %8s  （无数据）" % (d, "-"))
            continue
        print("%-13s %8d %8d %8d %8d %8d"
              % (d, s.get("literals", 0), s.get("mapped", 0), s.get("gate_reject", 0),
                 s.get("not_processed", 0), s.get("dict_gap", 0)))
    tot_lit = sum((stat.get(d) or {}).get("literals", 0) for d in DOMAINS)
    tot_map = sum((stat.get(d) or {}).get("mapped", 0) for d in DOMAINS)
    print("=" * 66)
    print("写法 %d → 标签 %d（收敛 %.1f%%）  错误记录 %d 条"
          % (tot_lit, len(canons), 100.0 * (1 - len(canons) / max(1, tot_lit)), len(errors)))
    kinds = collections.Counter(r["kind"] for r in errors)
    for k, v in kinds.most_common():
        print("  %-20s %6d   → %s" % (k, v, ACTION.get(k, "")))
    print("\n产物：")
    for p in (db, os.path.join(a.out, "mapping_literal.csv"),
              os.path.join(a.out, "mapping_canonical.csv"),
              os.path.join(a.out, "error_%s.csv" % REV), ep, err_md,
              os.path.join(a.out, "映射字典说明.md")):
        print("  %8.1f KB  %s" % (os.path.getsize(p) / 1024.0,
                                  os.path.relpath(p, ROOT)))

    if a.sql:
        con = sqlite3.connect(db)
        cur = con.execute(a.sql)
        cols = [d[0] for d in cur.description]
        print("\n" + " | ".join(cols))
        for r in cur.fetchall()[:40]:
            print(" | ".join("" if v is None else str(v) for v in r))
        con.close()

if __name__ == "__main__":
    main()
