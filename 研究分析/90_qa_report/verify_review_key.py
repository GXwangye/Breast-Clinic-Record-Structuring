#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import json
from collections import defaultdict

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
PKG = DATA + "/qa_reports/审核包_20261112"
TAR = DATA + "/中间产物/_packed/assembled_v7_final.tar"
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "00_core"))
sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本", "05_agents"))
sys.path.insert(0, os.path.join(ROOT, "研究分析", "10_analysis"))
sys.path.insert(0, os.path.join(ROOT, "研究分析"))
from tarstream import iter_records                                     # noqa: E402
from describe_structured_cohort import classify_subtype                # noqa: E402
from probe_granularity import norm_stage                               # noqa: E402

KEY = {i["样本号"]: i for i in json.load(open(PKG + "/_答案键.json", encoding="utf-8"))["items"]}

def stages_of(tnm):
    out = []
    if isinstance(tnm, dict):
        if tnm.get("stage"):
            out.append(str(tnm["stage"]))
    elif isinstance(tnm, list):
        for t in tnm:
            if isinstance(t, dict) and t.get("stage"):
                out.append(str(t["stage"]))
    return out

def main():
    want = defaultdict(set)
    for sid, it in KEY.items():
        want[it["记录ID"]].add(sid)

    got = {}
    for name, rec in iter_records(TAR):
        rid = str(rec.get("record_id") or name.split(".")[0])
        if rid not in want:
            continue
        orders = (rec.get("orders") or {}).get("items") or []
        kinds = defaultdict(list)
        for it in orders:
            if isinstance(it, dict):
                kinds[str(it.get("name") or "").strip()].append(str(it.get("kind") or ""))

        calls = defaultdict(list)
        panels = ((rec.get("auxiliary_exam") or {}).get("ihc") or {}).get("panels") or []
        subtypes = []
        pick_ki67 = None
        for p in panels:
            if not isinstance(p, dict):
                continue
            for m in p.get("markers") or []:
                if isinstance(m, dict):
                    nm = str(m.get("marker_name") or m.get("name") or "").strip()
                    calls[nm].append(str(m.get("call") or "") + ("|" + str(m.get("percent")) if m.get("percent") is not None else ""))
            try:
                st = classify_subtype(p)
            except Exception:
                st = ("ERR", None)
            if st[0] and pick_ki67 is None:
                for m in p.get("markers") or []:
                    if isinstance(m, dict) and str(m.get("marker_name") or "").replace("-", "").upper().startswith("KI"):
                        pick_ki67 = m.get("percent")
            subtypes.append(st[0] if isinstance(st, tuple) else st)

        dstages = []
        for d in (rec.get("diagnoses") or {}).get("items") or []:
            if isinstance(d, dict):
                dstages.extend(stages_of(d.get("tnm")))

        got[rid] = {"kinds": {k: v for k, v in kinds.items()},
                    "calls": {k: v for k, v in calls.items()},
                    "subtypes": subtypes,
                    "pick_ki67": pick_ki67,
                    "stages": dstages}
        if len(got) == len(want):
            break

    out = {}
    for rid, sids in want.items():
        g = got.get(rid) or {}
        for sid in sids:
            it = KEY[sid]
            pref = sid.split("-")[0]
            obj = str(it.get("待判对象") or "").strip()
            if pref == "K":
                vals = [v for v in (g.get("kinds") or {}).get(obj) or []]
            elif pref == "C":
                nm = obj
                cand = [k for k in (g.get("calls") or {}) if k.strip().lower() == nm.lower()
                        or k.strip().replace("-", "").lower() == nm.replace("-", "").lower()]
                vals = []
                for k in cand:
                    vals.extend((g.get("calls") or {})[k])
            elif pref == "S":
                vals = list(g.get("stages") or [])
            elif pref == "M":
                vals = [s for s in (g.get("subtypes") or [])]
            else:
                vals = []
            uniq = sorted({str(v) for v in vals if str(v) != ""})
            out[sid] = {"取值集合": uniq, "出现次数": len(vals), "多值": len(uniq) > 1,
                        "产物值": it["产物值"],
                        "所判panel_Ki67": (got.get(rid) or {}).get("pick_ki67")}

    n_multi = sum(1 for v in out.values() if v["多值"])
    by = defaultdict(lambda: [0, 0])
    for sid, v in out.items():
        p = sid.split("-")[0]
        by[p][0] += 1
        by[p][1] += 1 if v["多值"] else 0
    json.dump({"多值样本数": n_multi, "分字段": {k: {"样本": a, "多值": b} for k, (a, b) in by.items()},
               "明细": out}, open(DATA + "/qa_reports/review_key_check.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("多值样本数", n_multi, "/", len(out))
    for k, (a, b) in sorted(by.items()):
        print(f"  {k}: {b}/{a} 多值")
    return 0

if __name__ == "__main__":
    sys.exit(main())
