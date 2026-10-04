"""secrets-check.yml must supply every key check_secrets_present.py audits.

The audit script reads keys from os.environ. A key listed in its KEYS table but
never mapped into the workflow's `env:` block therefore reports MISSING whether
or not the secret exists -- a false negative in the one tool the repo has for
answering "is this key actually reaching Actions?".

That is not hypothetical. COINGECKO_API_KEY was added to KEYS without being
mapped into the workflow, so the audit would have reported it missing even on a
repo that had the secret set correctly. The two files have to be edited
together and nothing enforced that, so this test does.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_secrets_present.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "secrets-check.yml"

# ("NAME", "unlocks", "where") -- first element of each KEYS row.
_KEYS_ROW = re.compile(r'^\s*\(\s*"([A-Z0-9_]+)"', re.MULTILINE)
# NAME: ${{ secrets.NAME }} inside the job's env: block.
_ENV_ROW = re.compile(r'^\s*([A-Z0-9_]+)\s*:\s*\$\{\{\s*secrets\.', re.MULTILINE)


def _audited() -> set[str]:
    return set(_KEYS_ROW.findall(SCRIPT.read_text()))


def _supplied() -> set[str]:
    return set(_ENV_ROW.findall(WORKFLOW.read_text()))


def test_every_audited_key_is_supplied_by_the_workflow():
    missing = sorted(_audited() - _supplied())
    assert not missing, (
        "check_secrets_present.py audits these keys but secrets-check.yml never "
        "maps them into env, so the audit will report them MISSING even when the "
        "secret is set: " + ", ".join(missing)
    )


def test_workflow_supplies_nothing_the_audit_ignores():
    """The reverse direction: a mapped key the script does not audit is dead
    config, and more importantly hides the fact that nothing reports on it."""
    extra = sorted(_supplied() - _audited())
    assert not extra, (
        "secrets-check.yml maps these into env but check_secrets_present.py does "
        "not audit them, so nothing reports their status: " + ", ".join(extra)
    )


def test_the_tables_are_non_trivial():
    """Guard against a regex that silently matches nothing and passes both
    assertions above by comparing two empty sets."""
    assert len(_audited()) >= 10, f"parsed only {len(_audited())} audited keys"
    assert len(_supplied()) >= 10, f"parsed only {len(_supplied())} supplied keys"
