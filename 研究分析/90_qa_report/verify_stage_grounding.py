#!/usr/bin/env python
import json
import os
import re
import sys
import collections

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records                      # noqa: E402

TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"
SRC = ROOT + "/publish/02_结构化结果与过程/中间产物/_agents_input/input_records.jsonl"
OUT = ROOT + "/publish/02_结构化结果与过程/qa_reports/stage_grounding.json"

FW = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"}
CN_NUM = {"一": "I", "二": "II", "三": "III", "四": "IV"}

# 原文期别字样
LAB = re.compile(r"(Ⅰ|Ⅱ|Ⅲ|Ⅳ|I{1,3}V?|IV)\s*([ABCabc])?\s*期|([一二三四])\s*期")
# 阿拉伯数字期（`1期` `2期`）：契约 §3.6 判为「分期体系不可判定」，产物理应置空，
# 故单列、不进回指分母；原文仅写此形态的 tnm 对象不是「漏抽」也不是「有期别」。
LAB_NUM = re.compile(r"[0-9０-９]\s*期")
FAKE = re.compile(r"(重建|再造|成形|修补|闭式|缝合)")

# 产物期别形态：0 / I / II / III / IV，可带 A/B/C
STAGE_OK = re.compile(r"^(0|IV|I{1,3})[ABC]?$")

# 占位词：这些不是值，按三态显式铁律一律空
UNSPEC = {"", "none", "null", "nan", "未提及", "未指明", "未标注", "未填", "空", "无",
          "-", "—", "/", "∅", "不适用", "未分期", "未定"}

def norm_main(v):
    t = str(v).strip()
    if not t or t.lower() in UNSPEC:
        return ""
    for k, h in FW.items():
        t = t.replace(k, h)
    t = t.replace("期", "").replace(" ", "").upper()
    m = re.match(r"(0|IV|I{1,3})", t)
    return m.group(1) if (m and STAGE_OK.match(t)) else ""

def pick_stage(tnm):
    for k in ("stage_norm", "stage"):
        v = norm_main(tnm.get(k))
        if v:
            return v, k
    return "", ""

def text_stages(text):
    out = set()
    for m in LAB.finditer(text or ""):
        if FAKE.search((text or "")[m.end():m.end() + 4]):
            continue
        if m.group(1):
            raw = m.group(1).replace("Ⅳ", "IV")
            for k, h in FW.items():
                raw = raw.replace(k, h)
            mm = re.match(r"(IV|I{1,3})", raw.upper())
            if mm:
                out.add(mm.group(1))
        else:
            out.add(CN_NUM[m.group(3)])
    return out

