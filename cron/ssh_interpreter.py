"""Remote shell selection after script realpath; preserves the upstream Python-only contract."""

import shlex


def selection_command(interpreter):
    raw = interpreter.strip() if isinstance(interpreter, str) else ""
    if not raw:
        return 'case "$p" in *.[sS][hH]|*.[bB][aA][sS][hH]) interpreter=bash;; *) interpreter=python3;; esac; '
    program = r"""import os,re,sys
from pathlib import Path
raw=sys.argv[1]
if not (raw.startswith('/') or raw.startswith('~/')):raise ValueError('Interpreter must be absolute or ~-prefixed')
p=Path(raw).expanduser(); resolved=p.resolve(strict=True)
pattern=re.compile(r'^python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?$',re.I)
if not all(pattern.fullmatch(x.name) for x in (p,resolved)):raise ValueError('Interpreter must be Python')
if not p.is_file() or not os.access(p,os.X_OK):raise ValueError('Interpreter must be executable')
print(p)
"""
    return (
        'case "$p" in *.[sS][hH]|*.[bB][aA][sS][hH]) interpreter=bash;; *) '
        "interpreter=$(python3 -c "
        + shlex.quote(program)
        + " "
        + shlex.quote(raw)
        + ") || exit 74;; esac; "
    )
