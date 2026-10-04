#!/bin/sh
# Support both containerd's create and BuildKit's run entry points.
bundle=
previous=
prepare=0
for argument in "$@"; do
    if [ "$previous" = "--bundle" ] || [ "$previous" = "-b" ]; then
        bundle=$argument
    fi
    case "$argument" in
        create|run) prepare=1 ;;
        --bundle=*) bundle=${argument#--bundle=} ;;
    esac
    previous=$argument
done
if [ "$prepare" = 1 ] && [ -n "$bundle" ]; then
    /usr/bin/python3 - "$bundle/config.json" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
spec = json.loads(path.read_text())
linux = spec.setdefault('linux', {})
linux.pop('resources', None)
linux['cgroupsPath'] = ''
for mount in spec.get('mounts', []):
    if mount.get('type') == 'devpts':
        mount['options'] = [x for x in mount.get('options', []) if not x.startswith('gid=')]
path.write_text(json.dumps(spec))
PY
    [ "$?" = 0 ] || exit 1
fi
exec /home/ytzhang/budget-rl/.deps/docker-static/runc --rootless=true "$@"
