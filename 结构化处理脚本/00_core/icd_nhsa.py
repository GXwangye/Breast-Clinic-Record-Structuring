#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import collections

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
REF_YB = os.path.join(DATA, "数据源/ICD参照/医保ICD10_v2.0_海南医保局发布_原件.xlsx")
REF_GL = os.path.join(DATA, "数据源/ICD参照/ICD10国临版2.0对照医保版2.0_海南医保局发布_原件.xlsx")
SHEET_YB = "完整分类与代码"

# ------------------------------------------------ 人工核定的版本差异（证据＝医保版2.0原件）
VERSION_DIFF = {
    "L72.3": ("L72.105", "医保版无 L72.3 亚目；皮脂腺囊肿在医保版落 L72.1x（L72.105）"),
    "Z22.5": ("Z22.800", "医保版 Z22 无 .5 亚目；WHO「病毒性肝炎病原携带者」并入 Z22.800 传染病病原携带者，其他的"),
    "R97.0": ("R77.803", "医保版无 R97 类目；WHO「肿瘤标记物异常」在医保版落 R77.8x（R77.803 癌胚抗原CEA升高）"),
    "R19.7": ("A09.900x007", "医保版 R19 无 .7 亚目；腹泻在医保版归 A09（A09.900x007 腹泻）"),
    "R91.1": ("R91.x04", "医保版 R91 收敛为 .x00/.x01/.x03/.x04；肺结节→R91.x04 孤立性肺结节"),
    "R91.8": ("R91.x00x001", "医保版 R91 收敛为 .x00/.x01/.x03/.x04；肺部阴影→R91.x00x001，肺占位→R91.x03"),
}

_CACHE = None

# ------------------------------------------------------------------ 载入
def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    import openpyxl
    cat_of, name_of, subs_of = {}, {}, collections.defaultdict(list)
    cat_rows = collections.defaultdict(list)
    sub_rows = collections.defaultdict(list)
    wb = openpyxl.load_workbook(REF_YB, read_only=True)
    ws = wb[SHEET_YB]
    it = ws.iter_rows(values_only=True)
    next(it)
    for r in it:
        cat, catn, sub, subn, code, name = r[5], r[6], r[7], r[8], r[9], r[10]
        if not code:
            continue
        code = str(code).strip()
        name_of[code] = name or ""
        cat_rows[str(cat or "")].append(code)
        if sub:
            sub = str(sub).strip()
            sub_rows[sub].append(code)
            subs_of[str(cat or "")].append(sub)
        if cat and str(cat) not in cat_of:
            cat_of[str(cat)] = catn or ""
    wb.close()

    # 国临版对照：医保版码 → (国临版码, 国临版名称)
    gl_of = {}
    if os.path.exists(REF_GL):
        wb = openpyxl.load_workbook(REF_GL, read_only=True)
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        next(it)
        for r in it:
            glc, gln, ybc, ybn = r[0], r[1], r[2], r[3]
            if ybc and str(ybc).strip() not in gl_of:
                gl_of[str(ybc).strip()] = (str(glc or "").strip(), gln or "")
        wb.close()

    _CACHE = dict(name_of=name_of, cat_rows=cat_rows, sub_rows=sub_rows,
                  subs_of=subs_of, cat_of=cat_of, gl_of=gl_of)
    return _CACHE

def n_yb_codes():
    return len(_load()["name_of"])

# ------------------------------------------------------------------ 检索
def lookup_gl(yb_code):
    return _load()["gl_of"].get(yb_code)

def yb_name(code):
    return _load()["name_of"].get(code, "")

def search_name(kw, limit=6):
    d = _load()["name_of"]
    return [(c, n) for c, n in d.items() if kw in n][:limit]

_NAME_IDX = None

def _name_index():
    global _NAME_IDX
    if _NAME_IDX is not None:
        return _NAME_IDX
    idx = collections.defaultdict(list)
    for c, n in _load()["name_of"].items():
        k = norm_for_match(n)
        if k:
            idx[k].append(c)
    _NAME_IDX = idx
    return _NAME_IDX

