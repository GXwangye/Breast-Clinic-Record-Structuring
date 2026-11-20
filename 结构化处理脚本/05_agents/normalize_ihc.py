# -*- coding: utf-8 -*-

import argparse
import glob
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

# 默认写死主工作区（权威脚本铁律）；测试工作区用 BC_CLINIC_ROOT 环境变量覆盖以实现多模型隔离。
ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
ASSEMBLED = DATA + "/中间产物/assembled_v7"
OUTDIR = DATA + "/中间产物/_ihc_normalize"
BACKUP = OUTDIR + "/ihc_backup.jsonl"
DROPPED = OUTDIR + "/ihc_dropped_fields.jsonl"
REPORT = OUTDIR + "/normalize_ihc_report.json"
DONE = OUTDIR + "/ihc_done.txt"          # 断点续跑：已处理 rid 清单（append+flush）

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
from ihc_standard import parse_ihc, marker_kind          # noqa: E402
import ihc_alias_tables as TAB                            # noqa: E402

#   special_stains[]  特殊染色（PAS / Ag 嗜银 / 抗酸 / 阿辛蓝 / Masson），原值保留，不进 IHC 统计
#   fish_metrics[]    HER2/CEP17 比值、CEP17 均值、拷贝数等原位杂交度量，不是独立标志物
#   unresolved[]      字段错位内容与无法归属的字面量，原样保留，供下一轮补表
ROUTE_KEYS = {
    "special": "special_stains",       # SPECIAL 集（Masson/PAS/铁染/Ag…）
    "stain": "special_stains",
    "fish": "fish_metrics",
    "non_marker": "unresolved", "unmapped": "unresolved",
}
# 注意：kind=ish（HER2(FISH)）不在此表 —— 原位杂交是正式检测项，留在 markers[] 里。

def _already_routed(m):
    return set(m.keys()) <= {"name", "raw_value"} and "name" in m

# ---------------------------------------------------------------------------
# 一、标志物名归一表（逐条显式，作为论文可开源的映射表）
# ---------------------------------------------------------------------------
# 标准名（治疗决策型 4 + 诊断鉴别型/其他 37）
CANON = {
    # 治疗决策型
    "ER", "PR", "HER2", "Ki-67",
    # 诊断鉴别型及其他
    "AR", "BCL-2", "CD3", "CD10", "CD20", "CD21", "CD31", "CD34", "CD56", "CDX-2",
    "CK", "CK7", "CK19", "CK20", "CK5/6", "CK8/18", "Calponin", "CgA", "COL4",
    "D2-40", "E-cadherin", "EGFR", "EMA", "F8", "GATA-3", "GCDFP15", "GFAP",
    "MHC", "Napsin A", "P16", "P40", "P53", "P63", "P120", "PAX-8", "PD-L1",
    "S-100", "SMA", "SMMHC", "Syn", "TTF-1", "VEGF", "Vimentin", "Villin",
    "TRDS1", "ACTA2", "CNN1", "CEA",
}

