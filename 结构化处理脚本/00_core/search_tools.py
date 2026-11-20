# -*- coding: utf-8 -*-
import argparse
import difflib
import math
import os
import re
import sqlite3
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
DB = DATA + "/数据源/知识库/kb_medical.sqlite"

CJK = re.compile(r"[\u4e00-\u9fff]")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_text import units, squash                    # noqa: E402,F401

_conn = None

def conn():
    global _conn
    if _conn is None:
        if not os.path.exists(DB):
            raise SystemExit("知识库索引不存在：%s（先跑 build_kb_index.py）" % DB)
        _conn = sqlite3.connect(DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn

def _df(bgs):
    c = conn()
    q = "SELECT bg, df FROM bgstat WHERE bg IN (%s)" % ",".join("?" * len(bgs))
    return {r["bg"]: r["df"] for r in c.execute(q, bgs)}

def build_df():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS bgstat(bg TEXT PRIMARY KEY, df INTEGER)")
    c.execute("DELETE FROM bgstat")
    c.execute("INSERT INTO bgstat SELECT bg, count(*) FROM bigram GROUP BY bg")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bgstat_bg ON bgstat(bg)")
    c.commit()
    n = c.execute("SELECT count(*) FROM bgstat").fetchone()[0]
    print("bgstat 建好：%d 个 bigram" % n)

def _candidates(bgs, need, cap=4000):
    c = conn()
    if not bgs:
        return {}
    q = "SELECT bg, tid FROM bigram WHERE bg IN (%s)" % ",".join("?" * len(bgs))
    hit = {}
    for r in c.execute(q, bgs):
        hit.setdefault(r["tid"], set()).add(r["bg"])
    if need > 1:
        hit = {t: s for t, s in hit.items() if len(s) >= need}
    if len(hit) > cap:
        # 命中 bigram 越多的越可能是目标，先按此截断
        top = sorted(hit.items(), key=lambda kv: -len(kv[1]))[:cap]
        hit = dict(top)
    return hit

def _score(qs, qu, name, df, total):
    ns = squash(name)
    if not qs or not ns:
        return 0.0
    if qs == ns:
        return 1.0
    nu = set(units(name))
    qu = set(qu)
    if not qu:
        return 0.0
    w = {}
    for bg in qu:
        # idf 上限 3.0：跨词边界切出的生僻二元组（如「腺超」df=1）会给出
        # 极大权重，把真正的实词（乳腺、超声）压掉，导致正确候选被埋。
        w[bg] = min(3.0, math.log(1.0 + total / (df.get(bg, 1) + 10.0)))
    tot_w = sum(w.values()) or 1.0
    cov = sum(w[bg] for bg in qu if bg in nu) / tot_w
    sim = difflib.SequenceMatcher(None, qs, ns).ratio()
    contain = 1.0 if (qs in ns or ns in qs) else 0.0
    s = 0.55 * cov + 0.25 * sim + 0.20 * contain
    # 长度悬殊惩罚："乳腺" 命中 "乳腺恶性肿瘤" 不该拿高分
    ratio = min(len(qs), len(ns)) / max(len(qs), len(ns))
    s *= (0.70 + 0.30 * ratio)
    return round(min(1.0, s), 4)

def search(domain, query, k=10, level=None, min_score=0.15, explain=False):
    c = conn()
    qs = squash(query)
    if len(qs) < 2:
        return []
    qu = sorted(set(units(query)))
    total = c.execute("SELECT coalesce(sum(df),1) FROM bgstat WHERE bg IN (%s)"
                      % ",".join("?" * len(qu)), qu).fetchone()[0] if qu else 1
    total = max(total, 1000)
    df = _df(qu)

    out = {}
    # 1) 精确名命中（走 name 索引，最快最准）
    sql = "SELECT * FROM term WHERE domain=? AND name=?"
    args = [domain, str(query).strip()]
    for r in c.execute(sql, args):
        out[r["tid"]] = (1.0, "exact", r)

    # 2) 倒排召回。need 是"至少命中几个检索单元"：
    #    短查询（≤4 单元，多为英文缩写）只要求 1 个，否则 CA15-3 会被
    #    "Cancer Ag15-3" 这类带轴词的写法挡在门外；长查询要求 2–3 个控噪。
    n_qu = len(qu)
    need = 1 if n_qu <= 4 else min(3, max(2, int(math.ceil(n_qu * 0.45))))
    hit = _candidates(qu, need)
    if hit:
        ph = ",".join("?" * len(hit))
        rows = c.execute("SELECT * FROM term WHERE domain=? AND tid IN (%s)" % ph,
                         [domain] + list(hit)).fetchall()
        for r in rows:
            if level and r["level"] != level:
                continue
            s = _score(qs, qu, r["name"], df, total)
            if s >= min_score and (r["tid"] not in out or s > out[r["tid"]][0]):
                out[r["tid"]] = (s, "fuzzy", r)

    res = []
    for tid, (s, how, r) in out.items():
        d = {"code": r["code"], "name": r["name"], "display": r["display"],
             "level": r["level"], "score": s, "match": how}
        if explain:
            d["domain"] = r["domain"]
        res.append(d)
    # 同码同名的去重，保留最高分
    dedup = {}
    for d in res:
        key = (d["code"], d["name"])
        if key not in dedup or d["score"] > dedup[key]["score"]:
            dedup[key] = d
    return sorted(dedup.values(), key=lambda x: (-x["score"], len(x["name"])))[:k]

def search_icd10(q, k=10, **kw):   return search("icd10", q, k, **kw)
def search_icd9(q, k=10, **kw):    return search("icd9", q, k, **kw)
def search_loinc(q, k=10, **kw):   return search("loinc", q, k, **kw)
def search_drug(q, k=10, **kw):    return search("drug", q, k, **kw)
def search_morph(q, k=10, **kw):   return search("morph", q, k, **kw)

def exists(domain, code):
    r = conn().execute("SELECT 1 FROM term WHERE domain=? AND code=? LIMIT 1",
                       (domain, str(code).strip())).fetchone()
    return bool(r)

def get(domain, code):
    rows = conn().execute(
        "SELECT code,name,display,level,extra FROM term WHERE domain=? AND code=? ORDER BY length(name) LIMIT 6",
        (domain, str(code).strip())).fetchall()
    return [dict(r) for r in rows]

def lookup(domain, name, k=6):
    c = conn()
    nm = str(name or "").strip()
    if len(nm) < 2:
        return []
    out, seen = [], set()

    def push(rows, how):
        for r in rows:
            key = (r["code"], r["name"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"code": r["code"], "name": r["name"], "display": r["display"],
                        "level": r["level"], "match": how})

    push(c.execute("SELECT * FROM term WHERE domain=? AND name=? LIMIT 20", (domain, nm)), "exact")
    if len(out) < k:
        push(c.execute("SELECT * FROM term WHERE domain=? AND name LIKE ? LIMIT 40",
                       (domain, "%" + nm + "%")), "contains")
    if len(out) < k:
        push(c.execute("SELECT * FROM term WHERE domain=? AND ? LIKE '%'||name||'%' AND length(name)>=4 LIMIT 40",
                       (domain, nm)), "contained")
    return out[:k]

def codes_by_name(domain, name):
    rows = conn().execute(
        "SELECT DISTINCT code FROM term WHERE domain=? AND name=?",
        (domain, str(name).strip())).fetchall()
    return [r["code"] for r in rows]

def verify(domain, code, name=None):
    if not exists(domain, code):
        return False, "码不存在"
    if not name:
        return True, "码存在"
    r = conn().execute("SELECT 1 FROM term WHERE domain=? AND code=? AND name=? LIMIT 1",
                       (domain, code, str(name).strip())).fetchone()
    return (True, "码与名一致") if r else (False, "码存在但名称不一致")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domain", nargs="?")
    ap.add_argument("query", nargs="?")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--level")
    ap.add_argument("--min-score", type=float, default=0.15)
    ap.add_argument("--build-df", action="store_true")
    a = ap.parse_args()
    if a.build_df:
        build_df()
        return
    if not a.domain or not a.query:
        ap.print_help()
        return
    for d in search(a.domain, a.query, a.k, level=a.level, min_score=a.min_score, explain=True):
        print("  %.3f  %-14s %-28s [%s] %s" % (d["score"], d["code"], d["name"], d["level"], d["display"][:60]))

if __name__ == "__main__":
    main()
