"""企业表单与材料受理。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .crawler import sha256_hex
from .models import Material
from .storage import Storage, now_iso


class IntakeError(RuntimeError):
    pass


class IntakeService:
    """接收企业表单字段与材料清单，材料按内容版本化。"""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def intake_material(
        self,
        app_id: str,
        filename: str,
        material_type: str,
        content: bytes,
        note: str = "",
    ) -> Material:
        """登记一份材料；内容未变化时复用既有版本，变化时生成新版本。"""
        if self.storage.get_application(app_id) is None:
            raise IntakeError(f"申请 {app_id} 不存在")
        existing = [
            m for m in self.storage.latest_material_snapshot(app_id)
            if m.filename == filename
        ]
        digest = sha256_hex(content)
        if existing and existing[0].content_hash == digest:
            return existing[0]
        version = self.storage.next_material_version(app_id)
        return self.storage.add_material(Material(
            material_id=None, app_id=app_id, filename=filename,
            material_type=material_type, version=version, content_hash=digest,
            bytes_len=len(content), uploaded_at=now_iso(),
            supersedes=existing[0].material_id if existing else None, note=note,
        ))

    def intake_material_file(
        self, app_id: str, path: str | Path, material_type: str,
        note: str = "",
    ) -> Material:
        path = Path(path)
        return self.intake_material(
            app_id, path.name, material_type, path.read_bytes(), note
        )

    def intake_manifest(
        self, app_id: str, entries: list[dict], base_dir: Optional[str | Path] = None
    ) -> list[Material]:
        """批量受理材料清单。

        每条 entry: {"filename", "material_type", "path"(可选)}；
        提供 path 时读取文件字节，否则以空内容占位（演示/测试用）。
        """
        base = Path(base_dir) if base_dir else None
        results = []
        for entry in entries:
            if entry.get("path"):
                path = Path(entry["path"])
                if base and not path.is_absolute():
                    path = base / path
                content = path.read_bytes()
            else:
                content = entry.get("content", b"").encode("utf-8") \
                    if isinstance(entry.get("content"), str) \
                    else entry.get("content", b"")
            results.append(self.intake_material(
                app_id, entry["filename"], entry["material_type"],
                content, entry.get("note", ""),
            ))
        return results
