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

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records, count  # noqa: E402

PACKED = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed"
# 派生链中的位置：辅检形态/枚举归一（_aux.tar）之后、契约字段补齐（→ final.tar）之前。
# 100% 为 true，而 final.tar 晚于它 41 分钟 —— 历史上本步产物覆盖了 _aux.tar。
# 重建时为免「读写同一文件」损坏，改落 _kind.tar，cf 步以它为输入。
SRC_TAR_DEFAULT = PACKED + "/assembled_v7_final_aux.tar"
OUT_TAR_DEFAULT = PACKED + "/assembled_v7_final_kind.tar"

# 规范七桶
BUCKETS = ("药品", "检查", "检验", "操作", "材料", "费用", "其他")

# ---------------------------------------------------------------------------
# ① 确定性映射表：非规范 kind 值 -> 规范桶（按「能确定归属」的原则，不确定归其他）
# ---------------------------------------------------------------------------
KIND_MAP = {}

# 检验
for v in ["lab", "lab_test", "lab_exam", "lab_order", "实验室检查", "实验室检验",
          "检验项目", "检验检查", "检验-血液", "检验-生化", "检验-血常规",
          "检验-肝功能十六项", "检验-肾功能三项", "检验-血脂八项",
          "检验-空腹血葡萄糖", "检验-性激素组合3", "检验-癌胚抗原CEA",
          "检验-糖基抗原CA125", "检验-糖基抗原CA153", "检验-糖基抗原CA199",
          "化验", "检验/检查", "检验/病理", "检查/化验", "检查检验"]:
    KIND_MAP[v] = "检验"

# 检查
for v in ["exam", "examination", "imaging", "imaging_exam", "影像", "影像学",
          "影像检查", "影像学检查", "影像诊断", "超声", "超声检查", "超声/CT",
          "CT", "CT检查", "X线摄影", "心电图", "心电图检查", "病理", "病理检查",
          "报告", "图文报告", "超声图文报告", "报告图文", "report", "document",
          "医疗文书", "文书", "文书服务", "文件", "证明", "证明书", "证明文件",
          "证明文书", "检查项目", "检查/影像", "检查-影像", "检查-图文报告",
          "诊断报告", "结构化报告",
          "检查-CT平扫胸部", "检查-腹部彩超", "检查-甲状腺彩超", "检查-乳腺彩超",
          "检查-骨密度测定", "检查-报告", "检查-心电功能", "检查-加收", "检查附件",
          "检查部位", "检查建议", "检查加收", "检查/手术建议", "检查-加收",
          "检查-图文", "检查/手术", "检查/操作", "检查/处置", "检查/治疗",
          "检查/检验", "检查操作", "检查检验", "功能检查", "医学影像", "超声引导",
          "磁共振", "MRI", "钼靶", "造影", "透视", "骨密度", "影像学", "X线",
          "放射学", "放射治疗", "放疗", "体检", "医技", "医技服务", "医事服务"]:
    KIND_MAP[v] = "检查"

# 药品
for v in ["drug", "drugs", "medication", "med", "chemotherapy", "chemo", "chemo_drug",
          "antiemetic", "止吐药", "止吐", "化疗药", "化疗", "化学治疗", "化学治疗药",
          "化学治疗药物", "化学药物治疗", "靶向药", "靶向治疗", "靶向治疗药物",
          "口服药", "保肝药", "保肝", "护肝药", "护肝", "护胃药", "护胃",
          "升白药", "升白", "补血药", "补血/升血", "辅助药", "辅助用药", "辅助药品",
          "辅助治疗", "辅助止吐", "辅助护肝", "辅助护胃", "辅助升白", "辅助口服",
          "辅助支持", "化疗辅助药", "抗肿瘤药", "抗过敏药", "抗过敏", "抗凝药",
          "糖皮质激素", "激素/抗过敏", "维生素类", "胃黏膜保护剂", "免疫调节药",
          "内分泌治疗药", "西药", "中成药", "处方", "外延处方", "氯化钠注射液",
          "肝素钠注射液", "盐酸肾上腺素注射液", "盐酸利多卡因注射液",
          "生血中成药", "溶解液", "溶解剂", "输液溶媒", "溶剂", "溶媒", "溶媒/冲洗",
          "溶媒/辅助", "溶媒/冲管", "溶剂/溶媒", "溶剂/冲洗液", "配置用溶剂",
          "辅助用溶剂", "输液载体", "输液组", "造血刺激剂", "抗肿瘤化学药物配置",
          "化学治疗", "日间化疗", "化疗方案", "化疗形式", "化疗类型", "化疗相关",
          "化疗药物", "止吐", "预防恶心呕吐", "对症处理", "治疗方案", "治疗建议",
          "治疗类型", "治疗类别", "治疗方式", "治疗目的", "主治疗", "靶向"]:
    KIND_MAP[v] = "药品"

