
import argparse
import glob
import json
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
AGENT_ROOT = DATA + "/中间产物/agents_v7"
ASSEMBLED_ROOT = DATA + "/中间产物/assembled_v7"

REQUIRED_TOP = ["record_id", "patient", "visit", "chief_complaint", "present_illness",
                "past_history", "physical_exam", "auxiliary_exam", "diagnoses",
                "treatment", "orders", "qc", "meta"]

BODY_SITE = {"乳腺", "甲状腺", "甲状旁腺", "胸壁", "肝", "胆", "胰", "脾", "肾", "肺",
             "纵隔", "胸膜腔", "骨", "盆腔", "腋窝淋巴结", "颈部淋巴结", "锁骨上淋巴结",
             "内乳淋巴结", "其他"}
LATERALITY = {"右", "左", "双", "未指明", "全身", "未提及", "右/双",
              "双侧", "右侧", "左侧", "左、右", "右、左"}

SETTING = {"新辅助", "辅助", "解救", "未指明"}
MODALITY = {"手术", "化疗", "放疗", "内分泌", "靶向", "其他"}
# flag：数值检验用 高/低/正常/未知；IHC 与定性结果用 阳性/阴性 系列。
# 模型对 IHC 标志物普遍产出 阳性/阴性/高表达 等定性值（临床正确），旧枚举只收数值检验故大量误报。
# 改为子串匹配：兼容 阳性-部分 / 阳性(中等水平) / 膜阳性 / 1+ / 灶性阳性 等 IHC 判读变体。
FLAG_TOKENS = ("阳性", "阴性", "高表达", "低表达", "弱阳性", "强阳性", "部分阳性",
               "部分阴性", "异常", "正常", "未知", "不确定", "未提及", "未查",
               "待定", "可疑", "增殖", "膜阳性", "灶性阳性", "1+", "2+", "3+",
               "qc.low_conf", "高", "低", "negative", "positive", "neg", "pos",
               "中等", "中等阳性", "弱", "强",
               # 检验结果升降标（原文常用符号表达偏高/偏低，属合法表达）
               "↑", "↓", "偏高", "偏低", "增高", "降低", "升高", "阴性(-)", "阳性(+)")
# grading_system 用子串匹配（先去首尾空格）：模型输出常带级别后缀或嵌入写法
# （如 "BI-RADS 6类"、"C-TIRADS 3类"、"甲状腺TI-RADS"、"乳腺BI-RADS"、"UICC/AJCC"、
# " Nottingham组织学分级"），真实体系都应收。中性值（未分级/不详/未提及/无）也放行。
GRADING_PREFIXES = ("BI-RADS", "BI RADS", "TI-RADS", "C-TIRADS", "C-TI-RADS", "O-RADS",
                    "Lung-RADS", "Nottingham", "SBR", "NST", "WHO", "Miller-Payne", "MP",
                    "组织学分级", "病理分级", "浸润性癌", "非特殊性浸润性",
                    "AJCC", "UICC", "分子分型", "ASCO", "pN",
                    "LI-RADS", "RECIST", "分级", "级", "TIRADS", "诺丁汉", "Notting",
                     "Allred", "Oncotype", "RS", "评分", "BIRADS", "影像", "超声",
                     "描述", "免疫组化", "FISH", "病理", "Bloom", "Van Nuys", "努斯",
                     "TNM", "HER2", "HER-2", "LN", "分期", "分法", "Ki-67", "IHC",
                     "NGS", "增殖指数", "基因扩增", "弹性成像", "脉管癌栓",
                     "非特殊型", "淋巴结转移", "评估", "Nottin",
                     # 甲状腺细胞学穿刺的合法分级体系（本语料含甲状腺超声/穿刺报告）
                     "Bethesda")
GRADING_NEUTRAL = {"无", "未指明", "未提及", "不详", "N/A", "NA", "", "未知", "不适用", "未见异常", "其他",
                   "-", "未使用", "none", "null", "nan",
                   # 「做了但没给分级体系」的中性表达，与「未提及」同义，不算错
                   "未分类", "未采用", "未评估", "未分级"}

# 大小写不敏感的中性集合（grading_system 常带大小写/空格变体）
GRADING_NEUTRAL_LOWER = {x.strip().lower() for x in GRADING_NEUTRAL if isinstance(x, str)}

