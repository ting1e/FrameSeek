from __future__ import annotations

from frameseek.media.scope import validate_scope, directories

import io
import mimetypes
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from frameseek.media import bif
from frameseek.core.auth import Auth
from frameseek.core.config import Settings
from frameseek.core.paths import media_path
from frameseek.engine.search import Runtime
from frameseek.core.preferences import Preferences, WebPreferences, current, requested, compose_override, DOCKER_FIELDS
from frameseek.media.directories import REGISTRY_KEY, plan_directories, monitor_directories
from frameseek.engine.monitoring import folders, sql_scope
from frameseek.integrations.emby import Emby, EmbySettings, EmbyError, EmbyNotFound
from frameseek.media.playback import local_mp4
from frameseek.media.images import validate_media_type, IMAGE_FORMATS
from frameseek.media.decode import decode_frame

Image.MAX_IMAGE_PIXELS = 20_000_000
MAX_UPLOAD = 10 * 1024 * 1024


class UploadTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, '图片最大 10MB')


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        total, started = 0, False
        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_UPLOAD + 1024 * 1024:
                raise UploadTooLarge()
            return message
        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await self.app(scope, limited_receive, tracked_send)
        except UploadTooLarge:
            if not started:
                await JSONResponse({"detail": "图片最大 10MB"}, status_code=413)(scope, receive, send)


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class WebPlayTicket(BaseModel):
    ticket: str = Field(min_length=1,max_length=100)


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = runtime or Runtime(settings)
    auth = Auth(settings, runtime.db)
    static = Path(__file__).parent / "static"
    # Windows registry associations can identify .js as text/plain. With
    # nosniff enabled, browsers then refuse to execute the login script.
    mimetypes.add_type("text/javascript", ".js")

    @asynccontextmanager
    async def lifespan(app):
        runtime.worker.start()
        yield
        await run_in_threadpool(runtime.close)

    app = FastAPI(title="FrameSeek", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    from frameseek.integrations.remote import roots
    remote_roots = roots()
    emby = Emby(runtime.db, {key:remote_roots.get(key, path.resolve().as_posix()) for key,path in settings.sources.items()})
    app.state.emby = emby
    app.add_middleware(BodyLimit)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            count = runtime.get_store().count()
            return {"status": "ready", "vectors": count}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.post("/api/login")
    def login(request: Request, values: Login):
        token = auth.login(request, values.username, values.password)
        response = JSONResponse({"username": settings.username, "csrf": auth.csrf(token)})
        response.set_cookie("imgsearch_session", token, max_age=12 * 3600, httponly=True,
                            secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        token = auth.require(request, mutation=True)
        auth.revoke(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie("imgsearch_session", path="/", secure=settings.secure_cookie,
                               httponly=True, samesite="strict")
        return response

    @app.get('/api/emby/settings')
    def emby_settings_get(request: Request):
        auth.require(request)
        return emby.public()

    @app.get('/emby/player/{frame_id}')
    @app.get('/player/local/{frame_id}')
    def emby_player(request: Request, frame_id: str):
        auth.require(request)
        return FileResponse(static / 'player.html')

    def local_video_path(row):
        try:
            return local_mp4(settings, row)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error) if isinstance(error, ValueError) else '无法读取同目录的 MP4 文件。') from None

    def local_playback(row):
        path = local_video_path(row)
        return {'local':True, 'player_url':'/player/local/'+row['id'],
                'stream_url':'/api/local/stream/'+row['id'], 'stop_url':'',
                'time_ms':row['time_ms'], 'seek_seconds':row['time_ms']/1000,
                'offset_seconds':0, 'transcoding':False, 'name':path.name}

    def playable_frame(frame_id):
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(410, '命中帧的 BIF 已修改或删除，请重新搜索。')
        return row

    @app.post('/api/local/open/{frame_id}')
    def local_open(request: Request, frame_id: str):
        auth.require(request, mutation=True)
        return local_playback(playable_frame(frame_id))

    @app.get('/api/local/stream/{frame_id}')
    def local_stream(request: Request, frame_id: str):
        auth.require(request)
        row = playable_frame(frame_id)
        return FileResponse(local_video_path(row), media_type='video/mp4')

    @app.post('/api/emby/open/{frame_id}')
    def emby_open(request: Request, frame_id: str):
        auth.require(request, mutation=True)
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(410, '命中帧的 BIF 已修改或删除，请重新搜索。')
        try:
            return emby.prepare(row)
        except EmbyNotFound:
            return local_playback(row)
        except EmbyError as error:
            raise HTTPException(409, str(error)) from None

    @app.get('/api/emby/stream/{token}')
    async def emby_stream(request: Request, token: str):
        auth.require(request)
        try:
            entry = emby.stream(token)
        except EmbyError as error:
            raise HTTPException(410, str(error)) from None
        import httpx
        config = entry['config']
        base = config.server_url
        if not base.lower().endswith('/emby'):
            base += '/emby'
        headers = {'X-Emby-Token':config.api_key,'Accept-Encoding':'identity'}
        if entry['direct'] and request.headers.get('range'):
            headers['Range'] = request.headers['range']
        client = httpx.AsyncClient(timeout=httpx.Timeout(120,connect=30),follow_redirects=False)
        try:
            upstream = await client.send(client.build_request('GET',base+entry['route'],params=entry['params'],headers=headers),stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502,'Emby 视频流连接失败，请核对连接和转码权限。') from None
        if upstream.status_code not in {200,206,416}:
            await upstream.aclose(); await client.aclose()
            raise HTTPException(502, 'Emby 无法提供视频流，请核对播放权限和转码配置。')
        async def chunks():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose(); await client.aclose()
        forwarded = {key:value for key,value in upstream.headers.items() if key.lower() in {'content-type','content-length','content-range','accept-ranges'}}
        return StreamingResponse(chunks(),status_code=upstream.status_code,headers=forwarded)

    @app.post('/api/emby/stop/{token}')
    def emby_stop(request: Request, token: str):
        auth.require(request, mutation=True)
        try:
            emby.stop(token)
            return {'ok':True}
        except EmbyError as error:
            raise HTTPException(502, str(error)) from None

    @app.put('/api/emby/settings')
    def emby_settings_put(request: Request, values: EmbySettings):
        auth.require(request, mutation=True)
        if set(values.source_paths) - set(settings.sources):
            raise HTTPException(422, '未知的视频来源映射')
        try:
            return emby.save(values)
        except EmbyError as error:
            raise HTTPException(422, str(error)) from None

    @app.get('/api/emby/clients')
    def emby_clients(request: Request):
        auth.require(request)
        try:
            return emby.clients()
        except EmbyError as error:
            raise HTTPException(502, str(error)) from None

    @app.post('/api/emby/play/{frame_id}')
    def emby_play(request: Request, frame_id: str):
        auth.require(request, mutation=True)
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(410, '命中帧的 BIF 已修改或删除，请重新搜索。')
        try:
            return emby.play(row)
        except EmbyNotFound:
            return local_playback(row)
        except EmbyError as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/emby/web-begin/{frame_id}')
    def emby_web_begin(request: Request, frame_id: str):
        auth.require(request, mutation=True)
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(410, '命中帧的 BIF 已修改或删除，请重新搜索。')
        try:
            emby.resolve(row)
            return emby.web_begin(row)
        except EmbyNotFound:
            return local_playback(row)
        except EmbyError as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/emby/web-play/{frame_id}')
    def emby_web_play(request: Request, frame_id: str, values: WebPlayTicket | None = None):
        auth.require(request, mutation=True)
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(410, '命中帧的 BIF 已修改或删除，请重新搜索。')
        try:
            return emby.web_poll(row, values.ticket) if values else emby.play(row, web_only=True)
        except EmbyNotFound:
            return local_playback(row)
        except EmbyError as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/emby/refresh')
    def emby_refresh(request: Request):
        auth.require(request, mutation=True)
        with emby.lock:
            runtime.db.execute("DELETE FROM meta WHERE key='emby_items_updated'")
        try:
            emby.refresh(emby.config())
            return {'message':'Emby 视频路径映射已刷新。'}
        except EmbyError as error:
            raise HTTPException(502, str(error)) from None

    @app.get('/api/settings')
    def settings_get(request: Request):
        auth.require(request)
        desired = requested(settings, runtime.db)
        active = current(settings)
        from frameseek.engine.inference import inference_options
        return {'saved': desired, 'running': active,
                'monitor_directories': monitor_directories(settings, desired['monitor_folders']),
                'source_directories': [{'source': source, 'root': root.resolve().as_posix(), 'nas_root': remote_roots.get(source)} for source, root in settings.sources.items()],
                'restart_required': any(desired[key] != active[key] for key in desired if key not in {'default_top', 'collapse_results'}),
                'device': settings.device, 'precision': settings.precision,
                'inference_options': inference_options(settings), 'background_files_concurrency': 1,
                'memory_limit_note': '内存上限仅在 Docker 中生效，保存后需导出配置并重建容器；本地直接运行的应用无此限制。'}

    @app.put('/api/settings')
    def settings_put(request: Request, values: WebPreferences):
        auth.require(request, mutation=True)
        previous = requested(settings, runtime.db)
        # Older clients must not reset a saved inference choice when editing unrelated settings.
        values = values.model_copy(update={name: previous[name] for name in ('device', 'precision') if name not in values.model_fields_set})
        directories = values.monitor_directories
        values = Preferences.model_validate(values.model_dump(exclude={'monitor_directories'}))
        new_sources, registry = settings.sources, None
        if directories is not None:
            try:
                new_sources, selected, registry = plan_directories(settings, runtime.db, directories, remote_roots)
                values = Preferences.model_validate({**values.model_dump(), 'monitor_folders':selected})
            except (ValueError, OSError) as error:
                raise HTTPException(422, str(error)) from None
        # Keep accepting legacy profiles, but container resources are owned by Compose.
        values = values.model_copy(update={name: current(settings)[name] for name in DOCKER_FIELDS})
        if (values.device, values.precision) != (previous['device'], previous['precision']):
            from frameseek.engine.inference import validate_inference_choice
            try:
                validate_inference_choice(settings, values.device)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        for folder in values.monitor_folders:
            if folder.source not in new_sources:
                raise HTTPException(422, '未知的视频来源：' + folder.source)
            if folder.path:
                try:
                    media_path(new_sources[folder.source], folder.path, settings.escaped_paths)
                except ValueError as error:
                    raise HTTPException(422, '监控目录不能超出视频来源范围') from error
        with runtime.db.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (values.model_dump_json(),))
            if registry is not None:
                connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (REGISTRY_KEY, json.dumps(registry, ensure_ascii=False)))
        roots_changed = settings.sources != new_sources
        settings.sources = new_sources
        emby.media_roots = {key:remote_roots.get(key, path.resolve().as_posix()) for key,path in new_sources.items()}
        previous_scan = (settings.auto_update, settings.interval, settings.stable_seconds, settings.monitor_folders)
        settings.auto_update = values.auto_update
        settings.interval = values.scan_interval_seconds
        settings.stable_seconds = values.stable_seconds
        settings.default_top = values.default_top
        settings.collapse_results = values.collapse_results
        settings.monitor_folders = [folder.model_dump() for folder in values.monitor_folders]
        if roots_changed or previous_scan != (settings.auto_update, settings.interval, settings.stable_seconds, settings.monitor_folders):
            runtime.worker.configuration_changed()
        return {'ok': True, 'restart_required': any(values.model_dump()[key] != current(settings)[key]
                for key in values.model_dump() if key not in {'default_top', 'collapse_results'}),
                'source_directories': [{'source':key,'root':path.resolve().as_posix(),'nas_root':remote_roots.get(key)} for key,path in new_sources.items()],
                'message': '配置已保存。自动检查、扫描间隔和稳定等待已生效；监控范围在下一轮扫描生效。推理设备、精度、线程、处理批次和保存进度设置需重启应用。'}

    @app.get('/api/settings/compose')
    def settings_compose(request: Request):
        auth.require(request)
        return Response(compose_override(requested(settings, runtime.db)), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="compose.settings.yml"'})

    @app.get("/api/status")
    def status(request: Request):
        token = auth.require(request)
        try:
            manifest = settings.manifest
            model_ready = True
        except Exception:
            manifest, model_ready = {}, False
        paused = runtime.db.one("SELECT value FROM meta WHERE key='paused'")
        stats = runtime.db.stats()
        condition, arguments = sql_scope(folders(settings, runtime.db))
        stats['pending'] = runtime.db.one(f"SELECT COUNT(*) AS n FROM versions v JOIN files f ON f.desired_version=v.id WHERE v.status IN ('pending','processing','failed') AND {condition}", arguments)['n']
        tasks = runtime.db.one(f"""SELECT
            SUM(CASE WHEN f.status='observed' AND f.error IS NULL THEN 1 ELSE 0 END) AS stabilizing,
            SUM(CASE WHEN v.status='pending' THEN 1 ELSE 0 END) AS queued,
            SUM(CASE WHEN v.status='processing' THEN 1 ELSE 0 END) AS processing,
            SUM(CASE WHEN v.status='failed' OR (f.status='observed' AND f.error IS NOT NULL) THEN 1 ELSE 0 END) AS failed
            FROM files f LEFT JOIN versions v ON v.id=f.desired_version
            WHERE f.status!='deleted' AND {condition}""", arguments)
        current_task = None
        if runtime.worker.activity.startswith('indexing:'):
            current_task = runtime.db.one("""SELECT f.source,f.relpath,v.cursor,v.total FROM files f
                JOIN versions v ON v.id=f.desired_version WHERE f.id=?""", (runtime.worker.activity.split(':', 1)[1],))
        totals = runtime.db.one(f"""SELECT COUNT(v.id) AS version_count, COALESCE(SUM(v.created),0) AS generation,
            COALESCE(SUM(v.cursor),0) AS completed,
            COALESCE(SUM(CASE WHEN v.status IN ('pending','processing','failed') THEN MAX(0,v.total-v.cursor) ELSE 0 END),0) AS remaining
            FROM files f JOIN versions v ON v.id=f.desired_version WHERE f.status!='deleted' AND {condition}""", arguments)
        is_paused = bool(paused and paused['value']=='true')
        progress = runtime.progress.update(totals['completed'], totals['remaining'],
            (condition, tuple(arguments), totals['version_count'], totals['generation']),
            active=bool(totals['remaining'] and (settings.auto_update or runtime.worker.manual_active or tasks['processing'])),
            paused=is_paused, blocked=bool(tasks['failed'] or tasks['stabilizing']))
        if current_task and current_task["source"] in settings.sources:
            current_task["root_directory"] = settings.sources[current_task["source"]].resolve().as_posix()
        last_scan = runtime.db.one("SELECT value FROM meta WHERE key='last_scan'")
        return {**stats, "mode": settings.mode, "model": "DINOv3 ViT-L/16",
                "model_ready": model_ready, "fingerprint": manifest.get("fingerprint"),
                "activity": runtime.worker.activity, "worker_error": runtime.worker.error,
                "auto_update": settings.auto_update, "paused": bool(paused and paused["value"] == "true"),
                "realtime_monitoring": runtime.worker.watcher.active, "watcher_error": runtime.worker.watcher.error,
                "csrf": auth.csrf(token), "sources": list(settings.sources),
                "tasks": {key: value or 0 for key, value in tasks.items()}, "current_task": current_task, "progress":progress,
                "monitor_directories": [settings.sources[item['source']].resolve().as_posix() + ('/' + item['path'] if item['path'] else '') for item in folders(settings, runtime.db) if item['source'] in settings.sources],
                "last_scan": float(last_scan['value']) if last_scan else None,
                "scan_interval_seconds": settings.interval, "stable_seconds": settings.stable_seconds,
                "scan_requested": runtime.worker.scan_event.is_set(), "manual_active": runtime.worker.manual_active}

    @app.get('/api/search/directories')
    def search_directories(request: Request, source: str = '', q: str = Query('', max_length=256), media_type: str = 'all'):
        auth.require(request)
        try:
            source, _ = validate_scope(settings.sources, source)
            validate_media_type(media_type)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return directories(runtime.db, source, q.strip(), {key:path.resolve().as_posix() for key,path in settings.sources.items()}, media_type)

    @app.post("/api/search")
    async def search(request: Request, image: UploadFile = File(...),
                     top: int = Form(20), collapse: bool = Form(True), source: str = Form(''), directory: str = Form(''), directory_keyword: str = Form('', max_length=256), media_type: str = Form('all')):
        auth.require(request, mutation=True)
        try:
            source, directory = validate_scope(settings.sources, source, directory)
            validate_media_type(media_type)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if top not in {20, 50, 100, 200, 500}:
            raise HTTPException(422, "结果数量应为 20、50、100、200 或 500")
        if not runtime.query_slots.acquire(blocking=False):
            raise HTTPException(429, "搜索队列已满，请稍后重试")
        decoded = None
        try:
            data = await image.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "图片最大 10MB")
            try:
                with Image.open(io.BytesIO(data)) as opened:
                    if opened.format not in IMAGE_FORMATS:
                        raise ValueError("Unsupported format")
                    if opened.width * opened.height > 20_000_000:
                        raise ValueError("Image exceeds 20 million pixels")
                    opened.load()
                    decoded = opened.copy()
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
                raise HTTPException(422, "请上传有效 JPG、PNG、WebP、BMP、GIF 或 TIFF 图片（最多 2000 万像素）") from error
            try:
                result = await run_in_threadpool(runtime.query, decoded, top, collapse, False, source, directory, directory_keyword, media_type)
            except Exception as error:
                runtime.db.event("search_error", str(error))
                raise HTTPException(503, "模型或索引暂未就绪，请查看任务状态") from error
            history_id = str(uuid.uuid4())
            def save_history():
                with decoded.convert('RGB') as thumbnail:
                    thumbnail.thumbnail((960, 960))
                    buffer = io.BytesIO()
                    thumbnail.save(buffer, format='JPEG', quality=85)
                snapshot = {**result, 'top': top, 'collapse': collapse}
                runtime.db.save_search(history_id, image.filename or '粘贴截图', buffer.getvalue(), snapshot)
            try:
                await run_in_threadpool(save_history)
                result['history_id'] = history_id
            except Exception as error:
                runtime.db.event('history_error', str(error))
                result['history_saved'] = False
            return result
        finally:
            if decoded:
                decoded.close()
            await image.close()
            runtime.query_slots.release()

    @app.get('/api/history')
    def history_list(request: Request, offset: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100)):
        auth.require(request)
        rows = runtime.db.rows('SELECT id,created,filename,response FROM search_history ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', (limit, offset))
        total = runtime.db.one('SELECT COUNT(*) AS count FROM search_history')['count']
        return {'total': total, 'has_more': offset + len(rows) < total, 'items': [{'id': row['id'], 'created': row['created'], 'filename': row['filename'],
                           'returned': json.loads(row['response'])['returned'],
                           'thumbnail_url': f"/api/history/{row['id']}/image"} for row in rows]}

    @app.get('/api/history/{history_id}/image')
    def history_image(request: Request, history_id: str):
        auth.require(request)
        with runtime.db.connect() as c:
            row = c.execute('SELECT thumbnail FROM search_history WHERE id=?', (history_id,)).fetchone()
        if not row:
            raise HTTPException(404, '历史记录不存在')
        return Response(bytes(row['thumbnail']), media_type='image/jpeg')

    @app.get('/api/history/{history_id}')
    def history_detail(request: Request, history_id: str):
        auth.require(request)
        row = runtime.db.one('SELECT id,created,filename,response FROM search_history WHERE id=?', (history_id,))
        if not row:
            raise HTTPException(404, '历史记录不存在')
        response = json.loads(row['response'])
        response.setdefault('source', '')
        response.setdefault('directory', '')
        response.setdefault('directory_keyword', '')
        response.setdefault('media_type', 'all')
        runtime.db.add_durations(response['results'])
        for result in response['results']:
            if result.get('source') in settings.sources:
                result['root_directory'] = settings.sources[result['source']].resolve().as_posix()
        return {**row, 'response': response, 'thumbnail_url': f'/api/history/{history_id}/image'}

    @app.delete('/api/history/{history_id}')
    def history_delete(request: Request, history_id: str):
        auth.require(request, mutation=True)
        runtime.db.execute('DELETE FROM search_history WHERE id=?', (history_id,))
        return {'ok': True}

    @app.get("/api/frames/{frame_id}")
    def frame(request: Request, frame_id: str):
        auth.require(request)
        row = runtime.db.published_frame(frame_id)
        if not row:
            raise HTTPException(404, "帧不存在或版本已失效")
        if not runtime.available(row):
            raise HTTPException(410, "源文件已改变，等待重新索引")
        path = media_path(settings.sources[row["source"]], row["relpath"], settings.escaped_paths)
        try:
            if row['media_type'] == 'image':
                _, decoded, error = decode_frame(path, row)
                if decoded is None:
                    raise HTTPException(410, '无法读取源图片')
                try:
                    decoded.thumbnail((1600, 1600))
                    buffer = io.BytesIO()
                    decoded.save(buffer, format='JPEG', quality=90)
                    data = buffer.getvalue()
                finally:
                    decoded.close()
            else:
                data = bif.read_frame(path, row["offset"], row["length"])
            if not runtime.available(row):
                raise HTTPException(410, "读取期间源文件发生变化")
            return Response(data, media_type="image/jpeg")
        except (OSError, bif.BifError):
            raise HTTPException(410, "无法读取源帧")

    @app.get("/api/frames/{frame_id}/neighbors")
    def neighbors(request: Request, frame_id: str):
        auth.require(request)
        row = runtime.db.published_frame(frame_id)
        if not row or not runtime.available(row):
            raise HTTPException(404, "帧不存在或版本已失效")
        rows = runtime.db.rows("SELECT id,frame_no,time_ms FROM frames WHERE version=? AND valid=1 AND frame_no BETWEEN ? AND ? ORDER BY frame_no",
                               (row["version"], max(0, row["frame_no"] - 5), row["frame_no"] + 5))
        return {"source": row["source"], "media_type":row['media_type'], "root_directory": settings.sources[row["source"]].resolve().as_posix(), "relpath": row["relpath"], "frames": rows}

    @app.post("/api/updates/scan")
    def scan(request: Request):
        auth.require(request, mutation=True)
        paused = runtime.db.one("SELECT value FROM meta WHERE key='paused'")
        if paused and paused['value'] == 'true':
            raise HTTPException(409, '后台任务已暂停，请先恢复再检查文件变动。')
        if not folders(settings, runtime.db):
            raise HTTPException(409, '尚未设置监控目录，请先到设置页填写目录。')
        already_requested = runtime.worker.scan_event.is_set()
        runtime.worker.scan_event.set()
        return {"queued": True, "already_requested": already_requested,
                "message": '已有检查请求等待执行。' if already_requested else '检查请求已提交，将扫描监控目录并处理新增或修改的 BIF 和图片。'}

    @app.post("/api/updates/{action}")
    def pause(request: Request, action: str):
        auth.require(request, mutation=True)
        if action not in {"pause", "resume"}:
            raise HTTPException(404, "Unknown action")
        runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('paused',?)", ("true" if action == "pause" else "false",))
        if action == 'resume' and not settings.auto_update and folders(settings, runtime.db):
            runtime.worker.scan_event.set()
        return {"paused": action == "pause", "message": '暂停请求已提交，当前处理分块完成后暂停；已完成的画面仍可搜索。' if action == 'pause' else ('已解除暂停，后台将继续检查和处理监控目录。' if folders(settings, runtime.db) else '已解除暂停。尚未设置监控目录，请到设置页填写。')}

    return app
