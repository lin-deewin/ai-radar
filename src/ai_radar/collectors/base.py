"""采集器基类与公共工具。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from ..models import Article

log = logging.getLogger("ai_radar")


@dataclass
class FetchResult:
    ok: bool
    text: str = ""
    status: int = 0
    error: str = ""


def fetch(url: str, *,
          headers: dict | None = None,
          timeout: float = 30.0,
          retries: int = 3,
          backoff: float = 1.5) -> FetchResult:
    """带重试的同步 HTTP GET。遵守 arXiv 建议：3 秒间隔、失败退避。"""
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36 AI-Radar/0.1"
    }
    if headers:
        base_headers.update(headers)
    last_err = ""
    for attempt in range(retries):
        try:
            with httpx.Client(headers=base_headers, timeout=timeout,
                              follow_redirects=True) as c:
                r = c.get(url)
                if r.status_code == 200:
                    return FetchResult(ok=True, text=r.text, status=200)
                if r.status_code in (429, 503):
                    last_err = f"HTTP {r.status_code}"
                else:
                    return FetchResult(ok=False, status=r.status_code,
                                       error=f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            last_err = repr(e)
        time.sleep(backoff ** attempt)
    return FetchResult(ok=False, error=last_err)


class BaseCollector:
    """采集器接口：collect() -> list[Article]。"""
    name: str = "base"

    def __init__(self, cfg: dict, on_item: Callable[[Article], None] | None = None):
        self.cfg = cfg
        self.on_item = on_item

    def collect(self) -> list[Article]:
        raise NotImplementedError

    def _emit(self, a: Article) -> Article:
        if self.on_item:
            self.on_item(a)
        return a
