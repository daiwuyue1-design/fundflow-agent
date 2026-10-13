---
name: fundflow-verify
description: 验收fundflow单个dataset交付物，执行完整版五项检查：非空交付物、章级/标注数值回溯、层级与洗钱证据、口径、表述红线和免责句，并输出PASS/FAIL及问题清单。
---

# 研判材料验收

显式激活：“按 fundflow-verify 技能验收这个dataset”。选择真实存在的 `agent/deliverables/<dataset_id>/`，不要对父目录或旧dataset混合验收。

运行项目源脚本：`.venv/Scripts/python.exe agent/verify/report_check.py <dataset目录>`。本包部署副本为 [checks/report_check.py](checks/report_check.py)，可用同一Python独立执行，两处字节保持一致；不自行改一份绕过失败。

检查五项（警告不自动判FAIL，问题才判FAIL）：

1. 交付物：01_intake.md、02_cleaned.csv、03_entities.csv、04_graph_stats.md、05_tiers.csv、05_laundering.csv、06_report.html、06_evidence_index.json；逐项非空，脚本生成07_verify.md并核存在；阶段表执行列级检查。
2. 证据完整：索引内每条层级嫌疑包含指标值/百分位/适用规则；洗钱每条含五维/阈值/命中说明；结构失败须在正文显式列出；空表或缺项FAIL。
3. 数值回溯：HTML data-tool/data-pointer标注值核对06_evidence_index.json工具结果，并对层级分布、节点/边、洗钱命中和失败文件做章级核对；无法唯一对应的数字只警告。**自由文本全部数字、旧可视化仍需人工复核**。
4. 口径：report-scope与索引“口径”及01体检金额单位/时间范围一致；正文单位声明与scope一致；带证据标注的金额检查元/万元换算；无法唯一绑定的金额提示人工复核。账号范围、流水/赌资及平台内转账明示，完整交易回算仍需人工。
5. 表述：可见案件结论禁用认定/定罪/犯罪事实；固定章名例外；必须含“供研判方向对照，最终认定以司法机关为准”类免责句，明确法源blockquote不作为案件结论。

输出07_verify.md中的PASS/FAIL、问题、警告与覆盖范围。FAIL退回对应工序修正，保留异常，不重新建模或补造证据。缺Trace、旧检测器失败、未核法源须在报告中列待核实，即使自动检查PASS也不表示真实会话、审批和案件事实已核验。验收前读主技能norms“验收防错”，需要法源时只读对应条目。
