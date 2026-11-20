# 结构化病历示例

两份来自终态产物的完整结构化记录，用于展示输出形态。已脱敏：登记号、病历ID、就诊医生、病理号/蜡块号均替换为占位值；正文未发现手机号、身份证号。

| 文件 | 场景 | 结构要点 |
|---|---|---|
| `47195738.json` | 初诊 · 单诊断深度结构化 | 1 条诊断、35 份报告、11 个 IHC panel（含 ER/PR/HER2/Ki-67 Calponin/P63/E-cadherin 等）、医嘱 30 条覆盖药品/操作/检验/检查四类 |
| `49338074.json` | 初诊 · 多诊断与转移分期 | 9 条诊断（含 ICD 与 TNM 字段）、23 份报告、2 个 IHC panel（含 HER2(FISH)）、6 条检验、现病史叙述 9 条症状条目 |

## 字段结构

每条记录为 12 个板块的扁平对象，加一个 `meta`：

- `patient` / `visit`：患者基本信息与就诊元数据。
- `chief_complaint`、`present_illness`、`past_history`、`physical_exam`、`treatment`、`follow_up`：叙述类板块，各含 `raw`（逐字原文）与 `structured`（分项抽取）。`structured` 内条目带 `anchor`，其中 `quote` 为原文逐字片段、`field` 为来源字段、`seg_start` 为段内位置。
- `auxiliary_exam`：辅助检查，含 `report_groups`（按器官×侧别归并的报告组）、`reports`（报告条目）、`labs`（检验条目，多种形态并存）、`ihc.panels`（免疫组化 panel，每 panel 含 `markers[]`）、`excluded_text`（移出文本）、`summary`。
- `diagnoses`：诊断条目，含 `name`、`icd_yb_code`、`icd_yb_name`、`side`、`setting`、`qc`，有分期信息时含 `tnm`（t/n/m/stage 原值及 `*_norm` 归一值）。
- `orders`：医嘱条目，三键并存（`items` 明细 / `advice` 建议 / `narrative` 叙述），每条 `items` 带 `kind`（药品/检查/检验/操作/费用/材料/其他七桶）。
- `qc`：记录级质检标记（`missing` / `low_conf` / `conflicts` / `review_status`）。
- `meta`：装配来源与各派生步骤的落位标记。

## 两类值的关系

每个叙述板块的 `raw` 是逐字原文，`structured` 是模型抽取结果，二者通过 `anchor.quote` 关联。IHC marker 以 `marker_name` 存标准名、`status`/`percent`/`score`/`intensity` 存判读值、`raw_value` 存原文转写。诊断 `tnm` 等派生字段由规则层从抽取结果计算，原值与 `*_norm` 归一值分别存放。
