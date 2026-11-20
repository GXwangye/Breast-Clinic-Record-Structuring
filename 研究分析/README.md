# 研究分析层 —— 脚本清单与数据路径

本目录收纳**研究/论文层**脚本：对工作流终态产物（`assembled_v7_final.tar`）做数字计算、
口径重算、回指核验、人审质检与稿件溯源。它们**不在工作流主链上**，按需单跑，只读产物、
输出落 `qa_reports/` 与 `_qa_out/`。

工作流本体（主链六步 + 派生六步 + 归一化）在 `结构化处理脚本/`（00_core、05_agents），
与本层单向依赖：本层 import 工作流模块（tarstream / validate / ihc_standard /
normalize_enum / derive_ihc_call / normalize_order_kind / ihc_reports），工作流不感知本层。

## 目录结构

```
研究分析/
  probe_granularity.py        判据定义点：就诊性质 / BI-RADS 有无分级与归一
  recount_structure_dims.py   判据定义点：marker 口径（keys_of / leaves / anchors）
  10_analysis/                研究分析（7 个）
  20_qa/                      审核包构建与结果分析（7 个）
  90_qa_report/               质检报告（27 个）
```

## 环境变量

- `BC_CLINIC_ROOT` —— 项目根目录，所有脚本以此定位，运行前必须设置。
- `AUX_LLM_KEY` —— `error_analysis_rq2.py` 调用 LLM 时的 API key（可选）。

## 数据路径约定

- 唯一数据根：`$BC_CLINIC_ROOT/publish/02_结构化结果与过程`
- 终态 tar：`<数据根>/中间产物/_packed/assembled_v7_final.tar`
- 全量输入：`<数据根>/中间产物/_agents_input/input_records.jsonl`
- 质检输出：`<数据根>/qa_reports/`、`<数据根>/中间产物/_qa_out/`

---

## 10_analysis（研究分析，7 个）

| 脚本 | 作用 | 输出 |
|---|---|---|
| `build_final_numbers.py` | 汇总终态关键数字（BI-RADS 记录级四格、值级回指、阳性率等） | `qa_reports/终态数字.json` |
| `build_result_ledger.py` | 生成稿件数字清单（唯一数字源，含内部一致性与论文反查两道闸） | `qa_reports/结果清单.md` + `.json` |
| `describe_raw_corpus.py` | 源表原始数据描述性统计 | `权威数据/原始数据描述性统计_20261027.json` |
| `describe_raw_fundamentals.py` | 源表原始数据基本面分析 | `权威数据/原始数据基本面_20261027.json` |
| `describe_structured_cohort.py` | 结构化队列描述（亚型/就诊性质/TNM 等分布） | `中间产物/_qa_out/结构化基础描述统计.json` |
| `profile_research_directions.py` | 研究方向可及性画像 | `中间产物/_qa_out/研究方向可及性画像_20261107.json` |
| `verify_usability.py` | 结构化产物可用性验证 | `中间产物/_qa_out/可用性验证_20261107.json` |

## 20_qa（审核包构建与结果分析，7 个）

| 脚本 | 作用 | 输出 |
|---|---|---|
| `build_review_pack.py` | 构建人工审核包（v1，取证与判据） | `qa_reports/审核包_20261112` |
| `build_review_pack_v2.py` | 构建人工审核包（v2，按块式取证） | `qa_reports/审核包_20261113` |
| `analyze_review_pack.py` | 分析审核结果（v1 口径） | `qa_reports/审核结果_20261112.md` |
| `analyze_review_pack_v2.py` | 分析审核结果（v2 口径） | `qa_reports/审核结果_20261113.md` |
| `analyze_reissue.py` | 补取证结果分析 | `qa_reports/补取证结果_20261113.md` |
| `reissue_evidence.py` | 补取证证据包构建 | `qa_reports/审核包_20261113_补取证` |
| `compare_tars.py` | 两个 tar 版本逐条比对 | — |

## 90_qa_report（质检报告，27 个）

### IHC 归属与值源

| 脚本 | 作用 |
|---|---|
| `probe_ihc_l2_anomaly.py` | 核查「L2 优于工作流」是否成立（反常识核查） |
| `probe_ihc_attribution.py` | IHC 反常识逐条归因 |
| `probe_ihc_crossfield_full.py` | 全量量化「IHC 写在现病史」规模 |
| `probe_ihc_route_reject.py` | IHC 断链定位（归位 vs 归属裁决） |
| `probe_ihc_subset_layer.py` | 100 条子集内分层 + 静默去向 |

### IHC 准确性量具

| 脚本 | 作用 |
|---|---|
| `probe_accuracy_calibrated.py` | IHC 四维准确性（正/负控） |
| `probe_value_source.py` | 值-源语义一致率 |
| `probe_anchor_coverage.py` | panel anchor 回指覆盖度 |
| `compute_ihc_source.py` | IHC 记录级召回 + source_field 覆盖 |

### 漏抽 / 缺陷探测

| 脚本 | 作用 |
|---|---|
| `probe_two_defects.py` | 空日期 / 值域越界两条缺陷实证 |
| `probe_emptydate_vs_source.py` | 空日期回原文归因 |
| `probe_standardize_degrade.py` | 标准化失败去向核验 |
| `audit_tnm_coverage.py` | TNM 空壳是「原文无」还是「漏抽」 |

### 口径重算

| 脚本 | 作用 |
|---|---|
| `recount_six_dims.py` | 六维三口径重算（定漏抽率） |
| `compute_item_alignment.py` | 条目级对齐（唯一无伪影漏抽） |
| `error_analysis_rq2.py` | RQ2 四类共性失败（规则候选 + LLM 定案） |

### 回指核验

| 脚本 | 作用 |
|---|---|
| `verify_birads_grounding.py` | BI-RADS 原文四格 + 归因 |
| `verify_stage_grounding.py` | 分期值回指原文 |
| `verify_error_homology.py` | 错判行同源 / 异源交叉核验 |
| `verify_review_key.py` | 答案键「单值」假设校验 |

### 归因 / 回测

| 脚本 | 作用 |
|---|---|
| `audit_error_attribution.py` | 错判逐行归因四分类 |
| `retest_fix.py` | 修复后回测（重比专家判定） |

### 工程自检

| 脚本 | 作用 |
|---|---|
| `verify_chain_reproducible.py` | 全链复现是否逐条同构 |
| `verify_gate_breakdown.py` | 闸门类别构成对账 |
| `audit_chain.py` | 链条完整性 / 镜像 / 编译 / 时序审计 |
| `test_spec_enum_sync.py` | 枚举闭集三方一致自测（无 main，import 用） |
| `probe_final_tar_provenance.py` | 终态 tar 工序留痕盘点 |

---

## 判据定义点（禁止建副本）

`probe_granularity.py` 与 `recount_structure_dims.py` 是判据的**单一权威定义点**，
被 10_analysis / 20_qa / 90_qa_report 反向 import。判据只此一处定义，禁止在别处自建副本。
