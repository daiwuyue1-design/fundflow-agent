---
name: fundflow-investigation
description: 涉赌资金流研判七阶段工序；用于按步骤调用fundflow工具、保留证据与异常、产出线索报告并交付验收。
---

# 涉赌资金流研判

显式激活指令：“按 fundflow-investigation 技能执行”。先识别当前项目与输入范围，演示一律使用合成数据；没有用户授权不得读真实账单。工作产物写 `agent/deliverables/<dataset_id>/`，源技能不写入工序结果。

服务器启动期在主线程预载数值依赖，后台子进程只负责暖缓存。会话启动后约1分钟内先检查 `agent/deliverables/_warmup.json` 的本次 `done`，然后执行研判链；旧标记不能证明当前连接就绪。重连与首调验证由编排方负责。

每步可能弹审批：60秒内点“允许”；超时=自动拒绝，需重新发起。审批拒绝或工具返回失败时停止依赖步骤，报告原文原因，不能绕过、关闭审批或替换为未授权执行。阈值偏离、对外导出均须“提议（理由与范围）→等待用户确认→执行”；等待期间不推进依赖步骤。默认参数也是研判假设，不是法定罪名标准。

## 七阶段

1. **受理体检**：读取 [norms.md](references/norms.md)“受理与完整性”；调用 `scan_dataset`，保存 `dataset_id`，产出 `01_intake.md`（完整JSON见01_intake.json）。列文件清单、失败、时间范围、身份未核实比例，不根据错误文件推算交易。不在此阶段判层；工具失败先修正，不跳过异常。
2. **归一**：读取 norms.md“防错清单”的“跨账单重复/平台内转账/口径”；调用 `build_fund_graph`，产出 `02_cleaned.csv`。这是旧清洗tx，可能仍含未进入图的记录；不可称为图有效去重清单，也不可把流水当赌资。本工具一次完成阶段2–4底层计算，模型仍分别核对各阶段证据。
3. **实体归一**：读取 norms.md“实体与身份”；复核同次build返回的合并计数、产出 `03_entities.csv`。需要核查某规范节点时调用 `profile_account`；显示姓名是线索，不凭备注中文字或图节点数推算实际人数，不自动补造身份。
4. **建模**：读取 norms.md“资金图口径与环”；核对build的四层分项、节点边数与 `04_graph_stats.md`。有向环仍保图并显式阻断判层，不能擅自SCC凝缩；不变更旧双向去重规则。
5. **研判**：按需读 norms.md“角色识别决策树”“阈值口径与理由”，法源按结论类型读 [legal-basis.md](references/legal-basis.md) 对应条目。依次 `classify_tiers` → `detect_laundering` → `scan_structure` → `rank_suspects`；用 `trace_funds` 和 `profile_account` 补穿透与重点账号证据。产出 `05_tiers.csv`、`05_laundering.csv`、`05_structure.csv`、`05_ranking.csv`，追线时产出05_trace.csv/html。classify参数变化会重置节点、失效检测和排名，必须重跑洗钱、结构及rank；相同参数缓存命中不重置。检测失败是待核实，未检测≠无洗钱，命中≠概率。每条层级嫌疑必须引用指标值/百分位/适用规则三元组，无证据不下结论。
6. **输出**：读取 norms.md“报告与表述”、legal-basis对应条目及 [report-template.md](references/report-template.md)。对外导出先提议并等待确认，再调用 `export_evidence`；产出固定八章 `06_report.html`、`06_evidence_index.json`，旧可视化为analysis_report.html辅助材料。金额须带单位、时间、账号范围、平台内转账与流水/赌资口径。Trace由调用方回填，禁止伪造；不得写定罪性结论。法源不足保留“待核”，不把摘要当逐字引文。
7. **验收**：读取 norms.md“验收防错”；显式调用 fundflow-verify，执行其检查脚本，产出 `07_verify.md`。不重新设计算法、伪造上游结果或默默放行FAIL。PASS仅覆盖初版可核对项，人工待核实与未提供Trace仍须列明。

每阶段只读对应知识切片，不一次性通读references全文。保留工具的完整结果文件；超过50行读落盘表核对，返回摘要不等于完整证据。输出结论仅用“线索/嫌疑/符合……特征”，司法结论留给司法机关。
