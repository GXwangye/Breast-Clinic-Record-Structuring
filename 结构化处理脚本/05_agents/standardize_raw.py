# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
INPUT = DATA + "/中间产物/_agents_input/input_records.jsonl"
OUT_DIR = DATA + "/中间产物/standardized_v1"
LOG_DIR = DATA + "/中间产物/_standardize"
DONE = LOG_DIR + "/standardize_done.txt"
CYCLE_LOG = LOG_DIR + "/standardize_cycles.jsonl"
QC = LOG_DIR + "/standardize_qc.jsonl"

BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
MODEL = "claude-sonnet-4-6"
CYCLE_START, CYCLE_MAX, GROW_AFTER = 50, 100, 2
ANCHOR_HIT_MIN = 0.80
ERR_RATE_MAX = 0.10

FIELDS = ("主诉", "现病史", "既往史", "辅助检查", "体格检查")
KINDS = {"narrative", "exam_report", "lab_result", "pathology", "ihc", "cross_ref_stub"}
ROUTE_KINDS = {"exam_report", "lab_result", "pathology", "ihc"}   # 命中即由规则层路由到辅助检查
CROSS_REF_RE = re.compile(r"^(同前|同上|同现病史|同上次|同入院|同术后|同门诊)[。\.]?$")

CONN_RE = re.compile(r"Connection refused|Connection reset|timed out|Remote end closed|"
                     r"Errno 61|Errno 104|Errno 54|Name or service|Temporary failure|"
                     r"Network is unreachable|EOF occurred|SSL|"
                     # 属基础设施故障而非抽取质量问题，应与 Erdno 类一并走自愈重跑通道）
                     r"HTTP Error 500|HTTP Error 502|HTTP Error 503|HTTP Error 504|"
                     r"Service Unavailable|Bad Gateway|Gateway Timeout|Too Many Requests", re.I)
RATE_RE = re.compile(r"429|Too Many Requests", re.I)   # 命中即延长退避，避免重试加剧限流
CONN_RETRY_MAX = 10

SYS = """你是原始病历标准化子智能体。任务：给病历的 5 个文本字段标出【分段点】和每段类型。原文一字不改，你只需指出每段从哪开始。

输出一个 JSON 对象：
{"fields": {"主诉": [mark,...], "现病史": [mark,...], "既往史": [mark,...], "辅助检查": [mark,...], "体格检查": [mark,...]},
 "cross_ref": ["..."]}

mark = {"kind": "类型", "start": "该段开头6~30字的逐字原文"}

硬规则：
1) start 必须是原文中逐字存在的连续片段（6~30字，取该段开头），禁止改写一字、禁止拼凑不相邻文字。规则层会按 start 定位并切片，段正文不需要你抄写。
2) 每段一个 mark，按原文先后顺序排列。每个字段的第一个 mark 必须对应该字段原文的开头。
3) kind 只允许：narrative(一般叙述) / exam_report(影像检查报告段) / lab_result(检验结果段) / pathology(病理报告段) / ihc(免疫组化段) / cross_ref_stub(同前/同上等占位符)。
4) 拆分点落在语义边界：一份报告从其第一个字（通常是日期或检查名）开始。单段不超过 500 字，报告各自独立成段，禁止整字段并成一段。
5) 现病史/既往史/主诉/体格检查中凡涉及检查、检验、病理、免疫组化的内容必须独立成段并标对应 kind，禁止裹在 narrative 大段里。规则层会把这些段自动归位登记到辅助检查，你无需重复输出。
6) 字段原文为「无」「无特殊」等短句时输出一个 narrative mark。「同前」「同上」「同现病史」类占位符标 cross_ref_stub 并登记进 cross_ref 数组，禁止虚构其指代内容。
7) 空字段输出空数组。只输出一个合法 JSON 对象，禁止解释文字，禁止 ``` 围栏，字符串内引号换行必须转义。"""

try:
    import demjson3
    _HAVE_DEMJSON = True
except Exception:
    _HAVE_DEMJSON = False

_lock = threading.Lock()
REQ_TIMEOUT = 240
MAX_TOKENS = 20000   # 服务端默认开启 thinking 并计入额度，给足防截断
OFF_MAX_TOKENS = 4000
ESCALATE_HIT_MIN = 0.85  # 快路径质量线：marker≥5 且 anchor 命中低于此值 → 升级 thinking 重跑

