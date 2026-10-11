"""Token-authenticated exports, isolated from the searchable index."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import struct
import threading
import time
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
from fastapi import HTTPException

from frameseek.core.config import DIMENSION, MODEL_ID
from frameseek.core.preferences import requested
from frameseek.engine.model import digest
from frameseek.media import bif
from frameseek.media.decode import decoded_batches
from frameseek.media.directories import contains
from frameseek.media.images import media_type, supported, MAX_IMAGE_BYTES

MAX_API_UPLOAD = 128 * 1024 * 1024
INLINE_FRAMES = 8


class EmbeddingService:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db
        self.root = runtime.settings.data / 'api_embeddings'
        self.root.mkdir(exist_ok=True)
        (self.root/'uploads').mkdir(exist_ok=True)
        (self.root/'cache').mkdir(exist_ok=True)
        self.executor, self.operation = threading.Lock(), threading.RLock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.db.execute("UPDATE embedding_jobs SET status='pending' WHERE status='running'")

    def config(self):
        row = self.db.one("SELECT value FROM meta WHERE key='embedding_api'")
        return json.loads(row['value']) if row else {'enabled':False,'token_hash':''}

    def public(self):
        value = self.config()
        stats = self.db.one("SELECT COUNT(*) AS jobs,SUM(status IN ('pending','running')) AS queued FROM embedding_jobs")
        return {'enabled':value['enabled'],'token_set':bool(value['token_hash']),
                'jobs':stats['jobs'],'queued':stats['queued'] or 0,
                'upload_limit_mib':128,'inline_frames':INLINE_FRAMES}

    def save(self, enabled=None, token=None, generate=False, clear=False):
        with self.operation:
            value = self.config()
            issued = secrets.token_urlsafe(32) if generate else token
            if issued is not None:
                if len(issued) < 32 or len(issued) > 512 or not issued.isascii() or any(c.isspace() or ord(c)<33 for c in issued):
                    raise ValueError('Token 使用至少 32 位不含空白的英文字符')
                value['token_hash'] = hashlib.sha256(issued.encode()).hexdigest()
            if clear:
                value['token_hash'] = ''
                value['enabled'] = False
            if enabled is not None:
                value['enabled'] = enabled
            if value['enabled'] and not value['token_hash']:
                raise ValueError('请先配置 Token')
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('embedding_api',?)",(json.dumps(value),))
        self.wake.set()
        return {**self.public(), **({'token':issued} if generate else {})}

    def require(self, request):
        value = self.config()
        if not value['enabled']:
            raise HTTPException(503,'Embedding API 未开启')
        scheme, _, token = request.headers.get('authorization','').partition(' ')
        if scheme.lower() != 'bearer' or not token or not hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(),value['token_hash']):
            raise HTTPException(401,'Token 无效',headers={'WWW-Authenticate':'Bearer'})

    def permitted_path(self, value):
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError('请填写媒体文件的完整路径')
        path = path.resolve()
        if not any(path.is_relative_to(root.resolve()) for root in self.runtime.settings.sources.values()):
            raise PermissionError('文件不在已配置的媒体目录内')
        if any(contains(excluded,path.as_posix()) for excluded in requested(self.runtime.settings,self.db)['excluded_directories']):
            raise PermissionError('文件位于排除目录内')
        return path

    def frames(self, path, kind):
        if kind == 'bif':
            with path.open('rb') as stream:
                header = stream.read(16)
            if len(header)==16 and struct.unpack_from('<I',header,12)[0]>100000:
                raise ValueError('BIF 最多包含 100000 帧')
            values = [asdict(frame) for frame in bif.parse(path)]
            if not values or len(values)>100000:
                raise ValueError('BIF 需要包含 1～100000 帧')
            return [dict(frame_no=v.pop('index'),media_type='bif',**v) for v in values]
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError('图片最大 64 MiB')
        return [{'frame_no':0,'time_ms':0,'offset':0,'length':path.stat().st_size,'media_type':'image'}]

    def submit(self, path, uploaded=False):
        if not supported(path):
            raise ValueError('支持 BIF、JPG、PNG、WebP、BMP、GIF 和 TIFF')
        kind = media_type(path)
        frames = self.frames(path,kind)
        sha = digest(path)
        key = hashlib.sha256((self.runtime.settings.manifest['fingerprint']+sha).encode()).hexdigest()
        ident = str(uuid.uuid4())
        with self.operation:
            if self.db.one("SELECT COUNT(*) AS n FROM embedding_jobs WHERE status IN ('pending','running')")['n']>=100:
                raise HTTPException(429,'任务队列已满，请稍后再试')
            self.db.execute('INSERT INTO embedding_jobs(id,path,uploaded,sha256,cache_key,kind,total,created) VALUES(?,?,?,?,?,?,?,?)',
                            (ident,str(path),int(uploaded),sha,key,kind,len(frames),time.time()))
            folder = self.root/'cache'/key
            if (folder/'metadata.json').exists() and (folder/'result.npz').exists():
                self.db.execute("UPDATE embedding_jobs SET status='ready',cursor=total,cache_hit=1 WHERE id=?",(ident,))
                if uploaded:
                    path.unlink(missing_ok=True)
                return self.job(ident)
        if len(frames)<=INLINE_FRAMES and self.executor.acquire(blocking=False):
            try:
                self.process(ident)
            finally:
                self.executor.release()
        self.wake.set()
        return self.job(ident)

    def job(self, ident):
        row = self.db.one('SELECT * FROM embedding_jobs WHERE id=?',(ident,))
        if not row:
            raise HTTPException(404,'任务不存在')
        result = {'id':row['id'],'status':row['status'],'total_frames':row['total'],
                  'completed_frames':row['cursor'],'cache_hit':bool(row['cache_hit']),'error':row['error']}
        if row['status']=='ready':
            result.update(self.metadata(row))
            result['result_url'] = f"/api/v1/embeddings/jobs/{ident}/result"
            result['npz_url'] = result['result_url']+'?format=npz'
        return result

    def folder(self, row):
        return self.root/'cache'/row['cache_key']

    def metadata(self,row):
        return json.loads((self.folder(row)/'metadata.json').read_text(encoding='utf-8'))

    def write_chunk(self, folder, start, frames, vectors):
        folder.mkdir(exist_ok=True)
        path = folder/f'{start:08d}.npz'
        temp = path.with_suffix('.tmp')
        with temp.open('wb') as stream:
            np.savez(stream,frame_no=np.array([f['frame_no'] for f in frames],np.int32),
                     time_ms=np.array([f['time_ms'] for f in frames],np.int64),vectors=np.asarray(vectors,np.float32).reshape(-1,DIMENSION))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)

    def reuse_index(self,row,frames,folder):
        bound = self.db.one("SELECT value FROM meta WHERE key='model_fingerprint'")
        if not bound or bound['value'] != self.runtime.settings.manifest['fingerprint']:
            return False
        versions = self.db.rows("""SELECT v.id FROM versions v JOIN files f ON f.id=v.file_id
            WHERE v.sha256=? AND v.total=? AND v.status='ready'
            ORDER BY (v.id=f.active_version) DESC LIMIT 5""",(row['sha256'],row['total']))
        for version in versions:
            chunks = self.db.rows('SELECT * FROM chunks WHERE version=? ORDER BY start',(version['id'],))
            cursor, skipped = 0, []
            try:
                for chunk in chunks:
                    if chunk['start']!=cursor:
                        raise ValueError('Incomplete cached chunks')
                    path = self.runtime.settings.data/chunk['path']
                    if digest(path)!=chunk['sha256']:
                        raise ValueError('Cached chunk checksum mismatch')
                    with np.load(path,allow_pickle=False) as saved:
                        numbers, vectors = saved['frames'],saved['vectors']
                        if vectors.shape!=(len(numbers),DIMENSION) or not np.isfinite(vectors).all():
                            raise ValueError('Invalid cached vectors')
                        good = [frames[int(number)] for number in numbers]
                        if any(not chunk['start']<=f['frame_no']<chunk['end'] for f in good):
                            raise ValueError('Cached frame mapping mismatch')
                        self.write_chunk(folder,cursor,good,vectors)
                        existing = set(int(n) for n in numbers)
                        skipped.extend({'frame_no':n,'error':'原索引记录为损坏帧'} for n in range(chunk['start'],chunk['end']) if n not in existing)
                    cursor = chunk['end']
                if cursor!=len(frames):
                    raise ValueError('Incomplete index version')
                self.db.execute("UPDATE embedding_jobs SET cursor=total,cache_hit=1,details=? WHERE id=?",
                                (json.dumps({'skipped_frames':len(skipped),'bad_frames':skipped[:100]},ensure_ascii=False),row['id']))
                return True
            except (OSError,ValueError,KeyError,IndexError):
                for path in folder.glob('*.npz'):
                    path.unlink(missing_ok=True)
                # Snapshot-only deployments may have the vectors in Qdrant
                # without the original NPZ archives.
                try:
                    self.reuse_qdrant(row,version['id'],frames,folder)
                    return True
                except Exception:
                    for path in folder.glob('*.npz'):
                        path.unlink(missing_ok=True)
        return False

    def reuse_qdrant(self,row,version,frames,folder):
        store = self.runtime.get_store()
        skipped, errors = 0, []
        for start in range(0,len(frames),128):
            end = min(len(frames),start+128)
            records = self.db.rows('SELECT id,frame_no,valid,error FROM frames WHERE version=? AND frame_no>=? AND frame_no<? ORDER BY frame_no',(version,start,end))
            if len(records)!=end-start:
                raise ValueError('Incomplete stored frame mapping')
            valid = [record for record in records if record['valid']]
            points = store.client.retrieve(store.collection,ids=[record['id'] for record in valid],with_vectors=True,with_payload=False) if valid else []
            mapping = {str(point.id):point.vector for point in points}
            if len(mapping)!=len(valid):
                raise ValueError('Incomplete stored vectors')
            vectors = np.asarray([mapping[record['id']] for record in valid],np.float32).reshape(-1,DIMENSION)
            if not np.isfinite(vectors).all():
                raise ValueError('Invalid stored vectors')
            self.write_chunk(folder,start,[frames[record['frame_no']] for record in valid],vectors)
            bad = [record for record in records if not record['valid']]
            skipped += len(bad)
            errors.extend({'frame_no':record['frame_no'],'error':record['error'] or '损坏帧'} for record in bad[:max(0,100-len(errors))])
        self.db.execute('UPDATE embedding_jobs SET cursor=total,cache_hit=1,details=? WHERE id=?',
                        (json.dumps({'skipped_frames':skipped,'bad_frames':errors},ensure_ascii=False),row['id']))

    def process(self,ident):
        row = self.db.one('SELECT * FROM embedding_jobs WHERE id=?',(ident,))
        if not row or row['status'] not in {'pending','running'} or not self.config()['enabled']:
            return
        try:
            with self.operation:
                self.db.execute("UPDATE embedding_jobs SET status='running',error=NULL WHERE id=?",(ident,))
                folder = self.folder(row); folder.mkdir(exist_ok=True)
                if (folder/'metadata.json').exists() and (folder/'result.npz').exists():
                    self.db.execute("UPDATE embedding_jobs SET status='ready',cursor=total,cache_hit=1 WHERE id=?",(ident,))
                    return
                path = Path(row['path'])
                if not row['uploaded']:
                    self.permitted_path(str(path))
                if digest(path)!=row['sha256']:
                    raise ValueError('文件已变动，请重新提交')
                frames = self.frames(path,row['kind'])
                if not row['cursor']:
                    for old in folder.glob('*.npz'):
                        old.unlink(missing_ok=True)
                    self.reuse_index(row,frames,folder)
            row = self.db.one('SELECT * FROM embedding_jobs WHERE id=?',(ident,))
            details = json.loads(row['details'])
            cursor = row['cursor']
            while cursor<len(frames):
                with self.operation:
                    live = self.db.one('SELECT status FROM embedding_jobs WHERE id=?',(ident,))
                    if not live or live['status']=='cancelled':
                        return
                    if self.stop_event.is_set() or not self.config()['enabled']:
                        self.db.execute("UPDATE embedding_jobs SET status='pending' WHERE id=?",(ident,))
                        return
                    if not row['uploaded']:
                        self.permitted_path(str(path))
                    end = min(len(frames),cursor+min(16,self.runtime.settings.chunk_frames))
                    good, pieces, bad = [], [], []
                    for batch in decoded_batches(path,frames[cursor:end],self.runtime.settings.batch,self.runtime.settings.decode_workers):
                        images = [image for _,image,_ in batch if image is not None]
                        try:
                            good.extend(frame for frame,image,_ in batch if image is not None)
                            bad.extend({'frame_no':frame['frame_no'],'error':error} for frame,image,error in batch if image is None)
                            if images:
                                pieces.append(self.runtime.embedder.embed(images))
                        finally:
                            for image in images:
                                image.close()
                    vectors = np.concatenate(pieces) if pieces else np.empty((0,DIMENSION),np.float32)
                    if row['kind']=='image' and not good:
                        raise ValueError(bad[0]['error'] if bad else '无法解码图片')
                    if vectors.shape!=(len(good),DIMENSION) or not np.isfinite(vectors).all():
                        raise ValueError('模型返回了无效向量')
                    self.write_chunk(folder,cursor,good,vectors)
                    details['skipped_frames'] = details.get('skipped_frames',0)+len(bad)
                    details['bad_frames'] = (details.get('bad_frames',[])+bad)[:100]
                    self.db.execute('UPDATE embedding_jobs SET cursor=?,details=? WHERE id=?',(end,json.dumps(details,ensure_ascii=False),ident))
                    cursor = end
            with self.operation:
                if self.db.one('SELECT status FROM embedding_jobs WHERE id=?',(ident,))['status']=='cancelled':
                    return
                if digest(path)!=row['sha256']:
                    raise ValueError('文件在处理期间发生变动，请重新提交')
                if not row['uploaded']:
                    self.permitted_path(str(path))
                self.publish(row,folder,details)
                self.db.execute("UPDATE embedding_jobs SET status='ready' WHERE id=?",(ident,))
        except Exception as error:
            self.db.execute("UPDATE embedding_jobs SET status='failed',error=? WHERE id=? AND status!='cancelled'",(str(error)[:1000],ident))
        finally:
            live = self.db.one('SELECT status FROM embedding_jobs WHERE id=?',(ident,))
            if row['uploaded'] and live and live['status'] in {'ready','failed','cancelled'}:
                Path(row['path']).unlink(missing_ok=True)

    def publish(self,row,folder,details):
        chunks = sorted(p for p in folder.glob('*.npz') if p.name!='result.npz')
        count = 0
        for path in chunks:
            with np.load(path,allow_pickle=False) as data:
                count += len(data['frame_no'])
        meta = {'model':MODEL_ID,'model_fingerprint':self.runtime.settings.manifest['fingerprint'],
                'dimension':DIMENSION,'dtype':'float32','normalized':True,'feature':'CLS',
                'input_size':[256,256],'media_type':row['kind'],'sha256':row['sha256'],
                'vector_count':count,'skipped_frames':details.get('skipped_frames',0),
                'bad_frames':details.get('bad_frames',[]),'bad_frames_truncated':details.get('skipped_frames',0)>100}
        temp = folder/'result.tmp'
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for key,dtype,shape in [('frame_no',np.int32,(count,)),('time_ms',np.int64,(count,)),('vectors',np.float32,(count,DIMENSION))]:
                with archive.open(key+'.npy','w',force_zip64=True) as stream:
                    np.lib.format.write_array_header_1_0(stream,{'descr':np.dtype(dtype).str,'fortran_order':False,'shape':shape})
                    for path in chunks:
                        with np.load(path,allow_pickle=False) as data:
                            stream.write(np.asarray(data[key],dtype=dtype).tobytes())
            archive.writestr('metadata.json',json.dumps(meta,ensure_ascii=False))
        os.replace(temp,folder/'result.npz')
        temp = folder/'metadata.tmp'
        temp.write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
        os.replace(temp,folder/'metadata.json')

    def result_row(self,ident):
        row = self.db.one('SELECT * FROM embedding_jobs WHERE id=?',(ident,))
        if not row:
            raise HTTPException(404,'任务不存在')
        if row['status']!='ready':
            raise HTTPException(409,'结果尚未就绪')
        return row

    def json_result(self,ident):
        row = self.result_row(ident)
        yield json.dumps(self.job(ident),ensure_ascii=False)[:-1]+',"frames":['
        first = True
        for path in sorted(p for p in self.folder(row).glob('*.npz') if p.name!='result.npz'):
            with np.load(path,allow_pickle=False) as data:
                for number,stamp,vector in zip(data['frame_no'],data['time_ms'],data['vectors']):
                    yield ('' if first else ',')+json.dumps({'frame_no':int(number),'time_ms':int(stamp),'embedding':vector.tolist()})
                    first = False
        yield ']}'

    def clear_cache(self):
        with self.operation:
            self.db.execute("UPDATE embedding_jobs SET status='cancelled',error='API 缓存已清理' WHERE status!='cancelled'")
            for name in ('cache','uploads'):
                path = (self.root/name).resolve()
                if path.parent!=self.root.resolve():
                    raise ValueError('API 缓存目录无效')
                shutil.rmtree(path)
                path.mkdir()
        return self.public()

    def start(self):
        self.thread = threading.Thread(target=self.run,daemon=True,name='embedding-api')
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            if self.config()['enabled']:
                with self.executor:
                    row = self.db.one("SELECT id FROM embedding_jobs WHERE status='pending' ORDER BY created LIMIT 1")
                    if row:
                        self.process(row['id'])
                        continue
            self.wake.wait(1); self.wake.clear()

    def stop(self):
        self.stop_event.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=120)