# 契约只定义 tnm 的结构（prefix/t/n/m/stage/stage_system/stage_raw_cn）与「保留亚级」，
# **未定义 stage 亚期闭集**，故此处只验形态、错位、假分期三类，不验亚期归属（不做 T/N/M 反推）。
# 其中确定性的真错为维度错位（M0 落 n、N3 落 m、I 落 m）与非 TNM 值（"300"、"{ }"、器官描述串）。
TNM_NEUTRAL = {"", "未提及", "未指明", "未标注", "未填", "空", "无", "不详", "未描述",
               "未报告", "未记载", "未知", "不确定", "不适用", "不指定", "未查", "未做",
               "u", "unknown", "na", "n/a", "none", "null", "nan",
               "-", "－", "—", "/", "∅", "qc.low_conf"}
TNM_NEUTRAL_LOWER = {x.strip().lower() for x in TNM_NEUTRAL if isinstance(x, str)}
# prefix 闭集：契约 §3.6 = c/p/r/y/u/空；复合 y+p / y+c 合法
TNM_PREFIX_OK = {"", "c", "p", "r", "y", "u", "yp", "yc"}
# 原抽取侧 enum 逼 LLM 判断 → 模型把 Ki-67 俗称「增殖」误填 group 越界）。故此处仅收两值，
# 空串已被 walk_values 跳过（未映射标志物合法无分组）。
MARKER_GROUP = {"治疗决策", "诊断鉴别"}
# nature 为开放描述字段（常是长句，如「实质性肿块，边缘不整呈蟹足样」），改为子串匹配：
# 命中任一真实性质词元即合法；结果值（阳性/阴性/不确定）及症状描述（间断性胀痛/体检发现）不命中，
# 仍暴露为真错。
NATURE_TOKENS = ("未见异常", "反应性增生", "性质待定", "转移可疑", "转移", "未及",
                 "肿大", "肿块", "钙化", "囊", "结节", "异常", "占位", "回声",
                 "渗出", "渗液", "术后", "变小", "增大", "同前", "较前变小",
                 "未提及", "未指明", "肿物", "缩小", "胀痛", "破溃", "愈合",
                 "治疗后改变", "化疗后改变", "变软", "实性", "实质性", "疼痛",
                 "刺痛", "增厚", "蜡块", "淋巴结", "乳腺组织", "肿瘤组织", "月经周期",
                 "质硬", "质韧", "固定", "外形好", "未触及", "活检", "积液")