# 同义 / 变体 → 标准名（键一律大写、去 */＊/#、全角转半角后比对）
ALIAS = {
    # ER
    "雌激素受体": "ER", "ER*": "ER",
    # PR
    "孕激素受体": "PR", "PR*": "PR",
    # HER2
    "HER-2": "HER2", "HER2*": "HER2", "HER-2*": "HER2", "C-ERBB-2": "HER2",
    "CERBB2": "HER2", "人类表皮生长因子受体2": "HER2", "人表皮生长因子受体2": "HER2",
    "HER2（人类表皮生长因子受体2）": "HER2", "HER2（IHC）": "HER2",
    # HER2 FISH（独立检测，不与 IHC 混）
    "HER-2(FISH)": "HER2(FISH)", "HER-2_FISH": "HER2(FISH)",
    "HER-2基因（FISH）": "HER2(FISH)", "HER2 FISH": "HER2(FISH)",
    "HER2_FISH": "HER2(FISH)", "HER2基因扩增": "HER2(FISH)",
    "HER2基因扩增状态（FISH）": "HER2(FISH)", "HER2（FISH）": "HER2(FISH)",
    "HER2基因": "HER2(FISH)",
    # Ki-67
    "KI67": "Ki-67", "KI-67": "Ki-67", "KI-67*": "Ki-67", "KI-67#": "Ki-67",
    "KI-67（增殖指数）": "Ki-67", "MKI67": "Ki-67",
    "增殖指数": "Ki-67", "细胞增殖指数": "Ki-67",
    "KI-67增殖指数": "Ki-67", "KI67增殖指数": "Ki-67", "KI-67指数": "Ki-67",
    "增殖活性": "Ki-67", "细胞增殖活性": "Ki-67",
    # CK5/6
    "CK56": "CK5/6", "CK5/6（细胞角蛋白5/6）": "CK5/6", "细胞角蛋白5/6": "CK5/6",
    # CK
    "CK（细胞角蛋白）": "CK", "广谱细胞角蛋白": "CK",
    "CK7（细胞角蛋白7）": "CK7",
    # E-cadherin
    "E-CA": "E-cadherin", "E-CAD": "E-cadherin", "E-CADHERIN": "E-cadherin",
    "ECA": "E-cadherin", "ECAD": "E-cadherin",
    "E-CADHERIN（E-CA）": "E-cadherin", "E-CA（E钙黏蛋白）": "E-cadherin",
    "E-钙黏蛋白": "E-cadherin", "CDH1": "E-cadherin",
    # Calponin
    "CALP": "Calponin", "CALPONIN": "Calponin", "CALPONIN（平滑肌蛋白）": "Calponin",
    "钙调蛋白": "Calponin", "CNN1": "Calponin",
    # D2-40
    "D240": "D2-40", "D2-40（淋巴管内皮标记物）": "D2-40",
    # GATA-3
    "GATA3": "GATA-3", "GATA3（GATA结合蛋白3）": "GATA-3",
    # P120
    "P120膜": "P120", "P120（连环蛋白p120）": "P120", "CTNND1": "P120",
    # P53 / P63
    "TP53": "P53", "P53（肿瘤抑制因子p53）": "P53",
    "P63（肿瘤抑制因子p63）": "P63",
    # Syn
    "SYNAPTOPHYSIN": "Syn", "SYN（突触素）": "Syn",
    # Vimentin / Villin
    "VIM": "Vimentin", "VIMENTIN（波形蛋白）": "Vimentin",
    "VILLIN": "Villin",
    # EMA / EGFR
    "EMA（上皮膜抗原）": "EMA", "EGFR（表皮生长因子受体）": "EGFR",
    # 其他
    "CD34（血管内皮标记物）": "CD34", "F8因子": "F8", "Ⅳ胶原": "COL4",
    "ACTA2": "SMA", "广谱CK": "CK", "CK-P": "CK", "CK高": "CK",
    "GCDFP-15": "GCDFP15", "NAPSINA": "Napsin A", "NAPSIN A": "Napsin A",
}

# 特殊染色（在"免疫组化"段里出现，但不是 IHC 标志物，单列不删）
SPECIAL = {
    "Masson", "PAS", "PAS特殊染色", "D-PAS", "Dpas", "铁染", "铜染", "银染",
    "弹力纤维染色", "Ag",
}
SPECIAL_CANON = {"D-PAS": "D-PAS", "PAS特殊染色": "PAS", "Dpas": "D-PAS"}

# 非标志物（字段错位：报告其他内容被塞进 marker）
NON_MARKER = {
    "分子分型", "切缘", "缝钉切缘", "淋巴结总数", "淋巴结转移(0/5)", "病理分级",
    "神经侵犯", "神经束侵犯", "肌上皮状态", "脉管断端", "脉管癌栓", "转移情况",
    "肿瘤侵犯肺脏层胸膜", "支气管残端",
}

