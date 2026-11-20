# -*- coding: utf-8 -*-
import argparse
import collections
import io
import json
import os
import re
import sys
import tarfile
import time

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records, count  # noqa: E402

PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
JSONL = ROOT + "/publish/02_结构化结果与过程/中间产物/_agents_input/input_records.jsonl"
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_ihc.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final_tnm.tar"
OUTD = DATA + "/中间产物/_qa_out"

# ---- 契约值域（与 derive_contract_fields.py 同源，改这里必须先改契约 §3.6）----
PREFIX_OK = {"c", "p", "r", "y", "u"}
PREFIX_ORDER = {"c": 0, "p": 1, "r": 2, "y": 3, "u": 4, "": 5}   # 并存时的排列序
UNSPEC = {"", "未提及", "未指明", "未标注", "未填", "空", "none", "null", "无",
          "-", "—", "/", "∅", "n/a", "na", "u", "unknown"}

# 三段式完整 TNM：前缀 0~2 字母 + T + N（含亚级）+ M
RE_TNM3 = re.compile(
    r"(?<![A-Za-z])"
    r"([cpyruCOP]{0,2})"
    r"\s*T\s*(is|[0-4])"
    r"\s*N\s*([0-3])\s*(mi|a1|a2|b1|b2|a|b|c)?"
    r"\s*M\s*([01])",
    re.I,
)

# 紧邻分期（必须带「期」字，宁可少填不可错填）
RE_TAIL_STAGE = re.compile(
    r"^[\s，,、;；:：()（）\[\]【】\-—–·]{0,4}(?:stage\s*)?([ⅠⅡⅢⅣIVX]{1,4})\s*([ABCabc])?\s*期",
    re.I,
)

# 假分期陷阱（契约 §3.6 必拦）：期别紧跟术式词 → 手术分期
RE_FAKE_SURG = re.compile(r"[ⅠⅡⅢⅣI]{1,4}\s*期?.{0,4}(重建|再造|成形|修补|闭式|缝合)")

MALIGNANT = re.compile(r"恶性|癌")

# N 亚级白名单（契约 §3.6 N_VALID：0/1MI/1A/1B/1/2/2A/3/3A/X）。
# 原文偶见 `N3c`，AJCC 乳腺无此取值 —— 不搬，宁可留空。
N_SUB_OK = {"", "mi", "a", "b", "a1", "a2", "b1", "b2"}

TNM_KEYS = ("prefix", "t", "n", "m", "stage")

