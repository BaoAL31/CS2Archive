"""Fail when the test suite has failures that are not in tests/known_failures.txt.

CR-13. Baseline semantics:
  * NEW id                     -> exit 1 (the point of the gate)
  * listed id changes STATUS   -> exit 1 (FAILED -> ERROR means collection/fixture breakage, which
                                  is a regression even though the id was already known)
  * listed id now passes       -> pass, warn to shrink the list
  * environment not comparable -> SKIPPED, exit 0 (never emit a wall of false "NEW" failures)

Environment comparability matters: the baseline was measured with the gitignored runtime state
(.data/, .cache/) present and with the project dependency set installed. Without those, the suite's
failure set is different for reasons unrelated to code, so comparing would be pure noise.

Usage:
  python .github/scripts/check_known_failures.py                # run pytest, then compare
  python .github/scripts/check_known_failures.py --update       # rewrite the list from a run
  python .github/scripts/check_known_failures.py --reuse FILE   # compare against a saved log
  python .github/scripts/check_known_failures.py --force        # compare even if the env looks wrong
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / 'tests' / 'known_failures.txt'
ANSI = re.compile(r'\x1b\[[0-9;]*m')
BS = chr(92)

HEADER = """# tests/known_failures.txt
#
# CR-13 (expires 2026-11-15) — pre-existing test failures, baselined so a NEW failure fails the
# build instead of hiding in the noise.
#
# Format: one `<STATUS> <test id>` per line, STATUS being FAILED or ERROR, exactly as pytest
# reports it. The status is compared too: a known id that flips from FAILED to ERROR fails the
# check, because that means collection or fixture breakage rather than the same known defect.
#
# How to use:
#   python .github/scripts/check_known_failures.py            # strict: fail on NEW or flip
#   python .github/scripts/check_known_failures.py --update   # rewrite from a run
#
# Rules (agreed with the review council):
#   * Entries may only be listed with a CR-<n> ticket named here and an expiry date above;
#     .github/scripts/check_waivers.py enforces both, and that the ticket exists in the register.
#   * The safe direction (listed but passing) is a warning, never a failure, and never rewritten
#     silently.
#
# Clusters, so this file does not read as N unrelated silences:
#   * Five entries in test_build_short_timeline.py share one root cause — tracked as CR-17.
#   * Four entries in test_render_shorts.py reference renamed killfeed constants and fail on
#     AttributeError (the source module no longer defines them) — CR-15/CR-03 territory.
#
# NOTE: measured WITH the gitignored runtime state present (.data/, .cache/). Without it, this
# script reports SKIPPED instead of comparing.
"""


def normalise(line: str) -> str:
    s = re.sub(r' - .*$', '', line)
    s = s.removeprefix('FAILED ').removeprefix('ERROR ')
    return s.replace(BS, '/').strip()


def parse_statuses(text: str) -> dict[str, str]:
    """id -> STATUS from pytest output."""
    text = ANSI.sub('', text)
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        for status in ('FAILED', 'ERROR'):
            if stripped.startswith(status + ' '):
                n = normalise(stripped)
                if n:
                    out[n] = status
                break
    return out


def read_list() -> dict[str, str]:
    out: dict[str, str] = {}
    if not LIST.exists():
        return out
    for line in LIST.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        parts = s.split(None, 1)
        if len(parts) == 2 and parts[0] in ('FAILED', 'ERROR'):
            out[parts[1].replace(BS, '/')] = parts[0]
        else:
            out[s.replace(BS, '/')] = 'FAILED'   # tolerate a bare id
    return out


def write_list(observed: dict[str, str]) -> None:
    body = '\n'.join('%s %s' % (observed[k], k) for k in sorted(observed))
    LIST.write_text(HEADER + '\n# --- measured %s: %d entries ---\n\n%s\n'
                    % (dt.date.today().isoformat(), len(observed), body), encoding='utf-8')


def env_comparable() -> list[str]:
    """Return the reasons the environment cannot be compared, if any."""
    reasons: list[str] = []
    probe = subprocess.run([sys.executable, '-c', 'import pandas, numpy'],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        reasons.append('interpreter %s lacks pandas/numpy' % sys.executable)
    if not (ROOT / '.data').is_dir():
        reasons.append('.data/ is missing (gitignored runtime state)')
    return reasons


def run_pytest() -> str:
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests', '-q', '--no-header',
         '-p', 'no:cacheprovider', '--tb=no', '-rf'],
        cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace')
    return (proc.stdout or '') + (proc.stderr or '')


def main() -> int:
    args = sys.argv[1:]
    if '--reuse' in args:
        log = Path(args[args.index('--reuse') + 1]).read_text(encoding='utf-8', errors='replace')
    else:
        print('running pytest ...', flush=True)
        log = run_pytest()

    observed = parse_statuses(log)
    summary = next((l for l in ANSI.sub('', log).splitlines() if re.search(r'\d+ (failed|passed)', l)), '?')

    if '--update' in args:
        write_list(observed)
        print('updated %s with %d entr(ies)' % (LIST, len(observed)))
        return 0

    reasons = env_comparable()
    if reasons and '--force' not in args:
        print('pytest: %s' % summary)
        print('SKIPPED — environment is not comparable to the baseline:')
        for r in reasons:
            print('  - ' + r)
        print('Run this in the project env with .data/ present (or pass --force to compare anyway).')
        return 0

    listed = read_list()
    new = sorted(set(observed) - set(listed))
    gone = sorted(set(listed) - set(observed))
    flips = sorted(i for i in set(observed) & set(listed) if observed[i] != listed[i])

    print('pytest: %s' % summary)
    print('observed: %d   listed: %d   new: %d   status flips: %d   no-longer-failing: %d'
          % (len(observed), len(listed), len(new), len(flips), len(gone)))

    if gone:
        print('\nwarning: listed but now passing — shrink tests/known_failures.txt:')
        for g in gone:
            print('  - %s' % g)
    if flips:
        print('\nFAILED — %d known id(s) changed status (collection/fixture breakage):' % len(flips))
        for f in flips:
            print('  - %s: %s -> %s' % (f, listed[f], observed[f]))
    if new:
        print('\nFAILED — %d failure(s) not in tests/known_failures.txt:' % len(new))
        for n in new:
            print('  - %s %s' % (observed[n], n))
        print('\nIf it is legitimate pre-existing debt, add it under a CR-<n> ticket; otherwise fix it.')
    if new or flips:
        return 1
    print('\nOK — no new failures and no status flips.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
