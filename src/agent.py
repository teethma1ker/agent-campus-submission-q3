"""问答 Agent：模型选动作 → 工具 → 观察 → 调整或结束（支持多轮会话）。

质量防线（与验收对应）：
- 引用校验：citations 的 quote 必须是文档原文的逐字子串，否则记为“无效引用”并剔除；
- 数字支撑：回答中的数字必须出现在引用片段中，否则“引用不支撑回答”；
- 无引用作答：若检索/读取发生过而答案是实质回答（非冲突说明/资料缺口），标注“无依据回答风险”；
- 冲突：compare_docs 检出 conflicts 但模型未请求确认、又给出单一数值结论 → 标注；
- 上限：步数/工具调用上限，超限返回明确未完成状态。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from gateway import SYSTEM_PROMPT, MockLLM, extract_json, render_user_prompt
from tools import build_registry, schema_error

TOOL_ACTIONS = ("search_docs", "read_snippet", "compare_docs", "resolve_conflict")
GAP_MARKERS = ("没有找到", "未找到", "没有相关", "无法回答", "无法确认", "暂无相关", "知识库中没")
CONFIRM_MARKERS = ("确认", "请指定", "适用哪", "哪一份", "哪一种", "哪份")


@dataclass
class Limits:
    max_steps: int = 16
    max_tool_calls: int = 12
    max_protocol_errors: int = 5


def summarize(action: str, obs: dict) -> str:
    if obs.get("error"):
        return f"error: {obs['error']}"
    if action == "search_docs":
        return "命中 " + ", ".join(f"{h['doc_id']}({h['score']})" for h in obs.get("hits", [])) or "无命中"
    if action == "read_snippet":
        return f"{obs.get('doc_id')}《{obs.get('title')}》片段：{(obs.get('snippets') or [''])[0][:60]}"
    if action == "compare_docs":
        return f"conflict={obs.get('conflict')}｜" + "；".join(
            f"{d['doc_id']}:{[v['numbers'] for v in obs['numeric_variants'] if d['doc_id'] in v['docs']][0]}"
            for d in obs.get("views", []))
    if action == "resolve_conflict":
        return f"已记录适用制度 {obs.get('selected_doc_id')}"
    return str(obs)[:120]


class Agent:
    def __init__(self, gateway, ctx: dict, limits: Limits | None = None):
        self.gateway = gateway
        self.ctx = ctx
        self.limits = limits or Limits()
        self.history: list[dict] = []   # 已完成轮次（问题/回答/引用）

    @staticmethod
    def _scope_docs(turn: dict) -> list[str] | None:
        scope = turn.get("scope") or {}
        return scope.get("docs") if isinstance(scope, dict) else scope

    def run_turn(self, request: dict, turn: dict) -> dict:
        scope = self._scope_docs(turn)
        request["task"] = request.get("task", {})
        registry = build_registry(self.ctx, scope)
        trace: list[dict] = []
        tool_calls = 0
        protocol_errors = 0
        status = "incomplete_limit_reached"
        finish_payload = None
        is_mock = isinstance(self.gateway, MockLLM)

        while True:
            if len(trace) >= self.limits.max_steps:
                status = "incomplete_limit_reached"
                break
            if is_mock:
                decision = self.gateway.decide(request, turn, trace, self.history)
            else:
                try:
                    raw = self.gateway.complete(SYSTEM_PROMPT, render_user_prompt(request, turn, trace, self.history))
                except Exception as e:  # noqa: BLE001
                    trace.append({"step": len(trace) + 1, "thought": "", "action": "model_error",
                                  "arguments": {}, "observation_summary": f"模型调用失败：{e}",
                                  "decision_basis": "模型不可用，终止本轮"})
                    status = "model_error"
                    break
                try:
                    decision = extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    protocol_errors += 1
                    trace.append({"step": len(trace) + 1, "thought": "模型输出不可解析",
                                  "action": "protocol_error", "arguments": {},
                                  "observation_summary": f"输出被拒绝：{e}",
                                  "decision_basis": "解析失败，未执行"})
                    if protocol_errors >= self.limits.max_protocol_errors:
                        status = "model_protocol_error"
                        break
                    continue

            action = decision.get("action")
            args = decision.get("arguments") or {}
            thought = str(decision.get("thought", ""))[:300]

            if action == "finish":
                finish_payload = args
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "finish",
                              "arguments": args, "observation_summary": "给出最终回答",
                              "decision_basis": thought})
                status = "completed"
                break

            err = schema_error(action, args)
            if err:
                protocol_errors += 1
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "invalid_action",
                              "arguments": args if isinstance(args, dict) else {},
                              "observation_summary": f"动作被拒绝：{err}",
                              "decision_basis": "动作/参数不符合工具协议，未执行"})
                if protocol_errors >= self.limits.max_protocol_errors:
                    status = "model_protocol_error"
                    break
                continue

            if tool_calls >= self.limits.max_tool_calls:
                status = "incomplete_limit_reached"
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "limit_reached",
                              "arguments": {}, "observation_summary": "工具调用次数已达上限，停止",
                              "decision_basis": "达到工具调用上限"})
                break

            try:
                obs = json.loads(registry[action](**args))
            except Exception as e:  # noqa: BLE001
                obs = {"error": f"工具执行异常：{e}"}
            tool_calls += 1
            trace.append({"step": len(trace) + 1, "thought": thought, "action": action,
                          "arguments": args, "observation_summary": summarize(action, obs),
                          "observation": obs, "decision_basis": thought})

        result = self._build_result(request, turn, trace, status, finish_payload, tool_calls)
        self.history.append({"question": turn["message"], "answer": result["answer"],
                             "citations": result["citations"],
                             "pending_confirmation": result["pending_confirmation"]})
        return result

    # -------------------------------------------------------------- 质量校验
    def _turn_selects_scope(self, turn: dict, trace: list[dict]) -> bool:
        """本轮追问中用户明确指定了适用部门/制度（如“我是销售部的”）。"""
        msg = turn.get("message", "")
        for t in trace:
            if t["action"] == "read_snippet":
                obs = t.get("observation") or {}
                for kw in (obs.get("applies_to", ""), obs.get("department", "")):
                    key = (kw or "").replace("员工", "").replace("全体", "").strip()
                    if key and key in msg:
                        return True
        return False

    def _build_result(self, request, turn, trace, status, finish_payload, tool_calls) -> dict:
        answer = str((finish_payload or {}).get("answer", "")) if finish_payload else ""
        raw_cites = (finish_payload or {}).get("citations") or []
        citations, invalid = [], []
        read_obs = {}
        for t in trace:
            if t["action"] == "read_snippet" and "observation" in t:
                read_obs[t["arguments"]["doc_id"]] = t["observation"]
        for c in raw_cites:
            doc_id = (c or {}).get("doc_id", "")
            quote = ((c or {}).get("quote") or "").strip()
            doc = self.ctx["docs"].get(doc_id)
            if not doc:
                invalid.append({"doc_id": doc_id, "quote": quote, "reason": "文档不存在"})
                continue
            if not quote or quote not in doc["content"]:
                invalid.append({"doc_id": doc_id, "quote": quote, "reason": "引用未在原文中逐字出现"})
                continue
            # 去重：同一文档同一片段只保留一条
            if any(x["doc_id"] == doc_id and x["quote"] == quote for x in citations):
                continue
            if len(citations) >= 4:
                continue
            citations.append({"doc_id": doc_id, "title": doc["title"],
                              "version_date": doc["version_date"], "quote": quote,
                              "supports": (c or {}).get("supports", "")})

        answer_numbers = set(re.findall(r"\d+(?:\.\d+)?", answer))
        cite_text = " ".join(c["quote"] for c in citations)
        cite_numbers = set(re.findall(r"\d+(?:\.\d+)?", cite_text))
        unsupported_numbers = sorted(n for n in answer_numbers - cite_numbers
                                     if n in " ".join(d["content"] for d in self.ctx["docs"].values()))

        is_gap = any(m in answer for m in GAP_MARKERS)
        is_confirm = any(m in answer for m in CONFIRM_MARKERS) and "不一致" in answer or "冲突" in answer
        substantive = (status == "completed" and bool(answer) and not is_gap and not is_confirm
                       and not any(w in answer for w in ("无法", "不能确认")))
        issues = []
        if invalid:
            issues.append(f"{len(invalid)} 条引用无效（未逐字命中原文或文档不存在），已从引用列表剔除")
        if substantive and not citations:
            issues.append("实质回答但没有任何有效引用（无依据回答风险）")
        if substantive and unsupported_numbers:
            issues.append(f"回答中的数字未出现在引用原文中：{', '.join(unsupported_numbers)}（引用不支撑回答）")
        read_ids = [t["arguments"]["doc_id"] for t in trace if t["action"] == "read_snippet"]
        compared = any(t["action"] == "compare_docs" for t in trace)
        if (substantive and len(set(read_ids)) >= 2 and not compared
                and self._turn_selects_scope(turn, trace) is False):
            issues.append("读取了多份相关文档但未调用 compare_docs 核对冲突，直接给出单一结论")
        conflicts = self.ctx["conflicts"]
        resolved = bool(self.ctx["resolutions"]) or self._turn_selects_scope(turn, trace)
        if conflicts and substantive and not resolved:
            issues.append("知识库中存在文档冲突，但回答给出了单一结论且未请求用户确认")

        evidence = {
            "retrievals": self.ctx["retrieval_log"],
            "reads": self.ctx["reads"],
            "conflicts": [{"doc_ids": c["doc_ids"], "about": c["about"]} for c in conflicts],
            "resolutions": self.ctx["resolutions"],
        }
        return {
            "turn": turn["message"],
            "status": status,
            "status_note": {
                "completed": "本轮已给出结果",
                "incomplete_limit_reached": "达到执行步数/工具调用上限，本轮未完成",
                "model_protocol_error": "模型连续输出非法协议内容，本轮中止",
                "model_error": "模型调用失败，本轮中止",
            }.get(status, status),
            "answer": answer,
            "citations": citations,
            "invalid_citations": invalid,
            "gap": is_gap or (not citations and not is_confirm and substantive is False),
            "pending_confirmation": is_confirm,
            "quality": {"passed": not issues, "issues": issues},
            "evidence": evidence,
            "trace": trace,
            "tool_calls": tool_calls,
            "steps": len(trace),
        }
