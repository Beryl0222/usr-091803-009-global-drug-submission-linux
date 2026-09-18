"""各法域随时间生效的申报要求与截止日期。

同一法域的要求会换版（指南更新、签章规则变化）。每次登记一条
RequirementVersion 都带 [effective_from, effective_until) 生效区间；
系统按提交发生的时点选出当时有效的版本。截止日期同样按版本保存，
调整时限即登记新版本，旧区间内"当时适用的截止日"仍可精确还原。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .errors import NotFoundError, RuleViolation
from .events import EventLog


@dataclass(frozen=True)
class RequirementVersion:
    requirement_id: str
    version: int
    jurisdiction: str          # 如 US / EU / JP
    title: str
    effective_from: str        # ISO 时间，闭区间起点
    effective_until: str | None  # None 表示至今有效
    # 必须覆盖的受试者人群代码（空集合表示不限制）。
    required_populations: frozenset[str]
    # 必须出现的统计表标记（artifact tags 中的 kind 值）。
    required_table_kinds: frozenset[str]
    required_languages: frozenset[str]
    require_signature: bool
    # 提交时限要求（相对某锚点日期的天数，None 表示无固定时限）。
    deadline_days: int | None
    notes: str


class RequirementRegistry:
    def __init__(self, events: EventLog):
        self._events = events
        # requirement_id -> 按版本排列
        self._requirements: dict[str, list[RequirementVersion]] = {}
        # (jurisdiction, title) -> requirement_id
        self._index: dict[tuple[str, str], str] = {}

    def register(
        self,
        jurisdiction: str,
        title: str,
        effective_from: datetime,
        moment: datetime,
        registered_by: str,
        required_populations: set[str] | frozenset[str] | None = None,
        required_table_kinds: set[str] | frozenset[str] | None = None,
        required_languages: set[str] | frozenset[str] | None = None,
        require_signature: bool = False,
        deadline_days: int | None = None,
        notes: str = "",
        effective_until: datetime | None = None,
    ) -> RequirementVersion:
        """登记某法域要求的一个新版本。

        同一 (法域, 标题) 的新版本会把上一版本的区间关闭；不允许生效
        起点早于既有版本，保证时间线单向、可回放。
        """
        key = (jurisdiction, title)
        requirement_id = self._index.get(key)
        if requirement_id is None:
            requirement_id = str(uuid.uuid4())
            self._index[key] = requirement_id
            self._requirements[requirement_id] = []
            version_no = 1
        else:
            versions = self._requirements[requirement_id]
            version_no = len(versions) + 1
            prev = versions[-1]
            prev_from = datetime.fromisoformat(prev.effective_from)
            if self._aware(effective_from) < prev_from:
                raise RuleViolation(
                    f"{jurisdiction}《{title}》新版本生效时间不得早于既有版本"
                )
            # 关闭上一版本的开放区间（登记为替换事件，不改写旧记录）。
            versions[-1] = self._replace_until(
                prev, self._aware(effective_from)
            )

        version = RequirementVersion(
            requirement_id=requirement_id,
            version=version_no,
            jurisdiction=jurisdiction,
            title=title,
            effective_from=self._aware(effective_from).isoformat(),
            effective_until=(
                self._aware(effective_until).isoformat()
                if effective_until is not None
                else None
            ),
            required_populations=frozenset(required_populations or ()),
            required_table_kinds=frozenset(required_table_kinds or ()),
            required_languages=frozenset(required_languages or ()),
            require_signature=require_signature,
            deadline_days=deadline_days,
            notes=notes,
        )
        self._requirements[requirement_id].append(version)
        self._events.append(
            "requirement.registered",
            {
                "stream_id": requirement_id,
                "version": version_no,
                "jurisdiction": jurisdiction,
                "title": title,
                "effective_from": version.effective_from,
                "effective_until": version.effective_until,
                "required_populations": sorted(version.required_populations),
                "required_table_kinds": sorted(version.required_table_kinds),
                "required_languages": sorted(version.required_languages),
                "require_signature": require_signature,
                "deadline_days": deadline_days,
                "notes": notes,
            },
            actor=registered_by,
            timestamp=moment,
        )
        return version

    @staticmethod
    def _replace_until(
        version: RequirementVersion, until: datetime
    ) -> RequirementVersion:
        return RequirementVersion(
            requirement_id=version.requirement_id,
            version=version.version,
            jurisdiction=version.jurisdiction,
            title=version.title,
            effective_from=version.effective_from,
            effective_until=until.isoformat(),
            required_populations=version.required_populations,
            required_table_kinds=version.required_table_kinds,
            required_languages=version.required_languages,
            require_signature=version.require_signature,
            deadline_days=version.deadline_days,
            notes=version.notes,
        )

    # ------------------------------------------------------------------ 查询

    def effective_version(
        self, jurisdiction: str, title: str, at: datetime
    ) -> RequirementVersion:
        requirement_id = self._index.get((jurisdiction, title))
        if requirement_id is None:
            raise NotFoundError(f"法域要求不存在: {jurisdiction}/{title}")
        point = self._aware(at)
        for version in self._requirements[requirement_id]:
            start = datetime.fromisoformat(version.effective_from)
            end = (
                datetime.fromisoformat(version.effective_until)
                if version.effective_until
                else None
            )
            if start <= point and (end is None or point < end):
                return version
        raise RuleViolation(
            f"{at.isoformat()} 时 {jurisdiction}《{title}》没有生效版本"
        )

    def effective_requirements(
        self, jurisdiction: str, at: datetime
    ) -> list[RequirementVersion]:
        """返回该法域在指定时点所有"当前生效"的要求（每标题取一版）。"""
        result = []
        for juris, title in self._index:
            if juris != jurisdiction:
                continue
            result.append(self.effective_version(jurisdiction, title, at))
        return sorted(result, key=lambda v: v.title)

    def history(self, jurisdiction: str, title: str) -> list[RequirementVersion]:
        requirement_id = self._index.get((jurisdiction, title))
        if requirement_id is None:
            raise NotFoundError(f"法域要求不存在: {jurisdiction}/{title}")
        return list(self._requirements[requirement_id])

    @staticmethod
    def _aware(moment: datetime) -> datetime:
        from datetime import timezone

        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)


@dataclass
class Deadline:
    """某法域针对某锚点事件（如首例数据锁定）适用的截止日期。"""

    jurisdiction: str
    anchor: str
    due_at: str
    basis_requirement_id: str
    basis_version: int
    adjusted_from: str | None = None
    reason: str = ""
