"""Record official pytest exit codes, without changing assertions or outcomes."""
import json
import os
from pathlib import Path
import subprocess
import sys

result = subprocess.run([sys.executable, '-P', '-m', 'pytest', *sys.argv[1:]])
with (Path(os.environ['RSI_GRADING_LOG']) / 'pytest-exits.jsonl').open('a') as stream:
    stream.write(json.dumps({'arguments': sys.argv[1:], 'exit_code': result.returncode}) + '\n')
sys.exit(result.returncode)
