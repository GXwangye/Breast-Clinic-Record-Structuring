# -*- coding: utf-8 -*-
import io
import json
import os
import re
import sys
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
INPUT = ROOT + "/publish/02_结构化结果与过程/中间产物/_agents_input/input_records.jsonl"

IHC_KW = re.compile(r"免疫组化|免疫组织化学")
MK = re.compile(r"(ER|PR|HER-?2?|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3)"
                r"\s*(\(|:|\s*\d|\s*[+-])", re.I)
NO_RESULT = re.compile(r"免疫组化未做|未行免疫组化|建议(做|行)?免疫组化|加做免疫组化|待免疫组化|"
                       r"免疫组化协诊|免疫组化结果未出|已送免疫组化")
ROUTE = re.compile(r"路由|不属于本(模块|字段|板块)|非辅助检查|excluded_text")
# 标志物后跟括号/冒号且括号内出现取值符号 → 该记录确有 IHC 结果
VAL = re.compile(r"(ER|PR|HER-?2|Ki-?67|C-?erbB-?2|P53|AR|E-?cadherin|CK5/6|EGFR|TOP2A|GATA3|"
                 r"P63|Calponin|P120|CK19|CK7|CK20|EMA|Vimentin|D2-40|CD34|P16|GATA-?3)"
                 r"\s*[*＊#]?\s*[（(：:]\s*([^）)]{0,24})[）)]", re.I)
VAL_CH = re.compile(r"[+\-−%％0-9约强弱阴阳膜浆核]")

def has_value(t):
    for m in VAL.finditer(t):
        if VAL_CH.search(m.group(2) or ""):
            return True
    return False

def has_ihc(t):
    return bool(IHC_KW.search(t) or len(MK.findall(t)) >= 2)

def marker_count(rec):
    n = 0
    for path in ("ihc", "auxiliary_exam"):
        o = rec.get(path)
        if isinstance(o, dict):
            ih = o if path == "ihc" else o.get("ihc")
            if isinstance(ih, dict):
                for p in (ih.get("panels") or []):
                    if isinstance(p, dict):
                        n += len(p.get("markers") or [])
    return n

def aux_state(rec):
    ae = rec.get("auxiliary_exam")
    if not isinstance(ae, dict):
        return "无辅检板块"
    ih = ae.get("ihc")
    txt = json.dumps(ae, ensure_ascii=False)
    if isinstance(ih, dict):
        for p in (ih.get("panels") or []):
            if isinstance(p, dict) and (p.get("markers") or []):
                return "panels非空"
    if ROUTE.search(json.dumps(ih, ensure_ascii=False) if isinstance(ih, dict) else ""):
        return "有路由声明无落地"
    for r in (ae.get("reports") or []):
        if isinstance(r, dict) and IHC_KW.search(str(r.get("exam_name") or "")):
            return "记为报告条目"
    if "免疫组化" in txt:
        return "其他提及"
    return "静默为空"

def main():
    src = {}
    with io.open(INPUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            src[r["record_id"]] = r.get("fields") or {}
    print("输入 %d 条" % len(src))

    stat = Counter()
    by_state = Counter()
    strata = Counter()
    per_stat = {}
    n = 0
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        n += 1
        f = src.get(rid)
        if not f:
            continue
        aux_txt = f.get("辅助检查") or ""
        other = {k: (f.get(k) or "") for k in ("现病史", "既往史", "体格检查", "主诉")}
        aux_has = has_ihc(aux_txt)
        other_hits = [k for k, v in other.items() if has_ihc(v)]
        if not aux_has and not other_hits:
            continue
        no_result = bool(NO_RESULT.search(" ".join([aux_txt] + list(other.values()))))
        full_txt = " ".join([aux_txt] + list(other.values()))
        # 判「无结果」须同时满足：出现未做/建议做类措辞 且 全文找不到任何带取值的标志物
        if no_result and not has_value(full_txt):
            stat["原文无IHC结果(不计)"] += 1
            continue
        stat["靶集记录"] += 1
        strict = bool(IHC_KW.search(" ".join([aux_txt] + list(other.values()))))
        if strict:
            stat["其中含『免疫组化』字样"] += 1
        if aux_has:
            if per_stat.get("双写") is None:
                per_stat["双写"] = Counter()
            per_stat["双写"][aux_state(rec)] += 1
            strata["双写"] += 1
            if strict:
                stat["严格子集_双写"] += 1
                if marker_count(rec) > 0:
                    stat["严格子集_双写_有marker"] += 1
        else:
            key = "仅" + "/".join(other_hits)
            if per_stat.get(key) is None:
                per_stat[key] = Counter()
            per_stat[key][aux_state(rec)] += 1
            strata[key] += 1
            if strict:
                stat["严格子集_仅他字段"] += 1
                if marker_count(rec) > 0:
                    stat["严格子集_仅他字段_有marker"] += 1
        if marker_count(rec) > 0:
            stat["产物有marker"] += 1

    out = {
        "口径": "全量 80,718 条；靶集=原文任一字段含 IHC 表达且非'未做/建议做'；按 IHC 是否写在辅助检查字段分层",
        "扫描记录数": n,
        "总计数": dict(stat),
        "分层规模": dict(strata),
        "分层×产物状态": {k: dict(v) for k, v in per_stat.items()},
    }
    p = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC跨字段全量核查.json"
    with io.open(p, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)

    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
    print("wrote", p)

if __name__ == "__main__":
    main()