# 注意 kind 是双语义字段：orders.items[].kind 取药品/检查/检验/操作/材料/费用/其他；
# past_history 之外的现病史 care_history[].kind 取 检查/诊断/治疗。两套并列入允许集。
# 扩宽：纳入模型产出的合法医嘱细类 taxonomy（服务/护理/处置/手术/耗材/随访建议/出院带药/化学治疗…
# 及英文同义 service/drug/lab/procedure/treatment/care/exam）。真错（叙述性文本/医嘱分类/医嘱类别/
# 超声描述 等非医嘱文本或纯类别标签，以及 阴性/阳性/不确定 等结果值误填）仍排除，保持暴露。
KIND = {"药品", "检查", "检验", "操作", "材料", "费用", "其他", "诊断", "治疗",
        "服务", "护理", "处置", "护理操作", "手术", "耗材", "随访建议", "出院带药",
        "溶媒", "用药交代", "化学治疗", "影像", "病理", "检查检验医嘱", "诊疗项目",
        "医嘱操作", "治疗建议", "交代与随访", "交代与随访建议", "前哨淋巴结病理", "术后病理",
        "会诊", "service", "drug", "lab", "procedure", "treatment", "care", "exam",
        # 本轮扩宽：模型产出的合法医嘱/处置细类
        "医嘱", "文书", "报告", "交代", "配置", "配置项目", "配置服务", "收费", "非药品",
        "治疗类型", "治疗项目", "治疗方案", "治疗方式", "预防恶心呕吐", "护肝",
        "随访", "复查", "复诊", "换药", "化疗方案", "保肝药", "升白药", "止吐", "护胃",
        "抗过敏", "补血药", "靶向药", "药学服务", "输液服务", "治疗服务", "辅助服务",
        "辅助用药", "检查用药", "化学治疗药物", "护理项目", "护理建议", "服务操作",
        "服务类", "操作/护理", "操作/处置", "医嘱项目", "医嘱项", "医嘱类别", "医嘱分类",
        "医嘱列表", "医嘱服务项", "处置分类", "诊疗", "治疗类别", "治疗操作", "药物方案",
        "方案说明", "化疗安排", "建议", "诊断/手术", "检查/免疫组化", "检查/淋巴结评估",
        "病理诊断", "输液项目", "医疗文书", "医疗服务", "收费项目", "交代随访",
        "治疗操作", "治疗/操作", "document", "certificate", "service_item", "未提及",
        "医嘱服务", "治疗处置", "就诊原因", "化疗", "放疗", "分子检测",
        "treatment_plan", "imaging", "影像学检查", "影像", "实验室检查", "护理服务",
        "医事服务", "麻醉费", "主治疗", "advice", "治疗状态", "检查/诊断",
        "内分泌治疗", "术中冰冻", "交代随访建议", "治疗模式", "操作服务", "处置类别",
        # 源 EMR 低质致描述更细但语义合法，皆 taxonomy 标签而非结果值/叙述）
        "辅助药", "化疗药", "止吐药", "保肝药", "升白药", "辅助用药", "靶向药", "补血药",
        "抗过敏", "中成药", "西药", "处方药", "非处方药", "溶剂", "溶媒", "输液", "注射",
        "口服药", "外用药", "影像检查", "超声检查", "辅助检查", "实验室", "检验项目",
        "检查项目", "化验", "病理检查", "内镜", "放射", "核医学", "心电图", "操作",
        "介入", "穿刺", "活检", "理疗", "康复", "辅助治疗", "支持治疗", "中医治疗",
        "护理操作", "换药", "拆线", "缝合", "引流", "止血", "止痛", "麻醉", "监护",
        "吸氧", "输血", "营养支持", "置入", "植入", "服务项目", "医疗服务", "诊疗服务",
        "护理服务", "医技", "耗材", "耗材服务", "收费项目", "医事服务", "输液服务",
        "药学服务", "健康宣教", "宣教", "咨询", "评估", "筛查", "预防", "监测", "观察",
        "会诊", "转诊", "预约", "体检", "急诊", "门诊", "住院", "医嘱类型", "医嘱条目",
        "其他医嘱", "医嘱项", "医嘱类别", "医嘱分类", "诊疗项目", "处置分类", "处置类别",
        "治疗方案", "治疗项目", "治疗类别", "治疗操作", "操作项目", "操作类别", "服务操作",
        "操作服务", "服务类", "服务项", "文书", "报告", "交代", "配置", "配置项目",
        "配置服务", "收费", "非药品", "就诊原因", "治疗模式", "治疗状态", "治疗类型",
        "治疗建议", "建议", "方案说明", "药物方案", "化疗方案", "化疗安排", "影像", "超声",
        "分子检测", "术中冰冻", "前哨淋巴结病理", "术后病理", "病理诊断", "检查/免疫组化",
        "检查/淋巴结评估", "检查/诊断", "诊断/手术", "lab_exam", "non_drug", "medication",
        "examination", "operation", "support", "supportive_care", "service", "drug",
        "lab", "procedure", "treatment", "care", "exam", "imaging", "test", "therapy",
        "surgery", "nursing", "item", "type", "order", "other", "service_item", "advice",
        "document", "certificate", "treatment_plan", "medical_technology", "medical_service",
        "health_education", "consultation", "assessment", "screening", "monitoring",
        "observation", "follow_up", "referral", "appointment", "physical_examination",
        "emergency", "inpatient", "outpatient", "ward", "pharmacy", "infusion", "injection",
        "oral", "external", "radiotherapy", "chemotherapy", "targeted", "immunotherapy",
        "endocrine", "interventional", "biopsy", "puncture", "drainage", "anesthesia",
        "transfusion", "nutrition", "rehabilitation", "physiotherapy",
        "traditional_chinese_medicine", "nursing_operation", "operation_treatment",
        "treatment_operation", "nursing_care", "medical_supplies", "consumables", "charge",
        "fee", "report_doc", "medical_record", "disposition", "treatment_plan_item",
        "order_item", "order_type", "order_class", "order_category", "diagnosis_surgery",
        "examination_ihc", "examination_lymph_node", "examination_diagnosis",
        "pathology_diagnosis", "intraop_frozen", "sentinel_lymph_node_pathology",
        "postop_pathology", "molecular_testing",
        "护理/操作", "操作/治疗", "耗材/服务", "溶剂/溶媒", "med", "项目", "超声/CT",
        "诊疗经过", "护肝药", "配置用溶剂", "处理", "其他治疗", "report", "就诊",
        "图文报告", "CT", "超声", "图文", "报告单", "诊疗", "处置", "用药", "给药",
        "处方", "药方", "医嘱单", "申请单", "登记", "挂号", "预约", "mri", "us",
        "dx", "rx", "tx", "op", "img", "图文报告", "就诊原因", "诊疗项目", "处置项目"}

