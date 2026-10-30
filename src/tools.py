"""知识库工具：search_docs / read_snippet / compare_docs / resolve_conflict。

检索：小型合成知识库，采用「字二元组 + 标签命中 + 标题命中」的轻量检索，
      对改写后的相似问题（同义词换序）具有鲁棒性；不做语义幻觉。
引用校验（agent 层）：引用的 quote 必须是原文逐字片段，回答中的数字必须出现在引用片段中。
"""
from __future__ import annotations

import json
import os
import re


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_context(data_dir: str = "data") -> dict:
    docs = load_json(os.path.join(data_dir, "docs.json"))
    return {
        "docs": {d["id"]: d for d in docs},
        "retrieval_log": [],      # {"query": ..., "hits": [...]}
        "reads": [],              # {"doc_id": ..., "at": ...}
        "conflicts": [],          # compare_docs 检测出的冲突
        "resolutions": [],        # 用户确认适用制度
        "faults": {},
        "seq": 0,
    }


# ---------------------------------------------------------------- 轻量检索
STOP_GRAMS = {"公司", "员工", "什么", "怎么", "多少", "请问", "一下", "多久", "哪些", "可以"}


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    grams = set()
    for tok in re.findall(r"[一-龥]+|[A-Za-z0-9]+", text):
        if re.match(r"[A-Za-z0-9]+", tok):
            grams.add(tok.lower())
        else:
            for i in range(len(tok) - 1):
                grams.add(tok[i:i + 2])
    return grams


def _score(query: str, doc: dict) -> float:
    q = _bigrams(query) - STOP_GRAMS
    if not q:
        return 0.0
    body = doc["title"] + doc["content"] + " ".join(doc["tags"]) + doc["department"]
    b = _bigrams(body)
    hit = len(q & b)
    score = hit / max(len(q), 1)
    for tag in doc["tags"]:
        if tag in query:
            score += 0.15
    for k in re.findall(r"[一-龥]{3,}", query):
        if k in doc["title"]:
            score += 0.1
            break
    return round(score, 4)


def search(ctx: dict, query: str, top_k: int = 4, scope: list[str] | None = None) -> list[dict]:
    if not query or not query.strip():
        raise ValueError("检索关键词为空")
    docs = ctx["docs"] if not scope else {k: v for k, v in ctx["docs"].items() if k in scope}
    scored = []
    for d in docs.values():
        s = _score(query, d)
        if s >= 0.12:
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], x[1]["version_date"]), reverse=False)
    hits = [{"doc_id": d["id"], "title": d["title"], "score": s,
             "version_date": d["version_date"], "type": d["type"],
             "department": d["department"], "applies_to": d["applies_to"],
             "tags": d["tags"], "excerpt": d["content"][:60] + "…"}
            for s, d in scored[:top_k]]
    ctx["retrieval_log"].append({"query": query, "hits": [h["doc_id"] for h in hits]})
    return hits


def relevant_sentences(content: str, query: str, max_n: int = 2) -> list[str]:
    q = _bigrams(query) - STOP_GRAMS
    sents = [s for s in re.split(r"(?<=[。；！？])", content) if s.strip()]
    scored = sorted(sents, key=lambda s: -len(q & _bigrams(s)))
    return [s.strip() for s in scored[:max_n] if q & _bigrams(s)][:max_n]


# ---------------------------------------------------------------- 工具 1：检索
def build_search_docs(ctx: dict, scope: list[str] | None):
    def search_docs(query: str, top_k: int = 4) -> str:
        fault = ctx["faults"].get("search_docs")
        if fault and fault.get("remaining", 0) > 0:
            fault["remaining"] -= 1
            return json.dumps({"error": fault.get("error", "检索服务暂不可用")}, ensure_ascii=False)
        try:
            hits = search(ctx, query, top_k=top_k, scope=scope)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        return json.dumps({"query": query, "count": len(hits), "hits": hits}, ensure_ascii=False)
    return search_docs


