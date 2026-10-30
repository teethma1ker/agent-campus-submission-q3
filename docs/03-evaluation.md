# 03 评估

测试命令：`python3 -m unittest discover -s tests -v`（24 个测试通过，见 `examples/test-results.txt`）。
示例：`python3 -m src.run_examples`（8 个场景，见 `examples/index.md`；真实模型见 `examples/real-model/index.md`）。

| 验收点 | 预期 | 实际（证据） | 结果 |
|---|---|---|---|
| 明确答案 | 引用 D01，回答 30 天，无冲突 | `examples/traces/Q3-01.trace.json` | 通过 |
| 文档冲突 | 列出 30 天与 15 天原文，请求确认，不选一个 | `examples/traces/Q3-02.trace.json`：pending_confirmation=true，引用 D01+D02 | 通过 |
| 版本冲突 | 14 天 vs 7 天并列为冲突 | `examples/traces/Q3-07.trace.json` | 通过 |
| 资料外问题 | 明确说明无相关资料 | `examples/traces/Q3-03.trace.json`：gap=true，引用为空 | 通过 |
| 改写问题 | 仍检索到 D01 并回答 30 天 | `examples/traces/Q3-04.trace.json` + 测试 `test_swap_document_content_changes_answer`（替换原文为 45 天后答案随之改变） | 通过 |
| 上下文追问 | 第二轮引用 D03 回答“提前 3 个工作日” | `examples/traces/Q3-05.trace.json` | 通过 |
| 工具失败 | 检索失败重试一次，随后说明无法确认 | `examples/traces/Q3-06.trace.json`：两次检索均 error，回答如实 | 通过 |
| 执行上限 | 超限返回未完成状态 | `examples/traces/Q3-08.trace.json` | 通过 |
| 错误引用防御 | 伪造 quote 剔除；数字不支撑标出；无引用实质回答标出 | 测试 `test_fake_quote_removed_and_flagged`、`test_unsupported_number_flagged`、`test_answer_without_any_citation_flagged` | 通过 |
| 冲突不裁决防御 | 冲突时给单一结论且未确认 → 标出 | 测试 `test_conflict_requests_confirmation_not_one_answer` 与质量校验 | 通过 |

## 真实模型（DeepSeek）验证

`examples/real-model/index.md`：8 个场景全部符合预期。真实运行中发现并修复：
- **Q3-07 首轮漏判冲突**：真实模型只引用了 D12（7 天），未发现 D06 的 14 天 —— 根因是提示中只放了截断的观察摘要。
  修复：提示携带完整工具观察 + 系统提示强制“多文档必须 compare_docs” + 质量校验新增“读了多份却未比较”标记。重跑后正确列出 14 天与 7 天冲突。
- 重复引用：同一片段多次引用 → 引用去重（按 doc_id+quote）。

## 失败与限制

- 检索为轻量实现，极远同义改写可能漏检（会表现为资料缺口而非错误答案）；
- 冲突检测基于数字差异；纯语义冲突需依赖真实模型；
- 引用校验验证“逐字存在 + 数字支撑”，不验证逻辑蕴含；
- 故障注入为模拟，非真实服务故障。
