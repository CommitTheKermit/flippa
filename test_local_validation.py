import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jev_client import JevClient, DecisionFailure
from local_validation import ValidationRun, candidates_for, validate_scenario, main


def node(text='검색', **extra):
    return dict(id=0, text=text, resource='app:id/search', package='app.test',
                bounds=[0, 0, 100, 100], enabled=True, clickable=True, **extra)


def scenario():
    return {'goal': '검색 결과 확인', 'package': 'app.test',
            'allowed_actions': [{'kind': 'tap', 'selector': {'text': '검색'}, 'description': '검색 화면 열기'}],
            'expect': [{'text': '결과'}]}


class FakeDevice:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.last = []
        self.effects = []

    def observe(self):
        self.last = next(self.observations, self.last)
        return self.last

    def execute(self, candidate):
        self.effects.append(candidate)


class FakeChooser:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.calls = 0

    def choose(self, state, candidates):
        self.calls += 1
        return next(self.choices, ('blocked', 1, 1))


class RunTests(unittest.TestCase):
    def run_case(self, observations, choices, max_steps=10):
        device = FakeDevice(observations)
        chooser = FakeChooser(choices)
        result = ValidationRun(scenario(), device, chooser, max_steps=max_steps, sleep=lambda _: None).run()
        return result, device, chooser

    def test_action_and_postcondition_without_capture(self):
        result, device, chooser = self.run_case([[node()], [node()], [node('결과')]], [('a0', .99, .99)], 1)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(device.effects), 1)
        self.assertEqual(chooser.calls, 1)

    def test_already_satisfied_needs_no_model(self):
        result, _, chooser = self.run_case([[node('결과')]], [])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(chooser.calls, 0)

    def test_model_done_cannot_claim_success(self):
        result, device, _ = self.run_case([[node()]], [('done', 1, 1)])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(device.effects)

    def test_low_confidence_and_low_probability_do_not_act(self):
        for values in [('a0', .4, .99), ('a0', .99, .4)]:
            result, device, _ = self.run_case([[node()]], [values])
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse(device.effects)

    def test_stale_observation_does_not_click_old_coordinates(self):
        changed = node('새 화면')
        result, device, _ = self.run_case([[node()], [changed]], [('a0', 1, 1), ('blocked', 1, 1)])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(device.effects)

    def test_ineffective_action_is_not_repeated(self):
        result, device, _ = self.run_case([[node()]], [('a0', 1, 1)] * 5)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(device.effects), 1)

    def test_wait_loop_is_bounded(self):
        result, _, chooser = self.run_case([[node()]], [('wait', 1, 1)] * 10)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(chooser.calls, 2)

    def test_duplicate_or_disabled_target_is_not_offered(self):
        self.assertFalse(candidates_for([node(), node()], scenario()))
        disabled = node(); disabled['enabled'] = False
        self.assertFalse(candidates_for([disabled], scenario()))

    def test_unknown_model_action_is_not_executed(self):
        result, device, _ = self.run_case([[node()]], [('shell', 1, 1)])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(device.effects)

    def test_text_requires_focused_editable_target(self):
        spec = scenario(); spec['allowed_actions'][0].update(kind='text', text='test')
        self.assertFalse(candidates_for([node()], spec))
        self.assertTrue(candidates_for([node(editable=True, focused=True)], spec))

    def test_validate_rejects_unsupported_or_secret_input(self):
        for value in ['한글', 'test; rm', 'password=secret']:
            spec = scenario(); spec['allowed_actions'][0].update(kind='text', text=value)
            with self.assertRaises(ValueError):
                validate_scenario(spec)

    def test_provider_key_is_redacted_from_observations(self):
        import device
        sample = 'apikey_' + 'a' * 32 + '_' + 'b' * 64
        self.assertEqual(device.redact(sample), '[REDACTED]')

    def test_dry_run_does_not_contact_device_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scenario.json'; path.write_text(json.dumps(scenario()))
            with patch('local_validation.AndroidDevice') as device, patch('local_validation.JevClient') as client:
                self.assertEqual(main(['--scenario', str(path), '--serial', 'emulator-5554', '--output', directory+'/out']), 0)
                device.assert_not_called(); client.assert_not_called()


class ClientTests(unittest.TestCase):
    def answer(self, confidence=.99, probability=.99, choice='wait'):
        return {'answers': {'action': {'type': 'choice', 'choice': choice,
                'confidence': confidence, 'probabilities': {choice: probability}}},
                'usage': {'input_tokens': 10, 'output_tokens': 2}}

    def request(self, answer):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(answer).encode()
            client = JevClient('test-only-placeholder')
            result = client.choose({'goal': 'test'}, {})
            return client, result

    def test_real_response_shape_and_usage(self):
        client, result = self.request(self.answer())
        self.assertEqual(result, ('wait', .99, .99))
        self.assertEqual(client.usage, {'input_tokens': 10, 'output_tokens': 2})

    def test_invalid_provider_decisions_fail_closed(self):
        for answer in [self.answer(confidence=float('nan')), self.answer(probability=True),
                       self.answer(choice='shell'), {}, self.answer(confidence=2)]:
            with self.assertRaises(DecisionFailure):
                self.request(answer)

    def test_provider_error_body_is_not_exposed(self):
        import urllib.error
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('https://api.typesafe.ai', 401, 'secret', {}, None)
            with self.assertRaisesRegex(DecisionFailure, 'HTTP 401') as caught:
                JevClient('test-only-placeholder').choose({}, {})
            self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
