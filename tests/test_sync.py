import io
import json
from types import SimpleNamespace

from frameseek.engine.model import digest
from frameseek.core.paths import media_path
from frameseek.integrations.sync import sync


class RemoteFile(io.BytesIO):
    def prefetch(self,*args,**kwargs):
        pass


class SFTP:
    def __init__(self,data): self.data=data
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def stat(self,path): return SimpleNamespace(st_size=len(self.data),st_mtime=1234)
    def open(self,path,mode): return RemoteFile(self.data)


class Client:
    def __init__(self,data): self.data=data
    def open_sftp(self): return SFTP(self.data)


def test_unicode_line_separator_and_corrupt_resume_prefix(tmp_path):
    data = b'JPEG-containing BIF bytes for transfer checksum testing'
    relative = 'directory/movie\u2028part.bif'
    manifest = tmp_path/'inventory.jsonl'
    manifest.write_text(json.dumps({'type':'file','source':'sda','relpath':relative,
                                   'size':len(data),'mtime_ns':1234_000_000_000},ensure_ascii=False)+'\n',encoding='utf-8')
    destination = tmp_path/'bif'
    target = media_path(destination/'sda',relative,True)
    target.parent.mkdir(parents=True)
    target.with_name(target.name+'.partial').write_bytes(b'corrupt-prefix')
    result = sync(Client(data),manifest,destination,True,media_roots={'sda':'/mnt/test-videos'})
    assert result['copied']==1 and not result['errors']
    assert target.read_bytes()==data
    assert not target.with_name(target.name+'.partial').exists()
    again = sync(Client(data),manifest,destination,True,media_roots={'sda':'/mnt/test-videos'})
    assert again['copied']==0 and again['skipped']==1
    assert digest(target)