# 操作
for v in ["procedure", "operation", "op", "surgery", "surgical", "手术", "手术操作",
          "手术/操作", "手术费", "手术费用", "手术相关", "手术建议", "手术决策",
          "操作/手术", "处置/手术", "治疗/手术", "检查/手术", "护理", "护理操作",
          "护理项目", "护理服务", "护理治疗", "护理/操作", "换药", "输液", "静脉输液",
          "静脉注射", "皮下注射", "肌肉注射", "注射", "注射/操作", "注射操作",
          "注射药", "静脉采血", "采血材料", "给药", "配置", "配置服务", "配置项目",
          "药品配置", "药品配置服务", "药品准备", "治疗操作", "治疗处置", "治疗/操作",
          "操作/治疗", "操作/处置", "处置/操作", "处置/治疗", "处置建议", "处置类别",
          "处置", "操作", "操作服务", "操作费", "操作/服务", "操作/护理", "操作/检查",
          "操作/处置", "操作/治疗", "处理", "术后处理", "对症处理", "处置建议",
          "麻醉", "麻醉费", "麻醉/收费", "麻醉/操作",
          "麻醉/收费项", "麻醉/操作相关收费", "麻醉/费用", "麻醉费用", "治疗/麻醉",
          "医疗操作", "诊疗操作", "诊疗", "诊疗类别", "诊疗项目", "会诊", "康复治疗",
          "支持治疗", "支持治疗药物", "支持", "support", "supportive", "supportive_care",
          "nursing", "narrative", "narrative_item", "narrative_segment", "instruction",
          "指导", "交代", "监测", "随访", "随访建议", "复诊", "随诊", "就诊",
          "休息", "rest", "rest_order", "活动", "评估", "计划", "检查/手术",
          "静脉输液(第二组起)", "静脉输液(集中配置)", "植入式给药装置护理",
          "抗肿瘤化学药物配置(集中配置)", "采血", "冲管", "拆线", "换药",
          "注射", "皮下注射(项目)", "门诊疾病证明书", "定期复查"]:
    KIND_MAP[v] = "操作"

# 材料
for v in ["material", "supply", "device", "耗材", "材料", "辅助材料", "材料/证明",
          "耗材/器械", "耗材/服务", "耗材/操作", "器械", "医材", "物品",
          "一次性使用人体静脉血样采集容器", "一次性使用真空采血管",
          "一次性使用静脉采血针", "procedure_supply", "附件"]:
    KIND_MAP[v] = "材料"

# 费用
for v in ["fee", "收费", "收费项目", "费用", "其他费用", "其他收费", "其他/收费",
          "报告费", "收费/麻醉", "附加费用", "附加项目", "附加", "费", "操作费",
          "操作相关收费", "procedure_fee", "材料/证明", "收费项目"]:
    KIND_MAP[v] = "费用"

# 其他（显式归其他；注意「处置」「处理」已在上方归操作，「document」「诊断报告」归检查）
for v in ["other", "其他医嘱", "无法结构化", "字段", "文本", "备注", "常规",
          "综合", "医嘱", "医嘱项", "医嘱项目", "医嘱条目", "医嘱类型", "医嘱类别",
          "医嘱说明", "医嘱备注", "医嘱操作", "医嘱服务", "医嘱服务项", "医嘱/处置",
          "医嘱类", "建议", "项目", "其他", "分类汇总", "叙述",
          "叙述性文本", "advice", "ICD", "ICD映射", "医保", "行政",
          "疾病证明", "病假", "证件", "certificate", "cert",
          "费用类", "处理方式", "处理意见"]:
    KIND_MAP[v] = "其他"

