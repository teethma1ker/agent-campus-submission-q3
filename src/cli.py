"""CLI：企业知识问答 Agent。

  python -m src.cli                                  # 列出场景
  python -m src.cli --requests Q3-02                 # 跑指定场景（多轮按顺序执行）
  python -m src.cli --all                            # 全部场景
  python -m src.cli --requests Q3-02 --model real    # 真实模型
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402
from tools import load_context  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_requests() -> list[dict]:
    with open(os.path.join(ROOT, "data", "requests.json"), encoding="utf-8") as f:
        return json.load(f)


def make_ctx(req: dict) -> dict:
    ctx = load_context(os.path.join(ROOT, "data"))
    for tool, spec in (req.get("faults") or {}).items():
        ctx["faults"][tool] = dict(spec)
    return ctx


def print_result(req: dict, results: list[dict], mode: str) -> None:
    print(f"\n=== {req['id']}｜{req['scenario']}｜模型 {mode} ===")
    for i, r in enumerate(results, 1):
        print(f"--- 第 {i} 轮：{r['turn']}")
        print(f"状态：{r['status']}（{r['status_note']}）｜步数 {r['steps']}｜工具调用 {r['tool_calls']}")
        print(f"回答：{r['answer'][:300]}")
        for c in r["citations"]:
            print(f"  引用：{c['doc_id']}《{c['title']}》「{c['quote'][:60]}」")
        if r["invalid_citations"]:
            print(f"  无效引用（已剔除）：{[(c['doc_id'], c['reason']) for c in r['invalid_citations']]}")
        q = r["quality"]
        print(f"质量校验：{'通过' if q['passed'] else '未通过：' + '；'.join(q['issues'])}"
              f"｜缺口 {r['gap']}｜待确认 {r['pending_confirmation']}")


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(description="企业知识问答 Agent")
    ap.add_argument("--requests")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", choices=["mock", "real", "auto"], default=None)
    ap.add_argument("--limit-steps", type=int, default=16)
    ap.add_argument("--limit-tools", type=int, default=12)
    ap.add_argument("--trace-dir")
    args = ap.parse_args()

    reqs = load_requests()
    by_id = {r["id"]: r for r in reqs}
    mode = args.model or os.environ.get("MODEL_MODE", "mock")

    if not args.requests and not args.all:
        print("场景列表：\n")
        for r in reqs:
            print(f"{r['id']:<7}{r['scenario']:<34}轮数={len(r['turns'])}")
        print("\n运行示例：python -m src.cli --requests Q3-02（全部：--all）")
        return 0

    chosen = reqs if args.all else [by_id[i.strip()] for i in args.requests.split(",") if i.strip() in by_id]
    if not chosen:
        print("没有可运行的请求")
        return 2

    failures = 0
    for req in chosen:
        ctx = make_ctx(req)
        gateway = build_gateway("mock" if mode == "mock" else "real")
        limits = Limits(max_steps=req.get("limit_steps", args.limit_steps),
                        max_tool_calls=req.get("limit_tools", args.limit_tools))
        agent = Agent(gateway, ctx, limits=limits)
        request = {"id": req["id"], "scenario": req["scenario"]}
        results = [agent.run_turn(request, turn) for turn in req["turns"]]
        print_result(req, results, mode)
        if args.trace_dir:
            os.makedirs(args.trace_dir, exist_ok=True)
            with open(os.path.join(args.trace_dir, f"{req['id']}.json"), "w", encoding="utf-8") as f:
                json.dump({"request": req, "mode": mode, "turns": results}, f, ensure_ascii=False, indent=2)
        if any(r["status"] in ("model_error", "model_protocol_error") for r in results):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
