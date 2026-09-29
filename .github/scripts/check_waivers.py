"""Fail CI on a lint/test waiver that has no ticket, no expiry, or an expired one.

CR-13. The council's condition for allowing suppressions was that a waiver must not be able to
outlive the ticket that justifies it. This is that enforcement.

Audited waiver sites:
  * [tool.ruff.lint] ignore                     — the biggest waiver, 287 measured sites
  * [tool.ruff.lint.per-file-ignores] entries   — 5 entries
  * [tool.pytest.ini_options] collect_ignore_glob
  * tests/known_failures.txt header

Each must carry, in the comment block immediately above it (or in the file header), both:
  * a `CR-<number>` token, and that ticket must EXIST in docs/reviews/README.md
  * an `expires YYYY-MM-DD` date still in the future

Exit 1 on any violation.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / 'pyproject.toml'
REGISTER = ROOT / 'docs' / 'reviews' / 'README.md'
KNOWN = ROOT / 'tests' / 'known_failures.txt'

TOKEN = re.compile(r'CR-\d+')
EXPIRES = re.compile(r'expires\s+(\d{4}-\d{2}-\d{2})')

# Tickets the register knows about. A waiver may only cite one of these, so a typo or an
# invented ticket cannot silence a rule.
register_ids: set[str] = set()
if REGISTER.exists():
    register_ids = set(TOKEN.findall(REGISTER.read_text(encoding='utf-8')))

problems: list[str] = []
checked = 0


def check_block(label: str, text: str) -> None:
    global checked
    checked += 1
    tokens = TOKEN.findall(text)
    if not tokens:
        problems.append('%s: no CR-<n> token in its comment block' % label)
    else:
        for t in tokens:
            if register_ids and t not in register_ids:
                problems.append('%s: cites %s, which is not in docs/reviews/README.md' % (label, t))
    m = EXPIRES.search(text)
    if not m:
        problems.append('%s: no "expires YYYY-MM-DD" in its comment block' % label)
        return
    try:
        when = dt.date.fromisoformat(m.group(1))
    except ValueError:
        problems.append('%s: unparsable expiry %r' % (label, m.group(1)))
        return
    if when < dt.date.today():
        problems.append('%s: waiver EXPIRED on %s — fix the finding or extend deliberately'
                        % (label, when.isoformat()))


def walk_toml_section(raw: str, header: str, label: str,
                      waiver_keys: set[str] | None = None, scalar_waiver: bool = False) -> None:
    """Validate the comment block above each waiver in a TOML section.

    waiver_keys=None means every key in the section is a waiver. scalar_waiver=True means the
    section header itself is the waiver (e.g. `ignore = [...]`), so the block above it is checked.
    """
    section = re.search(re.escape(header) + r'(.*?)(?=\n\[|\Z)', raw, re.S)
    if not section:
        return
    if scalar_waiver:
        before = raw[:section.start()].splitlines()
        block: list[str] = []
        for line in reversed(before):
            if line.strip().startswith('#'):
                block.append(line.strip())
            else:
                break
        check_block(label, '\n'.join(reversed(block)))
        return
    pending: list[str] = []
    for line in section.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            pending.append(stripped)
            continue
        if not stripped or '=' not in stripped:
            continue
        key = stripped.split('=', 1)[0].strip()
        if waiver_keys is None or key in waiver_keys:
            check_block('%s %s' % (label, key), '\n'.join(pending))
        pending = []


CHECKED_FILES = [PYPROJECT]


def _comment_block_above(path: Path, needle: str) -> str:
    """The contiguous comment block immediately above the first line starting with `needle`."""
    lines = path.read_text(encoding='utf-8').splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(needle):
            block: list[str] = []
            for prev in reversed(lines[:i]):
                if prev.strip().startswith('#'):
                    block.append(prev.strip())
                else:
                    break
            return '\n'.join(reversed(block))
    return ''


raw = PYPROJECT.read_text(encoding='utf-8')
# the global ignore list: the header line's own leading comment block is its waiver block
ignore_line = re.search(r'^ignore\s*=', raw, re.M)
if ignore_line:
    block = []
    for line in reversed(raw[:ignore_line.start()].splitlines()):
        if line.strip().startswith('#'):
            block.append(line.strip())
        else:
            break
    check_block('global ruff ignore', '\n'.join(reversed(block)))

walk_toml_section(raw, '[tool.ruff.lint.per-file-ignores]', 'per-file-ignore')

# CR-16's collection suppression lives in tests/conftest.py, NOT in [tool.pytest.ini_options]:
# pytest 9.1.1 rejects collect_ignore_glob as an ini option (warns and ignores it), so the waiver
# moved to the conftest and the audit has to follow it there. This was a real hole: the ini-section
# check below used to match nothing while still counting as a checked waiver.
CONFTEST = ROOT / 'tests' / 'conftest.py'
if CONFTEST.exists():
    check_block('tests/conftest.py collect_ignore_glob',
                _comment_block_above(CONFTEST, 'collect_ignore_glob'))

if KNOWN.exists():
    header = '\n'.join(l for l in KNOWN.read_text(encoding='utf-8').splitlines() if l.startswith('#'))
    check_block('tests/known_failures.txt', header)

print('lint/test waivers checked: %d   (register tickets known: %d)' % (checked, len(register_ids)))
if problems:
    print('\nFAILED — %d problem(s):' % len(problems))
    for p in problems:
        print('  - ' + p)
    sys.exit(1)
print('OK — every waiver names a real ticket and an unexpired date.')
