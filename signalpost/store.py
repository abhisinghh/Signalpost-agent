"""Content-addressed snapshot store and per-company refresh state."""
from __future__ import annotations

import gzip
import json
import os
import uuid
from pathlib import Path

from .util import now_iso, sha256_hex


class SnapshotStore:
    """Immutable raw snapshots, stored gzip-compressed by SHA-256 of the body."""

    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)

    def blob_path(self, sha: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}.gz"

    def put(self, *, content: bytes, requested_url: str, final_url: str, status: int,
            content_type: str, redirects: list[str], truncated: bool, source_class: str) -> dict:
        sha = sha256_hex(content)
        path = self.blob_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            with gzip.GzipFile(tmp, "wb", mtime=0) as f:
                f.write(content)
            os.replace(tmp, path)
        snapshot_id = "snap_" + sha256_hex(f"{final_url}\n{sha}")[:20]
        return {
            "snapshot_id": snapshot_id,
            "requested_url": requested_url,
            "final_url": final_url,
            "redirects": redirects,
            "status": status,
            "retrieved_at": now_iso(),
            "content_sha256": sha,
            "content_type": content_type,
            "bytes": len(content),
            "truncated": truncated,
            "source_class": source_class,
            "blob": str(path.relative_to(self.root)),
        }


class StateStore:
    """Last-known claims per company, used to compute refresh diffs."""

    def __init__(self, root: Path):
        self.dir = Path(root) / "companies"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, orgnr: str) -> Path:
        return self.dir / orgnr[:3] / f"{orgnr}.json"

    def load(self, orgnr: str) -> dict | None:
        p = self._path(orgnr)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, orgnr: str, state: dict) -> None:
        p = self._path(orgnr)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), "utf-8")
        os.replace(tmp, p)