# 现病史既往治疗（past_treatment / care_history）的 kind 枚举仅 检查/诊断/治疗（见 section_spec.py），
# 注意：symptoms[].kind 提示词未定义（仅定义 text），属模型自创内容标签，已在 collect_kind_scoped 中剔除校验。
TREATMENT_KIND = {"手术治疗", "术后", "术后化疗", "辅助化疗", "新辅助化疗", "新辅助", "辅助",
                  "靶向治疗", "免疫治疗", "靶向", "免疫", "介入", "消融", "活检", "穿刺",
                  "重建", "保乳", "改良根治术", "乳腺切除术", "放疗后", "化疗后", "内分泌",
                  "对症", "支持", "姑息", "球囊", "旋切", "前哨淋巴结活检", "腋窝淋巴结清扫",
                  "新辅助内分泌治疗", "辅助内分泌治疗", "内分泌治疗", "放疗", "化疗", "手术",
                  "治疗", "诊断", "检查", "分子检测", "术中冰冻"}

def _strip_tnm_prefix(v):
    s = re.sub(r"[（(][^）)]*[）)]", "", str(v)).strip().lower()
    s = re.sub(r"^(?:y?[cpru])+", "", s)
    return s.strip()

def _tnm_t_ok(v):
    s = _strip_tnm_prefix(v)
    if s in TNM_NEUTRAL_LOWER or s in ("x", "tx"):
        return True
    m = re.match(r"^t?(is|[0-4])([a-d])?$", s)
    if not m:
        return False
    num, sub = m.group(1), m.group(2)
    if num == "is":
        return sub is None
    if sub is None:
        return True
    return (num == "1" and sub in "abc") or (num == "4" and sub in "abcd")

def _tnm_n_ok(v):
    s = _strip_tnm_prefix(v)
    if s in TNM_NEUTRAL_LOWER or s in ("x", "nx"):
        return True
    return bool(re.match(r"^n?([0-3])(mi|[a-c])?$", s))

def _tnm_m_ok(v):
    s = _strip_tnm_prefix(v)
    if s in TNM_NEUTRAL_LOWER or s in ("x", "mx"):
        return True
    return bool(re.match(r"^m?[01]$", s))

def _tnm_stage_ok(v):
    s = str(v).strip()
    if s.lower() in TNM_NEUTRAL_LOWER:
        return True
    # 笼统表述必须先判：若先 replace("期") 会把「晚期/早期/局部晚期」截成「晚/早/局部晚」而漏判
    if s in ("局部晚期", "局部进展", "早期", "晚期", "中晚期", "局部晚期乳腺癌"):
        return True
    s = s.replace("期", "").replace(" ", "").replace("\u3000", "")
    for zh, en in (("Ⅰ", "i"), ("Ⅱ", "ii"), ("Ⅲ", "iii"), ("Ⅳ", "iv")):
        s = s.replace(zh, en)
    return bool(re.match(r"^(0|i{1,3}|iv)[abc]?$", s.lower()))

def _tnm_prefix_ok(v):
    s = str(v).strip().lower()
    return s in TNM_PREFIX_OK or s in TNM_NEUTRAL_LOWER

def _tnm_misfit(key, v):
    s = _strip_tnm_prefix(v)
    if not s or s in TNM_NEUTRAL_LOWER:
        return False
    head = s[0]
    return head in ("t", "n", "m") and head != key

def _grading_ok(v):
    if v.strip().lower() in GRADING_NEUTRAL_LOWER:
        return True
    s = v.strip().lower()
    return any(p.lower() in s for p in GRADING_PREFIXES)

