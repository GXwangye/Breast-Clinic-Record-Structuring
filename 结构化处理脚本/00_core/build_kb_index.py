# -*- coding: utf-8 -*-
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
KB_DIR = DATA + "/数据源/知识库"
DB = KB_DIR + "/kb_medical.sqlite"

ICD10_XLSX = DATA + "/数据源/ICD参照/医保ICD10_v2.0_海南医保局发布_原件.xlsx"
ICD9_XLSX = DATA + "/数据源/ICD参照/医保ICD9_v2.0_海南医保局发布_原件.xlsx"
MORPH_XLSX = DATA + "/数据源/ICD参照/ICD10医保版_肿瘤形态学_海南医保局发布_原件.xlsx"
DRUG_XLSX = DATA + "/数据源/药品参照/医保药品分类与代码_西药中成药_20210730_海南医保局发布_原件.xlsx"
LOINC_DIR = DATA + "/数据源/LOINC参照/loinc_2.83_official"
LOINC_CORE = LOINC_DIR + "/LoincTableCore/LoincTableCore.csv"
LOINC_ZH = LOINC_DIR + "/AccessoryFiles/LinguisticVariants/zhCN5LinguisticVariant.csv"

CJK = re.compile(r"[\u4e00-\u9fff]")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_text import units as _units, squash as _squash   # noqa: E402

NOISE = re.compile(r"[\s\u3000\u200b\"'“”‘’()（）\[\]【】<>《》,，、;；:：/\\|+*#]+")

SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE IF NOT EXISTS term(
  tid     INTEGER PRIMARY KEY,
  domain  TEXT NOT NULL,
  code    TEXT NOT NULL,
  name    TEXT NOT NULL,
  display TEXT,
  level   TEXT,
  extra   TEXT
);
CREATE TABLE IF NOT EXISTS bigram(
  bg  TEXT NOT NULL,
  tid INTEGER NOT NULL,
  PRIMARY KEY(bg, tid)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

def units(s):
    return _units(s)

def norm_name(s):
    s = str(s or "").replace("\u3000", " ").replace("\u200b", "")
    s = s.replace("＋", "+").replace("＊", "*")
    s = re.sub(r"\s+", " ", s).strip()
    return s

class Builder:
    def __init__(self, conn):
        self.c = conn
        self.tid = 0
        self.rows = []
        self.bgs = []
        self.stat = Counter()
        self.seen = set()      # (domain,code,name,level) 去重：原始表里层级行大量重复

    def add(self, domain, code, name, display=None, level=None, extra=None):
        name = norm_name(name)
        if not name or len(name) < 2:
            return
        code = norm_name(code)
        if not code:
            return
        key = (domain, code, name, level or "")
        if key in self.seen:
            return
        self.seen.add(key)
        self.tid += 1
        tid = self.tid
        self.rows.append((tid, domain, code, name, norm_name(display or name),
                          level or "", json.dumps(extra, ensure_ascii=False) if extra else ""))
        seen = set()
        for bg in units(name):
            if bg in seen:
                continue
            seen.add(bg)
            self.bgs.append((bg, tid))
        self.stat[domain] += 1
        if len(self.rows) >= 20000:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        self.c.executemany("INSERT INTO term VALUES(?,?,?,?,?,?,?)", self.rows)
        self.c.executemany("INSERT OR IGNORE INTO bigram VALUES(?,?)", self.bgs)
        self.c.commit()
        self.rows.clear()
        self.bgs.clear()

def build_icd10(b):
    from openpyxl import load_workbook
    wb = load_workbook(ICD10_XLSX, read_only=True)
    # 1) 层级表：类目/亚目用于"只能归到类目"时的候选
    ws = wb["完整分类与代码"]
    hdr = None
    for r in ws.iter_rows(min_row=1, values_only=True):
        if hdr is None:
            hdr = [str(x or "").strip() for x in r]
            idx = {k: i for i, k in enumerate(hdr)}
            continue
        g = lambda k: str(r[idx[k]] or "").strip() if k in idx and idx[k] < len(r) else ""
        chapter = g("章的名称")
        for code_k, name_k, lv in (("类目代码", "类目名称", "category"),
                                   ("亚目代码", "亚目名称", "subcategory"),
                                   ("细目代码", "细目名称", "subclassification")):
            if code_k in idx and g(code_k) and g(name_k):
                b.add("icd10", g(code_k), g(name_k), level=lv, extra={"chapter": chapter})
    wb.close()
    print("  ICD10 层级条目已入：%d" % b.stat["icd10"], flush=True)
    # 2) 实际使用条目（细目扩展码）
    wb = load_workbook(ICD10_XLSX, read_only=True)
    ws = wb["实际使用条目"]
    n = 0
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if not r or not r[0] or not r[1]:
            continue
        b.add("icd10", r[0], r[1], level="code")
        n += 1
    wb.close()
    print("  ICD10 实际条目 %d 行，累计 term=%d" % (n, b.stat["icd10"]), flush=True)

def build_icd9(b):
    from openpyxl import load_workbook
    wb = load_workbook(ICD9_XLSX, read_only=True)
    ws = wb["完整分类与代码"]
    hdr = None
    n = 0
    for r in ws.iter_rows(min_row=1, values_only=True):
        if hdr is None:
            hdr = [str(x or "").strip() for x in r]
            idx = {k: i for i, k in enumerate(hdr)}
            continue
        g = lambda k: str(r[idx[k]] or "").strip() if k in idx and idx[k] < len(r) else ""
        for code_k, name_k, lv in (("类目代码", "类目名称", "category"),
                                   ("亚目代码", "亚目名称", "subcategory"),
                                   ("细目代码", "细目名称", "subclassification")):
            if code_k in idx and g(code_k) and g(name_k):
                b.add("icd9", g(code_k), g(name_k), level=lv, extra={"chapter": g("章的名称")})
                n += 1
    wb.close()
    wb = load_workbook(ICD9_XLSX, read_only=True)
    ws = wb["实际使用条目"]
    m = 0
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0] or not r[1]:
            continue
        b.add("icd9", r[0], r[1], level="code")
        m += 1
    wb.close()
    print("  ICD9 层级 %d / 条目 %d，累计 term=%d" % (n, m, b.stat["icd9"]), flush=True)