# ---------------------------------------------------------------- 工具 2：读取原文
def build_read_snippet(ctx: dict, scope: list[str] | None):
    def read_snippet(doc_id: str, about: str = "") -> str:
        doc = ctx["docs"].get(doc_id)
        if not doc:
            return json.dumps({"error": f"文档 {doc_id} 不存在"}, ensure_ascii=False)
        if scope and doc_id not in scope:
            return json.dumps({"error": f"文档 {doc_id} 不在本次可访问范围"}, ensure_ascii=False)
        ctx["reads"].append({"doc_id": doc_id})
        snippets = relevant_sentences(doc["content"], about) if about else [doc["content"]]
        return json.dumps({
            "doc_id": doc_id, "title": doc["title"], "version_date": doc["version_date"],
            "effective_date": doc["effective_date"], "department": doc["department"],
            "applies_to": doc["applies_to"], "content": doc["content"],
            "snippets": snippets,
        }, ensure_ascii=False)
    return read_snippet


# ---------------------------------------------------------------- 工具 3：冲突比较
def build_compare_docs(ctx: dict):
    def compare_docs(doc_ids: list, about: str = "") -> str:
        if not isinstance(doc_ids, list) or len(doc_ids) < 2:
            return json.dumps({"error": "compare_docs 需要至少两个文档 ID"}, ensure_ascii=False)
        missing = [d for d in doc_ids if d not in ctx["docs"]]
        if missing:
            return json.dumps({"error": f"文档不存在：{', '.join(missing)}"}, ensure_ascii=False)
        views = []
        for did in doc_ids:
            doc = ctx["docs"][did]
            views.append({
                "doc_id": did, "title": doc["title"], "version_date": doc["version_date"],
                "department": doc["department"], "applies_to": doc["applies_to"],
                "snippets": relevant_sentences(doc["content"], about) if about else [doc["content"]],
            })
        numbers = {}
        for v in views:
            text = " ".join(v["snippets"])
            nums = re.findall(r"\d+(?:\.\d+)?", text)
            key = tuple(sorted(set(nums)))
            numbers.setdefault(key, []).append(v["doc_id"])
        # 不同文档给出不同数字组 → 视为数字口径冲突（需人工确认，不擅自裁决）
        conflict = len(numbers) > 1
        result = {"doc_ids": doc_ids, "about": about, "views": views,
                  "numeric_variants": [{"numbers": list(k), "docs": v} for k, v in numbers.items()],
                  "conflict": conflict}
        if conflict:
            ctx["conflicts"].append(result)
        return json.dumps(result, ensure_ascii=False)
    return compare_docs


# ---------------------------------------------------------------- 工具 4：确认适用制度
def build_resolve_conflict(ctx: dict):
    def resolve_conflict(selected_doc_id: str, reason: str = "") -> str:
        doc = ctx["docs"].get(selected_doc_id)
        if not doc:
            return json.dumps({"error": f"文档 {selected_doc_id} 不存在"}, ensure_ascii=False)
        ctx["resolutions"].append({"doc_id": selected_doc_id, "reason": reason})
        return json.dumps({"ok": True, "selected_doc_id": selected_doc_id, "reason": reason,
                           "note": "已记录用户指定的适用制度（仅当用户在对话中明确指定后调用）"},
                          ensure_ascii=False)
    return resolve_conflict


SCHEMAS = {
    "search_docs": {"required": ["query"], "optional": ["top_k"]},
    "read_snippet": {"required": ["doc_id"], "optional": ["about"]},
    "compare_docs": {"required": ["doc_ids"], "optional": ["about"]},
    "resolve_conflict": {"required": ["selected_doc_id"], "optional": ["reason"]},
}


def schema_error(action: str, args: dict) -> str | None:
    schema = SCHEMAS.get(action)
    if schema is None:
        return f"未知动作 {action}"
    if not isinstance(args, dict):
        return f"{action} 的参数必须是 JSON 对象"
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        return f"{action} 缺少参数：{', '.join(missing)}"
    extra = [k for k in args if k not in schema["required"] + schema["optional"]]
    if extra:
        return f"{action} 不接受参数：{', '.join(extra)}"
    if action == "compare_docs" and (not isinstance(args.get("doc_ids"), list) or len(args["doc_ids"]) < 2):
        return "compare_docs 需要至少两个文档 ID"
    return None


def build_registry(ctx: dict, scope: list[str] | None) -> dict:
    return {
        "search_docs": build_search_docs(ctx, scope),
        "read_snippet": build_read_snippet(ctx, scope),
        "compare_docs": build_compare_docs(ctx),
        "resolve_conflict": build_resolve_conflict(ctx),
    }