ZERO_WIDTH = str.maketrans("", "", " \t\r\n")
_CJK_GLOSS = re.compile(r"[（(][^（()）]*[)）]\s*$")
_ISH_RE = re.compile(r"(FISH|ISH|基因扩增|原位杂交|荧光原位杂交)")
_SCORE_RE = re.compile(r"(?<![0-9])([0123])\s*[+＋]")
_SCORE_REV_RE = re.compile(r"[+＋]\s*([0123])(?![0-9])")
# 连写标志物拆分：病理报告原文常把两个标志物连写（如「CD34及D2-40」），
# 联合染色判断同一结果（如「无脉管内癌栓」）。必须拆成两个独立 marker 各自归一，
# 否则 canon_name 无法识别、两个标志物在统计中丢失。连接词仅限「及/和/、」（顿号/逗号
# 可能出现在单个标志物名内，如「CK5/6」斜杠不拆、GCDFP15 无分隔），避免误拆。
_JOIN_SPLIT_RE = re.compile(r"\s*(?:及|和|、)\s*")

def canon_name(raw):
    s = str(raw or "").translate(ZERO_WIDTH).strip()
    if not s:
        return "", "empty"
    s = s.replace("＋", "+").replace("＊", "*").replace("#", "").strip()
    if s in NON_MARKER:
        return s, "non_marker"
    if s in SPECIAL:
        _c, _b = TAB.classify(s)         # 统一染色标准名：Ag → Ag嗜银染色、PAS → PAS染色
        return (_c if _b == "stain" else SPECIAL_CANON.get(s, s)), "special"
    up = s.upper()
    for cand in (s, up):
        if cand in CANON:
            return cand, "marker"
        if cand in ALIAS:
            tgt = ALIAS[cand]
            return tgt, ("ish" if "FISH" in tgt else "marker")
    # ISH 判定必须在「去中文括注」之前：否则 HER2(FISH) 会被剥成 HER2，检测方法丢失
    if _ISH_RE.search(up):
        return "HER2(FISH)", "ish"
    # 去尾部中文括注再试
    base = _CJK_GLOSS.sub("", s).strip()
    if base and base != s:
        bup = base.upper()
        for cand in (base, bup):
            if cand in CANON:
                return cand, "marker"
            if cand in ALIAS:
                tgt = ALIAS[cand]
                return tgt, ("ish" if "FISH" in tgt else "marker")
    if _ISH_RE.search(s):
        return "HER2(FISH)", "ish"
    _canon, _bucket = TAB.classify(s)
    return _canon, _bucket

# ---------------------------------------------------------------------------
# 二、字段族（按优先级，先命中先用）
# ---------------------------------------------------------------------------
NAME_F = ["marker_name", "name", "marker", "code", "marker_code", "marker_alt",
          "marker_id", "display_name", "marker_display"]
RAW_F = ["raw_value", "value", "value_raw", "result_raw", "original_value",
         "original", "value_text", "result", "result_value", "result_display",
         "expression", "raw", "value_detail", "interpretation"]
CALL_F = ["status", "call", "call_orig", "flag", "ihc_call", "final_call",
          "stain_call", "interpretation", "result"]
PCT_F = ["percent", "percentage", "positive_rate", "proportion", "value_pct",
         "staining_percent", "positive_pct", "value_numeric", "numeric_value",
         "ki67_percent", "proliferation_index"]
INT_F = ["intensity"]

# 要丢弃并留痕的元信息键
DROP_F = ["basis", "call_basis", "call_orig", "call_category", "call_type",
          "call_confidence", "standard", "note", "notes", "note_", "marker_note",
          "comment", "impression", "interpretation_basis", "qualifier", "modifier",
          "marker_type", "marker_category", "category", "role", "panel_type",
          "panel_role", "purpose", "decision_type", "decision_role",
          "diagnostic_role", "therapeutic_role", "call_source", "source",
          "source_field", "source_offset", "source_wax_block", "source_text",
          "therapeutic", "treatment_relevant", "is_therapeutic", "diagnostic",
          "is_decision_marker", "is_treatment_decision", "decision_marker",
          "treatment_call", "therapeutic_call", "stain_pattern", "localization",
          "distribution", "unit", "units", "unit_orig", "result_unit", "value_unit",
          "subtype", "er_pr_her2_call", "her2_ihc_score", "ihc_score", "score",
          "fish_result", "fish_confirmed", "fish_recommended", "fish_note",
          "fished", "ultralow_expression", "internal_control", "laterality",
          "panel_id", "panel_seq", "marker_seq", "anchor", "anchor_quote",
          "qc", "qc_confidence", "qc_low_conf", "low_conf", "confidence"]

