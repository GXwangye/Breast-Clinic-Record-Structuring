#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import random
import re
import subprocess
import time
import urllib.request

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
AGENTS = os.path.join(ROOT, "结构化处理脚本/05_agents")
ASM_DIR = os.path.join(DATA, "中间产物/assembled_v7")
ASM_TAR = os.path.join(DATA, "中间产物/_packed/assembled_v7_final.tar")
STATE = os.path.join(DATA, "中间产物/_standardize")
DONE_FILE = os.path.join(STATE, "restore_done.txt")
CYCLES_FILE = os.path.join(STATE, "restore_cycles.jsonl")
QC_FILE = os.path.join(STATE, "restore_qc.jsonl")
PDCA_LOG = os.path.join(STATE, "pdca_log.jsonl")
ENV_KEY = "AUX_LLM_KEY"
API = os.environ.get("OPENAI_BASE_URL", "") + "/chat/completions"
MODEL = "claude-sonnet-4-6"

# ---- Check 阈值（契约第2节）----
CHECK_EVERY = 5          # 每几个新周期体检一次
ANCHOR_HALT = 0.90       # 窗口 anchor 均值 < 90% → HALT（慢性，严于内嵌 80% 单周期）
ERR_WARN, ERR_HALT = 0.08, 0.12
SHRINK_RATIO, SHRINK_STREAK = 0.70, 2   # 每百字条目 < 基线 70%，连续 2 窗 → HALT
BASELINE_CYCLES = 20     # 基线 = 前 20 个 PASS 周期「每百字条目」中位数
# 短病历天然条目少，用绝对条目数比较不公平；且旧基线 9.5 取自早期闸门少、冗余条目多的
# 高冗余期，后续每轮加闸门（空壳/属性支撑/症状支撑/重复去重）后密度回归真实水平，
# 而「条目/记录」同期从 10.3 降到 6.5（-37%），是口径缺陷而非抽取退化。
BASELINE_MIN_CYCLES = 5  # 周期不足此数时，用下列稳态值兜底
BASELINE_FALLBACK = 8.5  # 冷启动基线
SAMPLE_N = 3             # 语义抽检记录数
DEAD_RESTART_AFTER = 720  # 断点文件停写超过此秒数且进程死亡 → 拉起
POLL_SEC = 60

SECTIONS = ("chief_complaint", "present_illness", "past_history", "physical_exam")
BATCH_PAT = "restore_narrative.py"

# 三处都是「值没进对字段」的确定性可机验缺陷，不依赖语义，不占 LLM 额度：
#   1) markers 裸字符串（schema 要求对象，模型偷懒写名字）
#   2) 四大靶标 percent 未抽（原文含百分数但 percent 空）
#   3) 淋巴结跨模块错位（病理 N 分期/伤口/皮肤等塞进 nature）
# 扫描数据源 = assembled_v7 的 auxiliary_exam / physical_exam（跑 tar 流式读，
# 避免 8 万小文件逐 open OOM）。判据复用 derive_ihc_call.marker_key 与
# p3_normalize.norm_nature，**不在此重写**，防口径漂移。
TREAT_MARKERS = ("ER", "PR", "HER2", "Ki67")

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()

def _load_modules():
    import sys as _sys
    _sys.path.insert(0, AGENTS)
    import derive_ihc_call as _D
    import p3_normalize as _P
    return _D, _P

