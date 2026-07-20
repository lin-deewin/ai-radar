"""LLM 摘要器。多后端可降级:
  1) Ollama 本地（默认，http://localhost:11434）
  2) OpenAI 兼容接口（GLM、DeepSeek、Qwen 等，用 env: AI_RADAR_LLM_BASE / AI_RADAR_LLM_KEY / AI_RADAR_LLM_MODEL）
  3) 抽取式兜底（首句 + 关键词）

输出结构化: 中文摘要 / 关键贡献 / 类别标签。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .models import Article

log = logging.getLogger("ai_radar")


@dataclass
class SummaryResult:
    summary_zh: str = ""
    key_points: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    backend: str = ""
    model: str = ""
    ok: bool = False


# ---------- Prompt ----------

_PROMPT_SYS = ("你是一名 AI 与硬件领域的资深研究员。把英文论文/博客摘要翻译并精炼为中文，"
               "输出 JSON，字段: summary_zh(<=120字中文摘要), "
               "key_points(数组,2-4条关键贡献/亮点), "
               "topics(数组,2-5个主题标签如 'RL'/'MoE'/'推理加速'/'芯片微架构'). "
               "只输出 JSON，不要多余文字。")

_PROMPT_USER_TMPL = """标题: {title}
原文摘要:
{abstract}