# 已经是标准键、收敛时直接保留语义（不重复计 drop）
STD_KEYS = {"marker_name", "group", "status", "percent", "score", "intensity", "raw_value"}

# 治疗决策型 / 诊断鉴别型分组（契约 3.5.2）：按标准名查表派生，**不做 LLM 判断**。
# 原 section_spec.py 把 group 放在抽取侧 enum 逼 LLM 判断，模型把 Ki-67 俗称「增殖」
TREAT_MARKERS = {"ER", "PR", "HER2", "HER2(FISH)", "Ki-67"}

def split_joined(name):
    s = str(name or "").translate(ZERO_WIDTH).strip()
    if not s:
        return [name]
    parts = [p.strip() for p in _JOIN_SPLIT_RE.split(s) if p.strip()]
    if len(parts) <= 1:
        return [name]
    # 每个子名须能归一成 marker（排除特殊染色/非标志物/空），否则不拆
    for p in parts:
        _std, _kind = canon_name(p)
        if _kind != "marker" and _kind != "ish":
            return [name]
    return parts

def derive_group(std):
    if not std:
        return ""
    if std in TREAT_MARKERS or std.startswith("HER2"):
        return "治疗决策"
    if std in CANON:
        return "诊断鉴别"
    return ""

# marker 对象列表型容器（panel 内 / ihc 顶层的键名）
MARKER_LIST_KEYS = ["markers", "diagnostic_markers", "therapeutic_markers",
                    "treatment_call", "decision_markers", "markers_raw",
                    "markers_raw_ooc", "markers_candidate", "markers_index",
                    "markers_summary", "excluded_markers", "special_stains"]
VALUE_DICT_KEYS = ["calls", "call", "call_summary", "summary", "summary_call",
                   "treatment_call", "treatment_calls", "treatment_decision_markers",
                   "diagnostic_markers", "treatment_markers", "markers_call",
                   "markers_diagnostic", "er", "pr", "her2", "ki67"]

def _first(m, keys):
    for k in keys:
        v = m.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip() not in ("", "None", "null"):
            return str(v).strip()
    return ""

def _num(s):
    if s is None:
        return None
    t = str(s).replace("约", "").replace("＞", ">").replace("%", "").strip()
    m = re.search(r"(\d{1,3}(?:\.\d+)?)", t)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if 0 <= v <= 100 else None

def _score(raw):
    if not raw:
        return None
    for tok in str(raw).split("，"):
        tok = tok.strip()
        if not tok:
            continue
        sc = _SCORE_RE.findall(tok) or _SCORE_REV_RE.findall(tok)
        if sc:
            return sc[0] + "+"
        if re.fullmatch(r"0", tok):
            return "0"
    return None

_STATUS_MAP = {"阳": "阳性", "阴性": "阴性", "阴": "阴性", "不确定": "不确定",
               "缺失": "未提及", "未解析": "未提及", "高表达": "未提及",
               "低表达": "未提及", "未提及": "未提及"}

def _canon_call(v, kind_marker):
    t = str(v or "").strip()
    if t in ("阳性", "阴", "阳", "阴性", "不确定", "不适用", "未提及"):
        return {"阴": "阴性", "阳": "阳性"}.get(t, t)
    r = parse_ihc(t, kind_marker)
    return _STATUS_MAP.get(r.get("status"), "未提及") if r else "未提及"

def _fmt_pct(p):
    return None if p is None else ("%g%%" % p)