def scan_extract_defects(tar_path=ASM_TAR, limit=0):
    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "结构化处理脚本/00_core"))
    from tarstream import iter_records
    _D, _P = _load_modules()
    st = {
        "bare_markers": 0,        # markers 里的非对象元素数
        "bare_recs": set(),       # 涉及记录数（去重）
        "pct_miss": 0,            # 四大靶标原文含%但 percent 空（真缺陷口径）
        "ln_routed": 0,           # 淋巴结 nature 跨模块错位条目数
        "samples": [],            # 违规样例（前若干条）
    }
    pct_re = re.compile(r"[%％]")
    for name, d in iter_records(tar_path, limit=limit):
        rid = _s(d.get("record_id")) or name[:-5]
        ax = d.get("auxiliary_exam")
        if isinstance(ax, dict):
            ihc = ax.get("ihc")
            if isinstance(ihc, dict):
                for p in (ihc.get("panels") or []):
                    if not isinstance(p, dict):
                        continue
                    for m in (p.get("markers") or []):
                        if not isinstance(m, dict):
                            st["bare_markers"] += 1
                            st["bare_recs"].add(rid)
                            if len(st["samples"]) < 20:
                                st["samples"].append("bare:%s:%r" % (rid, m))
                            continue
                        k = _D.marker_key(m.get("marker_name"))
                        if k not in TREAT_MARKERS:
                            continue
                        if _s(m.get("percent")):
                            continue
                        rv = _s(m.get("raw_value"))
                        if not rv:
                            continue
                        if k == "HER2":
                            continue  # HER2 量纲是 score，percent 本就该空
                        if pct_re.search(rv):
                            # 真缺陷口径：原文含百分号但 percent 没抽（不含裸数字，
                            # 裸数字可能是「80」这种原文本身就无 % 的定性，不猜）
                            st["pct_miss"] += 1
                            if len(st["samples"]) < 20:
                                st["samples"].append("pct:%s:%s:raw=%r" % (rid, k, rv[:30]))
        ln = ((d.get("physical_exam") or {}).get("structured") or {}).get("lymph_nodes")
        for x in (ln if isinstance(ln, list) else []):
            if not isinstance(x, dict):
                continue
            nature = x.get("nature")
            nm, _basis = _P.norm_nature(nature)
            if nm == "不适用":
                st["ln_routed"] += 1
    st["bare_recs"] = len(st["bare_recs"])
    return st

AUDIT_SYS = """你是病历结构化质量审计员（PDCA-Check 语义通道）。给你一条门诊病历的四个叙述板块：每板块含 raw 原文与 structured 抽取结果。逐板块审查：
1) 漏抽：原文中的症状/就诊经历/用药/手术/合并症/查体所见是否被 structured 覆盖（明显的语义级遗漏才算）；
2) 幻觉：structured 中是否有原文不存在的信息（编造的日期、部位、数值、药名）；
3) 错值：字段取值与原文是否矛盾（侧别、时间、阴阳性）；
4) anchor 抽查：任选 2 个条目，其 anchor.quote 是否逐字出现在该板块 raw 中；
5) 搬迁识别（2026-10-19 新增，务必遵守）：按抽取规则，既往手术/治疗/用药信息即使写在主诉或现病史原文里，条目也必须落到 past_history（surgeries / care_history / medications / comorbidities），这类条目带 source_field 标记，其 anchor.field 指向源字段（如「现病史」「主诉」）。
   - 这是既定设计，**不得判为「跨板块串入」「锚点错位」或「幻觉」**；
   - 判漏抽时必须**跨板块核对**：现病史原文里的手术/化疗/放疗/用药信息，若已出现在 past_history 的对应数组中，即为已覆盖，不得因 present_illness 无条目就判「成片漏抽」；
   - 反之，同一份信息不得因「在这板块没找到」和「在那板块看到异源锚点」被判两次错。
   - 区分「既往治疗」与「本次/现行治疗」：**仅当原文带既往标志（术后X天/月/年、曾行、已行、已完成、既往、已用、X年前/月前）时才必须落 past_history**；若原文带本次/现行标志（现为行/目前/本次/第N周期化疗/新辅助治疗中/需行/拟行/行第一次），则该信息属现病史/治疗板块，落在现病史 care_history 或 treatment 即为正确，**不得判为迁移遗漏或成片漏抽**（例：主诉「新辅助治疗2天余」、现病史「现为行第一次PCbHP方案第一周期化疗」属本次进行的治疗，正确落在现病史，不判遗漏）。
   - 区分「就诊目的」与「治疗」：care_history.kind 取值域只有 检查/诊断/治疗（规则6），就诊目的句（来诊/复诊/看结果/取药/复查/为进一步就诊等，无具体检查或治疗动作）按规则22/37 统一标 kind=治疗 是既定设计，**不得判为「分类错置」或「幻觉」**；判漏抽时，主诉里的就诊目的（如「复诊」）若已由现病史对应 care_history 条目覆盖，不得因主诉无独立条目而判漏抽（例：主诉「乳腺疼痛4月余，复诊」、现病史「来诊看结果」已抽为 care_history(kind=治疗)，即为已覆盖，不判漏抽/错置）。
只输出 JSON：{"verdict": "ok|warn|halt", "issues": ["板块:问题描述", ...]}
判定口径：单条记录个别小瑕疵=ok或warn；出现编造信息、或成片漏抽（整段关键信息无条目，且经跨板块核对确认确实无条目覆盖）=halt。"""

