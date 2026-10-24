"""本地脱敏工具包。

对外主要入口见 ``masking.cli``；编排逻辑见 ``masking.engine``。
"""

from __future__ import annotations

__version__ = "0.3.0"

from .config import Lexicon, Rules, Settings
from .entity import Edit, Entity, EntityType, Finding, TextBlock

__all__ = [
    "__version__",
    "Edit",
    "Entity",
    "EntityType",
    "Finding",
    "Lexicon",
    "Rules",
    "Settings",
    "TextBlock",
]