def _s(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "|".join(_s(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()

def is_empty(v):
    return _s(v).lower() in UNSPEC

def text_segments(r):
    segs = []
    f = r.get("fields")
    if isinstance(f, dict):
        for k, v in f.items():
            sv = _s(v)
            if sv:
                segs.append((k, sv))
    dt = _s(r.get("diagnoses_text"))
    if dt:
        segs.append(("diagnoses_text", dt))
    return segs

def scan_record(r):
    found = {}
    n_hit = 0
    for fname, text in text_segments(r):
        for m in RE_TNM3.finditer(text):
            raw_pref = (m.group(1) or "").lower()
            t_core = (m.group(2) or "").upper()
            n_core = m.group(3)
            n_sub = (m.group(4) or "").lower()
            m_core = m.group(5)

            if n_sub not in N_SUB_OK:
                continue    # N3c 之类：值域没有，不搬

            # 前缀归一：yp（新辅助后病理）/ yc（新辅助后临床）→ y（闭集无 yp/yc 组合）
            pref = raw_pref
            pref_note = None
            if pref in ("yp", "yc"):
                pref_note = "前缀「%s」归 y（契约闭集无 yp/yc）" % pref
                pref = "y"
            elif pref and pref not in PREFIX_OK:
                continue  # 前缀不在闭集（如 m），不搬
            if not pref and pref_note:
                pref_note = None

            t_val = "T" + ("is" if t_core.lower() == "is" else t_core)
            n_val = "N" + n_core + n_sub
            m_val = "M" + m_core
            key = "%s|%s|%s|%s" % (pref, t_val, n_val, m_val)

            n_hit += 1
            if key in found:
                found[key]["dup"] += 1
                continue

            # 紧邻分期
            stage = ""
            tail = text[m.end():m.end() + 24]
            mt = RE_TAIL_STAGE.match(tail)
            if mt:
                cand = "%s%s期" % (mt.group(1), mt.group(2) or "")
                if not RE_FAKE_SURG.search(cand + tail[mt.end():mt.end() + 4]):
                    stage = cand

            found[key] = {
                "source_field": fname,
                "offset": m.start(),
                "length": m.end() - m.start(),
                "quote": m.group(0),
                "prefix": pref, "t": t_val, "n": n_val, "m": m_val,
                "stage": stage,
                "prefix_note": pref_note,
                "dup": 0,
            }
    return found, n_hit

def iter_items(rec):
    d = rec.get("diagnoses")
    if isinstance(d, dict):
        items = d.get("items")
    elif isinstance(d, list):
        items = d
    else:
        items = None
    return items if isinstance(items, list) else []

def backfill_record(rec, idx, stat, ledger):
    items = iter_items(rec)
    mal = [it for it in items if isinstance(it, dict) and MALIGNANT.search(_s(it.get("name")))]

    def skip(why):
        stat["skip_" + why] += 1
        return False

    if not items:
        return skip("no_diag_item")
    if not mal:
        return skip("no_malignant_diag")

    multi_diag = False
    if len(mal) > 1:
        # 多恶性项（如「乳腺恶性肿瘤」+「乳腺癌术后」）：同一次就诊同一病情，TNM 只有一套。
        # 允许归首条，但必须满足两条——所有恶性项 tnm 全空（不覆盖）、侧别不冲突（防双侧癌错归）。
        for it in mal:
            tm = it.get("tnm")
            if not isinstance(tm, dict) or any(not is_empty(tm.get(k)) for k in TNM_KEYS):
                return skip("multi_malignant_diag")
        sides = {_s(it.get("side")) for it in mal}
        sides = {s for s in sides if not is_empty(s)}
        if len(sides) > 1:
            return skip("multi_diag_side_conflict")
        multi_diag = True
    if idx is None:
        return skip("no_tnm_in_text")

    found, n_hit = idx
    if not found:
        return skip("no_tnm_in_text")

    # 契约 §3.6：临床（c）与病理（p）分期常并存，「不可只留一个值」。
    # 故按前缀分套并存，每套一个 tnm 对象；但同一前缀出现两种取值 = 原文自相矛盾，整条不搬。
    by_pref = {}
    for k, v in found.items():
        p = v["prefix"]
        if p in by_pref:
            stat["skip_same_prefix_conflict"] += 1
            return False
        by_pref[p] = v
    vals = [by_pref[p] for p in sorted(by_pref, key=lambda x: PREFIX_ORDER.get(x, 9))]

    it = mal[0]
    t = it.get("tnm")
    if isinstance(t, list):
        return skip("already_filled")
    if not isinstance(t, dict):
        if t not in (None, "", [], {}):
            return skip("tnm_slot_malformed")
        t = {}
        it["tnm"] = t
        stat["slot_created"] += 1

    # 已有值一律不动
    if any(not is_empty(t.get(k)) for k in TNM_KEYS):
        return skip("already_filled")

    def _mk(val, multi):
        o = {
            "prefix": val["prefix"],
            "t": val["t"],
            "n": val["n"],
            "m": val["m"],
            "stage": val["stage"] or "",
            # 溯源只记位置与规则，不把原文引文写进结构化数据（契约第 41 条）
            "backfill": {
                "rule": "text_tnm_v1",
                "source_field": val["source_field"],
                "offset": val["offset"],
                "length": val["length"],
            },
        }
        if val["prefix_note"]:
            o["backfill"]["prefix_note"] = val["prefix_note"]
        if val["dup"]:
            o["backfill"]["same_key_repeat"] = val["dup"]
        if multi_diag:
            # 归属不是原文事实，是约定：留痕，统计时可按规则回溯剔除
            o["backfill"]["assign_note"] = (
                "原文含 %d 条恶性诊断项、侧别无冲突，TNM 归首条" % len(mal))
        if multi:
            o["backfill"]["set_of"] = len(vals)
        return o

    multi = len(vals) > 1
    objs = [_mk(v, multi) for v in vals]
    if multi:
        # 多套：契约「同一病人可同时存在多套」，按前缀各存一个对象
        it["tnm"] = objs
        stat["multi_set_filled"] += 1
    else:
        it["tnm"] = objs[0]

    for val in vals:
        if val["stage"]:
            stat["stage_filled"] += 1
        stat["prefix_dist"][val["prefix"] or "empty"] += 1
        ledger.write(json.dumps({
            "record_id": rec.get("_rid"),
            "diag_name": _s(it.get("name")),
            "quote": val["quote"],
            "source_field": val["source_field"],
            "offset": val["offset"],
            "filled": {"prefix": val["prefix"], "t": val["t"], "n": val["n"],
                       "m": val["m"], "stage": val["stage"]},
            "set_of": len(vals),
        }, ensure_ascii=False) + "\n")

    stat["backfilled"] += 1
    stat["sets_filled"] += len(vals)
    if multi_diag:
        stat["multi_diag_filled"] += 1
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份（抽样试跑）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出 tar")
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--out-tar", default=OUT_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--jsonl", default=JSONL, metavar="PATH")
    a = ap.parse_args()

    if not os.path.exists(a.src_tar):
        raise SystemExit("缺输入包 %s" % a.src_tar)
    if not os.path.exists(a.jsonl):
        raise SystemExit("缺原文 %s（回填只能回原文，缺则不出结果）" % a.jsonl)

    n_src = count(a.src_tar)
    print("输入：%s（%d 份）" % (os.path.basename(a.src_tar), n_src))

    if not a.dry_run:
        if os.path.exists(a.out_tar) and not a.force:
            raise SystemExit("输出 tar 已存在，加 --force 覆盖：%s" % a.out_tar)
        os.makedirs(os.path.dirname(a.out_tar) or ".", exist_ok=True)
        os.makedirs(OUTD, exist_ok=True)

    # ---- 第一遍：原文侧建索引（只留含三段式 TNM 的记录）----
    t0 = time.time()
    index = {}
    n_line = 0
    with open(a.jsonl, encoding="utf-8") as f:
        for line in f:
            n_line += 1
            r = json.loads(line)
            found, n_hit = scan_record(r)
            if n_hit:
                index[str(r.get("record_id"))] = (found, n_hit)
    print("原文 %d 行，含三段式 TNM 的记录 %d 条（%.1fs）"
          % (n_line, len(index), time.time() - t0))

    # ---- 第二遍：产物侧回填 ----
    stat = collections.defaultdict(int)
    stat["prefix_dist"] = collections.Counter()

    ledger_path = os.path.join(OUTD, "tnm_backfill_ledger.jsonl")
    tf_out = tarfile.open(a.out_tar, "w") if (a.out_tar and not a.dry_run) else None
    ledger_file = None if a.dry_run else open(ledger_path, "w", encoding="utf-8")
    ledger = ledger_file if ledger_file is not None else io.StringIO()

    def _emit(fn, payload):
        ti = tarfile.TarInfo("./" + fn)
        ti.size = len(payload)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tf_out.addfile(ti, io.BytesIO(payload))

    n_rec = 0
    t1 = time.time()
    for fn, rec in iter_records(a.src_tar, limit=a.limit):
        n_rec += 1
        rid = fn.split("/")[-1].replace(".json", "")
        rec["_rid"] = rid
        backfill_record(rec, index.get(rid), stat, ledger)
        rec.pop("_rid", None)
        if not a.dry_run:
            _emit(fn, json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))

    el = time.time() - t1
    if ledger_file is not None:
        ledger_file.close()
    if tf_out is not None:
        tf_out.close()

    print("\n记录 %d / 回填 %d 条（%d 套，并存多套 %d 条，多诊断项归首条 %d 条）/ 带分期 %d / 新建 tnm 槽位 %d / 用时 %.1fs"
          % (n_rec, stat["backfilled"], stat["sets_filled"], stat["multi_set_filled"],
             stat["multi_diag_filled"], stat["stage_filled"], stat["slot_created"], el))
    print("前缀分布：%s" % dict(stat["prefix_dist"].most_common()))
    skips = {k[5:]: v for k, v in stat.items() if k.startswith("skip_")}
    print("跳过：%s" % dict(sorted(skips.items(), key=lambda x: -x[1])))

    if not a.dry_run:
        summary = {
            "generated_by": "05_agents/backfill_tnm_from_text.py",
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": a.src_tar, "out": a.out_tar, "jsonl": a.jsonl,
            "n_record": n_rec,
            "n_text_with_tnm": len(index),
            "n_backfilled": stat["backfilled"],
            "n_sets_filled": stat["sets_filled"],
            "n_multi_set": stat["multi_set_filled"],
            "n_multi_diag_assigned": stat["multi_diag_filled"],
            "n_stage_filled": stat["stage_filled"],
            "n_slot_created": stat["slot_created"],
            "skip_detail": dict(sorted(skips.items(), key=lambda x: -x[1])),
            "prefix_dist": dict(stat["prefix_dist"].most_common()),
            "ledger": ledger_path,
            "rule": ("原文三段式完整TNM + 同一前缀下串型唯一 + 恶性诊断项唯一"
                     "（多条时须侧别无冲突，归首条并记 assign_note）+ 目标tnm五字段全空；"
                     "多套按前缀并存；不查分期表、不推亚期、不覆盖已有值"),
            "elapsed_sec": round(el, 1),
        }
        fp = os.path.join(OUTD, "tnm_backfill_summary.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("✓ tar %s  %.1f MB" % (a.out_tar, os.path.getsize(a.out_tar) / 1048576))
        print("✓ 摘要 %s" % fp)
        print("✓ 账本 %s" % ledger_path)
    else:
        print("\n（dry-run，未落盘）")

if __name__ == "__main__":
    main()