def main():
    raw = {}
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            raw[str(r["record_id"])] = " ".join(str(v) for v in (r.get("fields") or {}).values())

    g = collections.Counter()
    no_src, mism = [], []
    rec_flag = {}          # 记录级：{rid: [原文有期别, 该记录是否有任一 tnm 给值]}
    for name, rec in iter_records(TAR):
        rid = str(rec.get("record_id"))
        body = raw.get(rid, "")
        have = text_stages(body)
        only_num = (not have) and bool(LAB_NUM.search(body))
        if have:
            rec_flag.setdefault(rid, [True, False])
        dx = rec.get("diagnoses")
        items = (dx.get("items") if isinstance(dx, dict) else dx) or []
        for it in items:
            if not isinstance(it, dict):
                continue
            tnm = it.get("tnm")
            if not isinstance(tnm, dict):
                continue
            val, _k = pick_stage(tnm)
            g["tnm对象"] += 1
            if val and rid in rec_flag:
                rec_flag[rid][1] = True
            if only_num:
                g["原文仅阿拉伯数字期"] += 1
                g["其中产物仍给值"] += 1 if val else 0
                continue
            if have and val:
                g["①原文有_产物有"] += 1
                if val in have:
                    g["回指命中"] += 1
                else:
                    g["期不符"] += 1
                    if len(mism) < 20:
                        mism.append({"记录ID": rid, "产物": val, "原文均有": sorted(have),
                                     "诊断": str(it.get("name") or "")[:30]})
            elif have and not val:
                g["②原文有_产物空"] += 1
            elif not have and val:
                g["③原文无_产物有"] += 1
                if len(no_src) < 20:
                    no_src.append({"记录ID": rid, "产物": val, "诊断": str(it.get("name") or "")[:30]})
            else:
                g["④原文无_产物空"] += 1

    d = g["①原文有_产物有"]
    rec_have = len(rec_flag)
    rec_got = sum(1 for v in rec_flag.values() if v[1])
    out = {
        "_说明": "stage 值级回指。占位词（None/未提及/未指明）计为无值；不含「多期别记录的归属」判断",
        "_判据": {
            "原文期别": "罗马数字或半角拉丁＋可选亚期＋「期」，或中文数字期；剔术后 4 字内含重建/再造/成形/修补/闭式/缝合的手术分期",
            "产物期别": "0/I/II/III/IV ＋可选 A/B/C，优先 stage_norm 取值以免与 stage 双计",
        },
        "tnm对象": g["tnm对象"],
        "四格": {
            "①原文有_产物有": g["①原文有_产物有"],
            "②原文有_产物空": g["②原文有_产物空"],
            "③原文无_产物有": g["③原文无_产物有"],
            "④原文无_产物空": g["④原文无_产物空"],
        },
        "值级回指率": round(g["回指命中"] / d, 4) if d else None,
        "回指命中": g["回指命中"],
        "期不符": g["期不符"],
        "原文有期别的召回率": round(d / (d + g["②原文有_产物空"]), 4) if (d + g["②原文有_产物空"]) else None,
        "记录级_原文有期别": rec_have,
        "记录级_其中至少一个tnm给值": rec_got,
        "记录级召回": round(rec_got / rec_have, 4) if rec_have else None,
        "无出处_原文无期别却给值": g["③原文无_产物有"],
        "无出处占比_分母为原文无期别": round(g["③原文无_产物有"] /
                                     (g["③原文无_产物有"] + g["④原文无_产物空"]), 4)
        if (g["③原文无_产物有"] + g["④原文无_产物空"]) else None,
        "原文仅阿拉伯数字期_单列不计分母": g["原文仅阿拉伯数字期"],
        "其中产物仍给值": g["其中产物仍给值"],
        "样例_无出处": no_src,
        "样例_期不符": mism,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("tnm 对象 %d" % g["tnm对象"])
    print("四格：原文有·产物有 %d ｜ 原文有·产物空 %d ｜ 原文无·产物有 %d ｜ 原文无·产物空 %d"
          % (g["①原文有_产物有"], g["②原文有_产物空"], g["③原文无_产物有"], g["④原文无_产物空"]))
    print("值级回指 %d/%d = %.2f%%" % (g["回指命中"], d, 100.0 * out["值级回指率"] if d else 0))
    print("期不符 %d（同一记录原文有别的期，产物那期找不到）" % g["期不符"])
    print("召回（对象级，分母含良性诊断的 tnm）%d/%d = %.2f%%"
          % (d, d + g["②原文有_产物空"], 100.0 * (out["原文有期别的召回率"] or 0)))
    print("召回（记录级）%d/%d = %.2f%%"
          % (rec_got, rec_have, 100.0 * (out["记录级召回"] or 0)))
    print("无出处（原文无期别却给值）%d，占原文无期别的 %.2f%%"
          % (g["③原文无_产物有"], 100.0 * (out["无出处占比_分母为原文无期别"] or 0)))
    print("原文仅写阿拉伯数字期（如「1期」，体系不可判）%d 个对象，其中产物仍给值 %d"
          % (g["原文仅阿拉伯数字期"], g["其中产物仍给值"]))
    for b in no_src[:5]:
        print("   无出处", b)
    for b in mism[:5]:
        print("   期不符", b)
    print("-> %s" % OUT)

if __name__ == "__main__":
    main()