def _nature_ok(v):
    s = v.strip()
    return any(p in s for p in NATURE_TOKENS)

def _flag_ok(v):
    s = v.strip()
    return any(p in s for p in FLAG_TOKENS)

# 医保 ICD 码：字母开头含数字；放行星剑号双编码（B37.301+N77.1*）。
ICD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*(?:\+[A-Za-z][A-Za-z0-9.]*\*?)?\*?$")

def walk_values(o, key, out=None):
    if out is None:
        out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key and isinstance(v, str) and v:
                out.append(v)
            walk_values(v, key, out)
    elif isinstance(o, list):
        for v in o:
            walk_values(v, key, out)
    return out

def _walk_kv(o, path="", skip_qc=True):
    if isinstance(o, dict):
        for k, v in o.items():
            if skip_qc and k == "qc":
                continue
            p = "%s.%s" % (path, k) if path else k
            if isinstance(v, str):
                if v.strip():
                    yield p, k, v
            else:
                yield from _walk_kv(v, p, skip_qc)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_kv(v, path + "[]", skip_qc)

def collect_enum_scoped(rec):
    out = []

    def add(key, label, v):
        if isinstance(v, str) and v and v.strip():
            out.append((key, "%s.%s" % (label, key) if label else key, v))

    d = rec.get("diagnoses")
    items = d.get("items") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        add("setting", "diagnoses.items[%d]" % i, it.get("setting"))
        add("laterality", "diagnoses.items[%d]" % i, it.get("laterality"))

    t = rec.get("treatment")
    t_items = t.get("items") if isinstance(t, dict) else (t if isinstance(t, list) else [])
    for i, it in enumerate(t_items or []):
        if not isinstance(it, dict):
            continue
        add("modality", "treatment.items[%d]" % i, it.get("modality"))
        add("laterality", "treatment.items[%d]" % i, it.get("laterality"))

    ax = rec.get("auxiliary_exam") or {}
    if isinstance(ax, dict):
        ihc = ax.get("ihc") or {}
        for i, pn in enumerate(ihc.get("panels") or [] if isinstance(ihc, dict) else []):
            if not isinstance(pn, dict):
                continue
            add("laterality", "auxiliary_exam.ihc.panels[%d]" % i, pn.get("laterality"))
            for j, mk in enumerate(pn.get("markers") or []):
                if isinstance(mk, dict):
                    add("group", "auxiliary_exam.ihc.panels[%d].markers[%d]" % (i, j),
                        mk.get("group"))
        for key, name in (("reports", "auxiliary_exam.reports"),
                          ("report_groups", "auxiliary_exam.report_groups"),
                          ("labs", "auxiliary_exam.labs")):
            for i, it in enumerate(ax.get(key) or []):
                if isinstance(it, dict):
                    add("laterality", "%s[%d]" % (name, i), it.get("laterality"))

    o = rec.get("orders") or {}
    if isinstance(o, dict):
        for i, it in enumerate(o.get("items") or []):
            if isinstance(it, dict):
                add("laterality", "orders.items[%d]" % i, it.get("laterality"))
    return out

def collect_kind_scoped(rec):
    vals = set()
    o = rec.get("orders")
    if isinstance(o, dict):
        for it in (o.get("items") or []):
            k = it.get("kind") if isinstance(it, dict) else None
            if isinstance(k, str) and k:
                vals.add(k)
    pi = rec.get("present_illness")
    if isinstance(pi, dict):
        for lst_name in ("past_treatment", "care_history"):
            for it in (pi.get(lst_name) or []):
                k = it.get("kind") if isinstance(it, dict) else None
                if isinstance(k, str) and k:
                    vals.add(k)
    return vals

def collect_tnm_scoped(rec):
    out = []
    d = rec.get("diagnoses")
    items = d.get("items") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        t = it.get("tnm")
        objs = t if isinstance(t, list) else [t]
        for j, o in enumerate(objs):
            if not isinstance(o, dict):
                continue
            base = "diagnoses.items[%d].tnm" % i
            if isinstance(t, list):
                base += "[%d]" % j
            for k in ("prefix", "t", "n", "m", "stage"):
                v = o.get(k)
                if isinstance(v, str) and v.strip():
                    out.append((base + "." + k, k, v))
    return out

