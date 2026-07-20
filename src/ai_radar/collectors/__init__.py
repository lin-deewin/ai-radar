"""采集器包。"""
from .arxiv import ArxivCollector
from .hf_papers import HfPapersCollector
from .rss import RssCollector

__all__ = ["ArxivCollector", "HfPapersCollector", "RssCollector"]
