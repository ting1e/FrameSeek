from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MODEL_ID = "facebook/dinov3-vitl16-pretrain-lvd1689m"
DIMENSION = 1024


def boolean(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in {"true", "1", "yes"}


def directory_sources(value: str) -> dict[str, Path]:
    """Public configuration is a directory list; source IDs remain private index keys."""
    import hashlib
    directories = json.loads(value)
    if not isinstance(directories, list) or not directories or len(directories) > 100:
        raise ValueError('IMGS_MEDIA_DIRECTORIES must be a nonempty JSON directory list')
    result, seen = {}, set()
    for directory in directories:
        if not isinstance(directory, str) or not directory.strip() or '\x00' in directory:
            raise ValueError('Media directories must be nonempty path strings')
        root = Path(directory.strip()).expanduser()
        normalized = root.as_posix().rstrip('/')
        resolved = os.path.normcase(str(root.resolve()))
        if resolved in seen:
            raise ValueError('Duplicate media directory')
        seen.add(resolved)
        # Compatibility with existing local/NAS indexes; never relabel millions of vectors.
        legacy = {'bif/sda':'sda','bif/sdc':'sdc','/media/sda':'sda','/media/sdc':'sdc'}
        ident = legacy.get(normalized)
        if root.name == 'video' and root.parent.name in {'sda', 'sdc'}:
            ident = root.parent.name
        if not ident:
            for suffix in ('/bif/sda','/bif/sdc'):
                if normalized.lower().endswith(suffix): ident = suffix.rsplit('/',1)[1]
        ident = ident or 'dir_' + hashlib.sha256(resolved.encode()).hexdigest()[:20]
        if ident in result:
            raise ValueError('Ambiguous directory roots for an existing index source')
        result[ident] = root
    return result


def configured_sources():
    value = os.getenv('IMGS_MEDIA_DIRECTORIES')
    if value is not None:
        return directory_sources(value)
    legacy = os.getenv('IMGS_SOURCES')
    if legacy is not None:
        return {key:Path(value) for key,value in json.loads(legacy).items()}
    return {}


@dataclass
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv("IMGS_DATA", "data")))
    model: Path = field(default_factory=lambda: Path(os.getenv("IMGS_MODEL", "models/dinov3-vitl16")))
    manifest_file: Path | None = field(default_factory=lambda: Path(os.environ['IMGS_MODEL_MANIFEST']) if os.getenv('IMGS_MODEL_MANIFEST') else None)
    sources: dict[str, Path] = field(default_factory=configured_sources)
    mode: str = field(default_factory=lambda: os.getenv("IMGS_MODE", "low_memory"))
    qdrant_url: str = field(default_factory=lambda: os.getenv("IMGS_QDRANT_URL", "http://127.0.0.1:6333"))
    qdrant_key: str | None = field(default_factory=lambda: os.getenv("IMGS_QDRANT_KEY"))
    device: str = field(default_factory=lambda: os.getenv("IMGS_DEVICE", "cpu"))
    precision: str = field(default_factory=lambda: os.getenv('IMGS_PRECISION', 'fp32'))
    openvino_model: Path | None = field(default_factory=lambda: Path(os.environ['IMGS_OPENVINO_MODEL']) if os.getenv('IMGS_OPENVINO_MODEL') else None)
    batch: int = field(default_factory=lambda: int(os.getenv("IMGS_BATCH", "1")))
    chunk_frames: int = field(default_factory=lambda: int(os.getenv("IMGS_CHUNK_FRAMES", "64")))
    decode_workers: int = field(default_factory=lambda: int(os.getenv("IMGS_DECODE_WORKERS", "1")))
    index_scope: dict[str, list[str]] = field(default_factory=lambda: json.loads(os.getenv('IMGS_INDEX_SCOPE', '{}')))
    interval: int = field(default_factory=lambda: int(os.getenv("IMGS_SCAN_INTERVAL", "86400")))
    stable_seconds: int = field(default_factory=lambda: int(os.getenv("IMGS_STABLE_SECONDS", "60")))
    auto_update: bool = field(default_factory=lambda: boolean("IMGS_AUTO_UPDATE", False))
    escaped_paths: bool = field(default_factory=lambda: boolean("IMGS_ESCAPED_PATHS", os.name == "nt"))
    secure_cookie: bool = field(default_factory=lambda: boolean("IMGS_SECURE_COOKIE", True))
    username: str = field(default_factory=lambda: os.getenv("IMGS_USERNAME", "admin"))
    password_hash: str = field(default_factory=lambda: os.getenv("IMGS_PASSWORD_HASH", ""))
    session_secret: str = field(default_factory=lambda: os.getenv("IMGS_SESSION_SECRET", ""))
    cpu_threads: int = field(default_factory=lambda: int(os.getenv('IMGS_TORCH_THREADS', '4')))
    indexing_threads: int = field(default_factory=lambda: int(os.getenv('IMGS_INDEXING_THREADS', '1')))
    qdrant_memory_gib: int = field(default_factory=lambda: 12 if os.getenv('IMGS_MODE') == 'high_memory' else 2)
    app_memory_gib: int = 4
    default_top: int = 20
    collapse_results: bool = True
    excluded_directories: list[str] = field(default_factory=list)
    monitor_folders: list[dict] | None = None

    def __post_init__(self):
        # Directory IDs and roots travel with SQLite, independently of Compose.
        if self.db_path.exists():
            import sqlite3
            from frameseek.core.preferences import Preferences, FIELDS, DOCKER_FIELDS
            with sqlite3.connect(self.db_path, timeout=60) as connection:
                has_meta = connection.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                saved = connection.execute("SELECT value FROM meta WHERE key='runtime_preferences'").fetchone() if has_meta else None
                registry = connection.execute("SELECT value FROM meta WHERE key='media_registry'").fetchone() if has_meta else None
            if registry:
                from frameseek.media.directories import restore_sources
                self.sources = restore_sources(json.loads(registry[0]), self.sources)
            if saved:
                profile = json.loads(saved[0])
                profile.setdefault("decode_workers", self.decode_workers)
                profile.setdefault('device', self.device)
                profile.setdefault('precision', self.precision)
                profile.setdefault("monitor_folders", self.monitor_folders or [{'source':name, 'path':''} for name in self.sources])
                values = Preferences.model_validate(profile).model_dump()
                for name, attribute in FIELDS.items():
                    if name not in DOCKER_FIELDS:
                        setattr(self, attribute, values[name])
        if self.mode not in {"low_memory", "high_memory"}:
            raise ValueError("IMGS_MODE must be low_memory or high_memory")
        from frameseek.core.preferences import Preferences
        Preferences(device=self.device, precision=self.precision)
        if self.batch < 1 or self.chunk_frames < 1 or self.interval < 1 or self.stable_seconds < 0:
            raise ValueError("Invalid batch/scan/stability settings")
        if not 1 <= self.decode_workers <= 16:
            raise ValueError("IMGS_DECODE_WORKERS must be between 1 and 16")
        if self.monitor_folders is None:
            self.monitor_folders = [{'source':name, 'path':''} for name in self.sources]
        if any(not key.isidentifier() for key in self.sources):
            raise ValueError("Source IDs must be identifiers")
        if not isinstance(self.index_scope, dict):
            raise ValueError('IMGS_INDEX_SCOPE must map source IDs to directory lists')
        for source, directories in self.index_scope.items():
            if source not in self.sources or not isinstance(directories, list) or not directories:
                raise ValueError('Invalid indexing source/directory list')
            for directory in directories:
                if (not isinstance(directory, str) or not directory or '\\' in directory
                        or ':' in directory or any(part in {'', '.', '..'} for part in directory.split('/'))):
                    raise ValueError('Indexing directories must be relative paths without traversal')
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "vectors").mkdir(exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data / "metadata.sqlite3"

    @property
    def manifest(self) -> dict:
        path = self.manifest_file or self.model / "manifest.json"
        if not path.is_file():
            raise RuntimeError("模型未就绪：请先运行 frameseek prepare-model")
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("model_id") != MODEL_ID or result.get("dimension") != DIMENSION:
            raise RuntimeError("模型清单不匹配 DINOv3 ViT-L/16")
        return result
