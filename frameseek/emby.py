"""Emby item mapping and precise playback through the supported session API."""
from __future__ import annotations

import json
import re
import threading
import time
import secrets
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit, quote
from typing import Literal
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EmbySettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    playback_mode: Literal['standalone','emby_web'] = 'standalone'
    web_target: Literal['new_tab','recent'] = 'new_tab'
    server_url: str = Field('', max_length=2000)
    api_key: str = Field('', max_length=1000)
    user_id: str = Field('', max_length=100)
    device_id: str = Field('', max_length=200)
    source_paths: dict[str, str] = Field(default_factory=dict)

    @field_validator('server_url')
    @classmethod
    def url(cls, value):
        value = value.strip().rstrip('/')
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Emby 地址必须是完整 HTTP/HTTPS 服务地址，不能包含账号、查询或片段')
        return value

    @field_validator('source_paths')
    @classmethod
    def paths(cls, values):
        for source, value in values.items():
            if not source.isidentifier() or not value or '..' in value.replace('\\', '/').split('/') or '\x00' in value:
                raise ValueError('Emby 路径映射必须是有效的完整目录')
            if not value.startswith('/') and not re.match(r'^[A-Za-z]:[\\/]', value):
                raise ValueError('请填写 Emby 所见的完整媒体目录')
        return {key: value.replace('\\', '/').rstrip('/') or '/' for key, value in values.items()}

    @model_validator(mode='after')
    def required(self):
        if self.enabled and not (self.server_url and self.user_id):
            raise ValueError('启用 Emby 播放需要服务地址、API Key 和用户')
        return self


class EmbyError(Exception):
    pass


def normalize(path):
    path = path.replace('\\', '/')
    return path.casefold() if re.match(r'^[A-Za-z]:/', path) else path


def video_stem(path):
    return normalize(str(PurePosixPath(path.replace('\\', '/')).with_suffix('')))


def bif_stem(relative):
    # Emby sidecar format: video-name-320-10.bif (width and interval).
    stem = str(PurePosixPath(relative).with_suffix(''))
    return re.sub(r'-\d+-\d+$', '', stem)


