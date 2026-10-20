from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_update import ApkInspector, AppUpdater


class AppUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)/'chaekchaek'
        (self.repo/'.git').mkdir(parents=True)
        (self.repo/'android').mkdir()
        self.wrapper = self.repo/'android'/('gradlew.bat' if os.name == 'nt' else 'gradlew')
        self.wrapper.write_text('wrapper')
        apk = self.repo/'android/app/build/outputs/apk/debug/app-debug.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'apk')
        self.settings = {'PLIPA_APP_REPO': str(self.repo), 'PLIPA_APP_BRANCH': 'an-develop'}
        self.metadata = {'package': 'com.chamsae.chaekchaek', 'version_name': '1.3.0',
                         'version_code': 10, 'signer_sha256': 'a' * 64}
        inspector = patch('app_update.ApkInspector.inspect', return_value=self.metadata)
        inspector.start()
        self.addCleanup(inspector.stop)

    def wait(self, updater):
        updater.worker.join(2)
        self.assertFalse(updater.worker.is_alive())
        return updater.status()

    def test_update_fast_forwards_builds_installs_and_launches(self):
        updater = AppUpdater(self.settings)
        replies = {
            ('status', '--porcelain', '--untracked-files=no'): '',
            ('branch', '--show-current'): 'an-develop',
            ('rev-parse', '--short=12', 'HEAD'): 'abc123def456',
            ('fetch', '--prune', 'origin', 'an-develop'): '',
            ('merge', '--ff-only', 'origin/an-develop'): '',
        }
        revisions = iter(['abc123def456', 'fed987cba654'])
        def git(*args, **_):
            if args == ('rev-parse', '--short=12', 'HEAD'):
                return next(revisions)
            return replies[args]
        with patch.object(updater, '_git', side_effect=git), \
             patch.object(updater, '_command', return_value='BUILD SUCCESSFUL') as command, \
             patch('app_update.subprocess.run', return_value=Mock(returncode=0)), \
             patch('app_update.device.devices', return_value=[{'serial':'emulator-5554','state':'device'}]), \
             patch('app_update.device.adb') as adb, patch('app_update.device.launch') as launch, \
             patch('app_update.device.environment', return_value={'app':'versionName=1.1.1'}):
            updater.start('emulator-5554')
            status = self.wait(updater)
        self.assertEqual((status['phase'], status['revision'], status['source_changed']),
                         ('done', 'fed987cba654', True))
        command.assert_called_once()
        self.assertEqual(command.call_args.args[:2],
                         ([str(self.wrapper), ':app:assembleDebug',
                           '--console=plain', '--no-daemon'], updater.repo/'android'))
        self.assertEqual(adb.call_args.args[:3], ('emulator-5554', 'install', '-r'))
        self.assertEqual(Path(adb.call_args.args[3]).name, 'update.apk')
        self.assertEqual(status['signer_sha256'], 'a' * 64)
        launch.assert_called_once_with('emulator-5554', 'com.chamsae.chaekchaek')

    def test_dirty_checkout_stops_before_fetch_build_or_install(self):
        updater = AppUpdater(self.settings)
        with patch.object(updater, '_git', return_value=' M android/app/build.gradle.kts') as git, \
             patch.object(updater, '_command') as command, \
             patch('app_update.device.devices', return_value=[{'serial':'emulator-5554','state':'device'}]), \
             patch('app_update.device.adb') as adb:
            updater.start('emulator-5554')
            status = self.wait(updater)
        self.assertEqual(status['phase'], 'error')
        self.assertIn('커밋하지 않은 변경', status['message'])
        git.assert_called_once_with('status', '--porcelain', '--untracked-files=no')
        command.assert_not_called()
        adb.assert_not_called()

    def test_failed_build_does_not_replace_installed_app(self):
        updater = AppUpdater(self.settings)
        revisions = iter(['abc123def456', 'abc123def456'])
        def git(*args, **_):
            if args == ('status', '--porcelain', '--untracked-files=no'): return ''
            if args == ('branch', '--show-current'): return 'an-develop'
            if args == ('rev-parse', '--short=12', 'HEAD'): return next(revisions)
            return ''
        with patch.object(updater, '_git', side_effect=git), \
             patch.object(updater, '_command', side_effect=ValueError('Gradle 빌드 실패')), \
             patch('app_update.subprocess.run', return_value=Mock(returncode=0)), \
             patch('app_update.device.devices', return_value=[{'serial':'emulator-5554','state':'device'}]), \
             patch('app_update.device.adb') as adb, patch('app_update.device.launch') as launch:
            updater.start('emulator-5554')
            status = self.wait(updater)
        self.assertEqual((status['phase'], status['message']), ('error', 'Gradle 빌드 실패'))
        adb.assert_not_called()
        launch.assert_not_called()

    def test_rejects_unconfigured_device_and_unsafe_paths(self):
        with self.assertRaisesRegex(ValueError, '상대 경로'):
            AppUpdater({**self.settings, 'PLIPA_APP_APK': '../secret.apk'})
        updater = AppUpdater(self.settings)
        with patch('app_update.device.devices', return_value=[]):
            with self.assertRaisesRegex(ValueError, '선택'):
                updater.start('emulator-5554')

    def build(self, install_error=None):
        updater = AppUpdater(self.settings)
        self.addCleanup(updater._discard_candidate)
        def git(*args, **_):
            if args == ('branch', '--show-current'): return 'an-develop'
            if args == ('rev-parse', '--short=12', 'HEAD'): return 'abc123def456'
            return ''
        def pull(serial, directory):
            apk = directory/'installed.apk'
            apk.write_bytes(b'existing installed app')
            return apk
        with patch.object(updater, '_git', side_effect=git), \
             patch.object(updater, '_command', return_value='BUILD SUCCESSFUL'), \
             patch('app_update.subprocess.run', return_value=Mock(returncode=0)), \
             patch('app_update.device.devices', return_value=[{'serial':'emulator-5554','state':'device'}]), \
             patch.object(updater, '_installed_apk', side_effect=pull), \
             patch.object(updater, '_device_identity', return_value='0123456789abcdef'), \
             patch('app_update.device.adb', side_effect=install_error), \
             patch('app_update.device.launch'), patch('app_update.device.environment', return_value={'app': '1.3.0'}):
            updater.start('emulator-5554')
            self.wait(updater)
        return updater

    def mismatch(self):
        return self.build(ValueError('INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match'))

    def test_signature_mismatch_offers_confirmation_without_deleting_or_launching(self):
        updater = self.mismatch()
        status = updater.status()
        self.assertEqual(status['phase'], 'signature_mismatch')
        self.assertTrue(status['reinstall_token'])
        self.assertFalse(status['running'])
        self.assertIn('기기 내 앱 데이터가 삭제', status['message'])
        self.assertTrue(updater.candidate['apk'].is_file())
        # The snapshot is independent of subsequent Gradle output changes.
        (updater.repo/updater.apk).write_bytes(b'another build')
        self.assertEqual(updater.candidate['apk'].read_bytes(), b'apk')

    def test_other_install_failures_never_offer_destructive_reinstall(self):
        updater = self.build(ValueError('INSTALL_FAILED_VERSION_DOWNGRADE'))
        self.assertEqual(updater.status()['phase'], 'error')
        self.assertIsNone(updater.candidate)

    def test_unexpected_apk_package_stops_before_install_or_delete(self):
        self.metadata['package'] = 'com.other.app'
        updater = self.build()
        self.assertIn('패키지', updater.status()['message'])
        self.assertEqual(updater.status()['phase'], 'error')
        self.assertIsNone(updater.candidate)

    def test_reinstall_rejects_unconfirmed_wrong_target_or_wrong_token(self):
        updater = self.mismatch()
        token = updater.status()['reinstall_token']
        with patch('app_update.device.adb') as adb:
            for serial, supplied, consent in [('emulator-5556', token, True),
                                             ('emulator-5554', 'stale', True),
                                             ('emulator-5554', '잘못된 확인', True),
                                             ('emulator-5554', token, None),
                                             ('emulator-5554', token, 'true')]:
                with self.assertRaises(ValueError):
                    updater.confirm_reinstall(serial, supplied, consent)
            adb.assert_not_called()
        self.assertIsNotNone(updater.candidate)

    def test_cancel_consumes_confirmation_and_preserves_installed_app(self):
        updater = self.mismatch()
        token = updater.status()['reinstall_token']
        with patch('app_update.device.adb') as adb:
            status = updater.confirm_reinstall('emulator-5554', token, False)
            self.assertEqual(status['phase'], 'cancelled')
            with self.assertRaises(ValueError):
                updater.confirm_reinstall('emulator-5554', token, True)
            adb.assert_not_called()
        self.assertIsNone(updater.artifact)

    def test_expired_confirmation_does_not_delete(self):
        updater = self.mismatch()
        updater.candidate['expires'] = 0
        with patch('app_update.device.adb') as adb:
            with self.assertRaisesRegex(ValueError, '만료'):
                updater.confirm_reinstall('emulator-5554', updater.status()['reinstall_token'], True)
            adb.assert_not_called()

    def reinstall(self, updater, existing=b'existing installed app', failure=None):
        def pull(serial, directory):
            apk = directory/'installed.apk'
            apk.write_bytes(existing)
            return apk
        def adb(*args, **_):
            if failure and args[1] == failure:
                raise ValueError('기기 연결 끊김')
            return 'Success'
        with patch.object(updater, '_installed_apk', side_effect=pull), \
             patch('app_update.device.adb', side_effect=adb) as command, \
             patch.object(updater, '_device_identity', return_value='0123456789abcdef'), \
             patch('app_update.device.launch') as launch, \
             patch('app_update.device.environment', return_value={'app': 'versionName=1.3.0'}):
            token = updater.status()['reinstall_token']
            updater.confirm_reinstall('emulator-5554', token, True)
            self.wait(updater)
        return command, launch, token

    def test_confirmed_reinstall_deletes_only_bound_package_and_installs_snapshot_once(self):
        updater = self.mismatch()
        snapshot = str(updater.candidate['apk'])
        command, launch, token = self.reinstall(updater)
        self.assertEqual([call.args for call in command.call_args_list],
                         [('emulator-5554', 'uninstall', 'com.chamsae.chaekchaek'),
                          ('emulator-5554', 'install', snapshot)])
        launch.assert_called_once_with('emulator-5554', 'com.chamsae.chaekchaek')
        self.assertEqual(updater.status()['phase'], 'done')
        self.assertTrue(updater.status()['data_deleted'])
        with self.assertRaises(ValueError):
            updater.confirm_reinstall('emulator-5554', token, True)

    def test_changed_artifact_or_installed_app_never_deletes(self):
        for target in ('apk', 'installed'):
            with self.subTest(target=target):
                updater = self.mismatch()
                if target == 'apk':
                    updater.candidate['apk'].write_bytes(b'replaced APK')
                command, launch, _ = self.reinstall(updater, existing=b'changed' if target == 'installed' else b'existing installed app')
                command.assert_not_called()
                launch.assert_not_called()
                self.assertEqual(updater.status()['phase'], 'error')
                self.assertFalse(updater.status()['data_deleted'])

    def test_same_serial_reused_for_different_device_does_not_delete(self):
        updater = self.mismatch()
        updater.candidate['device_identity'] = 'fedcba9876543210'
        command, launch, _ = self.reinstall(updater)
        command.assert_not_called()
        launch.assert_not_called()
        self.assertIn('기기가 바뀌었', updater.status()['message'])

    def test_partial_reinstall_failure_reports_whether_data_was_deleted(self):
        for failure, deleted in [('uninstall', None), ('install', True)]:
            with self.subTest(failure=failure):
                updater = self.mismatch()
                command, launch, _ = self.reinstall(updater, failure=failure)
                self.assertEqual(updater.status()['phase'], 'error')
                self.assertEqual(updater.status()['data_deleted'], deleted)
                if deleted:
                    self.assertIn('데이터는 삭제됐지만', updater.status()['message'])
                else:
                    self.assertEqual(command.call_count, 1)
                    self.assertIn('데이터 삭제 여부를 확인', updater.status()['message'])
                launch.assert_not_called()


