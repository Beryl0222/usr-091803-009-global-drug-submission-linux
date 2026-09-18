"""只增事件日志（哈希链）。

所有状态变更都先落事件：事件按序号串联，每条事件包含前一条的摘要，
任何对历史的删除、重排或篡改都会让 verify() 失败。审计与通知记录
最终都以这条链为准。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from .hashing import canonical_json, digest_json


@dataclass(frozen=True)
class Event:
    seq: int
    timestamp: str
    event_type: str
    payload: dict
    actor: str
    prev_hash: str
    hash: str
    idempotency_key: str | None = field(default=None)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": self.payload,
            "actor": self.actor,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "idempotency_key": self.idempotency_key,
        }


GENESIS_HASH = "0" * 64


class EventLog:
    def __init__(self):
        self._events: list[Event] = []
        self._idempotency: dict[str, int] = {}

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_HASH

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        timestamp: datetime,
        idempotency_key: str | None = None,
    ) -> Event:
        """追加事件。

        相同 idempotency_key 的重试直接返回首次事件，保证上传重试、
        超时重发不会在历史里产生重复动作。
        """
        if idempotency_key is not None:
            existing_seq = self._idempotency.get(idempotency_key)
            if existing_seq is not None:
                return self._events[existing_seq - 1]

        seq = len(self._events) + 1
        body = {
            "seq": seq,
            "timestamp": timestamp.isoformat(),
            "event_type": event_type,
            "payload": payload,
            "actor": actor,
            "prev_hash": self.head_hash,
        }
        event = Event(
            seq=seq,
            timestamp=body["timestamp"],
            event_type=event_type,
            payload=dict(payload),
            actor=actor,
            prev_hash=body["prev_hash"],
            hash=digest_json(body),
            idempotency_key=idempotency_key,
        )
        self._events.append(event)
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = seq
        return event

    def all(self) -> list[Event]:
        return list(self._events)

    def filter(
        self, event_type: str | None = None, stream_id: str | None = None
    ) -> Iterator[Event]:
        for event in self._events:
            if event_type is not None and event.event_type != event_type:
                continue
            if stream_id is not None and event.payload.get("stream_id") != stream_id:
                continue
            yield event

    def verify(self) -> None:
        """从头重算整条链，发现任何偏差立即抛出 IntegrityError。"""
        from .errors import IntegrityError

        prev = GENESIS_HASH
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                raise IntegrityError(f"事件序号断裂: 期望 {index}，实际 {event.seq}")
            if event.prev_hash != prev:
                raise IntegrityError(f"事件 {index} 前序哈希不匹配")
            body = {
                "seq": event.seq,
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "payload": event.payload,
                "actor": event.actor,
                "prev_hash": event.prev_hash,
            }
            if digest_json(body) != event.hash:
                raise IntegrityError(f"事件 {index} 内容哈希不匹配")
            prev = event.hash

    def export_jsonl(self) -> bytes:
        import json

        return b"".join(
            json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
            for event in self._events
        )

    @classmethod
    def from_jsonl(cls, raw: bytes) -> "EventLog":
        import json

        log = cls()
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            event = Event(
                seq=record["seq"],
                timestamp=record["timestamp"],
                event_type=record["event_type"],
                payload=record["payload"],
                actor=record["actor"],
                prev_hash=record["prev_hash"],
                hash=record["hash"],
                idempotency_key=record.get("idempotency_key"),
            )
            log._events.append(event)
            if event.idempotency_key is not None:
                log._idempotency[event.idempotency_key] = event.seq
        log.verify()
        return log
