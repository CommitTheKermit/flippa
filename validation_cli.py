"""JSON stdin/stdout control plane, intended for an authenticated SSH channel."""
import argparse
import base64
import io
import json
import sys
import time
import zipfile
from pathlib import Path

from validation_jobs import JobStore, load_config, resource_lock


def dispatch(config, message):
    store=JobStore(config['state_dir'])
    action=message.get('action')
    if action=='submit':return store.submit(message['request'],config['profiles'])
    if action=='list':return store.list()
    if action=='status':return store.get(message['job'])
    if action=='wait':
        deadline=time.monotonic()+min(50,max(0,float(message.get('seconds',40))))
        while True:
            row=store.get(message['job'])
            if row['state'] in ('succeeded','failed','cancelled','blocked') or time.monotonic()>=deadline:return row
            time.sleep(.5)
    if action=='cancel':return store.cancel(message['job'])
    if action=='recover':
        if message.get('confirmed_stopped') is not True:raise ValueError('프로세스와 기기 정리 확인이 필요합니다.')
        with resource_lock(store.root/'worker.lock'), resource_lock(store.root/'device.lock'):
            return store.recover(message['job'])
    if action=='results':
        job=store.get(message['job'])
        if job['state'] not in ('succeeded','failed','cancelled','blocked'):raise ValueError('아직 작업 결과가 확정되지 않았습니다.')
        root=store.root/'artifacts'/job['id']
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('result.json',json.dumps(job,ensure_ascii=False,indent=2))
            for path in root.rglob('*'):
                if path.is_file() and path.suffix in ('.log','.xml','.json') and path.name!='result.json':
                    if path.stat().st_size>10*1024*1024:raise ValueError('결과 파일이 제한 크기를 넘었습니다.')
                    archive.write(path,str(path.relative_to(root)))
        if len(output.getvalue())>20*1024*1024:raise ValueError('결과 묶음이 제한 크기를 넘었습니다.')
        return {'job':job['id'],'encoding':'base64-zip','data':base64.b64encode(output.getvalue()).decode('ascii')}
    raise ValueError('지원하지 않는 원격 검증 명령입니다.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('요청이 너무 큽니다.')
        result=dispatch(load_config(args.config),json.loads(raw))
        sys.stdout.write(json.dumps({'ok':True,'result':result},ensure_ascii=True)+'\n')
    except (ValueError,KeyError,OSError,TypeError) as error:
        # Do not expose server paths from native exceptions to the remote client.
        message=str(error) if isinstance(error,ValueError) else '원격 검증 요청을 처리하지 못했습니다.'
        sys.stdout.write(json.dumps({'ok':False,'error':message},ensure_ascii=True)+'\n')
        return 2
    return 0


if __name__=='__main__':sys.exit(main())
