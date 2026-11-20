# -*- coding: utf-8 -*-
import argparse
import collections
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records, count, write_json  # noqa: E402

SRC_TAR = DATA + "/中间产物/_packed/assembled_v7_base.tar"  # 链起点：assemble 产物
SRC_DIR = DATA + "/中间产物/assembled_v7_enum"
SRC_TAR_DEFAULT = SRC_TAR
SRC_DIR_DEFAULT = SRC_DIR
DST = DATA + "/中间产物/assembled_v8"
OUTD = DATA + "/中间产物/_norm/ihc_call"

# 治疗决策型恰此四种（契约 3.5.2）。其余 29 个诊断鉴别型不出 call。
TREAT = {"ER": "ER", "PR": "PR", "HER2": "HER2", "KI67": "Ki67"}

_DASH = r"[-~～—–至到]"

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return "|".join(_s(x) for x in v) if isinstance(v, list) else json.dumps(v, ensure_ascii=False)
    return str(v).strip()

def marker_key(name):
    n = re.sub(r"[\s\-_·．.]", "", _s(name)).upper()
    if not n:
        return None
    if n.startswith("KI67") or n == "KI":
        return "Ki67"
    if n.startswith("HER2") or n.startswith("HER") or "CERBB" in n or n == "C-ERB-B2":
        return "HER2"
    if n.startswith("ER") and not n.startswith("ERCC"):
        return "ER"
    if n.startswith("PR"):
        return "PR"
    return None

