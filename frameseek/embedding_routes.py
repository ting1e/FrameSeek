from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from frameseek.engine.embedding_api import INLINE_FRAMES, MAX_API_UPLOAD
from frameseek.media.images import supported


class ServiceSettings(BaseModel):
    enabled: bool | None = None
    token: str | None = Field(default=None,max_length=512)
    generate: bool = False
    clear: bool = False


class FileEmbedding(BaseModel):
    path: str = Field(min_length=1,max_length=4000)


def install_embedding_routes(app,runtime,auth):
    service = runtime.embedding_api

    @app.get('/api/embedding-service')
    def settings(request: Request):
        auth.require(request)
        return service.public()

    @app.put('/api/embedding-service')
    def save(request: Request, values: ServiceSettings):
        auth.require(request,mutation=True)
        try:
            return service.save(**values.model_dump())
        except ValueError as error:
            raise HTTPException(422,str(error)) from None

    @app.delete('/api/embedding-service/cache')
    def clear(request: Request):
        auth.require(request,mutation=True)
        return service.clear_cache()

    def submitted(path,uploaded=False):
        try:
            result = service.submit(path,uploaded)
        except PermissionError as error:
            raise HTTPException(403,str(error)) from None
        except (ValueError,OSError) as error:
            raise HTTPException(422,str(error)) from None
        if result['status']=='failed':
            raise HTTPException(422,result['error'])
        if result['status']=='ready' and result['total_frames']<=INLINE_FRAMES:
            return JSONResponse(json.loads(''.join(service.json_result(result['id']))))
        return JSONResponse(result,status_code=202)

    @app.post('/api/v1/embeddings/file')
    def from_path(request: Request, values: FileEmbedding):
        service.require(request)
        try:
            path = service.permitted_path(values.path)
        except PermissionError as error:
            raise HTTPException(403,str(error)) from None
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        return submitted(path)

    @app.post('/api/v1/embeddings/upload')
    async def from_upload(request: Request, file: UploadFile = File(...)):
        service.require(request)
        if not supported(file.filename or ''):
            raise HTTPException(422,'请上传 BIF 或图片')
        path = service.root/'uploads'/(str(uuid.uuid4())+Path(file.filename).suffix.lower())
        size = 0
        try:
            with path.open('wb') as stream:
                while block := await file.read(1024*1024):
                    size += len(block)
                    if size>MAX_API_UPLOAD:
                        raise HTTPException(413,'上传文件最大 128 MiB')
                    stream.write(block)
            result = await run_in_threadpool(submitted,path,True)
            if result.status_code!=202:
                path.unlink(missing_ok=True)
            return result
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get('/api/v1/embeddings/jobs/{ident}')
    def job(request: Request, ident: str):
        service.require(request)
        return service.job(ident)

    @app.get('/api/v1/embeddings/jobs/{ident}/result')
    def result(request: Request, ident: str, format: str = Query('json',pattern='^(json|npz)$')):
        service.require(request)
        row = service.result_row(ident)
        if format=='npz':
            return FileResponse(service.folder(row)/'result.npz',media_type='application/octet-stream',filename='embeddings.npz')
        return StreamingResponse(service.json_result(ident),media_type='application/json')
