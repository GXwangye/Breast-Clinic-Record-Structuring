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

from tarstream import iter_records                    # noqa: E402
import normalize_ihc as N                             # noqa: E402
import ihc_standard as IHC                            # noqa: E402

INPUT = ROOT + "/publish/03_抽样/数据/input_records_100.jsonl"
TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
DIRS = {
    "L1_无标准化": ROOT + "/publish/05_消融与基线评测/results/l1_no_standardize",
    "L2_无分板块": ROOT + "/publish/05_消融与基线评测/results/l2_no_split",
    "裸LLM": ROOT + "/publish/02_结构化结果与过程/中间产物/ablation_naive",
}
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/IHC准确性_校准后.json"
FIELDS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]

# ---------------------------------------------------------------- 规格常量
STATUS_GENERAL = {"阳性", "阴性", "不确定", "未提及"}
STATUS_KI67 = {"不适用", "未提及"}
STATUS_LEGACY = {"阳", "阴"}
INTENSITY_OK = {"强", "中", "弱", "", None}
SCORE_OK = re.compile(r"^[0-3]\+?$|^\+{1,3}$")

def norm(s):
    s = str(s or "").translate(str.maketrans("０１２３４５６７８９％＋－（）", "0123456789%+-()"))
    return re.sub(r"[\s,，、;；。．\.]+", "", s)

