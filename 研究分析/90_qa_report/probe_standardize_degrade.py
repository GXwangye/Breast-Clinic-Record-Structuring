#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import io
import json
import os
import re
from collections import Counter, defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
QC = DATA + "/中间产物/_standardize/standardize_qc.jsonl"
DONE = DATA + "/中间产物/_standardize/standardize_done.txt"
OUT_DIR = DATA + "/中间产物/standardized_v1"
INPUT = DATA + "/中间产物/_agents_input/input_records.jsonl"
OUT = DATA + "/qa_reports/standardize_degrade.json"

N_TOTAL_RECORDS = 80718
SAMPLE_N = 400          # 守恒核验抽样数（按 rid 升序取前 N，可复现）
LOC_ERR = ("start空", "start未命中", "无marker整段兜底")
EXC_KINDS = ("服务端错误(5xx)", "读超时或连接中断", "返回格式损坏", "权限或限流", "空响应或缺正文")

def _exc_kind(msg):
    if "HTTP Error 5" in msg:
        return "服务端错误(5xx)"
    if "read operation" in msg or "urlopen error" in msg:
        return "读超时或连接中断"
    if "HTTP Error 40" in msg or "HTTP Error 429" in msg:
        return "权限或限流"
    if "空响应" in msg or "缺content" in msg:
        return "空响应或缺正文"
    return "返回格式损坏"

