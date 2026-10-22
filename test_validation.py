import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from validation_jobs import JobStore, ResourceBusy, resource_lock
from validation_cli import dispatch
from validation_worker import ValidationWorker


class RemoteValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'state_dir':str(self.root/'state'),'app_repo':str(self.root/'source'),'origin':'http://127.0.0.1:4317','signers':'a'*64,'package':'com.example.app','avd':'qa','profiles':{'smoke':{'tasks':[':app:testDebugUnitTest'], 'apk':'android/app.apk'}}}
        self.store=JobStore(self.config['state_dir'])
        self.request={'request_id':'test-request-01','sha':'a'*40,'profile':'smoke'}

    def submit(self,request=None):return self.store.submit(request or self.request,self.config['profiles'])

    def test_idempotency_and_changed_request_rejection(self):
        first=self.submit();self.assertEqual(first['id'],self.submit()['id'])
        with self.assertRaises(ValueError):self.submit({**self.request,'sha':'b'*40})

    def test_client_cannot_supply_commands_or_unapproved_profile(self):
        for request in [{**self.request,'command':'anything'},{**self.request,'profile':'shell'},{**self.request,'sha':'main'}]:
            with self.assertRaises(ValueError):self.submit(request)

    def test_fifo_and_crash_does_not_reexecute(self):
        a=self.submit();b=self.submit({**self.request,'request_id':'test-request-02'})
        self.assertEqual(self.store.claim(),a['id']);self.assertIsNone(self.store.claim())
        self.store.recover(a['id']);self.assertEqual(self.store.claim(),b['id'])

    def test_cancel_queued_and_running(self):
        a=self.submit();self.store.cancel(a['id']);self.assertIsNone(self.store.claim())
        b=self.submit({**self.request,'request_id':'test-request-02'});self.store.claim()
        self.store.cancel(b['id']);self.assertTrue(self.store.cancelled(b['id']))
        self.assertIsNone(self.store.claim())

    def test_cross_process_resource_excludes_web_and_worker(self):
        lock=self.root/'lock'
        with resource_lock(lock):
            result=subprocess.run([sys.executable,'-c',"from validation_jobs import resource_lock; import sys\nwith resource_lock(sys.argv[1]): pass",str(lock)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
        with resource_lock(lock):pass

    def test_results_roundtrip_excludes_apk_and_keystore(self):
        job=self.submit();self.store.finish(job['id'],'succeeded',{'source_sha':'a'*40})
        folder=self.store.root/'artifacts'/job['id'];folder.mkdir(parents=True)
        (folder/'build.log').write_text('BUILD SUCCESSFUL')
        (folder/'app.apk').write_bytes(b'fake apk')
        (folder/'keystore.properties').write_text('private')
        response=dispatch(self.config,{'action':'results','job':job['id']})
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(response['data']))) as archive:
            self.assertEqual(set(archive.namelist()),{'result.json','build.log'})
            self.assertEqual(json.loads(archive.read('result.json'))['result']['source_sha'],'a'*40)

    def test_worker_waits_for_web_activity_without_claiming(self):
        job=self.submit();worker=ValidationWorker(self.config)
        with patch.object(worker,'server_idle',return_value=False):self.assertFalse(worker.tick())
        self.assertEqual(self.store.get(job['id'])['state'],'queued')

    def test_nonzero_build_cannot_report_success(self):
        job=self.submit();self.store.claim();worker=ValidationWorker(self.config)
        with patch.object(worker,'prepare_checkout',side_effect=ValueError('fetch failed')):
            worker.execute(job['id'])
        self.assertEqual(self.store.get(job['id'])['state'],'failed')

    def test_no_tests_is_not_a_pass(self):
        job=self.submit();worker=ValidationWorker(self.config);worker.job=job['id']
        (worker.root/'artifacts'/worker.job).mkdir(parents=True)
        with self.assertRaises(ValueError):worker.test_results(self.root)

    def test_mismatched_apk_never_installs(self):
        job=self.submit();self.store.claim();worker=ValidationWorker(self.config)
        checkout=self.root/'checkout';checkout.mkdir()
        with patch.object(worker,'prepare_checkout',return_value=checkout), patch.object(worker,'command'), patch.object(worker,'test_results',return_value={'tests':1}), patch('validation_worker.ApkInspector.inspect',return_value={'package':'wrong'}), patch.object(worker,'install_and_smoke') as install, patch('validation_worker.subprocess.run'):
            worker.execute(job['id'])
        install.assert_not_called();self.assertEqual(self.store.get(job['id'])['state'],'failed')

    def test_installed_signer_mismatch_stops_before_install(self):
        worker=ValidationWorker(self.config)
        candidate={'package':'com.example.app','signer_sha256':'a','version_code':2}
        old={**candidate,'signer_sha256':'b'}
        with patch('validation_worker.device.adb',side_effect=['package:com.example.app','package:/data/app/base.apk','']),patch('validation_worker.ApkInspector.inspect',side_effect=[candidate,old]):
            with self.assertRaisesRegex(ValueError,'서명'):worker.inspect_install_target('emulator-5554',self.root/'apk',self.root)

    def test_first_install_does_not_call_pm_path_for_absent_package(self):
        worker=ValidationWorker(self.config)
        with patch('validation_worker.device.adb',return_value='') as adb,patch('validation_worker.ApkInspector.inspect',return_value={'package':'com.example.app'}):
            worker.inspect_install_target('emulator-5554',self.root/'apk',self.root)
        adb.assert_called_once_with('emulator-5554','shell','pm','list','packages','com.example.app')
        self.assertIsNone(json.loads((self.root/'install-check.json').read_text())['installed'])

    def test_existing_device_is_not_taken_over(self):
        job=self.submit();worker=ValidationWorker(self.config);worker.job=job['id']
        with patch('validation_worker.device.find_avd',return_value={'serial':'existing'}),patch('validation_worker.device.boot') as boot:
            with self.assertRaises(ResourceBusy):worker.install_and_smoke(self.root/'fake.apk',self.root)
        boot.assert_not_called()

    def test_untrusted_post_is_rejected_before_device_lock(self):
        from server import Handler
        from unittest.mock import Mock
        handler=Handler.__new__(Handler)
        handler.trusted=Mock(return_value=False);handler.reply=Mock()
        with patch('server.remote_resource') as lock:
            handler.do_POST()
        lock.assert_not_called()
        self.assertEqual(handler.reply.call_args.args[1],403)

    def test_existing_lease_cannot_be_taken_even_when_device_is_stopped(self):
        from validation_device_lease import DeviceLease
        first=DeviceLease('qa','first',self.root/'leases'); first.acquire()
        with self.assertRaises(ResourceBusy):DeviceLease('qa','second',self.root/'leases').acquire()
        first.bind('emulator-5554')
        self.assertEqual(json.loads(first.path.read_text())['serial'],'emulator-5554')
        first.release()
        second=DeviceLease('qa','second',self.root/'leases');second.acquire();second.release()

    def test_command_cancellation_preserves_request_and_returns_failure(self):
        job=self.submit();self.store.claim();worker=ValidationWorker(self.config);worker.job=job['id']
        (worker.root/'artifacts'/worker.job).mkdir(parents=True)
        self.store.cancel(job['id'])
        from validation_worker import Cancelled
        with self.assertRaises(Cancelled):worker.command([sys.executable,'-c','print(1)'],self.root,'test')


if __name__=='__main__':unittest.main()
