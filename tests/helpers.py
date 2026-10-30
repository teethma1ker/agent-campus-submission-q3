import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cli import load_requests, make_ctx  # noqa: E402


def get_request(rid: str) -> dict:
    for r in load_requests():
        if r["id"] == rid:
            return r
    raise KeyError(rid)


def run_scenario(rid: str, limits=None, gateway=None):
    from agent import Agent, Limits
    from gateway import MockLLM
    req = get_request(rid)
    ctx = make_ctx(req)
    agent = Agent(gateway or MockLLM(), ctx, limits=limits or Limits())
    results = [agent.run_turn({"id": req["id"]}, turn) for turn in req["turns"]]
    return req, ctx, agent, results


class ScriptLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete(self, system, user):
        if not self.decisions:
            return json.dumps({"thought": "end", "action": "finish", "arguments": {"answer": "", "citations": []}})
        d = self.decisions.pop(0)
        return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
