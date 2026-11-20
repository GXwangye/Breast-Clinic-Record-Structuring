
import argparse
import glob
import json
import os

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
AGENT_ROOT = DATA + "/中间产物/agents_v7"

TOP_ORDER = ["record_id", "row_index", "source_file", "patient", "visit",
             "chief_complaint", "present_illness", "past_history", "physical_exam",
             "auxiliary_exam", "diagnoses", "treatment", "orders", "qc", "meta"]

def assemble(record_id):
    d = os.path.join(AGENT_ROOT, record_id)
    files = sorted(glob.glob(os.path.join(d, "*.json")))
    files = [f for f in files if os.path.basename(f) != "%s.json" % record_id]

    merged = {"record_id": record_id, "parts": []}
    for f in files:
        with open(f, encoding="utf-8") as fh:
            part = json.load(fh)
        name = os.path.basename(f)
        merged["parts"].append(name)

        if not isinstance(part, dict):
            # 板块文件偶发非 dict（模型产出结构性异常 / 截断残留），跳过该文件，
            # 不拖垮整条记录；缺失由填充率 gate 暴露。
            print("  [assemble skip] %s 非 dict，已跳过" % name)
            continue
        for k, v in part.items():
            if k in ("record_id", "field", "row_index", "source_file", "note"):
                continue
            if k == "qc":
                # qc 各板块合并：列表拼接，键取并集
                if not isinstance(v, dict):
                    merged.setdefault("qc", {})["_bad_type"] = type(v).__name__
                    continue
                cur = merged.setdefault("qc", {"missing": [], "low_conf": [], "conflicts": [], "review_status": "未核"})
                for kk, vv in v.items():
                    if isinstance(vv, list):
                        bucket = cur.setdefault(kk, [])
                        if not isinstance(bucket, list):
                            cur[kk] = bucket = []
                        bucket.extend(vv)
                    else:
                        cur[kk] = vv
            elif k == "auxiliary_exam":
                if not isinstance(v, dict):
                    merged["auxiliary_exam"] = {"_bad_type": type(v).__name__}
                    continue
                aux = merged.setdefault("auxiliary_exam",
                                        {"report_groups": [], "reports": [], "labs": [],
                                         "ihc": None, "excluded_text": [], "summary": None})
                for kk, vv in v.items():
                    if isinstance(vv, list):
                        bucket = aux.setdefault(kk, [])
                        if not isinstance(bucket, list):
                            aux[kk] = bucket = []
                        bucket.extend(vv)
                    elif vv is not None:
                        aux[kk] = vv
            else:
                merged[k] = v

    # 报告单元按 section_seq 重排，labs 按出现顺序
    aux = merged.get("auxiliary_exam")
    if aux:
        reps = aux.get("reports")
        if isinstance(reps, list):
            def _seq(r):
                v = r.get("section_seq")
                return v if isinstance(v, (int, float)) else 0
            aux["reports"] = sorted([r for r in reps if isinstance(r, dict)],
                                    key=_seq)

    out = {}
    for k in TOP_ORDER:
        if k in merged:
            out[k] = merged[k]
    for k in merged:
        if k not in out and k != "parts":
            out[k] = merged[k]
    out["meta"] = dict(out.get("meta", {}))
    out["meta"]["assembled_from"] = merged["parts"]
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("record_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if a.all:
        ids = [os.path.basename(p) for p in glob.glob(AGENT_ROOT + "/*") if os.path.isdir(p)]
    else:
        ids = [a.record_id]

    n_ok = 0
    n_err = 0
    for rid in ids:
        try:
            rec = assemble(rid)
        except Exception as e:
            n_err += 1
            print("  [assemble ERROR] %s: %s" % (rid, e))
            continue
        out = os.path.join(AGENT_ROOT, rid, "%s.json" % rid)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        aux = rec.get("auxiliary_exam", {})
        print("%s → %s" % (rid, os.path.basename(out)))
        print("   板块: %s" % ", ".join(k for k in rec if k not in ("record_id", "meta", "qc")))
        print("   报告单元 %d ｜ 检验组合 %d ｜ 诊断 %d ｜ 医嘱项 %d"
              % (len(aux.get("reports", [])), len(aux.get("labs", [])),
                 len(rec.get("diagnoses", {}).get("items", [])),
                 len(rec.get("orders", {}).get("items", []))))
        n_ok += 1
    print("=== assemble 完成：成功 %d ｜ 失败 %d ===" % (n_ok, n_err))

if __name__ == "__main__":
    main()