class ApkInspectorTest(unittest.TestCase):
    def test_reads_verified_signers_and_package_from_android_build_tools(self):
        badging = "package: name='com.chamsae.chaekchaek' versionCode='10' versionName='1.3.0' platformBuildVersionName=''"
        certificate = 'Signer #1 certificate SHA-256 digest: ' + 'A' * 64
        inspector = ApkInspector()
        with patch.object(inspector, '_tool', side_effect=lambda name: name), \
             patch('app_update.device.run', side_effect=[badging, certificate]) as run:
            metadata = inspector.inspect(Path('sample.apk'))
        self.assertEqual(metadata, {'package': 'com.chamsae.chaekchaek', 'version_code': 10,
                                    'version_name': '1.3.0', 'signer_sha256': 'a' * 64})
        self.assertEqual(run.call_args.args[0], ['apksigner', 'verify', '--print-certs', 'sample.apk'])

    def test_invalid_signatures_cannot_be_used_for_reinstall(self):
        inspector = ApkInspector()
        with patch.object(inspector, '_tool', side_effect=lambda name: name), \
             patch('app_update.device.run', side_effect=[
                 "package: name='com.chamsae.chaekchaek' versionCode='10' versionName='1.3.0'", ValueError('invalid signature')]):
            with self.assertRaisesRegex(ValueError, 'invalid signature'):
                inspector.inspect(Path('sample.apk'))

    def test_reads_scheme_named_signers_from_new_build_tools(self):
        inspector = ApkInspector()
        with patch.object(inspector, '_tool', side_effect=lambda name: name), \
             patch('app_update.device.run', side_effect=[
                 "package: name='com.chamsae.chaekchaek' versionCode='10' versionName='1.3.0'",
                 'V2 Signer: certificate SHA-256 digest: ' + 'b' * 64]):
            self.assertEqual(inspector.inspect(Path('sample.apk'))['signer_sha256'], 'b' * 64)


if __name__ == '__main__':
    unittest.main()
