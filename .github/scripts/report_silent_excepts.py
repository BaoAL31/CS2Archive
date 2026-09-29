"""CR-09 ratchet: report silent-except debt and fail only when it GROWS.

The council's decision (D9a): S110/BLE001 is a NON-blocking advisory in the normal sense — the
gate does not require 445 fixes today — but it is a *ratchet*: CI fails when the count rises above
the recorded ceiling. That way the debt can only shrink, without a 445-site refactor blocking
everything.

Ceiling lives in `.github/silent_except_ceiling.txt` (one integer + comments).

Usage:
  python .github/scripts/report_silent_excepts.py            # run ruff, compare to the ceiling
  python .github/scripts/report_silent_excepts.py --reuse F  # parse a saved ruff --statistics output
  python .github/scripts/report_silent_excepts.py --update   # lower/raise the ceiling to today's count
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CEILING_FILE = ROOT / '.github' / 'silent_except_ceiling.txt'
ANSI = re.compile(r'\x1b\[[0-9;]*m')
RULE = re.compile(r'^\s*(\d+)\s+(S110|BLE001)\b', re.M)
RUFF = 'ruff@0.16.9'


def read_ceiling() -> int | None:
    if not CEILING_FILE.exists():
        return None
    for line in CEILING_FILE.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if s and not s.startswith('#') and s.isdigit():
            return int(s)
    return None


def ruff_stats() -> str:
    proc = subprocess.run(['uvx', RUFF, 'check', '--select', 'S110,BLE001', '--statistics', '.'],
                          cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8',
                          errors='replace')
    return (proc.stdout or '') + (proc.stderr or '')


def main() -> int:
    args = sys.argv[1:]
    if '--reuse' in args:
        text = Path(args[args.index('--reuse') + 1]).read_text(encoding='utf-8', errors='replace')
    else:
        print('measuring with %s ...' % RUFF, flush=True)
        text = ruff_stats()
    text = ANSI.sub('', text)

    counts = {m.group(2): int(m.group(1)) for m in RULE.finditer(text)}
    total = sum(counts.values())
    if not counts:
        print('could not parse ruff statistics; output was:')
        print(text[:500])
        return 2

    ceiling = read_ceiling()
    print('silent-except debt now: %d  (%s)' % (total, ', '.join('%s=%d' % kv for kv in sorted(counts.items()))))

    if '--update' in args:
        CEILING_FILE.write_text(
            '# CR-09 (expires 2026-11-15) — ceiling for S110 (try/except/pass) + BLE001 (blind except).\n'
            '# CI fails when the measured count EXCEEDS this number, so the debt can only shrink.\n'
            '# Lower it deliberately as sites are fixed: report_silent_excepts.py --update\n'
            '%d\n' % total, encoding='utf-8')
        print('ceiling updated to %d' % total)
        return 0

    if ceiling is None:
        print('no ceiling recorded; writing %d' % total)
        CEILING_FILE.write_text(
            '# CR-09 (expires 2026-11-15) — ceiling for S110 (try/except/pass) + BLE001 (blind except).\n'
            '# CI fails when the measured count EXCEEDS this number, so the debt can only shrink.\n'
            '# Lower it deliberately as sites are fixed: report_silent_excepts.py --update\n'
            '%d\n' % total, encoding='utf-8')
        print('ceiling recorded: %d' % total)
        return 0

    delta = total - ceiling
    if delta > 0:
        print('\nFAILED — silent-except debt GREW by %d (ceiling %d).' % (delta, ceiling))
        print('Fix the new site(s), or raise the ceiling deliberately with --update and say why.')
        return 1
    if delta < 0:
        print('OK — %d below the ceiling. Lower it with --update to lock the gain.' % -delta)
        return 0
    print('OK — exactly at the ceiling.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