def chat(key, user_text, max_tokens=MAX_TOKENS, retry=3, enable_thinking=None):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user_text}],
        "max_tokens": max_tokens, "temperature": 0,
    }
    if enable_thinking is False:
        body["enable_thinking"] = False
    body = json.dumps(body).encode("utf-8")
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(BASE_URL + "/chat/completions", data=body,
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join((p.get("text", "") if isinstance(p, dict) else str(p)) for p in content)
            if not content:
                raise ValueError("响应缺content: " + json.dumps(d, ensure_ascii=False)[:200])
            return content
        except Exception as e:
            last = e
            if i < retry - 1:
                time.sleep(2 + 4 * i)
    raise last

def parse_json(raw):
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a >= 0 and b > a:
        raw = raw[a:b + 1]
    if not raw:
        raise ValueError("空响应(仅思考段,无JSON)")
    try:
        return json.loads(raw)
    except Exception:
        if _HAVE_DEMJSON:
            return demjson3.decode(raw)
        raise

def locate_verbatim(field_text, quote, from_pos=0):
    p = field_text.find(quote, from_pos)
    if p >= 0:
        return p, p + len(quote)
    if from_pos == 0:
        p = field_text.find(quote)
        if p >= 0:
            return p, p + len(quote)
    qs = re.sub(r"\s+", "", quote)
    if not qs:
        return None
    fs_chars, idx_map = [], []
    for i, ch in enumerate(field_text):
        if not ch.isspace():
            fs_chars.append(ch)
            idx_map.append(i)
    fs = "".join(fs_chars)
    # 非空白串中的 from_pos 换算：找第一个 idx_map[i]>=from_pos
    base = 0
    while base < len(idx_map) and idx_map[base] < from_pos:
        base += 1
    pos = fs.find(qs, base)
    if pos < 0 and from_pos == 0:
        pos = fs.find(qs)
    if pos < 0:
        return None
    return idx_map[pos], idx_map[pos + len(qs) - 1] + 1

def build_user_text(rid, rec):
    f = rec.get("fields") or {}
    parts = ["【病历ID】%s" % rid, "【源文字段原文】"]
    for k in FIELDS:
        v = (f.get(k) or "").strip()
        parts.append("〈%s〉\n%s" % (k, v if v else "（空）"))
    return "\n\n".join(parts)

def _marks_to_segments(raw_fields, llm_fields):
    n_anchor, n_total, n_routed, autofilled_chars, n_fallback = 0, 0, 0, 0, 0
    dropped = []
    result_fields = {}
    for fld in FIELDS:
        raw = raw_fields[fld]
        marks = llm_fields.get(fld) or []
        if not isinstance(marks, list):
            marks = []
        if not raw.strip():
            result_fields[fld] = {"segments": [], "excluded_text": []}
            continue
        # 逐 marker 保序定位（start=段首6~30字逐字）
        located = []
        pos = 0
        for m in marks:
            if not isinstance(m, dict):
                continue
            n_total += 1
            q = str(m.get("start", "") or "").strip()
            kind = m.get("kind") if m.get("kind") in KINDS else "narrative"
            if not q:
                dropped.append({"fld": fld, "err": "start空",
                                "item": json.dumps(m, ensure_ascii=False)[:120]})
                continue
            loc = locate_verbatim(raw, q, from_pos=pos)
            if loc is None:
                dropped.append({"fld": fld, "err": "start未命中",
                                "item": json.dumps(m, ensure_ascii=False)[:120]})
                continue
            n_anchor += 1
            located.append((loc[0], kind))
            pos = loc[0] + 1
        # 同起点去重（先到优先），按位置排序
        seen, boundaries = set(), []
        for p0, kind in sorted(located, key=lambda x: x[0]):
            if p0 in seen:
                continue
            seen.add(p0)
            boundaries.append((p0, kind))
        # 切片：段=相邻边界间的原文区间，守恒由构造保证
        segs = []
        if not boundaries:
            segs.append({"text": raw, "kind": "narrative", "no_marker": True,
                         "anchor": {"field": fld, "quote": raw, "start": 0, "end": len(raw)}})
            dropped.append({"fld": fld, "err": "无marker整段兜底", "item": raw[:60]})
            n_fallback += 1
        else:
            if boundaries[0][0] > 0:
                lead = raw[:boundaries[0][0]]
                if lead.strip():
                    autofilled_chars += len(re.sub(r"\s+", "", lead))
                    segs.append({"text": lead, "kind": "narrative", "auto_fill": True,
                                 "anchor": {"field": fld, "quote": lead,
                                            "start": 0, "end": boundaries[0][0]}})
            for idx, (p0, kind) in enumerate(boundaries):
                p1 = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(raw)
                segs.append({"text": raw[p0:p1], "kind": kind,
                             "anchor": {"field": fld, "quote": raw[p0:p1],
                                        "start": p0, "end": p1}})
        excluded = [{"quote": sg["text"], "routed_to": "辅助检查"}
                    for sg in segs if sg["kind"] in ROUTE_KINDS and fld != "辅助检查"]
        result_fields[fld] = {"segments": segs, "excluded_text": excluded}
    # 源字段 segments 不再保留该段（消除重复内容），移出痕迹由 excluded_text 承载。
    # 与旧「复制保留」口径的区别：旧口径源字段留副本导致下游叙述板块重复抽取辅检内容。
    for fld in FIELDS:
        if fld == "辅助检查":
            continue
        keep, moved = [], []
        for sg in result_fields[fld]["segments"]:
            (moved if sg["kind"] in ROUTE_KINDS else keep).append(sg)
        if moved:
            result_fields[fld]["segments"] = keep
            result_fields["辅助检查"]["segments"].extend(dict(sg) for sg in moved)
            n_routed += len(moved)
    stats = {"anchor_hit": n_anchor, "total": n_total, "fallback": n_fallback,
             "routed": n_routed, "autofilled": autofilled_chars,
             "segments": sum(len(result_fields[k]["segments"]) for k in FIELDS)}
    return result_fields, stats, dropped

def _attempt(key, user_text, raw_fields, thinking):
    if thinking:
        try:
            raw = chat(key, user_text)
            out = parse_json(raw)
        except Exception:
            raw = chat(key, user_text, max_tokens=32000, retry=1)
            out = parse_json(raw)
    else:
        raw = chat(key, user_text, max_tokens=OFF_MAX_TOKENS, retry=3, enable_thinking=False)
        out = parse_json(raw)
    llm_fields = (out.get("fields") or {}) if isinstance(out, dict) else {}
    cross_ref = out.get("cross_ref") or []
    result_fields, stats, dropped = _marks_to_segments(raw_fields, llm_fields)
    return result_fields, stats, dropped, cross_ref

def process_one(rid, rec, key, write):
    raw_fields = {k: (rec.get("fields") or {}).get(k) or "" for k in FIELDS}
    user_text = build_user_text(rid, rec)
    escalated = 0
    ok = False
    try:
        result_fields, stats, dropped, cross_ref = _attempt(key, user_text, raw_fields, False)
        hit = stats["anchor_hit"] / stats["total"] if stats["total"] else 1.0
        ok = not ((stats["total"] >= 5 and hit < ESCALATE_HIT_MIN) or stats["fallback"])
    except Exception:
        result_fields = None
    if not ok:
        # 升级：开 thinking 原链路重跑本条（失败则异常上抛，由周期层记 qc）
        escalated = 1
        result_fields, stats, dropped, cross_ref = _attempt(key, user_text, raw_fields, True)

    raw_chars = sum(len(re.sub(r"\s+", "", raw_fields[k])) for k in FIELDS)
    doc = {"record_id": rid, "std_version": "standardized_v1",
           "fields": result_fields,
           "qc": {"cross_ref": [str(x) for x in cross_ref if x],
                  "autofilled_chars": stats["autofilled"],
                  "raw_chars": raw_chars,
                  "escalated_thinking": escalated,
                  "notes": []}}
    if write:
        json.dump(doc, open(os.path.join(OUT_DIR, rid + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return rid, {"anchor_hit": stats["anchor_hit"], "total": stats["total"], "dropped": dropped,
                 "autofilled": stats["autofilled"], "raw_chars": raw_chars,
                 "n_segments": stats["segments"], "n_routed": stats["routed"],
                 "escalated": escalated, "doc": doc}

def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--limit", type=int, default=0)
    ap_.add_argument("--rids", type=str, default="", help="只跑指定 rid（逗号分隔），用于定点验证")
    ap_.add_argument("--workers", type=int, default=2)
    ap_.add_argument("--dry-run", action="store_true", help="只调LLM+校验，不写盘")
    ap_.add_argument("--verbose", type=int, default=0, help="干跑时打印前N条产物的段结构")
    args = ap_.parse_args()
    key = os.environ.get("AUX_LLM_KEY", "")
    if not key:
        print("缺 AUX_LLM_KEY 环境变量")
        sys.exit(2)
    write = not args.dry_run
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    done = set()
    if write and os.path.exists(DONE):
        done = set(x.strip() for x in open(DONE, encoding="utf-8") if x.strip())

    print("加载输入快照...", flush=True)
    want = set(x.strip() for x in args.rids.split(",") if x.strip())
    targets = []
    recs = {}
    for line in open(INPUT, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        rid = str(d.get("record_id"))
        if want:
            if rid in want:
                recs[rid] = d
                targets.append(rid)
            continue
        if rid in done:
            continue
        recs[rid] = d
        targets.append(rid)
    if args.limit and not want:
        targets = targets[: args.limit]
    print("靶点 %d 条（跳过已完成 %d）" % (len(targets), len(done)), flush=True)

    clf = open(CYCLE_LOG, "a", encoding="utf-8")
    qf = open(QC, "a", encoding="utf-8")
    df = open(DONE, "a", encoding="utf-8") if write else None
    cycle_size, streak, n_cycle = CYCLE_START, 0, 0
    conn_retries = 0
    verbose_left = args.verbose
    i = 0
    while i < len(targets):
        batch = targets[i:i + cycle_size]
        i += cycle_size
        n_cycle += 1
        c_anchor, c_total, c_err, c_err_conn, c_esc, c_rate = 0, 0, 0, 0, 0, 0
        c_segs, c_routed, c_autofilled, c_rawchars = 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_one, rid, recs[rid], key, write): rid for rid in batch}
            for fut in as_completed(futs):
                rid = futs[fut]
                try:
                    rid, res = fut.result()
                except Exception as e:
                    c_err += 1
                    _msg = str(e)
                    if CONN_RE.search(_msg):
                        c_err_conn += 1
                    if RATE_RE.search(_msg):
                        c_rate += 1
                    qf.write(json.dumps({"rid": rid, "err": "exception:" + str(e)[:200]},
                                        ensure_ascii=False) + "\n")
                    continue
                c_anchor += res["anchor_hit"]
                c_total += res["total"]
                c_segs += res["n_segments"]
                c_routed += res["n_routed"]
                c_autofilled += res["autofilled"]
                c_rawchars += res["raw_chars"]
                c_esc += res.get("escalated", 0)
                for dp in res["dropped"]:
                    dp["rid"] = rid
                    qf.write(json.dumps(dp, ensure_ascii=False) + "\n")
                if verbose_left > 0 and not write:
                    verbose_left -= 1
                    doc = res.get("doc") or {}
                    print("  [样例] rid=%s 段数=%d 路由=%d 自填字符=%d" %
                          (rid, res["n_segments"], res["n_routed"], res["autofilled"]), flush=True)
                    for fld in FIELDS:
                        fd = (doc.get("fields") or {}).get(fld) or {}
                        for sg in (fd.get("segments") or []):
                            mark = "AUTO" if sg.get("auto_fill") else sg.get("kind", "")
                            print("    〈%s〉[%s] %s" % (fld, mark, str(sg.get("text", ""))[:60].replace("\n", " ")),
                                  flush=True)
                        for ex_ in (fd.get("excluded_text") or []):
                            print("    〈%s〉[路由出→%s] %s" % (fld, ex_.get("routed_to"),
                                  str(ex_.get("quote", ""))[:60].replace("\n", " ")), flush=True)
                if df:
                    with _lock:
                        df.write(rid + "\n")
                        df.flush()
        if df:
            df.flush()
        qf.flush()
        hit = c_anchor / c_total if c_total else 1.0
        verdict, why = "PASS", ""
        if c_err / max(len(batch), 1) > ERR_RATE_MAX:
            verdict, why = "HALT", "周期记录异常率 %.0f%% 越限(>10%%)" % (100 * c_err / len(batch))
        elif c_total >= 10 and hit < ANCHOR_HIT_MIN:
            verdict, why = "HALT", "anchor命中 %.0f%% <80%%（系统性失败信号）" % (100 * hit)
        rec = {"cycle": n_cycle, "size": len(batch), "segments": c_segs, "routed": c_routed,
               "anchor_hit": round(hit, 3), "errors": c_err, "conn_errors": c_err_conn,
               "escalated": c_esc, "rate_limited": c_rate, "autofilled_chars": c_autofilled,
               "raw_chars": c_rawchars, "verdict": verdict, "why": why}
        clf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        clf.flush()
        print("周期 %d (size %d): 段 %d, 路由段 %d, anchor命中 %.0f%%, 升级thinking %d, 自填字符 %d/%d, %s" %
              (n_cycle, len(batch), c_segs, c_routed, 100 * hit, c_esc, c_autofilled, c_rawchars, verdict),
              flush=True)
        if verdict == "HALT" and c_err and c_err_conn >= 0.9 * c_err and conn_retries < CONN_RETRY_MAX:
            conn_retries += 1
            backoff = 300 if c_rate else 180   # 429 是限流信号，退避加倍避免越挤越堵
            print("[RETRY] 连接类异常 %d/%d（第 %d/%d 次，含限流 %d），%d 秒后重跑本批" %
                  (c_err_conn, c_err, conn_retries, CONN_RETRY_MAX, c_rate, backoff), flush=True)
            time.sleep(backoff)
            i -= cycle_size
            n_cycle -= 1
            continue
        conn_retries = 0
        if verdict == "HALT":
            print("[HALT] %s — 停批待修" % why)
            break
        streak += 1
        if streak >= GROW_AFTER and cycle_size < CYCLE_MAX:
            cycle_size = min(cycle_size * 2, CYCLE_MAX)
            streak = 0
            print("  [提速] 步长增至 %d" % cycle_size, flush=True)
    clf.close()
    qf.close()
    if df:
        df.close()
    print("[DONE]")

if __name__ == "__main__":
    main()
