"""领域错误类型。"""


class DomainError(Exception):
    """所有领域规则违反的基类。"""


class IntegrityError(DomainError):
    """哈希校验失败、引用悬空、事件链断裂等不可自动恢复的问题。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class DuplicateError(DomainError):
    """业务键冲突（非幂等重试）。"""


class WorkflowError(DomainError):
    """对象当前状态不允许该操作。"""


class RuleViolation(DomainError):
    """违反法域规则：翻译、签章、人群、统计表或时限要求。"""
