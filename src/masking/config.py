"""配置与词库加载。"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from .entity import DEFAULT_POLICY, EntityType


def project_home() -> pathlib.Path:
    """项目根目录。可用 MASK_LOCAL_HOME 覆盖，便于打包后迁移。"""
    env = os.environ.get("MASK_LOCAL_HOME")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    return pathlib.Path(__file__).resolve().parents[2]


def config_dir() -> pathlib.Path:
    return project_home() / "config"


@dataclass
class Lexicon:
    """代号词库。

    词池按首次出现顺序取用，池子用尽时抛异常而不是循环复用——
    复用会让两个不同主体拿到同一代号，这在法律分析里是致命错误。
    """

    open: str = "【"
    close: str = "】"
    pools: dict[str, list[str]] = field(default_factory=dict)
    org_suffix: dict[str, str] = field(default_factory=dict)
    place_suffix: list[str] = field(default_factory=list)
    mask_token: str = "XXX"
    mask_enumerate: bool = True

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "Lexicon":
        p = path or (config_dir() / "lexicon.yaml")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        frame = data.get("frame") or {}
        mask = data.get("mask") or {}
        lex = cls(
            open=frame.get("open", "【"),
            close=frame.get("close", "】"),
            pools={k: list(v) for k, v in (data.get("pools") or {}).items()},
            org_suffix=dict(data.get("org_suffix") or {}),
            place_suffix=list(data.get("place_suffix") or []),
            mask_token=mask.get("token", "XXX"),
            mask_enumerate=bool(mask.get("enumerate", True)),
        )
        lex._validate()
        return lex

    def _validate(self) -> None:
        if not self.open or not self.close:
            raise ValueError("lexicon.frame 不能为空，代号必须带框")
        for name in ("person", "place", "org"):
            pool = self.pools.get(name) or []
            if len(pool) < 10:
                raise ValueError(f"词池 {name} 少于 10 个，不足以支撑一个案件")
            if len(set(pool)) != len(pool):
                dup = [w for w in set(pool) if pool.count(w) > 1]
                raise ValueError(f"词池 {name} 存在重复词: {dup[:5]}")

    def wrap(self, word: str) -> str:
        return f"{self.open}{word}{self.close}"

    def pool(self, name: str) -> list[str]:
        pool = self.pools.get(name)
        if not pool:
            raise KeyError(f"词池不存在: {name}")
        return pool


@dataclass
class Rules:
    """替换策略。"""

    policy: dict[str, str] = field(default_factory=dict)
    case_no: dict[str, Any] = field(default_factory=dict)
    detect: dict[str, Any] = field(default_factory=dict)
    residual: dict[str, list[str]] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "Rules":
        p = path or (config_dir() / "rules.yaml")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        policy = {k.value: v for k, v in DEFAULT_POLICY.items()}
        policy.update(data.get("policy") or {})
        return cls(
            policy=policy,
            case_no=dict(data.get("case_no") or {}),
            detect=dict(data.get("detect") or {}),
            residual=dict(data.get("residual_patterns") or {}),
            output=dict(data.get("output") or {}),
        )

    def policy_for(self, etype: EntityType) -> str:
        return self.policy.get(etype.value, "mask")

    def name_cues(self) -> list[str]:
        return list(self.detect.get("name_cues") or [])

    def name_blockers(self) -> list[str]:
        return list(self.detect.get("name_blockers") or [])


@dataclass
class Settings:
    lexicon: Lexicon
    rules: Rules

    @classmethod
    def load(cls, lexicon_path=None, rules_path=None) -> "Settings":
        return cls(Lexicon.load(lexicon_path), Rules.load(rules_path))
