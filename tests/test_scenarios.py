"""场景测试：六类验收 + 引用校验 + 上下文 + 防御。"""
import json
import unittest

from helpers import ScriptLLM, get_request, run_scenario
from agent import Agent, Limits
from gateway import MockLLM
from tools import load_context


class ClearAnswerTest(unittest.TestCase):
    def test_cite_d01_when_no_conflict(self):
        req, ctx, _, results = run_scenario("Q3-01")
        r = results[0]
        self.assertEqual(r["status"], "completed")
        self.assertFalse(r["gap"])
        self.assertEqual(r["citations"][0]["doc_id"], "D01")
        self.assertIn("30 天", r["answer"])
        self.assertIn("30 天内提交报销", r["citations"][0]["quote"])
        self.assertTrue(r["quality"]["passed"])


class ConflictTest(unittest.TestCase):
    def test_conflict_requests_confirmation_not_one_answer(self):
        req, ctx, _, results = run_scenario("Q3-02")
        r = results[0]
        self.assertTrue(r["pending_confirmation"])          # 请求确认
        self.assertIn("30", r["answer"]) and self.assertIn("15", r["answer"])
        self.assertEqual({c["doc_id"] for c in r["citations"]}, {"D01", "D02"})
        self.assertGreaterEqual(len(ctx["conflicts"]), 1)

    def test_version_conflict_trial_days(self):
        req, ctx, _, results = run_scenario("Q3-07")
        r = results[0]
        self.assertTrue(r["pending_confirmation"])
        text = r["answer"]
        self.assertIn("14 天", text)
        self.assertIn("7 天", text)

    def test_after_user_selects_department_answer_with_d02(self):
        """冲突后用户确认适用制度，第二轮按指定制度回答并引用。"""
        req, ctx, agent, results = run_scenario("Q3-02")
        self.assertTrue(results[0]["pending_confirmation"])
        r2 = agent.run_turn({"id": req["id"]},
                            {"message": "我是销售部的，按我们部门的来。报销期限多久？", "scope": {"docs": ["D01", "D02"]}})
        self.assertEqual([c["doc_id"] for c in r2["citations"]], ["D02"])
        self.assertIn("15 天", r2["answer"])
        self.assertTrue(r2["quality"]["passed"])


class OutOfScopeTest(unittest.TestCase):
    def test_no_material_gap(self):
        req, ctx, _, results = run_scenario("Q3-03")
        r = results[0]
        self.assertTrue(r["gap"])
        self.assertEqual(r["citations"], [])
        self.assertIn("没有找到", r["answer"])


class RewrittenQuestionTest(unittest.TestCase):
    def test_paraphrase_answered_from_d01(self):
        req, ctx, _, results = run_scenario("Q3-04")
        r = results[0]
        self.assertEqual(r["citations"][0]["doc_id"], "D01")
        self.assertIn("30 天", r["answer"])

    def test_swap_document_content_changes_answer(self):
        """审核要点：替换文档内容，答案与动作应随之改变。"""
        req = get_request("Q3-01")
        ctx = load_context()
        ctx["docs"]["D01"]["content"] = ctx["docs"]["D01"]["content"].replace("30 天", "45 天")
        agent = Agent(MockLLM(), ctx)
        r = agent.run_turn({"id": "swap"}, {"message": "报销期限多久？", "scope": {"docs": ["D01"]}})
        self.assertIn("45 天", r["answer"])
        self.assertNotIn("30 天", r["answer"])
        self.assertIn("45 天", r["citations"][0]["quote"])


class ContextFollowupTest(unittest.TestCase):
    def test_second_turn_uses_history(self):
        req, ctx, _, results = run_scenario("Q3-05")
        self.assertIn("5 天", results[0]["answer"])
        r2 = results[1]
        self.assertFalse(r2["gap"])
        self.assertIn("3 个工作日", r2["answer"])
        self.assertEqual(r2["citations"][0]["doc_id"], "D03")


class ToolFailureTest(unittest.TestCase):
    def test_retry_then_report_no_answer(self):
        req, ctx, _, results = run_scenario("Q3-06")
        r = results[0]
        self.assertTrue(r["gap"])
        self.assertEqual(r["citations"], [])
        searches = [t for t in r["trace"] if t["action"] == "search_docs"]
        self.assertEqual(len(searches), 2)                 # 一次重试
        self.assertTrue(all("error" in (t.get("observation") or {}) for t in searches))


class LimitTest(unittest.TestCase):
    def test_step_limit(self):
        req, ctx, _, results = run_scenario("Q3-08", limits=Limits(max_steps=2))
        self.assertEqual(results[0]["status"], "incomplete_limit_reached")
        self.assertEqual(results[0]["answer"], "")


class CitationGuardTest(unittest.TestCase):
    def test_fake_quote_removed_and_flagged(self):
        script = ScriptLLM([
            {"thought": "直接答", "action": "finish",
             "arguments": {"answer": "报销期限是 15 天。",
                           "citations": [{"doc_id": "D01", "quote": "报销期限为 15 天", "supports": "期限"}]}},
        ])
        req = get_request("Q3-01")
        ctx = load_context()
        agent = Agent(script, ctx)
        r = agent.run_turn({"id": "g1"}, {"message": "报销期限多久？", "scope": {"docs": ["D01"]}})
        self.assertEqual(r["citations"], [])               # 伪造引用被剔除
        self.assertEqual(len(r["invalid_citations"]), 1)
        self.assertFalse(r["quality"]["passed"])

    def test_unsupported_number_flagged(self):
        script = ScriptLLM([
            {"thought": "答", "action": "finish",
             "arguments": {"answer": "报销期限是 15 天。",
                           "citations": [{"doc_id": "D01", "quote": "第一条 员工出差产生的交通、住宿费用，应在出差结束后 30 天内提交报销申请，逾期不予受理。"}]}},
        ])
        req = get_request("Q3-01")
        agent = Agent(script, load_context())
        r = agent.run_turn({"id": "g2"}, {"message": "报销期限多久？", "scope": {"docs": ["D01"]}})
        self.assertTrue(r["citations"])                    # 引用真实
        self.assertFalse(r["quality"]["passed"])           # 但 15 天不在引文中 → 不支撑

    def test_duplicate_citations_deduped(self):
        quote = "第一条 员工出差产生的交通、住宿费用，应在出差结束后 30 天内提交报销申请，逾期不予受理。"
        script = ScriptLLM([
            {"thought": "答", "action": "finish", "arguments": {
                "answer": "报销期限是 30 天。",
                "citations": [{"doc_id": "D01", "quote": quote},
                              {"doc_id": "D01", "quote": quote},
                              {"doc_id": "D01", "quote": quote}]}},
        ])
        agent = Agent(script, load_context())
        r = agent.run_turn({"id": "g4"}, {"message": "报销期限多久？", "scope": {"docs": ["D01"]}})
        self.assertEqual(len(r["citations"]), 1)

    def test_answer_without_any_citation_flagged(self):
        script = ScriptLLM([
            {"thought": "答", "action": "finish", "arguments": {"answer": "报销期限是 30 天。", "citations": []}},
        ])
        agent = Agent(script, load_context())
        r = agent.run_turn({"id": "g3"}, {"message": "报销期限多久？", "scope": {"docs": ["D01"]}})
        self.assertFalse(r["quality"]["passed"])
        self.assertTrue(any("无依据" in i for i in r["quality"]["issues"]))


if __name__ == "__main__":
    unittest.main()
