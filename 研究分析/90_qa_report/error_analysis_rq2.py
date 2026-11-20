# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
DEFAULT_INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
DEFAULT_FINAL = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"

# 强否定词（跟"具体病症"连用时才是真否定；"无意/无法"等词素已排除）
NEG_STRONG = ["否认", "未见", "未及", "未触及", "未扪及", "未发现", "无异常", "无明显",
              "无特殊", "无压痛", "阴性", "未查见", "无殊"]
# 时间归因：既往史里不该出现的"本次/今日"（本次就诊内容），或现病史里混入的纯既往手术史
NOW_WORDS = ["今日", "本次", "来诊", "现", "目前", "今"]

def load_input(path):
    recs = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            recs[r["record_id"]] = r
    return recs

def iter_items(structured):
    if not isinstance(structured, dict):
        return
    for k, v in structured.items():
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    yield k, it

def scan_negation(final):
    cands = []
    for sec in ["chief_complaint", "present_illness", "past_history", "physical_exam"]:
        st = (final.get(sec) or {}).get("structured") or {}
        for k, it in iter_items(st):
            q = (it.get("anchor") or {}).get("quote", "")
            neg = it.get("neg")
            txt = it.get("text") or it.get("detail") or it.get("finding") or ""
            # 只有"强否定词"且 quote 里有具体病症语义，才可能翻转
            has_strong = any(w in q for w in NEG_STRONG)
            if not has_strong:
                continue
            # 真矛盾信号：neg 不是 True（即未标否定），或 text 为空（抽空了）
            if neg is not True or txt == "":
                cands.append({
                    "sec": sec, "key": k, "text": txt, "neg_field": neg,
                    "quote": q[:120],
                })
    return cands

def scan_time_attribution(final):
    cands = []
    st = (final.get("past_history") or {}).get("structured") or {}
    for k, it in iter_items(st):
        time = it.get("time", "") or ""
        detail = it.get("detail") or it.get("text") or ""
        q = (it.get("anchor") or {}).get("quote", "")
        # 既往史里出现"本次/今日/来诊/目前"——疑似把本次就诊内容错归到既往史
        if any(w in (str(time) + detail + q) for w in NOW_WORDS):
            cands.append({
                "sec": "past_history", "key": k, "time": str(time),
                "detail": detail, "quote": q[:120],
            })
    return cands

def scan_ihc_ambiguity(final):
    cands = []
    ax = final.get("auxiliary_exam") or {}
    ihc = ax.get("ihc") or {}
    panels = ihc.get("panels", []) if isinstance(ihc, dict) else []
    for p in panels:
        for m in (p.get("markers") or []):
            name = (m.get("marker_name") or m.get("name") or "")
            status = m.get("status", "")
            call = m.get("call", "")
            raw = m.get("raw_value", "")
            # AR 出 call（AR 归诊断鉴别型，不应出 call）
            if "AR" in str(name).upper() and call:
                cands.append({"kind": "AR出call", "marker": name, "status": status, "call": call})
            # status 阳性/阴性 与 call 矛盾
            if status in ("阳性", "阴性") and call and call not in ("不确定", "", status):
                cands.append({"kind": "status-call矛盾", "marker": name, "status": status, "call": call})
            # 空值出明确 call
            if (raw in ("", "()", "（）", None) or status == "") and call in ("阳性", "阴性"):
                cands.append({"kind": "空值出call", "marker": name, "status": status, "raw": raw, "call": call})
    return cands

def scan_value_range(final):
    cands = []
    ax = final.get("auxiliary_exam") or {}
    ihc = ax.get("ihc") or {}
    panels = ihc.get("panels", []) if isinstance(ihc, dict) else []
    for p in panels:
        for m in (p.get("markers") or []):
            name = m.get("marker_name") or ""
            pc = m.get("percent")
            if isinstance(pc, (int, float)) and (pc < 0 or pc > 100):
                cands.append({"kind": "percent越界", "marker": name, "value": pc})
    return cands

