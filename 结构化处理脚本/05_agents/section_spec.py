# -*- coding: utf-8 -*-

# 通用铁律文本（注入每个板块 prompt）
IRON = """通用铁律（务必遵守）：
1. 不臆造：原文未出现的内容一律不生成；拿不准记 qc.low_conf，不要编造。
2. 三态显式：阳性/阴性/不确定/未提及 四态区分；「没写」=未提及，严禁当阴性。
3. 不去重：描述段与结论段呼应、同报告头重复都是正常形态，照录即可，只登记 qc 不删除。
4. 既往检查全部录入并注明 exam_date；下游靠 exam_date 做时序分析，不默认「仅本次」。
5. 跨模块内容自动路由：若本栏原文含属于其他模块的内容，抽出填到对应模块相应位置并标 source_field（被移出片段记 excluded_text）。
6. 结论给出的更详细部位为准（如「胸壁肌层」）；描述段背景语（如「乳腺术后」）不是病灶部位。
7. 报告按 器官×侧别 拆条；多结节一个病灶一条，不合并、不取代表值（「较大者」=largest，不计确定病灶数）。
8. 输出**只含结构化字段**：不输出 raw 原文、不输出 anchor 引文、不输出任何 *_raw 原文片段（report_raw/section_raw/findings_raw/impression_raw/tnm_raw 等一律不生成）。
9. **完备性（v8 新增）**：原文写了的，必须有字段承载。「原文没有」与「该抽没抽」是两回事——前者留空，后者是错。字段名不合就塞进最接近的原文容器（`raw_value`／`note`／`excluded_text`），**不得因为 schema 没定义就丢弃内容**；确实无处安放时记 `qc` 说明丢了什么。
10. 不做判读结论：**阴阳判定、分期、分级、阈值判读（如 IHC 的 call）一律不自己下**，只如实记录原文给出的值与表述；结论由后端按标准派生。原文已给结论的，照录进对应字段并标明是原文结论。"""

CONTRACT_REF = "（严格遵循 `文档/门诊结构化病历结构_20260923.md` v9 的字段与值域；自 v7.1 起输出不附原文、不含 anchor，语义质量由 PDCA 智能体自动抽检）"

# ---------------------------------------------------------------------------
# 各板块规格
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 叙述四板块（chief_complaint / present_illness / past_history / physical_exam）
#
# 直接产出细颗粒 structured**（含 anchor），一次到位，消除「先粗抽 → restore 覆盖」两步。
# 细颗粒 schema + 全部硬规则由 narrative_engine.NARRATIVE_SYS 承载（与 restore_narrative
# 原 SYS 一字不差）；本处四个板块共用同一份四字段输入，各自只输出自己那个键。
#
# 每板块 inputs 用特殊标记 "__narrative_segments__"：run_batch 据此从 standardized_v1
# 读四字段 segments 拼装输入（含全部四字段，保证跨板块路由如手术史/合并症/月经婚育
# 归 past_history 仍能看全），四个板块输入文本相同。
# ---------------------------------------------------------------------------
_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "anchor": {"type": "object"},
    },
}