# 只限定在本板块 `reports[]` 内比对：`body_site` 在 detail／lesions 等位置是开放描述，
# 全量 walk 会把合法记录判失败（旧注释「body_site 不卡封闭词表」即指此）。
AUX_REPORT_TYPES = {"超声", "钼靶", "X线", "CT", "MRI", "核医学", "分子与基因检测",
                    "骨密度", "病理", "心电", "造影", "内镜", "其他"}
AUX_BODY_SITES = {"乳腺", "甲状腺", "甲状旁腺", "胸壁", "肝", "胆", "胰", "脾", "肾",
                  "肺", "纵隔", "胸膜腔", "骨", "盆腔",
                  "腋窝淋巴结", "颈部淋巴结", "锁骨上淋巴结", "内乳淋巴结", "其他"}
AUX_LATERALITY = {"右", "左", "双", "未指明"}
AUX_DATE_RE = re.compile(r"^\d{4}(-\d{1,2}(-\d{1,2})?)?$")
AUX_DATE_PH = {"未指明", "未提及", "未标注", "未注明", "未填", "未写", "空", "无", "不详",
               "未知", "不明", "none", "null", "unknown", "-", "—", "/", "∅", "n/a", "na"}

def validate_aux(rec, rep):
    aux = rec.get("auxiliary_exam")
    if not isinstance(aux, dict):
        return

    for i, r in enumerate(aux.get("reports") or []):
        if not isinstance(r, dict):
            rep["aux"].append({"at": "auxiliary_exam.reports[%d]" % i,
                               "error": "单元非对象", "value": type(r).__name__})
            continue
        at = "auxiliary_exam.reports[%d]" % i

        rt = r.get("report_type")
        if rt not in AUX_REPORT_TYPES:
            if r.get("enum_leak"):
                # 检验类误落报告端口：契约禁止塞「其他」，只能挂账等结构层路由
                rep["aux_pending"].append({"at": at + ".report_type",
                                           "error": "检验错位待路由", "value": rt})
            else:
                rep["aux"].append({"at": at + ".report_type", "error": "闭集外", "value": rt})

        bs = r.get("body_site")
        if bs not in ("", None) and bs not in AUX_BODY_SITES:
            rep["aux"].append({"at": at + ".body_site", "error": "闭集外", "value": bs})

        lat = r.get("laterality")
        if lat not in AUX_LATERALITY:
            rep["aux"].append({"at": at + ".laterality", "error": "闭集外", "value": lat})

        imps = r.get("impressions")
        if isinstance(imps, str):
            rep["aux"].append({"at": at + ".impressions", "error": "整段为字符串",
                               "value": imps[:40]})
        elif isinstance(imps, list):
            for j, x in enumerate(imps):
                if not isinstance(x, dict):
                    rep["aux"].append({"at": at + ".impressions[%d]" % j,
                                       "error": "元素非对象", "value": str(x)[:40]})
                elif not str(x.get("text") or "").strip():
                    if x.get("text_key_variant"):
                        # 模型用了别的键名装内容：信息在，只是没落到 text，挂账不判失败
                        rep["aux_pending"].append({"at": at + ".impressions[%d]" % j,
                                                   "error": "结论键名变体",
                                                   "value": x["text_key_variant"]})
                    else:
                        rep["aux"].append({"at": at + ".impressions[%d].text" % j,
                                           "error": "text空", "value": ""})

        v = str(r.get("exam_date") or "").strip()
        if v:
            if v in AUX_DATE_PH:
                rep["aux"].append({"at": at + ".exam_date", "error": "占位词未清", "value": v})
            elif not AUX_DATE_RE.match(v):
                rep["aux"].append({"at": at + ".exam_date", "error": "形态非日期", "value": v})
        elif r.get("exam_date_absent"):
            rep["aux_pending"].append({"at": at + ".exam_date", "error": "原文未载明日期",
                                       "value": ""})

    for i, l in enumerate(aux.get("labs") or []):
        if not isinstance(l, dict):
            continue
        at = "auxiliary_exam.labs[%d]" % i
        v = str(l.get("exam_date") or "").strip()
        if v and (v in AUX_DATE_PH or not AUX_DATE_RE.match(v)):
            rep["aux"].append({"at": at + ".exam_date", "error": "形态非日期", "value": v})
        has_name = bool(str(l.get("panel_name") or "").strip())
        has_res = bool(l.get("results") or [])
        if not has_name and not has_res:
            # 原文只写「复查正常」而无指标：空壳是事实，不是抽取缺陷
            rep["aux_pending"].append({"at": at, "error": "无组合名与明细", "value": ""})

