"""模型网关：真实 LLM 与离线 Mock 决策器（企业知识问答域）。

Mock 决策器按“工具结果”推进：检索 → 读原文 → 多文档时比较冲突 → 有冲突则请求确认 /
无冲突则引用原文作答；零命中给资料缺口；检索失败重试一次后停止。
边界：只验证流程、引用校验与冲突处理逻辑，不证明真实模型的语义能力。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


def load_dotenv(path: str = ".env") -> None:
    import os
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = val.strip().strip('"').strip("'")


@dataclass
class GatewayConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0
    timeout: float = 60.0

    @staticmethod
    def from_env() -> "GatewayConfig":
        import os
        return GatewayConfig(
            base_url=os.environ.get("MODEL_BASE_URL", "https://api.deepseek.com").strip(),
            model=os.environ.get("MODEL_NAME", "deepseek-chat").strip(),
            api_key_env=os.environ.get("MODEL_API_KEY_ENV", "DEEPSEEK_API_KEY").strip(),
            temperature=float(os.environ.get("MODEL_TEMPERATURE", "0") or 0))


SYSTEM_PROMPT = """你是企业的内部知识问答助手，只依据检索到的制度/FAQ 原文回答员工问题。

# 可用工具
1. search_docs{"query":"报销期限"}                    检索文档（返回文档 ID/标题/摘要/适用对象）
2. read_snippet{"doc_id":"D01","about":"报销期限"}    读取指定文档原文片段
3. compare_docs{"doc_ids":["D01","D02"],"about":"报销期限"}  对比多份文档，检测数字口径冲突
4. resolve_conflict{"selected_doc_id":"D02","reason":"用户指定"}  仅当用户在对话中明确指定适用制度时调用

# 硬性规则
- 回答必须引用文档原文：citations 中每条都要给 doc_id 与逐字 quote（必须是原文片段），
  回答中出现的数字必须在所引 quote 中出现；禁止编造或改写引用；同一片段只引一次。
- 若读取了两份及以上相关文档，必须调用 compare_docs 核对口径（不允许只挑一份作答）；
  多份文档给出不同规定且未说明适用范围/优先级时：必须列出冲突（各自引用原文），
  请求用户确认适用制度；不得擅自选择其中一个答案。
- 检索不到相关资料时：直接说明资料缺口（知识库没有相关内容），不得凭空作答。
- 工具报错时：可重试一次；仍失败则说明无法确认答案及原因，不猜测。
- 多轮对话：结合 prior_turns 理解省略与指代（如“那需要提前几天？”）。

# 输出格式：只输出一个 JSON 对象
{"thought":"判断","action":"search_docs|read_snippet|compare_docs|resolve_conflict|finish","arguments":{...}}
结束时：
{"thought":"...","action":"finish","arguments":{
  "answer":"对员工的回答；冲突时说明冲突并要求确认；无资料时说明缺口",
  "citations":[{"doc_id":"D01","quote":"逐字原文片段","supports":"该片段支持回答中的哪一点"}]}}
