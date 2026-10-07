"""Explicit, temporary NAS CPU benchmark. Never installs a persistent service."""
from __future__ import annotations

import argparse
import json
import shlex
import time
import uuid
from pathlib import Path

from .console import configure_console
from .sync import connect, NAS_ROOTS


def command(client, text: str, output: Path | None = None):
    stdin, stdout, stderr = client.exec_command(text,get_pty=True)
    with output.open('w',encoding='utf-8') if output else open(Path('reports/nas-benchmark-commands.log'),'a',encoding='utf-8') as log:
        for line in stdout:
            log.write(line); log.flush()
            if line.lstrip().startswith('{'):
                print(line.strip(),flush=True)
    code = stdout.channel.recv_exit_status()
    if code:
        raise RuntimeError(f'Temporary NAS command failed ({code}); see {log.name}')


def main():
    configure_console()
    parser = argparse.ArgumentParser()
    parser.add_argument('--image',type=Path,default=Path('reports/benchmark-app-image.tar'))
    parser.add_argument('--model',type=Path,default=Path('models/dinov3-vitl16'))
    parser.add_argument('--frames',type=int,default=64)
    parser.add_argument('--repeat',type=int,default=3)
    args = parser.parse_args()
    if args.frames < 1 or args.repeat < 1:
        parser.error('frames and repeat must be positive')
    directory = '/tmp/imgsearch-benchmark-'+uuid.uuid4().hex
    tag = 'bif-imgsearch-benchmark:20261006'
    reports = Path('reports'); reports.mkdir(exist_ok=True)
    client = connect()
    image_loaded = False
    print(json.dumps({'stage':'prepare','temporary_directory':directory}),flush=True)
    try:
        command(client,'mkdir -p '+shlex.quote(directory+'/model')+' '+shlex.quote(directory+'/scratch')+
                ' && chmod 777 '+shlex.quote(directory+'/scratch'))
        with client.open_sftp() as sftp:
            files = [(args.image,directory+'/image.tar')]
            files.extend((p,directory+'/model/'+p.name) for p in args.model.iterdir() if p.is_file())
            for path,target in files:
                last = [0.0]
                def progress(done,total):
                    if time.monotonic()-last[0] > 30 or done==total:
                        print(json.dumps({'stage':'upload','file':path.name,'bytes':done,'total':total}),flush=True)
                        last[0]=time.monotonic()
                sftp.put(str(path),target,callback=progress,confirm=True)
        command(client,'docker load -i '+shlex.quote(directory+'/image.tar'))
        image_loaded = True
        _, user_output, _ = client.exec_command('id -u; id -g')
        user_id, group_id = [int(value) for value in user_output.read().decode().split()]
        mounts = ' '.join('-v '+shlex.quote(root+':/media/'+source+':ro') for source,root in NAS_ROOTS.items())
        environment = json.dumps({source:'/media/'+source for source in NAS_ROOTS})
        print(json.dumps({'stage':'benchmark','frames':args.frames,'repeat':args.repeat,'memory_limit':'4g',
                          'torch_threads':4,'batch':1,'published_ports':[]}),flush=True)
        run = ('docker run --rm --memory 4g --cpus 4 --cpu-shares 128 --user '+str(user_id)+':'+str(group_id)+' '
               '-e HOME=/scratch '
               '-e IMGS_DATA=/scratch/data -e IMGS_MODEL=/models/dinov3 -e IMGS_ESCAPED_PATHS=false '
               '-e IMGS_TORCH_THREADS=4 -e IMGS_SOURCES='+shlex.quote(environment)+' '
               '-v '+shlex.quote(directory+'/model:/models/dinov3:ro')+' '
               '-v '+shlex.quote(directory+'/scratch:/scratch')+' '+mounts+' '+tag+
               ' python -m imgsearch.benchmark --device cpu --batch 1 --frames '+str(args.frames)+
               ' --repeat '+str(args.repeat)+' --output /scratch/result.json')
        command(client,run,reports/'nas-benchmark-run.log')
        with client.open_sftp() as sftp:
            sftp.get(directory+'/scratch/result.json',str(reports/'nas-12300t-benchmark.json'))
        result = json.loads((reports/'nas-12300t-benchmark.json').read_text(encoding='utf-8'))
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    finally:
        # Remove only the exact unique benchmark directory, never a computed parent.
        cleanup = ("import os,shutil; p="+repr(directory)+
                   "; r=os.path.realpath(p); assert r==p and r.startswith('/tmp/imgsearch-benchmark-') "
                   "and len(r.rsplit('-',1)[1])==32; shutil.rmtree(r)")
        try:
            command(client,'python3 -c '+shlex.quote(cleanup))
            if image_loaded:
                command(client,'docker image rm '+shlex.quote(tag))
            print(json.dumps({'stage':'cleanup_complete','temporary_directory':directory}),flush=True)
        finally:
            client.close()


if __name__ == '__main__':
    main()