# ---------------------------------------------------------------------------
# LLM 复核（语义类：否定/时间归因）
# ---------------------------------------------------------------------------
LLM_SYS = """你是病历结构化抽取的质检员。给定一条「原文片段」和抽取出的「结构化条目」，判断该条目的抽取是否存在下列错误之一：
1. 否定翻转：原文明确否定某病症（如"否认高血压""无红肿""未及肿物"），但结构化把它标成了阳性存在（neg 应为 True 却非 True），或把否定内容抽成了阳性症状。
2. 时间归因错位：本该归「既往史」的内容（既往手术/化疗/放疗）被错放到其他板块；或把本次就诊内容（今日/本次/来诊）错归到既往史。

只输出一个 JSON 对象，形如 {"is_error": true/false, "error_type": "否定翻转"/"时间归因"/"无", "reason": "一句话理由"}。
不要输出任何解释文字、不要 ``` 围栏。"""

def llm_review(backend, cand):
    user = json.dumps({
        "原文片段": cand.get("quote", ""),
        "抽取条目": {"text": cand.get("text", ""), "detail": cand.get("detail", ""),
                     "time": cand.get("time", ""), "neg_field": cand.get("neg_field", "")},
        "候选类型": cand.get("type", ""),
    }, ensure_ascii=False)
    try:
        obj = backend.chat(LLM_SYS, user, schema=None)
        return obj if isinstance(obj, dict) else {"is_error": None, "reason": "解析失败"}
    except Exception as e:
        return {"is_error": None, "reason": "调用失败:%s" % str(e)[:80]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--final", default=DEFAULT_FINAL)
    ap.add_argument("--out", default=DATA + "/中间产物/_qa_out/error_analysis_rq2.json")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--base-url", default=os.environ.get("AUX_LLM_BASE_URL", os.environ.get("OPENAI_BASE_URL", "")))
    ap.add_argument("--api-key", default=os.environ.get("AUX_LLM_KEY", ""))
    a = ap.parse_args()

    recs = load_input(a.input)
    rids = list(recs.keys())

    backend = None
    if a.llm:
        if not a.api_key:
            print("✗ --llm 需要 --api-key（或 AUX_LLM_KEY）")
            sys.exit(1)
        sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
        from run_batch import OpenAIBackend
        backend = OpenAIBackend(a.base_url, a.api_key, a.model,
                                is_deepseek=("deepseek" in a.model.lower()),
                                is_claude=("claude" in a.model.lower()))

    details = []
    for rid in rids:
        fp = os.path.join(a.final, rid + ".json")
        if not os.path.exists(fp):
            continue
        with open(fp, encoding="utf-8") as f:
            final = json.load(f)

        for c in scan_negation(final):
            c = {"rid": rid, "type": "否定翻转", **c}
            if backend:
                r = llm_review(backend, c)
                c["llm_is_error"] = r.get("is_error")
                c["llm_reason"] = r.get("reason")
            details.append(c)

        for c in scan_time_attribution(final):
            c = {"rid": rid, "type": "时间归因", **c}
            if backend:
                r = llm_review(backend, c)
                c["llm_is_error"] = r.get("is_error")
                c["llm_reason"] = r.get("reason")
            details.append(c)

        for c in scan_ihc_ambiguity(final):
            details.append({"rid": rid, "type": "IHC歧义", **c})

        for c in scan_value_range(final):
            details.append({"rid": rid, "type": "数值越界", **c})

    # 统计
    by_type = {}
    for d in details:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    # 定案（LLM 复核后：is_error=True 的才计入"失败"）
    confirmed = [d for d in details if d.get("llm_is_error") is True or d["type"] in ("IHC歧义", "数值越界")]
    result = {
        "total": len(rids),
        "candidate_total": len(details),
        "by_type": by_type,
        "confirmed_error_total": len(confirmed),
        "confirmed_by_type": {},
        "details": details,
    }
    for d in confirmed:
        result["confirmed_by_type"][d["type"]] = result["confirmed_by_type"].get(d["type"], 0) + 1

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    print("=== RQ2 错误分析 ===")
    print("总记录数:", result["total"])
    print("候选总数:", result["candidate_total"], "（按类型）:", json.dumps(by_type, ensure_ascii=False))
    print("定案失败总数:", result["confirmed_error_total"], "（按类型）:", json.dumps(result["confirmed_by_type"], ensure_ascii=False))
    print("输出:", a.out)

if __name__ == "__main__":
    main()
