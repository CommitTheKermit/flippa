"""Run trusted, fixed Android validation profiles outside the SSH session."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import device
from validation_device_lease import DeviceLease
from app_update import ApkInspector, ApkSigningPolicy
from validation_jobs import JobStore, ResourceBusy, load_config, resource_lock, sha256


class Cancelled(Exception):
    pass


class ValidationWorker:
    def __init__(self, config):
        self.config = config
        self.store = JobStore(config['state_dir'])
        self.root = self.store.root
        self.job = None
        self.secrets = []

    def check_cancel(self):
        if self.store.cancelled(self.job):
            raise Cancelled('요청한 작업이 취소됐습니다.')

    def clean_text(self, value):
        value = device.redact(str(value))
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        for path in (str(self.root), self.config['app_repo'], str(Path.home())):
            value = value.replace(path, '[PRIVATE_PATH]')
        return value

    def command(self, args, cwd, phase, timeout=1800):
        self.check_cancel()
        self.store.phase(self.job, phase)
        log = self.root/'artifacts'/self.job/(phase+'.log')
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        env = dict(os.environ, GRADLE_USER_HOME=self.config.get('gradle_home', str(Path.home()/'.gradle')))
        with log.open('w', encoding='utf-8') as output:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', **options)
            def drain():
                for line in process.stdout:
                    output.write(self.clean_text(line)); output.flush()
            reader = threading.Thread(target=drain, daemon=True); reader.start()
            deadline = time.monotonic()+timeout
            try:
                while process.poll() is None:
                    self.check_cancel()
                    if time.monotonic() >= deadline:
                        raise ValueError(phase+' 실행 시간이 초과됐습니다.')
                    time.sleep(.2)
                reader.join(10)
                if reader.is_alive():
                    raise ResourceBusy('명령 종료 후 하위 프로세스 종료를 확인하지 못해 다음 작업을 차단합니다.')
                if process.returncode:
                    raise ValueError(phase+' 실패. 작업 로그를 확인하세요.')
            except BaseException:
                if process.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=20)
                reader.join(10)
                if reader.is_alive():
                    raise ResourceBusy('하위 프로세스 종료를 확인하지 못해 다음 작업을 차단합니다.')
                raise
            finally:
                process.stdout.close()
        return log.read_text(encoding='utf-8').strip()

    def server_idle(self):
        origin=self.config['origin'].rstrip('/')
        req=Request('http://127.0.0.1:4317/api/state',headers={'Host':urlsplit(origin).netloc,'Origin':origin})
        with urlopen(req,timeout=10) as response:
            state=json.load(response)
        return not (state.get('running') or state.get('pending') or state.get('external_running') or state.get('device_preparing') or state.get('app_update',{}).get('running'))

    def prepare_checkout(self, request):
        source=Path(self.config['app_repo'])
        target=self.root/'worktrees'/self.job
        target.parent.mkdir(exist_ok=True)
        self.command(['git','fetch','origin','+refs/heads/*:refs/remotes/origin/*'],source,'fetch',300)
        permitted=False
        for ref in self.config.get('allowed_refs',['refs/remotes/origin/an-develop']):
            result=subprocess.run(['git','merge-base','--is-ancestor',request['sha'],ref],cwd=source,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if result.returncode==0:
                permitted=True; break
        if not permitted:
            raise ValueError('허용된 원격 브랜치에 포함되지 않은 커밋입니다.')
        self.command(['git','worktree','add','--detach',str(target),request['sha']],source,'checkout',120)
        actual=self.command(['git','rev-parse','HEAD'],target,'revision',30)
        if actual!=request['sha']:
            raise ValueError('검증 소스 커밋이 요청과 다릅니다.')
        for relative in self.config.get('private_files',[]):
            rel=Path(relative)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('비공개 설정의 상대 경로가 잘못됐습니다.')
            source_file=source/rel; destination=target/rel
            if not source_file.is_file():
                raise ValueError('서버의 필수 빌드 설정 파일이 없습니다.')
            ignored=subprocess.run(['git','check-ignore','--quiet',str(rel)],cwd=target).returncode==0
            if not ignored:
                raise ValueError('비공개 빌드 설정은 Git 제외 경로에만 공급할 수 있습니다.')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source_file,destination)
            if rel.suffix=='.properties':
                for line in destination.read_text(encoding='utf-8').splitlines():
                    if '=' in line and re.search('password|secret|token',line.split('=',1)[0],re.I):
                        self.secrets.append(line.split('=',1)[1].strip())
        return target

    def test_results(self, checkout):
        totals={'tests':0,'failures':0,'errors':0,'skipped':0}
        artifacts=self.root/'artifacts'/self.job/'tests'; artifacts.mkdir(exist_ok=True)
        for n,path in enumerate(checkout.glob('android/**/build/test-results/**/TEST-*.xml')):
            tree=ET.parse(path).getroot()
            for key in totals:
                totals[key]+=int(tree.get(key,'0'))
            (artifacts/f'{n:04d}.xml').write_text(self.clean_text(path.read_text(encoding='utf-8')),encoding='utf-8')
        if not totals['tests'] or totals['failures'] or totals['errors']:
            raise ValueError('실제 실행된 단위 테스트가 없거나 실패했습니다.')
        return totals

    def inspect_install_target(self, serial, apk, artifacts):
        inspector=ApkInspector()
        candidate=inspector.inspect(apk)
        listed=device.adb(serial,'shell','pm','list','packages',self.config['package']).splitlines()
        present=('package:'+self.config['package']) in [line.strip() for line in listed]
        paths=device.adb(serial,'shell','pm','path',self.config['package']).splitlines() if present else []
        paths=[line.removeprefix('package:').strip() for line in paths if line.startswith('package:')]
        installed=None
        if paths:
            base=next((path for path in paths if path.endswith('/base.apk')),paths[0])
            with tempfile.TemporaryDirectory(prefix='plipa-compare-') as folder:
                previous=Path(folder)/'installed.apk'
                device.adb(serial,'pull',base,str(previous),timeout=120)
                installed=inspector.inspect(previous)
            if installed['package']!=candidate['package'] or installed['signer_sha256']!=candidate['signer_sha256']:
                raise ValueError('기존 앱과 패키지 또는 서명이 달라 데이터를 유지한 설치를 중단합니다.')
            if installed['version_code']>candidate['version_code']:
                raise ValueError('기존 앱보다 낮은 버전은 자동 설치하지 않습니다.')
        (artifacts/'install-check.json').write_text(json.dumps({'candidate':candidate,'installed':installed},ensure_ascii=False),encoding='utf-8')

    def install_and_smoke(self, apk, artifacts):
        avd=self.config['avd']
        if device.find_avd(avd):
            raise ResourceBusy('이미 실행 중인 검증 기기는 자동 인수하지 않습니다.')
        lease=DeviceLease(avd, 'plipa-validation:'+self.job, self.config.get('lease_dir'))
        lease.acquire()
        self.store.phase(self.job,'booting')
        # Durable ownership precedes boot, so crash recovery can identify the device.
        (artifacts/'device-owner.json').write_text(json.dumps({'job':self.job,'avd':avd}),encoding='utf-8')
        serial=None; started=False
        try:
            self.check_cancel(); device.boot(avd); started=True
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                self.check_cancel()
                entry=device.find_avd(avd)
                if entry:
                    serial=entry['serial']
                    lease.bind(serial)
                    if entry['state']=='device' and device.boot_completed(serial): break
                time.sleep(2)
            else: raise ValueError('검증 기기 부팅 시간이 초과됐습니다.')
            self.store.phase(self.job,'installing')
            # install -r fails closed on signer mismatch or downgrade, never uninstalls.
            self.inspect_install_target(serial,apk,artifacts)
            device.adb(serial,'install','-r',str(apk),timeout=300)
            self.check_cancel(); self.store.phase(self.job,'smoke')
            package=self.config['package']
            device.launch(serial,package)
            deadline=time.monotonic()+45
            foreground=''
            while time.monotonic()<deadline:
                self.check_cancel()
                activity=device.adb(serial,'shell','dumpsys','activity','activities',timeout=20)
                foreground='\n'.join(line.strip() for line in activity.splitlines() if 'ResumedActivity' in line or 'mResumedActivity' in line)
                if re.search(re.escape(package)+r'/[\w.$]+',foreground): break
                time.sleep(2)
            else: raise ValueError('앱의 foreground 실행을 확인하지 못했습니다.')
            pid=device.adb(serial,'shell','pidof',package).strip()
            if not pid: raise ValueError('실행 직후 앱 프로세스가 종료됐습니다.')
            environment=device.environment(serial,package)
            (artifacts/'smoke.json').write_text(json.dumps({'environment':environment,'foreground':foreground,'process_alive':True},ensure_ascii=False),encoding='utf-8')
            return {'serial':serial,'avd':avd,'foreground':foreground,'process_alive':True}
        finally:
            if started:
                entry=device.find_avd(avd)
                if not entry and serial is None:
                    raise ResourceBusy('부팅한 기기의 종료 상태를 확인하지 못해 임대와 다음 작업을 차단합니다.')
                if entry:
                    # Only the job's own AVD is stopped; do not use global adb resets.
                    device.adb(entry['serial'],'shell','sync',timeout=20)
                    device.adb(entry['serial'],'emu','kill',timeout=10)
                    deadline=time.monotonic()+45
                    while device.find_avd(avd) and time.monotonic()<deadline: time.sleep(1)
                    if device.find_avd(avd): raise ResourceBusy('검증 기기 종료를 확인하지 못해 다음 작업을 차단합니다.')
            lease.release()

    def execute(self, job):
        self.job=job; self.secrets=[]
        request=self.store.get(job)['request']
        artifacts=self.root/'artifacts'/job; artifacts.mkdir(parents=True,exist_ok=True)
        result={'source_sha':request['sha'],'profile':request['profile'],'checks':{}}
        state='failed'; checkout=None
        try:
            checkout=self.prepare_checkout(request)
            profile=self.config['profiles'][request['profile']]
            project=checkout/'android'
            wrapper=project/('gradlew.bat' if os.name=='nt' else 'gradlew')
            self.command([str(wrapper),*profile['tasks'],'--console=plain','--no-daemon','--max-workers=2','-Pkotlin.compiler.execution.strategy=in-process'],project,'build-and-test',profile.get('timeout',1800))
            result['checks']['unit_tests']=self.test_results(checkout)
            apk=checkout/profile['apk']
            metadata=ApkInspector().inspect(apk)
            if metadata['package']!=self.config['package']: raise ValueError('APK 패키지가 설정과 다릅니다.')
            ApkSigningPolicy(self.config['signers']).require_allowed(metadata)
            snapshot=artifacts/'app.apk'; shutil.copyfile(apk,snapshot)
            result['apk']={**metadata,'sha256':sha256(snapshot),'bytes':snapshot.stat().st_size}
            result['checks']['smoke']=self.install_and_smoke(snapshot,artifacts)
            self.check_cancel(); state='succeeded'
        except Cancelled as error:
            state='cancelled'; result['error']=str(error)
        except ResourceBusy as error:
            state='blocked'; result['error']=str(error)
        except Exception as error:
            result['error']=self.clean_text(error)
            result['failed_phase']=self.store.get(job)['phase']
            (artifacts/'failure.log').write_text(self.clean_text(traceback.format_exc()),encoding='utf-8')
        finally:
            # Never retain keystore properties in job worktrees after completion.
            target=checkout or self.root/'worktrees'/job
            for relative in self.config.get('private_files',[]):
                path=target/relative
                if path.is_file(): path.unlink()
            if target.exists():
                subprocess.run(['git','-c','core.longpaths=true','worktree','remove','--force',str(target)],cwd=self.config['app_repo'],capture_output=True)
                if target.exists():
                    # Windows may remove Git registration but leave read-only generated files.
                    def remove_readonly(function, path, error):
                        os.chmod(path, 0o700)
                        function(path)
                    try:
                        shutil.rmtree(target,onerror=remove_readonly)
                        subprocess.run(['git','worktree','remove','--force',str(target)],cwd=self.config['app_repo'],capture_output=True)
                    except OSError:
                        result['cleanup_warning']='작업용 checkout 정리가 필요합니다.'
            result['artifacts']=[str(p.relative_to(artifacts)).replace('\\','/') for p in artifacts.rglob('*') if p.is_file()]
            self.store.finish(job,state,result)
            (artifacts/'result.json').write_text(json.dumps(self.store.get(job),ensure_ascii=False,indent=2),encoding='utf-8')

    def tick(self):
        try:
            with resource_lock(self.root/'device.lock'):
                if not self.server_idle(): return False
                job=self.store.claim()
                if not job:return False
                self.execute(job)
                return True
        except ResourceBusy:
            return False


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    worker=ValidationWorker(load_config(args.config))
    with resource_lock(worker.root/'worker.lock'):
        while True:
            try: worker.tick()
            except Exception as error: print(worker.clean_text(error),file=sys.stderr,flush=True)
            if args.once:break
            time.sleep(2)


if __name__=='__main__':main()
