# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
QA_OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports"
DEFAULT_TAR = PACKED + "/assembled_v7_final.tar"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
from tarstream import iter_records  # noqa: E402
from validate import validate_record  # noqa: E402

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return ""
    return str(v).strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", default=DEFAULT_TAR)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample", type=int, default=400, help="抽样明细条数")
    ap.add_argument("--json-out", default=QA_OUT + "/两条缺陷_实证.json")
    a = ap.parse_args()

    C = collections.Counter()
    vac = collections.defaultdict(collections.Counter)     # 值域：key -> value 计数
    vac_rec = collections.defaultdict(set)                 # 值域：key -> record_id 集合
    vac_ex = collections.defaultdict(list)                 # 值域：key -> 样本
    samples = []                                           # 空日期样本
    seen_rid = set()
    rt_dist = collections.Counter()                        # 空日期报告的 report_type
    en_dist = collections.Counter()                        # 空日期报告的 exam_name

    for name, rec in iter_records(a.tar, limit=a.limit):
        rid = _s(rec.get("record_id")) or name.replace(".json", "")
        ae = rec.get("auxiliary_exam") or {}

        # ---------- 缺陷一：空日期无标记 ----------
        for rep in (ae.get("reports") or []):
            if not isinstance(rep, dict):
                continue
            if not _s(rep.get("exam_date")) and not rep.get("exam_date_absent"):
                C["报告_空且无标记"] += 1
                if rep.get("exam_date_raw"):
                    C["报告_空无标记_但有raw"] += 1
                if _s(rep.get("source_field")):
                    C["报告_空无标记_有source_field"] += 1
                rt_dist[_s(rep.get("report_type")) or "(空)"] += 1
                en = _s(rep.get("exam_name")) or _s(rep.get("exam_name_alt"))
                if en:
                    en_dist[en] += 1
                if len(samples) < a.sample and rid not in seen_rid:
                    seen_rid.add(rid)
                    samples.append({
                        "record_id": rid, "kind": "report",
                        "exam_name": _s(rep.get("exam_name")),
                        "exam_name_alt": _s(rep.get("exam_name_alt")),
                        "report_type": _s(rep.get("report_type")),
                        "source_field": _s(rep.get("source_field")),
                        "exam_date": _s(rep.get("exam_date")),
                        "exam_date_absent": bool(rep.get("exam_date_absent")),
                    })
            elif not _s(rep.get("exam_date")):
                C["报告_空且有标记"] += 1
            else:
                C["报告_有日期"] += 1

        for lab in (ae.get("labs") or []):
            if not isinstance(lab, dict):
                continue
            if not _s(lab.get("exam_date")) and not lab.get("exam_date_absent"):
                C["检验_空且无标记"] += 1
                if lab.get("exam_date_raw"):
                    C["检验_空无标记_但有raw"] += 1
                if _s(lab.get("source_field")):
                    C["检验_空无标记_有source_field"] += 1
            elif not _s(lab.get("exam_date")):
                C["检验_空且有标记"] += 1
            else:
                C["检验_有日期"] += 1

        # ---------- 缺陷二：值域越界 ----------
        try:
            rep_chk = validate_record(rec, record_id=rid)
        except Exception as e:  # noqa: BLE001
            C["校验异常"] += 1
            if len(vac_ex["__error__"]) < 5:
                vac_ex["__error__"].append({"record_id": rid, "err": repr(e)[:200]})
            continue
        for v in (rep_chk.get("vocab") or []):
            key = _s(v.get("key"))
            val = _s(v.get("value"))
            C["值域越界_处数"] += 1
            vac[key][val] += 1
            vac_rec[key].add(rid)
            if len(vac_ex[key]) < 8:
                vac_ex[key].append({"record_id": rid, "at": _s(v.get("at")), "value": val})

    out = {
        "tar": a.tar,
        "缺陷一_空日期无标记": {
            "_口径": "条目级；值为空且 exam_date_absent 非 true 即计入",
            "报告": {k[3:]: v for k, v in C.items() if k.startswith("报告_")},
            "检验": {k[3:]: v for k, v in C.items() if k.startswith("检验_")},
            "合计无标记条目": C["报告_空且无标记"] + C["检验_空且无标记"],
            "空日期报告_report_type分布": dict(rt_dist.most_common(20)),
            "空日期报告_exam_name_top": dict(en_dist.most_common(20)),
            "抽样明细": samples,
        },
        "缺陷二_值域越界": {
            "_口径": "validate_record 的 vocab 出口；记录数按 record_id 去重",
            "处数": C["值域越界_处数"],
            "校验异常记录": C["校验异常"],
            "按字段": {
                k: {
                    "处数": sum(vac[k].values()),
                    "记录数": len(vac_rec[k]),
                    "取值分布": dict(vac[k].most_common(12)),
                    "样本": vac_ex[k],
                }
                for k in sorted(vac, key=lambda x: -sum(vac[x].values()))
            },
        },
    }
    if vac_ex["__error__"]:
        out["缺陷二_值域越界"]["异常样本"] = vac_ex["__error__"]

    os.makedirs(os.path.dirname(a.json_out), exist_ok=True)
    with open(a.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print("== 缺陷一 空日期无标记 ==")
    print("  报告 空且无标记 %d / 空且有标记 %d / 有日期 %d"
          % (C["报告_空且无标记"], C["报告_空且有标记"], C["报告_有日期"]))
    print("  检验 空且无标记 %d / 空且有标记 %d / 有日期 %d"
          % (C["检验_空且无标记"], C["检验_空且有标记"], C["检验_有日期"]))
    print("  合计 %d 条" % (C["报告_空且无标记"] + C["检验_空且无标记"]))
    print("  source_field 非空：报告 %d / 检验 %d"
          % (C["报告_空无标记_有source_field"], C["检验_空无标记_有source_field"]))
    print("  空日期报告 report_type 分布:", dict(rt_dist.most_common(10)))
    print("  空日期报告 exam_name top:", dict(en_dist.most_common(10)))
    print("\n== 缺陷二 值域越界（处数 %d）==" % C["值域越界_处数"])
    for k in sorted(vac, key=lambda x: -sum(vac[x].values())):
        print("  [%s] 处数 %d / 记录 %d" % (k, sum(vac[k].values()), len(vac_rec[k])))
        for val, n in vac[k].most_common(6):
            print("      %-24s %d" % (repr(val)[:24], n))
    print("\nwrote " + a.json_out)

if __name__ == "__main__":
    main()
