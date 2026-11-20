#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re

CORE = ("ER", "PR", "HER2", "KI67")
NAME_RE = {
    "ER": re.compile(r"(?<![A-Z])ER\s*\*?\s*", re.I),
    "PR": re.compile(r"(?<![A-Z])PR\s*\*?\s*", re.I),
    "HER2": re.compile(r"(?:HER\s*-?\s*2|CerbB\s*-?\s*2|ERBB2)\s*\*?\s*", re.I),
    "KI67": re.compile(r"KI\s*-?\s*67\s*#?\s*", re.I),
}

def mkey(name):
    n = str(name or "").upper().replace("-", "").replace(" ", "").replace("*", "")
    return n if n in CORE else None

def _payload(seg, m):
    rest = seg[m.end():]
    km = re.match(r"\s*[:：]?\s*[^\s（(]{0,3}?[（(]([^（）()]{0,40})[）)]", rest)
    if km:
        return km.group(1)
    km = re.match(r"\s*[:：]?\s*(-|\+|阴性|阳性|阴|阳)", rest)
    return km.group(1) if km else ""

def polarity(pay, key):
    p = (pay or "").strip()
    if not p:
        return ""
    if key == "HER2":
        rng = re.search(r"([0-3])\s*[-~]\s*([0-3])", p)
        if rng:
            m = {0: "阴性", 1: "阴性", 2: "不确定", 3: "阳性"}
            return "/".join(sorted({m[int(rng.group(1))], m[int(rng.group(2))]}))
        g = re.match(r"([0-3])\s*([-+＋])?", p)
        if g and (g.group(2) or g.group(1) in "0123"):
            return {0: "阴性", 1: "阴性", 2: "不确定", 3: "阳性"}[int(g.group(1))]
    if re.search(r"阳", p) or re.search(r"[+＋]", p):
        return "阳性"
    if re.search(r"阴", p) or re.match(r"^\s*[-－]\s*$", p):
        return "阴性"
    return ""

def vals_of_text(seg):
    out = {}
    for k in CORE:
        for m in NAME_RE[k].finditer(seg or ""):
            pay = _payload(seg, m).strip()
            if not pay:
                continue
            if k == "KI67":
                nums = re.findall(r"\d+(?:\.\d+)?", pay)
                if nums:
                    out.setdefault(k, []).append(float(nums[0]))
            else:
                p = polarity(pay, k)
                if p:
                    out.setdefault(k, []).append(p)
    return out

def blocks_of(text):
    segs = [s.strip() for s in re.split(r"[；;。\n]", text or "") if s.strip()]
    out, cur, curvec = [], [], {}
    for s in segs:
        v = vals_of_text(s)
        conflict = any(k in curvec and any(val != curvec[k][0] for val in v[k])
                       for k in v if k in curvec)
        if conflict and cur:
            out.append({"text": "；".join(cur), "all": curvec})
            cur, curvec = [], {}
        cur.append(s)
        for k, vs in v.items():
            curvec.setdefault(k, [])
            for val in vs:
                if val not in curvec[k]:
                    curvec[k].append(val)
    if cur:
        out.append({"text": "；".join(cur), "all": curvec})
    blocks = []
    for b in out:
        flat = {k: vs[0] for k, vs in b["all"].items()}
        if len([k for k in flat if k != "KI67"]) >= 2 or (len(flat) >= 2 and "KI67" in flat):
            blocks.append({"text": b["text"], "vec": flat, "all": b["all"]})
    return blocks

def match_block(vec, blocks, keys, min_hits=None):
    if not keys:
        return []
    need = min_hits if min_hits is not None else max(1, min(2, len(keys)))
    scored = []
    for i, b in enumerate(blocks):
        hit = 0
        for k in keys:
            if k not in vec or k not in b["all"]:
                continue
            if k == "KI67":
                if any(abs(float(x) - float(vec[k])) <= 1 for x in b["all"][k]):
                    hit += 1
            else:
                cand = []
                for x in b["all"][k]:
                    cand += str(x).split("/")
                if str(vec[k]) in cand:
                    hit += 1
        scored.append((hit, i))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    if top < need:
        return []
    return [i for s, i in scored if s == top]

def block_matches(pv, b, min_hits=None):
    shared = [k for k in pv if k in b["all"]]
    if not shared:
        return False
    hit = 0
    for k in shared:
        if k == "KI67":
            ok = any(abs(float(x) - float(pv[k])) <= 1 for x in b["all"][k])
        else:
            cand = []
            for x in b["all"][k]:
                cand += str(x).split("/")
            ok = str(pv[k]) in cand
        if ok:
            hit += 1
            continue
        # 2+ 经 ISH 可升为阳性、无扩增可降为阴性：HER2 一侧为「不确定」时不作冲突，
        # 也不计命中（`vec_of_panel` 取最后一条 HER2，ISH 常把 2+ 覆盖掉）
        if k == "HER2" and ("不确定" in [str(pv[k])] + [str(x) for x in b["all"][k]]):
            continue
        return False
    need = min_hits if min_hits is not None else max(1, min(2, len(shared)))
    return hit >= need

def vec_of_segment(seg, keys):
    best = {}
    for sent in re.split(r"[；;。\n]", seg or ""):
        v = vec_of_text(sent)
        if all(k in v for k in keys):
            return v, sent.strip()
        if len(v) > len(best):
            best = v
    return best, ""

def seg_conflicts(seg, key):
    vals = []
    for sent in re.split(r"[；;。\n]", seg or ""):
        for v in vals_of_text(sent).get(key, []):
            if v not in vals:
                vals.append(v)
    return vals

def vec_of_text(seg):
    v = vals_of_text(seg)
    return {k: vs[0] for k, vs in v.items()}

def vec_of_panel(p):
    out = {}
    for m in (p.get("markers") or []):
        k = mkey(m.get("marker_name"))
        if not k:
            continue
        if k == "KI67":
            if m.get("percent") is not None:
                out[k] = float(m["percent"])
        else:
            v = m.get("call") or m.get("status")
            if v in ("阳性", "阴性", "不确定"):
                out[k] = v
    return out
