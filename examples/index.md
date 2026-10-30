# 证据索引（由 src/run_examples.py 生成）

模型模式：`mock`｜场景数：8

| 场景 | 验收点 | 轮数 | 每轮状态 | 引用 | 质量校验 | 关键结论 |
|---|---|---|---|---|---|---|
| [Q3-01](traces/Q3-01.trace.json) | 明确答案 | 1 | completed | 第1轮 ['D01'] | 通过 | 第1轮：引用 D01 |
| [Q3-02](traces/Q3-02.trace.json) | 文档冲突 | 1 | completed | 第1轮 ['D01', 'D02'] | 通过 | 第1轮：冲突待确认 |
| [Q3-03](traces/Q3-03.trace.json) | 资料外问题 | 1 | completed | 第1轮 [] | 通过 | 第1轮：资料缺口 |
| [Q3-04](traces/Q3-04.trace.json) | 改写后的相似问题 | 1 | completed | 第1轮 ['D01'] | 通过 | 第1轮：引用 D01 |
| [Q3-05](traces/Q3-05.trace.json) | 上下文追问 | 2 | completed / completed | 第1轮 ['D03']；第2轮 ['D03'] | 通过 | 第1轮：引用 D03；第2轮：引用 D03 |
| [Q3-06](traces/Q3-06.trace.json) | 工具失败 | 1 | completed | 第1轮 [] | 通过 | 第1轮：资料缺口 |
| [Q3-07](traces/Q3-07.trace.json) | 文档冲突 | 1 | completed | 第1轮 ['D06', 'D12'] | 通过 | 第1轮：冲突待确认 |
| [Q3-08](traces/Q3-08.trace.json) | 执行上限 | 1 | incomplete_limit_reached | 第1轮 [] | 通过 | 第1轮：资料缺口 |

每份 trace 含每轮完整工具轨迹、回答、逐条引用（doc_id + 逐字原文）、无效引用、
质量校验（引用支撑/无依据/冲突未确认）、资料缺口与待确认状态。