def conv_marker(m, loc, rid):
    name_lit = _first(m, NAME_F)
    std, kind = canon_name(name_lit)
    raw = _first(m, RAW_F)
    pct_f = _first(m, PCT_F)
    int_f = _first(m, INT_F)
    # HER2(FISH) 是独立检测名，但判读规则仍走 HER2（FISH 结论覆盖 IHC 评分）
    kind_marker = "HER2" if std.startswith("HER2(") else std

    # 第一步：只在原文值上判读，取出 percent / intensity 的初值
    res0 = parse_ihc(raw, kind_marker) if raw else None
    percent = _num(pct_f)
    if percent is None and res0 and res0.get("value") is not None:
        percent = _num(res0.get("value"))
    inten = int_f if int_f in ("强", "中", "弱") else ""
    if not inten and res0:
        inten = res0.get("intensity") or ""
    if inten not in ("强", "中", "弱"):
        inten = ""

    # 第二步：用**已持久化的三个量**重组判读串再判 status。
    # 这三个量下一轮会原样读回（raw_value / percent / intensity），拼出同样的串 → 幂等。
    comp = "，".join(x for x in (raw, _fmt_pct(percent), inten) if x)
    res = parse_ihc(comp, kind_marker) if comp else None

    status = "未提及"
    if res:
        status = _STATUS_MAP.get(res.get("status"), "未提及")
    if std == "Ki-67":                       # 铁律：Ki67 不压阴阳，只留数值
        status = "不适用" if (percent is not None or raw) else "未提及"
    # 原文重判失败（如 raw 只记了强度「中」）→ 退回判读族取值。
    # 必须在 comp 非空时也执行，否则二次跑会把已判定的 status 抹成未提及（破坏幂等）。
    if status == "未提及" and std != "Ki-67":
        vote = {_canon_call(_first(m, [k]), kind_marker) for k in CALL_F if _first(m, [k])}
        vote.discard("未提及")
        if vote == {"阳性"}:
            status = "阳性"
        elif vote == {"阴性"}:
            status = "阴性"
        elif vote:
            status = "不确定"
    new = {
        "marker_name": std,
        "group": derive_group(std),
        "status": status,
        "percent": percent,
        # score 只从原文值取（不从拼接串取）：拼接串里的 percent/intensity 不是分级来源
        "score": _score(raw) if std == "HER2" else None,
        "intensity": inten or None,
        "raw_value": raw or None,
    }
    drops = []
    for k, v in m.items():
        if k in STD_KEYS:
            continue
        drops.append({"rid": rid, "at": loc, "key": k,
                      "val": v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)[:200]})
    return new, drops, kind

def conv_marker_multi(m, loc, rid):
    name_lit = _first(m, NAME_F)
    parts = split_joined(name_lit)
    if len(parts) <= 1:
        new, drops, kind = conv_marker(m, loc, rid)
        return [new], drops, kind
    out = []
    drops = []
    for sub in parts:
        sub_m = dict(m)
        sub_m["marker_name"] = sub
        new, d, kind = conv_marker(sub_m, loc, rid)
        out.append(new)
        drops.extend(d)
    return out, drops, "marker"

_NAME_HINT = ("marker_name", "marker_code")
# 路由产出容器：不进 discovery，否则下一轮会把它们当成标志物容器再次处理（非幂等）
_ROUTE_BUCKETS = ("special_stains", "fish_metrics", "unresolved")

def _iter_marker_containers(node, prefix):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _ROUTE_BUCKETS:
                continue
            path = "%s.%s" % (prefix, k) if prefix else k
            if isinstance(v, list) and v:
                dicts = [x for x in v if isinstance(x, dict)]
                if dicts and len(dicts) * 2 >= len(v):
                    has_name = sum(1 for x in dicts if any(h in x for h in _NAME_HINT))
                    kl = k.lower()
                    if has_name * 2 >= len(dicts) or (
                            ("marker" in kl or "stain" in kl)
                            and sum(1 for x in dicts if "name" in x) * 2 >= len(dicts)):
                        yield path, v, node
                        continue          # 已认作容器，不再深入
                for i, x in enumerate(v):
                    yield from _iter_marker_containers(x, "%s[%d]" % (path, i))
            elif isinstance(v, (dict, list)):
                yield from _iter_marker_containers(v, path)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            yield from _iter_marker_containers(x, "%s[%d]" % (prefix, i))

