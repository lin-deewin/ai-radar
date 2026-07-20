"""配置加载。从 config/sources.yaml 读取并校验。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path

    @property
    def lookback_hours(self) -> int:
        return int(self.raw.get("schedule", {}).get("lookback_hours", 24))

    @property
    def ranking(self) -> dict[str, Any]:
        return self.raw.get("ranking", {})

    @property
    def arxiv(self) -> dict[str, Any]:
        return self.raw.get("arxiv", {})

    @property
    def hf_papers(self) -> dict[str, Any]:
        return self.raw.get("hf_papers", {})

    @property
    def rss_sources(self) -> list[dict[str, Any]]:
        return self.raw.get("rss_sources", [])


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, base_dir=path.parent.parent)
