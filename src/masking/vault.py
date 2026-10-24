"""映射表（vault）：代号 ↔ 原文对照表，加密落盘。

安全约定
--------
1. 映射表是最高机密：明文只在内存里存在，落盘一律 Fernet 加密。
2. 密钥固定放在映射表同目录的 ``.key``，文件权限 0o600（仅本人可读）。
3. 密钥丢失 = 映射不可恢复。此时必须明确报错并要求重新脱敏，
   绝不尝试「猜」或降级成明文读取。
4. 加密前的 JSON 用 ``ensure_ascii=False, indent=2`` 生成，
   便于日后授权取证时解密后直接阅读。
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken

from . import __version__
from .allocator import MappingEntry

#: 密钥文件名（与映射表同目录）
KEY_FILENAME = ".key"
#: 映射表结构版本，便于日后迁移
VAULT_VERSION = 1
#: 私有文件权限
_PRIVATE_FILE_MODE = 0o600
#: 私有目录权限
_PRIVATE_DIR_MODE = 0o700


class VaultError(RuntimeError):
    """映射表相关的可预期错误基类。"""


class VaultKeyMissing(VaultError):
    """密钥文件不存在或不可读。"""


class VaultKeyInvalid(VaultError):
    """密钥文件内容不是合法的 Fernet 密钥。"""


class VaultDecryptError(VaultError):
    """密文无法解密（密钥不匹配、文件损坏或被篡改）。"""


def _now_iso() -> str:
    """本地时区的 ISO 8601 时间戳。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ensure_private_dir(path: pathlib.Path) -> None:
    """创建目录并收紧权限为 0o700（已存在则只收紧）。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, _PRIVATE_DIR_MODE)
    except OSError:  # 某些文件系统（如 FAT/NTFS 挂载）不支持 chmod
        pass


def _write_private_file(path: pathlib.Path, data: bytes) -> None:
    """原子写入私有文件：临时文件 -> os.replace，全程 0o600。

    先写 ``.tmp`` 再替换，避免加密写入中途失败留下半个映射表。
    """
    _ensure_private_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, _PRIVATE_FILE_MODE)
    except OSError:
        pass


def _read_key(key_path: pathlib.Path) -> bytes:
    """读取并校验密钥文件。"""
    if not key_path.exists():
        raise VaultKeyMissing(
            f"密钥文件不存在：{key_path}\n"
            "映射不可恢复，需重新脱敏。\n"
            "若映射表来自备份，请把配套的 .key 文件放回同一目录后重试；"
            "没有 .key 时任何程序都无法解密该映射表。"
        )
    try:
        raw = key_path.read_bytes().strip()
    except OSError as exc:
        raise VaultKeyMissing(f"密钥文件不可读：{key_path}（{exc}）") from exc
    try:
        Fernet(raw)  # 触发格式校验
    except (ValueError, TypeError) as exc:
        raise VaultKeyInvalid(
            f"密钥文件损坏或不是合法的 Fernet 密钥：{key_path}\n"
            "映射不可恢复，需重新脱敏（请勿手工编辑 .key 文件）。"
        ) from exc
    return raw


def load_or_create_key(key_path: pathlib.Path) -> bytes:
    """读取密钥；不存在则生成一个新的并落盘（仅 ``save`` 路径调用）。"""
    if key_path.exists():
        return _read_key(key_path)
    key = Fernet.generate_key()
    _write_private_file(key_path, key + b"\n")
    return key


def key_path_for(vault_path: str | pathlib.Path) -> pathlib.Path:
    """映射表配套密钥路径：与映射表同目录的 ``.key``。"""
    return pathlib.Path(vault_path).expanduser().resolve().parent / KEY_FILENAME


@dataclass
class Vault:
    """一份加密映射表。

    一个 ``scope``（案件作用域）对应一份：案件内不同文件、不同格式共用同一份，
    因此同一个人在 docx 与 pdf 里拿到的代号一定相同。
    """

    scope: str
    created_at: str = field(default_factory=_now_iso)
    entries: list[MappingEntry] = field(default_factory=list)
    tool_version: str = __version__

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_entries(
        cls,
        scope: str,
        entries: Iterable[MappingEntry],
        *,
        created_at: str | None = None,
        tool_version: str | None = None,
    ) -> "Vault":
        """从分配器的 ``entries`` 生成映射表（复制条目，避免共享可变状态）。"""
        copied = [
            MappingEntry(
                type=e.type,
                original=e.original,
                placeholder=e.placeholder,
                count=int(e.count),
            )
            for e in entries
        ]
        return cls(
            scope=scope,
            created_at=created_at or _now_iso(),
            entries=copied,
            tool_version=tool_version or __version__,
        )

    # ------------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        """内存对象 -> 可 JSON 化的字典（加密前的明文结构）。"""
        return {
            "vault_version": VAULT_VERSION,
            "tool_version": self.tool_version,
            "scope": self.scope,
            "created_at": self.created_at,
            "entry_count": len(self.entries),
            "entries": [
                {
                    "type": e.type,
                    "original": e.original,
                    "placeholder": e.placeholder,
                    "count": e.count,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Vault":
        """解码后的字典 -> Vault。对字段缺失保持宽容，但类型必须正确。"""
        if not isinstance(data, dict):
            raise VaultDecryptError("映射表内容不是 JSON 对象，文件可能已损坏。")
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            raise VaultDecryptError("映射表缺少 entries 列表，文件可能已损坏。")
        entries: list[MappingEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise VaultDecryptError("映射表 entries 中存在非对象条目，文件可能已损坏。")
            try:
                entries.append(
                    MappingEntry(
                        type=str(item["type"]),
                        original=str(item["original"]),
                        placeholder=str(item["placeholder"]),
                        count=int(item.get("count", 0)),
                    )
                )
            except KeyError as exc:
                raise VaultDecryptError(f"映射表条目缺少字段 {exc}，文件可能已损坏。") from exc
        return cls(
            scope=str(data.get("scope", "")),
            created_at=str(data.get("created_at", "")),
            entries=entries,
            tool_version=str(data.get("tool_version", "unknown")),
        )

    def to_json_text(self) -> str:
        """明文 JSON 文本（仅用于加密前构造或授权取证时展示）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ 落盘 / 读取

    def save(self, path: str | pathlib.Path, *, key_path: str | pathlib.Path | None = None) -> pathlib.Path:
        """加密保存映射表，返回实际路径。

        密钥默认写到同目录 ``.key``；不存在则生成。密钥与密文权限均为 0o600。
        """
        target = pathlib.Path(path).expanduser().resolve()
        key_file = pathlib.Path(key_path).expanduser().resolve() if key_path else (target.parent / KEY_FILENAME)
        key = load_or_create_key(key_file)
        plaintext = self.to_json_text().encode("utf-8")
        ciphertext = Fernet(key).encrypt(plaintext)
        # 附一行注释性头部，便于人肉辨认文件类型（不泄露任何明细）
        header = b"# masking-vault:v1 fernet\n"
        _write_private_file(target, header + ciphertext)
        return target

    @classmethod
    def load(cls, path: str | pathlib.Path, *, key_path: str | pathlib.Path | None = None) -> "Vault":
        """读取并解密映射表。

        密钥缺失 / 损坏 / 不匹配时抛出带下一步指引的 ``VaultError`` 子类。
        """
        source = pathlib.Path(path).expanduser().resolve()
        if not source.exists():
            raise VaultError(f"映射表不存在：{source}\n请确认 --vault 指向 mask 运行后生成的映射表（<案件>.json，位于输出包的 vault/ 下）。")
        key_file = pathlib.Path(key_path).expanduser().resolve() if key_path else (source.parent / KEY_FILENAME)
        key = _read_key(key_file)
        blob = source.read_bytes()
        header = b"# masking-vault:v1 fernet\n"
        if blob.startswith(header):
            blob = blob[len(header):]
        try:
            plaintext = Fernet(key).decrypt(blob)
        except InvalidToken as exc:
            raise VaultDecryptError(
                f"映射表解密失败：{source}\n"
                f"密钥文件：{key_file}\n"
                "可能原因：密钥与密文不配套、文件被改动过。\n"
                "映射不可恢复，需重新脱敏；若把本项目整体搬迁过，请确认 .key 与映射表同时被复制。"
            ) from exc
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultDecryptError(f"映射表解密后不是合法 JSON：{source}（{exc}）") from exc
        return cls.from_dict(data)

    # ------------------------------------------------------------------ 查询

    def __len__(self) -> int:
        return len(self.entries)

    def by_placeholder(self) -> dict[str, list[MappingEntry]]:
        """代号 -> 条目列表（正常情况下 value 长度为 1）。"""
        index: dict[str, list[MappingEntry]] = {}
        for entry in self.entries:
            index.setdefault(entry.placeholder, []).append(entry)
        return index

    def ambiguous_placeholders(self) -> dict[str, list[MappingEntry]]:
        """同一个代号对应多个原文的异常情况（掩码类不同实体撞号时会出现）。

        这种代号无法自动回填，``restore`` 会跳过并告警，交给人工处理。
        """
        return {ph: items for ph, items in self.by_placeholder().items() if len(items) > 1}

    def find_original(self, placeholder: str) -> str | None:
        """代号 -> 原文；撞号时返回 None（不猜）。"""
        items = self.by_placeholder().get(placeholder, [])
        return items[0].original if len(items) == 1 else None

    def find_placeholder(self, original: str) -> str | None:
        """原文 -> 代号；同一原文若因类型不同出现多条，优先返回第一条。"""
        for entry in self.entries:
            if entry.original == original:
                return entry.placeholder
        return None

    def type_stats(self) -> dict[str, int]:
        """按实体类型统计「不同实体数」。"""
        stats: dict[str, int] = {}
        for entry in self.entries:
            stats[entry.type] = stats.get(entry.type, 0) + 1
        return stats

    def total_occurrences(self) -> int:
        """所有条目的命中次数之和。"""
        return sum(int(e.count) for e in self.entries)

    def describe(self) -> str:
        """一句话摘要，供 CLI / 报告使用。"""
        return (
            f"作用域 {self.scope or '(未命名)'}：{len(self.entries)} 个不同实体，"
            f"累计替换 {self.total_occurrences()} 处，创建于 {self.created_at}"
        )


__all__ = [
    "KEY_FILENAME",
    "Vault",
    "VaultDecryptError",
    "VaultError",
    "VaultKeyInvalid",
    "VaultKeyMissing",
    "key_path_for",
    "load_or_create_key",
]