# ---------------------------------------------------------------------------
# ② name 关键词回退（kind 空时用）
# ---------------------------------------------------------------------------
NAME_RULES = [
    (re.compile(r"(片|胶囊|注射液|注射用|滴丸|颗粒|口服液|软膏|栓|喷雾|贴剂|"
                r"缓释|微球|乳膏|溶液|滴眼|鼻喷|干混悬|口溶膜|分散片|丸)"), "药品"),
    (re.compile(r"(血常规|尿常规|肝功|肾功|血脂|血糖|血生化|甲功|性激素|"
                r"肿瘤标志物|癌胚抗原|糖基抗原|CA12|CA15|CA19|CEA|电解质|凝血|"
                r"血沉|C反应|免疫球蛋白|抗体|五项|八项|四项|三项|十六项|组合|"
                r"基因检测|脱落细胞|病理)"), "检验"),
    (re.compile(r"(超声|彩超|多普勒|CT|X线|X光|钼靶|核磁|MR|磁共振|骨密度|"
                r"心电图|内镜|造影|摄片|摄影|显像|图文报告|透视)"), "检查"),
    (re.compile(r"(采血|采血管|采血针|血样采集|静脉输液|皮下注射|肌肉注射|"
                r"静脉注射|换药|护理|给药|配置|麻醉|拆线)"), "操作"),
]

# ---------------------------------------------------------------------------
# ②b 名称强判：kind 已是合法七桶、但名称无歧义地属于另一桶时改判
# ---------------------------------------------------------------------------
# 于是 LLM 把「血常规／肝功能十六项／糖基抗原CA153」记成「检查」时被照单全收，
# 单这一种（检查→检验）就占 59 行。此类名称本身已足以判类，属确定性可修。
# 只在高置信、短名称（非叙述句）上改判，其余一律尊重原值。
OVERRIDE_RULES = [
    # 顺序即优先级：费用最强；药品先于操作与检验，否则「注射用紫杉醇(白蛋白结合型)」
    # 会被「白蛋白」判成检验、「…微球…皮下注射」会被「皮下注射」判成操作。
    (re.compile(r"(费(?:$|[（(])|收费|价格|金额)"), "费用"),
    (re.compile(r"(片(?=[0-9０-９ ])|胶囊|注射液|注射用|滴丸|颗粒|口服液|软膏|"
                r"栓剂|喷雾|贴剂|缓释|微球|乳膏|分散片|片剂)"), "药品"),
    (re.compile(r"(日间化疗|皮下注射|肌肉注射|静脉输液|静脉注射|换药|拆线|"
                r"采血|置管|穿刺|引流|麻醉|临床操作|引导)"), "操作"),
    (re.compile(r"(超声|彩超|多普勒|CT|X线|X光|钼靶|核磁|磁共振|骨密度|心电图|"
                r"内镜|造影|摄片|摄影|显像|图文报告|透视|病理|平扫|增强扫描)"), "检查"),
    (re.compile(r"(血常规|尿常规|肝功|肾功|血脂|血糖|血葡萄糖|葡萄糖|血生化|甲功|"
                r"性激素|心肌酶|肿瘤标志物|癌胚抗原|糖基抗原|CA12|CA15|CA19|CEA|"
                r"AFP|PSA|电解质|凝血|血沉|C反应|免疫球蛋白|糖化血红蛋白|降钙素原|"
                r"乳酸脱氢酶|D-二聚体|白蛋白|胆红素|肌酐|尿酸|培养|药敏|血型|"
                r"肝炎|梅毒|HIV|病毒载量|基因检测|FISH)"), "检验"),
]
NARRATIVE = re.compile(r"(建议|复查|随诊|监测|警惕|必要时|注意|说明|告知|如需|期间|疗效)")
# 药名在前 12 字内出现：长处方行（含「定期复查」等）也算药品，不受叙述句守卫限制
PREFIX_DRUG = re.compile(r"^[^，。；:：]{0,12}?(片(?=[0-9０-９ ])|胶囊|注射液|注射用|"
                         r"滴丸|颗粒|口服液|软膏|栓剂|缓释|微球|乳膏|分散片|片剂)")

def _override(nm, kind):
    if not nm:
        return None
    if PREFIX_DRUG.match(nm):
        return "药品" if kind != "药品" else None
    if len(nm) > 40 or NARRATIVE.search(nm):
        return None
    for rx, bucket in OVERRIDE_RULES:
        if rx.search(nm):
            return bucket if bucket != kind else None
    return None

# ---------------------------------------------------------------------------
# ③ 归一主函数
# ---------------------------------------------------------------------------
def normalize_kind(kind, name):
    nm = _s(name)
    # 已规范七桶 → 名称能无歧义推翻时才改判
    if kind in BUCKETS:
        ov = _override(nm, kind)
        if ov:
            return ov, "name_override"
        return kind, "already"
    # 非规范值映射
    if kind and kind in KIND_MAP:
        return KIND_MAP[kind], "kind_map"
    # kind 空 → name 关键词回退
    if nm:
        for rx, bucket in NAME_RULES:
            if rx.search(nm):
                return bucket, "name_rule"
    # 都无 → 其他（不臆造）
    return "其他", "fallback_other"

