# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
ASM = DATA + "/中间产物/assembled_v7"
AGT = DATA + "/中间产物/agents_v7"
# 本脚本自此为纯原文直抽，靶集判据改由 remediate_scope_index.py 的 IHC_KW 从输入层原文判定。
INPUT = DATA + "/中间产物/_agents_input/input_records.jsonl"
OUT_DIR = DATA + "/中间产物/_remediate"
BAK = DATA + "/中间产物/_remediate_bak/aux"
DONE = OUT_DIR + "/aux_done.txt"
CYCLE_LOG = OUT_DIR + "/aux_cycles.jsonl"
QC = OUT_DIR + "/aux_qc_dropped.jsonl"

BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
MODEL = "claude-sonnet-4-6"
# 起始步长 50：成功率已稳定在 83%+ 且连续多期零停批，再把前 400 条耗在步长爬坡上
# 会让高并发线程长时间空转（瓶颈是端到端延迟，不是产出质量）。
CYCLE_START, CYCLE_MAX, GROW_AFTER = 50, 100, 2
ANCHOR_HIT_MIN = 0.80   # 用户批准：仅作系统性失败信号
ERR_RATE_MAX = 0.10     # 周期记录级异常率上限
SCOPE_INDEX = OUT_DIR + "/scope_index.json"

LAT_OK = {"右", "左", "双", "未指明"}
DATE_RE = re.compile(r"^\d{4}(-\d{1,2}(-\d{1,2})?)?$")  # YYYY / YYYY-MM / YYYY-MM-DD，部分日期保留不丢
EXAM_KW = re.compile(r"超声|彩超|钼靶|磁共振|MRI|CT|胸片|骨密度|心电图|病理|活检|免疫组化|FISH|穿刺|BI-RADS")
# 「标志物名后必须跟括号」用于排除心肌酶 CK（肌酸激酶）被判成细胞角蛋白。
_MARKERS = (r"(?:ER|PR|HER-?2|C-?erbB-?2|Ki-?\s*67|P53|P63|CK5/6|CK|EGFR|E-?Ca|P120|"
            r"CD10|CD68|CD31|Calponin|AR|SMMHC|p40|GATA3|Mammaglobin|GCDFP-?15|TRPS1)")
IHC_KW = re.compile(
    r"免疫组化"
    r"|(?:" + _MARKERS + r")\s*[*＊#]?\s*[（(]"
    r"|(?:ER|PR)\s*[*＊#]?\s*[强弱]?\s*(?:阳性|阴性)"
    r"|HER-?\s*2\s*[（(]?\s*(?:\d|[＋+])",
    re.I)
# 连接类基础设施异常：服务端宕机/网络抖动。这类错误与抽取质量无关，
# 不应触发质量停批——等待恢复后重跑本批即可。
CONN_RE = re.compile(r"Connection refused|Connection reset|timed out|Remote end closed|"
                     r"Errno 61|Errno 104|Errno 54|Name or service|Temporary failure|"
                     r"Network is unreachable|EOF occurred|SSL", re.I)
CONN_RETRY_MAX = 10   # 连续重试 10 个周期（约 30 分钟）仍不通才放弃

SYS = """你是辅助检查结构化修缮子智能体。任务：从病历源文中找出【尚未结构化】的辅助检查内容，输出补丁 JSON。

硬规则：
1) 只抽取源文中逐字存在的信息，禁止臆造、禁止用医学常识补全。
2) 每个条目必须带 anchor，且 anchor 必须是 JSON 对象 {"field": "...", "quote": "..."}，禁止写成字符串；field=源字段名（辅助检查/现病史/既往史之一），quote=该条内容在源文中的逐字片段（10~80字，必须能在原文中精确找到，不得改写一字）。
3) exam_date：原文出现的检查日期填 YYYY-MM-DD；原文没有日期就填空字符串 ""。不同日期的检查必须拆成不同条目。
4) 不同器官/部位/侧别的检查拆成不同 reports；body_site 用原文所指器官（如 乳腺/腋窝/颈部/锁骨上/甲状腺/胸壁/肺/骨/肝）；laterality 只允许 右/左/双/未指明，拿不准填 未指明。
5) reports 条目字段：exam_name, exam_date, body_site, laterality, findings(数组,可空), impressions(数组,可空), grading_system(如 BI-RADS,可空), grading(如 4A,可空), anchor。
6) labs 条目字段：name, value, unit(可空), flag(高/低/正常/可空), exam_date, anchor。
7) 免疫组化进 ihc_panels：specimen(标本部位,可空), exam_date, markers 数组，每个 marker 字段：marker(标准名如 ER/PR/HER2/Ki67/CK5/6/P63/Calponin/P120/E-cadherin/CK/P53/GATA-3 等), value(原文取值如 +/2+/阳性/阴性/少许+), percent(如 90%,无则空), intensity(如 强/中/弱,无则空)。markers 禁止为空——没有标志物就不要创建该 panel。
8) 【已结构化摘要】里列出的内容禁止重复抽取。确实没有新内容时输出 {"no_new": true, "reports": [], "labs": [], "ihc_panels": []}。
9) 只输出一个合法 JSON 对象，禁止任何解释文字，禁止 ``` 围栏。字符串内引号换行必须转义。"""

