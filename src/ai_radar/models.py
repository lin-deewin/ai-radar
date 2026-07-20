"""统一文章数据模型。所有采集器输出 Article，进入下游去重/评分/发布。"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def normalize_url(url: str) -> str:
    """URL 归一化：去 fragment、去追踪参数、小写 host、去尾部斜杠。"""
    if not url:
        return ""
    p = urlparse(url.strip())
    scheme = (p.scheme or "http").lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    tracking = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                "utm_content", "ref", "source", "from", "share"}
    qs = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in tracking]
    return urlunparse((scheme, host, path, "", urlencode(qs), ""))


def simhash(text: str, bits: int = 64) -> int:
    """Charikar SimHash。用于标题/正文近重复检测。
    参考: M. Charikar, 2002, "Similarity Estimation Techniques from Rounding Algorithms".
    """
    if not text:
        return 0
    tokens = [text[i:i + 3] for i in range(0, max(1, len(text) - 2), 1)]
    v = [0] * bits
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int, bits: int = 64) -> int:
    return bin((a ^ b) & ((1 << bits) - 1)).count("1")


@dataclass
class Article:
    source: str                 # 源名称，如 "arXiv" / "OpenAI Blog"
    source_type: str            # arxiv / hf / rss
    title: str
    url: str
    summary: str = ""           # 原始摘要/描述
    published: Optional[datetime] = None
    tags: list[str] = field(default_factory=list)      # ["ai","hardware"]
    lang: str = "en"
    authority: float = 0.5
    engagement: int = 0         # HN points / reddit upvotes / 知乎赞同
    authors: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)          # 论文 id / pdf / 评论数等

    # 计算字段
    url_norm: str = field(default="", init=False)
    title_hash: int = field(default=0, init=False)
    score: float = 0.0
    collected_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.url_norm = normalize_url(self.url)
        self.title_hash = simhash(self.title)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.published:
            d["published"] = self.published.isoformat()
        d["collected_at"] = datetime.fromtimestamp(
            self.collected_at, tz=timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        if isinstance(d.get("published"), str) and d["published"]:
            d["published"] = datetime.fromisoformat(d["published"])
        if "collected_at" in d and isinstance(d["collected_at"], str):
            d["collected_at"] = datetime.fromisoformat(
                d["collected_at"]).timestamp()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
