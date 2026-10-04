import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('active_review',
    Path(__file__).resolve().parents[1] / 'scripts/review-active-self-skill.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ActiveReviewTests(unittest.TestCase):
    def test_read_only_noninteractive_cli(self):
        command = review.codex_command(Path('/tmp/review.txt'))
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertEqual(command[command.index('--ask-for-approval') + 1], 'never')
        self.assertIn('--ephemeral', command)
        self.assertNotIn('--approve-for-me', command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)

    def test_complete_needs_verified_finished_updates(self):
        status = dict(state='complete', total_tasks=112, completed_steps=112, seals_valid=True)
        self.assertTrue(review.completed(status))
        self.assertFalse(review.completed(dict(status, seals_valid=False)))
        self.assertFalse(review.completed(dict(status, completed_steps=111)))
        self.assertFalse(review.completed(dict(status, state='awaiting_environment')))
        self.assertFalse(review.completed({}))

    def test_compact_preserves_relevant_chains_without_mutation(self):
        original = {'chains': {'done': {'scored': 5, 'complete': True},
                                'pending': {'scored': 0, 'complete': False}}}
        value = review.compact(original)
        self.assertEqual(list(value['chains']), ['done'])
        self.assertEqual(value['chain_totals'], {'total': 2, 'completed': 1})
        self.assertIn('pending', original['chains'])

    def test_arbitrary_root_rejected(self):
        with self.assertRaises(ValueError):
            review.active_root({'root': '/tmp'})

    def test_atomic_receipt_and_real_turn_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipt.json'
            review.write_json(path, {'state': 'reviewing'})
            self.assertEqual(review.read_json(path)['state'], 'reviewing')
            self.assertFalse(path.with_name('receipt.json.tmp').exists())
            events = Path(directory) / 'events.jsonl'
            review.write_text(events, '\n'.join(json.dumps(e) for e in [
                {'type': 'thread.started', 'thread_id': 'test-thread'},
                {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}]))
            result = review.event_receipt(events)
            self.assertTrue(result['turn_completed'])
            self.assertEqual(result['thread_id'], 'test-thread')
            self.assertEqual(result['usage']['output_tokens'], 2)


if __name__ == '__main__':
    unittest.main()
