import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_validation import main


class WrapperTests(unittest.TestCase):
    def test_delegates_with_required_agent_coordination(self):
        with patch('jev_mobile_use.cli.main', return_value=2) as run:
            self.assertEqual(main(['--scenario', 'case.json']), 2)
            run.assert_called_once_with(['--scenario', 'case.json', '--coordination', 'agent'])

    def test_caller_cannot_replace_existing_agent_guard(self):
        with patch('jev_mobile_use.cli.main', return_value=0) as run:
            main(['--coordination', 'standalone'])
            self.assertEqual(run.call_args.args[0][-2:], ['--coordination', 'agent'])

    def test_missing_dependency_reports_installation_without_traceback(self):
        with patch.dict('sys.modules', {'jev_mobile_use.cli': None}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main([]), 2)
            self.assertIn('requirements-jev.txt', err.getvalue())

    def test_existing_example_still_validates_without_device(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--scenario', str(Path(__file__).parent/'examples/local-validation.json'),
                                  '--serial', 'emulator-5554', '--output', directory+'/out']), 0)
            self.assertEqual(json.loads(out.getvalue())['status'], 'validated')