def log(rec):
    rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(PDCA_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)

def load_cycles():
    out = []
    if not os.path.exists(CYCLES_FILE):
        return out
    for line in open(CYCLES_FILE, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

def load_done():
    if not os.path.exists(DONE_FILE):
        return []
    return [l.strip() for l in open(DONE_FILE, encoding="utf-8") if l.strip()]

def structural_check():
    if not os.path.exists(ASM_TAR):
        return {}, ["结构性扫描跳过：缺 %s" % ASM_TAR]
    st = scan_extract_defects(ASM_TAR)
    stats = {
        "bare_markers": st["bare_markers"],
        "bare_recs": st["bare_recs"],
        "pct_miss": st["pct_miss"],
        "ln_routed": st["ln_routed"],
    }
    issues = []
    if st["bare_markers"]:
        issues.append("markers裸字符串 %d 例/%d 记录" % (st["bare_markers"], st["bare_recs"]))
    if st["pct_miss"]:
        issues.append("四大靶标 percent 未抽 %d 条" % st["pct_miss"])
    if st["ln_routed"]:
        issues.append("淋巴结跨模块错位 %d 条" % st["ln_routed"])
    return stats, issues

def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

# ---------------- 确定性通道 ----------------
def det_check(window, baseline):
    issues = []
    if not window:
        return {}, "continue", issues    # 连接类异常不计入质量（跑批内嵌闸门已自愈重试，那是网络问题不是抽取问题）
    # 条目/记录按成功记录折算（失败记录天然 0 条目，不折算会被网络故障拉垮）
    anchor = [c["anchor_hit"] for c in window if c.get("size")]
    err_rate = [max(0, c["errors"] - c["conn_errors"]) / c["size"]
                for c in window if c.get("size")]
    ipr = [c["items"] / max(1, c["size"] - c["errors"])
           for c in window if c.get("size")]
    ipc = [100.0 * c["items"] / c["chars"] for c in window if c.get("chars")]
    stats = {
        "cycles": len(window),
        "records": sum(c["size"] for c in window),
        "anchor_mean": round(sum(anchor) / len(anchor), 3) if anchor else None,
        "err_rate_mean": round(sum(err_rate) / len(err_rate), 3) if err_rate else None,
        "items_per_record": round(median(ipr), 2) if ipr else None,
        "items_per_100chars": round(median(ipc), 2) if ipc else None,
    }
    verdict = "continue"
    if stats["anchor_mean"] is not None and stats["anchor_mean"] < ANCHOR_HALT:
        verdict = "halt"
        issues.append("窗口anchor均值%.0f%%<%.0f%%（慢性漂移）" %
                      (100 * stats["anchor_mean"], 100 * ANCHOR_HALT))
    if stats["err_rate_mean"] is not None:
        if stats["err_rate_mean"] > ERR_HALT:
            verdict = "halt"
            issues.append("窗口异常率均值%.0f%%>%.0f%%" % (100 * stats["err_rate_mean"], 100 * ERR_HALT))
        elif stats["err_rate_mean"] > ERR_WARN:
            issues.append("窗口异常率均值%.0f%%偏高(%.0f%%~%.0f%%)" %
                          (100 * stats["err_rate_mean"], 100 * ERR_WARN, 100 * ERR_HALT))
    if baseline and stats["items_per_100chars"] is not None:
        if stats["items_per_100chars"] < SHRINK_RATIO * baseline:
            issues.append("每百字条目 %.2f 低于基线 %.2f 的 %d%%" %
                          (stats["items_per_100chars"], baseline, int(100 * SHRINK_RATIO)))
    return stats, verdict, issues

# ---------------- 语义通道 ----------------
# content 为空（「审计输出为空，按WARN处理」），语义通道长期空转；加入搬迁识别条款后
# 提示变长、更易触发。此改动只放开输出额度、不动任何判据与阈值。
def chat(key, user_text, max_tokens=20000):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": AUDIT_SYS},
                     {"role": "user", "content": user_text}],
        "max_tokens": max_tokens, "temperature": 0,
    }).encode("utf-8")
    last = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            msg = str(e)
            # 限流/服务端故障：长退避（高峰期 429 常见，短退避不够用）
            if "429" in msg or "500" in msg or "502" in msg or "503" in msg or "504" in msg:
                time.sleep(min(300, 60 * (attempt + 1)))
            else:
                time.sleep(20)
    raise last

