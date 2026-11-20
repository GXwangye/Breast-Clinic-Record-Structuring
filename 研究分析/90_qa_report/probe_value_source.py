# -*- coding: utf-8 -*-
import json
import io
import os
import re
import sys
import glob
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")

from tarstream import iter_records        # noqa: E402
import normalize_ihc as N                 # noqa: E402
import ihc_standard as IHC                # noqa: E402

INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
DIRS = {
    "L1_无标准化": ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize",
    "L2_无分板块": ROOT + "/publish/05_消融与基线评测/results/l2_no_split",
}
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC值源一致.json"
FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]

def variants(canon):
    c = canon.strip()
    v = {c, c.replace("-", ""), c.replace("/", ""), c.replace("-", "").replace("/", "")}
    if c.startswith("HER2"):
        v |= {"HER2", "HER-2", "C-erbB-2", "HER2neu"}
    if c == "Ki-67":
        v |= {"Ki-67", "Ki67", "KI-67", "MIB-1"}
    if c == "E-cadherin":
        v |= {"E-Ca", "E-ca", "E-cad"}
    if c == "P53":
        v |= {"P53", "p53"}
    return {x for x in v if x}

def locate(src, canon):
    segs = []
    name_hit = False
    for v in variants(canon):
        ev = re.escape(v)
        if re.search(ev, src, re.I):
            name_hit = True
        for m in re.finditer(ev + r"\s*[*＊#]?\s*[（(]\s*([^）)]{0,60}?)\s*[）)]", src, re.I):
            segs.append(m.group(1))
        for m in re.finditer(ev + r"\s*[*＊#]?\s*([-+－＋]|阴性|阳性)(?![0-9])", src, re.I):
            segs.append(m.group(1))
    return segs, name_hit

def num(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except Exception:
        return None

def mname(m):
    for k in ("marker_name", "name", "marker", "symbol"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def get_markers(rec):
    ae = rec.get("auxiliary_exam")
    if not isinstance(ae, dict):
        return []
    ih = ae.get("ihc")
    if not isinstance(ih, dict):
        return []
    r = []
    for pan in (ih.get("panels") or []):
        if isinstance(pan, dict):
            for m in (pan.get("markers") or []):
                if isinstance(m, dict):
                    r.append(m)
    return r

def judge_one(m, src):
    name = mname(m)
    canon, bucket = N.canon_name(name)
    if bucket not in ("marker", "ish"):
        return "非标志物", None
    segs, name_hit = locate(src, canon)
    if not segs:
        return ("共享判读串" if name_hit else "原文无此标志物"), None
    best = None
    any_parsed = False
    for seg in segs:
        res = IHC.parse_ihc(seg, "HER2" if canon.startswith("HER2(") else canon)
        st = {"阳": "阳性", "阴": "阴性", "不确定": "不确定"}.get(res.get("status"), None)
        if canon == "Ki-67":
            exp, got = num(res.get("value")), num(m.get("percent"))
            if exp is None:
                continue
            any_parsed = True
            ok = (got is not None and abs(exp - got) < 0.6)
        else:
            exp, got = st, m.get("status")
            if exp is None:
                continue
            any_parsed = True
            ok = (exp == got)
        if ok:
            return "一致", seg
        if best is None:
            best = (seg, exp, got)
    if not any_parsed:
        return "原文不可判读", segs[0]
    # 值不一致：区分"产物压根没给值"与"给了但判反"
    if canon == "Ki-67":
        missing = num(m.get("percent")) is None
    else:
        missing = m.get("status") in ("未提及", None, "")
    return ("缺值" if missing else "判反"), best

def load_dir(d):
    o = {}
    for fn in glob.glob(os.path.join(d, "*.json")):
        try:
            o[os.path.basename(fn)[:-5]] = json.load(io.open(fn, encoding="utf-8"))
        except Exception:
            pass
    return o

def main():
    inp = {}
    for line in io.open(INPUT, encoding="utf-8"):
        r = json.loads(line)
        inp[r["record_id"]] = r
    rids = sorted(inp)

    # ---- 正控：四个决策型各取一条真实样本，必须"一致"
    pos = []
    for rid in rids:
        f = inp[rid]["fields"]
        s = " ".join(str(f.get(k) or "") for k in FIELDS)
        if re.search(r"ER\s*[*＊#]?\s*[（(]", s, re.I):
            pos.append(rid)
        if len(pos) >= 8:
            break
    print("正控候选记录:", pos[:4])

    wf = {}
    want = set(rids)
    for name, rec in iter_records(TAR):
        b = os.path.basename(name)
        rid = b[:-5] if b.endswith(".json") else b
        if rid in want:
            wf[rid] = rec
            if len(wf) == len(want):
                break
    groups = {"完整工作流": wf}
    for tag, d in DIRS.items():
        groups[tag] = load_dir(d)

    res = {"说明": "一致率 = 一致/(一致+判反+缺值)，分母仅含原文给出独立值的条目；"
                   "共享判读串与原文无此标志物单列，不进一致率",
           "分组": {}}
    for tag, store in groups.items():
        c = Counter()
        samples = {"判反": [], "缺值": [], "原文无此标志物": [], "共享判读串": []}
        for rid in rids:
            rec = store.get(rid)
            if not isinstance(rec, dict):
                continue
            f = inp[rid]["fields"]
            src = " ".join(str(f.get(k) or "") for k in FIELDS)
            for m in get_markers(rec):
                st, info = judge_one(m, src)
                c[st] += 1
                if st in samples and len(samples[st]) < 6:
                    samples[st].append({"record_id": rid, "name": mname(m),
                                        "产物status": m.get("status"),
                                        "产物percent": m.get("percent"),
                                        "原文片段": (info if isinstance(info, str) else
                                                 (info[0] if info else ""))[:60]})
        judged = c["一致"] + c["判反"] + c["缺值"]
        res["分组"][tag] = {
            "marker总数": sum(c.values()),
            "一致": c["一致"], "判反": c["判反"], "缺值": c["缺值"],
            "共享判读串": c["共享判读串"], "原文无此标志物": c["原文无此标志物"],
            "非标志物": c["非标志物"],
            "已判分母": judged,
            "一致率": round(100.0 * c["一致"] / judged, 1) if judged else None,
            "样例": samples,
        }

    io.open(OUT, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    print("\n%-12s %7s %6s %5s %5s %8s %8s %8s %8s" % (
        "组", "marker", "一致", "判反", "缺值", "共享串", "无此物", "一致率", "非标志"))
    for tag in ("完整工作流", "L1_无标准化", "L2_无分板块"):
        s = res["分组"].get(tag)
        if not s:
            continue
        print("%-12s %7d %6d %5d %5d %8d %8d %8s %8d" % (
            tag, s["marker总数"], s["一致"], s["判反"], s["缺值"],
            s["共享判读串"], s["原文无此标志物"], s["一致率"], s["非标志物"]))
    for k in ("判反", "缺值", "原文无此标志物", "共享判读串"):
        print("\n工作流 %s 样例:" % k)
        for x in res["分组"]["完整工作流"]["样例"][k][:6]:
            print("  ", json.dumps(x, ensure_ascii=False))
    print("->", OUT)

if __name__ == "__main__":
    main()