def validate_record(rec, record_id=None):
    rep = {"record_id": record_id or rec.get("record_id"),
           "blocks_missing": [], "vocab": [], "tnm": [], "aux": [], "aux_pending": []}

    for k in REQUIRED_TOP:
        if k not in rec:
            rep["blocks_missing"].append(k)

    # body_site 在肿瘤场景下为开放字段（任何器官都可能是病灶/转移部位，
    # 如 乳腺/甲状腺/肝/肺/骨/脑/胰腺/胆囊…），不卡封闭词表，避免把合法记录判失败；
    # 封闭枚举严格校验，但**只在契约定义位置**（见 collect_enum_scoped 说明）。
    ENUM_SETS = {"laterality": LATERALITY, "group": MARKER_GROUP,
                 "setting": SETTING, "modality": MODALITY}
    for key, label, v in collect_enum_scoped(rec):
        if v not in ENUM_SETS[key]:
            rep["vocab"].append({"key": key, "at": label, "value": v})
    # kind 限定到契约定义位置（orders.items + present_illness.past_treatment/care_history），
    # 剥离 symptoms[].kind 等未定义位置的误用
    for v in collect_kind_scoped(rec):
        if v not in (KIND | TREATMENT_KIND):
            rep["vocab"].append({"key": "kind", "value": v})
    # flag / nature / grading_system / icd_yb_code：语义全库一致，按 key 泛收，
    # 但跳过 qc 子树（qc 内是自由备注，如 qc.flag="low_conf"、qc.grading_system="未分类"，
    # 按值域校验必然假阳）。同一值只报一次，避免多条目重复计数。
    _STR_CHECKS = {"flag": _flag_ok, "nature": _nature_ok, "grading_system": _grading_ok}
    _seen = set()
    for _p, _key, _v in _walk_kv(rec):
        if _key in ("laterality", "group", "setting", "modality", "kind"):
            continue  # 已由 collect_enum_scoped / collect_kind_scoped 按位置处理
        if _key in _STR_CHECKS:
            if not _STR_CHECKS[_key](_v) and (_key, _v) not in _seen:
                _seen.add((_key, _v))
                rep["vocab"].append({"key": _key, "value": _v})
        elif _key == "icd_yb_code":
            # 医保版 ICD：字母开头含数字；星剑号双编码（B37.301+N77.1*）是标准写法，须放行。
            s = _v.strip()
            if s and not ICD_RE.match(s) and (_key, s) not in _seen:
                _seen.add((_key, s))
                rep["vocab"].append({"key": _key, "value": _v})

    # 限定位置 diagnoses.items[].tnm；只验「填的值对不对」，不验「该填的填没填」。
    # 漏抽（原文有 pTNM 而 tnm 空）由 研究分析/90_qa_report/audit_tnm_coverage.py 单独审计，此处不判失败。
    _tnm_ok = {"prefix": _tnm_prefix_ok, "t": _tnm_t_ok, "n": _tnm_n_ok,
               "m": _tnm_m_ok, "stage": _tnm_stage_ok}
    for label, key, v in collect_tnm_scoped(rec):
        if key in ("t", "n", "m") and _tnm_misfit(key, v):
            rep["tnm"].append({"at": label, "error": "字段错位", "value": v})
        elif not _tnm_ok[key](v):
            rep["tnm"].append({"at": label, "error": "形态越界", "value": v})

    validate_aux(rec, rep)

    return rep

def rep_ok(rep):
    return not (rep["blocks_missing"] or rep["vocab"] or rep["tnm"] or rep["aux"])

def print_rep(rep):
    print("=== %s ===" % rep["record_id"])
    print("板块齐备: %s" % ("齐全" if not rep["blocks_missing"] else "缺 %s" % rep["blocks_missing"]))
    print("值域越界: %s" % (rep["vocab"] if rep["vocab"] else "无"))
    print("TNM 问题: %s" % (rep["tnm"] if rep["tnm"] else "无"))
    print("辅检问题: %s" % (rep["aux"] if rep["aux"] else "无"))
    print("辅检待处置: %d 项" % len(rep.get("aux_pending") or []))

