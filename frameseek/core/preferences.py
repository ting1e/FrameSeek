"""Validated, persistent settings; resource changes apply on restart."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from pathlib import PurePosixPath


class MonitorFolder(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source: str = Field(min_length=1, max_length=100)
    path: str = Field(max_length=2000)

    @field_validator('path')
    @classmethod
    def safe_path(cls, value):
        value = value.strip()
        path = PurePosixPath(value)
        if path.is_absolute() or '\\' in value or '..' in path.parts or '\x00' in value:
            raise ValueError('监控目录应为来源内的相对目录，不能含绝对路径或 ..')
        return '' if value in {'', '.'} else path.as_posix()


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['low_memory', 'high_memory'] = 'low_memory'
    qdrant_memory_gib: int = Field(2, ge=1, le=24)
    app_memory_gib: int = Field(4, ge=2, le=16)
    cpu_threads: int = Field(4, ge=1, le=16)
    device: str = Field('cpu', max_length=40)
    precision: Literal['fp32', 'fp16'] = 'fp32'
    indexing_threads: int = Field(1, ge=1, le=8)
    decode_workers: int = Field(1, ge=1, le=16)
    batch: int = Field(1, ge=1, le=32)
    chunk_frames: int = Field(64, ge=1, le=1024)
    auto_update: bool = False
    scan_interval_seconds: int = Field(86400, ge=60, le=86400)
    stable_seconds: int = Field(60, ge=0, le=3600)
    default_top: Literal[20, 50, 100, 200, 500] = 20
    collapse_results: bool = True
    excluded_directories: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("excluded_directories")
    @classmethod
    def excluded_paths(cls, values):
        result = []
        for value in values:
            value = value.strip().replace("\\", "/")
            if not value or len(value) > 2000 or "\x00" in value or ".." in value.split("/") or not (value.startswith("/") or re.match(r"^[A-Za-z]:/", value)):
                raise ValueError("排除目录请填写完整路径，每行一个")
            result.append(value.rstrip("/") or "/")
        return list(dict.fromkeys(result))

    monitor_folders: list[MonitorFolder] = Field(default_factory=list, max_length=100)

    @field_validator('device')
    @classmethod
    def inference_device(cls, value):
        if value not in {'cpu', 'openvino:GPU', 'openvino:CPU'} and not re.fullmatch(r'cuda(?::\d+)?', value):
            raise ValueError('请选择 CPU、Intel 核显或 NVIDIA GPU')
        return value

    @model_validator(mode='after')
    def constrain_low_memory(self):
        if self.mode == 'low_memory' and self.indexing_threads != 1:
            raise ValueError('低内存模式的数据库建索引线程数应为 1')
        return self


class WebPreferences(Preferences):
    # Legacy source-relative profiles remain readable; new pages submit full paths.
    monitor_directories: list[str] | None = Field(None, max_length=100)

    @field_validator('monitor_directories')
    @classmethod
    def directory_lengths(cls, values):
        if values is not None and any(len(path) > 2000 for path in values):
            raise ValueError('目录最长 2000 个字符')
        return values


FIELDS = {name: name for name in Preferences.model_fields}
FIELDS['scan_interval_seconds'] = 'interval'
DOCKER_FIELDS = {'mode', 'qdrant_memory_gib', 'app_memory_gib', 'indexing_threads'}


def current(settings) -> dict:
    return {name: getattr(settings, attribute) for name, attribute in FIELDS.items()}


def requested(settings, db) -> dict:
    row = db.one("SELECT value FROM meta WHERE key='runtime_preferences'")
    profile = json.loads(row['value']) if row else current(settings)
    profile.setdefault('decode_workers', settings.decode_workers)
    profile.setdefault('monitor_folders', settings.monitor_folders)
    profile.setdefault('device', settings.device)
    profile.setdefault('precision', settings.precision)
    values = Preferences.model_validate(profile).model_dump()
    values.update({name: getattr(settings, FIELDS[name]) for name in DOCKER_FIELDS})
    return values


def compose_override(values: dict) -> str:
    p = Preferences.model_validate(values)
    # JSON is accepted as YAML by Compose and avoids hand-built escaping.
    return json.dumps({'services': {
        'qdrant': {'mem_limit': f'{p.qdrant_memory_gib}g', 'environment': {
            'QDRANT__STORAGE__HNSW_INDEX__MAX_INDEXING_THREADS': str(p.indexing_threads),
            'QDRANT__STORAGE__OPTIMIZERS__MAX_OPTIMIZATION_THREADS': '1'}},
        'app': {'mem_limit': f'{p.app_memory_gib}g', 'environment': {'IMGS_MODE': p.mode}}}}, indent=2)