def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        if isinstance(v, dict):
            for k in ("name", "text", "std", "value"):
                if v.get(k):
                    return str(v[k])
            return ""
        return "|".join(_s(x) for x in v)
    return str(v).strip()

def _deep(o):
    return hashlib.md5(json.dumps(o, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-tar", default=SRC_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--out-tar", default=OUT_TAR_DEFAULT, metavar="PATH")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out-summary", default="")
    a = ap.parse_args()

    if os.path.exists(a.out_tar) and not a.force:
        raise SystemExit("输出 tar 已存在，加 --force：%s" % a.out_tar)
    os.makedirs(os.path.dirname(a.out_tar) or ".", exist_ok=True)

    t0 = time.time()
    n_rec = n_item = n_changed = n_filled = 0
    by_path = collections.Counter()
    by_new = collections.Counter()
    bad = []

    tf_out = tarfile.open(a.out_tar, "w")

    for fn, d in iter_records(a.src_tar, limit=a.limit):
        n_rec += 1
        # 只处理 orders.items 的 kind；先取其它字段的深指纹做防越权
        orders = d.get("orders")
        if not isinstance(orders, dict):
            orders = {"items": [], "advice": [], "narrative": []}
            d["orders"] = orders
        items = orders.get("items")
        if not isinstance(items, list):
            items = []
            orders["items"] = items

        # 除 orders.items 外的深指纹
        rest0 = _deep({k: v for k, v in d.items() if k != "orders"})
        items_wo_kind0 = _deep([{k: v for k, v in it.items() if k != "kind"}
                                for it in items if isinstance(it, dict)])

        for it in items:
            if not isinstance(it, dict):
                continue
            n_item += 1
            old = it.get("kind", "")
            new, path = normalize_kind(old, it.get("name"))
            if new != old:
                it["kind"] = new
                n_changed += 1
                by_path[path] += 1
                by_new[new] += 1
            if old in ("", None):
                n_filled += 1

        # 防越权校验（在 meta 打标记之前做，确保标记不影响指纹）
        rest1 = _deep({k: v for k, v in d.items() if k != "orders"})
        items_wo_kind1 = _deep([{k: v for k, v in it.items() if k != "kind"}
                                for it in items if isinstance(it, dict)])
        if rest1 != rest0 or items_wo_kind1 != items_wo_kind0:
            bad.append(fn)

        # 在 meta 打派生标记（防越权指纹已算完，此改动是有意为之）
        meta = d.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            d["meta"] = meta
        meta["order_kind_normalized"] = True

        # 写 tar
        payload = json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")
        ti = tarfile.TarInfo("./" + fn)
        ti.size = len(payload)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tf_out.addfile(ti, io.BytesIO(payload))

        if n_rec % 20000 == 0:
            print("进度", n_rec, flush=True)

    tf_out.close()
    el = time.time() - t0

    print("\n=== kind 七桶归一 ===")
    print("记录 %d / 医嘱条目 %d / 改动 %d（%.1f%%）/ 其中空值填充 %d / 用时 %.1fs"
          % (n_rec, n_item, n_changed, 100.0 * n_changed / max(n_item, 1), n_filled, el))
    print("\n归一路径：")
    for k, v in by_path.most_common():
        print("   %-18s %d" % (k, v))
    print("\n归一后七桶分布：")
    for b in BUCKETS:
        print("   %-6s %d" % (b, by_new.get(b, 0)))
    print("\n✓ tar %s  %.1f MB" % (a.out_tar, os.path.getsize(a.out_tar) / 1048576))

    if bad:
        print("\n✗ 越权改写 %d 份：%s" % (len(bad), bad[:5]))
        sys.exit(2)

    if a.out_summary:
        summary = {
            "generated_by": "normalize_order_kind.py",
            "src": a.src_tar, "dst": a.out_tar,
            "n_record": n_rec, "n_item": n_item, "n_changed": n_changed,
            "n_filled": n_filled, "elapsed_s": round(el, 1),
            "by_path": dict(by_path), "by_new": dict(by_new),
        }
        os.makedirs(os.path.dirname(a.out_summary) or ".", exist_ok=True)
        with open(a.out_summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("\n✓ 摘要落盘 %s" % a.out_summary)

if __name__ == "__main__":
    main()