def build_morph(b):
    from openpyxl import load_workbook
    wb = load_workbook(MORPH_XLSX, read_only=True)
    ws = wb[wb.sheetnames[0]]
    n = 0
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if not r or not r[0]:
            continue
        tcode = str(r[0] or "").strip()          # M850 （粗分类码）
        tname = str(r[1] or "").strip()          # 导管性、小叶性和髓样肿瘤
        mcode = str(r[2] or "").strip()          # M85000/3 （带动态码）
        mname = str(r[3] or "").strip()          # 浸润性导管癌
        dyn = ""
        if "/" in mcode:
            d = mcode.rsplit("/", 1)[1]
            dyn = {"0": "良性", "1": "动态未定", "2": "原位癌", "3": "恶性", "6": "转移性", "9": "未知"}.get(d, "")
        # 主名称：形态学分类
        if mname and mname != tname:
            b.add("morph", mcode, mname, display="%s %s" % (mcode, mname),
                  level="morph", extra={"category": tname, "dynamic": dyn, "tumor_code": tcode})
            n += 1
        elif mname:
            b.add("morph", mcode, mname, display="%s %s %s" % (mcode, mname, dyn),
                  level="morph", extra={"dynamic": dyn, "tumor_code": tcode})
            n += 1
        # 粗分类名单独可查（LLM 只给得出"导管性肿瘤"时用）
        if tname:
            b.add("morph", tcode, tname, display="%s %s" % (tcode, tname), level="group",
                  extra={"dynamic": dyn})
    wb.close()
    print("  形态学 %d 条，累计 term=%d" % (n, b.stat["morph"]), flush=True)

