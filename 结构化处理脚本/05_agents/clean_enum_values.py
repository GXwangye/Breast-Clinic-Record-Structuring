# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
QA_OUT = DATA + "/中间产物/_qa_out"
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_aux.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final_clean.tar"

sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records, write_json  # noqa: E402

# ---------------------------------------------------------------------------
# 触诊淋巴结 nature：阴性表达 → 契约值「未见异常」
# ---------------------------------------------------------------------------
# 纯符号：`(-)` `（-）` `（-)` `-` `—` `阴性`
_NEG_PURE = re.compile(r"^[（(]?\s*[-－—–]+\s*[）)]?$")
# 部位 + 符号：`腋LN（-）` `双腋窝（-)` `右乳(-)`
_NEG_SITED = re.compile(r"^.{0,12}[（(]\s*[-－—–]+\s*[）)]\s*[。;；]?$")
# 文字阴性表达
_NEG_WORDS = ("未触及", "未及", "未扪及", "未扪到", "未触到", "阴性", "未见异常",
              "无明显肿大", "未见肿大", "未发现肿大")
LN_NEG_VALUE = "未见异常"

def _norm_ln_nature(v):
    s = v.strip()
    if not s or s == LN_NEG_VALUE:
        return None
    if not (_NEG_PURE.match(s) or _NEG_SITED.match(s) or any(w in s for w in _NEG_WORDS)):
        return None
    return LN_NEG_VALUE

# ---------------------------------------------------------------------------
# 治疗方式 modality：写法归一（不跨语义）
# ---------------------------------------------------------------------------
MODALITY_MAP = {
    "内分泌治疗": "内分泌", "内分泌治疗药物": "内分泌", "endocrinotherapy": "内分泌",
    "靶向治疗": "靶向", "targeted": "靶向", "targeted therapy": "靶向",
    "化学治疗": "化疗", "chemotherapy": "化疗", "chemo": "化疗",
    "放射治疗": "放疗", "radiotherapy": "放疗",
    "surgery": "手术", "operation": "手术",
    "other": "其他",
}
MODALITY_NULL = {"未提及", "未指明", "不详", "无", "未描述", "not_present", "none", "na", "n/a"}

# 诊断 setting：同义写法归一（术后/既往/病理 等须读上下文，不归）
SETTING_MAP = {
    "新辅助治疗": "新辅助", "新辅助治疗后": "新辅助", "新辅助化疗": "新辅助",
    "辅助治疗": "辅助", "术后辅助": "辅助", "辅助化疗": "辅助", "辅助内分泌治疗": "辅助",
    "解救治疗": "解救", "晚期解救": "解救",
}

# 侧别：只归「纯侧别写法」的英文/标点变体。
# 不含 `双侧`（LATERALITY 闭集已收）、不含 `单侧`/`无`（语义含糊，须读上下文）、
# 不含 `右腋窝`/`双乳`（含部位，归一为单字会丢部位信息）。
LAT_MAP = {
    "undefined": "未指明", "未知": "未指明", "unverified": "未指明",
    "not specified": "未指明", "unknown": "未指明",
    "right": "右", "left": "左", "bilateral": "双", "both": "双",
    "右,左": "右、左", "左,右": "左、右", "右, 左": "右、左", "左, 右": "左、右",
}

# 分级体系：只修拼写错（BI-RADS 的各种漏/错字母），不归中性值（中性值校验器已放行，
# 清空会丢「做了但未给分级」这一事实）。
GRADING_SPELL = {"BI-RAID": "BI-RADS", "BI-BADS": "BI-RADS", "BIRADS": "BI-RADS",
                 "BI-RAD": "BI-RADS"}

def _map_or_keep(v, table, null_set=None):
    s = v.strip()
    if null_set and s in null_set:
        return ""
    t = table.get(s)
    return t if t is not None else None

