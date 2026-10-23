"""Check frozen source-machine parity without model calls; nonzero exit means mismatch."""
import argparse
import json
from pathlib import Path
from _bootstrap import REPO
from rsi_portable.reproducibility import verify

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path)
    p.add_argument('--software-only', action='store_true', help='Offline diagnostic only; paid start always checks system tools too')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    report = verify(args.session, args.software_only)
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
    raise SystemExit(0 if report['passed'] else 1)