def validate(record_id, rewrite=True):
    # 优先读扁平合并目录；回退到旧位置（assemble v3 前写入的）
    p_flat = os.path.join(ASSEMBLED_ROOT, "%s.json" % record_id)
    p_old = os.path.join(AGENT_ROOT, record_id, "%s.json" % record_id)
    path = p_flat if os.path.exists(p_flat) else p_old
    if not os.path.exists(path):
        print("✗ 未找到合并产物 %s（先跑 assemble.py，或用 --tar 直接校验流式产物）" % path)
        return 1
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)

    rep = validate_record(rec, record_id)

    if rewrite:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)

    print_rep(rep)
    return 0 if rep_ok(rep) else 2

def validate_tar(tar_path, show=10):
    sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
    from tarstream import iter_records  # noqa: E402

    stat = {"total": 0, "pass": 0, "fail": 0,
            "blocks_missing": 0, "vocab": 0, "tnm": 0, "aux": 0,
            "aux_pending_rec": 0, "aux_pending_item": 0}
    by_tnm = {}
    by_aux = {}
    samples = []
    for fn, rec in iter_records(tar_path):
        stat["total"] += 1
        rid = os.path.basename(fn).replace(".json", "")
        rep = validate_record(rec, rid)
        if rep["aux_pending"]:
            stat["aux_pending_rec"] += 1
            stat["aux_pending_item"] += len(rep["aux_pending"])
        if rep_ok(rep):
            stat["pass"] += 1
            continue
        stat["fail"] += 1
        if rep["blocks_missing"]:
            stat["blocks_missing"] += 1
        if rep["vocab"]:
            stat["vocab"] += 1
        if rep["tnm"]:
            stat["tnm"] += 1
            for it in rep["tnm"]:
                k = (it["at"].rsplit(".", 1)[-1], it["error"])
                by_tnm[k] = by_tnm.get(k, 0) + 1
            if len(samples) < show:
                samples.append({"record_id": rid, "tnm": rep["tnm"]})
        if rep["aux"]:
            stat["aux"] += 1
            for it in rep["aux"]:
                k = (it["at"].split("]")[-1].lstrip(".") or it["at"].rsplit(".", 1)[-1], it["error"])
                by_aux[k] = by_aux.get(k, 0) + 1
            if len(samples) < show:
                samples.append({"record_id": rid, "aux": rep["aux"]})

    print("=== tar 校验：%s ===" % tar_path)
    print("记录 %(total)d | pass %(pass)d | fail %(fail)d | "
          "板块缺 %(blocks_missing)d | 值域越界 %(vocab)d | TNM 问题 %(tnm)d | "
          "辅检问题 %(aux)d" % stat)
    if by_tnm:
        print("TNM 问题分布（字段 / 类型 → 条数）：")
        for (k, e), n in sorted(by_tnm.items(), key=lambda x: -x[1]):
            print("  %-8s %-6s %d" % (k, e, n))
    if by_aux:
        print("辅检问题分布（位置 / 类型 → 条数）：")
        for (k, e), n in sorted(by_aux.items(), key=lambda x: -x[1]):
            print("  %-34s %-12s %d" % (k, e, n))
    print("辅检待处置（原文缺失类，不计失败）：%d 项 / %d 条记录"
          % (stat["aux_pending_item"], stat["aux_pending_rec"]))
    for s in samples:
        print("  ✗ %s %s" % (s["record_id"], s.get("tnm") or s.get("aux")))
    return stat

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("record_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tar", metavar="PATH",
                    help="直接校验 tar 流产物（如 publish/.../_packed/assembled_v7_final.tar）")
    a = ap.parse_args()
    if a.tar:
        stat = validate_tar(a.tar)
        sys.exit(0 if stat["fail"] == 0 else 2)
    if a.all:
        ids = [os.path.basename(p) for p in glob.glob(AGENT_ROOT + "/*") if os.path.isdir(p)]
    else:
        ids = [a.record_id]
    rc = 0
    for rid in ids:
        rc = max(rc, validate(rid))
    sys.exit(rc)

if __name__ == "__main__":
    main()