"""


def render_user_prompt(request: dict, turn: dict, trace: list[dict], history: list[dict]) -> str:
    lines = ["## 本轮问题", turn["message"], ""]
    scope = turn.get("scope") or {}
    if scope:
        docs = scope.get("docs") if isinstance(scope, dict) else scope
        lines.append(f"可访问文档范围：{', '.join(docs)}")
    lines += ["", "## 历史对话（prior_turns）"]
    if history:
        for h in history:
            cites = "、".join(c["doc_id"] for c in h.get("citations", [])) or "无"
            lines.append(f"- 问：{h['question']}")
            lines.append(f"  答：{h.get('answer', '')[:160]}")
            lines.append(f"  引用：{cites}")
    else:
        lines.append("（无）")
    lines += ["", "## 本轮工具轨迹（含完整观察，引用须逐字取自 read_snippet 返回的原文）"]
    for st in trace[-12:]:
        lines.append(f"[{st['step']}] {st['action']} 参数={json.dumps(st['arguments'], ensure_ascii=False)}")
        obs = st.get("observation")
        if obs is not None:
            body = json.dumps(obs, ensure_ascii=False)
            if len(body) > 1600:
                body = body[:1600] + "…（截断）"
            lines.append(f"  观察：{body}")
        else:
            lines.append(f"  观察：{st['observation_summary']}")
    if not trace:
        lines.append("（尚无动作）")
    lines += ["", "请输出下一个动作（JSON）。"]
    return "\n".join(lines)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg

    def _key(self) -> str:
        import os
        key = os.environ.get(self.cfg.api_key_env, "").strip()
        if not key:
            raise LLMError(f"缺少环境变量 {self.cfg.api_key_env}")
        return key

    def complete(self, system: str, user: str) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.cfg.model, "temperature": self.cfg.temperature,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._key()}"})
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                    return json.loads(r.read().decode())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last = LLMError(f"HTTP {e.code}")
                if e.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                last = LLMError(str(e))
            time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("模型调用失败")


def extract_json(text: str) -> dict:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中没有 JSON 对象")
    return json.loads(stripped[start:end + 1])


def build_gateway(mode: str):
    return MockLLM() if mode == "mock" else LLMClient(GatewayConfig.from_env())


# ---------------------------------------------------------------- 离线 Mock 决策器
class MockLLM:
    """确定性决策器：按工具返回自适应（含冲突与失败分支），供离线评审。"""

    TOOLS = ("search_docs", "read_snippet", "compare_docs", "resolve_conflict")

    def decide(self, request: dict, turn: dict, trace: list[dict], history: list[dict]) -> dict:
        q = turn["message"]
        tool_obs = {t["action"]: t.get("observation") or {} for t in trace if t["action"] in self.TOOLS}
        searches = [t for t in trace if t["action"] == "search_docs"]
        reads = {t["arguments"]["doc_id"]: (t.get("observation") or {})
                 for t in trace if t["action"] == "read_snippet"}
        compares = [t["observation"] for t in trace if t["action"] == "compare_docs"]
        resolutions = [t["observation"] for t in trace if t["action"] == "resolve_conflict"]

        # 1) 检索（失败可重试一次；零命中时结合历史主题再试一次）
        if not searches:
            return self._act("先检索知识库。", "search_docs", {"query": q})
        last_search = searches[-1].get("observation") or {}
        if last_search.get("error"):
            if len(searches) < 2:
                return self._act(f"检索失败（{last_search['error']}），重试一次。", "search_docs", {"query": q})
            return {"thought": "检索服务连续失败，停止并说明无法确认。", "action": "finish",
                    "arguments": {"answer": f"检索服务暂时不可用（{last_search['error']}），我无法确认答案；请稍后重试或直接提供相关制度文档。", "citations": []}}
        hits = last_search.get("hits", [])
        weak = hits and all(h["score"] < 0.3 for h in hits)
        if (not hits or weak) and history and len(searches) < 2:
            topic = history[-1]["question"]
            return self._act(f"追问句缺少主题词，结合上一轮主题「{topic[:18]}」再检索一次。",
                             "search_docs", {"query": f"{topic[:20]} {q}"})
        if not hits:
            return {"thought": "知识库无相关资料。", "action": "finish",
                    "arguments": {"answer": "知识库中没有找到与这个问题相关的资料，无法回答；建议咨询行政/HR 或提供对应文档。", "citations": []}}

        # 2) 读取最相关文档原文（最多 2 篇，分数 >= 0.3 的都读）
        candidates = [h["doc_id"] for h in hits if h["score"] >= 0.3][:2]
        for did in candidates:
            if did not in reads:
                return self._act(f"检索到 {did}，读取原文核对条款。", "read_snippet",
                                 {"doc_id": did, "about": q})
        if not reads:
            return {"thought": "检索结果相关度过低，无法支撑回答。", "action": "finish",
                    "arguments": {"answer": "知识库中没有足够相关的资料，无法给出可靠答案；请补充文档。", "citations": []}}

        read_docs = [d for d, obs in reads.items() if not obs.get("error")]
        # 3) 多篇文档 → 冲突比较
        if len(read_docs) >= 2 and not compares:
            return self._act("两篇文档都相关，比较是否存在口径冲突。", "compare_docs",
                             {"doc_ids": read_docs[:2], "about": q})
        conflict = bool(compares and compares[-1].get("conflict"))
        user_selected = resolutions[-1]["selected_doc_id"] if resolutions else None
        # 用户在本轮追问中指定适用范围的（如“我是销售部的”），按部门/适用对象匹配制度
        if not user_selected and history and history[-1].get("pending_confirmation"):
            for did in read_docs:
                obs = reads.get(did) or {}
                applies = obs.get("applies_to") or ""
                dept = obs.get("department") or ""
                for keyword in (applies, dept):
                    kw = keyword.replace("员工", "").replace("全体", "").strip()
                    if kw and kw in q:
                        user_selected = did
                        break
                if user_selected:
                    break

        # 4) 冲突且用户未指定 → 请求确认（不擅自裁决）
        if conflict and not user_selected:
            cites = []
            lines = []
            for did in read_docs[:2]:
                obs = reads[did]
                quote = self._best_quote(obs, q)
                if quote:
                    cites.append({"doc_id": did, "quote": quote,
                                  "supports": f"{obs['title']}（{obs['version_date']}，适用于{obs['applies_to']}）中的期限规定"})
                    lines.append(f"- {did}《{obs['title']}》（{obs['version_date']}，适用于{obs['applies_to']}）：{quote}")
            answer = ("查到两份制度对“" + q + "”规定不一致，暂不能给出单一答案：\n" + "\n".join(lines) +
                      "\n适用哪份取决于你的身份/部门与制度优先级，请确认应适用哪份制度后我再给出结论。")
            return {"thought": "文档间存在冲突且适用范围未说明，按规则请求用户确认。", "action": "finish",
                    "arguments": {"answer": answer, "citations": cites, "status": "pending_confirmation"}}

        # 5) 用户已指定 → 按指定文档回答
        target = user_selected or read_docs[0]
        obs = reads.get(target) or {}
        quote = self._best_quote(obs, q)
        if not quote:
            return {"thought": "原文中未找到直接支持的条款。", "action": "finish",
                    "arguments": {"answer": "找到了相关文档，但原文中没有能直接回答该问题的条款，建议咨询制度负责人。", "citations": []}}
        answer = f"依据 {target}《{obs.get('title')}》：{quote}"
        return {"thought": "原文条款直接支持回答。", "action": "finish",
                "arguments": {"answer": answer,
                              "citations": [{"doc_id": target, "quote": quote, "supports": "直接回答问题的条款"}]}}

    @staticmethod
    def _best_quote(obs: dict, q: str) -> str:
        content = obs.get("content", "")
        sents = [s for s in re.split(r"(?<=[。；])", content) if s.strip()]
        if not sents:
            return ""
        from tools import _bigrams, STOP_GRAMS
        q_grams = _bigrams(q) - STOP_GRAMS
        def score(s):
            overlap = len(q_grams & _bigrams(s))
            nums = len(re.findall(r"\d+", s))
            rule_bonus = 2 if any(w in s for w in ("期限", "试用期", "天内", "年假", "申请", "结转")) else 0
            return overlap * 3 + rule_bonus + nums
        best = max(sents, key=score)
        return best.strip()

    def _act(self, thought, action, args):
        return {"thought": thought, "action": action, "arguments": args}
