"""内容寻址的附件库。

附件（原始统计表、翻译稿、签章页、报告 PDF 等）以字节哈希作为唯一
指纹存储；共享盘里同名文件被更新时，会产生新版本而旧版本仍可按哈希
原样取回。上传重试（同内容或同幂等键）不会产生重复版本，也不会改写
任何历史。
"""

from dataclasses import dataclass
from datetime import datetime

from .errors import IntegrityError, NotFoundError
from .hashing import sha256_bytes
from .events import EventLog


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_id: str
    name: str
    version: int
    sha256: str
    size: int
    media_type: str
    uploaded_at: str
    uploaded_by: str
    tags: tuple[tuple[str, str], ...] = ()

    def ref(self) -> dict:
        """供快照/提交包引用的不可变指针。"""
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
        }

    def tag(self, key: str) -> str | None:
        return dict(self.tags).get(key)


class ArtifactStore:
    def __init__(self, events: EventLog):
        self._events = events
        # 内容寻址：同一份字节只存一次。
        self._blobs: dict[str, bytes] = {}
        self._by_id: dict[str, dict] = {}        # artifact_id -> 元数据
        self._name_index: dict[str, str] = {}    # 逻辑文件名 -> artifact_id

    # ------------------------------------------------------------------ 写入

    def register(
        self,
        name: str,
        data: bytes,
        uploaded_by: str,
        moment: datetime,
        media_type: str = "application/octet-stream",
        idempotency_key: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> ArtifactVersion:
        """登记附件；同名 + 不同内容形成新版本。

        * 同名且内容未变（含网络重试）：直接返回当前版本，不追加事件。
        * 携带 idempotency_key 的重试：返回首次调用产生的版本。
        * 同名但内容变化：创建新版本，旧版本与旧哈希保持可读。

        tags 是版本随附的语义标记（如 kind=stat_table、language=zh、
        signed=true），供法域规则检查，不参与内容哈希。
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("附件内容必须是字节")
        digest = sha256_bytes(bytes(data))
        tag_tuple = tuple(sorted((tags or {}).items()))
        existing_artifact_id = self._name_index.get(name)

        # 显式幂等键：由事件链保证只生效一次。
        if idempotency_key is not None:
            for prior in self._events.filter("artifact.registered"):
                if prior.idempotency_key == idempotency_key:
                    return self.get_version(
                        prior.payload["stream_id"], prior.payload["version"]
                    )

        if existing_artifact_id is not None:
            artifact = self._by_id[existing_artifact_id]
            latest = artifact["versions"][-1]
            if latest.sha256 == digest:
                # 纯重试或重复上传：历史不增加一行。
                return latest

        if existing_artifact_id is None:
            import uuid

            artifact_id = str(uuid.uuid4())
            version_no = 1
            self._name_index[name] = artifact_id
            self._by_id[artifact_id] = {"name": name, "versions": []}
        else:
            artifact_id = existing_artifact_id
            version_no = len(self._by_id[artifact_id]["versions"]) + 1

        self._blobs[digest] = bytes(data)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            name=name,
            version=version_no,
            sha256=digest,
            size=len(data),
            media_type=media_type,
            uploaded_at=moment.isoformat(),
            uploaded_by=uploaded_by,
            tags=tag_tuple,
        )
        self._by_id[artifact_id]["versions"].append(version)
        self._events.append(
            "artifact.registered",
            {
                "stream_id": artifact_id,
                "name": name,
                "version": version_no,
                "sha256": digest,
                "size": len(data),
                "media_type": media_type,
                "tags": dict(tag_tuple),
            },
            actor=uploaded_by,
            timestamp=moment,
            idempotency_key=idempotency_key,
        )
        return version

    # ------------------------------------------------------------------ 读取

    def get_bytes(self, digest: str) -> bytes:
        """按内容哈希取回字节，并当场重新哈希验证。"""
        blob = self._blobs.get(digest)
        if blob is None:
            raise NotFoundError(f"附件内容不存在: {digest}")
        if sha256_bytes(blob) != digest:
            raise IntegrityError(f"附件内容损坏: {digest}")
        return blob

    def get_version(self, artifact_id: str, version: int) -> ArtifactVersion:
        artifact = self._by_id.get(artifact_id)
        if artifact is None:
            raise NotFoundError(f"附件不存在: {artifact_id}")
        if version < 1 or version > len(artifact["versions"]):
            raise NotFoundError(f"附件 {artifact_id} 版本 {version} 不存在")
        return artifact["versions"][version - 1]

    def get_by_name(self, name: str, version: int | None = None) -> ArtifactVersion:
        artifact_id = self._name_index.get(name)
        if artifact_id is None:
            raise NotFoundError(f"附件名不存在: {name}")
        if version is None:
            return self._by_id[artifact_id]["versions"][-1]
        return self.get_version(artifact_id, version)

    def latest(self, artifact_id: str) -> ArtifactVersion:
        artifact = self._by_id.get(artifact_id)
        if artifact is None:
            raise NotFoundError(f"附件不存在: {artifact_id}")
        return artifact["versions"][-1]

    def list_versions(self, name: str) -> list[ArtifactVersion]:
        artifact_id = self._name_index.get(name)
        if artifact_id is None:
            raise NotFoundError(f"附件名不存在: {name}")
        return list(self._by_id[artifact_id]["versions"])

    def resolve_ref(self, ref: dict) -> tuple[ArtifactVersion, bytes]:
        """解析快照里保存的引用指针，校验哈希与版本一致。"""
        version = self.get_version(ref["artifact_id"], ref["version"])
        if version.sha256 != ref["sha256"] or version.name != ref["name"]:
            raise IntegrityError(f"附件引用与库中记录不一致: {ref}")
        return version, self.get_bytes(version.sha256)

    # ------------------------------------------------------------------ 校验

    def verify(self) -> None:
        """重新哈希全部 blob，并确认版本事件与库状态一致。"""
        for digest, blob in self._blobs.items():
            if sha256_bytes(blob) != digest:
                raise IntegrityError(f"附件内容损坏: {digest}")