PATCH_KEYS = ("reports", "labs", "ihc_panels")

try:
    import demjson3
    _HAVE_DEMJSON = True
except Exception:
    _HAVE_DEMJSON = False

_lock = threading.Lock()

# 服务端默认强制开启 thinking，思考段同样计入 max_tokens。
# 大量条目被隔离进 qc 的主因，因此首次调用就给足额度。
REQ_TIMEOUT = 240          # 生成 20k token 需要时间，超时过短会把正常慢请求误杀
MAX_TOKENS = 20000

def chat(key, user_text, max_tokens=MAX_TOKENS, retry=3):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user_text}],
        "max_tokens": max_tokens, "temperature": 0,
    }).encode("utf-8")
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(BASE_URL + "/chat/completions", data=body,
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):  # 某些服务端 content 为分片数组
                content = "".join((p.get("text", "") if isinstance(p, dict) else str(p)) for p in content)
            if not content:
                raise ValueError("响应缺content: " + json.dumps(d, ensure_ascii=False)[:200])
            return content
        except Exception as e:
            last = e
            if i < retry - 1:
                time.sleep(2 + 4 * i)   # 退避：限流时留出间隔，避免雪崩
    raise last

def parse_json(raw):
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()  # 剥离思考段
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a >= 0 and b > a:
        raw = raw[a:b + 1]
    if not raw:
        raise ValueError("空响应(仅思考段,无JSON)")
    try:
        return json.loads(raw)
    except Exception:
        if _HAVE_DEMJSON:
            return demjson3.decode(raw)
        raise

FIELD_PRIORITY = ("辅助检查", "现病史", "既往史")

def locate_verbatim(field_text, quote):
    if quote in field_text:
        return quote
    qs = re.sub(r"\s+", "", quote)
    if not qs:
        return None
    fs_chars, idx_map = [], []
    for i, ch in enumerate(field_text):
        if not ch.isspace():
            fs_chars.append(ch)
            idx_map.append(i)
    fs = "".join(fs_chars)
    pos = fs.find(qs)
    if pos < 0:
        return None
    return field_text[idx_map[pos]:idx_map[pos + len(qs) - 1] + 1]

def validate_item(kind, it, fields_text):
    an = it.get("anchor")
    if isinstance(an, str):
        s = an
        if "quote=" in s:  # LLM 的查询串形态: "field=现病史&quote=逐字片段"
            mf = re.search(r"field=([^&]*)", s)
            an = {"field": mf.group(1) if mf else "", "quote": s.split("quote=", 1)[1]}
        else:
            an = {"field": "", "quote": s}
    if not isinstance(an, dict):
        an = {}
    f, q = an.get("field", ""), an.get("quote", "")
    if not q:
        # quote 缺失时的保守修复：依次用 specimen/exam_name/name（含 /／,，、 分段逐个试）
        # 定位源文区域，截取逐字片段作锚
        cands = []
        for kn in ("specimen", "exam_name", "name"):
            v0 = str(it.get(kn, "") or "")
            if len(v0) >= 2:
                segs = [s for s in re.split(r"[/／,，、]", v0) if len(s) >= 2]
                cands.extend(segs if segs else [v0[:20]])
        for spec in cands:
            hits = [(k, v.find(spec)) for k, v in fields_text.items() if spec in v]
            if hits:
                k, idx = hits[0]
                v = fields_text[k]
                an["field"] = k
                an["quote"] = v[max(0, idx - 10):idx + 50]
                it["anchor"] = an
                f, q = k, an["quote"]
                break
        if not q:
            return "anchor.quote空"
    if f not in fields_text:
        hits = [k for k in FIELD_PRIORITY if k in fields_text and locate_verbatim(fields_text[k], q)]
        if hits:
            f = hits[0]
            an["field"] = f
        else:
            return "anchor.field非法:" + str(f)
    v = locate_verbatim(fields_text[f], q)
    if v is None:
        return "anchor未命中"
    if v != q:
        an["quote"] = v  # 回映为源文逐字片段
    it["anchor"] = an
    ed = it.get("exam_date", "")
    if ed and not DATE_RE.match(ed):
        return "exam_date形态错:" + str(ed)
    if kind == "reports":
        if it.get("laterality", "未指明") not in LAT_OK:
            return "laterality越界"
        if not it.get("body_site"):
            return "body_site空"
    if kind == "labs" and not it.get("name"):
        return "lab.name空"
    if kind == "ihc_panels":
        ms = it.get("markers") or []
        if not ms:
            return "panel.markers空"
        for m in ms:
            if not m.get("marker"):
                return "marker空"
    return None

