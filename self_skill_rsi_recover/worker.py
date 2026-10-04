"""Reuse the unchanged solver/update implementation with audited environments."""
import argparse
from contextlib import contextmanager
from pathlib import Path

from self_skill_rsi_expand import worker as original
from . import campaign


def run(root, arm, uid, port, output):
    original.c = campaign
    from terminal_offline_recover import grading
    original.grading = grading
    original.resilient.grading = grading
    adapter = original.adapter
    @contextmanager
    def recovered_adapter(root, m):
        with adapter(root, m):
            command = original.clean.harbor_command
            def updated(*a, **kw):
                return [x.replace('self_skill_rsi_resilient.harbor:ResilientVerifier',
                                  'terminal_offline_recover.harbor:RecoveryVerifier') for x in command(*a, **kw)]
            original.clean.harbor_command = updated
            try: yield
            finally: original.clean.harbor_command = command
    original.adapter = recovered_adapter
    return original.run(root, arm, uid, port, output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    p.add_argument('--arm', required=True, choices=campaign.ARMS); p.add_argument('--uid', required=True)
    p.add_argument('--port', required=True, type=int); p.add_argument('--output', required=True, type=Path)
    a=p.parse_args(); run(a.root,a.arm,a.uid,a.port,a.output)
