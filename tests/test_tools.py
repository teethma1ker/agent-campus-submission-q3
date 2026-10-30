"""检索与工具测试。"""
import json
import unittest

from helpers import ROOT
from tools import load_context, build_registry, search, relevant_sentences, schema_error


def fresh(scope=None):
    ctx = load_context()
    return ctx, build_registry(ctx, scope)


class SearchTest(unittest.TestCase):
    def test_direct_query_hits_d01(self):
        ctx, reg = fresh()
        obs = json.loads(reg["search_docs"]("报销期限多久"))
        self.assertEqual(obs["hits"][0]["doc_id"], "D01")
        self.assertEqual(obs["hits"][1]["doc_id"], "D02")

    def test_rewritten_query_still_hits(self):
        ctx, reg = fresh()
        obs = json.loads(reg["search_docs"]("出差完了多久之内要报销"))
        ids = [h["doc_id"] for h in obs["hits"]]
        self.assertIn("D01", ids)

    def test_out_of_scope(self):
        ctx, reg = fresh(["D01"])
        obs = json.loads(reg["search_docs"]("报销期限多久"))
        self.assertEqual([h["doc_id"] for h in obs["hits"]], ["D01"])

    def test_irrelevant_query_no_hits(self):
        ctx, reg = fresh()
        obs = json.loads(reg["search_docs"]("公司年会什么时候办"))
        self.assertEqual(obs["count"], 0)

    def test_search_fault_then_recover(self):
        ctx, reg = fresh()
        ctx["faults"]["search_docs"] = {"error": "timeout", "remaining": 1}
        first = json.loads(reg["search_docs"]("报销期限多久"))
        second = json.loads(reg["search_docs"]("报销期限多久"))
        self.assertIn("error", first)
        self.assertGreater(second["count"], 0)


class SnippetTest(unittest.TestCase):
    def test_read_returns_verbatim_snippets(self):
        ctx, reg = fresh()
        obs = json.loads(reg["read_snippet"]("D01", "报销期限"))
        self.assertTrue(obs["snippets"])
        for s in obs["snippets"]:
            self.assertIn(s, obs["content"])

    def test_read_out_of_scope(self):
        ctx, reg = fresh(["D01"])
        obs = json.loads(reg["read_snippet"]("D02"))
        self.assertIn("error", obs)


class CompareTest(unittest.TestCase):
    def test_conflict_detected(self):
        ctx, reg = fresh()
        obs = json.loads(reg["compare_docs"](["D01", "D02"], "报销期限"))
        self.assertTrue(obs["conflict"])
        variants = {tuple(v["numbers"]): v["docs"] for v in obs["numeric_variants"]}
        self.assertTrue(any("30" in k for k in variants))
        self.assertTrue(any("15" in k for k in variants))

    def test_no_conflict_same_numbers(self):
        ctx, reg = fresh()
        obs = json.loads(reg["compare_docs"](["D01", "D01"], "报销期限"))
        self.assertFalse(obs["conflict"])


class SchemaTest(unittest.TestCase):
    def test_rules(self):
        self.assertIsNotNone(schema_error("compare_docs", {"doc_ids": ["D01"]}))
        self.assertIsNotNone(schema_error("hack_tool", {}))
        self.assertIsNone(schema_error("read_snippet", {"doc_id": "D01"}))


if __name__ == "__main__":
    unittest.main()