def resolve_by_name(name):
    k = norm_for_match(name)
    if not k:
        return dict(yb_code="", yb_name="", level="未解析", conf="低", note="空名")
    cands = _name_index().get(k, [])
    nm = _load()["name_of"]
    if len(cands) == 1:
        c = cands[0]
        return dict(yb_code=c, yb_name=nm.get(c, ""), level="名称精确", conf="高",
                    note="")
    if len(cands) > 1:
        # 同名多码：取最短码（类目级优先），并标注歧义供人工复核
        c = sorted(cands, key=lambda x: (len(x), x))[0]
        return dict(yb_code=c, yb_name=nm.get(c, ""), level="名称同名多码", conf="中",
                    note="同名 %d 码：%s" % (len(cands), "/".join(sorted(cands)[:4])))
    return dict(yb_code="", yb_name="", level="未解析", conf="低", note="名称未命中")

def list_sub(cat):
    d = _load()
    out = []
    for s in sorted(set(d["subs_of"].get(cat, []))):
        for c in d["sub_rows"][s]:
            out.append((c, d["name_of"].get(c, "")))
    for c in d["cat_rows"].get(cat, []):
        if not any(c == x for x, _ in out):
            out.append((c, d["name_of"].get(c, "")))
    return out

# ------------------------------------------------------------------ 名称分流
# 名称归一（**仅用于亚目分流匹配**，不改动原诊断名、不回写任何表）
#   乳腺→乳房：医保版用「乳房」，临床文本两者混用（乳腺炎/乳房炎 指同一诊断）
#   肿物|包块→肿块：同上
#   去尾部「症」「病」：男性乳房发育症→男性乳房发育、高血压病→高血压
_SYN = ((re.compile(r"乳腺"), "乳房"),
        (re.compile(r"肿物|包块"), "肿块"),
        (re.compile(r"[症病]$"), ""))

def norm_for_match(s):
    s = (s or "").strip()
    for p, r in _SYN:
        s = p.sub(r, s)
    return s

def route_by_name(who_code, diag_name, cur_code=""):
    d = _load()
    c = (who_code or "").strip()
    n = norm_for_match(diag_name)
    if not c or not n:
        return None
    best = None
    for code, nm in d["name_of"].items():
        if not code.startswith(c):
            continue
        if norm_for_match(nm) == n:
            # 同名的多个码取最短（最贴切）的那个
            if best is None or len(code) < len(best[0]):
                best = (code, nm, c, diag_name)
    if best and best[0] != cur_code:
        return best
    return None

# ------------------------------------------------------------------ 解析
def resolve(who_code):
    d = _load()
    c = (who_code or "").strip()
    res = dict(yb_code="", yb_name="", level="", conf="低", note="",
               n_sub=0, gl_code="", gl_name="")

    def _fin(code, level, conf, note, n):
        res.update(yb_code=code, yb_name=d["name_of"].get(code, ""),
                   level=level, conf=conf, note=note, n_sub=n)
        g = d["gl_of"].get(code)
        if g:
            res["gl_code"], res["gl_name"] = g
        return res

    # ---- R1 亚目级
    if "." in c:
        subs = d["sub_rows"].get(c, [])
        if subs:
            main = c + "00"
            if main not in d["name_of"]:
                main = subs[0]
            return _fin(main, "亚目", "高", "", len(subs))

    # ---- R2/R3/R4 类目级
    subs_of_cat = sorted(set(d["subs_of"].get(c, [])))
    cat_codes = d["cat_rows"].get(c, [])
    if subs_of_cat:
        pick = None
        for s in subs_of_cat:
            if s.endswith(".9") or s.endswith(".x"):
                pick = s
                break
        pick = pick or subs_of_cat[0]
        main = pick + "00"
        if main not in d["name_of"] and d["sub_rows"].get(pick):
            main = d["sub_rows"][pick][0]
        return _fin(main, "类目→亚目", "中",
                    "WHO 类目级码；医保版下分亚目 %s，须临床确认落哪一个" % "、".join(subs_of_cat[:4]),
                    len(cat_codes))
    if c + ".x00" in d["name_of"]:
        return _fin(c + ".x00", "类目", "高", "", len(cat_codes))
    if cat_codes:
        return _fin(cat_codes[0], "类目", "中", "医保版无 .x00 默认码，取该亚目首码", len(cat_codes))

    # ---- R5 版本差异 / 名称候选
    if c in VERSION_DIFF:
        code, why = VERSION_DIFF[c]
        return _fin(code, "版本差异", "中", why, 1)

    kw = re.sub(r"[，,（(].*$", "", who_code or "").strip()
    cands = search_name(kw[:6], 3) if kw else []
    if cands:
        return _fin(cands[0][0], "名称候选", "低",
                    "医保版未按 WHO 亚目划分；候选：" + "；".join("%s %s" % x for x in cands), 0)
    return _fin("", "未解析", "低", "医保版 2.0 表中未找到", 0)