def main():
    events = Counter()
    rid_of = defaultdict(set)
    rid_by_kind = defaultdict(set)
    exc_times = Counter()
    for line in io.open(QC, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        e = str(d.get("err") or "?")
        rid = str(d.get("rid") or "")
        kind = _exc_kind(e) if e.startswith("exception") else e
        events[kind] += 1
        if rid:
            rid_of["exception" if kind in EXC_KINDS else "location"].add(rid)
            rid_by_kind[kind].add(rid)
        if kind in EXC_KINDS and rid:
            exc_times[rid] += 1

    loc_rids = rid_of["location"]
    exc_rids = rid_of["exception"]
    all_rids = loc_rids | exc_rids

    def has_out(rid):
        return os.path.exists(os.path.join(OUT_DIR, rid + ".json"))

    exc_missing = [r for r in exc_rids if not has_out(r)]
    loc_missing = [r for r in loc_rids if not has_out(r)]

    # 异常记录补齐后的形态（段数），空壳判据
    def n_seg(rid):
        p = os.path.join(OUT_DIR, rid + ".json")
        if not os.path.exists(p):
            return -1
        d = json.load(open(p, encoding="utf-8"))
        return sum(len((fd or {}).get("segments") or [])
                   for fd in (d.get("fields") or {}).values())

    exc_seg = [n_seg(r) for r in exc_rids]
    exc_empty = sum(1 for x in exc_seg if x <= 0)

    # 整段兜底涉及多少条记录
    fallback_rids = set()
    for line in io.open(QC, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get("err") == "无marker整段兜底":
            fallback_rids.add(str(d.get("rid")))
    # 同一记录可能多字段兜底，"无marker整段兜底"事件数 > 记录数

    # 定位类是否被事后重跑：done.txt 重复写入的 rid 落在哪一类
    done_lines = [x.strip() for x in io.open(DONE, encoding="utf-8") if x.strip()]
    dup = {k for k, v in Counter(done_lines).items() if v > 1}
    dup_in_loc = len(dup & loc_rids)
    dup_in_exc = len(dup & exc_rids)

    # 字段级逐字守恒（抽样）
    sample = sorted(loc_rids)[:SAMPLE_N]
    S = set(sample)
    raw_of = {}
    for line in io.open(INPUT, encoding="utf-8"):
        line = line.strip()
        if not line or '"record_id"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        rid = str(d.get("record_id"))
        if rid in S:
            raw_of[rid] = d.get("fields") or {}
        if len(raw_of) == len(S):
            break

    def nows(s):
        return re.sub(r"\s+", "", s or "")

    # 守恒分两层核：记录级（字符频次相等，不受跨字段路由影响）与字段级（仅在
    # 无段路由出、且非辅助检查这类接收方字段上可比，因为归位会改变字段内文本）。
    cmp_rec = eq_rec = 0
    cmp_fields = 0
    eq_fields = 0
    bad = []
    no_marker_segs = 0
    for rid in sample:
        raw = raw_of.get(rid)
        if raw is None:
            continue
        p = os.path.join(OUT_DIR, rid + ".json")
        if not os.path.exists(p):
            continue
        doc = json.load(open(p, encoding="utf-8"))
        fields = doc.get("fields") or {}
        raw_all = "".join(nows(str(v or "")) for v in raw.values())
        segs_all = [sg for fd in fields.values() for sg in (fd.get("segments") or [])]
        seg_all = "".join(nows(str(sg.get("text") or "")) for sg in segs_all)
        cmp_rec += 1
        if Counter(raw_all) == Counter(seg_all):
            eq_rec += 1
        elif len(bad) < 5:
            bad.append({"rid": rid, "层": "记录",
                        "raw_len": len(raw_all), "seg_len": len(seg_all)})
        for fld, txt in raw.items():
            fd = fields.get(fld) or {}
            segs = fd.get("segments") or []
            for sg in segs:
                if sg.get("no_marker"):
                    no_marker_segs += 1
            # 归位会把别的字段的段移入本字段（如治疗史并入既往史），故只比
            # 段归属仍为本字段者；有段被移出或移入即跳过该字段。
            moved_in = any((sg.get("anchor") or {}).get("field") not in (None, fld)
                           for sg in segs)
            if fd.get("excluded_text") or moved_in:
                continue
            cmp_fields += 1
            joined = nows("".join(str(sg.get("text") or "") for sg in segs))
            if joined == nows(str(txt or "")):
                eq_fields += 1

    res = {
        "口径": "qc 一行是一处失败事件；记录数为 rid 去重",
        "事件": {
            "合计": sum(events.values()),
            "定位类": sum(v for k, v in events.items() if k in LOC_ERR),
            "异常类": sum(v for k, v in events.items() if k in EXC_KINDS),
            "明细": dict(events.most_common()),
        },
        "各类记录": {k: len(v) for k, v in rid_by_kind.items()},
        "记录": {
            "全库": N_TOTAL_RECORDS,
            "涉及": len(all_rids),
            "涉及占比": 100.0 * len(all_rids) / N_TOTAL_RECORDS,
            "定位类": len(loc_rids),
            "异常类": len(exc_rids),
            "两类重叠": len(loc_rids & exc_rids),
        },
        "定位类去向": {
            "规则": "定位失败的 marker 不成为段边界，其原文并入相邻段；整字段无可用 marker 时整段作为叙述段（no_marker）",
            "产物缺失": len(loc_missing),
            "守恒抽样": {
                "抽样记录": cmp_rec,
                "记录级_字符频次相等": eq_rec,
                "记录级_不等": cmp_rec - eq_rec,
                "字段级_可比": cmp_fields,
                "字段级_逐字相等": eq_fields,
                "字段级_不等": cmp_fields - eq_fields,
                "不等样例": bad,
            },
            "抽样内no_marker段": no_marker_segs,
            "事后定向重跑": 0,
            "done重复写入": dup_in_loc,
        },
        "异常类去向": {
            "规则": "当次不写产物、不写完成记录，续跑时重跑；不做内容修补",
            "产物缺失": len(exc_missing),
            "补齐记录": len(exc_rids) - len(exc_missing),
            "补齐后仍无段": exc_empty,
            "重送轮次分布": dict(sorted(Counter(exc_times.values()).items())),
            "事后重跑记录数": dup_in_exc,
        },
        "整段兜底": {
            "事件": events.get("无marker整段兜底", 0),
            "记录": len(fallback_rids),
            "占全库": 100.0 * len(fallback_rids) / N_TOTAL_RECORDS,
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:2400])
    print("\n写入", OUT)

if __name__ == "__main__":
    main()
