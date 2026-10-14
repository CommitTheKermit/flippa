from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_update import AppUpdater


class AppUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)/'chaekchaek'
        (self.repo/'.git').mkdir(parents=True)
        (self.repo/'android').mkdir()
        (self.repo/'android'/'gradlew').write_text('wrapper')
        apk = self.repo/'android/app/build/outputs/apk/debug/app-debug.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'apk')
        self.settings = {'PLIPA_APP_REPO': str(self.repo), 'PLIPA_APP_BRANCH': 'an-develop'}

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
                         ([str(updater.repo/'android/gradlew'), ':app:assembleDebug',
                           '--console=plain', '--no-daemon'], updater.repo/'android'))
        adb.assert_called_once_with('emulator-5554', 'install', '-r', str(updater.repo/updater.apk), timeout=300)
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


if __name__ == '__main__':
    unittest.main()
