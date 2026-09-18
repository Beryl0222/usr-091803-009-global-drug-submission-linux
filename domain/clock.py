"""时间抽象。

领域代码不直接调用 datetime.now()，而是依赖 Clock，使"法规在某时点
生效""截止日调整""当天发送了什么"这类时间相关逻辑可以被精确重放。
"""

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """测试与审计重放用的固定/可推进时钟。"""

    def __init__(self, moment: datetime):
        self._moment = self._ensure_aware(moment)

    @staticmethod
    def _ensure_aware(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._moment

    def advance(self, **kwargs) -> datetime:
        self._moment = self._moment + timedelta(**kwargs)
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = self._ensure_aware(moment)
