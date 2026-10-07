from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv


def init_environment(destination: Path):
    from .auth import password_hash
    if destination.exists():
        raise RuntimeError("Environment file already exists; refusing to overwrite credentials")
    password = secrets.token_urlsafe(18)
    text = "\n".join([
        "IMGS_USERNAME=admin", "IMGS_PASSWORD_HASH=" + password_hash(password),
        "IMGS_SESSION_SECRET=" + secrets.token_hex(32),
        "IMGS_QDRANT_KEY=" + secrets.token_hex(32),
        "IMGS_QDRANT_URL=http://127.0.0.1:16333",
        "IMGS_SECURE_COOKIE=false", "IMGS_DEVICE=cuda",
        "",
    ])
    destination.write_text(text, encoding="utf-8")
    os.chmod(destination, 0o600)
    credentials = destination.parent / "credentials.txt"
    credentials.write_text(f"用户名: admin\n密码: {password}\n本地地址: http://127.0.0.1:18443\n", encoding="utf-8")
    os.chmod(credentials, 0o600)
    print("Credentials saved to", credentials.resolve(), "(not printed)")


def main():
    from .console import configure_console
    configure_console()
    load_dotenv(Path.cwd() / '.env', override=False)
    parser = argparse.ArgumentParser(prog="frameseek")
    sub = parser.add_subparsers(dest="command", required=True)
    initial = sub.add_parser("init")
    initial.add_argument("--env-file", default=".env")
    sub.add_parser('bootstrap', help='Initialize persistent Docker login and model metadata')
    prepare = sub.add_parser("prepare-model")
    prepare.add_argument("--revision", default="main")
    prepare.add_argument("--existing", action="store_true", help="Validate an existing official Hugging Face model directory")
    inventory = sub.add_parser("inventory")
    inventory.add_argument("--output", default="reports/nas-inventory.jsonl")
    sync = sub.add_parser("sync")
    sync.add_argument("--manifest", default="reports/nas-inventory.jsonl")
    sync.add_argument("--destination", default="bif")
    sync.add_argument("--limit-files", type=int)
    scan = sub.add_parser("scan")
    scan.add_argument("--trust-stable", action="store_true", help="Only for finished, verified local copies")
    index = sub.add_parser("index")
    index.add_argument("--max-frames", type=int)
    index.add_argument("--source", help="Process only this source ID (for balanced preflight samples)")
    index.add_argument("--retry", action="store_true")
    index.add_argument("--verify-only", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18443)
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    config_export = sub.add_parser('export-config')
    config_export.add_argument('--output', default='compose.settings.yml')
    backup = sub.add_parser("backup")
    backup.add_argument("--destination", default="data/backups")
    args = parser.parse_args()
    if args.command == 'bootstrap':
        from .bootstrap import initialize
        initialize(Path(os.getenv('IMGS_DATA', '/data')), Path(os.getenv('IMGS_MODEL', '/models/dinov3')),
                   os.getenv('IMGS_USERNAME', 'admin'),
                   {key:os.getenv(key, '') for key in ('IMGS_PASSWORD_HASH','IMGS_SESSION_SECRET','IMGS_QDRANT_KEY')})
        return
    if args.command == "init":
        init_environment(Path(args.env_file))
        return
    from .config import Settings
    if os.getenv('IMGS_AUTH_FILE'):
        load_dotenv(os.environ['IMGS_AUTH_FILE'], override=False)
    settings = Settings()
    if args.command == "prepare-model":
        from .model import create_manifest, prepare_model
        result = create_manifest(settings.model) if args.existing else prepare_model(settings.model, args.revision)
        print(json.dumps(result, indent=2))
        return
    if args.command in {"inventory", "sync"}:
        from .sync import connect, inventory, sync
        with connect() as connection:
            result = inventory(connection, Path(args.output)) if args.command == "inventory" else sync(
                connection, Path(args.manifest), Path(args.destination), settings.escaped_paths, args.limit_files)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("errors"):
            raise SystemExit(2)
        return
    from .search import Runtime
    runtime = Runtime(settings)
    try:
        if args.command == "serve":
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(settings, runtime), host=args.host, port=args.port,
                        proxy_headers=True, forwarded_allow_ips=os.getenv("IMGS_PROXY_IPS", "127.0.0.1"))
        elif args.command == "status":
            print(json.dumps({**runtime.db.stats(), 'scope': runtime.get_indexer().scope_stats()}, ensure_ascii=False, indent=2))
        elif args.command == 'export-config':
            from .preferences import requested, compose_override
            Path(args.output).write_text(compose_override(requested(settings, runtime.db)), encoding='utf-8')
            print('Deployment settings exported to', Path(args.output).resolve())
        elif args.command in {"pause", "resume"}:
            runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('paused',?)", ("true" if args.command == "pause" else "false",))
            print(args.command)
        elif args.command == "scan":
            print(json.dumps(runtime.get_indexer().scan(args.trust_stable), ensure_ascii=False))
        elif args.command == "index":
            if args.source and args.source not in settings.sources:
                raise ValueError('Unknown source ID')
            if args.max_frames is not None and args.max_frames < 1:
                raise ValueError('--max-frames must be positive')
            indexer = runtime.get_indexer()
            if args.retry:
                runtime.db.execute("UPDATE versions SET retry_at=0 WHERE status='failed'")
            if args.verify_only:
                from .model import digest
                chunks = runtime.db.rows("SELECT * FROM chunks")
                for chunk in chunks:
                    if digest(settings.data / chunk["path"]) != chunk["sha256"]:
                        raise RuntimeError("Corrupt vector block: " + chunk["path"])
                print(json.dumps({"verified_chunks": len(chunks), **runtime.db.stats(),
                                  'scope': indexer.scope_stats()}, ensure_ascii=False))
                return
            processed = 0
            failures = 0
            started = time.perf_counter()
            while jobs := indexer.pending(args.source):
                paused = runtime.db.one("SELECT value FROM meta WHERE key='paused'")
                if paused and paused['value'] == 'true':
                    print('Indexing paused; run frameseek resume to continue')
                    break
                for job in jobs:
                    remaining = args.max_frames - processed if args.max_frames is not None else None
                    if remaining is not None and remaining <= 0:
                        break
                    try:
                        processed += indexer.process_version(job["id"], max_frames=remaining)
                    except Exception as error:
                        failures += 1
                        print(json.dumps({"file_id": job["file_id"], "error": str(error)}), flush=True)
                    elapsed = time.perf_counter() - started
                    print(json.dumps({"processed_frames": processed, "file_id": job["file_id"],
                                      "elapsed_seconds": round(elapsed, 2),
                                      "frames_per_second": round(processed / max(elapsed, 0.001), 2)}), flush=True)
                if args.max_frames is not None and processed >= args.max_frames:
                    break
            indexer.cleanup()
            while runtime.db.one('SELECT version FROM deletions LIMIT 1'):
                indexer.cleanup()
            print(json.dumps({**runtime.db.stats(), 'scope': indexer.scope_stats()}, ensure_ascii=False, indent=2))
            if failures:
                raise SystemExit(2)
        elif args.command == "backup":
            folder = Path(args.destination) / time.strftime("%Y%m%d-%H%M%S")
            folder.mkdir(parents=True)
            indexer = runtime.get_indexer()
            with indexer.operation_lock:
                runtime.db.backup(folder / "metadata.sqlite3")
                latest = runtime.get_store().create_snapshot()
                import httpx
                store = runtime.get_store()
                url = settings.qdrant_url.rstrip("/") + f"/collections/{store.collection}/snapshots/{latest.name}"
                with httpx.stream("GET", url, headers={"api-key": settings.qdrant_key or ""}, timeout=3600) as response:
                    response.raise_for_status()
                    with (folder / "qdrant.snapshot").open("wb") as out:
                        for block in response.iter_bytes(1024 * 1024):
                            out.write(block)
                (folder / "release.json").write_text(json.dumps({"model": settings.manifest, "collection": store.collection,
                    "qdrant_version": "1.19.2", "stats": runtime.db.stats(), "snapshot": latest.name}, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Backup saved to", folder.resolve())
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