# ------------------------------------------------------------------ 自检
SELF_TEST = [
    # (WHO 码, 期望医保版码, 期望 level, 期望 conf)
    ("C50.9", "C50.900", "亚目", "高"),
    ("C50.0", "C50.000", "亚目", "高"),
    ("D05.1", "D05.100", "亚目", "高"),
    ("D24", "D24.x00", "类目", "高"),
    ("N61", "N61.x00", "类目", "高"),
    ("N63", "N63.x00", "类目", "高"),
    ("N60.1", "N60.100", "亚目", "高"),
    ("Z85.3", "Z85.300", "亚目", "高"),
    ("I97.2", "I97.200", "亚目", "高"),
    # C80 未特指部位恶性肿瘤 → 取「未特指」亚目 C80.9（C80.900），比 C80.0 更贴 WHO 语义
    ("C80", "C80.900", "类目→亚目", "中"),
    ("L72.3", "L72.105", "版本差异", "中"),
    ("Z22.5", "Z22.800", "版本差异", "中"),
    ("R97.0", "R77.803", "版本差异", "中"),
    ("R19.7", "A09.900x007", "版本差异", "中"),
    ("R91.1", "R91.x04", "版本差异", "中"),
    ("R91.8", "R91.x00x001", "版本差异", "中"),
]

# 名称分流自检：(WHO 码, 诊断名, 期望落码 或 None=不产生变更)
ROUTE_TEST = [
    ("N63", "乳房结节", "N63.x01"),          # 21,408 行 · 本项目最高频分流
    ("N63", "乳腺结节", "N63.x01"),
    ("N63", "乳房肿物", None),              # 归一到「乳房肿块」＝N63.x00，与兜底同码 → 不产生变更
    ("N61", "乳腺炎", "N61.x04"),
    ("N61", "乳腺脓肿", "N61.x03"),
    ("N61", "浆细胞性乳腺炎", "N61.x06"),
    ("N62", "男性乳房发育症", "N62.x02"),
    ("I10", "高血压病", "I10.x00x002"),
    ("D70", "中性粒细胞减少症", "D70.x05"),
    ("R53", "乏力", "R53.x00x002"),
    ("R21", "皮疹", "R21.x02"),
    ("R42", "头晕", "R42.x00x004"),
    ("C80", "恶性肿瘤", "C80.000"),
    # ---- 反例：不得误分流（守「精确匹配、宁缺勿错」）
    ("I10", "妊娠期高血压", None),
    ("N63", "乳房结节样增生", None),
    ("C50", "乳腺癌", None),                 # 名称与 C50.9「乳房恶性肿瘤」不等，保持兜底
]

def _self_test():
    bad = 0
    for who, e_code, e_level, e_conf in SELF_TEST:
        r = resolve(who)
        ok = (r["yb_code"] == e_code and r["level"] == e_level and r["conf"] == e_conf)
        if not ok:
            bad += 1
        print("%s %-8s -> %-14s %-10s %-3s %s%s" % (
            "OK " if ok else "FAIL", who, r["yb_code"], r["level"], r["conf"],
            r["yb_name"], ("  " + r["note"]) if r["note"] else ""))

    print("\n---- 名称分流 route_by_name ----")
    for who, name, e_code in ROUTE_TEST:
        got = route_by_name(who, name, cur_code=resolve(who)["yb_code"])
        code = got[0] if got else None
        ok = (code == e_code)
        if not ok:
            bad += 1
        print("%s %-8s %-14s -> %-14s %s" % (
            "OK " if ok else "FAIL", who, name,
            code or "(不变更)", (got[1] if got else "")))

    print("\n医保版 2.0 码总数 %d ｜ 自检 %d/%d 通过" % (
        n_yb_codes(), len(SELF_TEST) + len(ROUTE_TEST) - bad,
        len(SELF_TEST) + len(ROUTE_TEST)))
    return bad

if __name__ == "__main__":
    import sys
    sys.exit(1 if _self_test() else 0)