def mname(m):
    for k in ("marker_name", "name", "marker", "symbol"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def mraw(m):
    for k in ("raw_value", "raw", "value_text"):
        v = m.get(k)
        if isinstance(v, str):
            return v
    return ""

def num(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except Exception:
        return None

def judge(m, src_norm):
    dims = {}
    name = mname(m)
    canon, bucket = N.canon_name(name)
    dims["A"] = bucket in ("marker", "ish")

    rv = norm(mraw(m))
    dims["B"] = (rv in src_norm) if rv else None

    st = m.get("status")
    if canon == "Ki-67" or name.upper().replace("-", "").startswith("KI67"):
        st_ok = st in STATUS_KI67
    else:
        st_ok = st in STATUS_GENERAL
    p_ok = True
    p = m.get("percent")
    if p not in (None, "", "null"):
        pv = num(p)
        p_ok = pv is not None and 0 <= pv <= 100
    s = m.get("score")
    sc_ok = True if s in (None, "", "null") else bool(SCORE_OK.match(str(s).strip()))
    in_ok = m.get("intensity") in INTENSITY_OK
    dims["C"] = bool(st_ok and p_ok and sc_ok and in_ok)

    # D 判读：仅对照组用；Ki-67 比 percent 数值，HER2 比 score 档位，其余比 status
    if not rv or not dims["A"]:
        dims["D"] = None
    else:
        res = IHC.parse_ihc(mraw(m), canon)
        if canon == "Ki-67":
            exp_p = res.get("value")
            got_p = num(p)
            dims["D"] = (exp_p is None) or (got_p is not None and abs(got_p - exp_p) < 0.6)
        else:
            got = {"阳": "阳性", "阴": "阴性", "不确定": "不确定"}.get(res.get("status"), None)
            dims["D"] = (got is None) or (st == got)

    info = {"name": name, "canon": canon, "bucket": bucket, "raw": mraw(m)[:40],
            "status": st, "percent": m.get("percent"), "score": m.get("score")}
    return dims, info

# ---------------------------------------------------------------- 正控 / 负控
def calibration_check():
    src = "患者女性ER(90%强+)PR(80%强+)HER23+Ki-67(约5%+)CK5/6(阴性)P63(-)EGFR(-)"
    src_n = norm(src)
    pos = [
        {"marker_name": "ER", "status": "阳性", "percent": 90.0, "score": None,
         "intensity": "强", "raw_value": "90%强+"},
        {"marker_name": "HER2", "status": "阳性", "percent": None, "score": "3+",
         "intensity": None, "raw_value": "3+"},
        {"marker_name": "Ki-67", "status": "不适用", "percent": 5.0, "score": None,
         "intensity": None, "raw_value": "约5%+"},
    ]
    neg = [
        ({"marker_name": "Luminal A型", "status": "阳性", "percent": 80.0,
          "raw_value": "90%强+"}, "A"),
        ({"marker_name": "P63", "status": "很阳性", "percent": None,
          "raw_value": "-"}, "C"),
        ({"marker_name": "EGFR", "status": "阴性", "percent": 150.0,
          "raw_value": "-"}, "C"),
    ]
    log = {"正控": [], "负控": [], "结论": ""}
    ok = True
    for m in pos:
        dims, info = judge(m, src_n)
        passed = all(dims[k] is True for k in ("A", "B", "C"))
        log["正控"].append({"name": info["name"], "dims": dims, "通过": passed})
        if not passed:
            ok = False
    for m, must_fail in neg:
        dims, info = judge(m, src_n)
        caught = dims.get(must_fail) is False
        log["负控"].append({"name": info["name"], "dims": dims,
                            "应挂维度": must_fail, "抓住": caught})
        if not caught:
            ok = False
    log["结论"] = "判据可用" if ok else "判据不可用"
    return ok, log

def get_markers(rec):
    ae = rec.get("auxiliary_exam")
    if not isinstance(ae, dict):
        return []
    ih = ae.get("ihc")
    if not isinstance(ih, dict):
        return []
    out = []
    for pan in (ih.get("panels") or []):
        if not isinstance(pan, dict):
            continue
        for m in (pan.get("markers") or []):
            if isinstance(m, dict):
                out.append(m)
    return out

def load_dir(d):
    o = {}
    for fn in glob.glob(os.path.join(d, "*.json")):
        try:
            o[os.path.basename(fn)[:-5]] = json.load(io.open(fn, encoding="utf-8"))
        except Exception:
            pass
    return o

def main():
    ok, calib = calibration_check()
    print("=== 校准 ===")
    print(json.dumps(calib, ensure_ascii=False))
    if not ok:
        print("判据不可用，终止，不产出数字。")
        return

    inp = {}
    for line in io.open(INPUT, encoding="utf-8"):
        r = json.loads(line)
        inp[r["record_id"]] = r
    rids = sorted(inp)

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

    res = {"校准": calib, "样本": len(rids), "分组": {}}
    for tag, store in groups.items():
        n = 0
        cnt = Counter()
        b_na = 0
        d_na = 0
        legacy = 0
        p_nonnum = 0
        buckets = Counter()
        bad = []
        corr = 0
        for rid in rids:
            rec = store.get(rid)
            if not isinstance(rec, dict):
                continue
            src = norm(" ".join(str((inp[rid].get("fields") or {}).get(k) or "") for k in FIELDS))
            for m in get_markers(rec):
                n += 1
                dims, info = judge(m, src)
                for k, v in dims.items():
                    if v is True:
                        cnt[k] += 1
                if dims["B"] is None:
                    b_na += 1
                if dims["D"] is None:
                    d_na += 1
                if info["status"] in STATUS_LEGACY:
                    legacy += 1
                if m.get("percent") not in (None, "", "null") and num(m.get("percent")) is None:
                    p_nonnum += 1
                if info["bucket"] not in ("marker", "ish"):
                    buckets[info["bucket"]] += 1
                    if len(bad) < 10:
                        bad.append({"record_id": rid, "name": info["name"],
                                    "bucket": info["bucket"], "status": info["status"]})
                if dims["A"] and dims["C"] and dims["B"] is not False:
                    corr += 1
        n_rec = len([r for r in rids if r in store])
        b_den = n - b_na
        res["分组"][tag] = {
            "有产物记录": n_rec, "marker总数": n,
            "A_名称合法率": round(100.0 * cnt["A"] / n, 1) if n else None,
            "B_原文可溯率": round(100.0 * cnt["B"] / b_den, 1) if b_den else None,
            "B_分母": b_den, "B_无raw_value条数": b_na,
            "C_值域合规率": round(100.0 * cnt["C"] / n, 1) if n else None,
            "D_判读合规率": round(100.0 * cnt["D"] / (n - d_na), 1) if (n - d_na) else None,
            "D_分母": n - d_na,
            "A且C且非B假": corr,
            "准确率_三选二": round(100.0 * corr / n, 1) if n else None,
            "每记录有效marker": round(corr / n_rec, 2) if n_rec else None,
            "旧态阳阴条数": legacy, "percent非数值条数": p_nonnum,
            "非标志物桶": dict(buckets), "非标志物例": bad,
        }

    io.open(OUT, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    print("\n=== 四组 ===")
    print("%-12s %7s %7s %8s %8s %8s %8s %9s" % ("组", "marker", "A名", "B溯", "C域", "D判", "有效mk", "有效/记录"))
    for tag in ("完整工作流", "L1_无标准化", "L2_无分板块", "裸LLM"):
        s = res["分组"].get(tag)
        if not s:
            continue
        f = lambda v: "-" if v is None else "%.1f" % v
        print("%-12s %7d %7s %8s %8s %8s %8d %9s" % (
            tag, s["marker总数"], f(s["A_名称合法率"]), f(s["B_原文可溯率"]),
            f(s["C_值域合规率"]), f(s["D_判读合规率"]),
            s["A且C且非B假"], s["每记录有效marker"]))
    print("\n非标志物桶:", {k: v["非标志物桶"] for k, v in res["分组"].items()})
    print("旧态阳阴:", {k: v["旧态阳阴条数"] for k, v in res["分组"].items()})
    print("->", OUT)

if __name__ == "__main__":
    main()
