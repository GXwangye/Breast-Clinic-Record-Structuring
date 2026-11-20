# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import subprocess
import sys
import time

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"
CYC = NORM + "/cycles"
PY = sys.executable
DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "drug"]

def _lines(dom):
    p = os.path.join(NORM, "canon_%s.jsonl" % dom)
    if not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as f:
        return sum(1 for _ in f)

def _dedup(dom):
    p = os.path.join(NORM, "canon_%s.jsonl" % dom)
    out = {}
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            out[r["key"]] = r
    return out

def run_domain(dom, budget, args):
    cmd = [PY, ROOT + "/结构化处理脚本/05_agents/normalize_canon.py",
           "--domain", dom, "--resume",
           "--base-url", args.base_url, "--api-key", args.api_key,
           "--model", args.model, "--workers", str(args.workers)]
    if budget:
        cmd += ["--limit", str(budget)]
        if args.retry:
            cmd += ["--retry-rejected"]      # 新块与作废块混跑
    else:
        # 预算 0 = **只重跑错误样本**。这里绝不能走 --retry-rejected：
        # 那个开关的含义是"把作废块也算待判"，未判块照样进队列，
        cmd += ["--only-rejected"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    tail = [x for x in (p.stdout or "").strip().splitlines() if x.strip()][-3:]
    return p.returncode, tail, (p.stderr or "").strip()[-300:]

def stats(dom):
    rows = _dedup(dom)
    if not rows:
        return None
    ok = [r for r in rows.values() if r.get("canonical")]
    noise = [r for r in rows.values() if r.get("noise")]
    bad = [r for r in rows.values() if not r.get("canonical") and not r.get("noise")]
    reasons = collections.Counter((r.get("gate") or "未标原因").split("：")[0] for r in bad)
    canons = collections.Counter(r["canonical"] for r in ok)
    # 收敛比的分母必须是**写法数**，不是折叠块数：
    n_lit = sum(r.get("n_variants") or 1 for r in rows.values())
    return {
        "blocks_judged": len(rows),
        "literals_judged": n_lit,
        "pass": len(ok),
        "reject": len(bad),
        "noise": len(noise),
        "with_code": sum(1 for r in ok if r.get("code")),
        "canonical_unique": len(canons),
        "convergence": round(len(canons) / n_lit, 4) if n_lit else None,
        "reject_reasons": dict(reasons.most_common()),
        "top_group": [[c, n] for c, n in canons.most_common(3)],
        "coverage": sum(r.get("n") or 0 for r in ok),
        "coverage_total": sum(r.get("n") or 0 for r in rows.values()),
    }

def write_cycle(n, args, before, after, per_dom, t0):
    os.makedirs(CYC, exist_ok=True)
    rec = {
        "cycle": n,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "budget_per_domain": args.budget,
        "retry": bool(args.retry),
        "workers": args.workers,
        "elapsed_s": round(time.time() - t0, 1),
        "new_lines": {d: after[d] - before[d] for d in DOMAINS},
        "domains": per_dom,
    }
    params = ["budget=%d/域" % args.budget, "workers=%d" % args.workers,
              "retry=%s" % ("on" if args.retry else "off")]
    with open(os.path.join(CYC, "cycle_%02d.json" % n), "w", encoding="utf-8") as f:
        json.dump({"record": rec, "params": params}, f, ensure_ascii=False, indent=1)

    # 可读版：每轮一张表，方便横向比「这轮比上轮好了没有」
    L = ["# 周期 %02d 记录" % n, "",
         "- 时间：%s　耗时 %.0fs" % (rec["ts"], rec["elapsed_s"]),
         "- 参数：%s" % "　".join(params),
         "- 本轮新增判定行：%s" % "　".join("%s +%d" % (d, v) for d, v in rec["new_lines"].items()), "",
         "| 域 | 已判块 | 写法 | 通过 | 通过率 | 作废 | 噪声 | 带码 | 规范标签数 | 收敛比 | 按次数覆盖 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for d in DOMAINS:
        s = per_dom.get(d)
        if not s:
            continue
        cov = s["coverage"] / s["coverage_total"] if s["coverage_total"] else 0
        L.append("| %s | %d | %d | %d | %.1f%% | %d | %d | %d | %d | %.3f | %.1f%% |"
                 % (d, s["blocks_judged"], s["literals_judged"], s["pass"],
                    100.0 * s["pass"] / s["blocks_judged"], s["reject"], s.get("noise", 0),
                    s["with_code"], s["canonical_unique"], s["convergence"] or 0, 100.0 * cov))
    L.append("")
    for d in DOMAINS:
        s = per_dom.get(d)
        if s and s["reject_reasons"]:
            L.append("- `%s` 作废原因：%s" % (d, s["reject_reasons"]))
    with open(os.path.join(CYC, "cycle_%02d.md" % n), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

def _run_agent(script, extra, tail_n=6):
    cmd = [PY, ROOT + "/结构化处理脚本/05_agents/" + script] + extra
    p = subprocess.run(cmd, capture_output=True, text=True)
    lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
    return p.returncode, lines[-tail_n:], (p.stderr or "").strip()[-300:]

def fold_terms(args):
    return _run_agent("fold_terms.py", [])

def merge_step5(args):
    return _run_agent("merge_step5.py", ["--apply"])

def regate_rejected(args):
    return _run_agent("regate_rejected.py", ["--apply"])

def export(args):
    cmd = [PY, ROOT + "/结构化处理脚本/05_agents/export_mapping.py"]
    if args.rev:
        cmd += ["--rev", args.rev]
    p = subprocess.run(cmd, capture_output=True, text=True)
    lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
    return p.returncode, lines[-6:], (p.stderr or "").strip()[-300:]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=200, help="每域本轮判多少块（0=不判新块，只重跑错误样本）")
    ap.add_argument("--retry", action="store_true", help="把闸门作废的块一并放回本轮")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--export", dest="do_export", action="store_true", default=True)
    ap.add_argument("--no-export", dest="do_export", action="store_false")
    ap.add_argument("--export-only", action="store_true", help="不跑 LLM，只重出映射字典")
    ap.add_argument("--no-fold", action="store_true", help="跳过 ⓪ 折叠（fold 块已是最新时）")
    ap.add_argument("--no-step5", action="store_true", help="跳过 ④ 二次归并")
    ap.add_argument("--no-regate", action="store_true", help="跳过 ⑤ 复判")
    ap.add_argument("--rev", default="v1")
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default="claude-sonnet-4-6")
    a = ap.parse_args()

    if not a.export_only and not a.api_key:
        raise SystemExit("缺 --api-key（或设 OPENAI_API_KEY）")

    os.makedirs(CYC, exist_ok=True)
    n = 1 + len([f for f in os.listdir(CYC) if f.startswith("cycle_") and f.endswith(".json")])
    t0 = time.time()

    if not a.export_only:
        if not a.no_fold:
            print("=== 周期 %02d ⓪ 折叠（写法 → 折叠块）===" % n)
            rc, tail, err = fold_terms(a)
            print(" rc=%d" % rc)
            for x in tail:
                print("   " + x)
            if err:
                print("   !!" + err)
        before = {d: _lines(d) for d in DOMAINS}
        print("=== 周期 %02d ① 判定（预算 %s/域）===" % (n, a.budget or "仅重跑错误样本"))
        for d in DOMAINS:
            rc, tail, err = run_domain(d, a.budget, a)
            print(" [%s] rc=%d %s%s" % (d, rc, " | ".join(tail), ("  !!" + err) if err else ""))
        after = {d: _lines(d) for d in DOMAINS}
        print("\n=== ② 体检 ===")
        per_dom = {}
        for d in DOMAINS:
            s = stats(d)
            per_dom[d] = s
            if not s:
                continue
            cov = s["coverage"] / s["coverage_total"] if s["coverage_total"] else 0
            print(" %-12s 已判块 %6d  写法 %6d  过 %6d (%5.1f%%)  作废 %4d  噪声 %3d  带码 %6d  标签 %6d  收敛比 %.3f  按次数覆盖 %5.1f%%"
                  % (d, s["blocks_judged"], s["literals_judged"], s["pass"],
                     100.0 * s["pass"] / s["blocks_judged"],
                     s["reject"], s.get("noise", 0), s["with_code"], s["canonical_unique"],
                     s["convergence"] or 0, 100.0 * cov))
            if s["reject_reasons"]:
                print("              作废原因：%s" % s["reject_reasons"])
        write_cycle(n, a, before, after, per_dom, t0)
    else:
        per_dom = {d: stats(d) for d in DOMAINS}
        before = after = {d: _lines(d) for d in DOMAINS}

    if not a.export_only and not a.no_step5:
        print("\n=== ④ 二次归并（步 5：跨块同质收敛）===")
        rc, tail, err = merge_step5(a)
        print(" rc=%d" % rc)
        for x in tail:
            print("   " + x)
        if err:
            print("   !!" + err)

    if not a.export_only and not a.no_regate:
        print("\n=== ⑤ 复判（闸门放宽后作废块重过新闸）===")
        rc, tail, err = regate_rejected(a)
        print(" rc=%d" % rc)
        for x in tail:
            print("   " + x)
        if err:
            print("   !!" + err)

    if a.do_export:
        print("\n=== ⑥ 导出映射字典 + 错误记录 ===")
        rc, tail, err = export(a)
        print(" rc=%d" % rc)
        for x in tail:
            print("   " + x)
        if err:
            print("   !!" + err)

    print("\n=== ⑦ 下一轮建议 ===")
    rej = {d: (per_dom.get(d) or {}).get("reject", 0) for d in DOMAINS}
    todo = {d: (per_dom.get(d) or {}).get("blocks_judged", 0) for d in DOMAINS}
    if sum(rej.values()):
        print("  · 有 %d 个作废块：先看 `错误记录/错误汇总` 的 gate_reject 段，成因若是提示不足则改提示，"
              "再 `--budget 0 --retry` 重跑这批（不新判、只重判错误样本）" % sum(rej.values()))
    print("  · 已完成 %d 块 / 全量 50,335 块（%.1f%%）"
          % (sum(todo.values()), 100.0 * sum(todo.values()) / 50335))
    print("  · 周期账：%s" % os.path.join(CYC, "cycle_%02d.json" % n))

if __name__ == "__main__":
    main()
