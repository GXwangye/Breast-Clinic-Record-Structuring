# -*- coding: utf-8 -*-
import argparse
import collections
import io
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
OUT = DATA + "/qa_reports/gate_breakdown.json"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
from tarstream import iter_records          # noqa: E402
from validate import validate_record, rep_ok  # noqa: E402

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", default=TAR)
    ap.add_argument("--show", type=int, default=3)
    a = ap.parse_args()

    n = 0
    passed = 0
    combo = collections.Counter()      # (has_vocab, has_tnm, has_aux, has_blocks)
    vkey = collections.Counter()       # 值域越界的键与值分布
    tkey = collections.Counter()
    only_blocks = []
    multi = []
    per_class = collections.Counter()

    for name, rec in iter_records(a.tar):
        rid = rec.get("record_id") or name.rsplit(".", 1)[0]
        n += 1
        rep = validate_record(rec, rid)
        if rep_ok(rep):
            passed += 1
            continue
        key = (bool(rep["vocab"]), bool(rep["tnm"]), bool(rep["aux"]),
               bool(rep["blocks_missing"]))
        combo[key] += 1
        for it in rep["vocab"]:
            vkey["%s=%s" % (it.get("key"), it.get("value"))] += 1
        for it in rep["tnm"]:
            tkey["%s %s" % (it.get("error"), it.get("value"))] += 1
        nc = sum(1 for x in key[:3] if x)
        if nc == 0:
            per_class["仅板块缺失"] += 1
            if len(only_blocks) < a.show:
                only_blocks.append({"record_id": rid, "missing": rep["blocks_missing"]})
        elif nc >= 2:
            per_class["两项及以上重叠"] += 1
            if len(multi) < a.show:
                multi.append({"record_id": rid,
                              "vocab": rep["vocab"][:2],
                              "tnm": rep["tnm"][:2],
                              "aux": rep["aux"][:2]})
        else:
            per_class["单一类别"] += 1

    fail = n - passed
    vocab_tot = sum(c for k, c in combo.items() if k[0])
    tnm_tot = sum(c for k, c in combo.items() if k[1])
    aux_tot = sum(c for k, c in combo.items() if k[2])
    overlap = per_class["两项及以上重叠"]
    only_b = per_class["仅板块缺失"]

    print("记录 %d  通过 %d  未通过 %d" % (n, passed, fail))
    print("分项：值域越界 %d  TNM %d  辅检 %d  合计 %d" %
          (vocab_tot, tnm_tot, aux_tot, vocab_tot + tnm_tot + aux_tot))
    print("重叠记录 %d  仅板块缺失 %d" % (overlap, only_b))
    print("闭合：分项合计 %d - 重叠 %d + 仅板块缺失 %d = %d（应等于 %d）" %
          (vocab_tot + tnm_tot + aux_tot, overlap, only_b,
           vocab_tot + tnm_tot + aux_tot - overlap + only_b, fail))
    for k, c in sorted(combo.items(), key=lambda x: -x[1]):
        print("  组合 vocab=%d tnm=%d aux=%d blocks=%d → %d 条" % (k[0], k[1], k[2], k[3], c))
    if only_blocks:
        print("仅板块缺失样例：", json.dumps(only_blocks, ensure_ascii=False))
    if multi:
        print("重叠样例：", json.dumps(multi, ensure_ascii=False)[:600])
    print("值域越界 Top：", json.dumps(vkey.most_common(8), ensure_ascii=False))
    print("TNM 越界 Top：", json.dumps(tkey.most_common(8), ensure_ascii=False))

    out = {"记录数": n, "通过": passed, "未通过": fail,
           "值域越界": vocab_tot, "TNM形态问题": tnm_tot, "辅检硬违规": aux_tot,
           "分类重叠记录": overlap, "仅板块缺失": only_b,
           "组合矩阵": [{"vocab": k[0], "tnm": k[1], "aux": k[2], "blocks": k[3], "条数": c}
                        for k, c in sorted(combo.items(), key=lambda x: -x[1])],
           "闭合式": "%d+%d+%d-%d+%d=%d" % (vocab_tot, tnm_tot, aux_tot, overlap, only_b, fail),
           "值域越界键值分布": vkey.most_common(20),
           "TNM越界分布": tkey.most_common(20),
           "tar": os.path.basename(a.tar)}
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print("→", OUT)

if __name__ == "__main__":
    main()
