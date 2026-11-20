# 门诊病历结构化流水线

将中文乳腺专科门诊病历结构化为主办者自定义 JSON 契约的多智能体流水线。全量语料 80,718 条就诊记录（25,081 名患者），每条输出 12 个板块，原文逐字保留于 `raw`，模型抽取写入 `structured`，二者通过逐字引文锚点关联。

## 目录

- `结构化处理脚本/` — 工作流本体（40 个脚本）。`00_core/` 为共享核心模块，`05_agents/` 为取数、标准化、分板块抽取、组装校验、派生与归一化各步骤。详见该目录 README。
- `研究分析/` — 论文层脚本（43 个），对终态产物做数字计算、口径重算、人审质检与稿件溯源，不在工作流主链上。详见该目录 README。
- `examples/` — 两份脱敏后的完整结构化记录及字段说明。

## 处理路线

主链六步由 `05_agents/orchestrator.py` 编排：取数 → 标准化 → 分板块抽取 → 组装与硬闸门校验 → 派生 → 术语归一化。派生层依次完成 IHC 判读、TNM 原文回填、辅检枚举归一、BI-RADS 分级回填、医嘱七桶归一与契约字段补齐。语义判断由语言模型完成，规则层只处理确定事务（ICD 编码、侧别、单位、值域、IHC 判读映射）。

## 运行

设置环境变量 `BC_CLINIC_ROOT` 指向数据根目录，再执行：

```
python3 结构化处理脚本/05_agents/orchestrator.py
```

`orchestrator.py` 支持 `--dry-run` 与 `--until <step>`。调用语言模型需在自己的环境中提供兼容的服务地址与密钥。

## 输出形态

每条记录为一个 JSON 对象，含 `patient`、`visit`、`chief_complaint`、`present_illness`、`past_history`、`physical_exam`、`auxiliary_exam`、`diagnoses`、`treatment`、`orders`、`follow_up`、`qc` 十二个板块与 `meta`。辅助检查按器官与侧别拆条，免疫组化以 panel 为单位收纳标志物，医嘱条目归入药品/检查/检验/操作/费用/材料/其他七类。完整字段见 `examples/README.md`。
