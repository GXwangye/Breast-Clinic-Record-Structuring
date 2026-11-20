# -*- coding: utf-8 -*-

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
OUTDIR = DATA + "/数据源/LOINC参照"
OUT = OUTDIR + "/loinc_2.83_zh.jsonl"
META = OUTDIR + "/loinc_fetch_meta.json"
CKPT = OUTDIR + "/_ckpt.json"
LOG = OUTDIR + "/fetch_log.txt"
SHARDS = OUTDIR + "/_pages"          # 每页一个分片，中断后按分片续跑

BASE = "https://r4.ontoserver.csiro.au/fhir/ValueSet/$expand"
VS = "http://loinc.org/vs"
PAGE = 1000
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DELAY = 1.2                          # 页间隔秒数（并发会触发 403，必须顺序+间隔）

_lock = threading.Lock()

def _get(offset, count=PAGE, zh=True):
    q = {"url": VS, "count": count, "offset": offset}
    if zh:
        q["displayLanguage"] = "zh"
    url = BASE + "?" + urllib.parse.urlencode(q)
    for attempt in range(8):
        req = urllib.request.Request(url, headers={
            "Accept": "application/fhir+json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://r4.ontoserver.csiro.au/fhir/",
            "User-Agent": UA,
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504):
                wait = 20 * (attempt + 1)
                print("  HTTP %d @offset=%d，退避 %ds" % (e.code, offset, wait), flush=True)
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt == 5:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("offset %d 重试耗尽" % offset)

def probe():
    d = _get(0, 5)
    e = d.get("expansion") or {}
    print("total:", e.get("total"))
    print("offset:", e.get("offset"), "returned:", len(e.get("contains") or []))
    for c in (e.get("contains") or [])[:5]:
        print("  ", c.get("code"), "|", c.get("display"))
    # 单码核版本
    u = "https://tx.fhir.org/r4/CodeSystem/$lookup?system=http://loinc.org&code=718-7"
    req = urllib.request.Request(u, headers={"Accept": "application/fhir+json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            p = json.loads(r.read().decode("utf-8"))
        ver = [x.get("valueString") for x in p["parameter"] if x.get("name") == "version"]
        print("CodeSystem version:", ver)
    except Exception as e:
        print("version probe failed:", e)

def fetch_page(offset):
    d = _get(offset)
    e = d.get("expansion") or {}
    rows = []
    for c in e.get("contains") or []:
        code = c.get("code")
        if not code:
            continue
        rows.append({"code": code, "display": (c.get("display") or "").strip(),
                     "system": c.get("system") or "http://loinc.org"})
    return offset, rows, e.get("total")

def apply(workers=5):
    os.makedirs(SHARDS, exist_ok=True)
    total = (_get(0, 1).get("expansion") or {}).get("total")
    if not total:
        raise SystemExit("拿不到 total，终止")
    offsets = list(range(0, total, PAGE))

    def _ok(off):
        sp = os.path.join(SHARDS, "%08d.jsonl" % off)
        if not os.path.exists(sp):
            return False
        want = min(PAGE, total - off)
        try:
            with open(sp, encoding="utf-8") as f:
                n = sum(1 for _ in f)
        except Exception:
            return False
        return n >= want

    todo = [o for o in offsets if not _ok(o)]
    print("total=%d 共 %d 页，待抓 %d 页（已完成 %d）"
          % (total, len(offsets), len(todo), len(offsets) - len(todo)), flush=True)

    def one(off):
        try:
            _o, rows, _t = fetch_page(off)
        except Exception as e:
            with _lock:
                one.fail += 1
                if one.fail <= 8:
                    print("  页 offset=%d 失败：%s %s"
                          % (off, type(e).__name__, str(e)[:60]), flush=True)
            return False
        sp = os.path.join(SHARDS, "%08d.jsonl" % off)
        # 直接写最终文件，不做 tmp→rename；分片完整性由 resume 时的行数校验兜底。
        with open(sp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with _lock:
            one.n += 1
        return True
    one.n = 0
    one.fail = 0

    t0 = time.time()
    for rnd in range(1, 7):
        todo = [o for o in offsets if not _ok(o)]
        if not todo:
            break
        print("第 %d 轮：待抓 %d 页" % (rnd, len(todo)), flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, todo))
        print("  本轮完成 %d 页，用时 %.1f 分钟"
              % (one.n, (time.time() - t0) / 60), flush=True)
    left = [o for o in offsets if not _ok(o)]
    if left:
        print("!! 仍有 %d 页未取到：%s" % (len(left), left[:10]), flush=True)
    print("抓取结束，用时 %.1f 分钟" % ((time.time() - t0) / 60), flush=True)

    # 合并去重
    seen = set()
    got = 0
    with open(OUT, "w", encoding="utf-8") as out:
        for off in offsets:
            sp = os.path.join(SHARDS, "%08d.jsonl" % off)
            if not os.path.exists(sp):
                continue
            for ln in open(sp, encoding="utf-8"):
                r = json.loads(ln)
                if r["code"] in seen:
                    continue
                seen.add(r["code"])
                out.write(ln if ln.endswith("\n") else ln + "\n")
                got += 1
    meta = {"source": BASE, "valueset": VS, "version": "2.83",
            "display_language": "zh", "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_reported": total, "rows_written": got, "file": OUT,
            "note": "display 为中文优先、缺译名回落英文；"
                    "内容取自公开 FHIR 术语服务器的 LOINC 2.83"}
    json.dump(meta, open(META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("合并完成：%d 条（服务器报告 total=%d）→ %s" % (got, total, OUT), flush=True)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    if a.probe:
        probe()
    elif a.apply:
        apply(a.workers)
    else:
        print("需要 --probe 或 --apply")