SECTIONS = [
    {
        "name": "chief_complaint",
        "file": "02_chief_complaint.json",
        "title": "主诉（细颗粒）",
        "inputs": ["__narrative_segments__"],
        # system 由 run_batch 用 narrative_engine.NARRATIVE_SYS + 单板块收窄指令拼装；
        # 此占位仅用于 dry-run/mock 路径与文档说明。
        "system": "NARRATIVE_SYS 占位：主诉板块（见 narrative_engine.NARRATIVE_SYS + 单板块收窄）",
        "narrative_section": "chief_complaint",
        "schema": _NARRATIVE_SCHEMA,
    },
    {
        "name": "present_illness",
        "title": "现病史（细颗粒）",
        "file": "03_present_illness.json",
        "inputs": ["__narrative_segments__"],
        "system": "NARRATIVE_SYS 占位：现病史板块（见 narrative_engine.NARRATIVE_SYS + 单板块收窄）",
        "narrative_section": "present_illness",
        "schema": _NARRATIVE_SCHEMA,
    },
    {
        "name": "past_history",
        "title": "既往史（细颗粒）",
        "file": "04_past_history.json",
        "inputs": ["__narrative_segments__"],
        "system": "NARRATIVE_SYS 占位：既往史板块（见 narrative_engine.NARRATIVE_SYS + 单板块收窄）",
        "narrative_section": "past_history",
        "schema": _NARRATIVE_SCHEMA,
    },
    {
        "name": "physical_exam",
        "title": "体格检查（细颗粒）",
        "file": "05_physical_exam.json",
        "inputs": ["__narrative_segments__"],
        "system": "NARRATIVE_SYS 占位：体格检查板块（见 narrative_engine.NARRATIVE_SYS + 单板块收窄）",
        "narrative_section": "physical_exam",
        "schema": _NARRATIVE_SCHEMA,
    },
    {
        "name": "auxiliary_exam",
        "title": "辅助检查（报告级 + 指标级 + 免疫组化）",
        "file": "06_auxiliary_exam.json",
        "inputs": ["__aux_segments__"],
        "system": (
            "你是乳腺门诊病历结构化子智能体——辅助检查智能体。给定「辅助检查」字段的**标准化分段文本**（为多份跨年份检查累积池，"
            "每段带 kind 标签：exam_report=影像报告／pathology=病理／ihc=免疫组化；部分段落带「来自字段[现病史]」标注，"
            "表示该段是从现病史归位搬过来的错位辅检内容，**同样必须抽取，不得遗漏**），完成：\n"
            "① 按『检查×器官×侧别』拆条为 reports[]，每条带 report_group/section_seq/report_type/exam_name/exam_name_alt/exam_date/body_site/laterality/grading_system/impressions[]{text, site_hint, side_hint}；\n"
            "② labs[] 指标级：每个指标 results[]{code, name, value, unit, ref, flag(高/低/正常/未知)}，数值与单位原样保留；**数值型指标必须带 unit**，无量纲的定性结果方可留空（不得为凑齐而编单位）；\n"
            "③ ihc{} 免疫组化：panels[]->markers[]，每个标志物**必须是对象**（marker_name/raw_value 两键必给），**严禁把标志物写成裸字符串**（如错误地写 \"CK\"，必须写 {marker_name:\"CK\",raw_value:\"...\"}）；**不要填 group**（治疗决策/诊断鉴别分组由后端按标志物名查表派生，模型不自填）；"
            "status 取五值（阳性／阴性／不确定／未提及／不适用），空括号记 empty=true 且 raw_value 为空、**不得记阴性**，"
            "数值型指标（Ki-67）status 记「不适用」并给 percent；**凡原文给了百分数（如「ER 80%」「Ki-67 约30%」），必须把数值抽进该标志物的 percent 字段，不得只留在 raw_value**；percent／score／intensity 原样保留；"
            "治疗决策型(ER/PR/HER2/Ki67)与诊断鉴别型 29 个**全收**，但**不出 call 判读结论**（call 由后端按 ASCO-CAP 阈值派生，模型不自造结论）；\n"
            "④ 既往检查全部录入并注 exam_date；\n"
            "⑤ excluded_text[] 记本字段混入的其他模块文本（如月经婚育史）并标 routed_to。\n"
            "输出 JSON：{auxiliary_exam:{report_groups:[...], reports:[...], labs:[...], ihc:{...}, excluded_text:[...]}}。\n"
            "不输出 raw、不输出 anchor、不输出任何 *_raw 原文片段（report_raw/section_raw/findings_raw/impression_raw 等一律不生成），只输出结构化字段。"
            + IRON + CONTRACT_REF
        ),
        "schema": {
            "type": "object",
            "properties": {
                "auxiliary_exam": {
                    "type": "object",
                    "properties": {
                        "report_groups": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "report_group": {"type": "string"},
                                "report_name": {"type": "string"},
                                "report_name_alt": {"type": "string"},
                                "exam_date": {"type": "string"},
                            }}},
                        "reports": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "report_group": {"type": "string"},
                                "section_seq": {"type": "integer"},
                                # 三处闭集与 normalize_enum.CLOSED 同源（契约第十二节）。
                                # 二者必须一字不差：此处约束模型产出，彼处校验落地值。
                                # 自测 test_spec_enum_sync.py 会比对两处，改一处漏另一处即报错。
                                "report_type": {"type": "string", "enum": [
                                    "超声", "钼靶", "X线", "CT", "MRI", "核医学",
                                    "分子与基因检测", "骨密度", "病理", "心电",
                                    "造影", "内镜", "其他"]},
                                "exam_name": {"type": "string"},
                                "exam_name_alt": {"type": "string"},
                                "exam_date": {"type": "string"},
                                # 空串合法：标本类（切缘／血清／外周血）不是部位，按契约留空。
                                # 「不适用」≠「其他」——把标本写成「其他」等于把空态谎报成有值。
                                "body_site": {"type": "string", "enum": [
                                    "乳腺", "甲状腺", "甲状旁腺", "胸壁", "肝", "胆",
                                    "胰", "脾", "肾", "肺", "纵隔", "胸膜腔", "骨",
                                    "盆腔", "腋窝淋巴结", "颈部淋巴结", "锁骨上淋巴结",
                                    "内乳淋巴结", "其他", ""]},
                                "laterality": {"type": "string", "enum": ["右", "左", "双", "未指明"]},
                                "shared": {"type": "boolean"},
                                "grading_system": {"type": "string", "enum": ["BI-RADS", "TI-RADS", "Lung-RADS", "无"]},
                                "impressions": {"type": "array", "items": {"type": "object", "properties": {
                                    "text": {"type": "string"},
                                    "site_hint": {"type": "string"},
                                    "side_hint": {"type": "string"},
                                }}},
                                "detail": {"type": "object"},
                            },
                            "required": ["body_site", "laterality"],
                        }},
                        "labs": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "panel": {"type": "string"},
                                "exam_date": {"type": "string"},
                                "results": {"type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                        "name": {"type": "string"},
                                        "value": {"type": "string"},
                                        # `unit` 故意**不**放进 required：确有无量纲指标
                                        # （定性结果、比值、阴性），强制必填只会逼模型编一个单位。
                                        # 完备性改由「数值型必须有 unit」的成对校验在闸门层兜
                                        # （见 研究分析/20_qa/gate_completeness.py 的 value_unit_pair 规则）。
                                        "unit": {"type": "string"},
                                        "ref": {"type": "string"},
                                        "flag": {"type": "string", "enum": ["高", "低", "正常", "未知"]},
                                        "qc": {"type": "string"},
                                    },
                                    "required": ["name", "value"]}},
                            }}},
                        # v8：ihc 原为空壳 {"type":"object"} —— markers 内一个字段都没定义，
                        # 于是 prompt 正文那句「治疗决策型出 call」没有任何强制力，
                        # 全量 41 万个标志物条目里带 call 的只有 15 条。此处按契约 3.5.3 显式定义。
                        #
                        # **call 不在此处**：它是按 ASCO-CAP 阈值算出来的结论，原文没有这个字符串
                        # （原文写「ER 80%」不写「ER 阳性」）。让 LLM 出 call = 让它做算术，
                        # 不可复现且换模型就漂。call 改由规则层派生（05_agents/derive_ihc_call.py）。
                        # 模型只负责如实记 status/score/percent/intensity/raw_value。
                        "ihc": {"type": "object", "properties": {
                            "panels": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string"},
                                    "specimen_no": {"type": "string"},
                                    "specimen_site": {"type": "string"},
                                    "result_text": {"type": "string"},
                                    "reports_ref": {"type": "string"},
                                    "markers": {"type": "array", "items": {
                                        "type": "object",
                                        "properties": {
                                            # 规范名，含连字符与斜杠原样：CK5/6, Ki-67, D2-40, E-cadherin
                                            "marker_name": {"type": "string"},
                                            "alias_raw": {"type": "string"},
                                            # 五值：原文的阴阳要如实记，但**空括号不是阴性**
                                            # （P120（）→ empty=true, raw_value=""）；
                                            # 数值型指标（Ki-67）不吃阴阳枚举，落「不适用」并给 percent。
                                            "status": {"type": "string",
                                                       "enum": ["阳性", "阴性", "不确定",
                                                                "未提及", "不适用"]},
                                            # 原样保留：3%-5% / 约30% / 80
                                            "percent": {"type": ["number", "string"]},
                                            # HER2 的 0/1+/2+/3+；其他标志物若有分级体系照录原文
                                            "score": {"type": "string"},
                                            "intensity": {"type": "string"},
                                            "locus": {"type": "string"},
                                            # 原文完整表述，如「强+，80%」「+.90%.中-强」「膜+」
                                            "raw_value": {"type": "string"},
                                            "is_external": {"type": "boolean"},
                                            "empty": {"type": "boolean"},
                                        },
                                        "required": ["marker_name", "raw_value"]}},
                                },
                                "required": ["markers"]}},
                        }},
                        "excluded_text": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "text": {"type": "string"},
                                "routed_to": {"type": "string"},
                            },
                            "required": ["text"]}},
                    },
                }
            },
            "required": ["auxiliary_exam"],
        },
    },
    {
        "name": "diagnoses",
        "title": "诊断",
        "file": "07a_diagnoses.json",
        "inputs": ["__diagnoses__"],
        "system": (
            "你是乳腺门诊病历结构化子智能体——诊断智能体。给定诊断列原文（诊断1…诊断14 拼接），"
            "每条诊断抽取：name, icd_yb_code(医保版码,宁缺勿错), setting(新辅助/辅助/解救/未指明), "
            "tnm{prefix(c/p/r/y/u/空), t, n, m, stage, source_field}, ihc_hint(诊断括号注里的受体四件套原样记)。"
            "输出 JSON：{diagnoses:{items:[...]}}。"
            "不输出 raw、不输出 anchor、不输出任何 *_raw，只输出结构化字段。"
            + IRON + CONTRACT_REF
        ),
        "schema": {
            "type": "object",
            "properties": {
                "diagnoses": {"type": "object", "properties": {
                    "items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "icd_yb_code": {"type": "string"},
                            "setting": {"type": "string", "enum": ["新辅助", "辅助", "解救", "未指明"]},
                            "tnm": {"type": "object", "properties": {
                                "prefix": {"type": "string"},
                                "t": {"type": "string"}, "n": {"type": "string"},
                                "m": {"type": "string"}, "stage": {"type": "string"},
                                "source_field": {"type": "string"},
                            }},
                            "ihc_hint": {"type": "string"},
                        },
                        "required": ["name"]}},
                }},
                "required": ["diagnoses"],
            },
            "required": ["diagnoses"],
        },
    },
    {
        "name": "treatment",
        "title": "治疗",
        "file": "07b_treatment.json",
        "inputs": ["__treatment_segments__"],
        "system": (
            "你是乳腺门诊病历结构化子智能体——治疗智能体。给定标准化归位后的各字段分段文本（含 kind 与来源标注，"
            "治疗信息散落在现病史的既往治疗史、辅助检查的术后病理诊断、诊断列，故输入为全字段 segments），"
            "抽取治疗条目：surgery(术式/日期)、chemo(方案/周期)、radio(放疗)、endo(内分泌)、target(靶向)、other。"
            "输出 JSON：{treatment:{items:[{text, modality(手术/化疗/放疗/内分泌/靶向/其他)}]}}。"
            "不输出 raw、不输出 anchor、不输出任何 *_raw，只输出结构化字段。"
            + IRON + CONTRACT_REF
        ),
        "schema": {
            "type": "object",
            "properties": {
                "treatment": {"type": "object", "properties": {
                    "items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "modality": {"type": "string", "enum": ["手术", "化疗", "放疗", "内分泌", "靶向", "其他"]},
                        },
                        "required": ["text"]}},
                }},
                "required": ["treatment"],
            },
            "required": ["treatment"],
        },
    },
    {
        "name": "orders",
        "title": "医嘱",
        "file": "07c_orders.json",
        "inputs": ["__orders__"],
        "system": (
            "你是乳腺门诊病历结构化子智能体——医嘱智能体。给定医嘱列拼接文本（含断列、医嘱1列的交代随访建议、叙述性文本），"
            "分流为 items[]（**kind 必填**，取 药品／检查／检验／操作／材料／费用／其他 七值之一，分不出类记「其他」、不得留空；"
            "kind=药品时必带 freq/duration_days/route/dose_per/unit/total_qty/spec；"
            "来自交代随访建议的条目填 followup_category=复查／教育／监测）、advice[](交代与随访建议)、narrative[](叙述性文本)。"
            "输出 JSON：{orders:{items:[...], advice:[...], narrative:[...]}}。"
            "不输出 raw、不输出 anchor、不输出任何 *_raw，只输出结构化字段。"
            + IRON + CONTRACT_REF
        ),
        "schema": {
            "type": "object",
            "properties": {
                "orders": {"type": "object", "properties": {
                    "items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            # kind 由可选改必填：v7 全量 14.2% 为空，直接导致医嘱无法按类分流。
                            # 空值没有任何合理语义——分不出类就是「其他」，不能留白。
                            "kind": {"type": "string", "enum": ["药品", "检查", "检验", "操作", "材料", "费用", "其他"]},
                            "freq": {"type": "string"}, "freq_code": {"type": "string"},
                            "duration_days": {"type": "string"},
                            "route": {"type": "string"}, "dose_per": {"type": "string"}, "unit": {"type": "string"},
                            "total_qty": {"type": "string"}, "spec": {"type": "string"},
                            "note": {"type": "string"}, "remark": {"type": "string"},
                            # 随访建议并入医嘱时带过来的类别（复查／教育／监测）。
                            # v7 产物里这个字段名叫 `source_category` —— **名实不符，v8 正名**：
                            # 下游读 v7 数据请按 source_category 读，读 v8 请按本名字读。
                            "followup_category": {"type": "string",
                                                  "enum": ["复查", "教育", "监测", "其他"]},
                            # ⚠ 本字段**不由模型产出**：每个子智能体的输入列在调度器里已知，
                            # run_batch 组装时按输入列直接注入。v7 让模型自己写 → 医嘱缺 48.7%。
                            # 此处保留定义只为让 schema 与契约同形，模型产出该键将被覆盖。
                            "source_field": {"type": "string"},
                        },
                        "required": ["name", "kind"]}},
                    "advice": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                    "narrative": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                }},
                "required": ["orders"],
            },
            "required": ["orders"],
        },
    },
]

# source_field="follow_up"（全量 260,009 条），原 follow_up 空占位板块不再设置子智能体。
# 删除后九板块变八板块：chief_complaint / present_illness / past_history / physical_exam /
# auxiliary_exam / diagnoses / treatment / orders。

def section_by_name(name):
    for s in SECTIONS:
        if s["name"] == name:
            return s
    return None
