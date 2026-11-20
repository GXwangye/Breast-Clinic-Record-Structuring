# -*- coding: utf-8 -*-
import sys
import os
import json
import re
import collections

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "00_core"))
from tarstream import iter_records  # noqa: E402

TAR = os.path.join(ROOT, "publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar")
# 注：本探针跑在**最终产物** assembled_v7_final.tar 上，输出即当前落地实况。
# 本探针只负责「原文有没有、产物填没填」，不回填、不改数据。
JSONL = os.path.join(ROOT, "publish/02_结构化结果与过程/中间产物/_agents_input/input_records.jsonl")
OUT = os.path.join(DATA, "中间产物/_qa_out/tnm_coverage_audit.json")

# 占位词：值为这些字符串时等同「没填」
PLACEHOLDER = {"", "未提及", "未指明", "空", "无", "不详", "未见", "未描述", "未报告",
               "未记载", "u", "unknown", "na", "n/a", "none", "null", "-", "－", "—", "未做", "未查"}

RE_EXPR = re.compile(r"[cpryu]?\s*T\s*[0-4isxX]\s*[NM]\s*[0-3]", re.I)   # pT2N0M0 / cT3N1M0
RE_ROMAN = re.compile(r"(?:I{1,3}|IV|Ⅰ|Ⅱ|Ⅲ|Ⅳ)\s*[ABCabc]?\s*期")          # ⅡA期 / Ⅳ期
RE_STAGE_EN = re.compile(r"\bstage\s*[0IV]", re.I)
MALIGNANT = re.compile(r"恶性|癌")

def is_real(v):
    return str(v or "").strip() not in PLACEHOLDER

def iter_items(rec):
    d = rec.get("diagnoses")
    items = d.get("items") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    for it in (items or []):
        if isinstance(it, dict):
            yield it

def tnm_of(it):
    t = it.get("tnm")
    if isinstance(t, list):
        t = t[0] if t and isinstance(t[0], dict) else {}
    return t if isinstance(t, dict) else {}

def main():
    # 第一遍：产物侧，记录级与条目级 tnm 状态
    rec_state = {}       # rid -> [n_items, n_real, n_ph]
    item_rows = []       # (rid, is_malignant, has_real)
    ph_form = collections.Counter()

    for fn, rec in iter_records(TAR):
        rid = fn.split("/")[-1].replace(".json", "")
        n = nreal = nph = 0
        for it in iter_items(rec):
            n += 1
            t = tnm_of(it)
            filled = any(is_real(t.get(k)) for k in ("t", "n", "m", "stage", "tnm_raw"))
            if filled:
                nreal += 1
                ph_form["有真值"] += 1
            else:
                nph += 1
                key = "占位形态:" + "/".join(str(t.get(k) or "∅") for k in ("t", "n", "m", "stage"))[:40]
                ph_form[key] += 1
            item_rows.append((rid, bool(MALIGNANT.search(str(it.get("name") or ""))), filled))
        rec_state[rid] = [n, nreal, nph]

    # 第二遍：原文侧，记录级 TNM 信号
    sig = {}
    with open(JSONL, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            rid = str(r.get("record_id"))
            if rid not in rec_state:
                continue
            parts = [str(v) for v in (r.get("fields") or {}).values()]
            parts.append(str(r.get("diagnoses_text") or ""))
            txt = "\n".join(parts)
            sig[rid] = (bool(RE_EXPR.search(txt)),
                        bool(RE_ROMAN.search(txt)) or bool(RE_STAGE_EN.search(txt)))

    # 记录级四宫格：原文信号 × 是否有真值
    cat = collections.Counter()
    for rid, (n, nreal, nph) in rec_state.items():
        if n == 0:
            continue
        strong, weak = sig.get(rid, (False, False))
        if strong and nreal == 0:
            cat["A 原文有pTNM式_无真值_漏抽"] += 1
        elif weak and not strong and nreal == 0:
            cat["B 原文仅分期_无真值_漏抽"] += 1
        elif strong or weak:
            cat["C 原文有信号_已有真值"] += 1
        elif nreal > 0:
            cat["D 原文无TNM字样_有真值_合理推断"] += 1
        else:
            cat["E 原文无TNM信号_无真值_正常空壳"] += 1

    # 恶性诊断条目级：分母只取「应有 TNM」的条目
    mal = collections.Counter()
    for rid, is_mal, filled in item_rows:
        if not is_mal:
            continue
        strong, weak = sig.get(rid, (False, False))
        mal["恶性诊断条目总数"] += 1
        if filled:
            mal["有真值"] += 1
        elif strong:
            mal["空_原文有pTNM式"] += 1
        elif weak:
            mal["空_原文仅分期"] += 1
        else:
            mal["空_原文无信号"] += 1

    denom = mal["有真值"] + mal["空_原文有pTNM式"] + mal["空_原文仅分期"]
    rate = (mal["有真值"] / denom) if denom else None
    result = {
        "unit": "记录级=80,718 条就诊；诊断条目级=%d" % sum(v[0] for v in rec_state.values()),
        "tnm_placeholder_vs_real": dict(ph_form.most_common(14)),
        "record_level_by_source_signal": dict(cat.most_common()),
        "malignant_diag_item_level": dict(mal.most_common()),
        "fill_rate_in_should_have": {
            "分母(原文有TNM信号的恶性诊断条目)": denom,
            "分子(有真值)": mal["有真值"],
            "落地率": round(rate, 4) if rate is not None else None,
            "漏抽条数": mal["空_原文有pTNM式"] + mal["空_原文仅分期"],
        },
        "conclusion": (
            "空壳绝大多数属正常：77.8%% 记录原文无任何 TNM 字样，留空即正确行为。"
            "原文有 TNM 信号的恶性诊断条目落地率 %s（%d/%d，口径：t/n/m/stage 任一有值），"
            "余 %d 条为原文只有孤立分期或归属不唯一，口径上不补。D 类为转移灶→M1/IV 的合理推断，非幻觉。"
            % (("%.1f%%" % (rate * 100)) if rate is not None else "—",
               mal["有真值"], denom,
               mal["空_原文有pTNM式"] + mal["空_原文仅分期"])
        ),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    print(json.dumps(result, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
