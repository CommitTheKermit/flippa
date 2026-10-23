import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_validation import main


class WrapperTests(unittest.TestCase):
    def test_delegates_only_to_shared_launcher(self):
        with patch('local_validation.Path.is_file', return_value=True), \
             patch('local_validation.subprocess.run', return_value=SimpleNamespace(returncode=2)) as run:
            self.assertEqual(main(['--scenario', 'case.json']), 2)
            argv = run.call_args.args[0]
            self.assertEqual(argv[1], str(Path.home()/'.agents/skills/jev-local-validation/scripts/run.py'))
            self.assertEqual(argv[2:], ['--scenario', 'case.json'])
            self.assertEqual(run.call_args.kwargs, {'check': False})

    def test_runtime_info_is_forwarded_without_extra_arguments(self):
        with patch('local_validation.Path.is_file', return_value=True), \
             patch('local_validation.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(main(['--runtime-info']), 0)
            self.assertEqual(run.call_args.args[0][2:], ['--runtime-info'])

    def test_missing_launcher_never_uses_old_package(self):
        with patch('local_validation.Path.is_file', return_value=False), \
             patch('local_validation.subprocess.run') as run, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main([]), 2)
            self.assertIn('스킬을 동기화', err.getvalue())
            run.assert_not_called()
