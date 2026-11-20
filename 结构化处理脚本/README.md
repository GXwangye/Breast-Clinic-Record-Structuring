# 结构化处理脚本 —— 工作流本体

本目录是**病历结构化工作流本体**：从源 Excel 病历一路处理到结构化终态
`assembled_v7_final.tar` 的完整流水线。研究/论文层脚本在 `研究分析/`，与本层单向依赖
（研究层 import 本层模块，本层不感知研究层）。

## 环境变量

- `BC_CLINIC_ROOT` —— 项目根目录，所有脚本以此定位，运行前必须设置。

## 数据路径约定

- 唯一数据根：`$BC_CLINIC_ROOT/publish/02_结构化结果与过程`
- 源表：`$BC_CLINIC_ROOT/数据源/乳腺疾病门诊记录_20240915.xlsx`
- 终态 tar：`<数据根>/中间产物/_packed/assembled_v7_final.tar`

## 主链六步（orchestrator 编排）

| 步骤 | 脚本 | 作用 |
|---|---|---|
| ① 取数 | `fetch_records.py` | 源 Excel → input_records.jsonl |
| ② 预处理 | `standardize_raw.py` | 标准化 → standardized_v1 |
| ③ 抽取 | `run_batch.py` | 分板块抽取 → agents_v7 |
| ④ 收口 | `assemble_all.py` + `validate_all.py` | 组装 + 硬闸门校验 → assembled_v7 |
| ⑤ 派生 | `derive_ihc_call.py` 等 6 个 | IHC call → TNM 回填 → 辅检归一 → BI-RADS 回填 → orders.kind 归一 → 契约字段补齐 |
| ⑥ 归一化 | `run_canon_cycle.py` | 术语归一化小周期 → _norm |

运行：`python3 05_agents/orchestrator.py`（支持 `--dry-run`、`--until <step>`）。

## 00_core（核心模块，10 个）

| 脚本 | 作用 |
|---|---|
| `tarstream.py` | tar 流式读写（iter_records / count / write_json） |
| `validate.py` | 单条校验（契约值域/形态/枚举） |
| `ihc_standard.py` | IHC 判读标准与解析（parse_ihc / marker_kind） |
| `ihc_reports.py` | IHC 报告切块与匹配（mkey / blocks_of / match_block） |
| `kb_text.py` | 文本知识库（单位 / 缩写清洗） |
| `search_tools.py` | 知识库检索工具 |
| `side_normalizer.py` | 侧别剥离 |
| `llm_client.py` | LLM 调用客户端（OpenAI 兼容） |
| `icd_nhsa.py` | ICD / 医保码字典 |
| `fetch_loinc.py` | LOINC 字典获取 |
| `build_kb_index.py` | 知识库索引构建 |

## 05_agents（主链 + 派生 + 归一，30 个）

### 主链

| 脚本 | 作用 |
|---|---|
| `orchestrator.py` | 六步主链编排 |
| `fetch_records.py` | ① 取数 |
| `standardize_raw.py` | ② 预处理（LLM 段首标记 + 规则切片/归位/守恒） |
| `run_batch.py` | ③ 分板块抽取（八板块子智能体） |
| `section_spec.py` | 八板块 prompt 与 JSON Schema |
| `assemble.py` / `assemble_all.py` | ④ 单条装配 / 批量装配 |
| `validate.py` | 单条校验（00_core 亦有同名，此处为入口） |
| `validate_all.py` | ④ 批量硬闸门校验 |
| `narrative_engine.py` | 叙述还原规则层（anchor 反幻觉闸门） |
| `smoke_test.py` | 冒烟测试 gate |
| `pdca_agent.py` | PDCA 监督（anchor/异常率阈值，HALT） |
| `remediate_aux.py` | 辅检补充 + IHC 结构化补丁 |

### 派生六步

| 脚本 | 作用 |
|---|---|
| `derive_ihc_call.py` | IHC call 派生（ER/PR/HER2/Ki67 出 call） |
| `backfill_tnm_from_text.py` | TNM 回填（搬原文三段式） |
| `normalize_aux_enum.py` | 辅助检查形态/枚举归一 |
| `backfill_birads_from_text.py` | BI-RADS 分级回填 |
| `normalize_order_kind.py` | orders.kind 七桶归一 |
| `derive_contract_fields.py` | 契约字段补齐（性别/TNM 派生等） |

### 归一化小周期

| 脚本 | 作用 |
|---|---|
| `run_canon_cycle.py` | 归一小周期编排 |
| `collect_terms.py` | 抽全量术语字面量去重计数 |
| `fold_terms.py` | 术语折叠 |
| `normalize_canon.py` | 规范名判定 |
| `merge_step5.py` | 术语合并 |
| `regate_rejected.py` | 拒绝项重判 |
| `export_mapping.py` | 映射字典导出 |
| `normalize_enum.py` | B 类枚举值清洗 |
| `normalize_ihc.py` | IHC 归一（四态/分桶） |
| `clean_enum_values.py` | 值域写法归一 + 三态纠偏 |
| `ihc_alias_tables.py` | IHC 别名表 |

## 依赖方向

- 本层模块互相 import，全部在同目录（00_core / 05_agents）内。
- 研究层（`研究分析/`）单向 import 本层：`tarstream`、`validate`、`ihc_standard`、
  `normalize_enum`、`normalize_ihc`、`derive_ihc_call`、`normalize_order_kind`、`ihc_reports`。
- 本层**不 import 研究层任何模块**。
