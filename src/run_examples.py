"""批量运行示例，产出证据（traces / requests / index.md）。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from cli import load_requests, make_ctx, print_result  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["mock", "real"], default="mock")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()
    if args.out is None:
        args.out = os.path.join(ROOT, "examples" if args.model == "mock" else "examples/real-model")

    reqs = load_requests()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        reqs = [r for r in reqs if r["id"] in wanted]
    os.makedirs(os.path.join(args.out, "traces"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "requests"), exist_ok=True)

    rows = []
    for req in reqs:
        trace_path = os.path.join(args.out, "traces", f"{req['id']}.trace.json")
        if args.index_only:
            try:
                data = json.load(open(trace_path, encoding="utf-8"))
            except OSError:
                print(f"[{req['id']}] 缺少 trace，跳过")
                continue
        else:
            ctx = make_ctx(req)
            gateway = build_gateway(args.model)
            limits = Limits(max_steps=req.get("limit_steps", 16), max_tool_calls=req.get("limit_tools", 12))
            agent = Agent(gateway, ctx, limits=limits)
            request = {"id": req["id"], "scenario": req["scenario"], "request": req.get("scenario", "")}
            results = [agent.run_turn(request, turn) for turn in req["turns"]]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_result(req, results, args.model)
            with open(os.path.join(args.out, "traces", f"{req['id']}.console.txt"), "w", encoding="utf-8") as f:
                f.write(buf.getvalue())
            with open(os.path.join(args.out, "requests", f"{req['id']}.request.json"), "w", encoding="utf-8") as f:
                json.dump(req, f, ensure_ascii=False, indent=2)
            data = {"request": req, "mode": args.model, "turns": results}
            with open(trace_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            first = results[0]
            print(f"[{req['id']}] {req['scenario']} → {first['status']}｜"
                  f"引用 {[c['doc_id'] for c in first['citations']]}｜质量 {'通过' if all(r['quality']['passed'] for r in results) else '未通过'}")
        rows.append(data)

    lines = ["# 证据索引（由 src/run_examples.py 生成）", "",
             f"模型模式：`{args.model}`｜场景数：{len(rows)}", "",
             "| 场景 | 验收点 | 轮数 | 每轮状态 | 引用 | 质量校验 | 关键结论 |",
             "|---|---|---|---|---|---|---|"]
    for data in rows:
        req = data["request"]
        turns = data["turns"]
        statuses = " / ".join(t["status"] for t in turns)
        cites = "；".join(f"第{i}轮 {[c['doc_id'] for c in t['citations']]}" for i, t in enumerate(turns, 1))
        q = all(t["quality"]["passed"] for t in turns)
        concl = []
        for i, t in enumerate(turns, 1):
            if t["gap"]:
                concl.append(f"第{i}轮：资料缺口")
            elif t["pending_confirmation"]:
                concl.append(f"第{i}轮：冲突待确认")
            elif t["citations"]:
                concl.append(f"第{i}轮：引用 {','.join(c['doc_id'] for c in t['citations'])}")
            else:
                concl.append(f"第{i}轮：无引用")
        lines.append(f"| [{req['id']}](traces/{req['id']}.trace.json) | {'、'.join(req.get('acceptance', []))} | "
                     f"{len(turns)} | {statuses} | {cites or '—'} | {'通过' if q else '未通过'} | {'；'.join(concl)} |")
    lines += ["", "每份 trace 含每轮完整工具轨迹、回答、逐条引用（doc_id + 逐字原文）、无效引用、",
              "质量校验（引用支撑/无依据/冲突未确认）、资料缺口与待确认状态。"]
    with open(os.path.join(args.out, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"证据已写入 {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
