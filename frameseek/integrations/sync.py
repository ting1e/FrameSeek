from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
from pathlib import Path

import paramiko

from frameseek.engine.model import digest
from frameseek.core.paths import media_path
from frameseek.integrations.remote import configuration, roots

# Compatibility for local tools; values come only from the ignored private file.
NAS_ROOTS = roots()

# This code executes read-only over SSH; it does not install or write anything on the NAS.
INVENTORY_SCRIPT = r'''
import os, json, struct, sys
roots = json.loads(sys.argv[1])
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
for source, root in roots.items():
    if not os.path.isdir(root):
        emit({"type":"error", "source":source, "path":root, "error":"Source unavailable"})
        continue
    def failure(error):
        emit({"type":"error", "source":source, "path":str(error.filename), "error":str(error)})
    for directory, dirs, files in os.walk(root, onerror=failure, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory,d)))
        for name in sorted(files):
            if not name.lower().endswith('.bif'):
                continue
            path = os.path.join(directory,name)
            if os.path.islink(path):
                continue
            try:
                stat = os.stat(path)
                with open(path,'rb') as f:
                    header = f.read(64)
                valid = len(header)==64 and header[:8]==b'\x89BIF\r\n\x1a\n'
                count = struct.unpack_from('<I',header,12)[0] if valid else None
                emit({"type":"file", "source":source, "relpath":os.path.relpath(path,root).replace(os.sep,'/'),
                      "size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"frames":count,"header_valid":valid})
            except OSError as error:
                failure(error)
'''


def connect(host: str | None = None, port: int | None = None, user: str | None = None):
    config_values = configuration(required=not (host and user))
    host = host or config_values.get('host')
    port = port if port is not None else config_values.get('port', 22)
    user = user or config_values.get('user')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    identities = None
    config_path = Path.home() / ".ssh" / "config"
    if config_path.is_file():
        config = paramiko.SSHConfig()
        with config_path.open(encoding="utf-8") as stream:
            config.parse(stream)
        for alias in config.get_hostnames():
            candidate = config.lookup(alias)
            if candidate.get("hostname") == host and int(candidate.get("port", port)) == port:
                identities = candidate.get("identityfile")
                break
    client.connect(host, port=port, username=user, key_filename=identities,
                   look_for_keys=True, allow_agent=True, timeout=20, auth_timeout=20)
    return client


def inventory(client, destination: Path) -> dict:
    import shlex
    media_roots = roots(required=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp")
    stdin, stdout, stderr = client.exec_command("python3 - " + shlex.quote(json.dumps(media_roots, ensure_ascii=False)))
    stdin.write(INVENTORY_SCRIPT)
    stdin.channel.shutdown_write()
    summary = {"files": 0, "bytes": 0, "frames": 0, "errors": [], "sources": {}}
    with temp.open("w", encoding="utf-8") as stream:
        for line in stdout:
            record = json.loads(line)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            if record["type"] == "file":
                summary["files"] += 1
                summary["bytes"] += record["size"]
                summary["frames"] += record["frames"] or 0
                summary["sources"][record["source"]] = summary["sources"].get(record["source"], 0) + 1
            else:
                summary["errors"].append(record)
        stream.flush()
        os.fsync(stream.fileno())
    error = stderr.read().decode("utf-8", errors="replace")
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError("NAS inventory failed: " + error[:1000])
    os.replace(temp, destination)
    destination.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def sync(client, manifest: Path, destination: Path, escaped: bool, limit: int | None = None, seed: int = 20261006, media_roots: dict | None = None):
    media_roots = roots(required=True) if media_roots is None else media_roots
    with manifest.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    records = [record for record in records if record["type"] == "file"]
    records.sort(key=lambda r: (r["source"], r["relpath"]))
    if limit is not None and limit < len(records):
        # Stratify the sample by source so the first test includes both roots.
        generator = random.Random(seed)
        selected = []
        sources = sorted({r["source"] for r in records})
        for index, source in enumerate(sources):
            pool = [r for r in records if r["source"] == source]
            count = min(len(pool), limit // len(sources) + (index < limit % len(sources)))
            selected.extend(generator.sample(pool, count))
        records = selected
    destination.mkdir(parents=True, exist_ok=True)
    state = sqlite3.connect(destination / "sync.sqlite3")
    state.execute("CREATE TABLE IF NOT EXISTS copies(source TEXT,relpath TEXT,size INTEGER,mtime_ns INTEGER,sha256 TEXT,PRIMARY KEY(source,relpath))")
    stats = {"copied": 0, "skipped": 0, "bytes": 0, "errors": []}
    try:
        with client.open_sftp() as sftp:
            for position, row in enumerate(records, 1):
                relative, source = row["relpath"], row["source"]
                remote = media_roots[source] + "/" + relative
                target = media_path(destination / source, relative, escaped)
                target.parent.mkdir(parents=True, exist_ok=True)
                old = state.execute("SELECT size,mtime_ns,sha256 FROM copies WHERE source=? AND relpath=?", (source, relative)).fetchone()
                if old and old[:2] == (row["size"], row["mtime_ns"]) and target.is_file() and digest(target) == old[2]:
                    stats["skipped"] += 1
                    continue
                try:
                    start_stat = sftp.stat(remote)
                    if (start_stat.st_size, start_stat.st_mtime) != (row["size"], row["mtime_ns"] // 1_000_000_000):
                        raise RuntimeError("Remote file changed since inventory; rescan required")
                    partial = target.with_name(target.name + ".partial")
                    done = partial.stat().st_size if partial.exists() else 0
                    if done > row["size"]:
                        done = 0
                    remote_hash, local_prefix = hashlib.sha256(), hashlib.sha256()
                    with sftp.open(remote, "rb") as incoming:
                        incoming.prefetch(row["size"], max_concurrent_requests=32)
                        if done:
                            with partial.open("rb") as previous:
                                remaining = done
                                while remaining:
                                    block = incoming.read(min(1024 * 1024, remaining))
                                    if not block:
                                        raise RuntimeError("Truncated remote prefix")
                                    remote_hash.update(block)
                                    local_prefix.update(previous.read(len(block)))
                                    remaining -= len(block)
                            if remote_hash.digest() != local_prefix.digest():
                                incoming.seek(0)
                                remote_hash = hashlib.sha256()
                                done = 0
                        with partial.open("ab" if done else "wb") as output:
                            while block := incoming.read(1024 * 1024):
                                remote_hash.update(block)
                                output.write(block)
                            output.flush()
                            os.fsync(output.fileno())
                    end_stat = sftp.stat(remote)
                    if (end_stat.st_size, end_stat.st_mtime) != (start_stat.st_size, start_stat.st_mtime):
                        raise RuntimeError("Remote file changed during download")
                    checksum = digest(partial)
                    if partial.stat().st_size != row["size"] or checksum != remote_hash.hexdigest():
                        raise RuntimeError("Download checksum mismatch")
                    os.utime(partial, ns=(row["mtime_ns"], row["mtime_ns"]))
                    os.replace(partial, target)
                    state.execute("INSERT OR REPLACE INTO copies VALUES(?,?,?,?,?)",
                                  (source, relative, row["size"], row["mtime_ns"], checksum))
                    state.commit()
                    stats["copied"] += 1
                    stats["bytes"] += row["size"]
                    if position % 100 == 0 or limit is not None:
                        print(json.dumps({"progress": position, "total": len(records), "copied": stats["copied"], "bytes": stats["bytes"]}), flush=True)
                except Exception as error:
                    stats["errors"].append({"source": source, "relpath": relative, "error": str(error)})
    finally:
        state.close()
    return stats