def parse_verdict(raw):
    raw = re.sub(r"<(?:thinking|think)>[\s\S]*?</(?:thinking|think)>", "", raw).strip()
    if not raw:
        return {"verdict": "warn", "issues": ["审计输出为空（thinking 吃满额度），按WARN处理"]}
    i = raw.find("{")
    j = raw.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(raw[i:j + 1])
        except Exception:
            pass
    return {"verdict": "warn", "issues": ["审计输出不可解析，按WARN处理: " + raw[:100]]}

def build_audit_payload(rid):
    p = os.path.join(ASM_DIR, rid + ".json")
    asm = json.load(open(p, encoding="utf-8"))
    parts = ["记录 %s" % rid]
    for sec in SECTIONS:
        v = asm.get(sec) or {}
        # 审计 LLM 看到残缺 JSON 误报「成片漏抽」触发假 HALT。
        raw_txt = str(v.get("raw", "") or "")
        st = v.get("structured") or {}
        st_txt = json.dumps(st, ensure_ascii=False)
        parts.append("〈%s〉原文：%s\n抽取：%s" % (sec, raw_txt or "（空）", st_txt or "（空）"))
    return "\n\n".join(parts)

def semantic_check(key, rids):
    rnd = random.Random(int(time.time()))
    sample = rnd.sample(rids, min(SAMPLE_N, len(rids)))
    payloads = []
    for rid in sample:
        try:
            payloads.append(build_audit_payload(rid))
        except Exception as e:
            return {"verdict": "warn", "issues": ["样例 %s 读取失败: %s" % (rid, e)], "rids": sample}
    raw = chat(key, "\n\n=====\n\n".join(payloads))
    v = parse_verdict(raw)
    v.setdefault("verdict", "warn")
    v.setdefault("issues", [])
    if v["verdict"] not in ("ok", "warn", "halt"):
        v["verdict"] = "warn"
    v["rids"] = sample
    return v

# ---------------- Act ----------------
def batch_alive():
    r = subprocess.run(["pgrep", "-f", BATCH_PAT], capture_output=True)
    return r.returncode == 0

def kill_batch():
    subprocess.run(["pkill", "-f", BATCH_PAT], capture_output=True)

