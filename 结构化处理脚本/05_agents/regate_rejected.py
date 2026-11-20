# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")

import normalize_canon as nc                                        # noqa: E402

NORM = DATA + "/中间产物/_norm"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="", help="只处理某一个域，默认全部")
    ap.add_argument("--apply", action="store_true", help="真写回（默认只报告）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.apply:
        a.dry_run = True

    doms = [a.domain] if a.domain else nc.DOMAINS
    total = collections.Counter()
    for dom in doms:
        out_p = os.path.join(NORM, "canon_%s.jsonl" % dom)
        if not os.path.exists(out_p):
            continue
        blocks = dict(nc.prep_blocks(dom))
        rows = nc.load_done(out_p)
        fix, still, skip, same = [], 0, 0, 0
        for k, r in rows.items():
            if r.get("canonical") or r.get("noise"):
                continue
            raw = (r.get("raw_canonical") or "").strip()
            if not raw:
                skip += 1               # 模型未返回，只能重跑 LLM
                continue
            if k not in blocks:
                skip += 1
                continue
            ans = {"canonical": raw, "code": r.get("raw_code") or "",
                   "reason": r.get("reason") or ""}
            new, ok = nc.gate_row(dom, k, blocks[k], ans)
            if ok and new.get("canonical"):
                fix.append(new)
            else:
                still += 1
                if new.get("gate") == r.get("gate"):
                    same += 1           # 原因没变，不必写回
                    continue
                fix.append(new)         # 作废原因变了也要记（错误记录跟着更新）
        print("[%s] 作废 %d：可救回 %d　仍作废 %d（原因未变 %d）　需重跑 LLM %d"
              % (dom, sum(1 for r in rows.values()
                          if not r.get("canonical") and not r.get("noise")),
                 sum(1 for x in fix if x.get("canonical")), still, same, skip))
        total["fix"] += sum(1 for x in fix if x.get("canonical"))
        total["still"] += still
        total["skip"] += skip
        if a.apply and fix:
            with open(out_p, "a", encoding="utf-8") as fh:
                for row in fix:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print("        已追加 %d 行 → %s" % (len(fix), out_p))

    print("\n合计：救回 %d　仍作废 %d　需重跑 LLM %d%s"
          % (total["fix"], total["still"], total["skip"],
             "" if a.apply else "　（dry-run，未写回）"))

if __name__ == "__main__":
    main()