def build_user_text(rid, rec, asm):
    f = rec.get("fields") or {}
    parts = ["【病历ID】%s" % rid, "【源文字段原文】"]
    for k in ("辅助检查", "现病史", "既往史"):
        v = (f.get(k) or "").strip()
        if len(v) > 6000:  # 截断防爆 token（辅检内容通常在文首/文中前部）
            v = v[:6000] + "…[截断]"
        parts.append("〈%s〉\n%s" % (k, v if v else "（空）"))
    # 该候选随规则路线删除；现为纯原文直抽，标志物由模型直接从上面三段原文中识别。
    aux = asm.get("auxiliary_exam") or {}
    ihc = aux.get("ihc") if isinstance(aux.get("ihc"), dict) else {}
    done = []
    for r in (aux.get("reports") or [])[:20]:
        if not isinstance(r, dict):
            continue
        done.append("报告:%s|%s|%s|%s" % (r.get("exam_name", ""), r.get("body_site", ""),
                                          r.get("laterality", ""), r.get("exam_date", "")))
    for l in (aux.get("labs") or [])[:20]:
        if not isinstance(l, dict):
            continue
        done.append("检验:%s=%s" % (l.get("name", ""), l.get("value", "")))
    for p in (ihc.get("panels") or [])[:10]:
        if not isinstance(p, dict):
            continue
        done.append("IHC:%s|%s" % (p.get("specimen", ""),
                    ",".join(str(m.get("marker", "")) for m in (p.get("markers") or []) if isinstance(m, dict))))
    parts.append("【已结构化摘要】（禁止重复）:\n" + ("\n".join(done) if done else "（空）"))
    return "\n\n".join(parts)

def _first_impression(r):
    im = r.get("impressions") or []
    if im and isinstance(im, list):
        return str(im[0])[:30]
    return ""

