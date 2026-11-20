# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"
OUT = NORM + "/step5"
DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "drug"]

# 归一键的构造：只做**同义改写**，不做语义压缩
SYN = [("乳房", "乳腺")]
DROP = ["恶性肿瘤", "癌"]      # 癌字有无同义：乳腺手术 = 乳腺癌手术

# 术式域专用同义：乳腺手术里「前哨淋巴结活检」默认就是腋窝前哨，写不写「腋窝」是同一台手术
# 「腋窝」就丢了部位，那是另一个意思。
SURG_SYN = [(r"腋[窝下]前哨", "前哨")]
SURG_SYN_DOM = "icd9_surgery"

def surg_fix(s, dom):
    if dom == SURG_SYN_DOM:
        for pat, rep in SURG_SYN:
            s = re.sub(pat, rep, s)
    return s

def gkey(s, dom=None):
    s = s.replace("乳房", "乳腺")
    s = surg_fix(s, dom)
    had_ca = ("癌" in s) or ("恶性肿瘤" in s)
    for w in DROP:
        s = s.replace(w, "")
    s = re.sub(r"[\s·\-—_/、,，。；;:：]", "", s)
    if not had_ca and len(s) <= 3:
        s += "\x00SITE"
    return s

def formal(s, dom=None):
    s = s.replace("乳房", "乳腺")
    s = s.replace("乳腺恶性肿瘤", "乳腺癌")
    s = s.replace("恶性肿瘤", "癌")
    return surg_fix(s, dom)

def load_dom(dom):
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--top", type=int, default=12, help="打印多少个大组")
    a = ap.parse_args()
    doms = [a.domain] if a.domain else DOMAINS

    os.makedirs(OUT, exist_ok=True)
    TOT = collections.Counter()
    for dom in doms:
        rows = load_dom(dom)
        ok = {k: r for k, r in rows.items() if r.get("canonical")}
        if not ok:
            continue
        # 标签 → (总次数, 块数, 代表码)
        agg = collections.defaultdict(lambda: [0, 0, None])
        for r in ok.values():
            e = agg[r["canonical"]]
            e[0] += r.get("n") or 0
            e[1] += 1
            if not e[2] and r.get("code"):
                e[2] = (r["code"], r.get("kb_name"))
        groups = collections.defaultdict(list)
        for c in agg:
            groups[gkey(c, dom)].append(c)

        multi = {g: cs for g, cs in groups.items() if len(cs) > 1}
        writes = []
        for g, cs in multi.items():
            cs.sort(key=lambda c: -agg[c][0])          # 次数最高在前
            base = cs[0]
            # 规范名**永远**取 formal(base)。不能因为 formal(base) 恰好是组内另一个
            # 低频标签就退回 base —— 那会让「乳房结节」21,720 次压过「乳腺结节」6 次，
            canon = formal(base, dom)
            # 码优先取规范标签自身的（它才是规范写法），没有再退回 base
            pick = agg[canon][2] if canon in agg else None
            code, kbname = pick or agg[base][2] or (None, None)
            for c in cs:
                # 注意：不能因为 `formal(c) == canon` 就跳过 —— 那正是「乳房」版要并进
                # 「乳腺」版的情况，跳过会导致 2,380 次 NSM 仍挂在含「乳房」的标签上。
                if c == canon:
                    continue
                for k, r in ok.items():
                    if r["canonical"] != c:
                        continue
                    writes.append({
                        "key": k, "canonical": canon, "code": r.get("code") or code,
                        "kb_name": r.get("kb_name") or kbname,
                        "source": "step5_merge", "from": c, "group": g,
                    })
            # 标签本身要改写法（乳房→乳腺）但组内只有它一个
            if canon != base:
                for k, r in ok.items():
                    if r["canonical"] == base:
                        writes.append({"key": k, "canonical": canon,
                                       "code": r.get("code") or code,
                                       "kb_name": r.get("kb_name") or kbname,
                                       "source": "step5_merge", "from": base, "group": g})
        # 单标签组里含「乳房」的，也要改成规范词
        for g, cs in groups.items():
            if len(cs) == 1 and cs[0] != formal(cs[0], dom):
                c = cs[0]
                for k, r in ok.items():
                    if r["canonical"] == c:
                        writes.append({"key": k, "canonical": formal(c, dom),
                                       "code": r.get("code"), "kb_name": r.get("kb_name"),
                                       "source": "step5_merge", "from": c, "group": g})

        # 去重（同一 key 只留一条）
        ded = {}
        for w in writes:
            ded[w["key"]] = w
        writes = list(ded.values())
        n_canon_before = len(agg)
        # 合并后每组恰好一个规范标签，故后标签数 = 组数（单标签组改写法仍是 1 个）
        n_canon_after = len(groups)
        print("[%s] 标签 %d → %d（-%.1f%%）｜多标签组 %d 个｜改写块 %d"
              % (dom, n_canon_before, n_canon_after,
                 100.0 * (n_canon_before - n_canon_after) / max(1, n_canon_before),
                 len(multi), len(writes)))
        for g, cs in sorted(multi.items(), key=lambda kv: -sum(agg[c][0] for c in kv[1]))[:a.top]:
            top = " ／ ".join("%s %d" % (c, agg[c][0]) for c in cs[:4])
            print("    %-34s %s" % (g[:34], top))
        TOT["before"] += n_canon_before
        TOT["after"] += n_canon_after
        TOT["writes"] += len(writes)
        TOT["groups"] += len(multi)

        if a.apply and writes:
            p = os.path.join(OUT, "canon5_%s.jsonl" % dom)
            with open(p, "w", encoding="utf-8") as f:
                for w in writes:
                    f.write(json.dumps(w, ensure_ascii=False) + "\n")
            print("    已写 %s（%d 行）" % (p, len(writes)))

    print("\n合计：标签 %d → %d（-%.1f%%）｜多标签组 %d｜改写块 %d%s"
          % (TOT["before"], TOT["after"],
             100.0 * (TOT["before"] - TOT["after"]) / max(1, TOT["before"]),
             TOT["groups"], TOT["writes"], "" if a.apply else "　（dry-run）"))

if __name__ == "__main__":
    main()
