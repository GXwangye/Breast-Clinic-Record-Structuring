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
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")
from tarstream import iter_records, count  # noqa: E402
# 值域写法归一（确定性事务）：触诊淋巴结 nature 的阴性符号 → 契约值「未见异常」、
# 治疗方式/诊断 setting/侧别/分级体系拼写的枚举归一。放在本工序末尾，与「按契约补齐
# 已定义字段」同一职责——都是把产物对齐到契约值域，不新增闭集、不做语义判断。
from clean_enum_values import clean_record  # noqa: E402

PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
# 链上末位：输入为 orders.kind 归一后的中间态 _kind.tar，输出即终态 final.tar。
#  本步之前；重建时 kind 步改落 _kind.tar，故本步输入相应指向它。）
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_kind.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final.tar"
OUTD = DATA + "/中间产物/_qa_out"

# ---- 契约值域（§11.1 / §3.6），改这里必须先改契约 ----
SEX_MAP = {"1": "男", "2": "女"}                      # GB/T 2261.1
PREFIX_OK = {"c", "p", "r", "y", "u"}                 # §3.6 前缀闭集
T_VALID = {"0", "IS", "1", "1A", "1B", "1C", "2", "3", "4", "4A", "4B", "4C", "4D", "X"}
N_VALID = {"0", "1MI", "1A", "1B", "1", "2", "2A", "3", "3A", "X"}
M_VALID = {"0", "1", "X"}
DIM_VALID = {"t": T_VALID, "n": N_VALID, "m": M_VALID}

# 「没写」不等于「阴性」——三态显式铁律：这些一律置空，不塞进任何值域
UNSPEC = {"", "未提及", "未指明", "未标注", "未填", "空", "none", "null", "无", "-", "—", "/", "∅"}

FW_ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V", "Ⅵ": "VI"}

# 中文分期（§3.6：以罗马数字期入 stage，中文形式入 stage_raw_cn）
CN_STAGE_RE = re.compile(r"局部晚期|中晚期|晚期|中期|早期")

# 假分期陷阱（§3.6 必拦）：罗马数字期后 4 字内含术式词 → 手术分期，非肿瘤分期
FAKE_SURG_RE = re.compile(r"[ⅠⅡⅢⅣI]{1,4}\s*期?.{0,4}(重建|再造|成形|修补|闭式|缝合)")

# 合法分期：0 / I / II / III / IV，可带 A/B/C 亚期（原文自带的亚期，非推导所得）
STAGE_OK_RE = re.compile(r"^(0|I|II|III|IV)([ABC])?$")

NOT_STAGE = [
    (re.compile(r"^pcr$", re.I), "pCR=病理完全缓解，非分期（另有 diagnoses.items[].pcr 字段）"),
    (re.compile(r"^[0-9]+\s*级$"), "组织学分级，应入 grade"),
    (re.compile(r"^[IVXⅠⅡⅢⅣ]{1,4}[ABC]?\s*度$", re.I), "程度描述，非分期"),
    (re.compile(r"figo", re.I), "FIGO 分期（妇科体系），错位到乳腺病历"),
    (re.compile(r"^[MNY]\s*[0-9]?[abc]?$", re.I), "字段错位：M/N 值落入 stage"),
    # 0 期是合法分期（AJCC 原位癌），不吃这条拦截；`1期`～`4期` 属阿拉伯数字写法，体系不可判定
    (re.compile(r"^[1-9][0-9]*\s*期$"), "阿拉伯数字期，分期体系不可判定"),
    (re.compile(r"^[0-9]+[ABC]$", re.I), "阿拉伯数字+亚期，分期体系不可判定"),
    (re.compile(r"[~\-—–至到]"), "区间分期，无法归单一值"),
]