def restart_batch(key):
    env = dict(os.environ)
    env[ENV_KEY] = key
    with open(os.path.join(STATE, "pdca_restarts.txt"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    subprocess.Popen(
        [sys.executable, "-u",
         os.path.join(AGENTS, "restore_narrative.py"), "--workers", "48"],
        env=env, cwd=AGENTS, start_new_session=True,
        stdout=open(os.path.join(STATE, "pdca_batch_stdout.log"), "a"),
        stderr=subprocess.STDOUT)
    return True

def done_mtime_age():
    try:
        return time.time() - os.path.getmtime(DONE_FILE)
    except OSError:
        return 1e9

# ---------------- 主循环 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次体检后退出（验证用）")
    args = ap.parse_args()
    key = os.environ.get(ENV_KEY, "")
    if not key:
        print("ERROR: set %s" % ENV_KEY)
        return

    total = len([f for f in os.listdir(ASM_DIR) if f.endswith(".json")])
    checked_cycles = len(load_cycles())   # 从当前已积累周期之后开始计窗
    baseline = None
    shrink_streak = 0
    warn_streak = 0

    while True:
        cycles = load_cycles()
        done = load_done()

        # 完成
        if len(done) >= total:
            log({"phase": "act", "event": "complete",
                 "done": len(done), "total": total})
            return

        last_verdict = cycles[-1].get("verdict") if cycles else ""
        if args.once:
            window = [c for c in cycles if c.get("verdict") == "PASS"][-CHECK_EVERY:]
            base_c = [c for c in cycles if c.get("verdict") == "PASS" and c.get("chars")]
            baseline_v = median([100.0 * c["items"] / c["chars"]
                                 for c in base_c[:BASELINE_CYCLES]]) if len(base_c) >= BASELINE_MIN_CYCLES else BASELINE_FALLBACK
            stats, dv, di = det_check(window, baseline_v)
            sv = semantic_check(key, done[-300:])
            sstats, sissues = structural_check()
            log({"phase": "check", "mode": "once", "stats": stats,
                 "det": dv, "det_issues": di, "semantic": sv,
                 "structural": sstats, "structural_issues": sissues})
            return

        # ---- Do：进程守护 ----
        if not batch_alive():
            if last_verdict == "HALT":
                log({"phase": "act", "event": "batch_halted_by_gate",
                     "msg": "跑批被内嵌闸门停批，PDCA不重启，待人工", "last_cycle": cycles[-1]})
                return
            if done_mtime_age() > DEAD_RESTART_AFTER:
                restart_batch(key)
                log({"phase": "act", "event": "batch_restarted",
                     "done": len(done), "total": total})
            # 刚被拉起或暂不可判：跳过本轮后续，等下一轮
            time.sleep(POLL_SEC)
            continue

        # ---- Check：窗口体检 ----
        new_cycles = cycles[checked_cycles:]
        if len(new_cycles) >= CHECK_EVERY:
            window = new_cycles[:len(new_cycles) // CHECK_EVERY * CHECK_EVERY]
            checked_cycles += len(window)
            pass_cycles = [c for c in cycles if c.get("verdict") == "PASS"]
            if baseline is None:
                withchars = [c for c in pass_cycles if c.get("chars")]
                if len(withchars) >= BASELINE_CYCLES:
                    baseline = median([100.0 * c["items"] / c["chars"]
                                       for c in withchars[:BASELINE_CYCLES]])
                    log({"phase": "check", "event": "baseline_set",
                         "baseline_per100chars": baseline, "src": "cycles"})
                elif len(withchars) >= BASELINE_MIN_CYCLES:
                    baseline = BASELINE_FALLBACK
                    log({"phase": "check", "event": "baseline_set",
                         "baseline_per100chars": baseline, "src": "fallback",
                         "note": "新口径周期不足%d，用实测稳态兜底" % BASELINE_CYCLES})
            stats, dv, di = det_check(window, baseline)
            sv = semantic_check(key, done[-300:])
            # 抽取层三处缺陷的结构完整性扫描（确定性通道，零 LLM）
            sstats, sissues = structural_check()
            # 漂移连续计数
            shrunk = baseline and stats.get("items_per_100chars") and \
                stats["items_per_100chars"] < SHRINK_RATIO * baseline
            shrink_streak = shrink_streak + 1 if shrunk else 0
            if shrink_streak >= SHRINK_STREAK:
                dv = "halt"
                di.append("每百字条目连续 %d 窗低于基线 %.2f 的 %d%%" %
                          (shrink_streak, baseline, int(100 * SHRINK_RATIO)))
            # 汇总裁决
            verdict = dv
            if sv["verdict"] == "halt":
                verdict = "halt"
            issues = di + ["语义:" + s for s in sv["issues"]] + \
                ["结构:" + s for s in sissues]
            if verdict == "continue" and sv["verdict"] == "warn":
                warn_streak += 1
                if warn_streak >= 2:
                    issues.append("连续 2 次语义 WARN，升级为人工关注项")
            else:
                warn_streak = 0
            log({"phase": "check", "event": "window_audit",
                 "window": {"from_cycle": window[0].get("cycle"), "to_cycle": window[-1].get("cycle")},
                 "done": len(done), "total": total,
                 "stats": stats, "semantic": {k: sv[k] for k in ("verdict", "issues", "rids")},
                 "structural": sstats,
                 "verdict": verdict, "issues": issues})
            # ---- Act ----
            if verdict == "halt":
                kill_batch()
                log({"phase": "act", "event": "pdca_halt", "reason": issues,
                     "msg": "PDCA 终止跑批，等待人工"})
                return

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
