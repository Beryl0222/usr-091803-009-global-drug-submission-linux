"""统一哈希与字节编码工具。

全系统对"同一份字节"必须得到同一个指纹，因此哈希入口只此一处，
任何模块都不得自行调用 hashlib 以免算法不一致。
"""

import hashlib
import json

HASH_ALGORITHM = "sha256"


def sha256_bytes(data: bytes) -> str:
    """返回字节内容的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """文本一律以 UTF-8 落字节后再哈希。"""
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(value) -> bytes:
    """生成确定性 JSON 字节：键排序、无空白、非 ASCII 原样保留。

    快照清单、提交包清单与事件载荷都通过它序列化，保证跨进程、
    跨语言版本重新计算时哈希一致。
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_json(value) -> str:
    """对确定性 JSON 计算摘要。"""
    return sha256_bytes(canonical_json(value))