def build_loinc(b, max_syn=0, max_syn_len=16):
    zh = {}
    with open(LOINC_ZH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("LOINC_NUM") or "").strip()
            if code:
                zh[code] = row
    n_zh = 0
    with open(LOINC_CORE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("LOINC_NUM") or "").strip()
            comp_en = norm_name(row.get("COMPONENT"))
            sys_en = norm_name(row.get("SYSTEM"))
            cls_en = norm_name(row.get("CLASS"))
            sn_en = norm_name(row.get("SHORTNAME"))
            z = zh.get(code)
            if z:
                comp = norm_name(z.get("COMPONENT")) or comp_en
                sysz = norm_name(z.get("SYSTEM"))
                cls = norm_name(z.get("CLASS")) or cls_en
                display = " / ".join([x for x in (comp, sysz.replace("^患者", ""), cls) if x])
                b.add("loinc", code, comp, display=display, level="component_zh",
                      extra={"system": sysz, "class": cls, "scale": norm_name(z.get("SCALE_TYP")),
                             "component_en": comp_en})
                n_zh += 1
            else:
                display = " / ".join([x for x in (comp_en, sys_en, cls_en) if x])
                b.add("loinc", code, comp_en, display=display, level="component_en",
                      extra={"system": sys_en, "class": cls_en})
            # 英文缩写始终保留：CEA / AFP / CA15-3 这类查询只能靠它
            if sn_en and sn_en != comp_en:
                b.add("loinc", code, sn_en,
                      display=display if z else " / ".join([x for x in (comp_en, sys_en, cls_en) if x]),
                      level="shortname_en",
                      extra={"system": sys_en, "class": cls_en})
    print("  LOINC 中文项 %d / 总 term=%d" % (n_zh, b.stat["loinc"]), flush=True)

def build_drug(b):
    from openpyxl import load_workbook
    wb = load_workbook(DRUG_XLSX, read_only=True)
    ws = wb[wb.sheetnames[0]]
    hdr = None
    idx = {}
    n = 0
    for r in ws.iter_rows(min_row=1, values_only=True):
        if hdr is None:
            hdr = [str(x or "").strip() for x in r]
            idx = {k: i for i, k in enumerate(hdr)}
            continue
        if not r:
            continue
        g = lambda k: str(r[idx[k]] or "").strip() if k in idx and idx[k] < len(r) else ""
        code = g("药品代码")
        if not code:
            continue
        name = g("注册名称")
        b.add("drug", code, name, display=" ".join(x for x in (name, g("实际剂型"), g("实际规格")) if x),
              level="registered",
              extra={"form": g("实际剂型"), "spec": g("实际规格"), "source": g("数据来源")})
        trade = g("商品名称")
        if trade and trade not in ("无", "-", ""):
            b.add("drug", code, trade, display="%s（商品名 %s）" % (name, trade), level="trade",
                  extra={"form": g("实际剂型"), "spec": g("实际规格")})
        n += 1
    wb.close()
    print("  药品 %d 行，累计 term=%d" % (n, b.stat["drug"]), flush=True)

# 影像模态的中文规范名（部件取值翻译，不是"病历写法→码"的映射）
MODALITY_ZH = {
    "US": "超声", "MG": "钼靶", "MR": "磁共振", "CT": "CT", "XR": "X线",
    "DX": "X线摄影", "CR": "X线摄影", "RF": "透视", "NM": "核医学",
    "PT": "PET", "XA": "血管造影", "BMD": "骨密度", "OCT": "OCT",
}