def _iter_marker_lists(ihc):
    yield from _iter_marker_containers(ihc, "")

def _iter_report_marker_lists(aux):
    for k in ("reports", "report_groups"):
        v = aux.get(k)
        if isinstance(v, (list, dict)):
            yield from _iter_marker_containers(v, k)

def process(rec, rid):
    ihc = (rec.get("auxiliary_exam") or {}).get("ihc")
    aux = rec.get("auxiliary_exam") or {}
    st = {"markers": 0, "changed": 0, "unmapped": 0,
          "keys_before": Counter(), "kinds": Counter(), "report_markers": 0}
    drops = []
    changed = False
    routed = {}          # id(owner) -> (owner, {bucket: [item, ...]})

    def _consume(entries):
        nonlocal changed
        for loc, arr, owner in entries:
            kept = []
            for i, m in enumerate(arr):
                if not isinstance(m, dict) or not m:
                    continue
                # 上一轮已路由过的 {name, raw_value} → 原样保留（幂等）
                if _already_routed(m) and loc.endswith(".special_stains"):
                    kept.append(m)
                    st["markers"] += 1
                    continue
                loc_i = "%s[%d]" % (loc, i)
                news, d, kind = conv_marker_multi(m, loc_i, rid)
                st["markers"] += 1
                if "reports[" in loc or "report_groups[" in loc:
                    st["report_markers"] += 1
                st["keys_before"][len(m)] += 1
                st["kinds"][kind] += 1
                if kind == "unmapped":
                    st["unmapped"] += 1
                drops.extend(d)
                bucket = ROUTE_KEYS.get(kind)
                if bucket:                  # 特殊染色 / FISH 度量 / 错位内容 → 容器级
                    tgt = routed.setdefault(id(owner), (owner, defaultdict(list)))[1]
                    for nm in news:
                        tgt[bucket].append({"name": nm["marker_name"],
                                            "raw_value": nm["raw_value"]})
                    changed = True
                    continue
                for new in news:
                    if new != m:
                        st["changed"] += 1
                        changed = True
                    kept.append(new)
            # 必须无条件回写：条目被「纯改名」（如 CerbB-2→HER2）时长度不变，
            arr[:] = kept

    if isinstance(ihc, dict):
        _consume(_iter_marker_lists(ihc))
    _consume(_iter_report_marker_lists(aux))

    for owner, tgt in routed.values():
        for bucket, items in tgt.items():
            cur = owner.get(bucket)
            if not isinstance(cur, list):
                cur = []
            cur.extend(items)
            owner[bucket] = cur
    return changed, st, drops

_APPLY = False
_BACKUP_DIR = None

def _init_worker(apply_flag, backup_dir):
    global _APPLY, _BACKUP_DIR
    _APPLY = apply_flag
    _BACKUP_DIR = backup_dir

