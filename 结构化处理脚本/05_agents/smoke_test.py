# -*- coding: utf-8 -*-

import argparse
import glob
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
AGENT_ROOT = DATA + "/中间产物/agents_v7"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import section_spec  # noqa: E402
import run_batch as R  # noqa: E402
import assemble as A  # noqa: E402

# 每个板块的「已填充」判定谓词
def _is_populated(name, val):
    if not isinstance(val, dict):
        return bool(val)
    if name == "auxiliary_exam":
        return bool(val.get("report_groups")) or bool(val.get("ihc"))
    if name == "diagnoses":
        items = val.get("items") or []
        return len(items) > 0
    if name == "orders":
        items = val.get("items") or []
        return len(items) > 0
    # 其余：只要有任意非空字段即算填充
    return any(v not in (None, "", [], {}, 0) for v in val.values())

CRITICAL = {"diagnoses", "orders", "auxiliary_exam"}

def check_existing(limit, min_rate, rid_filter=None):
    # 只取合并产物 <rid>/<rid>.json（排除 01_patient.json 等以数字开头的分板块文件）
    recs = []
    for d in sorted(glob.glob(os.path.join(AGENT_ROOT, "*"))):
        if not os.path.isdir(d):
            continue
        rid = os.path.basename(d)
        if "TEST_" in rid:
            continue
        # rid_filter 不为空时只校验本次实际跑过的记录，避免误读旧跑（如上一轮空诊断）的陈余产物
        if rid_filter is not None and rid not in rid_filter:
            continue
        merged = os.path.join(d, "%s.json" % rid)
        if os.path.exists(merged):
            recs.append(merged)
    if limit and rid_filter is None:
        recs = recs[:limit]
    if not recs:
        print("[check-only] 没有已生成的产物可检查")
        return 1

    n = len(recs)
    pop = {s["name"]: 0 for s in section_spec.SECTIONS}
    for f in recs:
        try:
            rec = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        for s in section_spec.SECTIONS:
            name = s["name"]
            if _is_populated(name, rec.get(name, {})):
                pop[name] += 1

    print("=== 冒烟校验（检查已有 %d 条产物）===" % n)
    print("%-16s %8s %8s %8s" % ("板块", "填充", "总数", "填充率"))
    failed = []
    for s in section_spec.SECTIONS:
        name = s["name"]
        rate = pop[name] / n if n else 0
        flag = ""
        if name in CRITICAL and rate < min_rate:
            flag = "  ← FAIL（关键板块低于 %.0f%%）" % (min_rate * 100)
            failed.append(name)
        elif rate < min_rate:
            flag = "  ← 偏低"
        print("%-16s %8d %8d %7.1f%%%s" % (name, pop[name], n, rate * 100, flag))

    print()
    if failed:
        print("结果: FAIL —— 关键板块 %s 填充率不足，不要放大到 100 条，先排查 prompt/输入。" % failed)
        return 1
    print("结果: PASS —— 关键板块填充率达标，可放大。")
    return 0

def run_and_check(args):
    is_deepseek = "deepseek" in args.base_url.lower()
    is_claude = "claude" in args.model.lower()
    backend = R.OpenAIBackend(args.base_url, args.api_key, args.model,
                              is_deepseek=is_deepseek, is_claude=is_claude)
    recs = R.load_input()[: args.limit]
    rids = []
    for r in recs:
        R.process_record(r, backend, False, args.section_workers)
        # 合并该条并【落盘】—— assemble() 仅返回 dict 不写文件，必须显式 dump，
        # 否则下游校验会误读旧跑（如空诊断）的陈旧合并产物，造成假阴性。
        merged = A.assemble(r["record_id"])
        out = os.path.join(A.AGENT_ROOT, r["record_id"], "%s.json" % r["record_id"])
        with open(out, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        rids.append(r["record_id"])
    # 只校验本次实际跑过的记录，避免误读旧跑陈余产物
    return check_existing(args.limit, args.min_rate, rid_filter=set(rids))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true", help="只检查已有产物，不调 LLM")
    ap.add_argument("--check-rids", default="", help="逗号分隔的 record_id 列表，仅校验这些记录（配合 --check-only，避免误读旧跑产物）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-rate", type=float, default=0.8, help="关键板块最低填充率阈值")
    ap.add_argument("--backend", default="openai")
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--section-workers", type=int, default=9)
    a = ap.parse_args()

    rid_filter = set(x for x in a.check_rids.split(",") if x) if a.check_rids else None
    if a.check_only:
        sys.exit(check_existing(a.limit, a.min_rate, rid_filter=rid_filter))
    sys.exit(run_and_check(a))

if __name__ == "__main__":
    main()