def _s(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "|".join(_s(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()

# ============================ 性别码 ============================

def norm_sex(v):
    s = _s(v)
    if s in SEX_MAP:
        return SEX_MAP[s], None
    if s in ("男", "女"):
        return s, "已是中文「%s」，非 1/2 码（源表应为码值）" % s
    return "", "性别码非 1/2：%r" % s

# ============================ TNM 前缀 ============================

def norm_prefix(v):
    s = _s(v)
    if s.lower() in UNSPEC:
        return "", None
    t = s.strip().lower()
    if t in PREFIX_OK:
        return t, None
    if t == "yc":
        # 新辅助后临床分期：契约闭集无 yc 组合，归 y（「新辅助后」这一信息不丢）
        return "y", "prefix「yc」归 y（契约闭集无 yc 组合）"
    if t == "uc":
        return "u", "prefix「uc」归 u"
    return "", "prefix 闭集外：%r" % s

# ============================ 分期 ============================

def norm_stage(v):
    s = _s(v)
    if not s or s.lower() in UNSPEC:
        return "", "", None

    # ① 中文分期：原样入 stage_raw_cn，不进罗马数字值域
    if CN_STAGE_RE.search(s):
        return "", s, "中文分期「%s」入 stage_raw_cn（契约 §3.6）" % s

    # ② 假分期陷阱：`I期乳房重建术` 不是肿瘤分期
    if FAKE_SURG_RE.search(s):
        return "", "", "假分期陷阱：期别紧跟术式词，判为手术分期（契约 §3.6 必拦）"

    # ③ 非分期形态（pCR / 分级 / FIGO / 字段错位 / 区间）
    for pat, why in NOT_STAGE:
        if pat.search(s):
            return "", "", "%s：%r" % (why, s)

    # ④ 字符级写法归一
    t = s
    for k, val in FW_ROMAN.items():
        t = t.replace(k, val)
    t = re.sub(r"(?i)stage", "", t)
    t = re.sub(r"\s", "", t).replace("期", "").upper()

    if STAGE_OK_RE.match(t):
        return t, "", None
    return "", "", "分期形态无法归一：%r" % s

# ============================ T / N / M ============================

def _split_dim(s):
    m = re.match(r"^([cpyruCPYRU]{0,2})([TNMtnm])(.*)$", s)
    if not m:
        return None, s
    return m.group(2).upper(), m.group(3).strip()

def norm_dim(v, kind):
    s = _s(v)
    if not s or s.lower() in UNSPEC:
        return "", None

    dim, core = _split_dim(s)
    if dim is not None and dim != kind.upper():
        # M0 落在 n 字段 / N3 落在 m 字段 —— 抽取层字段错位，不硬改也不硬塞
        return "", "字段错位：%r 是 %s 值却落在 %s 字段" % (s, dim, kind.upper())

    core = core.replace(" ", "").upper()
    if not core:
        return "", "只有维度字母无取值：%r" % s
    if re.match(r"^[TNM]", core):
        return "", "字段错位：%r 含其他维度字母，落在 %s 字段" % (s, kind.upper())
    if core in DIM_VALID[kind]:
        return core.lower(), None
    return "", "值域外：%r" % s

# ============================ 单条记录处理 ============================

def _iter_tnm(it):
    t = it.get("tnm")
    if isinstance(t, dict):
        yield t
    elif isinstance(t, list):
        for x in t:
            if isinstance(x, dict):
                yield x

def derive_record(rec, stat):
    added = 0
    guard = []

    # ---- ① patient.性别_text ----
    p = rec.get("patient")
    if isinstance(p, dict):
        guard.append(("patient.性别", _s(p.get("性别"))))
        text, why = norm_sex(p.get("性别"))
        if text:
            p["性别_text"] = text
            added += 1
            stat["sex"][text] += 1
        else:
            stat["sex"]["（空）"] += 1
        if why:
            stat["sex_qc"][why] += 1

    # ---- ② diagnoses.items[].tnm.* ----
    d = rec.get("diagnoses")
    if isinstance(d, dict):
        items = d.get("items") or []
    elif isinstance(d, list):
        items = d
    else:
        items = []
    if not isinstance(items, list):
        items = []

    for it in items:
        if not isinstance(it, dict):
            continue
        for t in _iter_tnm(it):
            stat["tnm_obj"] += 1
            raw = {k: t.get(k) for k in ("prefix", "t", "n", "m", "stage")}
            guard.append(("tnm", "|".join(_s(raw[k]) for k in ("prefix", "t", "n", "m", "stage"))))

            # tnm 五字段全空/仅占位词（未提及、未指明等）：不补派生字段，避免制造伪值；只计数。
            # 注意：这类「未填」多数是**正确行为**——原文本就无 TNM（良性结节、术后随访），
            # 不可当作抽取缺陷。真漏抽的判据须回原文核实（研究分析/90_qa_report/audit_tnm_coverage.py）。
            if not any(_s(raw[k]) for k in raw):
                stat["tnm_empty"] += 1
                continue
            stat["tnm_content"] += 1

            pref, p_why = norm_prefix(raw["prefix"])
            t_n, t_why = norm_dim(raw["t"], "t")
            n_n, n_why = norm_dim(raw["n"], "n")
            m_n, m_why = norm_dim(raw["m"], "m")
            st_n, st_cn, st_why = norm_stage(raw["stage"])

            t["prefix_norm"] = pref
            t["t_norm"] = t_n
            t["n_norm"] = n_n
            t["m_norm"] = m_n
            t["stage_norm"] = st_n
            added += 5

            if st_cn:
                t["stage_raw_cn"] = st_cn
                added += 1

            has_content = bool(st_n or t_n or n_n or m_n)
            if has_content:
                t["stage_system"] = "AJCC 8th"      # 契约 §3.6 版本锚定
                added += 1
                stat["stage_system"] += 1
            else:
                stat["tnm_no_valid"] += 1

            qc = []
            for field, (val, why) in (("prefix", (pref, p_why)), ("t", (t_n, t_why)),
                                      ("n", (n_n, n_why)), ("m", (m_n, m_why)),
                                      ("stage", (st_n, st_why))):
                if why:
                    qc.append({"field": field, "raw": _s(raw[field]), "why": why})
                    stat["qc"][field] += 1
            if qc:
                t["derived_qc"] = qc
                added += 1

            # 统计分布（归一前后对照）
            stat["prefix_before"][_s(raw["prefix"]) or "（空）"] += 1
            stat["prefix_after"][pref or "（空）"] += 1
            stat["stage_before"][_s(raw["stage"]) or "（空）"] += 1
            if st_n:
                stat["stage_after"][st_n] += 1

    return added, guard

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份（抽样试跑）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出 tar")
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--out-tar", default=OUT_TAR_DEFAULT, metavar="PATH")
    a = ap.parse_args()

    if not os.path.exists(a.src_tar):
        raise SystemExit("缺输入包 %s" % a.src_tar)
    n_src = count(a.src_tar)
    print("输入：%s（%d 份）" % (os.path.basename(a.src_tar), n_src))

    if not a.dry_run:
        if os.path.exists(a.out_tar) and not a.force:
            raise SystemExit("输出 tar 已存在，加 --force 覆盖：%s" % a.out_tar)
        os.makedirs(os.path.dirname(a.out_tar) or ".", exist_ok=True)
        os.makedirs(OUTD, exist_ok=True)

    stat = collections.defaultdict(collections.Counter)
    stat["tnm_obj"] = 0
    stat["tnm_empty"] = 0
    stat["tnm_content"] = 0
    stat["tnm_no_valid"] = 0
    stat["stage_system"] = 0

    t0 = time.time()
    n_rec = n_add = 0
    violated = []
    tf_out = tarfile.open(a.out_tar, "w") if (a.out_tar and not a.dry_run) else None
    # 值域归一账本（逐处留痕，便于回溯「改了哪一处、从什么改成什么」）
    led_path = OUTD + "/clean_enum_ledger.jsonl"
    led = open("/dev/null", "w") if a.dry_run else open(led_path, "w", encoding="utf-8")
    clean_stat = collections.Counter()

    def _emit(fn, payload):
        if payload is None:
            return
        ti = tarfile.TarInfo("./" + fn)
        ti.size = len(payload)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tf_out.addfile(ti, io.BytesIO(payload))

    for fn, rec in iter_records(a.src_tar, limit=a.limit):
        n_rec += 1
        added, guard = derive_record(rec, stat)
        n_add += added

        # 值域写法归一（在越权核查之前做：它只动 nature/modality/setting/laterality/
        # grading_system，不碰 patient.性别 与 tnm 原始五字段，故不影响 guard）
        class _Sink:
            def write(self, s):
                led.write(s)
                clean_stat[json.loads(s)["at"].rsplit(".", 1)[-1].split("[")[0]] += 1

        clean_record(rec, _Sink(), fn)

        # 越权改写核查：原字段一字不动才放行
        p = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
        d = rec.get("diagnoses")
        items = (d.get("items") if isinstance(d, dict) else d) if isinstance(d, (dict, list)) else []
        if not isinstance(items, list):
            items = []
        now = [("patient.性别", _s(p.get("性别")))]
        for it in items:
            if isinstance(it, dict):
                for t in _iter_tnm(it):
                    now.append(("tnm", "|".join(_s(t.get(k)) for k in ("prefix", "t", "n", "m", "stage"))))
        if now != guard:
            violated.append(fn)
            diff = [(x, y) for x, y in zip(guard, now) if x != y][:3]
            if len(violated) <= 3:
                print("  ✗ %s 原字段被改：%s" % (fn, diff))

        if not a.dry_run:
            _emit(fn, json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))

    el = time.time() - t0
    print("\n记录 %d / 新增字段 %d / 用时 %.1fs" % (n_rec, n_add, el))
    print("性别映射：%s" % dict(stat["sex"]))
    print("tnm 对象 %d（空壳 %d，有内容 %d，其中归一后仍无有效值 %d）"
          % (stat["tnm_obj"], stat["tnm_empty"], stat["tnm_content"], stat["tnm_no_valid"]))
    print("stage_system 写入 %d" % stat["stage_system"])

    print("\nprefix 归一前后：")
    for k, v in stat["prefix_before"].most_common(10):
        print("   before %-10s %d" % (k, v))
    for k, v in stat["prefix_after"].most_common():
        print("   after  %-10s %d" % (k, v))

    print("\nstage 归一后前 12：%s" % dict(stat["stage_after"].most_common(12)))
    print("stage 归一前变体数 %d / 归一后取值数 %d"
          % (len(stat["stage_before"]), len(stat["stage_after"])))

    if stat["qc"]:
        print("\nqc 拦截（按字段）：%s" % dict(stat["qc"]))
    if stat["sex_qc"]:
        print("性别 qc：%s" % dict(stat["sex_qc"].most_common(5)))

    if tf_out is not None:
        tf_out.close()
        print("\n✓ tar %s  %.1f MB" % (a.out_tar, os.path.getsize(a.out_tar) / 1048576))
    led.close()
    if clean_stat:
        print("值域写法归一：%s" % dict(clean_stat.most_common()))

    if violated:
        print("\n✗ 越权改写 %d 份：%s" % (len(violated), violated[:5]))
        sys.exit(2)

    if not a.dry_run:
        summary = {
            "generated_by": "05_agents/derive_contract_fields.py",
            "src": a.src_tar, "dst": a.out_tar,
            "n_record": n_rec, "n_field_added": n_add, "elapsed_s": round(el, 1),
            "value_clean": dict(clean_stat.most_common()),
            "sex": dict(stat["sex"]), "sex_qc": dict(stat["sex_qc"].most_common()),
            "tnm": {"obj": stat["tnm_obj"], "empty": stat["tnm_empty"],
                    "content": stat["tnm_content"], "no_valid": stat["tnm_no_valid"],
                    "stage_system_written": stat["stage_system"]},
            "prefix_before": dict(stat["prefix_before"].most_common()),
            "prefix_after": dict(stat["prefix_after"].most_common()),
            "stage_before_variants": len(stat["stage_before"]),
            "stage_after": dict(stat["stage_after"].most_common()),
            "qc": dict(stat["qc"]),
            "not_applicable": {
                "tnm_raw": "契约第 41 条（v7.1）已撤销全部 *_raw 原文片段字段，属撤销项而非缺口，不重建",
                "anchor": "同上，tnm.anchor 属原文片段字段，契约不再要求",
            },
        }
        fp = os.path.join(OUTD, "contract_fields_summary.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("✓ 摘要 %s" % fp)
    else:
        print("\n（dry-run，未落盘）")

if __name__ == "__main__":
    main()