def _work_file(p):
    rid = os.path.splitext(os.path.basename(p))[0]
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return {"rid": rid, "bad": 1}

    aux = rec.get("auxiliary_exam") or {}
    ihc = aux.get("ihc")
    # 备份必须在 process() 之前深拷贝：process 是原地改写
    old_ihc = json.loads(json.dumps(ihc, ensure_ascii=False)) if isinstance(ihc, dict) else None
    rg = aux.get("report_groups")
    old_rg = json.loads(json.dumps(rg, ensure_ascii=False)) if rg else None

    changed, st, drops = process(rec, rid)
    out = {"rid": rid, "st": {k: (dict(v) if isinstance(v, Counter) else v)
                              for k, v in st.items()},
           "ndrops": len(drops), "changed": changed, "bad": 0}
    if changed and _APPLY:
        bp = os.path.join(_BACKUP_DIR, "backup_%d.jsonl" % os.getpid())
        with open(bp, "a", encoding="utf-8") as fb:
            fb.write(json.dumps({"rid": rid, "ihc": old_ihc,
                                 "report_groups": old_rg}, ensure_ascii=False) + "\n")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
    if drops and _APPLY:
        dp = os.path.join(_BACKUP_DIR, "dropped_%d.jsonl" % os.getpid())
        with open(dp, "a", encoding="utf-8") as fd:
            fd.write(json.dumps({"rid": rid, "n": len(drops), "fields": drops},
                                ensure_ascii=False) + "\n")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    if not (a.dry_run or a.apply):
        print("需要 --dry-run 或 --apply")
        sys.exit(1)

    files = sorted(glob.glob(ASSEMBLED + "/*.json"))
    if a.dry_run:
        random.seed(42)
        files = random.sample(files, min(a.sample, len(files)))
    if a.limit:
        files = files[:a.limit]

    os.makedirs(OUTDIR, exist_ok=True)
    done = set()
    if a.resume and os.path.exists(DONE):
        with open(DONE, encoding="utf-8") as f:
            done = {ln.strip() for ln in f if ln.strip()}
    todo = [p for p in files if os.path.splitext(os.path.basename(p))[0] not in done]

    tot = Counter()
    kb = Counter()
    kinds = Counter()
    nfiles = 0
    with_drops = 0
    globals()["_APPLY"] = a.apply
    globals()["_BACKUP_DIR"] = OUTDIR
    fdone = open(DONE, "a", encoding="utf-8") if a.apply else None

    def absorb(r):
        nonlocal nfiles, with_drops
        if r["bad"]:
            tot["bad_json"] += 1
            return
        st = r["st"]
        if st.get("markers"):
            nfiles += 1
        if st.get("report_markers"):
            tot["report_files"] += 1
        for k, v in st.items():
            if k == "keys_before":
                kb.update(v)
            elif k == "kinds":
                kinds.update(v)
            else:
                tot[k] += v
        if r["ndrops"]:
            with_drops += 1

    if a.workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                                 initargs=(a.apply, OUTDIR)) as ex:
            for i, r in enumerate(ex.map(_work_file, todo, chunksize=50), 1):
                absorb(r)
                if a.apply:
                    fdone.write(r["rid"] + "\n")
                    if i % 200 == 0:
                        fdone.flush()
                if i % 2000 == 0:
                    print("…%d/%d 有marker=%d 收敛marker=%d"
                          % (i, len(todo), nfiles, tot["changed"]), flush=True)
    else:
        for i, p in enumerate(todo, 1):
            r = _work_file(p)
            absorb(r)
            if a.apply:
                fdone.write(r["rid"] + "\n")
                if i % 200 == 0:
                    fdone.flush()
            if i % 2000 == 0:
                print("…%d/%d 有marker=%d 收敛marker=%d"
                      % (i, len(todo), nfiles, tot["changed"]), flush=True)

    if fdone:
        fdone.close()

    rep = {"mode": "apply" if a.apply else "dry-run",
           "files": len(files), "todo": len(todo), "files_with_markers": nfiles,
           "markers": tot["markers"], "markers_changed": tot["changed"],
           "report_markers": tot["report_markers"], "report_files": tot["report_files"],
           "unmapped": tot["unmapped"], "bad_json": tot["bad_json"],
           "files_with_dropped_fields": with_drops,
           "keys_before": dict(sorted(kb.items())),
           "kinds": dict(kinds)}
    print("\n===== %s =====" % rep["mode"])
    for k in ("files", "todo", "files_with_markers", "markers", "markers_changed",
              "report_markers", "report_files", "unmapped", "bad_json",
              "files_with_dropped_fields"):
        print("  %-26s %s" % (k, rep[k]))
    print("  收敛前单 marker 键数分布:", rep["keys_before"])
    print("  名称类别:", rep["kinds"])
    if not a.apply:
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
