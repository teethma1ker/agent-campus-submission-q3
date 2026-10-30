# 企业知识问答 Agent

帮员工从制度/FAQ 中找到**有依据的答案**：检索文档、读取原文、冲突检测；每个回答引用文档 ID 与逐字原文；
资料不足时明确说明缺口；多轮追问结合上下文；全程只依据知识库作答。

## 1. 环境与安装

- Python ≥ 3.10，仅用标准库，无第三方依赖。
- 默认 `mock` 模式离线可跑；真实模型模式需 OpenAI 兼容接口（已在 DeepSeek 上验证）。

```bash
git clone <repo-url> && cd agent-campus-submission-q3
python3 -m unittest discover -s tests -v    # 24 个测试，全部应通过
```

## 2. 运行（CLI 方案）

```bash
# ① 查看场景
python3 -m src.cli

# ② 参考实例：D01（30 天）vs D02（15 天）冲突 → 列出冲突并请求确认
python3 -m src.cli --requests Q3-02

# ③ 参考实例：只检索到 D01 且无冲突 → 依据 D01 回答 30 天
python3 -m src.cli --requests Q3-01

# ④ 多轮追问（第二轮“那需要提前几天申请？”）
python3 -m src.cli --requests Q3-05

# ⑤ 全部场景与证据重建
python3 -m src.cli --all
python3 -m src.run_examples
```

真实模型：

```bash
cp .env.example .env    # 填 MODEL_MODE=real 与 DEEPSEEK_API_KEY
python3 -m src.cli --requests Q3-02 --model real
python3 -m src.run_examples --model real   # 写入 examples/real-model/
```

Mock 边界：确定性决策器，只验证流程、引用校验与冲突处理；不证明真实模型语义能力。两种模式共用同一循环与工具。

## 3. 评审入口

- 证据索引（Mock）：`examples/index.md`；真实模型：`examples/real-model/index.md`
- 参考实例冲突：`examples/traces/Q3-02.trace.json`、`examples/real-model/traces/Q3-02.trace.json`
- 验收点与文件对应：`docs/03-evaluation.md`
- 测试输出：`examples/test-results.txt`

## 4. 目录

```
src/    gateway.py（真实 LLM/离线决策器） tools.py（检索/读原文/比较冲突/确认） agent.py（循环+引用校验） cli.py run_examples.py
tests/  test_tools.py test_scenarios.py
examples/ traces/ requests/ index.md test-results.txt / real-model/
data/   docs.json requests.json
docs/   01-requirements ~ 05-retrospective
```

## 5. 已知限制

- 知识库为 12 份合成文档；检索为字二元组+标签的轻量实现，不覆盖同义改写极远的问法；
- 冲突判定基于数字口径差异，语义冲突（非数字）需真实模型判断；
- 引用校验只验证“逐字存在”与“数字支撑”，不判断逻辑蕴含；
- 真实模型回答质量依赖模型；系统只保证不越权引用、不擅自裁决冲突。