def merge_patch(rid, patch, write):
    ap = os.path.join(ASM, rid + ".json")
    asm = json.load(open(ap, encoding="utf-8"))
    aux = asm.setdefault("auxiliary_exam", {})
    if not isinstance(aux, dict):
        aux = {}
        asm["auxiliary_exam"] = aux
    reps = aux.setdefault("reports", [])
    labs = aux.setdefault("labs", [])
    ihc = aux.get("ihc")
    if not isinstance(ihc, dict):
        ihc = {}
        aux["ihc"] = ihc
    panels = ihc.setdefault("panels", [])
    reps = [r for r in reps if isinstance(r, dict)]
    labs = [l for l in labs if isinstance(l, dict)]
    panels = [p for p in panels if isinstance(p, dict)]
    aux["reports"], aux["labs"], ihc["panels"] = reps, labs, panels
    exist_r = {(r.get("exam_name"), r.get("body_site"), r.get("exam_date"),
                _first_impression(r)) for r in reps}
    exist_p = {(p.get("specimen", ""), tuple(sorted(str(m.get("marker", "")) if isinstance(m, dict) else str(m)
                                                    for m in (p.get("markers") or []))))
               for p in panels}
    n = {"reports": 0, "labs": 0, "panels": 0}
    for r in patch.get("reports") or []:
        key = (r.get("exam_name"), r.get("body_site"), r.get("exam_date"),
               _first_impression(r))
        if key in exist_r:
            continue
        r["source_patch"] = "aux_remediate_20261011"
        reps.append(r)
        n["reports"] += 1
    for l in patch.get("labs") or []:
        l["source_patch"] = "aux_remediate_20261011"
        labs.append(l)
        n["labs"] += 1
    for p in patch.get("ihc_panels") or []:
        key = (p.get("specimen", ""), tuple(sorted(str(m.get("marker", "")) if isinstance(m, dict) else str(m)
                                                   for m in (p.get("markers") or []))))
        if key in exist_p:
            continue
        p["source_patch"] = "aux_remediate_20261011"
        panels.append(p)
        n["panels"] += 1
    if write and sum(n.values()):
        os.makedirs(BAK, exist_ok=True)
        for p_ in (ap, os.path.join(AGT, rid, "06_auxiliary_exam.json")):
            if os.path.exists(p_):
                bp = os.path.join(BAK, rid + "__" + os.path.basename(p_))
                if not os.path.exists(bp):
                    import shutil
                    shutil.copy2(p_, bp)
        json.dump(asm, open(ap, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        p6 = os.path.join(AGT, rid, "06_auxiliary_exam.json")
        if os.path.exists(p6):
            try:
                sec = json.load(open(p6, encoding="utf-8"))
                sec["auxiliary_exam"] = asm["auxiliary_exam"]
                json.dump(sec, open(p6, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception:
                pass
    return n

def process_one(rid, rec, key, write):
    ap = os.path.join(ASM, rid + ".json")
    asm = json.load(open(ap, encoding="utf-8"))
    user_text = build_user_text(rid, rec, asm)
    try:
        raw = chat(key, user_text)
        patch = parse_json(raw)
    except Exception:
        # 空响应(思考段吞token)/截断JSON/解析失败：提额重试一次；仍败则异常上抛隔离进 qc
        raw = chat(key, user_text, max_tokens=32000, retry=1)
        patch = parse_json(raw)
    if patch.get("no_new"):
        return rid, {"no_new": True, "added": {}, "dropped": []}
    f = rec.get("fields") or {}
    fields_text = {k: (f.get(k) or "") for k in ("辅助检查", "现病史", "既往史")}
    ok_patch, dropped = {}, []
    n_anchor, n_total = 0, 0
    for kind in PATCH_KEYS:
        items = patch.get(kind) or []
        ok_patch[kind] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            n_total += 1
            err = validate_item(kind, it, fields_text)
            if err:
                dropped.append({"kind": kind, "err": err, "item": json.dumps(it, ensure_ascii=False)[:120]})
            else:
                n_anchor += 1
                ok_patch[kind].append(it)
    added = merge_patch(rid, ok_patch, write)
    return rid, {"added": added, "dropped": dropped,
                 "anchor_hit": n_anchor, "total": n_total}

def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--limit", type=int, default=0)
    ap_.add_argument("--workers", type=int, default=2)
    ap_.add_argument("--dry-run", action="store_true", help="只调LLM+校验，不写盘")
    args = ap_.parse_args()
    key = os.environ.get("AUX_LLM_KEY", "")
    if not key:
        print("缺 AUX_LLM_KEY 环境变量")
        sys.exit(2)
    write = not args.dry_run
    os.makedirs(OUT_DIR, exist_ok=True)

    done = set()
    if os.path.exists(DONE):
        done = set(x.strip() for x in open(DONE, encoding="utf-8") if x.strip())

    # 靶点定位：优先用范围索引的 A∪B（已确定性扫好，省 2 小时重扫）；索引缺失时退回全量扫描
    print("加载输入快照...", flush=True)
    recs = {}
    for line in open(INPUT, encoding="utf-8"):
        try:
            d = json.loads(line)
            recs[str(d.get("record_id"))] = d
        except Exception:
            continue
    print("快照 %d 条" % len(recs), flush=True)
    targets = []
    if os.path.exists(SCOPE_INDEX):
        idx = json.load(open(SCOPE_INDEX, encoding="utf-8"))
        pool = sorted(set(idx.get("A_aux_empty") or []) | set(idx.get("B_ihc_unstruct") or []))
        targets = [r for r in pool if r not in done and r in recs and os.path.exists(os.path.join(ASM, r + ".json"))]
        if args.limit:
            targets = targets[: args.limit]
        print("靶点来自范围索引 A∪B: %d 条（跳过已完成 %d）" % (len(targets), len(done)), flush=True)
    else:
        print("范围索引缺失，退回全量扫描靶点...", flush=True)
        for fn in sorted(os.listdir(ASM)):
            if not fn.endswith(".json"):
                continue
            rid = fn[:-5]
            if rid in done or rid not in recs:
                continue
            try:
                a = json.load(open(os.path.join(ASM, fn), encoding="utf-8"))
            except Exception:
                continue
            aux = a.get("auxiliary_exam") or {}
            ihc = aux.get("ihc") or {}
            empty = (len(aux.get("reports") or []) == 0 and len(aux.get("labs") or []) == 0
                     and len(ihc.get("panels") or []) == 0)
            need_ihc = False
            # 缺目录会静默把整类记录判成「不需要补 IHC」）。改为直接扫输入层原文，无外部依赖。
            if len(ihc.get("panels") or []) == 0:
                f = recs[rid].get("fields") or {}
                src_blob = "\n".join(str(f.get(k) or "") for k in ("辅助检查", "现病史", "既往史"))
                need_ihc = bool(IHC_KW.search(src_blob))
            if not empty and not need_ihc:
                continue
            if empty:
                f = recs[rid].get("fields") or {}
                blob = (f.get("辅助检查") or "") + (f.get("现病史") or "")
                if len(blob.strip()) < 2 or not EXAM_KW.search(blob):
                    if not need_ihc:
                        continue
            targets.append(rid)
            if args.limit and len(targets) >= args.limit:
                break
        print("靶点 %d 条（跳过已完成 %d）" % (len(targets), len(done)), flush=True)

    clf = open(CYCLE_LOG, "a", encoding="utf-8")
    qf = open(QC, "a", encoding="utf-8")
    df = open(DONE, "a", encoding="utf-8") if write else None
    cycle_size, streak, n_cycle = CYCLE_START, 0, 0
    conn_retries = 0
    i = 0
    while i < len(targets):
        batch = targets[i:i + cycle_size]
        i += cycle_size
        n_cycle += 1
        c_anchor, c_total, c_added, c_drop, c_nonew, c_err, c_err_conn = 0, 0, 0, 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_one, rid, recs[rid], key, write): rid for rid in batch}
            for fut in as_completed(futs):
                rid = futs[fut]
                try:
                    rid, res = fut.result()
                except Exception as e:
                    c_err += 1
                    if CONN_RE.search(str(e)):
                        c_err_conn += 1
                    qf.write(json.dumps({"rid": rid, "err": "exception:" + str(e)[:200]}, ensure_ascii=False) + "\n")
                    continue
                if res.get("no_new"):
                    c_nonew += 1
                else:
                    c_anchor += res.get("anchor_hit", 0)
                    c_total += res.get("total", 0)
                    c_added += sum((res.get("added") or {}).values())
                    c_drop += len(res.get("dropped") or [])
                    for dp in (res.get("dropped") or []):
                        dp["rid"] = rid
                        qf.write(json.dumps(dp, ensure_ascii=False) + "\n")
                if df:
                    with _lock:   # 逐条落盘，避免周期被慢请求拖住时看不到进度
                        df.write(rid + "\n")
                        df.flush()
        if df:
            df.flush()
        qf.flush()
        hit = c_anchor / c_total if c_total else 1.0
        drop = c_drop / c_total if c_total else 0.0
        verdict, why = "PASS", ""
        if c_err / max(len(batch), 1) > ERR_RATE_MAX:
            verdict, why = "HALT", "周期记录异常率 %.0f%% 越限(>10%%)" % (100 * c_err / len(batch))
        elif c_total >= 10 and hit < ANCHOR_HIT_MIN:
            verdict, why = "HALT", "anchor命中 %.0f%% <80%%（系统性失败信号）" % (100 * hit)
        rec = {"cycle": n_cycle, "size": len(batch), "added_items": c_added, "no_new": c_nonew,
               "anchor_hit": round(hit, 3), "drop": round(drop, 3), "errors": c_err,
               "conn_errors": c_err_conn, "verdict": verdict, "why": why}
        clf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        clf.flush()
        print("周期 %d (size %d): 新增条目 %d, 无新内容 %d, anchor命中 %.0f%%, %s" %
              (n_cycle, len(batch), c_added, c_nonew, 100 * hit, verdict), flush=True)
        # 连接类异常占比极高：服务端/网络问题，非质量失败 —— 不 HALT，等待后重跑本批
        if verdict == "HALT" and c_err and c_err_conn >= 0.9 * c_err and conn_retries < CONN_RETRY_MAX:
            conn_retries += 1
            print("[RETRY] 连接类异常 %d/%d（服务端断连，第 %d/%d 次重试），180 秒后重跑本批" %
                  (c_err_conn, c_err, conn_retries, CONN_RETRY_MAX), flush=True)
            time.sleep(180)
            i -= cycle_size
            n_cycle -= 1
            continue
        conn_retries = 0
        if verdict == "HALT":
            print("[HALT] %s — 停批待修" % why)
            break
        streak += 1
        if streak >= GROW_AFTER and cycle_size < CYCLE_MAX:
            cycle_size = min(cycle_size * 2, CYCLE_MAX)
            streak = 0
            print("  [提速] 步长增至 %d" % cycle_size, flush=True)
    clf.close()
    qf.close()
    if df:
        df.close()
    print("[DONE]")

if __name__ == "__main__":
    main()