请输出 JSON。"""


def _build_user(a: Article) -> str:
    ab = (a.summary or "").strip()
    if len(ab) > 3000:
        ab = ab[:3000] + "…"
    return _PROMPT_USER_TMPL.format(title=a.title, abstract=ab)


# ---------- 后端 ----------

def _try_ollama(model: str, host: str, user: str, sys: str,
                timeout: float = 60.0) -> str | None:
    url = host.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 600},
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
    }
    # 本地 Ollama 绕过 HTTP 代理
    headers = {"Content-Type": "application/json"}
    # 取 host:port 用于 no_proxy
    try:
        from urllib.parse import urlparse
        hp = urlparse(host).netloc or "127.0.0.1:11434"
    except Exception:
        hp = "127.0.0.1:11434"
    try:
        with httpx.Client(timeout=timeout, headers=headers,
                          trust_env=False) as c:
            r = c.post(url, json=payload)
            if r.status_code != 200:
                log.debug("ollama %s status %s body=%s",
                          url, r.status_code, r.text[:200])
                return None
            data = r.json()
            return (data.get("message") or {}).get("content") or ""
    except Exception as e:
        log.debug("ollama error: %s", e)
        return None


def _try_openai_compat(base: str, key: str, model: str,
                       user: str, sys: str, timeout: float = 60.0) -> str | None:
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
    }
    try:
        with httpx.Client(timeout=timeout, headers=headers) as c:
            r = c.post(url, json=payload)
            if r.status_code != 200:
                log.debug("openai-compat %s status %s body=%s",
                          url, r.status_code, r.text[:200])
                return None
            data = r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.debug("openai-compat error: %s", e)
        return None


# ---------- JSON 解析 ----------

_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_json(content: str) -> dict:
    if not content:
        return {}
    # 去掉 ```json ``` 包裹
    c = content.strip()
    if c.startswith("```"):
        c = re.sub(r"^```[a-zA-Z]*\n?", "", c)
        c = re.sub(r"\n?```$", "", c).strip()
    m = _JSON_RE.search(c)
    if m:
        c = m.group(0)
    try:
        return json.loads(c)
    except json.JSONDecodeError:
        # 尝试修复尾逗号
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", c))
        except json.JSONDecodeError:
            return {}


# ---------- 抽取式兜底 ----------

_STOP_EN = set("""a an the of to in on for and or but with as is are was were be been
this that these those it its their his her our your by from at into over under
which who whom whose what when where why how not no can could should would may
might must shall will do does did done have has had having more most much many
some any all both each few other such only own same so than too very just""".split())


def _extract_keywords(text: str, top_k: int = 6) -> list[str]:
    if not text:
        return []
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if len(w) < 4 or w in _STOP_EN or w.isdigit():
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_k]]


def _first_sentences(text: str, n: int = 2) -> str:
    if not text:
        return ""
    sents = re.split(r"(?<=[\.!?。！？])\s+", text.strip())
    return " ".join(sents[:n])


def _fallback_summary(a: Article) -> SummaryResult:
    ab = (a.summary or "").strip()
    sents = _first_sentences(ab, 2)
    if a.lang == "zh":
        summary_zh = sents[:120]
    else:
        # 英文：取首句作为"伪摘要"
        summary_zh = sents[:160] if sents else a.title
    kws = _extract_keywords(ab + " " + a.title, 5)
    topics = [k for k in kws if len(k) >= 4]
    return SummaryResult(
        summary_zh=summary_zh or a.title,
        key_points=kws[:3],
        topics=topics,
        backend="extractive",
        model="rules",
        ok=True,
    )


# ---------- 对外入口 ----------

class Summarizer:
    """根据可用性自动选择后端。失败降级为抽取式。"""

    def __init__(self,
                 ollama_host: str | None = None,
                 ollama_model: str | None = None,
                 openai_base: str | None = None,
                 openai_key: str | None = None,
                 openai_model: str | None = None,
                 prefer: str = "auto"):
        self.ollama_host = (ollama_host
                            or os.getenv("OLLAMA_HOST")
                            or "http://localhost:11434")
        self.ollama_model = ollama_model or os.getenv("AI_RADAR_OLLAMA_MODEL") \
            or os.getenv("OLLAMA_MODEL") or "qwen2.5:7b"
        self.openai_base = openai_base or os.getenv("AI_RADAR_LLM_BASE") or ""
        self.openai_key = openai_key or os.getenv("AI_RADAR_LLM_KEY") or ""
        self.openai_model = (openai_model
                             or os.getenv("AI_RADAR_LLM_MODEL")
                             or "glm-4.6")
        self.prefer = prefer
        self._backend: str | None = None
        # 探测一次可用后端
        self._probe()

    def _probe(self):
        if self.prefer in ("auto", "ollama"):
            if _try_ollama(self.ollama_model, self.ollama_host,
                           "ping", "you are a helpful assistant."):
                self._backend = "ollama"
                log.info("summarizer: backend=ollama(%s) @ %s",
                         self.ollama_model, self.ollama_host)
                return
        if self.prefer in ("auto", "openai") and self.openai_base and self.openai_key:
            if _try_openai_compat(self.openai_base, self.openai_key,
                                  self.openai_model, "ping", "assistant"):
                self._backend = "openai"
                log.info("summarizer: backend=openai-compat(%s) @ %s",
                         self.openai_model, self.openai_base)
                return
        self._backend = "extractive"
        log.warning("summarizer: LLM 不可用，降级为抽取式摘要。"
                    " (ollama=%s, openai_base=%s)",
                    self.ollama_host, self.openai_base or "<未配置>")

    @property
    def backend(self) -> str:
        return self._backend or "extractive"

    def summarize(self, a: Article) -> SummaryResult:
        user = _build_user(a)
        content: str | None = None
        backend = self._backend
        model = ""
        if backend == "ollama":
            content = _try_ollama(self.ollama_model, self.ollama_host,
                                  user, _PROMPT_SYS)
            model = self.ollama_model
        elif backend == "openai":
            content = _try_openai_compat(self.openai_base, self.openai_key,
                                         self.openai_model, user, _PROMPT_SYS)
            model = self.openai_model

        if not content:
            return _fallback_summary(a)

        data = _parse_json(content)
        if not data:
            # LLM 返回非 JSON，退化为取原文
            log.debug("summarizer: non-JSON from %s, fallback", backend)
            r = _fallback_summary(a)
            r.backend = backend + "(raw)"
            r.model = model
            return r

        return SummaryResult(
            summary_zh=str(data.get("summary_zh") or "").strip() or a.title,
            key_points=[str(x) for x in (data.get("key_points") or [])][:5],
            topics=[str(x) for x in (data.get("topics") or [])][:6],
            backend=backend,
            model=model,
            ok=True,
        )


def attach_summary(a: Article, r: SummaryResult) -> None:
    a.extra["summary_zh"] = r.summary_zh
    a.extra["key_points"] = r.key_points
    a.extra["topics"] = r.topics
    a.extra["llm_backend"] = r.backend
    a.extra["llm_model"] = r.model