def build_rad(b):
    p = LOINC_DIR + "/AccessoryFiles/LoincRsnaRadiologyPlaybook/LoincRsnaRadiologyPlaybook.csv"
    names, parts = {}, {}
    with open(p, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            n = (row.get("LoincNumber") or "").strip()
            if not n:
                continue
            names.setdefault(n, norm_name(row.get("LongCommonName")))
            parts.setdefault(n, {}).setdefault(row.get("PartTypeName") or "", row.get("PartName") or "")
    zh = {}
    with open(LOINC_ZH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            c = (row.get("LOINC_NUM") or "").strip()
            if c:
                zh[c] = row
    keymap = {
        "Rad.Modality.Modality Type": "modality",
        "Rad.Modality.Modality Subtype": "subtype",
        "Rad.Anatomic Location.Region Imaged": "region",
        "Rad.Anatomic Location.Laterality": "laterality",
        "Rad.Reason for Exam": "reason",
        "Rad.Guidance for.Action": "guidance",
    }
    n_zh = 0
    for n, nm in names.items():
        if not nm:
            continue
        ex = {v: parts[n].get(k) for k, v in keymap.items() if parts[n].get(k)}
        z = zh.get(n)
        if z:
            comp = norm_name(z.get("COMPONENT"))
            sysz = norm_name(z.get("SYSTEM")).replace("^患者", "")
            mod = MODALITY_ZH.get((ex.get("modality") or "").upper(), ex.get("modality") or "")
            ex["name_zh"] = " ".join(x for x in (mod, comp, sysz) if x)
            b.add("rad", n, ex["name_zh"], display="%s（%s）" % (ex["name_zh"], nm),
                  level="rad_zh", extra=ex)
            n_zh += 1
        b.add("rad", n, nm, display=nm, level="rad", extra=ex)
    print("  影像 %d 码入索引，其中中文名 %d 条" % (len(names), n_zh), flush=True)

BUILDERS = {"icd10": build_icd10, "icd9": build_icd9, "morph": build_morph,
            "loinc": build_loinc, "drug": build_drug, "rad": build_rad}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", action="append", help="只重建指定域（可多次）")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()

    os.makedirs(KB_DIR, exist_ok=True)
    if a.stat:
        if not os.path.exists(DB):
            print("索引不存在：%s" % DB)
            return
        c = sqlite3.connect(DB)
        for dom, cnt in c.execute("SELECT domain,count(*) FROM term GROUP BY domain ORDER BY 2 DESC"):
            codes = c.execute("SELECT count(DISTINCT code) FROM term WHERE domain=?", (dom,)).fetchone()[0]
            print("  %-8s term=%-8d distinct_code=%d" % (dom, cnt, codes))
        print("  bigram:", c.execute("SELECT count(*) FROM bigram").fetchone()[0])
        for k, v in c.execute("SELECT k,v FROM meta ORDER BY k"):
            print("  meta %s = %s" % (k, v))
        c.close()
        return

    t0 = time.time()
    if os.path.exists(DB) and a.rebuild:
        os.rename(DB, DB + ".old")
        print("已备份旧索引 ->", DB + ".old")
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    doms = a.domain or ["icd10", "icd9", "morph", "loinc", "drug"]
    if a.domain:
        conn.executemany("DELETE FROM bigram WHERE tid IN (SELECT tid FROM term WHERE domain=?)",
                         [(d,) for d in doms])
        conn.executemany("DELETE FROM term WHERE domain=?", [(d,) for d in doms])
        conn.commit()
    b = Builder(conn)
    b.tid = conn.execute("SELECT coalesce(max(tid),0) FROM term").fetchone()[0]
    for d in doms:
        print("构建 %s ..." % d, flush=True)
        BUILDERS[d](b)
        b.flush()
        print("  完成 %s，累计 term=%d bigram=%d，用时 %.0fs"
              % (d, b.stat[d], conn.execute("SELECT count(*) FROM bigram").fetchone()[0], time.time() - t0),
              flush=True)

    print("建索引 ...", flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_domain ON term(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_name ON term(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_code ON term(domain, code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bg_tid ON bigram(tid)")
    conn.commit()
    for k, v in [("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
                 ("term_total", conn.execute("SELECT count(*) FROM term").fetchone()[0]),
                 ("bigram_total", conn.execute("SELECT count(*) FROM bigram").fetchone()[0])]:
        conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    # domains 记全量（增量重建时不能覆盖成只本次构建的域）
    all_doms = [r[0] for r in conn.execute("SELECT DISTINCT domain FROM term ORDER BY domain")]
    conn.execute("INSERT OR REPLACE INTO meta VALUES('domains',?)", (",".join(all_doms),))
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    print("完成：%s  %.1f MB  用时 %.0fs"
          % (DB, os.path.getsize(DB) / 1e6, time.time() - t0))

if __name__ == "__main__":
    main()