class Emby:
    def __init__(self, db, media_roots=None):
        self.db = db
        self.media_roots = dict(media_roots or {})
        self.lock = threading.Lock()
        self.streams = {}
        self.stream_lock = threading.Lock()
        self.web_intents = {}
        self.web_lock = threading.Lock()
        db.execute('''CREATE TABLE IF NOT EXISTS emby_items(
            stem TEXT NOT NULL,item_id TEXT NOT NULL,media_source_id TEXT NOT NULL,
            path TEXT NOT NULL,PRIMARY KEY(stem,item_id,media_source_id))''')

    def config(self):
        row = self.db.one("SELECT value FROM meta WHERE key='emby_settings'")
        config = EmbySettings.model_validate_json(row['value']) if row else EmbySettings()
        config.source_paths = {**self.media_roots, **config.source_paths}
        return config

    def public(self):
        values = self.config().model_dump()
        values['api_key_set'] = bool(values.pop('api_key'))
        return values

    def save(self, incoming):
        old = self.config()
        data = incoming.model_dump()
        data['source_paths'] = {**self.media_roots, **data['source_paths']}
        if not data['api_key']:
            data['api_key'] = old.api_key
        config = EmbySettings.model_validate(data)
        if config.enabled and not config.api_key:
            raise EmbyError('启用 Emby 播放需要 API Key。')
        with self.lock, self.db.connect() as c:
            if any(getattr(old, key) != getattr(config, key) for key in ['server_url','api_key','user_id','source_paths']):
                c.execute('DELETE FROM emby_items')
                c.execute("DELETE FROM meta WHERE key='emby_items_updated'")
            c.execute("INSERT OR REPLACE INTO meta VALUES('emby_settings',?)", (config.model_dump_json(),))
        return self.public()

    def request(self, config, method, route, **kwargs):
        if not config.server_url or not config.api_key:
            raise EmbyError('请先保存 Emby 服务地址和 API Key。')
        base = config.server_url
        if not base.lower().endswith('/emby'):
            base += '/emby'
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(method, base + route, headers={'X-Emby-Token':config.api_key}, **kwargs)
                if response.status_code in {401,403}:
                    raise EmbyError('Emby 授权失败，请核对 API Key 和远程播放权限。')
                if not response.is_success:
                    raise EmbyError(f'Emby 请求失败（HTTP {response.status_code}），请核对服务地址。')
                return response.json() if response.content else None
        except (httpx.HTTPError, ValueError):
            # Never expose response URLs or token-bearing data in user-facing errors.
            raise EmbyError('无法连接 Emby 或收到无效响应，请核对地址及网络。') from None

    def clients(self):
        config = self.config()
        users = self.request(config, 'GET', '/Users')
        sessions = self.request(config,'GET','/Sessions')
        devices = [{'device_id':s['DeviceId'],'user_id':s.get('UserId',''),
                    'name':s.get('DeviceName','Emby Web'),'client':s.get('Client','')}
                   for s in sessions if self.web_session(s) and s.get('DeviceId')]
        return {'users':[{'id':u['Id'],'name':u.get('Name','')} for u in users], 'devices':devices}

    @staticmethod
    def web_session(session):
        return ('web' in session.get('Client','').lower() and session.get('SupportsRemoteControl') is True
                and 'Video' in session.get('PlayableMediaTypes',[]))

    @staticmethod
    def activity(session):
        try:
            value = datetime.fromisoformat((session.get('LastActivityDate') or '').replace('Z','+00:00'))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError):
            return 0

    def web_sessions(self, config):
        sessions = self.request(config,'GET','/Sessions',params={'ControllableByUserId':config.user_id})
        return [s for s in sessions if self.web_session(s) and s.get('UserId') == config.user_id
                and (not config.device_id or s.get('DeviceId') == config.device_id)]

    def web_begin(self, row):
        config = self.config()
        if not config.enabled or config.playback_mode != 'emby_web':
            raise EmbyError('请先启用 Emby 网页端播放。')
        sessions = self.web_sessions(config)
        latest = max(sessions,key=lambda s:(self.activity(s),s['Id'])) if sessions else None
        reuse = config.web_target == 'recent' and latest is not None
        ticket = secrets.token_urlsafe(32)
        intent = {'config':config,'frame_id':row['id'],'baseline':{s['Id']:self.activity(s) for s in sessions},
                  'target':latest['Id'] if reuse else None,'created':time.time(),'expires':time.time()+120}
        with self.web_lock:
            self.web_intents = {key:value for key,value in self.web_intents.items() if value['expires'] > time.time()}
            if len(self.web_intents) >= 32:
                raise EmbyError('等待中的网页播放请求过多，请稍后重试。')
            self.web_intents[ticket] = intent
        return {'ticket':ticket,'open_web':not reuse,'web_url':config.server_url+'/web/index.html'}

    def web_poll(self, row, ticket):
        with self.web_lock:
            intent = self.web_intents.get(ticket)
            if intent and intent.get('busy'):
                return {'waiting':True,'message':'该播放请求正在处理。'}
            if intent:
                intent['busy'] = True
        try:
            return self._web_poll(row, intent)
        finally:
            if intent:
                with self.web_lock:
                    intent['busy'] = False

    def _web_poll(self, row, intent):
        if not intent or intent['expires'] < time.time() or intent['frame_id'] != row['id']:
            raise EmbyError('网页播放请求已失效，请重新点击结果。')
        config = self.config()
        if config != intent['config']:
            raise EmbyError('Emby 配置已变动，请重新点击结果。')
        if intent.get('result'):
            return intent['result']
        sessions = self.web_sessions(config)
        target = next((s for s in sessions if s['Id'] == intent['target']),None) if intent['target'] else None
        if intent['target'] and target is None:
            intent.update(target=None,baseline={s['Id']:self.activity(s) for s in sessions},created=time.time())
            return {'waiting':True,'open_web':True,'web_url':config.server_url+'/web/index.html'}
        shared = False
        if target is None:
            new = [s for s in sessions if s['Id'] not in intent['baseline']]
            # A tab may reactivate the same browser session instead of creating a new ID.
            updated = [s for s in sessions if self.activity(s) > intent['baseline'].get(s['Id'],0)] if time.time()-intent['created'] >= 3 else []
            candidates = new or updated
            if not candidates:
                return {'waiting':True,'message':'正在等待新网页端登录或更新活动状态。'}
            target = max(candidates,key=lambda s:(self.activity(s),s['Id']))
            shared = not new
        result = self.play(row,web_only=True,target_session=target['Id'])
        if not result.get('waiting'):
            result.update(target_device=target.get('DeviceName','Emby Web'),shared_session=shared)
            intent['result'] = result
        return result

    def refresh(self, config):
        with self.lock:
            updated = self.db.one("SELECT value FROM meta WHERE key='emby_items_updated'")
            if updated and time.time() - float(updated['value']) < 3600:
                return
            records = []
            for page in range(100):
                payload = self.request(config, 'GET', '/Items', params={
                    'UserId':config.user_id, 'Recursive':'true','MediaTypes':'Video',
                    'Fields':'Path,MediaSources','StartIndex':page*1000,'Limit':1000,
                    'EnableImages':'false','EnableUserData':'false'})
                items = payload.get('Items', [])
                for item in items:
                    media = item.get('MediaSources') or [{'Path':item.get('Path'),'Id':''}]
                    for source in media:
                        path = source.get('Path')
                        if path and item.get('Id'):
                            records.append((video_stem(path),str(item['Id']),str(source.get('Id') or ''),path))
                if (page+1)*1000 >= payload.get('TotalRecordCount', 0) or not items:
                    break
            else:
                raise EmbyError('Emby 媒体库超过读取上限，请缩小该用户可访问的媒体库。')
            # A configuration changed mid-refresh must not publish the previous server's map.
            if self.config() != config:
                raise EmbyError('Emby 配置已变动，请重试。')
            with self.db.connect() as c:
                c.execute('DELETE FROM emby_items')
                c.executemany('INSERT OR IGNORE INTO emby_items VALUES(?,?,?,?)', records)
                c.execute("INSERT OR REPLACE INTO meta VALUES('emby_items_updated',?)", (str(time.time()),))

    def resolve(self, row):
        if row.get('media_type') == 'image':
            raise EmbyError('普通图片不支持视频播放。')
        config = self.config()
        if not config.enabled:
            raise EmbyError('请在配置页启用 Emby 播放。')
        root = config.source_paths.get(row['source'])
        if not root:
            raise EmbyError('该视频来源尚未设置 Emby 路径映射。')
        target = normalize(root.rstrip('/') + '/' + bif_stem(row['relpath']))
        self.refresh(config)
        matches = self.db.rows('SELECT * FROM emby_items WHERE stem=?', (target,))
        if not matches and PurePosixPath(target).suffix.lower() in {'.mp4','.mkv','.avi','.mov','.webm','.m4v','.ts','.m2ts'}:
            matches = self.db.rows('SELECT * FROM emby_items WHERE stem=?', (video_stem(target),))
        if len(matches) != 1:
            raise EmbyError('未找到唯一对应的 Emby 视频，请核对路径映射、确认媒体已入库，并在配置页刷新视频映射。')
        item = matches[0]
        return config, item

    def prepare(self, row):
        config, item = self.resolve(row)
        details = self.request(config, 'GET', '/Items/' + quote(item['item_id'],safe='') + '/PlaybackInfo', params={'UserId':config.user_id})
        sources = details.get('MediaSources', [])
        source = next((s for s in sources if str(s.get('Id') or '') == item['media_source_id']), None)
        if source is None and len(sources) == 1:
            source = sources[0]
        if source is None:
            raise EmbyError('无法确定视频的播放版本，请刷新视频映射后重试。')
        streams = source.get('MediaStreams', [])
        video = next((s for s in streams if s.get('Type') == 'Video'), {})
        audio = next((s for s in streams if s.get('Type') == 'Audio'), {})
        direct = (str(source.get('Container','')).lower() in {'mp4','m4v'} and video.get('Codec') == 'h264'
                  and audio.get('Codec') in {None,'aac','mp3'} and not source.get('RequiredHttpHeaders'))
        token = secrets.token_urlsafe(32)
        params = {'UserId':config.user_id,'MediaSourceId':str(source.get('Id') or item['media_source_id']),
                  'DeviceId':'frameseek-'+token,'PlaySessionId':uuid.uuid4().hex,'Static':'true' if direct else 'false'}
        if not direct:
            params.update(VideoCodec='h264',AudioCodec='aac',VideoBitrate='8000000',AudioBitrate='192000',
                          StartTimeTicks=str(int(row['time_ms'])*10000),AllowVideoStreamCopy='false',AllowAudioStreamCopy='false')
        entry = {'config':config,'route':'/Videos/'+quote(item['item_id'],safe='')+'/stream.mp4',
                 'params':params,'expires':time.time()+43200,'direct':direct}
        with self.stream_lock:
            self.streams = {key:value for key,value in self.streams.items() if value['expires'] > time.time()}
            if len(self.streams) >= 32:
                raise EmbyError('打开的播放窗口过多，请先关闭部分窗口。')
            self.streams[token] = entry
        return {'stream_url':'/api/emby/stream/'+token,'stop_url':'/api/emby/stop/'+token,
                'time_ms':row['time_ms'],'seek_seconds':row['time_ms']/1000 if direct else 0,
                'offset_seconds':0 if direct else row['time_ms']/1000,
                'transcoding':not direct,'name':PurePosixPath(item['path']).name}

    def stream(self, token):
        with self.stream_lock:
            entry = self.streams.get(token)
        config = self.config()
        if not entry or entry['expires'] < time.time() or not config.enabled or config != entry['config']:
            raise EmbyError('播放链接已失效，请重新打开搜索结果。')
        return entry

    def stop(self, token):
        with self.stream_lock:
            entry = self.streams.pop(token, None)
        if entry and not entry['direct']:
            self.request(entry['config'],'DELETE','/Videos/ActiveEncodings',params={
                'DeviceId':entry['params']['DeviceId'],'PlaySessionId':entry['params']['PlaySessionId']})

    def play(self, row, web_only=False, target_session=None):
        config, item = self.resolve(row)
        if web_only and config.playback_mode != 'emby_web':
            raise EmbyError('播放方式已变动，请重新点击搜索结果。')
        sessions = self.request(config, 'GET', '/Sessions', params={'ControllableByUserId':config.user_id})
        candidates = [s for s in sessions if (s.get('DeviceId') == config.device_id or (web_only and not config.device_id))
                      and s.get('UserId') == config.user_id and s.get('SupportsRemoteControl') is True
                      and 'Video' in s.get('PlayableMediaTypes',[]) and (not web_only or self.web_session(s))
                      and (not target_session or s.get('Id') == target_session)]
        if web_only and not candidates:
            return {'waiting':True,'message':'正在等待指定用户的 Emby 网页端上线，请在新窗口登录。'}
        if len(candidates) != 1:
            raise EmbyError('网页播放会话不唯一，请在配置页指定网页设备后重试。' if web_only else '播放设备未在线或会话不唯一。请先打开 Emby 客户端，或在配置页重新选择设备。')
        params = {'ItemIds':item['item_id'],'PlayCommand':'PlayNow', 'StartPositionTicks':int(row['time_ms'])*10000}
        body = {'ControllingUserId':config.user_id}
        if item['media_source_id']:
            body['MediaSourceId'] = item['media_source_id']
        if self.config() != config:
            raise EmbyError('Emby 配置已变动，请重新点击搜索结果。')
        self.request(config, 'POST', '/Sessions/' + quote(candidates[0]['Id'],safe='') + '/Playing', params=params, json=body)
        return {'ok':True,'time_ms':row['time_ms'],'message':'播放请求已发送到 Emby，将从命中时间开始播放。'}