def parse_percent(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return (float(v), float(v), "num")
    s = _s(v)
    if not s:
        return None
    # 单侧界限：＜5% / <5% / ≤5%
    m = re.match(r"^[<＜≤]\s*(\d+(?:\.\d+)?)", s)
    if m:
        return (0.0, float(m.group(1)), "lt")
    m = re.match(r"^[>＞≥]\s*(\d+(?:\.\d+)?)", s)
    if m:
        return (float(m.group(1)), 100.0, "gt")
    # 区间：3%-5% / 3~5%
    m = re.match(r"(\d+(?:\.\d+)?)\s*%%?\s*%s\s*(\d+(?:\.\d+)?)" % _DASH, s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (min(a, b), max(a, b), "range")
    # 单个数字
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        # `3+` 这类是分级：有 + 而无 % 时不当百分比
        if "%" not in s and re.search(r"\d\s*\+", s):
            return None
        x = float(m.group(1))
        return (x, x, "num")
    return None

def derive_er_pr(lo, hi):
    if lo >= 1.0:
        return "阳性", "%.0f%%-%.0f%% ≥1%%" % (lo, hi) if lo != hi else "%.0f%% ≥1%%" % lo
    if hi < 1.0:
        return "阴性", "%.0f%%-%.0f%% <1%%" % (lo, hi) if lo != hi else "%.0f%% <1%%" % lo
    return "不确定", "区间跨 1% 阈值"

def derive_her2(m, score, raw):
    txt = _s(score) + "|" + _s(raw)

    # ① ISH 已给结论时以 ISH 为准。含「未扩增」的要先判，否则「扩增」会把它反判成阳性。
    if re.search(r"ISH|FISH|原位杂交|扩增", txt):
        if re.search(r"未扩增|无扩增|未见扩增", txt):
            return "阴性", "ISH 未见扩增"
        if re.search(r"扩增", txt):
            return "阳性", "ISH 见扩增"

    # ② score 严格形态（含单个数字：`0` `1` `2` `3`）
    sc = re.sub(r"\s", "", _s(score))
    if sc in ("0", "0+", "1", "1+", "-", "阴性", "(-)"):
        return "阴性", (sc if sc != "-" else "0/1+")
    if sc in ("2", "2+"):
        return "不确定", "2+ 需 ISH"
    if sc in ("3", "3+"):
        return "阳性", "3+"

    # ③ 复合形态（`2+/3+`、`1+~2+`）走正则
    has3 = re.search(r"(?<!\d)3\s*\+", txt)
    has2 = re.search(r"(?<!\d)2\s*\+", txt)
    has01 = re.search(r"(?<!\d)([01])\s*\+", txt)
    if has3 and has2:
        return "不确定", "并列 2+/3+"
    if has3:
        return "阳性", "3+"
    if has2:
        return "不确定", "2+ 需 ISH"
    if has01:
        return "阴性", has01.group(1) + "+"
    return None, None

def derive_ki67(lo, hi):
    if hi < 20.0:
        return "低增殖", "全部 <20%"
    if lo >= 20.0:
        return "高增殖", "全部 ≥20%"
    return "跨20%分界", "区间跨 20% 分界"

_ISH_CTX = re.compile(
    r"(?:HER\s*-?\s*2|FISH|ISH|原位杂交|基因)[^。；，\n]{0,12}?"
    r"(未见扩增|无扩增|未扩增|扩增(?!检测|试验|试剂|探针|于))")

def record_ish(d):
    ax = d.get("auxiliary_exam")
    parts = [json.dumps({k: v for k, v in d.items() if k != "auxiliary_exam"},
                        ensure_ascii=False)]
    if isinstance(ax, dict):
        parts.append(json.dumps({k: v for k, v in ax.items() if k != "ihc"},
                                ensure_ascii=False))
    s = " ".join(parts)
    vals, quote = set(), ""
    for m in _ISH_CTX.finditer(s):
        vals.add("阴性" if m.group(1) != "扩增" else "阳性")
        quote = quote or m.group(0)[:24]
    if len(vals) == 1:
        return vals.pop(), quote
    if len(vals) > 1:
        return None, "记录内 ISH 结论并存"
    return None, ""

def decide(m):
    key = marker_key(m.get("marker_name"))
    if key is None:
        return None
    pct = parse_percent(m.get("percent"))
    out = {}

    if key in ("ER", "PR"):
        if pct:
            c, b = derive_er_pr(pct[0], pct[1])
            out.update(call=c, call_basis=b, call_standard="ASCO-CAP")
        else:
            st = _s(m.get("status"))
            if st in ("阳性", "阴性", "不确定"):
                # 原文只给定性、没给百分比：沿用原文判读，但标明依据是原文而非阈值
                out.update(call=st, call_basis="原文定性（无百分比，未按阈值复算）",
                           call_standard="ASCO-CAP")
            else:
                return None

    elif key == "HER2":
        c, b = derive_her2(m, m.get("score"), m.get("raw_value"))
        if c:
            out.update(call=c, call_basis=b, call_standard="ASCO-CAP")
        else:
            st = _s(m.get("status"))
            if st in ("阳性", "阴性", "不确定"):
                out.update(call=st, call_basis="原文定性（无 IHC 分级，未按阈值复算）",
                           call_standard="ASCO-CAP")
            else:
                return None

    elif key == "Ki67":
        if pct:
            band, b = derive_ki67(pct[0], pct[1])
            out.update(ki67_band=band, call_basis=b, call_standard="St Gallen",
                       call="")   # 显式空：契约要求不压二值
        else:
            return None

    if not out:
        return None
    out["call_src"] = "derived"
    # 冲突不覆盖：原文 status 是事实，派生 call 是按标准的换算，两者并存才可审
    st = _s(m.get("status"))
    if out.get("call") and st in ("阳性", "阴性") and out["call"] != st:
        out["call_conflict"] = True
    return out

# ---------------- 单条记录处理 ----------------

_DRY = False

def _init(dry):
    global _DRY
    _DRY = dry

def _shape(d):
    ax = d.get("auxiliary_exam")
    axk = tuple(sorted(k for k in ax.keys() if k != "ihc")) if isinstance(ax, dict) else ()
    return tuple(sorted(d.keys())), axk

def _deep(o):
    return hashlib.md5(json.dumps(o, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def _one(item, deep=False):
    fn, d = item
    rid = _s(d.get("record_id")) or fn[:-5]

    shape0 = _shape(d)
    body0 = ax0 = None
    if deep:
        body0 = _deep({k: v for k, v in d.items() if k != "auxiliary_exam"})
        ax = d.get("auxiliary_exam")
        if isinstance(ax, dict):
            ax0 = _deep({k: v for k, v in ax.items() if k != "ihc"})

    n_mk = n_call = n_conf = 0
    det = []
    ax = d.get("auxiliary_exam")
    ihc = ax.get("ihc") if isinstance(ax, dict) else None
    panels = ihc.get("panels") if isinstance(ihc, dict) else None
    ish_call, ish_quote = record_ish(d)
    for pi, p in enumerate(panels if isinstance(panels, list) else []):
        if not isinstance(p, dict):
            continue
        markers = p.get("markers")
        for mi, m in enumerate(markers if isinstance(markers, list) else []):
            if not isinstance(m, dict):
                continue
            n_mk += 1
            got = decide(m)
            if not got:
                continue
            # HER2 停在 2+ 而记录别处已给 ISH 结论：以 ISH 为准（只补不确定，不覆盖已有结论）
            if (ish_call and got.get("call") == "不确定"
                    and marker_key(m.get("marker_name")) == "HER2"):
                got.update(call=ish_call, call_basis="记录级 ISH：%s" % ish_quote,
                           call_standard="ASCO-CAP", call_src="derived_ish")
            m.update(got)
            n_call += 1
            if got.get("call_conflict"):
                n_conf += 1
            det.append({"panel": pi, "marker": mi,
                        "name": _s(m.get("marker_name")),
                        "call": got.get("call", ""),
                        "basis": got.get("call_basis", ""),
                        "raw_status": _s(m.get("status")),
                        "conflict": bool(got.get("call_conflict"))})

    bad = None
    if _shape(d) != shape0:
        bad = "shape"
    elif deep:
        if _deep({k: v for k, v in d.items() if k != "auxiliary_exam"}) != body0:
            bad = "top_level"
        elif ax0 is not None and _deep({k: v for k, v in d["auxiliary_exam"].items() if k != "ihc"}) != ax0:
            bad = "auxiliary_exam"

    if not _DRY:
        # 不在子进程里写盘：序列化后交给主进程统一写出（见 _emit）
        payload = json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")
    else:
        payload = None
    return rid, n_mk, n_call, n_conf, det, bad, payload, fn

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份（抽样试跑）")
    ap.add_argument("--workers", type=int, default=1,
                    help="默认单进程，避免并行把整条记录送进进程池导致无界预取")
    ap.add_argument("--deep-check", action="store_true",
                    help="对每份都做逐值深比对（慢，验收时用）")
    ap.add_argument("--force", action="store_true", help="允许写入已存在的输出目录/tar")
    ap.add_argument("--out-tar", default="", metavar="PATH",
                    help="直接输出单个 tar 流（推荐）")
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT, metavar="PATH",
                    help="输入 tar（默认 assembled_v7_base.tar，即派生链起点）")
    ap.add_argument("--src-dir", default=SRC_DIR_DEFAULT, metavar="PATH",
                    help="输入目录（仅当目录存在时做份数交叉校验）")
    a = ap.parse_args()

    src_tar = a.src_tar
    src_dir = a.src_dir

    if not os.path.exists(src_tar):
        raise SystemExit("缺输入包 %s" % src_tar)

    n_tar = count(src_tar)
    # 目录交叉校验只在目录仍在时做（产物统一走 tar）
    # 若无条件 listdir 会让 --src-tar 单模式直接崩，链条无法从头复现。
    if os.path.isdir(src_dir):
        n_dir = len([f for f in os.listdir(src_dir) if f.endswith(".json")])
        print("输入：tar %d 份 / 目录 %d 份%s" % (n_tar, n_dir, "" if n_tar == n_dir else "  ⚠ 不一致！"))
        if n_tar != n_dir:
            raise SystemExit("tar 与目录份数不一致，拒绝在过期输入上派生")
    else:
        print("输入：tar %d 份（目录 %s 不存在，目录模式已废弃，跳过份数交叉校验）" % (n_tar, src_dir))

    if not a.dry_run:
        if a.out_tar:
            if os.path.exists(a.out_tar) and not a.force:
                raise SystemExit("输出 tar 已存在，加 --force 覆盖：%s" % a.out_tar)
            os.makedirs(os.path.dirname(a.out_tar) or ".", exist_ok=True)
        else:
            os.makedirs(DST, exist_ok=True)
            old = [f for f in os.listdir(DST) if f.endswith(".json")]
            if old and not a.force:
                raise SystemExit("输出目录已有 %d 份，加 --force 覆盖" % len(old))
        os.makedirs(OUTD, exist_ok=True)

    t0 = time.time()
    n_rec = n_mk = n_call = n_conf = 0
    bad = []
    conf_samples = []
    by_name = collections.Counter()
    basis_c = collections.Counter()

    tf_out = tarfile.open(a.out_tar, "w") if (a.out_tar and not a.dry_run) else None

    def _emit(fn, payload):
        if payload is None:
            return
        if tf_out is not None:
            ti = tarfile.TarInfo("./" + fn)
            ti.size = len(payload)
            ti.mtime = int(time.time())
            ti.mode = 0o644
            tf_out.addfile(ti, io.BytesIO(payload))
        else:
            with open(os.path.join(DST, fn), "wb") as f:
                f.write(payload)

    def _handle(res):
        nonlocal n_rec, n_mk, n_call, n_conf
        rid, mk, cl, cf, det, err, payload, fn = res
        _emit(fn, payload)
        n_rec += 1
        n_mk += mk
        n_call += cl
        n_conf += cf
        if err:
            bad.append((rid, err))
        for x in det:
            by_name[x["name"]] += 1
            basis_c[x["basis"]] += 1
            if x["conflict"] and len(conf_samples) < 40:
                conf_samples.append((rid, x))

    # 默认单进程。本工序是纯规则匹配，瓶颈在 tar 顺序读（快），并行收益小；
    # 而把整条记录 dict 交给进程池会触发**无界预取**（map 会先把生成器耗尽再提交），
    jobs = iter_records(src_tar, limit=a.limit)
    _init(a.dry_run)
    DEEP_EVERY = 500
    if a.workers <= 1:
        i = 0
        for it in jobs:
            i += 1
            _handle(_one(it, deep=(a.deep_check or i % DEEP_EVERY == 0)))
    else:
        from concurrent.futures import wait, FIRST_COMPLETED
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                 initargs=(a.dry_run,)) as ex:
            inflight, cap, i = set(), a.workers * 4, 0
            for it in jobs:
                i += 1
                inflight.add(ex.submit(_one, it, a.deep_check or i % DEEP_EVERY == 0))
                if len(inflight) >= cap:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for f in done:
                        _handle(f.result())
            for f in inflight:
                _handle(f.result())

    el = time.time() - t0
    print("\n记录 %d / 标志物 %d / 派生 %d（%.1f%%）/ 冲突 %d / 用时 %.1fs"
          % (n_rec, n_mk, n_call, 100.0 * n_call / n_mk if n_mk else 0, n_conf, el))
    print("\n派生分布（按标志物）：")
    for k, v in by_name.most_common(8):
        print("   %-10s %d" % (k, v))
    print("\n判读依据 top10：")
    for k, v in basis_c.most_common(10):
        print("   %-36s %d" % (k[:36], v))
    if conf_samples:
        print("\n原文 status 与派生 call 冲突样例（两者都留，不覆盖）：")
        for rid, x in conf_samples[:10]:
            print("   %-12s %-8s 原文=%-4s 派生=%-4s 依据=%s"
                  % (rid, x["name"], x["raw_status"], x["call"], x["basis"]))
    if tf_out is not None:
        tf_out.close()
        print("\n✓ tar %s  %.1f MB" % (a.out_tar, os.path.getsize(a.out_tar) / 1048576))

    if bad:
        print("\n✗ 越权改写 %d 份：%s" % (len(bad), bad[:5]))
        sys.exit(2)

    if not a.dry_run:
        summary = {
            "generated_by": "05_agents/derive_ihc_call.py",
            "src": src_tar, "dst": a.out_tar or DST,
            "n_record": n_rec, "n_marker": n_mk, "n_derived": n_call,
            "n_conflict": n_conf, "elapsed_s": round(el, 1),
            "by_marker": dict(by_name.most_common()),
            "by_basis": dict(basis_c.most_common(30)),
            "conflict_sample": [dict(rid=r, **x) for r, x in conf_samples],
        }
        with open(os.path.join(OUTD, "ihc_call_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("\n✓ 落盘 %s，摘要 %s" % (a.out_tar or DST, OUTD + "/ihc_call_summary.json"))
    else:
        print("\n（dry-run，未落盘）")

if __name__ == "__main__":
    main()