# ---------------------------------------------------------------------------
# 清洗主逻辑
# ---------------------------------------------------------------------------
def clean_record(rec, ledger, rid):
    def log(where, old, new):
        ledger.write(json.dumps({"record_id": rid, "at": where, "old": old, "new": new},
                                ensure_ascii=False) + "\n")

    # 1) 触诊淋巴结 nature
    ln = ((rec.get("physical_exam") or {}).get("structured") or {}).get("lymph_nodes") or []
    for i, it in enumerate(ln):
        if not isinstance(it, dict):
            continue
        nat = it.get("nature")
        if isinstance(nat, str) and nat:
            new = _norm_ln_nature(nat)
            if new is not None and new != nat:
                log("physical_exam.structured.lymph_nodes[%d].nature" % i, nat, new)
                it["nature"] = new

    # 2) 治疗方式
    t = rec.get("treatment") or {}
    for i, it in enumerate(t.get("items") or [] if isinstance(t, dict) else []):
        if not isinstance(it, dict):
            continue
        v = it.get("modality")
        if isinstance(v, str) and v:
            new = _map_or_keep(v, MODALITY_MAP, MODALITY_NULL)
            if new is not None and new != v:
                log("treatment.items[%d].modality" % i, v, new)
                it["modality"] = new

    # 3) 诊断 setting
    d = rec.get("diagnoses")
    items = d.get("items") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        v = it.get("setting")
        if isinstance(v, str) and v:
            new = _map_or_keep(v, SETTING_MAP)
            if new is not None and new != v:
                log("diagnoses.items[%d].setting" % i, v, new)
                it["setting"] = new

    # 4) 侧别（只改英文/占位写法，含部位的错位值不动）
    def fix_lat(container, where):
        v = container.get("laterality")
        if isinstance(v, str) and v.strip():
            new = LAT_MAP.get(v.strip())
            if new and new != v:
                log(where + ".laterality", v, new)
                container["laterality"] = new

    for i, it in enumerate(items or []):
        if isinstance(it, dict):
            fix_lat(it, "diagnoses.items[%d]" % i)
    ax = rec.get("auxiliary_exam") or {}
    if isinstance(ax, dict):
        for key in ("reports", "report_groups", "labs"):
            for i, it in enumerate(ax.get(key) or []):
                if isinstance(it, dict):
                    fix_lat(it, "auxiliary_exam.%s[%d]" % (key, i))
        ihc = ax.get("ihc") or {}
        for i, pn in enumerate(ihc.get("panels") or [] if isinstance(ihc, dict) else []):
            if isinstance(pn, dict):
                fix_lat(pn, "auxiliary_exam.ihc.panels[%d]" % i)

    # 5) 分级体系拼写（只修漏字/错字，不动中性值与真实体系）
    def fix_grading(o, path=""):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                p = "%s.%s" % (path, k) if path else k
                if k == "grading_system" and isinstance(v, str) and v.strip():
                    s = v.strip()
                    t = GRADING_SPELL.get(s)
                    if t and t != s:
                        log(p, v, t)
                        o[k] = t
                elif k != "qc":
                    fix_grading(v, p)
        elif isinstance(o, list):
            for v in o:
                fix_grading(v, path + "[]")

    fix_grading(rec)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC_TAR_DEFAULT)
    ap.add_argument("--out", default=OUT_TAR_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    os.makedirs(QA_OUT, exist_ok=True)
    led_path = QA_OUT + "/clean_enum_ledger.jsonl"
    stat = collections.Counter()
    by_rule = collections.Counter()
    n = 0
    changed_recs = 0

    led = open("/dev/null", "w") if a.dry_run else open(led_path, "w", encoding="utf-8")
    out = None
    if not a.dry_run:
        import tarfile
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        out = tarfile.open(a.out, "w")

    try:
        for fn, rec in iter_records(a.src):
            n += 1
            rid = os.path.splitext(fn)[0]
            buf = []

            class _Sink:
                def write(self, s):
                    buf.append(s)

            clean_record(rec, _Sink(), rid)
            if buf:
                changed_recs += 1
                for line in buf:
                    it = json.loads(line)
                    stat[it["at"].split(".")[-1].split("[")[0]] += 1
                    by_rule["%s :: %s → %s" % (it["at"].rsplit(".", 1)[0].split("[")[0],
                                               it["old"], it["new"])] += 1
                    led.write(line)
            if out is not None:
                data = json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8")
                info = tarfile.TarInfo(rid + ".json")
                info.size = len(data)
                info.mtime = 0
                import io
                out.addfile(info, io.BytesIO(data))
    finally:
        led.close()
        if out is not None:
            out.close()

    summary = {
        "generated_by": "05_agents/clean_enum_values.py",
        "dry_run": a.dry_run,
        "src": a.src,
        "out": None if a.dry_run else a.out,
        "n_record": n,
        "n_record_changed": changed_recs,
        "by_field": dict(stat),
        "top_rules": dict(sorted(by_rule.items(), key=lambda x: -x[1])[:30]),
    }
    if not a.dry_run:
        write_json(summary, QA_OUT + "/clean_enum_summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
