#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Student hand-in submission script.

Collects and validates submission information locally, then creates a
confidential GitLab issue in the central course repository. A server-side
webhook handles downstream processing (group lookup, title formatting,
milestone assignment, etc.).

Supports code submissions (git-based) and report submissions (PDF upload)
with per-type configuration via handin.toml.

Usage:
    python3 handin.py [<hand-in id>] [--dry-run]

Without an id the tool shows a menu of everything the course accepts. With an
id it goes straight to that hand-in; an unknown id is refused before any
network or git work, with the valid ids listed.

The tool is standard library only, so every form works: `python3 handin.py`,
`uv run handin.py` and `uv run python handin.py`. That is deliberate — the
first hand-in in TDDE82 is in week A3 and uv is introduced in 4B, the week
after, so the tool must not need uv.

The script must be run from inside a git repository with an 'origin' remote
pointing to the student's GitLab project.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import getpass
import json
import io
import os
import platform
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import shutil
import tempfile
import time
import traceback
import unicodedata
import webbrowser
from dataclasses import dataclass
from contextlib import contextmanager
from operator import itemgetter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

# ─── Python version guard ───────────────────────────────────────────────────
#
# This must run before anything course-specific is imported, so that a too-old
# Python produces a readable instruction instead of a raw traceback. Keep this
# block free of modern syntax: it has to stay parseable on old interpreters.

if sys.version_info < (3, 12):
    print("This tool needs Python 3.12 or newer - see the course installation guide.")
    print("You are running Python " + sys.version.split()[0] + ".")
    sys.exit(1)



def _degrade_unencodable_output(stream: Any) -> None:
    """Print '?' for a character the stream cannot encode, instead of dying."""
    try:
        stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        # Not a reconfigurable text stream (captured output, a redirected
        # StringIO). Nothing to harden, and nothing that can crash on cp1252.
        pass


for _stream in (sys.stdout, sys.stderr):
    _degrade_unencodable_output(_stream)


# ─── Companion modules ──────────────────────────────────────────────────────
#
# `handin.py` is not a single file. It needs `handin_config.py` and
# `handin_api.py` next to it, and a fork that shipped only `handin.py` used to
# fail with a raw ModuleNotFoundError traceback — which is exactly the
# "the tool does not start" experience this tool exists to prevent. Say which
# file is missing and where it belongs instead.

try:
    from handin_api import (
        ApiAuth,
        ApiClient,
        ApiError,
        ApiNotFound,
        ApiTransport,
        client_from_env,
        encode_path,
        tls_context,
    )
    from handin_config import (
        Deadline,
        HandinConfig,
        HandinType,
        apply_config_update,
        check_script_version,
        describe_config_update,
        fetch_remote_config_text,
        find_config_file,
        issue_is_handin,
        submission_section,
        is_decline_answer,
        load_config,
        normalise_confirmation_answer,
        parse_deadline_date,
    )
except ImportError as _exc:
    if isinstance(_exc, ModuleNotFoundError):
        print(f"Error: a file this tool needs is missing: {_exc.name}.py")
        print()
        print("  handin.py needs handin_config.py and handin_api.py in the SAME")
        print("  directory. Check that your repository has all of them; if not,")
        print("  contact the course staff — this is a packaging problem, not")
        print("  something you did wrong.")
    else:
        print(f"Error: a file this tool needs is out of date: {_exc.name}.py")
        print()
        print("  handin.py, handin_config.py and handin_api.py must come from")
        print("  the same version. Update your repository from the course")
        print("  template so all of them are current; if that does not help,")
        print("  contact the course staff — this is a packaging problem, not")
        print("  something you did wrong.")
    sys.exit(1)

# ─── Constants ──────────────────────────────────────────────────────────────

SCRIPT_VERSION = "2.17.1"

# Språket som msg() använder när ingen config finns i scope. Sätts av main()
# direkt efter load_config(); se msg().
MESSAGE_LANGUAGE = "en"

# Svaren på en ja/nej-fråga, BÅDA språken i BÅDA riktningarna. De står som
# konstanter och inte i MESSAGES därför att de inte är text som visas: en
# student som kör en svensk konfiguration ska kunna svara "y", och en som kör
# en engelsk ska kunna svara "ja", oavsett vilket språk frågan ställdes på.
# Jämförelsen sker mot `input().strip().lower()`.
YES_ANSWERS = frozenset({"y", "yes", "j", "ja"})
NO_ANSWERS = frozenset({"n", "no", "nej"})

# Draft cache directory
DRAFT_CACHE_DIR = Path.home() / ".cache" / "handin"

# Things that went wrong but did NOT stop the hand-in, collected so the run can
# end by naming them. A warning printed in the middle of a long run scrolls
# away; the last screen is the one the student reads and the one they screenshot
# for a TA. Appended to by the step that noticed, printed once by
# `main`.
SUBMISSION_WARNINGS: list[str] = []

SUBMISSION_PROGRESS: dict[str, Any] = {}

HANDIN_KIND_NEW = "new"
HANDIN_KIND_RESUBMISSION = "resubmission"
HANDIN_KIND_UNKNOWN = "unknown"

# What step 4 actually did, for `handin_kind`. Keys: "searched" (True only when
# the search for earlier hand-ins of this id completed), "previous_state" (the
# state, "opened"/"closed", of the issue being superseded, if any).
EXISTING_SEARCH: dict[str, Any] = {}

# How long to wait before looking for an issue whose creation answer was lost.
# A 504 means the proxy gave up, not that GitLab did; the issue may land a
# moment later.
UNCLEAR_CREATE_WAIT_SECONDS = 3.0

# How many times a student may retype a group member's username before the
# script gives up. A typo must not discard a session in which they have already
# committed, pushed and run the checks.
MEMBER_NAME_ATTEMPTS = 3


# ─── Run log and --debug (2.11.0) ───────────────────────────────────────────
#
# Why this exists: students report "it does not work" and nothing else, and the
# terminal they saw is gone by the time anyone asks. So every run writes a log,
# and every run that ends in an error says where the log is. The log is the
# student's own file on their own machine; nothing is sent anywhere.
#
# Three rules, each of which is a way this could go wrong:
#
#   * THE LOCATION IS FIXED, never taken from handin.toml. `token_cache_dir`
#     comes from the configuration, and the tool has already met the case where
#     a configured directory (".") put a file inside the student's repository,
#     where it gets committed. A log inside the repository is refused outright
#     (see `RunLog.refuse_if_inside`).
#   * NOTHING SECRET GOES IN. Known token values are masked by value, and the
#     patterns catch what arrives unannounced: `gl*-` tokens, `Bearer ...`,
#     credentials inside a URL (`https://user:tok@host`, which a push error can
#     echo), and OAuth fields. POST bodies are never logged at all.
#   * LOGGING CAN NEVER STOP A HAND-IN. Every write is wrapped; the first
#     failure turns the log off silently.
#
# `--debug` additionally prints the traceback of an unexpected error, turns on
# the API client's request trace (stderr), and echoes every git command.

DEBUG = False

# ─── Dry run (2.17.0) ───────────────────────────────────────────────────────
#
# `--dry-run` makes no write to GitLab and no git write: no push, no tag, no
# upload, no issue, and no change to the repository's own git config. Until
# 2.17.0 it asked "push now? [Y/n]" and pushed on Enter. Two layers, so that a
# step that forgets its own check still cannot write:
#
#   * each step that would write says what a real hand-in would do instead;
#   * `run_git` runs only the READ commands in `_GIT_READS_IN_DRY_RUN` (an
#     allowlist: anything else, and any git option before the subcommand
#     except a plain `-c key=value`, is refused), and the API client is made
#     read-only (handin_api.ApiClient.read_only), so a write that slips
#     through stops the run before anything is sent.
#
# What a dry run still does, because it is not a write: it logs in (the OAuth
# device flow, which may refresh the saved token), reads from GitLab, runs the
# course's checks (which may install packages with uv) and any validation or
# pre-review command the course configured.
#
# Set by main() from the command line, before any git or network work.
DRY_RUN = False

# Git subcommands a dry run may run, each with the argument forms that only
# read. A subcommand mapped to None may take any arguments.
_GIT_READS_IN_DRY_RUN: dict[str, Any] = {
    "rev-parse": None,
    "rev-list": None,
    "status": None,
    "remote": lambda rest: bool(rest) and rest[0] in ("get-url", "show", "-v"),
    "tag": lambda rest: bool(rest) and rest[0] in ("-l", "--list"),
    "config": lambda rest: any(a in ("--get", "--get-all", "--get-regexp", "-l", "--list") for a in rest),
    "check-ignore": None,
    "ls-files": None,
    "cat-file": None,
    "archive": None,
    "log": None,
    "diff": None,
    "show": None,
}


class IncompleteSearch(RuntimeError):
    """GitLab reported more issues than a paginated search could read."""


class DryRunWriteRefused(RuntimeError):
    """A dry run reached a git command that writes. A bug, never a student error."""


def _git_write_refused(args: tuple[str, ...]) -> bool:
    """True unless `git <args>` is on the dry run's read allowlist."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        # Only a plain `-c key=value` may precede the subcommand, and never an
        # alias: `git -c alias.p=push p` is a push.
        if args[i] == "-c" and i + 1 < len(args) and not args[i + 1].lower().startswith("alias."):
            i += 2
            continue
        if args[i] == "-C" and i + 1 < len(args):
            i += 2  # only changes the directory git starts in
            continue
        return True
    if i >= len(args):
        return False  # `git --version` and the like
    sub, rest = args[i], args[i + 1:]
    if sub not in _GIT_READS_IN_DRY_RUN:
        return True
    rule = _GIT_READS_IN_DRY_RUN[sub]
    return rule is not None and not rule(rest)


# Which step the run is in, for the crash message and the log. Set by main().
CURRENT_STEP = "start"

LOG_DIR = DRAFT_CACHE_DIR / "logs"
LOG_KEEP = 10

_SECRET_PATTERNS = (
    # Up to the LAST @ before the path, as git reads it: a user name that is an
    # e-mail address (`https://name@liu.se:TOKEN@host/...`) left the token
    # visible when the pattern stopped at the first @.
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s]*@", re.IGNORECASE), r"\1[MASKED]@"),
    (re.compile(r"\bgl[a-z]{1,6}-[A-Za-z0-9_.-]{8,}"), "[MASKED]"),
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1[MASKED]"),
    (re.compile(r"(PRIVATE-TOKEN:?\s*)\S+", re.IGNORECASE), r"\1[MASKED]"),
    (
        re.compile(
            r"(\"?(?:access_token|refresh_token|device_code|id_token|private_token|token)\"?\s*[:=]\s*\"?)"
            r"[^\"\s,&}]+",
            re.IGNORECASE,
        ),
        r"\1[MASKED]",
    ),
)


class RunLog:
    """An append-only log of one run. Every method is safe to call when closed."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._fh: Any = None
        self._secrets: set[str] = set()

    def add_secret(self, value: str | None) -> None:
        """Mask this exact value wherever it appears from now on."""
        if value and len(value) >= 8:
            self._secrets.add(value)

    def mask(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[MASKED]")
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def open(self, directory: Path = LOG_DIR, keep: int = LOG_KEEP) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Rotation deletes ONLY files this tool names, in this directory.
            old = sorted(directory.glob("handin-*.log"), key=lambda f: f.stat().st_mtime)
            for stale in old[: max(0, len(old) - (keep - 1))]:
                if stale.is_file() and stale.parent == directory:
                    stale.unlink()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = directory / f"handin-{stamp}-{os.getpid()}.log"
            self._fh = open(self.path, "w", encoding="utf-8", errors="replace")
        except OSError:
            self.path = None
            self._fh = None

    def write(self, kind: str, text: str) -> None:
        if self._fh is None:
            return
        try:
            stamp = datetime.now().strftime("%H:%M:%S")
            body = self.mask(str(text)).rstrip("\n")
            self._fh.write(f"{stamp} [{kind}] {body}\n")
            self._fh.flush()
        except Exception:
            self._close_quietly()

    def refuse_if_inside(self, repo_root: Path) -> None:
        """Drop the log if it would live inside the student's repository."""
        if self.path is None:
            return
        try:
            inside = self.path.resolve().is_relative_to(repo_root.resolve())
        except (OSError, ValueError):
            inside = False
        if inside:
            path = self.path
            self._close_quietly()
            try:
                path.unlink()
            except OSError:
                pass

    def close(self) -> None:
        self._close_quietly(keep_path=True)

    def _close_quietly(self, keep_path: bool = False) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        if not keep_path:
            self.path = None

    @property
    def exists(self) -> bool:
        try:
            return self.path is not None and self.path.is_file()
        except OSError:
            return False


RUN_LOG = RunLog()


def log_event(kind: str, text: str) -> None:
    """Append to the run log (a no-op when no log is open, e.g. in tests)."""
    RUN_LOG.write(kind, text)


def set_step(name: str) -> None:
    """Record which step the run has reached."""
    global CURRENT_STEP
    CURRENT_STEP = name
    log_event("step", name)


def debug_echo(text: str) -> None:
    if DEBUG:
        try:
            print(f"[debug] {RUN_LOG.mask(text)}", file=sys.stderr)
        except Exception:
            pass


# ─── Student-facing messages (language-selectable) ──────────────────────────
#
# The bulk of this tool speaks English. These particular messages are the ones a
# student meets when something is WRONG or when they must WAIT, so they have to
# land in the student's own language: a beginner who cannot parse the sentence
# cannot act on it. Selected with [course] language in handin.toml ("en"
# default, so other courses using this tool are unaffected).
#
# Rule for every text here: say what happened, then say what the student should
# DO, in that order, with no jargon that the course has not taught.

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        'receipt_check_passed': 'Passed',
        'receipt_check_failed': 'Needs attention',
        'receipt_check_skipped': 'Could not run',
        'receipt_check_advice': 'Advice to review',
        'partner_same_account': '  Your partner must be a different account. Enter their GitLab username.',
        'checking_commit': 'Checking committed code at {commit}. Local edits are excluded.',
        'snapshot_failed': '  The committed code could not be checked. No check result is available; local edits were not checked instead.',
        'check_select_lab': 'Choose a code hand-in to check: python3 handin.py <hand-in id> --check',
        'check_uncommitted_note': (
            '  Note: {count} file(s) in this repository have changes that are not\n'
            '  committed (see git status). The check above tested your latest commit\n'
            '  ({commit}), not the files as they are now. Commit the changes and run\n'
            '  the check again if they are part of your solution.'
        ),
        'status_none': '  No visible submission found.',
        'status_incomplete': (
            'Note: GitLab reported more issues than could be read, so the list\n'
            'below may be incomplete. Run the command again in a minute.'
        ),
        'status_details': '  GitLab state: {state}; labels: {labels}; commit: {commit}',
        'status_partners': '  Partners listed: {members}',
        'status_not_grade': 'GitLab issue state alone does not mean approval. Read results and feedback through the channel designated by your course.',
        'participants': 'Participants: {members}',
        'participants_confirm': 'Is this the correct participant list?',
        'participants_solo': 'Hand in alone? Choose this only if you have approval to work alone.',
        'partner_reuse': 'Use @{username} as your partner again? The account will be checked again.',
        'partner_access_unverified': '  If your partner cannot open the submission, ask a TA to check access. Do not submit again just to fix access.',
        'receipt': 'Receipt: {lab}\nParticipants: {members}\nCommit: {commit}\nSubmission: {url}',
        "account_missing": (
            "  Note: if you have NEVER logged in to {gitlab_url} in a browser,\n"
            "  you do not have an account there yet. Logging in the first time\n"
            "  is what creates the account.\n"
            "\n"
            "  Do this:\n"
            "    1. Open {gitlab_url} in a browser and log in with your LiU-ID.\n"
            "    2. Log in once, then close the browser.\n"
            "    3. Run this script again.\n"
        ),
        "chain_header": "-- Checking your hand-in setup --",
        "handin_local_only": "{name}  (only in your local copy - may have been removed from the course)",
        "chain_account_ok": "  Account: {username} (you are logged in to GitLab)",
        "chain_fork_missing": (
            "  Your repository: NOT found on GitLab.\n"
            "\n"
            "  This directory has an 'origin' pointing at {project_path}, but\n"
            "  GitLab does not show that project to you. Usually one of:\n"
            "    - your personal course repository has not been created yet\n"
            "      (it is created automatically; this can take up to a day\n"
            "      after you first log in to GitLab), or\n"
            "    - you are standing in the wrong directory.\n"
            "\n"
            "  What to do: check that {gitlab_url}/{project_path} opens in your\n"
            "  browser. If it does not, contact the course staff.\n"
        ),
        "chain_fork_ok": "  Your repository: {project_path}",
        "fork_not_from_course": (
            "\n"
            "  Warning: {project_path} does not look like your repository for\n"
            "  {course}. The course hands out its labs as copies of one\n"
            "  repository, and GitLab does not record this one as a copy of it.\n"
            "\n"
            "  Most often this means you are standing in the wrong directory --\n"
            "  another project of your own, or a class-mate's repository you\n"
            "  cloned. It can also happen for a repository you rebuilt by hand.\n"
            "  Handing in from the wrong repository hands in the wrong work.\n"
        ),
        "paths_missing": (
            "\n"
            "  Warning: this hand-in is '{handin_id}', but {what}\n"
            "\n"
            "{advice}"
            "  Handing in now would hand in an incomplete assignment.\n"
        ),
        "paths_missing_advice_commit": (
            "  The checks read your latest commit, not your folder. Add the file\n"
            "  and commit it (git add <file>, then git commit), and run again.\n"
        ),
        "paths_missing_advice_check": (
            "  Check that the file has the name the lab text gives, in the folder\n"
            "  it gives, and that it is committed.\n"
        ),
        "paths_missing_uncommitted": "{path} is only in your folder, not in your latest commit.",
        "paths_missing_ignored": "{path} is in your folder, but git ignores it (.gitignore), so it is in no commit.",
        "paths_missing_advice_ignored": (
            "  The checks read your latest commit, not your folder. Remove the line in\n"
            "  .gitignore that matches the file, commit the file, and run again.\n"
        ),
        "paths_missing_absent": "{path} does not exist in this repository.",
        "paths_missing_empty": "the folder {path} is empty.",
        "origin_wrong_host": (
            "  Warning: this directory's 'origin' points at {origin_host}, but the\n"
            "  course uses {course_host}.\n"
            "\n"
            "  Usually this means you are standing in the wrong directory: this is\n"
            "  some other project of yours, not your course repository. The\n"
            "  hand-in would still be created for the course, but it would refer\n"
            "  to a tag and a commit in this repository.\n"
            "\n"
            "  Full address: {origin_url}"
        ),
        "origin_unexpected_path": (
            "  Warning: this directory's repository is {project_path}, which does\n"
            "  not look like your course repository.\n"
            "\n"
            "  The course expects: {expected}\n"
            "\n"
            "  You get this warning if you are standing in the wrong directory, or\n"
            "  if you are working in a copy of someone else's repository - then the\n"
            "  hand-in refers to their repository, not yours. If you have simply\n"
            "  renamed your own repository, everything is as it should be."
        ),
        "origin_continue_anyway": "  Hand in from this repository anyway?",
        "origin_cancelled": (
            "  Cancelled. Nothing has been handed in.\n"
            "  Move to your course repository with cd and run the script again."
        ),
        "chain_fork_unknown": (
            "  Your repository: could not be checked ({details}).\n"
            "  That is a problem talking to GitLab, not a problem with your\n"
            "  repository — carrying on."
        ),
        "chain_member_ok": "  Hand-in project: you are a member — hand-in works normally.",
        "chain_member_pending": (
            "  Hand-in project: you are not a member yet.\n"
            "\n"
            "  You can still hand in — your submission will be registered. But\n"
            "  membership is what lets your partner open the submission, and it\n"
            "  is added automatically a while after your first GitLab login.\n"
            "  If your partner cannot see the submission today, that is why;\n"
            "  it will appear by itself.\n"
        ),
        "chain_member_unknown": (
            "  Hand-in project: could not check your membership (that is fine —\n"
            "  it does not stop the hand-in)."
        ),
        "confidential_failed": (
            "Error: the submission was created, but GitLab did NOT mark it as\n"
            "confidential. That means people who should not read it may be able\n"
            "to.\n"
            "\n"
            "Your submission is NOT considered handed in. Do this:\n"
            "  1. Do not create a new submission.\n"
            "  2. Contact the course staff and send them this link:\n"
            "     {issue_url}\n"
        ),
        "bot_delay": (
            "  Your submission is registered. Two things are added afterwards,\n"
            "  automatically:\n"
            "    - the labels the TAs filter on\n"
            "    - your partner's access to the submission\n"
            "  You do not have to do anything if they are not there yet, and you\n"
            "  must NOT hand in again to trigger it. If your partner still\n"
            "  cannot open the submission the next day, contact the course staff.\n"
        ),
        "bot_delay_solo": (
            "  Your submission is registered. The labels the TAs filter\n"
            "  on are added afterwards, automatically — you do not have to do\n"
            "  anything, and you must NOT hand in again to trigger it.\n"
        ),
        "bot_delay_partner_only": (
            "  Your submission is registered. Your partner's access to the\n"
            "  submission is added afterwards, automatically. You do not have to\n"
            "  do anything if it is not there yet, and you must NOT hand in again\n"
            "  to trigger it. If your partner still cannot open the submission\n"
            "  the next day, contact the course staff.\n"
        ),
        "partner_unknown_user": (
            "    Error: there is no GitLab user named '{username}'.\n"
            "\n"
            "    Two common reasons:\n"
            "      - a typo in the username, or\n"
            "      - your partner has never logged in to {gitlab_url}. The\n"
            "        first login is what creates the account.\n"
            "\n"
            "    Ask your partner to open {gitlab_url}, log in once, and read\n"
            "    the username off their own profile page.\n"
        ),
        "partner_suggestions": "    Did you mean one of these? {suggestions}",
        "partner_try_again": (
            "    Type it again ({attempts} attempt(s) left), or press Enter to skip."
        ),
        "partner_giving_up": (
            "    Stopping here so that nothing is registered with a partner who\n"
            "    cannot be found. Nothing has been handed in. Get the correct\n"
            "    username and run the script again — your commits and your push\n"
            "    are still there, so you only repeat this dialogue."
        ),
        "partner_verify_failed": (
            "    Error: could not check whether '{username}' exists (GitLab did\n"
            "    not answer). Check your network and try again — a misspelled\n"
            "    partner never gets access to the submission, so the script does\n"
            "    not guess."
        ),
        "unknown_handin_id": (
            "Error: '{lab_id}' is not something you can hand in on this course.\n"
            "\n"
            "Nothing has been handed in. You can hand in:\n"
            "\n"
            "{valid}\n"
            "\n"
            "Write the name in the left column exactly as it is spelled:\n"
            "    python3 handin.py {example}\n"
            "\n"
            "Or leave it out, and pick from a menu instead:\n"
            "    python3 handin.py\n"
        ),
        "unknown_handin_id_suggestion": "  Did you mean '{suggestion}'?\n",
        "confirm_header": "-- Confirmation: the course rules for aids --",
        "confirm_question_short": (
            "Have you followed the course rules for aids, including the AI rules?"
        ),
        "confirm_rules_line": "  The rules are here: {rules_url}",
        "confirm_instruction_yes": "  Yes - I have followed them:  type  {word}",
        "confirm_instruction_no": "  No - I have not:             type  {decline_word}",
        "confirm_prompt": "  Your answer: ",
        "confirm_retry": "  That was neither {word} nor {decline_word}. Type one of them ({left} left).",
        "confirm_ok": "  Thank you - the confirmation goes with your hand-in.",
        "confirm_declined_intro": (
            "  Thank you for the clear answer. Nothing further happens automatically:\n"
            "  describe in your own words what you did, and the description goes with\n"
            "  your hand-in so that a teacher can read it next to the code.\n"
            "\n"
            "  Write one or more lines. End with an empty line.\n"
        ),
        "confirm_declined_prompt": "  > ",
        "confirm_declined_empty": (
            "  The description cannot be empty - an empty one says nothing about what\n"
            "  happened. Write at least one line, then an empty one ({left} left)."
        ),
        "confirm_declined_ok": "  Thank you - the description goes with your hand-in.",
        "confirm_unclear": (
            "  No clear answer, so NOTHING has been handed in.\n"
            "\n"
            "  That is deliberate. A hand-in marked 'not confirmed' reads as if you\n"
            "  had said the rules were not followed, and that must never happen just\n"
            "  because you pressed Enter or closed the window.\n"
            "\n"
            "  Your commits and your push are still there. Run the script again and\n"
            "  answer with the word it shows, or with {decline_word} if you want to\n"
            "  describe what you did instead.\n"
        ),
        "confirm_cancelled": "  Cancelled. Nothing has been handed in.",
        "labels_dropped": (
            "  Note: the hand-in was created, but GitLab did not set the label(s)\n"
            "  {labels} on it.\n"
            "  Your hand-in still counts - the labels are only used by the course\n"
            "  to sort submissions. Mention it to your TA if your hand-in\n"
            "  does not turn up where it should."
        ),
        "summary_warnings_header": "  One thing did not go as expected:",
        "not_a_git_repo": (
            "Error: This script must be run from inside a git repository.\n"
            "  Please cd into your project directory and try again."
        ),
        "no_origin_remote": (
            "Error: No 'origin' remote found in this repository.\n"
            "  Make sure your repository has an 'origin' remote configured.\n"
            "  You can add one with: git remote add origin <url>"
        ),
        "no_commits_yet": (
            "Error: this repository has no commits yet.\n"
            "\n"
            "  A hand-in is a commit: the tool tags the latest one and the\n"
            "  TA reads that version. There is nothing to hand in until\n"
            "  you have made one.\n"
            "\n"
            "  Do this in your project directory:\n"
            "    1. git add <your files>\n"
            "    2. git commit -m \"My solution\"\n"
            "    3. git push\n"
            "    4. run this script again."
        ),
        "oauth_box_title": "  Log in with your browser",
        "oauth_box_code": "  Code: {code}",
        "oauth_box_auto": "  A browser window should open automatically.",
        "oauth_box_manual": "  If not, open this URL manually:",
        "oauth_waiting": "  Waiting for you to log in... (press Ctrl+C to cancel)",
        "oauth_login_ok": "  [OK] Login successful!",
        "oauth_timed_out": "  Login timed out. Please try again.",
        "oauth_denied": "  Login was denied.",
        "oauth_failed": "  Login failed ({error}). Falling back to an access token.",
        "auth_cache_cleared": (
            "  The saved login in {path} was rejected by GitLab and has been removed.\n"
            "  Run the command again and you will be asked to log in afresh."
        ),
        "gitlab_connect_failed": (
            "Error: Could not connect to GitLab at {gitlab_url}\n"
            "  Details: {details}\n"
            "  Please check your internet connection and try again."
        ),
        "gitlab_auth_failed": "Error: GitLab authentication failed. Your token may be invalid or expired.",
        "token_instructions": (
            "If you HAVE logged in before, create a new personal access token:\n"
            "  1. Go to {gitlab_url}/-/user_settings/personal_access_tokens\n"
            "  2. Create a token with the 'api' scope\n"
            "  3. Save it in one of these locations:\n"
            "     - Environment variable: export GITLAB_TOKEN=<your-token>\n"
            "     - Config file: ~/.config/handin/token"
        ),
        "deadline_header": "-- Deadline Information --",
        "deadline_past": "  Latest deadline for {handin}: {when} ({days} days ago){extra}",
        "deadline_next": "  Next deadline for {handin}:   {when} (in {days} days){extra}",
        "deadline_none": "  No deadlines found for this hand-in type.",
        "deadline_extra": " — {text}",
        "help_description": "Student hand-in submission script",
        "help_usage_prefix": "usage: ",
        "help_error_prefix": "error",
        "help_positionals_title": "positional arguments",
        "help_optionals_title": "options",
        "help_help": "show this help message and exit",
        "help_lab_id_metavar": "<hand-in id>",
        "help_lab_id": "Which hand-in to submit. Leave it out to pick from a menu.",
        "help_dry_run": "Show what would happen without pushing, tagging, uploading or creating anything",
        "help_check": "Check the committed lab locally without logging in or submitting",
        "help_status": "Show your latest hand-ins without submitting",
        "help_no_config_update": "Do not consult the course's central configuration; use only your own handin.toml",
        "help_config": "Path to handin.toml configuration file",
        "help_config_metavar": "CONFIG",
        "help_debug": "Show every technical detail (git commands, GitLab requests, the full error) -- useful when asking for help",
        "dry_header": "-- Dry-run summary --",
        "value_yes": "yes",
        "value_no": "no",
        "language_name_en": "English",
        "language_name_sv": "Swedish",
        "partner_policy_bot": "the course's bot adds your partner to the issue",
        "partner_policy_off": "your partner is only named in the issue",
        "dry_type": "  Hand-in type: {name} ({id})",
        "dry_repo": "  Repository: {repo}",
        "dry_target": "  Target project: {target}",
        "dry_labels": "  Labels: {labels}",
        "dry_confidential": "  Confidential: {value}",
        "dry_language": "  Message language: {language}",
        "dry_partner_policy": "  Your lab partner: {policy}",
        "dry_bot_active": "  Bot active (completes labels/assignees): {value}",
        "dry_bot_active_access": "  Bot active (completes the partner's access): {value}",
        "dry_confirm": "  Confirmation (course rules on aids): {state} (word shown: {word})",
        "dry_confirm_yes": "confirmed",
        "dry_confirm_no": "declined, with a description",
        "dry_confirm_skipped": "  Confirmation (course rules on aids): not asked ([confirmation] enabled = false)",
        "dry_group": "  Group members: {members}",
        "dry_supersedes": "  Supersedes: #{iid}",
        "dry_message": "  Message: {message}",
        "dry_no_issue": "  Nothing was pushed, tagged, uploaded or created (dry-run mode).",
        "fatal_unexpected": "An unexpected error occurred:",
        "fatal_contact": "If this problem persists, please contact the course TA\nand include the error message above.",
        "env_header": "-- Checking environment --",
        "env_no_auth": "  No authentication found. You need a GitLab personal access token.",
        "env_pat_opening": "  1. Opening: {url}",
        "env_pat_steps": (
            "  2. Click 'Add new token'\n"
            "  3. Name: 'handin' (or anything you like)\n"
            "  4. Scopes: check 'api'\n"
            "  5. Click 'Create personal access token'\n"
            "  6. Copy the token (starts with 'glpat-')"
        ),
        "env_pat_hidden": "  What you paste is not shown on the screen. Paste the token and press Enter.",
        "env_pat_prompt": "  Paste your token here: ",
        "env_pat_none_entered": "  No token entered.",
        "env_pat_saved": "  Token saved to {path}",
        "env_pat_not_saved": (
            "  Note: the token could NOT be saved to {path}\n"
            "  ({reason}).\n"
            "  That is a permission problem in that directory, not a problem\n"
            "  with your token - the hand-in carries on with the token you just\n"
            "  pasted. You will be asked for it again next time. To be rid of\n"
            "  the question, set GITLAB_TOKEN in your environment instead."
        ),
        "token_file_unreadable": (
            "  Note: the saved token in {path} could not be read ({reason}).\n"
            "  Carrying on as if it was not there."
        ),
        "env_repository": "  Repository: {repo_name}",
        "env_authenticated": "  Authenticated as: {username}",
        "uncommitted_header": (
            "Warning: Your working directory has changes that won't be included\n"
            "  in the submission:"
        ),
        "uncommitted_unstaged": "  Modified but not staged ({count}):",
        "uncommitted_staged": "  Staged but not committed ({count}):",
        "uncommitted_untracked": "  Untracked files ({count}):",
        "uncommitted_more": "    ... and {count} more",
        "uncommitted_question": "Continue handing in the previous commit without these changes?",
        "uncommitted_cancelled": "Submission cancelled. Please commit your changes and try again.",
        "submission_cancelled": "Submission cancelled.",
        "push_no_upstream": "  Your branch '{branch}' has no upstream tracking branch.",
        "push_offer_upstream": "  Push it now with 'git push -u origin {branch}'?",
        "push_branch_failed": "Error: Failed to push branch to remote.",
        "push_ok": "  Push successful.",
        "push_warn_no_analysis": "Warning: Without pushing, no code analysis will be created.",
        "push_continue_without": "  Continue without pushing?",
        "push_unpushed": "  You have {count} unpushed commit{plural}.",
        "push_needed_for_analysis": "  Pushing is needed so that a code analysis can be created.",
        "push_warn_not_pushed": "Warning: Without pushing, your branch on GitLab does not show the code you are handing in.",
        "push_needed_reason": "  Pushing makes your branch on GitLab show the same version you are handing in.",
        "push_now": "  Push now?",
        "push_failed": "Error: Failed to push changes to remote.",
        "push_manual": (
            "  If your partner (or you, from another computer) has pushed something\n"
            "  new, fetch it first with `git pull`. Then push and try again."
        ),
        "lint_reads_worktree": (
            "  Note: these checks read the files as they are on disk right now,\n"
            "  including the {count} change(s) you just chose to leave out of the\n"
            "  hand-in. A result here is therefore not necessarily the result for\n"
            "  what you are handing in."
        ),
        "push_failed_tag_still_works": (
            "  The hand-in itself does not depend on this push: the tag is put on\n"
            "  your latest commit, and pushing the tag carries that commit to\n"
            "  GitLab on its own. Your TA reads the tag."
        ),
        "selected_handin": "  Selected: {name}",
        "select_handin_prompt": "Select what you are handing in:",
        "menu_enter_choice": "Enter your choice: ",
        "yesno_yes_default": "[Y/n]",
        "yesno_no_default": "[y/N]",
        "yesno_unrecognised": "  Please answer yes or no (y/n).",
        "menu_cancelled": "Submission cancelled.",
        "check_interrupted": "Check interrupted. Nothing was handed in.",
        "input_ended": (
            "Input ended: there is no terminal left to answer in.\n"
            "  Nothing has been handed in. Run the command again in a terminal."
        ),
        "menu_need_number": "  Please enter a number.",
        "menu_not_a_number": "  '{raw}' is not a valid number. Please try again.",
        "menu_out_of_range": "  Please enter a number between 1 and {count}.",
        "dry_run_banner": "  *** DRY-RUN MODE - nothing is pushed, tagged, uploaded or created ***",
        "dry_push_skipped": "  Dry run: a real hand-in would offer to push now. Nothing is pushed.",
        "dry_report_not_uploaded": "  Dry run: {name} is not uploaded.",
        "analysis_header": "-- Checking code analysis --",
        "analysis_commit": "  Current commit: {commit}",
        "analysis_unavailable": "  Could not check for analysis issues: {error}",
        "analysis_continuing": "  Continuing without analysis check.",
        "analysis_none_found": "  No code analysis issue found for the current commit.",
        "analysis_none_why": "  This could mean:",
        "analysis_none_pending": "    - The analysis hasn't run yet (try again in a few minutes)",
        "analysis_none_down": "    - The analysis system is currently down",
        "analysis_continue_q": "  Continue without code analysis?",
        "analysis_cancelled": "Submission cancelled. Try again later when the analysis is available.",
        "analysis_found": "  Code analysis found: {url}",
        "analysis_problems": "  The code analysis indicates problems that must be fixed first.",
        "analysis_no_data": "  Analysis issue found (no embedded data to check).",
        "analysis_critical": "  [!]  Code analysis found {count} critical issue{plural}.",
        "analysis_fatal": "  [!]  Code analysis found {count} fatal warning{plural}.",
        "analysis_setup": "  [!]  Code analysis found {count} project setup issue{plural}.",
        "existing_checking": "  Checking for existing submissions...",
        "existing_check_failed": (
            "  Warning: could not check whether you have handed this in before:\n"
            "  {details}\n"
            "  If you have, handing in now makes a SECOND issue for the same hand-in\n"
            "  instead of replacing the first. Safest: stop, wait a minute, and run\n"
            "  the command again."
        ),
        "existing_none": "  No existing submissions found.",
        "existing_found_header": "  Existing submission(s) found:",
        "existing_supersede_question": (
            "An existing submission for this hand-in was found (see above).\n"
            "  Do you want to supersede it with a new submission?"
        ),
        "existing_will_supersede": "  Will supersede issue #{iid}.",
        "validation_running": "  Running validation checks...",
        "validation_cannot_run": "  Warning: Could not run validation script: {details}",
        "validation_passed": "  Validation passed.",
        "validation_failed": "Validation failed. Please fix the issues above before submitting.",
        "validation_warnings_question": "Validation produced warnings (see above). Proceed anyway?",
        "validation_unexpected_code": "  Warning: Validation script exited with unexpected code {code}.",
        "checklist_header": "-- Pre-submission checklist --",
        "checklist_stop": (
            "  You answered no to:\n"
            "    {question}\n"
            "\n"
            "  That has to be done before you hand in, so the hand-in stopped here.\n"
            "  NOTHING has been handed in: no tag and no issue were created.\n"
            "\n"
            "  When it is done, run the same command again.\n"
        ),
        "message_header": "-- Optional message --",
        "message_prompt": "  Message to the TA (press Enter to skip): ",
        "message_recorded": "  Message recorded: {message}",
        "message_none": "  No message.",
        "tag_header": "-- Creating submission tag --",
        "tag_create_failed": "Error: Failed to create tag '{tag}'.",
        "tag_pushing": "  Pushing tag {tag}...",
        "tag_push_failed": "Error: Failed to push tag '{tag}' to remote.",
        "tag_resolve_and_retry": "  Please resolve the issue and try again.",
        "tag_created": "  Tag {tag} created at commit {commit}.",
        "tag_fetch_checking": "  Checking which tags already exist at GitLab...",
        "tag_fetch_timeout": (
            "  GitLab did not answer within {seconds} seconds, so the tags there\n"
            "  could not be checked. Continuing with the tags this clone knows."
        ),
        "tag_fetch_failed": (
            "  The tags at GitLab could not be checked.\n"
            "  Continuing with the tags this clone knows."
        ),
        "submit_header": "-- Submitting hand-in --",
        "issue_create_failed": (
            "Error: Failed to create issue on GitLab.\n"
            "  Details: {details}"
        ),
        "issue_tag_pushed_contact_ta": (
            "  Your tag '{tag}' has been pushed. You can contact the TA\n"
            "  and ask them to register your submission manually."
        ),
        "issue_contact_ta": "  Please contact the course TA for assistance.",
        "confidential_repaired": (
            "  Note: the submission had to be marked confidential in a second\n"
            "  step. It is confidential now, and your hand-in is registered."
        ),
        "ai_review_header": "-- AI pre-review --",
        "ai_review_running": "  Running automated review (this may take a few minutes)...",
        "ai_review_timeout": "  Warning: AI review timed out after 10 minutes.",
        "ai_review_failed": "  Warning: AI review failed: {details}",
        "ai_review_error": "  Warning: AI review returned an error.",
        "ai_review_no_findings": "  No review findings.",
        "ai_review_unparsable": "  Warning: Could not parse AI review output.",
        "ai_review_blocking": (
            "  AI review found issues. Please address them before submitting.\n"
            "  (Set block_on_errors = false in config to submit anyway.)"
        ),
        "report_header": "-- Select report file --",
        "report_allowed_formats": "  Allowed formats: {formats}",
        "report_max_size": "  Maximum size: {max_mb} MB",
        "report_not_found": "  Error: File not found: {path}",
        "report_bad_format": "  Error: File format '.{ext}' is not allowed.",
        "report_too_large": "  Error: File is too large ({size} MB, maximum {max_mb} MB).",
        "report_not_pdf": "  Error: File does not appear to be a valid PDF.",
        "report_unreadable": "  Error: Could not read file: {details}",
        "report_too_few_pages": "  Error: Report has {pages} pages (minimum {minimum}).",
        "report_too_many_pages": "  Error: Report has {pages} pages (maximum {maximum}).",
        "report_page_count": "  Page count: {pages}",
        "report_page_count_unknown": "  Warning: Could not verify page count (PyMuPDF not installed).",
        "report_selected": "  Selected: {name} ({size} MB)",
        "report_none_selected": "No report file selected. Submission cancelled.",
        "upload_running": "  Uploading report file...",
        "upload_ok": "  [OK] Uploaded: {name}",
        "upload_failed": (
            "Error: Failed to upload file to GitLab.\n"
            "  Details: {details}"
        ),
        "upload_retry": "  You can try submitting again, or contact the course TA.",
        "lint_header": "-- Pre-submission checks --",
        "lint_running": "  Running: {name}...",
        "lint_timeout": "    [!]  Timed out after 120 seconds",
        "lint_cannot_run": "    [!]  Could not run check: {details}",
        "lint_skipped_check_said": (
            "    [-]  Skipped: the check could not judge your code here, so it\n"
            "         does not count against the hand-in. The check says:"
        ),
        "lint_skipped_no_tool": (
            "    [-]  Skipped: the program this check needs is not available\n"
            "         here. This says nothing about your code, so it does not\n"
            "         count against the hand-in.\n"
        ),
        "uv_sync_running": "  Preparing: uv sync --frozen in {dir} ...",
        "uv_sync_ok": "    [OK]   Ready",
        "uv_sync_no_tool": "    [-]  uv is not available here, so the checks that need it are skipped.",
        "uv_sync_file_absent": (
            "    [-]  {path} is not in your latest commit, so the checks that use uv\n"
            "         cannot run."
        ),
        "uv_sync_file_uncommitted": (
            "    [-]  {path} is only in your folder, not in your latest commit, so the\n"
            "         checks that use uv cannot run. Add it with git add {path} and\n"
            "         git commit, and run again."
        ),
        "uv_sync_file_ignored": (
            "    [-]  {path} is in your folder, but git ignores it (.gitignore), so it\n"
            "         is not in your commit and the checks that use uv cannot run.\n"
            "         Remove the line in .gitignore that matches it, commit the file,\n"
            "         and run again."
        ),
        "uv_sync_broken": (
            "    [!]  uv could not read the project files in {dir} (see uv's message\n"
            "         below). Fix the file -- a git merge conflict, for example -- and\n"
            "         commit it."
        ),
        "uv_sync_failed": (
            "    [-]  The environment could not be prepared (uv sync --frozen failed).\n"
            "         Usually there is no network and uv has not saved the packages\n"
            "         before. This says nothing about your code."
        ),
        "lint_skipped_env": "    [-]  Not checked: the environment in {dir} could not be prepared (see above).",
        "lint_passed": "    [OK]   Passed",
        "lint_failed_line": "    [FAIL] Failed ({marker})",
        "lint_advice_line": "    [TIP]  Advice to review (does not affect the hand-in)",
        "lint_marker_required": "REQUIRED",
        "lint_marker_optional": "does not block the hand-in",
        "lint_required_failed": "  Required checks failed. Please fix the issues above.",
        "lint_optional_failed": "  Some checks failed. The requirements still apply at grading.",
        "lint_continue_anyway": "  Continue with submission anyway?",
        "lint_checks_failed_exit": "Lint checks failed. Please fix the issues above before submitting.",
        "supersede_marked": "  Marked issue #{iid} as superseded.",
        "supersede_failed": "  Warning: Could not mark old issue #{iid} as superseded: {details}",
        "group_header": "-- Group members --",
        "group_intro": (
            "  This is a group submission (max {max_size} members).\n"
            "  Enter your group member(s) GitLab username(s), one per line.\n"
            "  Press Enter on an empty line when done (max {max_additional} additional member(s))."
        ),
        "group_member_prompt": "  Member {n}: ",
        "group_summary": "  Group: you + {members}",
        "banner_subtitle": "Hand-in Submission Script",
        "issue_created": "  Issue created: {url}",
        "submitted_ok": "Hand-in submitted successfully!",
        "existing_check_failed_question": "  Hand in anyway?",
        "existing_incomplete": "GitLab reported more issues than the search could read, so the list is incomplete.",
        "issue_create_recovered": "  GitLab's answer was lost, but the issue was created -- found it: {url}",
        "interrupted_after_tag": (
            "Stopped. Your tag '{tag}' was pushed, but NO hand-in issue was created,\n"
            "  so nothing is handed in yet. Run the command again to hand in."
        ),
        "interrupted_before_issue": (
            "Stopped while the tag was being made. No hand-in issue was created, so\n"
            "  nothing is handed in yet. Run the command again to hand in."
        ),
        "interrupted_issue_uncertain": (
            "Stopped while the issue was being created. It is UNCLEAR whether your\n"
            "  hand-in was registered. Do not hand in again straight away: open\n"
            "  {issues_url} and look for your issue. If it is not there,\n"
            "  run the command again."
        ),
        "interrupted_issue_unconfirmed": (
            "Stopped just after your issue was created: {url}\n"
            "  The tool did not get to check that it is confidential. Open it: if it\n"
            "  does not show an eye icon with 'Confidential', tell the course staff."
        ),
        "interrupted_after_issue": (
            "Stopped, but your hand-in IS registered: {url}\n"
            "  Do not hand in again."
        ),
        "interrupted_supersede": (
            "Stopped, but your hand-in IS registered: {url}\n"
            "  The old issue #{iid} was perhaps not marked as replaced. That does not\n"
            "  matter for the new hand-in; do not hand in again."
        ),
        "git_missing": "Error: the program 'git' could not be found.\n  The hand-in tool uses git to tag your code. Install Git (on Windows:\n  https://git-scm.com/download/win), then open a NEW terminal and try\n  again. Check that it works with: git --version",
        "moved_to_script_repo": "  Note: you are not standing in a git repository, so the tool uses the\n  repository that handin.py itself is in:\n    {path}",
        "chain_member_not_needed": "  Hand-in project: you are not a member - that is not needed for this\n  hand-in.",
        "supersede_commented": "  The old issue #{iid} now has a comment pointing to the new hand-in.",
        "template_hint": (
            "  The course has new or corrected files in the lab template that you\n"
            "  have not fetched yet, for example:\n"
            "{files}\n"
            "  Fetch them with `{python} uppdatera.py` in your lab repository's\n"
            "  folder. If they belong to the lab you are handing in now: stop with\n"
            "  Ctrl+C, fetch them and start again. Otherwise you can continue."
        ),
        "lint_output_more": "       ({count} earlier lines left out - more of the output is in the log)",
        "log_saved": "  A log of this run is saved here:\n    {path}\n  If you need help, send that file to a TA. Passwords and\n  tokens are masked in it.",
        "fatal_where": "  (Step: {step}. Hand-in tool {version}, Python {python}, {system}.)",
        "fatal_debug_hint": "  Run the same command with --debug to see all the details.",
        "oauth_unavailable": "  Logging in through the browser could not be started ({reason}).\n  You can use a personal access token instead.",
        "oauth_poll_network": "  (Could not reach GitLab just now: {reason}. Still trying ...)",
        "oauth_rejected_rerun": "  GitLab no longer accepts your saved login, so it has been removed.\n  Run the same command again: you will be asked to log in in the\n  browser once more.",
        "issue_create_uncertain": "Error: GitLab's answer was lost or was an error while the issue was being\n  created ({details}).\n  It is UNCLEAR whether your hand-in was registered. Do not hand in again\n  straight away: open {issues_url} and look for your issue.\n  If it is not there, run the command again.",
        "token_instructions_powershell": "     - In PowerShell on Windows: $env:GITLAB_TOKEN=\"<your-token>\"",
        "lint_output_more_nolog": "       ({count} earlier lines left out)",
        "oauth_rejected_manual": "  GitLab no longer accepts your saved login, and the file could not be\n  removed automatically. Delete this file and run the command again:\n    {path}",
        "supersede_comment_failed": "  Warning: could not add a comment to the old issue #{iid}: {details}\n  Your new hand-in counts anyway. Tell your TA that it replaces #{iid}.",
    },
    "sv": {
        'receipt_check_passed': 'Gick igenom',
        'receipt_check_failed': 'Behöver ses över',
        'receipt_check_skipped': 'Kunde inte köras',
        'receipt_check_advice': 'Råd att se över',
        'partner_same_account': '  Din partner måste ha ett annat konto. Skriv hens användarnamn på GitLab.',
        'checking_commit': 'Kontrollerar committad kod i {commit}. Lokala ändringar ingår inte.',
        'snapshot_failed': '  Den committade koden kunde inte kontrolleras. Inget kontrollresultat finns; lokala ändringar har inte kontrollerats i stället.',
        'check_select_lab': 'Välj en kodinlämning att kontrollera: python3 handin.py <labb-id> --check',
        'check_uncommitted_note': (
            '  Obs: {count} fil(er) i repot har ändringar som inte är committade\n'
            '  (se git status). Kontrollen ovan gällde din senaste commit ({commit}),\n'
            '  inte filerna som de ser ut nu. Committa ändringarna och kör kontrollen\n'
            '  igen om de hör till din lösning.'
        ),
        'status_none': '  Ingen synlig inlämning hittades.',
        'status_incomplete': (
            'Obs: GitLab säger att det finns fler ärenden än som gick att läsa, så\n'
            'listan nedan kan vara ofullständig. Kör kommandot igen om en minut.'
        ),
        'status_details': '  Status i GitLab: {state}; etiketter: {labels}; commit: {commit}',
        'status_partners': '  Angivna labbpartners: {members}',
        'status_not_grade': 'Att ett GitLab-ärende är stängt betyder inte i sig godkänt. Läs resultat och återkoppling i den kanal kursen anger.',
        'participants': 'Deltagare: {members}',
        'participants_confirm': 'Stämmer deltagarlistan?',
        'participants_solo': 'Lämna in ensam? Välj detta bara om du har fått arbeta ensam.',
        'partner_reuse': 'Använd @{username} som labbpartner igen? Kontot kontrolleras på nytt.',
        'partner_access_unverified': '  Kan din partner inte öppna inlämningen, be en handledare kontrollera åtkomsten. Lämna inte in igen bara för att ordna åtkomsten.',
        'receipt': 'Kvitto: {lab}\nDeltagare: {members}\nCommit: {commit}\nInlämning: {url}',
        "account_missing": (
            "  Obs: om du ALDRIG har loggat in på {gitlab_url} i en webbläsare\n"
            "  har du inget konto där än. Det är den första inloggningen som\n"
            "  skapar kontot.\n"
            "\n"
            "  Gör så här:\n"
            "    1. Öppna {gitlab_url} i en webbläsare och logga in med\n"
            "       ditt LiU-ID.\n"
            "    2. Logga in en gång, stäng sedan webbläsaren.\n"
            "    3. Kör det här skriptet igen.\n"
        ),
        "chain_header": "-- Kontrollerar din inlämningsuppsättning --",
        "handin_local_only": "{name}  (bara i din lokala kopia - kan vara borttagen ur kursen)",
        "chain_account_ok": "  Konto: {username} (du är inloggad på GitLab)",
        "chain_fork_missing": (
            "  Ditt repo (repository): hittas INTE på GitLab.\n"
            "\n"
            "  Mappen du står i har en 'origin' som pekar på {project_path},\n"
            "  men GitLab visar inte det projektet för dig. Oftast något av:\n"
            "    - ditt personliga kursrepo är inte skapat än (det skapas\n"
            "      automatiskt, och det kan dröja upp till ett dygn efter att\n"
            "      du loggat in på GitLab första gången), eller\n"
            "    - du står i fel mapp.\n"
            "\n"
            "  Gör så här: kontrollera att {gitlab_url}/{project_path} går att\n"
            "  öppna i din webbläsare. Gör den inte det: kontakta kursledningen.\n"
        ),
        "chain_fork_ok": "  Ditt repo: {project_path}",
        "fork_not_from_course": (
            "\n"
            "  Varning: {project_path} ser inte ut att vara ditt repo för\n"
            "  {course}. Kursen delar ut labbarna som kopior av ett enda repo,\n"
            "  och GitLab har inte registrerat det här som en kopia av det.\n"
            "\n"
            "  Oftast betyder det att du står i fel mapp – ett annat eget\n"
            "  projekt, eller en kurskamrats repo som du har klonat. Det kan\n"
            "  också hända för ett repo du har byggt upp för hand. Lämnar du in\n"
            "  från fel repo lämnar du in fel arbete.\n"
        ),
        "paths_missing": (
            "\n"
            "  Varning: den här inlämningen är '{handin_id}', men {what}\n"
            "\n"
            "{advice}"
            "  Lämnar du in nu lämnar du in en ofullständig uppgift.\n"
        ),
        "paths_missing_advice_commit": (
            "  Kontrollerna läser din senaste commit, inte din mapp. Lägg till filen\n"
            "  och committa den (git add <fil>, sedan git commit), och kör igen.\n"
        ),
        "paths_missing_advice_check": (
            "  Kontrollera att filen heter det labbtexten säger, ligger i den mapp\n"
            "  labbtexten säger, och är committad.\n"
        ),
        "paths_missing_uncommitted": "{path} finns bara i din mapp, inte i din senaste commit.",
        "paths_missing_ignored": "{path} finns i din mapp, men git ignorerar den (.gitignore), så den finns inte i någon commit.",
        "paths_missing_advice_ignored": (
            "  Kontrollerna läser din senaste commit, inte din mapp. Ta bort raden i\n"
            "  .gitignore som gäller filen, committa filen och kör igen.\n"
        ),
        "paths_missing_absent": "{path} finns inte i det här repot.",
        "paths_missing_empty": "mappen {path} är tom.",
        "origin_wrong_host": (
            "  Varning: 'origin' i den här mappen pekar på {origin_host}, men\n"
            "  kursen använder {course_host}.\n"
            "\n"
            "  Oftast betyder det att du står i fel mapp – det här är något\n"
            "  annat projekt av dina, inte ditt kursrepo. Inlämningen skulle ändå\n"
            "  skapas i kursens projekt, men den skulle peka på en tagg och en\n"
            "  commit i det här repot.\n"
            "\n"
            "  Hela adressen: {origin_url}"
        ),
        "origin_unexpected_path": (
            "  Varning: repot i den här mappen är {project_path}, och det ser\n"
            "  inte ut som ditt kursrepo.\n"
            "\n"
            "  Kursen väntar sig: {expected}\n"
            "\n"
            "  Du får den här varningen om du står i fel mapp, eller om du\n"
            "  arbetar i en kopia av någon annans repo – då pekar inlämningen på\n"
            "  deras repo och inte på ditt. Har du bara döpt om ditt eget repo är\n"
            "  allt som det ska."
        ),
        "origin_continue_anyway": "  Vill du lämna in från det här repot ändå?",
        "origin_cancelled": (
            "  Avbrutet. Ingenting har lämnats in.\n"
            "  Byt till ditt kursrepo med cd och kör skriptet igen."
        ),
        "chain_fork_unknown": (
            "  Ditt repo (repository): kunde inte kontrolleras ({details}).\n"
            "  Det är ett problem med att prata med GitLab, inte ett problem med\n"
            "  ditt repo – fortsätter."
        ),
        "chain_member_ok": (
            "  Inlämningsprojektet: du är medlem – inlämningen fungerar som vanligt."
        ),
        "chain_member_pending": (
            "  Inlämningsprojektet: du är inte medlem än.\n"
            "\n"
            "  Du kan lämna in ändå – din inlämning registreras. Men det är\n"
            "  medlemskapet som gör att din labbpartner kan öppna inlämningen,\n"
            "  och det läggs till automatiskt en tid efter din första\n"
            "  GitLab-inloggning. Kan partnern inte se inlämningen i dag är det\n"
            "  därför; den dyker upp av sig själv.\n"
        ),
        "chain_member_unknown": (
            "  Inlämningsprojektet: kunde inte kontrollera ditt medlemskap (det\n"
            "  är inget problem – det stoppar inte inlämningen)."
        ),
        "confidential_failed": (
            "Fel: inlämningen skapades, men GitLab markerade den INTE som\n"
            "konfidentiell (confidential). Det betyder att personer som inte\n"
            "ska kunna läsa den kanske kan det.\n"
            "\n"
            "Din inlämning räknas INTE som inlämnad. Gör så här:\n"
            "  1. Skapa ingen ny inlämning.\n"
            "  2. Kontakta kursledningen och skicka den här länken:\n"
            "     {issue_url}\n"
        ),
        "bot_delay": (
            "  Din inlämning är registrerad. Två saker läggs till i efterhand,\n"
            "  automatiskt:\n"
            "    - etiketterna (labels) som handledarna filtrerar på\n"
            "    - din labbpartners åtkomst till inlämningen\n"
            "  Du behöver inte göra något om de inte finns där än, och du ska\n"
            "  INTE lämna in igen för att få det att hända. Kan din partner\n"
            "  fortfarande inte öppna inlämningen nästa dag: kontakta\n"
            "  kursledningen.\n"
        ),
        "bot_delay_solo": (
            "  Din inlämning är registrerad. Etiketterna (labels) som\n"
            "  handledarna filtrerar på läggs till i efterhand, automatiskt –\n"
            "  du behöver inte göra något, och du ska INTE lämna in igen för att\n"
            "  få det att hända.\n"
        ),
        "bot_delay_partner_only": (
            "  Din inlämning är registrerad. Din labbpartners åtkomst till\n"
            "  inlämningen läggs till i efterhand, automatiskt. Du behöver inte\n"
            "  göra något om den inte finns där än, och du ska INTE lämna in igen\n"
            "  för att få det att hända. Kan din partner fortfarande inte öppna\n"
            "  inlämningen nästa dag: kontakta kursledningen.\n"
        ),
        "partner_unknown_user": (
            "    Fel: det finns ingen GitLab-användare som heter '{username}'.\n"
            "\n"
            "    Två vanliga orsaker:\n"
            "      - felstavat användarnamn, eller\n"
            "      - din partner har aldrig loggat in på {gitlab_url}. Det är\n"
            "        den första inloggningen som skapar kontot.\n"
            "\n"
            "    Be din partner öppna {gitlab_url}, logga in en gång, och läsa\n"
            "    av användarnamnet på sin egen profilsida.\n"
        ),
        "partner_suggestions": "    Menade du någon av dessa? {suggestions}",
        "partner_try_again": (
            "    Skriv om namnet ({attempts} försök kvar), eller tryck Enter för att hoppa över."
        ),
        "partner_giving_up": (
            "    Avbryter här, så att inget registreras med en partner som inte\n"
            "    går att hitta. Ingenting är inlämnat. Ta reda på rätt\n"
            "    användarnamn och kör skriptet igen – dina commits och din push\n"
            "    finns kvar, så du gör bara om den här dialogen."
        ),
        "partner_verify_failed": (
            "    Fel: kunde inte kontrollera om '{username}' finns (GitLab\n"
            "    svarade inte). Kontrollera nätverket och försök igen – en\n"
            "    felstavad partner får aldrig åtkomst till inlämningen, så\n"
            "    skriptet gissar inte."
        ),
        "unknown_handin_id": (
            "Fel: '{lab_id}' är ingenting du kan lämna in i den här kursen.\n"
            "\n"
            "Ingenting är inlämnat. Det du kan lämna in är:\n"
            "\n"
            "{valid}\n"
            "\n"
            "Skriv namnet i vänstra kolumnen precis som det stavas:\n"
            "    python3 handin.py {example}\n"
            "\n"
            "Eller hoppa över det, och välj i menyn i stället:\n"
            "    python3 handin.py\n"
        ),
        "unknown_handin_id_suggestion": "  Menade du '{suggestion}'?\n",
        "confirm_header": "-- Bekräftelse: kursens regler för hjälpmedel --",
        "confirm_question_short": (
            "Har du följt kursens regler för hjälpmedel, inklusive reglerna för AI?"
        ),
        "confirm_rules_line": "  Reglerna står här: {rules_url}",
        "confirm_instruction_yes": "  Ja – jag har följt dem:  skriv  {word}",
        "confirm_instruction_no": "  Nej – det har jag inte:  skriv  {decline_word}",
        "confirm_prompt": "  Ditt svar: ",
        "confirm_retry": "  Det där var varken {word} eller {decline_word}. Skriv ett av dem ({left} kvar).",
        "confirm_ok": "  Tack – bekräftelsen följer med inlämningen.",
        "confirm_declined_intro": (
            "  Tack för det tydliga svaret. Inget händer automatiskt av det här:\n"
            "  beskriv med egna ord vad du har gjort, så följer beskrivningen med\n"
            "  inlämningen och en lärare kan läsa den bredvid koden.\n"
            "\n"
            "  Skriv en eller flera rader. Avsluta med en tom rad.\n"
        ),
        "confirm_declined_prompt": "  > ",
        "confirm_declined_empty": (
            "  Beskrivningen kan inte vara tom – en tom beskrivning säger ingenting\n"
            "  om vad som hänt. Skriv minst en rad, sedan en tom ({left} kvar)."
        ),
        "confirm_declined_ok": "  Tack – beskrivningen följer med inlämningen.",
        "confirm_unclear": (
            "  Inget tydligt svar, så INGENTING har lämnats in.\n"
            "\n"
            "  Det är med flit. En inlämning märkt \"ej bekräftat\" läses som att du\n"
            "  har sagt att reglerna inte följts, och det får aldrig hända bara för\n"
            "  att du råkade trycka Enter eller stänga fönstret.\n"
            "\n"
            "  Dina commits och din push ligger kvar. Kör skriptet igen och svara\n"
            "  med ordet som visas, eller med {decline_word} om du hellre vill\n"
            "  beskriva vad du har gjort.\n"
        ),
        "confirm_cancelled": "  Avbrutet. Ingenting har lämnats in.",
        "labels_dropped": (
            "  Obs: inlämningen skapades, men GitLab satte inte etiketten\n"
            "  (label) {labels} på den.\n"
            "  Din inlämning gäller ändå – etiketterna används bara av kursen för\n"
            "  att sortera inlämningar. Säg till din handledare om din inlämning\n"
            "  inte dyker upp där den ska."
        ),
        "summary_warnings_header": "  En sak blev inte som den skulle:",
        "not_a_git_repo": (
            "Fel: det här skriptet måste köras inifrån ett git-repository.\n"
            "  Byt till din projektmapp med cd och försök igen."
        ),
        "no_origin_remote": (
            "Fel: hittar ingen fjärrkoppling (remote) som heter 'origin' i repot.\n"
            "  Kontrollera att ditt repo (repository) har en 'origin' inställd.\n"
            "  Du kan lägga till en med: git remote add origin <url>"
        ),
        "no_commits_yet": (
            "Fel: det finns ingen commit i det här repot än.\n"
            "\n"
            "  En inlämning ÄR en commit: verktyget sätter en tagg på den senaste,\n"
            "  och det är den versionen handledaren läser. Det finns alltså\n"
            "  ingenting att lämna in förrän du har gjort en.\n"
            "\n"
            "  Gör så här i din projektmapp:\n"
            "    1. git add <dina filer>\n"
            "    2. git commit -m \"Min lösning\"\n"
            "    3. git push\n"
            "    4. kör det här skriptet igen."
        ),
        "oauth_box_title": "  Logga in med din webbläsare",
        "oauth_box_code": "  Kod: {code}",
        "oauth_box_auto": "  En webbläsare ska öppnas automatiskt.",
        "oauth_box_manual": "  Om inte: öppna den här adressen själv:",
        "oauth_waiting": "  Väntar på att du ska logga in ... (avbryt med Ctrl+C)",
        "oauth_login_ok": "  [OK] Inloggningen lyckades!",
        "oauth_timed_out": "  Inloggningen tog för lång tid. Försök igen.",
        "oauth_denied": "  Inloggningen nekades.",
        "oauth_failed": "  Inloggningen misslyckades ({error}). Vi provar med en åtkomsttoken i stället.",
        "auth_cache_cleared": (
            "  Den sparade inloggningen i {path} nekades av GitLab och har tagits bort.\n"
            "  Kör kommandot igen, så får du logga in på nytt."
        ),
        "gitlab_connect_failed": (
            "Fel: kunde inte nå GitLab på {gitlab_url}\n"
            "  Detaljer: {details}\n"
            "  Kontrollera din internetanslutning och försök igen."
        ),
        "gitlab_auth_failed": "Fel: GitLab-inloggningen misslyckades. Din token kan vara ogiltig eller ha gått ut.",
        "token_instructions": (
            "Om du HAR loggat in tidigare: skapa en ny personlig åtkomsttoken\n"
            "(personal access token):\n"
            "  1. Gå till {gitlab_url}/-/user_settings/personal_access_tokens\n"
            "  2. Skapa en token med behörigheten (scope) 'api'\n"
            "  3. Spara den på något av de här ställena:\n"
            "     - Miljövariabel: export GITLAB_TOKEN=<din-token>\n"
            "     - Konfigurationsfil: ~/.config/handin/token"
        ),
        "deadline_header": "-- Deadlines --",
        "deadline_past": "  Senaste deadline för {handin}: {when} (för {days} dagar sedan){extra}",
        "deadline_next": "  Nästa deadline för {handin}:   {when} (om {days} dagar){extra}",
        "deadline_none": "  Det finns ingen deadline för den här inlämningen.",
        "deadline_extra": " – {text}",
        "help_description": "Skript för inlämning i kursen",
        "help_usage_prefix": "användning: ",
        "help_error_prefix": "fel",
        "help_positionals_title": "argument",
        "help_optionals_title": "flaggor",
        "help_help": "visa den här hjälptexten och avsluta",
        "help_lab_id_metavar": "<labb-id>",
        "help_lab_id": "Vad du lämnar in. Utelämna det för att välja i en meny.",
        "help_dry_run": "Visa vad som skulle hända, utan att pusha, tagga, ladda upp eller skapa något",
        "help_check": "Kontrollera den committade labben på din dator, utan inloggning och utan inlämning",
        "help_status": "Visa dina senaste inlämningar utan att lämna in",
        "help_no_config_update": "Använd inte kursens centrala inställningar, bara din egen handin.toml",
        "help_config": "Sökväg till en handin.toml",
        "help_config_metavar": "FIL",
        "help_debug": "Visa alla tekniska detaljer (git-kommandon, anrop till GitLab, hela felet) – bra när du ber om hjälp",
        "dry_header": "-- Torrkörning: så här hade inlämningen sett ut --",
        "value_yes": "ja",
        "value_no": "nej",
        "language_name_en": "engelska",
        "language_name_sv": "svenska",
        "partner_policy_bot": "kursens bot lägger till din labbpartner i ärendet",
        "partner_policy_off": "din labbpartner nämns bara i ärendet",
        "dry_type": "  Inlämning: {name} ({id})",
        "dry_repo": "  Ditt repo (repository): {repo}",
        "dry_target": "  Inlämningsprojektet: {target}",
        "dry_labels": "  Etiketter (labels): {labels}",
        "dry_confidential": "  Konfidentiellt: {value}",
        "dry_language": "  Meddelandespråk: {language}",
        "dry_partner_policy": "  Din labbpartner: {policy}",
        "dry_bot_active": "  Boten aktiv (fyller i etiketter och tilldelning): {value}",
        "dry_bot_active_access": "  Boten aktiv (fyller i partnerns åtkomst): {value}",
        "dry_confirm": "  Bekräftelse om kursens regler för hjälpmedel: {state} (ordet som visades: {word})",
        "dry_confirm_yes": "bekräftad",
        "dry_confirm_no": "nekad, med en beskrivning",
        "dry_confirm_skipped": "  Bekräftelse om kursens regler för hjälpmedel: inte efterfrågad ([confirmation] enabled = false)",
        "dry_group": "  Gruppmedlemmar: {members}",
        "dry_supersedes": "  Ersätter: #{iid}",
        "dry_message": "  Meddelande: {message}",
        "dry_no_issue": "  Ingenting pushades, taggades, laddades upp eller skapades – det här var en torrkörning.",
        "fatal_unexpected": "Ett oväntat fel uppstod:",
        "fatal_contact": "Om felet återkommer: hör av dig till en handledare\noch ta med felmeddelandet ovanför.",
        "env_header": "-- Kontrollerar din miljö --",
        "env_no_auth": "  Ingen inloggning hittades. Du behöver en personlig åtkomsttoken (personal access token) till GitLab.",
        "env_pat_opening": "  1. Öppnar: {url}",
        "env_pat_steps": (
            "  2. Klicka på 'Add new token'\n"
            "  3. Name: skriv 'handin' (eller vad du vill)\n"
            "  4. Scopes: kryssa i 'api'\n"
            "  5. Klicka på 'Create personal access token'\n"
            "  6. Kopiera din token (den börjar med 'glpat-')"
        ),
        "env_pat_hidden": "  Det du klistrar in syns inte på skärmen. Klistra in din token och tryck Enter.",
        "env_pat_prompt": "  Klistra in din token här: ",
        "env_pat_none_entered": "  Ingen token angavs.",
        "env_pat_saved": "  Din token sparades i {path}",
        "env_pat_not_saved": (
            "  Obs: din token kunde INTE sparas i {path}\n"
            "  ({reason}).\n"
            "  Det är ett rättighetsproblem i den mappen – inte något fel på\n"
            "  din token – och inlämningen fortsätter med den du just klistrade\n"
            "  in. Du får ange den igen nästa gång. Vill du slippa frågan kan du\n"
            "  i stället lägga din token i miljövariabeln GITLAB_TOKEN."
        ),
        "token_file_unreadable": (
            "  Obs: den sparade token i {path} gick inte att läsa ({reason}).\n"
            "  Vi fortsätter som om den inte fanns."
        ),
        "env_repository": "  Ditt repo: {repo_name}",
        "env_authenticated": "  Inloggad som: {username}",
        "uncommitted_header": (
            "Varning: det finns ändringar i repot som INTE kommer med i\n"
            "  inlämningen:"
        ),
        "uncommitted_unstaged": "  Ändrade men inte tillagda med git add ({count}):",
        "uncommitted_staged": "  Tillagda med git add men inte committade ({count}):",
        "uncommitted_untracked": "  Filer som git inte känner till ({count}):",
        "uncommitted_more": "    ... och {count} till",
        "uncommitted_question": "Vill du lämna in den senaste commiten utan de här ändringarna?",
        "uncommitted_cancelled": "Inlämningen avbröts. Gör en commit av dina ändringar och försök igen.",
        "submission_cancelled": "Inlämningen avbröts.",
        "push_no_upstream": "  Din gren (branch) '{branch}' följer ingen gren på GitLab.",
        "push_offer_upstream": "  Vill du pusha den nu med 'git push -u origin {branch}'?",
        "push_branch_failed": "Fel: kunde inte pusha grenen till GitLab.",
        "push_ok": "  Pushen lyckades.",
        "push_warn_no_analysis": "Varning: utan en push skapas ingen kodanalys.",
        "push_continue_without": "  Vill du fortsätta utan att pusha?",
        "push_unpushed": "  Du har {count} commit{plural} som inte har pushats.",
        "push_needed_for_analysis": "  Pushen behövs för att en kodanalys ska kunna skapas.",
        "push_warn_not_pushed": "Varning: utan en push visar din gren på GitLab inte den kod du lämnar in.",
        "push_needed_reason": "  Pushen gör att din gren på GitLab visar samma version som du lämnar in.",
        "push_now": "  Vill du pusha nu?",
        "push_failed": "Fel: kunde inte pusha dina ändringar till GitLab.",
        "push_manual": (
            "  Har din partner (eller du från en annan dator) pushat något nytt\n"
            "  måste du först hämta det med `git pull`. Pusha sedan och försök igen."
        ),
        "lint_reads_worktree": (
            "  Obs: kontrollerna läser filerna som de ser ut på disken just nu,\n"
            "  alltså inklusive de {count} ändringar du nyss valde att lämna\n"
            "  utanför inlämningen. Resultatet här är därför inte nödvändigtvis\n"
            "  resultatet för det du lämnar in."
        ),
        "push_failed_tag_still_works": (
            "  Själva inlämningen hänger inte på den här pushen: taggen sätts på\n"
            "  din senaste commit, och när taggen pushas följer den commiten med\n"
            "  till GitLab. Det är taggen din handledare läser."
        ),
        "selected_handin": "  Vald inlämning: {name}",
        "select_handin_prompt": "Välj vad du lämnar in:",
        "menu_enter_choice": "Ditt val: ",
        "yesno_yes_default": "[J/n]",
        "yesno_no_default": "[j/N]",
        "yesno_unrecognised": "  Svara ja eller nej (j/n).",
        "menu_cancelled": "Inlämningen avbruten.",
        "check_interrupted": "Kontrollen avbröts. Ingenting lämnades in.",
        "input_ended": (
            "Inmatningen tog slut: det finns ingen terminal kvar att svara i.\n"
            "  Ingenting har lämnats in. Kör kommandot igen i en terminal."
        ),
        "menu_need_number": "  Skriv en siffra.",
        "menu_not_a_number": "  '{raw}' är inte en giltig siffra. Försök igen.",
        "menu_out_of_range": "  Skriv en siffra mellan 1 och {count}.",
        "dry_run_banner": "  *** TORRKÖRNING – ingenting pushas, taggas, laddas upp eller skapas ***",
        "dry_push_skipped": "  Torrkörning: en riktig inlämning hade erbjudit dig att pusha nu. Ingenting pushas.",
        "dry_report_not_uploaded": "  Torrkörning: {name} laddas inte upp.",
        "analysis_header": "-- Kontrollerar kodanalysen --",
        "analysis_commit": "  Nuvarande commit: {commit}",
        "analysis_unavailable": "  Kunde inte kontrollera kodanalysen: {error}",
        "analysis_continuing": "  Fortsätter utan kodanalys.",
        "analysis_none_found": "  Ingen kodanalys hittades för den här commiten.",
        "analysis_none_why": "  Det kan betyda:",
        "analysis_none_pending": "    - analysen har inte hunnit köra än (försök igen om några minuter)",
        "analysis_none_down": "    - analyssystemet ligger nere just nu",
        "analysis_continue_q": "  Fortsätta utan kodanalys?",
        "analysis_cancelled": "Inlämningen avbruten. Försök igen när kodanalysen är tillgänglig.",
        "analysis_found": "  Kodanalys hittad: {url}",
        "analysis_problems": "  Kodanalysen visar problem som måste rättas först.",
        "analysis_no_data": "  Kodanalys hittad (ingen inbäddad data att kontrollera).",
        # Numerus-neutrala med avsikt: {plural} är engelskans ("s" när count
        # != 1), och de här två böjde ett svenskt substantiv efter den, så
        # count = 3 gav "3 kritiskt fels" och "3 allvarlig varnings". Formen
        # "<sak>: {count}" stämmer i båda numerus utan att böja någonting, och
        # analysis_setup bar redan den sortens lösning (inget {plural} alls).
        # Anropsställena skickar fortfarande plural=; extra kwargs är ofarliga
        # för str.format. Engelskan är oförändrad -- Javakursen delar filen.
        "analysis_critical": "  [!]  Kodanalysen hittade kritiska fel: {count}.",
        "analysis_fatal": "  [!]  Kodanalysen hittade allvarliga varningar: {count}.",
        "analysis_setup": "  [!]  Kodanalysen hittade problem med projektuppsättningen: {count}.",
        "existing_checking": "  Letar efter tidigare inlämningar ...",
        "existing_check_failed": (
            "  Varning: kunde inte kontrollera om du har lämnat in det här förut:\n"
            "  {details}\n"
            "  Har du det, blir en inlämning nu ett ANDRA ärende för samma uppgift i\n"
            "  stället för en ersättning av det första. Säkrast: avbryt, vänta en\n"
            "  minut och kör kommandot igen."
        ),
        "existing_none": "  Ingen tidigare inlämning hittades.",
        "existing_found_header": "  Tidigare inlämning(ar) hittades:",
        "existing_supersede_question": (
            "Det finns redan en inlämning för den här uppgiften (se ovan).\n"
            "  Vill du ersätta den med en ny inlämning?"
        ),
        "existing_will_supersede": "  Ersätter ärende #{iid}.",
        "validation_running": "  Kör kontroller ...",
        "validation_cannot_run": "  Varning: kunde inte köra kontrollskriptet: {details}",
        "validation_passed": "  Kontrollerna gick igenom.",
        "validation_failed": "Kontrollerna misslyckades. Rätta problemen ovan innan du lämnar in.",
        "validation_warnings_question": "Kontrollerna gav varningar (se ovan). Vill du fortsätta ändå?",
        "validation_unexpected_code": "  Varning: kontrollskriptet avslutades med den oväntade koden {code}.",
        "checklist_header": "-- Checklista före inlämning --",
        "checklist_stop": (
            "  Du svarade nej på:\n"
            "    {question}\n"
            "\n"
            "  Det måste vara gjort innan du lämnar in, så inlämningen stoppades här.\n"
            "  INGENTING har lämnats in: ingen tagg (tag) och inget ärende (issue)\n"
            "  har skapats.\n"
            "\n"
            "  När det är gjort: kör samma kommando igen.\n"
        ),
        "message_header": "-- Frivilligt meddelande --",
        "message_prompt": "  Meddelande till handledaren (tryck Enter för att hoppa över): ",
        "message_recorded": "  Meddelande sparat: {message}",
        "message_none": "  Inget meddelande.",
        "tag_header": "-- Skapar en tagg (tag) för inlämningen --",
        "tag_create_failed": "Fel: kunde inte skapa taggen '{tag}'.",
        "tag_pushing": "  Pushar taggen {tag} ...",
        "tag_push_failed": "Fel: kunde inte pusha taggen '{tag}' till GitLab.",
        "tag_resolve_and_retry": "  Lös problemet och försök igen.",
        "tag_created": "  Taggen {tag} skapades på commit {commit}.",
        "tag_fetch_checking": "  Kontrollerar vilka taggar som redan finns hos GitLab ...",
        "tag_fetch_timeout": (
            "  GitLab svarade inte inom {seconds} sekunder, så taggarna där kunde\n"
            "  inte kontrolleras. Fortsätter med de taggar som finns i din klon."
        ),
        "tag_fetch_failed": (
            "  Taggarna hos GitLab kunde inte kontrolleras.\n"
            "  Fortsätter med de taggar som finns i din klon."
        ),
        "submit_header": "-- Lämnar in --",
        "issue_create_failed": (
            "Fel: kunde inte skapa ärendet (issue) på GitLab.\n"
            "  Detaljer: {details}"
        ),
        "issue_tag_pushed_contact_ta": (
            "  Din tagg '{tag}' är pushad. Du kan kontakta handledaren och\n"
            "  be hen registrera din inlämning för hand."
        ),
        "issue_contact_ta": "  Kontakta kursens handledare för hjälp.",
        "confidential_repaired": (
            "  Obs: inlämningen behövde markeras som konfidentiell\n"
            "  (confidential) i ett andra steg. Den är konfidentiell nu, och\n"
            "  din inlämning är registrerad."
        ),
        "ai_review_header": "-- AI-förgranskning --",
        "ai_review_running": "  Kör en automatisk granskning (det kan ta några minuter) ...",
        "ai_review_timeout": "  Varning: AI-granskningen tog mer än 10 minuter och avbröts.",
        "ai_review_failed": "  Varning: AI-granskningen misslyckades: {details}",
        "ai_review_error": "  Varning: AI-granskningen svarade med ett fel.",
        "ai_review_no_findings": "  Granskningen hittade ingenting.",
        "ai_review_unparsable": "  Varning: kunde inte tolka svaret från AI-granskningen.",
        "ai_review_blocking": (
            "  AI-granskningen hittade problem. Åtgärda dem innan du lämnar in.\n"
            "  (Sätt block_on_errors = false i konfigurationen för att lämna in ändå.)"
        ),
        "report_header": "-- Välj rapportfil --",
        "report_allowed_formats": "  Tillåtna format: {formats}",
        "report_max_size": "  Största storlek: {max_mb} MB",
        "report_not_found": "  Fel: hittar inte filen: {path}",
        "report_bad_format": "  Fel: filformatet '.{ext}' är inte tillåtet.",
        "report_too_large": "  Fel: filen är för stor ({size} MB, högst {max_mb} MB).",
        "report_not_pdf": "  Fel: filen verkar inte vara en giltig PDF.",
        "report_unreadable": "  Fel: kunde inte läsa filen: {details}",
        "report_too_few_pages": "  Fel: rapporten har {pages} sidor (minst {minimum}).",
        "report_too_many_pages": "  Fel: rapporten har {pages} sidor (högst {maximum}).",
        "report_page_count": "  Antal sidor: {pages}",
        "report_page_count_unknown": "  Varning: kunde inte kontrollera antalet sidor (PyMuPDF saknas).",
        "report_selected": "  Vald fil: {name} ({size} MB)",
        "report_none_selected": "Ingen rapportfil vald. Inlämningen avbröts.",
        "upload_running": "  Laddar upp rapportfilen ...",
        "upload_ok": "  [OK] Uppladdad: {name}",
        "upload_failed": (
            "Fel: kunde inte ladda upp filen till GitLab.\n"
            "  Detaljer: {details}"
        ),
        "upload_retry": "  Du kan försöka lämna in igen, eller kontakta kursens handledare.",
        "lint_header": "-- Kontroller före inlämning --",
        "lint_running": "  Kör: {name} ...",
        "lint_timeout": "    [!]  Tog mer än 120 sekunder och avbröts",
        "lint_cannot_run": "    [!]  Kunde inte köra kontrollen: {details}",
        "lint_skipped_check_said": (
            "    [-]  Överhoppad: kontrollen kunde inte bedöma din kod här, så den\n"
            "         räknas inte emot inlämningen. Kontrollen säger:"
        ),
        "lint_skipped_no_tool": (
            "    [-]  Överhoppad: programmet kontrollen behöver finns inte här.\n"
            "         Det säger ingenting om din kod, så det räknas inte emot\n"
            "         inlämningen.\n"
        ),
        "uv_sync_running": "  Förbereder: uv sync --frozen i {dir} ...",
        "uv_sync_ok": "    [OK]   Klar",
        "uv_sync_no_tool": "    [-]  uv finns inte här, så kontrollerna som behöver det hoppas över.",
        "uv_sync_file_absent": (
            "    [-]  {path} finns inte i din senaste commit, så kontrollerna som\n"
            "         använder uv kan inte köras."
        ),
        "uv_sync_file_uncommitted": (
            "    [-]  {path} finns bara i din mapp, inte i din senaste commit, så\n"
            "         kontrollerna som använder uv kan inte köras. Lägg till den med\n"
            "         git add {path} och git commit, och kör igen."
        ),
        "uv_sync_file_ignored": (
            "    [-]  {path} finns i din mapp, men git ignorerar den (.gitignore), så\n"
            "         den finns inte i din commit och kontrollerna som använder uv kan\n"
            "         inte köras. Ta bort raden i .gitignore som gäller filen, committa\n"
            "         filen och kör igen."
        ),
        "uv_sync_broken": (
            "    [!]  uv kunde inte läsa projektets filer i {dir} (se uv:s besked nedan).\n"
            "         Rätta filen – till exempel en konflikt från git merge – och\n"
            "         committa den."
        ),
        "uv_sync_failed": (
            "    [-]  Miljön kunde inte förberedas (uv sync --frozen misslyckades).\n"
            "         Oftast saknas nät, och uv har inte paketen sparade sedan tidigare.\n"
            "         Det säger ingenting om din kod."
        ),
        "lint_skipped_env": "    [-]  Kunde inte kontrolleras: miljön i {dir} gick inte att förbereda (se ovan).",
        "lint_passed": "    [OK]   Godkänd",
        "lint_failed_line": "    [FAIL] Underkänd ({marker})",
        "lint_advice_line": "    [RÅD]  Råd att se över (påverkar inte inlämningen)",
        "lint_marker_required": "OBLIGATORISK",
        "lint_marker_optional": "spärrar inte inlämningen",
        "lint_required_failed": "  Obligatoriska kontroller underkändes. Rätta problemen ovan.",
        "lint_optional_failed": "  Några kontroller underkändes. Kraven gäller ändå vid bedömningen.",
        "lint_continue_anyway": "  Vill du lämna in ändå?",
        "lint_checks_failed_exit": "Kontrollerna underkändes. Rätta problemen ovan innan du lämnar in.",
        "supersede_marked": "  Ärende #{iid} är markerat som ersatt.",
        "supersede_failed": "  Varning: kunde inte markera det gamla ärendet #{iid} som ersatt: {details}",
        "group_header": "-- Gruppmedlemmar --",
        "group_intro": (
            "  Det här är en gruppinlämning (högst {max_size} personer).\n"
            "  Skriv dina gruppmedlemmars användarnamn på GitLab, ett per rad.\n"
            "  Tryck Enter på en tom rad när du är klar (högst {max_additional} till)."
        ),
        "group_member_prompt": "  Medlem {n}: ",
        "group_summary": "  Grupp: du + {members}",
        "banner_subtitle": "Skript för inlämning",
        "issue_created": "  Ärende skapat: {url}",
        "submitted_ok": "Inlämningen är gjord!",
        "existing_check_failed_question": "  Lämna in ändå?",
        "existing_incomplete": "GitLab säger att det finns fler ärenden än sökningen fick med, så listan är ofullständig.",
        "issue_create_recovered": "  Svaret från GitLab kom bort, men ärendet skapades – det finns här: {url}",
        "interrupted_after_tag": (
            "Avbrutet. Din tagg '{tag}' är pushad, men INGET inlämningsärende skapades,\n"
            "  så ingenting är inlämnat än. Kör kommandot igen för att lämna in."
        ),
        "interrupted_before_issue": (
            "Avbrutet medan taggen gjordes. Inget inlämningsärende skapades, så\n"
            "  ingenting är inlämnat än. Kör kommandot igen för att lämna in."
        ),
        "interrupted_issue_uncertain": (
            "Avbrutet medan ärendet skapades. Det är OKLART om din inlämning\n"
            "  registrerades. Lämna inte in igen direkt: öppna\n"
            "  {issues_url} och se efter om ditt ärende finns där.\n"
            "  Finns det inte: kör kommandot igen."
        ),
        "interrupted_issue_unconfirmed": (
            "Avbrutet precis efter att ditt ärende skapades: {url}\n"
            "  Verktyget hann inte kontrollera att det är konfidentiellt. Öppna det:\n"
            "  visar det inte en ögonsymbol med 'Confidential' (eller 'Konfidentiell'),\n"
            "  säg till kursens personal."
        ),
        "interrupted_after_issue": (
            "Avbrutet, men din inlämning ÄR registrerad: {url}\n"
            "  Lämna inte in igen."
        ),
        "interrupted_supersede": (
            "Avbrutet, men din inlämning ÄR registrerad: {url}\n"
            "  Det gamla ärendet #{iid} markerades kanske inte som ersatt. Det spelar\n"
            "  ingen roll för den nya inlämningen; lämna inte in igen."
        ),
        "git_missing": "Fel: programmet git hittades inte.\n  Inlämningsverktyget använder git för att tagga din kod. Installera Git\n  (på Windows: https://git-scm.com/download/win), öppna sedan en NY\n  terminal och försök igen. Kontrollera att det fungerar med: git --version",
        "moved_to_script_repo": "  Obs: du står inte i ett git-repo, så verktyget använder repot som\n  handin.py själv ligger i:\n    {path}",
        "chain_member_not_needed": "  Inlämningsprojektet: du är inte medlem – det behövs inte för den här\n  inlämningen.",
        "supersede_commented": "  Det gamla ärendet #{iid} har fått en kommentar som pekar på den nya\n  inlämningen.",
        "template_hint": (
            "  Kursen har nya eller rättade filer i labbmallen som du inte har\n"
            "  hämtat än, till exempel:\n"
            "{files}\n"
            "  Hämta dem med `{python} uppdatera.py` i ditt labbrepos mapp. Hör de\n"
            "  till labben du lämnar in nu: avbryt med Ctrl+C, hämta dem och börja\n"
            "  om. Annars kan du fortsätta."
        ),
        "lint_output_more": "       ({count} tidigare rader utelämnade – mer av utdata finns i loggen)",
        "log_saved": "  En logg över körningen finns här:\n    {path}\n  Behöver du hjälp: skicka filen till en handledare. Lösenord och\n  token är maskade i den.",
        "fatal_where": "  (Steg: {step}. Inlämningsverktyget {version}, Python {python}, {system}.)",
        "fatal_debug_hint": "  Kör samma kommando med --debug för att se alla detaljer.",
        "oauth_unavailable": "  Inloggningen via webbläsaren gick inte att starta ({reason}).\n  Du kan använda en personlig åtkomsttoken (personal access token) i stället.",
        "oauth_poll_network": "  (Kunde inte nå GitLab just nu: {reason}. Försöker igen ...)",
        "oauth_rejected_rerun": "  GitLab godtar inte längre din sparade inloggning, så den har tagits bort.\n  Kör samma kommando igen: du får då logga in i webbläsaren på nytt.",
        "issue_create_uncertain": "Fel: svaret från GitLab kom bort eller var ett felsvar när ärendet\n  skulle skapas ({details}).\n  Det är OKLART om din inlämning registrerades. Lämna inte in igen direkt:\n  öppna {issues_url} och se efter om ditt ärende finns där.\n  Finns det inte: kör kommandot igen.",
        "token_instructions_powershell": "     - I PowerShell på Windows: $env:GITLAB_TOKEN=\"<din-token>\"",
        "lint_output_more_nolog": "       ({count} tidigare rader utelämnade)",
        "oauth_rejected_manual": "  GitLab godtar inte längre din sparade inloggning, och filen gick inte\n  att ta bort automatiskt. Ta bort den här filen och kör kommandot igen:\n    {path}",
        "supersede_comment_failed": "  Varning: kunde inte lägga en kommentar på det gamla ärendet #{iid}: {details}\n  Din nya inlämning gäller ändå. Säg till din handledare att den ersätter #{iid}.",
    },
}


def msg(config: HandinConfig | None, key: str, **kwargs: Any) -> str:
    """Return a student-facing message in the configured language.

    Falls back to English if the language or the key is missing, so a
    half-translated table can never crash a hand-in.

    Args:
        config: Loaded configuration (supplies the language), or None where
            no configuration is in scope; the language is then
            MESSAGE_LANGUAGE.
        key: Message key in MESSAGES.
        **kwargs: Values substituted into the message template.

    Returns:
        The formatted message string.
    """
    # config=None är tillåtet. Det finns för två fall: hjälpare vars
    # anropssignatur pinnas av ett test (prompt_choice), och `__main__`:s
    # avbrotts- och kraschhanterare, som körs utanför main() och aldrig kan ha
    # config i scope. Språket tas då ur MESSAGE_LANGUAGE, som sätts när konfigurationen
    # har lästs — faller den tillbaka på "en" är det för att avbrottet kom
    # innan filen ens gick att läsa, och då är engelska rätt svar.
    language = getattr(getattr(config, "course", None), "language", None) or MESSAGE_LANGUAGE
    table = MESSAGES.get(language, MESSAGES["en"])
    template = table.get(key) or MESSAGES["en"].get(key, "")
    try:
        return template.format(**kwargs)
    except KeyError:
        # A missing placeholder must never break a submission; show the raw text.
        return template


def _peek_language(argv: list[str]) -> str:
    """The course language, read before argument parsing so `--help` can use it.

    Looks where `find_config_file` would, minus the git call: an explicit
    `--config`, then handin.toml next to this script, then the user-level
    file. Never raises and never prints; anything unreadable means English.
    """
    candidates: list[Path] = []
    for index, arg in enumerate(argv):
        # argparse accepts any unambiguous prefix: --co, --con, ... --config.
        name, eq, value = arg.partition("=")
        if len(name) < 4 or not "--config".startswith(name):
            continue
        if eq:
            candidates.append(Path(value).expanduser())
        elif index + 1 < len(argv):
            candidates.append(Path(argv[index + 1]).expanduser())
    candidates.append(Path(__file__).resolve().parent / "handin.toml")
    candidates.append(Path.home() / ".config" / "handin" / "handin.toml")
    for candidate in candidates:
        try:
            import tomllib

            with open(candidate, "rb") as handle:
                raw = tomllib.load(handle)
        except Exception:  # noqa: BLE001, S112 - a guess at the language must never stop a run
            continue
        course = raw.get("course") if isinstance(raw, dict) else None
        language = course.get("language") if isinstance(course, dict) else None
        if isinstance(language, str) and language.strip().lower() in MESSAGES:
            return language.strip().lower()
        return "en"
    return "en"


class _HelpFormatter(argparse.HelpFormatter):
    """argparse's formatter with the "usage: " prefix in the course language."""

    usage_prefix = "usage: "

    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[override]
        return super().add_usage(usage, actions, groups, prefix=prefix or self.usage_prefix)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose own error line is in the course language."""

    error_prefix = "error"

    def error(self, message: str) -> Any:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: {self.error_prefix}: {message}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments, with `--help` in the course language (2.17.0)."""
    arguments = sys.argv[1:] if argv is None else argv
    language = _peek_language(arguments)
    # Until main() has read the configuration, this is the best guess at the
    # language -- so an argparse error and the run-log lines after it are not
    # English in a Swedish course.
    global MESSAGE_LANGUAGE
    MESSAGE_LANGUAGE = language

    def text(key: str) -> str:
        return MESSAGES.get(language, MESSAGES["en"]).get(key) or MESSAGES["en"][key]

    formatter = type("_CourseHelpFormatter", (_HelpFormatter,), {"usage_prefix": text("help_usage_prefix")})
    parser = _Parser(description=text("help_description"), formatter_class=formatter, add_help=False)
    parser.error_prefix = text("help_error_prefix")
    parser._positionals.title = text("help_positionals_title")
    parser._optionals.title = text("help_optionals_title")
    parser.add_argument("-h", "--help", action="help", help=text("help_help"))
    # Optional, and optional in the strong sense: with no id the tool behaves
    # exactly as it always has (menu). The id cannot be validated here — the
    # set of valid ids lives in handin.toml, which is loaded later — so this
    # only captures the string; resolve_handin_type() judges it.
    parser.add_argument(
        "lab_id",
        nargs="?",
        default=None,
        metavar=text("help_lab_id_metavar"),
        # No example id here: the ids are course-specific and come from
        # handin.toml. "e.g. lab1" was an invented one, and a student who
        # copied it got "'lab1' is not something you can hand in".
        help=text("help_lab_id"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=text("help_dry_run"))
    mode.add_argument("--check", action="store_true", help=text("help_check"))
    mode.add_argument("--status", action="store_true", help=text("help_status"))
    parser.add_argument("--no-config-update", action="store_true", help=text("help_no_config_update"))
    parser.add_argument("--config", type=str, default=None, metavar=text("help_config_metavar"), help=text("help_config"))
    parser.add_argument("--debug", action="store_true", help=text("help_debug"))
    return parser.parse_args(arguments)


def format_handin_ids(config: HandinConfig) -> str:
    """Format the hand-in types as an indented two-column 'id  name' listing.

    Args:
        config: Loaded configuration.

    Returns:
        One line per hand-in type, indented four spaces. Empty string if the
        configuration declares none.
    """
    if not config.handins:
        return ""
    width = max(len(ht.id) for ht in config.handins)
    return "\n".join(f"    {ht.id:<{width}s}  {ht.name}" for ht in config.handins)


def resolve_handin_type(config: HandinConfig, lab_id: str) -> HandinType:
    """Look up the hand-in type named on the command line.

    Called before any git or network work, so a misspelled id costs a sentence
    instead of a wait, and so the student can never be half-way through a
    submission before the id is judged.

    Args:
        config: Loaded configuration (supplies the valid ids).
        lab_id: The id as typed by the student.

    Returns:
        The matching HandinType.

    Raises:
        SystemExit: If no hand-in type has that id. The message lists the ids
            that do exist, because a beginner cannot act on "unknown id".
    """
    for handin_type in config.handins:
        if handin_type.id == lab_id:
            return handin_type

    example = config.handins[0].id if config.handins else "lab1"
    print()
    print(
        msg(
            config,
            "unknown_handin_id",
            lab_id=lab_id,
            valid=format_handin_ids(config),
            example=example,
        )
    )
    # A near miss is the common case (lab-b1 vs lab-B1, lab1 vs lab-1), and the
    # listing above can be long enough that the student does not spot it.
    #
    # Matched case-INSENSITIVELY, while acceptance above is exact: the labs are
    # written "Labb B1" in the course texts, so an uppercase B is the likeliest
    # typo of them all, and difflib would otherwise score "LAB-B1" against
    # "lab-b1" at 0.0 and say nothing. Suggesting rather than accepting is the
    # same choice this tool already makes for a misspelled partner username —
    # it never hands in something the student did not type.
    candidates = {ht.id.casefold(): ht.id for ht in config.handins}
    close = difflib.get_close_matches(lab_id.casefold(), list(candidates), n=1, cutoff=0.6)
    if close:
        print(msg(config, "unknown_handin_id_suggestion", suggestion=candidates[close[0]]))
    sys.exit(1)


# ─── Helper Functions: Git ──────────────────────────────────────────────────


def run_git(
    *args: str,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = None,
    discard_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Args:
        *args: Git subcommand and arguments.
        check: If True, raise on non-zero exit code.
        capture: If True, capture stdout and stderr.
        timeout: Seconds to wait before giving up, or None to wait forever.
            ``subprocess.TimeoutExpired`` is raised when it passes.
        discard_output: Send stdout and stderr to the null device instead of
            to pipes.

    Returns:
        The completed process result.

    Raises:
        subprocess.TimeoutExpired: If ``timeout`` is set and passes.

    ``discard_output`` exists FOR the timeout path and must be used with it.
    On Windows -- which is what most of the students run -- ``subprocess.run``
    drains the pipes after killing the child, and a GRANDCHILD that inherited
    them (``ssh``, ``git-remote-https``) holds them open; the call that was
    supposed to bound the wait would then block forever on precisely the
    platform the bound is for. With no pipes there is nothing to drain.
    """
    cmd = ["git"] + list(args)
    shown = "git " + " ".join(args)
    debug_echo(shown)
    if DRY_RUN and _git_write_refused(args):
        log_event("git", f"{shown} -> refused (dry run)")
        raise DryRunWriteRefused(f"a dry run never runs '{shown}'")
    try:
        if discard_output:
            result = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=capture,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
            )
    except FileNotFoundError:
        # No git on PATH. Used to surface as "[WinError 2] ..." under "an
        # unexpected error", which tells a beginner nothing.
        log_event("git", f"{shown} -> git not found on PATH")
        print(msg(None, "git_missing"))
        sys.exit(1)
    except subprocess.TimeoutExpired:
        log_event("git", f"{shown} -> timeout after {timeout} s")
        raise
    _log_git(shown, result)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _log_git(shown: str, result: subprocess.CompletedProcess[str]) -> None:
    """One log line per git call: the command, its exit code, and what it said."""
    parts = [f"{shown} -> exit {result.returncode}"]
    out = result.stdout if isinstance(result.stdout, str) else ""
    err = result.stderr if isinstance(result.stderr, str) else ""
    if out.strip():
        parts.append("stdout: " + out.strip()[:300])
    if err.strip():
        parts.append("stderr: " + err.strip()[:1500])
    log_event("git", "\n  ".join(parts))


# Where the student stood when the tool started. Kept because the tool may move
# into its own repository (enter_script_repo_if_outside), and a path the student
# types -- a report file -- means a path relative to where they stood.
ORIGINAL_CWD = Path.cwd()


def _git_toplevel(directory: Path) -> Path | None:
    """The repository root containing `directory`, or None. Never raises."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def enter_script_repo_if_outside() -> Path | None:
    """Move into handin.py's own repository when the student is outside every repo.

    handin.py ships in the root of the student's lab repository, so where the
    SCRIPT lives identifies the repository, while the current directory is
    wherever the terminal happened to open -- often the home directory, or
    VS Code's parent folder. Running `python3 kurs/handin.py lab-a3` from there
    used to stop with "must be run from inside a git repository".

    Deliberately narrow: only when the current directory is in NO repository.
    Standing in a different repository is left alone (a teacher testing the
    tool from its own checkout does exactly that), and the repository name is
    printed in step 0 either way.

    Returns:
        The directory moved to, or None if nothing changed.
    """
    if _git_toplevel(Path.cwd()) is not None:
        return None
    script_repo = _git_toplevel(Path(__file__).resolve().parent)
    if script_repo is None:
        return None
    try:
        os.chdir(script_repo)
    except OSError:
        return None
    log_event("repo", f"cwd {ORIGINAL_CWD} is in no repository; moved to {script_repo}")
    return script_repo


def get_repo_root(config: HandinConfig) -> Path:
    """Get the root directory of the current git repository.

    Args:
        config: Loaded configuration (supplies the message language).

    Returns:
        Path to the repository root.

    Raises:
        SystemExit: If not inside a git repository.
    """
    result = run_git("rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        print(msg(config, "not_a_git_repo"))
        sys.exit(1)
    return Path(result.stdout.strip())


def get_repo_name(config: HandinConfig) -> str:
    """Extract the repository name from the origin remote URL.

    Supports both SSH (git@server:path/name.git) and HTTPS
    (https://server/path/name.git) formats.

    Args:
        config: Loaded configuration (supplies the message language).

    Returns:
        The repository name (final path component without .git).

    Raises:
        SystemExit: If no origin remote is configured.
    """
    result = run_git("remote", "get-url", "origin", check=False)
    if result.returncode != 0:
        print(msg(config, "no_origin_remote"))
        sys.exit(1)

    url = result.stdout.strip()
    # Remove trailing .git if present
    if url.endswith(".git"):
        url = url[:-4]
    # Extract final path component
    name = url.rstrip("/").rsplit("/", 1)[-1]
    # Handle SSH format (git@server:path/name)
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name


def get_project_path() -> str | None:
    """Extract the full GitLab project path from the origin remote URL.

    For example, ``git@gitlab.liu.se:TDDD78-2024/my-project.git``
    returns ``TDDD78-2024/my-project``.

    Returns:
        The full project path, or None if it cannot be determined.
    """
    result = run_git("remote", "get-url", "origin", check=False)
    if result.returncode != 0:
        return None

    _host, path = parse_origin_url(result.stdout)
    return path


def parse_origin_url(url: str) -> tuple[str | None, str | None]:
    """Split a git remote URL into (host, project path).

    Handles the three forms a student can end up with:
    ``git@host:ns/name.git``, ``https://host/ns/name.git`` and
    ``ssh://git@host/ns/name.git``. A local path ("../bare.git", "C:/x") has
    no host and no GitLab project path, and both come back as None -- a teacher
    testing against a local remote must not be warned about anything.

    Args:
        url: The remote URL, as `git remote get-url` prints it.

    Returns:
        (host, path). Either element is None when it cannot be read.
    """
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if not url:
        return None, None

    if "://" in url:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https", "ssh", "git"):
            host = (parsed.hostname or "").lower() or None
            path = parsed.path.strip("/") or None
            return host, path
        return None, None

    # scp-like: [user@]host:path. A Windows drive letter ("C:/Users/...") is
    # NOT this, which is what the second condition rules out.
    if ":" in url:
        left, right = url.split(":", 1)
        host = left.rsplit("@", 1)[-1].lower()
        if host and "/" not in host and len(host) > 1:
            return host or None, right.strip("/") or None
    return None, None


def _host_of(url: str) -> str | None:
    """The host part of a course URL, lowercased, or None."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() or None


# Filer som LIGGER I MALLEN och darfor inte ar studentarbete. En labbmapp som
# bara innehaller dessa ar tom i den mening den har kontrollen bryr sig om.
# `.gitkeep` finns just for att Git inte kan spara en tom katalog -- den ar
# mallens satt att skapa `lab-a3/`, och rakna den som innehall gor kontrollen
# blind for precis det fall den finns for.
PLACEHOLDER_FILENAMES = frozenset({".gitkeep", ".gitignore", ".python-version"})


def warn_if_required_paths_missing(
    repo_root: Path, config: HandinConfig, handin_type: HandinType, *, interactive: bool = True,
    worktree: Path | None = None,
) -> bool:
    """Warn when the folders this hand-in is about are missing or empty.

    A LOCAL check with no network in it, against a failure the student cannot
    see: `handin.py lab-a3` run in a repository where `lab-a3/` is absent or
    empty submits successfully and hands in nothing. Every other check in the
    chain is about identity and permissions, and all of them pass in that case.

    Silent when `[[handin]] required_paths` is empty (the default). A miss is a
    WARNING with a confirmation, never a refusal: see the field's comment in
    handin_config.py.

    Args:
        repo_root: Path to the tree that is judged -- the copy of the commit
            when called from `run_committed_checks`.
        config: Loaded configuration.
        handin_type: The selected hand-in type.
        worktree: The student's own folder, when `repo_root` is a copy of the
            commit. A path missing from the commit but present here is
            reported as "not committed", with git add as the fix (2.17.0);
            until then it said "hand in from the repository's root folder",
            which the student already was in.

    Returns:
        True when nothing was missing or the check is off, False when the
        student was warned and chose to continue.

    Raises:
        SystemExit: If the student answers no to the confirmation.
    """
    paths = handin_type.required_paths
    if not paths:
        return True

    problems: list[str] = []
    missing: list[str] = []
    reasons: set[str] = set()
    for rel in paths:
        target = repo_root / rel
        empty_dir = target.is_dir() and not any(
            x.is_file() and x.name not in PLACEHOLDER_FILENAMES for x in target.rglob("*")
        )
        if target.exists() and not empty_dir:
            continue
        why = worktree_state(worktree, rel)
        reasons.add(why)
        if why != "absent":
            problems.append(msg(config, f"paths_missing_{why}", path=rel))
        elif empty_dir:
            problems.append(msg(config, "paths_missing_empty", path=rel))
        else:
            problems.append(msg(config, "paths_missing_absent", path=rel))
        missing.append(rel)

    # For the issue body: what the TA should know was handed in without.
    SUBMISSION_PROGRESS["missing_paths"] = missing
    if not problems:
        return True

    print(
        msg(
            config,
            "paths_missing",
            handin_id=handin_type.id,
            what=" ".join(problems),
            advice=msg(
                config,
                "paths_missing_advice_ignored" if "ignored" in reasons
                else "paths_missing_advice_commit" if "uncommitted" in reasons
                else "paths_missing_advice_check",
            ),
        )
    )
    print()
    if not interactive:
        return False
    if not prompt_yes_no(msg(config, "origin_continue_anyway"), default_yes=False):
        print(msg(config, "origin_cancelled"))
        sys.exit(0)
    print()
    return False


def warn_if_not_forked_from_course(
    config: HandinConfig, project: dict[str, Any] | None, project_path: str
) -> bool:
    """Warn when this repository is not a fork of the course template.

    Reads `forked_from_project` off the project GitLab has ALREADY been asked
    about in link 3, so this costs nothing extra. It answers a question no name
    pattern can: a fork keeps the link when it is renamed, moved to another
    namespace, or transferred, and a repository that merely LOOKS like the
    course repository (a class-mate's clone pushed to a new project, a copy
    made by hand) does not have the link at all.

    Silent when `[course] upstream_project_id` is 0 (the default) and when the
    id matches. A mismatch, and an ANSWER WITHOUT THE FIELD, is a WARNING with a
    confirmation; see the field's own comment in handin_config.py for why never
    a refusal. GitLab OMITS the key for a repository that is not a fork, so
    "silent when absent" would be silence for exactly the case the check
    exists for. The measurement is in the comment on the first line of the
    body.

    Args:
        config: Loaded configuration.
        project: The project as GitLab returned it in link 3.
        project_path: `namespace/name`, for the message.

    Returns:
        True when nothing was wrong or the check is off, False when the student
        was warned and chose to continue.

    Raises:
        SystemExit: If the student answers no to the confirmation.
    """
    expected = config.course.upstream_project_id
    if not expected or not isinstance(project, dict):
        return True

    parent = project.get("forked_from_project")
    if isinstance(parent, dict) and parent.get("id") == expected:
        return True

    # The message names the COURSE, not the upstream path. A student who forked
    # a class-mate's fork would otherwise be shown the class-mate's repository,
    # which is neither useful to her nor the course's to disclose.
    print(
        msg(
            config,
            "fork_not_from_course",
            project_path=project_path,
            course=config.course.display_name,
        )
    )
    print()
    if not prompt_yes_no(msg(config, "origin_continue_anyway"), default_yes=False):
        print(msg(config, "origin_cancelled"))
        sys.exit(0)
    print()
    return False


def check_origin_matches_course(config: HandinConfig, username: str) -> bool:
    """Warn when `origin` does not look like the student's course repository.

    A WARNING WITH A CONFIRMATION, NEVER A REFUSAL, and that is a deliberate
    trade-off rather than caution for its own sake. The tool is used at a
    deadline; the cost of a wrong refusal (a student who cannot hand in at all,
    at 23.55, over a repository the course merely did not recognise) is far
    higher than the cost of a wrong acceptance (a hand-in that points at the
    wrong repository, which a TA sees immediately in the issue and
    which the student can redo -- the tool supersedes old issues). Every way
    the check can be wrong -- a renamed fork, a second remote layout, a course
    that fills the patterns in wrongly, a group repository in a partner's
    namespace -- would otherwise lock someone out. So it prints what it
    measured and lets the student answer.

    Two measurements, both silent when they cannot be made:

    1. The HOST. `origin` must be on the same server as `[course] gitlab_url`.
       Always checked, needs no configuration, and has no legitimate exception:
       the tool creates the issue on the course server, so a repository
       somewhere else is a different repository. A local path remote (a
       teacher's test setup) has no host and is not judged.
    2. The PROJECT PATH, against `[course] expected_origin_paths` -- glob
       patterns over `namespace/name`, with `{username}` standing for the
       logged-in GitLab user. EMPTY BY DEFAULT, and an empty list checks
       nothing: the patterns depend on how a course hands out repositories
       (per student, per pair, forked into personal namespaces), and a guessed
       pattern would warn every student on the course.

    Args:
        config: Loaded configuration.
        username: The logged-in GitLab username, for `{username}` patterns.

    Returns:
        True if `origin` is on the course server (so a project lookup there is
        meaningful), False if it is not or could not be read.

    Raises:
        SystemExit: If the student answers no to the confirmation.
    """
    result = run_git("remote", "get-url", "origin", check=False)
    if result.returncode != 0:
        return False
    origin_url = result.stdout.strip()
    origin_host, project_path = parse_origin_url(origin_url)
    course_host = _host_of(config.course.gitlab_url)

    if origin_host and course_host and origin_host != course_host:
        print()
        print(
            msg(
                config,
                "origin_wrong_host",
                origin_host=origin_host,
                course_host=course_host,
                # The address can carry a user name and a token, and the
                # student shows this screen to a TA.
                origin_url=RUN_LOG.mask(origin_url),
            )
        )
        print()
        if not prompt_yes_no(msg(config, "origin_continue_anyway"), default_yes=False):
            print(msg(config, "origin_cancelled"))
            sys.exit(0)
        print()
        return False

    patterns = config.course.expected_origin_paths
    if patterns and project_path:
        expanded = [pattern.replace("{username}", username or "*") for pattern in patterns]
        if not any(fnmatch.fnmatch(project_path, pattern) for pattern in expanded):
            print()
            print(
                msg(
                    config,
                    "origin_unexpected_path",
                    project_path=project_path,
                    expected=", ".join(expanded),
                )
            )
            print()
            if not prompt_yes_no(msg(config, "origin_continue_anyway"), default_yes=False):
                print(msg(config, "origin_cancelled"))
                sys.exit(0)
            print()

    return bool(origin_host and course_host and origin_host == course_host)


def repo_has_commits() -> bool:
    """True when HEAD resolves to a commit.

    False in a repository that has been ``git init``-ed and never committed to
    -- the state every student passes through -- and in a branch created with
    ``git switch --orphan``.

    ``--verify --quiet`` rather than a bare ``rev-parse HEAD``: the bare form
    writes "fatal: ambiguous argument 'HEAD'" to stderr, which is captured
    here, but the pair makes the intent readable at the call site.
    """
    result = run_git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    return result.returncode == 0


def require_commits(config: HandinConfig) -> None:
    """End the run with an explanation when the repository has no commits.

    Called BEFORE step 1, deliberately: without commits, step 1 lists every
    file in the project as untracked and asks whether to hand in "the latest
    commit" without them, a question that has no true answer here.

    Args:
        config: Loaded configuration (supplies the message language).

    Raises:
        SystemExit: If there are no commits.
    """
    if repo_has_commits():
        return
    print(msg(config, "no_commits_yet"))
    sys.exit(1)


def get_current_commit() -> str:
    """Get the full SHA of the current HEAD commit.

    Returns:
        The 40-character commit hash.

    Raises:
        SystemExit: If the repository has no commits.

    The backstop for `require_commits`: the gate in `main` covers the path a
    student walks, and this covers every other caller, now and later. The
    signature is deliberately unchanged -- the message is looked up through
    `msg(None, ...)`, which reads MESSAGE_LANGUAGE, exactly as the handlers in `__main__`
    do, so no caller and no test has to learn a new argument to be safe.
    """
    result = run_git("rev-parse", "HEAD", check=False)
    if result.returncode != 0:
        print(msg(None, "no_commits_yet"))
        sys.exit(1)
    return result.stdout.strip()


def get_existing_tags() -> list[str]:
    """Get all existing tags in the repository.

    Returns:
        List of tag names.
    """
    result = run_git("tag", "-l")
    return [t for t in result.stdout.strip().split("\n") if t]


# How long the tag fetch may take before the hand-in gives up on it, in
# seconds.
#
# The fetch is ONLY an information lookup: it makes the tag number right on the
# first attempt. Everything still works without it -- the numbering falls back
# to the tags this clone knows, the push goes ahead, and a collision produces
# the same message it produced before the fetch existed. The fetch must
# therefore never be able to cost more than it can give, and the way it could
# was by not returning at all.
#
# Why 20 and not 5, and not 120: a ``--tags`` fetch of a lab repository is a ref
# advertisement plus at most a few small objects. On a working link it finishes
# in well under a second; on a loaded lab-session wifi, in a few. Twenty seconds
# is far above anything a fetch that is going to succeed needs, and below the
# point where a person decides that a silent program has died -- which is the
# failure this bound exists to prevent. Too short would start abandoning
# fetches that were about to work: harmless, but it wastes the fix. Too long
# would hand the student back the hang. The student is told before the wait
# starts (``tag_fetch_checking``) and told again if it is abandoned, so 20
# silent seconds are never 20 unexplained ones.
TAG_FETCH_TIMEOUT_SECONDS = 20.0


def fetch_tags_quietly(config: HandinConfig) -> bool:
    """Fetch the remote's tags, without ever letting that stop the hand-in.

    Args:
        config: Loaded configuration (supplies the message language).

    Returns:
        True if the remote's tags are now known here, False if the hand-in is
        continuing on local knowledge alone.
    """
    print(msg(config, "tag_fetch_checking"))
    try:
        result = run_git(
            "fetch",
            "--tags",
            "origin",
            check=False,
            timeout=TAG_FETCH_TIMEOUT_SECONDS,
            discard_output=True,
        )
    except subprocess.TimeoutExpired:
        print(msg(config, "tag_fetch_timeout", seconds=int(TAG_FETCH_TIMEOUT_SECONDS)))
        return False
    except OSError:
        # No git on PATH, or no process to be had. Same reading as a failed
        # fetch: we simply do not know what is at GitLab.
        print(msg(config, "tag_fetch_failed"))
        return False

    if result.returncode != 0:
        print(msg(config, "tag_fetch_failed"))
        return False
    return True


def next_free_tag_name() -> str:
    """Return the lowest unused t<N> tag name, counted over ALL known tags.

    Call ``git fetch --tags`` before this if the remote's tags matter -- this
    function reads only what the clone knows.

    Returns:
        A tag name of the form "t<N>".
    """
    tag_pattern = re.compile(r"^t(\d+)$")
    max_num = 0
    for tag in get_existing_tags():
        match = tag_pattern.match(tag)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num
    return f"t{max_num + 1}"


# ─── Helper Functions: Prompting ────────────────────────────────────────────


def prompt_yes_no(question: str, default_yes: bool = False) -> bool:
    """Ask a yes/no question and return the answer.

    Unrecognised input is therefore RE-ASKED rather than read as "no". Enter on
    an empty line still means the default: the student saw the question and
    accepted what it offered.

    NEITHER Ctrl+C NOR EOF is an answer. Both propagate and end the run from
    `__main__`. Returning False for them was "abort" at the six
    `default_yes=False` sites, but at `step6_checklist` it wrote "Nej" on a
    question the TA reads -- "Har du fått din redovisning godkänd?"
    answered no, by a student who never answered at all.

    Both languages are accepted in both directions on purpose: a Swedish
    student running an English-configured copy should not be trapped either.

    Args:
        question: The question text (without the [y/N] suffix).
        default_yes: If True, default is Yes [Y/n]; otherwise No [y/N].

    Returns:
        True if the user answered yes, False otherwise.
    """
    suffix = msg(None, "yesno_yes_default" if default_yes else "yesno_no_default")
    while True:
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Neither is an answer; see the docstring. Both reach __main__,
            # which ends the run without inventing a yes or a no.
            print()
            raise

        if not answer:
            return default_yes
        if answer in YES_ANSWERS:
            return True
        if answer in NO_ANSWERS:
            return False
        print(msg(None, "yesno_unrecognised"))


def prompt_choice(
    prompt_text: str, options: list[str], config: HandinConfig | None = None
) -> int:
    """Display a numbered menu and prompt for a choice.

    `config` is the LAST parameter and defaults to None on purpose. Two tests
    monkeypatch this function with a two-argument callable, and the menu's own
    five strings were the last hardcoded English in the student's normal path:
    a student who runs `handin.py` without a lab id got a Swedish heading over
    an English `Enter your choice:`. A defaulted third parameter translates the
    menu without touching either call signature.

    Args:
        prompt_text: Text to display before the menu.
        options: List of option display strings.
        config: Loaded configuration, or None for English.

    Returns:
        Zero-based index of the selected option.
    """
    print(prompt_text)
    print()
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    print()

    while True:
        try:
            raw = input(msg(config, "menu_enter_choice")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(msg(config, "menu_cancelled"))
            sys.exit(0)

        if not raw:
            print(msg(config, "menu_need_number"))
            continue

        try:
            choice = int(raw)
        except ValueError:
            print(msg(config, "menu_not_a_number", raw=raw))
            continue

        if 1 <= choice <= len(options):
            return choice - 1

        print(msg(config, "menu_out_of_range", count=len(options)))


# ─── Helper Functions: Authentication ───────────────────────────────────────


# The origins whose file this tool WROTE ITSELF, and is therefore allowed to
# delete again when GitLab rejects what is in it. Everything not listed here is
# the student's own: GITLAB_TOKEN in the environment, and `.handin-token` in
# the repository.
TOOL_OWNED_ORIGINS = frozenset({"cache", "prompt", "oauth-cache", "oauth-flow"})


@dataclass(frozen=True)
class Credential:
    """A token together with WHERE it was read from.

    The origin is not bookkeeping. It decides two things that a bare token
    string cannot:

      * WHICH HTTP HEADER carries it -- ``PRIVATE-TOKEN`` for a personal access
        token, ``Authorization: Bearer`` for one out of the device flow.
      * WHICH FILE, if any, may be deleted when GitLab answers 401.
    """

    token: str
    origin: str
    path: Path | None = None

    @property
    def kind(self) -> str:
        """"oauth" or "private": which header this token belongs in."""
        return "oauth" if self.origin.startswith("oauth") else "private"

    @property
    def deletable_path(self) -> Path | None:
        """The file that may be removed if GitLab rejects this token."""
        if self.origin in TOOL_OWNED_ORIGINS:
            return self.path
        return None


def find_token(repo_root: Path, config: HandinConfig) -> Credential | None:
    """Find a GitLab personal access token, and remember where it came from.

    Searches in order:
    1. GITLAB_TOKEN environment variable
    2. Token cache file (from config)
    3. .handin-token file in the repository root

    Args:
        repo_root: Path to the git repository root.
        config: Loaded configuration.

    Returns:
        The credential, or None if no token was found.
    """
    # 1. Environment variable
    token = os.environ.get("GITLAB_TOKEN")
    if token and token.strip():
        return Credential(token=token.strip(), origin="env")

    config_file = config.auth.token_cache_path / "token"
    if config_file.is_file():
        try:
            content = config_file.read_text().strip()
        except OSError as exc:
            print(msg(config, "token_file_unreadable", path=config_file, reason=exc.strerror or exc))
            content = ""
        if content:
            return Credential(token=content, origin="cache", path=config_file)

    # 3. Repo-level token file
    repo_token = repo_root / ".handin-token"
    if repo_token.is_file():
        try:
            content = repo_token.read_text().strip()
        except OSError as exc:
            print(msg(config, "token_file_unreadable", path=repo_token, reason=exc.strerror or exc))
            content = ""
        if content:
            return Credential(token=content, origin="repo", path=repo_token)

    return None


# ─── OAuth Device Flow ──────────────────────────────────────────────────────


def _load_cached_oauth_token(config: HandinConfig) -> dict[str, Any] | None:
    """Load a cached OAuth token if available and not expired.

    Args:
        config: Loaded configuration.

    Returns:
        Token dict with 'access_token' and optionally 'refresh_token',
        or None if no valid cache exists.
    """
    token_file = config.auth.oauth_token_file
    if not token_file.is_file():
        return None
    try:
        data = json.loads(token_file.read_text())
        # Check expiry (with 60-second buffer)
        if "expires_at" in data and data["expires_at"] < time.time() + 60:
            # Try refresh
            if "refresh_token" in data:
                refreshed = _refresh_oauth_token(data["refresh_token"], config)
                if refreshed:
                    return refreshed
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_oauth_token(token_data: dict[str, Any], config: HandinConfig) -> Path | None:
    """Save an OAuth token to the cache file.

    Args:
        token_data: Token data to save.
        config: Loaded configuration.

    Returns:
        The file that was written, or None if it could not be written. The
        caller carries that path in the Credential: a file may be deleted on a
        401 only if this tool is the one that wrote it, and a save that failed
        wrote nothing.
    """
    try:
        token_file = config.auth.oauth_token_file
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(token_data, indent=2))
        token_file.chmod(0o600)
        return token_file
    except OSError:
        return None  # Non-critical


# Why the last _oauth_post returned None or an error, for the student's message.
OAUTH_LAST_ERROR = ""


def _oauth_post(endpoint: str, data: dict[str, str], config: HandinConfig) -> dict[str, Any] | None:
    """Make a POST request to a GitLab OAuth endpoint.

    Args:
        endpoint: API path (e.g., '/oauth/token').
        data: Form data to POST.

    Returns:
        Parsed JSON response, or None if the server could not be reached or
        answered with something that is not JSON.

    A FAILED OAuth exchange is a normal, parseable answer -- not "no answer".
    RFC 6749 section 5.2 and RFC 8628 section 3.5 both put the error code in a
    JSON body behind HTTP 400, so ``authorization_pending``, ``slow_down``,
    ``expired_token`` and ``access_denied`` ALL arrive as ``HTTPError``.
    ``urlopen`` raises on 4xx, so a bare ``except Exception`` threw every one of
    them away and the caller saw ``None`` -- indistinguishable from a dead
    network. The whole error ladder in ``oauth_device_flow`` was therefore dead
    code: a student whose login was DENIED sat through the full five-minute
    poll and was then told the login had timed out.
    """
    url = f"{config.course.gitlab_url}{endpoint}"
    encoded = urlencode(data).encode("utf-8")
    req = Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    # Why a None came back is kept in OAUTH_LAST_ERROR (2.11.0): until then
    # every failure here was silent, and a student whose browser login could
    # not even start was simply dropped into the token instructions without a
    # word about why. Bodies are never logged -- they carry the device code and
    # the tokens.
    global OAUTH_LAST_ERROR
    try:
        with urlopen(req, timeout=15, context=tls_context()) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
            if isinstance(answer, dict):
                for key in ("access_token", "refresh_token", "device_code", "id_token"):
                    RUN_LOG.add_secret(str(answer.get(key) or ""))
            log_event("oauth", f"POST {endpoint} -> {getattr(resp, 'status', '?')}")
            return answer
    except HTTPError as exc:
        # The error body is the payload we came for. If it is not JSON (a proxy
        # page, an HTML error), fall back to "could not ask".
        try:
            answer = json.loads(exc.read().decode("utf-8"))
        except Exception:
            OAUTH_LAST_ERROR = f"HTTP {exc.code}"
            log_event("oauth", f"POST {endpoint} -> HTTP {exc.code}, body not JSON")
            return None
        if isinstance(answer, dict):
            log_event("oauth", f"POST {endpoint} -> HTTP {exc.code}, error={answer.get('error')!r}")
            OAUTH_LAST_ERROR = f"HTTP {exc.code}: {answer.get('error') or '?'}"
        return answer
    except Exception as exc:
        reason = getattr(exc, "reason", None) or exc
        OAUTH_LAST_ERROR = f"{type(exc).__name__}: {reason}"
        log_event("oauth", f"POST {endpoint} -> {OAUTH_LAST_ERROR}")
        return None


def _refresh_oauth_token(refresh_token: str, config: HandinConfig) -> dict[str, Any] | None:
    """Refresh an expired OAuth token.

    Args:
        refresh_token: The refresh token from a previous authentication.
        config: Loaded configuration.

    Returns:
        New token data, or None if refresh failed.
    """
    if not config.auth.oauth_client_id:
        return None
    result = _oauth_post(
        "/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.auth.oauth_client_id,
        },
        config,
    )
    if result and "access_token" in result:
        if "expires_in" in result:
            result["expires_at"] = time.time() + result["expires_in"]
        _save_oauth_token(result, config)
        return result
    return None


def oauth_device_flow(config: HandinConfig) -> Credential | None:
    """Authenticate using the OAuth2 Device Authorization Grant.

    This flow lets the student log in via their browser without
    needing to create a personal access token.

    Args:
        config: Loaded configuration.

    Returns:
        A credential carrying the access token AND the cache file it came from
        (or was just written to), or None if the flow failed or was cancelled.
    """
    if not config.auth.oauth_client_id:
        return None

    # Check for cached token first. The path is carried along from the read:
    # it is the only file a 401 may later remove. See Credential.
    cached = _load_cached_oauth_token(config)
    if cached and "access_token" in cached:
        return Credential(
            token=cached["access_token"],
            origin="oauth-cache",
            path=config.auth.oauth_token_file,
        )

    # Step 1: Request device code
    result = _oauth_post(
        "/oauth/authorize_device",
        {
            "client_id": config.auth.oauth_client_id,
            "scope": "api",
        },
        config,
    )

    if not result or "device_code" not in result:
        # Device flow not supported or failed. Say WHY before the caller falls
        # back to a personal access token -- silently switching methods left
        # the student with instructions and no idea what had gone wrong.
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("error_description") or result.get("error") or "")
        print(msg(config, "oauth_unavailable", reason=reason or OAUTH_LAST_ERROR or "?"))
        print()
        return None

    device_code = result["device_code"]
    user_code = result["user_code"]
    verification_uri = result.get("verification_uri", f"{config.course.gitlab_url}/oauth/device")
    interval = result.get("interval", 5)
    expires_in = result.get("expires_in", 300)

    # Step 2: Show instructions to student
    print()
    print("  +---------------------------------------------+")
    print(f"  |{msg(config, 'oauth_box_title'):<45s}|")
    print("  |                                             |")
    print(f"  |{msg(config, 'oauth_box_code', code=user_code):<45s}|")
    print("  |                                             |")
    print(f"  |{msg(config, 'oauth_box_auto'):<45s}|")
    print(f"  |{msg(config, 'oauth_box_manual'):<45s}|")
    print(f"  |{'  ' + verification_uri:<45s}|")
    print("  +---------------------------------------------+")
    print()

    # Try to open browser automatically
    try:
        webbrowser.open(verification_uri)
    except Exception:
        pass  # Student can open URL manually

    print(msg(config, "oauth_waiting"))

    # Step 3: Poll for token
    deadline = time.time() + expires_in
    poll_network_said = False
    while time.time() < deadline:
        time.sleep(interval)

        token_result = _oauth_post(
            "/oauth/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": config.auth.oauth_client_id,
            },
            config,
        )

        if not token_result:
            # Could not ask at all (network). Keep polling -- it may come back --
            # but say so once, instead of "waiting for you" for five minutes.
            if not poll_network_said:
                print(msg(config, "oauth_poll_network", reason=OAUTH_LAST_ERROR or "?"))
                poll_network_said = True
            continue

        if "access_token" in token_result:
            # Success!
            if "expires_in" in token_result:
                token_result["expires_at"] = time.time() + token_result["expires_in"]
            saved_path = _save_oauth_token(token_result, config)
            print(msg(config, "oauth_login_ok"))
            print()
            return Credential(
                token=token_result["access_token"],
                origin="oauth-flow",
                path=saved_path,
            )

        error = token_result.get("error")
        if error == "authorization_pending":
            continue  # Student hasn't approved yet
        elif error == "slow_down":
            interval = min(interval + 2, 30)
            continue
        elif error == "expired_token":
            print(msg(config, "oauth_timed_out"))
            return None
        elif error == "access_denied":
            print(msg(config, "oauth_denied"))
            return None
        else:
            # An error we do not have a specific text for. Say so and let the
            # caller fall back to a personal access token; silence here left
            # the student staring at "waiting for you to log in".
            print(msg(config, "oauth_failed", error=error or "okant fel"))
            return None

    print(msg(config, "oauth_timed_out"))
    return None


def forget_rejected_token(credential: Credential) -> Path | None:
    """Delete the CACHED credential that GitLab has just rejected.

    Why this has to happen at the moment of the 401, and not later: both caches
    are read before anything else on every run.  ``oauth_device_flow`` returns a
    cached OAuth token before it ever offers the device flow, and ``find_token``
    reads ``~/.config/handin/token`` before it asks the student for a new one.
    So a credential that has been revoked, expired at the server, or had its
    authorisation withdrawn is re-chosen on EVERY subsequent run -- and the new
    token the student then creates is never even looked at.  The failure looks
    to the student like "the tool is broken": the same error, every time, no
    matter what they do at GitLab.

    Deleting is the only thing this tool does to a file it did not just write,
    so it is fenced TWICE, and the two fences answer different questions:

    Never touched, because their origins are not in ``TOOL_OWNED_ORIGINS``:
    ``GITLAB_TOKEN`` in the environment and ``.handin-token`` in the
    repository.  Those are the student's own, put there deliberately, and
    deleting a file inside their repository would be a surprise.  They are
    reported by the caller instead.

    Args:
        credential: The credential GitLab rejected, with the origin and path it
            was read from.

    Returns:
        The path that was removed, or None if nothing was.
    """
    path = credential.deletable_path
    if path is None:
        return None

    try:
        if not path.is_file():
            return None
        raw = path.read_text()
    except OSError:
        return None

    if credential.kind == "oauth":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("access_token") != credential.token:
            return None
    elif raw.strip() != credential.token:
        return None

    try:
        path.unlink()
    except OSError:
        return None
    return path


def get_gitlab_client(credential: Credential, config: HandinConfig) -> ApiClient:
    """Create and return an authenticated GitLab client.

    Uses ``handin_api.ApiClient``, which is standard library only. That is what
    makes ``python3 handin.py`` work: the tool has no dependency to resolve, so
    it does not need uv — and the first hand-in in TDDE82 happens a week before
    uv is taught.

    Args:
        credential: The token AND where it was read from. The origin decides
            both which header carries it and which file, if any, may be removed
            when GitLab rejects it -- see Credential.
        config: Loaded configuration.

    Returns:
        An authenticated ApiClient.

    Raises:
        SystemExit: If authentication fails.
    """
    RUN_LOG.add_secret(credential.token)
    gl = client_from_env(
        config.course.gitlab_url, credential.token, token_kind=credential.kind
    )
    # --debug turns on the client's own request trace. Passed explicitly:
    # client_from_env reads only HANDIN_API_TRACE from the environment.
    if DEBUG:
        gl.trace = True
    gl.event_hook = lambda method, url, status: log_event("api", f"{method} {url} -> {status}")
    try:
        gl.authenticate()
    except ApiAuth as exc:
        # 401 only. A 403 on GET /user means the token is real but lacks the
        # scope, and the old client let that fall through to the "could not
        # connect" branch. Telling a student with a read-only token that they
        # may not have a GitLab account sends them to create an account they
        # already have.
        if exc.status != 401:
            print(
                msg(
                    config,
                    "gitlab_connect_failed",
                    gitlab_url=config.course.gitlab_url,
                    details=exc,
                )
            )
            sys.exit(1)
        print(msg(config, "gitlab_auth_failed"))
        print()
        # A rejected credential must not be re-chosen on the next run; see
        # forget_rejected_token.
        removed = forget_rejected_token(credential)
        if removed:
            print(msg(config, "auth_cache_cleared", path=removed))
            print()
        # Link 1 of the hand-in chain: the account itself. A student has NO
        # GitLab account until they have logged in manually once — the login is
        # what creates it. Without this paragraph the student sees only "token
        # invalid" and starts hunting for a token they cannot create yet.
        if credential.kind == "oauth" and not removed:
            # The cache could not be removed, so a rerun would pick the same
            # dead login again. Name the file instead of promising a fresh login.
            print(msg(config, "oauth_rejected_manual", path=credential.path or config.auth.oauth_token_file))
            sys.exit(1)
        if credential.kind == "oauth":
            # A browser login that GitLab no longer accepts (revoked, or the
            # application re-registered). The account plainly exists -- the
            # student logged in with it -- and the cure is to run again, which
            # starts a fresh browser login now that the cache is gone. The
            # token instructions below sent them to create a token instead.
            print(msg(config, "oauth_rejected_rerun"))
            sys.exit(1)
        print(msg(config, "account_missing", gitlab_url=config.course.gitlab_url))
        print(msg(config, "token_instructions", gitlab_url=config.course.gitlab_url))
        print(msg(config, "token_instructions_powershell"))
        sys.exit(1)
    except Exception as exc:
        print(
            msg(
                config,
                "gitlab_connect_failed",
                gitlab_url=config.course.gitlab_url,
                details=exc,
            )
        )
        sys.exit(1)

    return gl


# ─── Helper Functions: Deadlines ────────────────────────────────────────────


def load_deadlines(config: HandinConfig) -> list[Deadline]:
    """Load deadline information from config (and optionally from URL).

    Returns deadlines from the config. If a deadlines_url is configured,
    fetches and merges remote deadlines as well.

    Args:
        config: Loaded configuration.

    Returns:
        List of Deadline objects.
    """
    deadlines = list(config.deadlines)

    # Optionally fetch additional deadlines from URL.
    #
    # Uses the same hardened fetch as the central configuration: https only
    # (loopback excepted), redirects refused, a total time budget rather than a
    # per-socket timeout, a size cap that rejects instead of truncating, and
    # utf-8-sig. This used to be a second bare urlopen with none of that, which
    # is the hole a scheme rule on config_update_url alone would have left open.
    url = config.defaults.deadlines_url
    if url:
        text, _reason, _detail = fetch_remote_config_text(url)
        if text is not None:
            try:
                import tomllib

                remote_data = tomllib.loads(text)
                for dl_raw in remote_data.get("deadline", []):
                    if not isinstance(dl_raw, dict):
                        continue
                    date = dl_raw.get("date", "")
                    handins = dl_raw.get("handins", [])
                    message = dl_raw.get("message", "")
                    if not isinstance(date, str) or not isinstance(message, str):
                        continue
                    if not isinstance(handins, list):
                        continue
                    deadlines.append(Deadline(date=date, handins=handins, message=message))
            except Exception:
                pass  # Use local deadlines only

    return deadlines


def display_deadlines(
    config: HandinConfig, deadlines: list[Deadline], handin_type: HandinType
) -> None:
    """Display relevant deadline information for a hand-in type.

    Shows the latest past deadline and the next upcoming deadline that
    includes the given hand-in type.

    Args:
        config: Loaded configuration (supplies the message language).
        deadlines: List of Deadline objects.
        handin_type: The selected hand-in type -- its `id` filters, its `name`
            is printed.
    """
    if not deadlines:
        return

    handin_id = handin_type.id
    # A hand-in without a display name is not a configuration this tool
    # accepts, but the id is a truthful fallback and a KeyError here would
    # abort a hand-in over a label.
    handin_name = handin_type.name or handin_type.id

    now = datetime.now(timezone.utc)
    relevant: list[tuple[datetime, str]] = []

    for d in deadlines:
        if handin_id not in d.handins:
            continue
        dt = parse_deadline_date(d.date)
        if dt is None:
            continue
        relevant.append((dt, d.message))

    if not relevant:
        return

    relevant.sort(key=itemgetter(0))

    past = [(dt, msg) for dt, msg in relevant if dt < now]
    future = [(dt, msg) for dt, msg in relevant if dt >= now]

    print()
    print(msg(config, "deadline_header"))
    if past:
        last_dt, last_msg = past[-1]
        days_ago = (now - last_dt).days
        msg_part = msg(config, "deadline_extra", text=last_msg) if last_msg else ""
        print(
            msg(
                config,
                "deadline_past",
                handin=handin_name,
                when=last_dt.strftime("%Y-%m-%d %H:%M"),
                days=days_ago,
                extra=msg_part,
            )
        )
    if future:
        next_dt, next_msg = future[0]
        days_until = (next_dt - now).days
        msg_part = msg(config, "deadline_extra", text=next_msg) if next_msg else ""
        print(
            msg(
                config,
                "deadline_next",
                handin=handin_name,
                when=next_dt.strftime("%Y-%m-%d %H:%M"),
                days=days_until,
                extra=msg_part,
            )
        )
    if not past and not future:
        print(msg(config, "deadline_none"))
    print()


# ─── Helper Functions: Draft Recovery ───────────────────────────────────────


def get_draft_path(repo_name: str) -> Path:
    """Get the path to the draft file for a repository.

    Args:
        repo_name: Name of the repository.

    Returns:
        Path to the draft JSON file.
    """
    return DRAFT_CACHE_DIR / f"draft-{repo_name}.json"


def save_draft(repo_name: str, data: dict[str, Any]) -> None:
    """Save in-progress submission state to a draft file.

    Args:
        repo_name: Name of the repository.
        data: Draft data to save.
    """
    try:
        DRAFT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        draft_path = get_draft_path(repo_name)
        draft_path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # Non-critical, don't fail the submission


def delete_draft(repo_name: str) -> None:
    """Delete the draft file for a repository.

    Args:
        repo_name: Name of the repository.
    """
    try:
        draft_path = get_draft_path(repo_name)
        if draft_path.exists():
            draft_path.unlink()
    except OSError:
        pass


# ─── Step Functions ─────────────────────────────────────────────────────────


def step0_startup_checks(config: HandinConfig) -> tuple[Path, str, Any]:
    """Step 0: Verify environment and authenticate with GitLab.

    Tries OAuth Device Flow first (if configured), then falls back to
    Personal Access Token.  If no PAT is found, guides the student
    through creating one with a browser helper.

    Args:
        config: Loaded configuration.

    Returns:
        Tuple of (repo_root, repo_name, gitlab_client).
    """
    print(msg(config, "env_header"))

    repo_root = get_repo_root(config)
    repo_name = get_repo_name(config)

    # WHERE the token came from, not just what it is. A PAT goes in the
    # PRIVATE-TOKEN header and an OAuth token in Authorization: Bearer, and the
    # old code sent PRIVATE-TOKEN for both — including for tokens out of the
    # device flow. Losing this on the way to get_gitlab_client is exactly how
    # that bug survived: the knowledge existed here and was thrown away one
    # call later. It now also decides which file a 401 may delete, which is why
    # the origin travels in the Credential instead of in a second variable.
    credential: Credential | None = None

    # 1. Try OAuth Device Flow (if configured)
    if config.auth.oauth_client_id:
        credential = oauth_device_flow(config)

    # 2. Fall back to Personal Access Token
    if credential is None:
        credential = find_token(repo_root, config)

    # 3. If no token found, guide the student
    if credential is None:
        pat_url = f"{config.course.gitlab_url}/-/user_settings/personal_access_tokens"
        print(msg(config, "env_no_auth"))
        print()
        print(msg(config, "env_pat_opening", url=pat_url))
        try:
            webbrowser.open(pat_url)
        except Exception:
            pass  # Student can open URL manually
        print(msg(config, "env_pat_steps"))
        print()

        try:
            # getpass, not input(): what is pasted is not echoed, so the token
            # never lands in the terminal or its scrollback.
            print(msg(config, "env_pat_hidden"))
            entered = getpass.getpass(msg(config, "env_pat_prompt")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if not entered:
            print(msg(config, "env_pat_none_entered"))
            sys.exit(1)

        token_dir = config.auth.token_cache_path
        token_file = token_dir / "token"
        saved_path: Path | None = None
        try:
            token_dir.mkdir(parents=True, exist_ok=True)
            token_file.write_text(entered)
            token_file.chmod(0o600)
            saved_path = token_file
        except OSError as exc:
            print(msg(config, "env_pat_not_saved", path=token_file, reason=exc.strerror or exc))
        else:
            print(msg(config, "env_pat_saved", path=token_file))
        print()
        # `path` is the file a later 401 may remove, and a save that failed
        # wrote no file -- so it stays None, exactly as in `_save_oauth_token`.
        credential = Credential(token=entered, origin="prompt", path=saved_path)

    gl = get_gitlab_client(credential, config)

    username = gl.user["username"]
    print(msg(config, "env_repository", repo_name=repo_name))
    print(msg(config, "env_authenticated", username=username))
    print()

    return repo_root, repo_name, gl


def _is_not_found(exc: BaseException) -> bool:
    """Decide whether an exception from the GitLab client means "404, absent".

    Used to keep "this does not exist" apart from "I could not ask". A timeout or
    a 500 must never be reported to the student as a missing account, missing
    repository or missing membership — those have completely different remedies.

    The decision is made on the EXCEPTION TYPE, and on nothing else.
    ``handin_api`` raises ``ApiNotFound`` for 404 and ``ApiTransport`` for
    everything that means "I could not ask" — DNS, TLS, timeout, redirect, 5xx,
    a body that is not JSON.

    The old string fallback (``"404" in text or "not found" in text``) is gone,
    and must not come back. It was tolerable under ``requests``; under
    ``urllib`` it is actively dangerous. A ``URLError`` around a DNS miss reads
    *"Name or service not known"*, and a proxy error, an SSL failure or GitLab's
    own error body can easily contain the words *"not found"* without anything
    being a 404. Reporting that to a student as a missing repository sends them
    to fix something that is not broken.

    ``response_code`` is still honoured so the fakes in ``tests/`` keep working;
    it is never reached for a real client error.

    Args:
        exc: The exception raised by the GitLab client.

    Returns:
        True if this is a 404, False otherwise.
    """
    if isinstance(exc, ApiNotFound):
        return True
    if isinstance(exc, ApiError):
        return False
    code = getattr(exc, "response_code", None)
    if code is not None:
        return int(code) == 404
    return False


def step0b_check_handin_chain(gl: Any, config: HandinConfig, handin_type: HandinType) -> str:
    """Step 0b: Verify the hand-in chain and say WHERE it is incomplete.

    The chain is: GitLab account -> Guest membership in the hand-in project ->
    personal repository (fork) -> hand-in. Each link has a different cause and a
    different remedy, so a single "something went wrong" is useless. The student
    must be told which link is missing and what to do about it.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        handin_type: The selected hand-in type (decides the target project).

    Returns:
        The membership state: "member", "not-member" or "unknown".

    Raises:
        SystemExit: If the student's own repository is not reachable on GitLab.
    """
    gitlab_url = config.course.gitlab_url

    print(msg(config, "chain_header"))
    print()

    # ── Link 1: account ─────────────────────────────────────────────────────
    # Already proven: gl.auth() succeeded in step 0, which is impossible without
    # an account. Say so explicitly, so the student can see the chain progress.
    try:
        username = gl.user["username"]
        user_id = gl.user["id"]
    except Exception:
        username = "?"
        user_id = None
    print(msg(config, "chain_account_ok", username=username))

    # ── Link 2.5: is `origin` the course repository at all? ─────────────────
    # Before the lookup below, because that lookup ASSUMES the path belongs to
    # this GitLab: with an origin on another server it would ask the course
    # server about a path that means nothing there, and a 404 would tell the
    # student their course repository is missing -- the wrong diagnosis for
    # standing in the wrong directory. When the host does not match we
    # therefore skip link 3 rather than report it wrongly.
    on_course_host = check_origin_matches_course(config, username)

    # ── Link 3: the student's own repository (fork) ──────────────────────────
    # Checked before membership because this one CAN block the hand-in.
    project_path = get_project_path() if on_course_host else None
    if project_path:
        try:
            project = gl.get_project(project_path)
            print(msg(config, "chain_fork_ok", project_path=project_path))
            warn_if_not_forked_from_course(config, project, project_path)
        except Exception as exc:
            if _is_not_found(exc):
                print()
                print(
                    msg(
                        config,
                        "chain_fork_missing",
                        project_path=project_path,
                        gitlab_url=gitlab_url,
                    )
                )
                sys.exit(1)
            # A timeout, a 500 or a proxy error says nothing about whether the
            # repository exists. Telling the student "your repo is missing"
            # would send them chasing the wrong problem, so this only warns.
            print(
                msg(
                    config,
                    "chain_fork_unknown",
                    project_path=project_path,
                    details=str(exc)[:120],
                )
            )

    # ── Link 2: Guest membership in the hand-in project ─────────────────────
    membership = "unknown"
    target_project_id = config.resolve_target_project(handin_type)
    if user_id is not None and target_project_id:
        try:
            gl.get(
                f"projects/{encode_path(target_project_id)}"
                f"/members/all/{encode_path(user_id)}"
            )
            membership = "member"
        except Exception as exc:
            # 404 from this endpoint means "not a member". Anything else (403,
            # network, an unexpected shape) is genuinely unknown, and unknown
            # must not be reported to the student as "not a member".
            membership = "not-member" if _is_not_found(exc) else "unknown"

    if membership == "member":
        print(msg(config, "chain_member_ok"))
    elif membership == "not-member" and not handin_type.group_submission:
        print(msg(config, "chain_member_not_needed"))
    elif membership == "not-member":
        print()
        print(msg(config, "chain_member_pending"))
    else:
        print(msg(config, "chain_member_unknown"))

    print()
    return membership


def step1_check_uncommitted(config: HandinConfig) -> None:
    """Step 1: Warn about uncommitted/untracked changes and ask to proceed.

    Categorizes changes into:
    - Modified files not staged for commit
    - Files staged but not committed
    - Untracked files (not added to git)

    Args:
        config: Loaded configuration (supplies the message language).
    """
    result = run_git("status", "--porcelain")
    if not result.stdout.strip():
        return

    # splitlines() UTAN strip(): `git status --porcelain` bär betydelsen i de
    # två första kolumnerna, och en ledande blanksteg är en av dem. `.strip()`
    # åt bort FÖRSTA radens indexkolumn, så ` M fil.py` (ändrad, inte tillagd)
    # lästes som `M fil.py` — rapporterad som "tillagd med git add" och med
    # filnamnet kapat till `il.py` av `line[3:]`. Bara första raden drabbades,
    # vilket gjorde felet svårt att se i en lista med flera filer.
    lines = result.stdout.splitlines()
    staged = []  # Index has changes (first char is not ' ' or '?')
    unstaged = []  # Worktree has changes (second char is 'M', 'D', etc.)
    untracked = []  # Not tracked by git

    for line in lines:
        if len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        filepath = line[3:]

        if index_status == "?":
            untracked.append(filepath)
        else:
            if index_status not in (" ", "?"):
                staged.append(filepath)
            if worktree_status not in (" ", "?"):
                unstaged.append(filepath)

    print(msg(config, "uncommitted_header"))
    print()

    if unstaged:
        print(msg(config, "uncommitted_unstaged", count=len(unstaged)))
        for f in unstaged[:8]:
            print(f"    {f}")
        if len(unstaged) > 8:
            print(msg(config, "uncommitted_more", count=len(unstaged) - 8))
        print()

    if staged:
        print(msg(config, "uncommitted_staged", count=len(staged)))
        for f in staged[:8]:
            print(f"    {f}")
        if len(staged) > 8:
            print(msg(config, "uncommitted_more", count=len(staged) - 8))
        print()

    if untracked:
        print(msg(config, "uncommitted_untracked", count=len(untracked)))
        for f in untracked[:5]:
            print(f"    {f}")
        if len(untracked) > 5:
            print(msg(config, "uncommitted_more", count=len(untracked) - 5))
        print()

    if not prompt_yes_no(
        msg(config, "uncommitted_question"),
        default_yes=False,
    ):
        print(msg(config, "uncommitted_cancelled"))
        sys.exit(0)
    print()


def count_local_changes() -> int:
    """How many paths `git status --porcelain` lists (changed, staged or untracked).

    0 when git cannot answer: this feeds a note, and a note must never be the
    reason a run stops.
    """
    try:
        result = run_git("status", "--porcelain", check=False)
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0:
        return 0
    # Python's own bytecode cache is not the student's change: in a repository
    # without a .gitignore, importing handin_config/handin_api creates it.
    return sum(
        1 for line in result.stdout.splitlines()
        if line.strip() and not line.rstrip().rstrip("/").endswith("__pycache__")
    )


def step2_push_changes(config: HandinConfig, analysis_expected: bool = True) -> None:
    """Step 2: Ensure committed changes are pushed.

    Why the push is asked for depends on the course, so the REASON printed here
    is chosen rather than assumed:

    Args:
        config: Loaded configuration (supplies the message language).
        analysis_expected: Whether the hand-in's code analysis step will run
            (`[defaults.code] check_analysis`). Decides which reason is printed;
            defaults to True so that a caller written before this parameter
            existed keeps the original wording.
    """
    warn_key = "push_warn_no_analysis" if analysis_expected else "push_warn_not_pushed"
    reason_key = (
        "push_needed_for_analysis" if analysis_expected else "push_needed_reason"
    )
    # Check if the current branch has an upstream
    result = run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if result.returncode != 0:
        # check=False: in a repository with no commits this exits 128, and
        # with check=True it raised CalledProcessError straight past the
        # student. `require_commits` in main() catches that case first; this is
        # the belt to its braces, and it keeps a detached or freshly created
        # branch from ending the run on a name lookup.
        branch_result = run_git("rev-parse", "--abbrev-ref", "HEAD", check=False)
        branch = branch_result.stdout.strip() or "HEAD"
        print(msg(config, "push_no_upstream", branch=branch))
        if DRY_RUN:
            # No question: a question whose answer is ignored is a lie, and
            # one whose answer is obeyed is a push. See DRY_RUN.
            print(msg(config, "dry_push_skipped"))
            print()
            return
        if prompt_yes_no(msg(config, "push_offer_upstream", branch=branch), default_yes=True):
            result = run_git("push", "-u", "origin", branch, check=False)
            if result.returncode != 0:
                print(msg(config, "push_branch_failed"))
                if result.stderr:
                    _print_git_error(result.stderr)
                # SAME CLASS AS THE REJECTED PUSH BELOW, and the same answer:
                # the hand-in rides on the tag, not on this branch. Both sites
                # must behave alike, or the same dead end is one branch away.
                print(msg(config, "push_failed_tag_still_works"))
                if not prompt_yes_no(msg(config, "push_continue_without"), default_yes=False):
                    print(msg(config, "submission_cancelled"))
                    sys.exit(1)
            else:
                print(msg(config, "push_ok"))
            print()
        else:
            print(msg(config, warn_key))
            if not prompt_yes_no(msg(config, "push_continue_without"), default_yes=False):
                print(msg(config, "submission_cancelled"))
                sys.exit(0)
            print()
        return

    # Check if there are commits ahead of the remote
    result = run_git("rev-list", "@{u}..HEAD")
    ahead_commits = [c for c in result.stdout.strip().split("\n") if c]

    if ahead_commits:
        count = len(ahead_commits)
        print(msg(config, "push_unpushed", count=count, plural="s" if count != 1 else ""))
        if DRY_RUN:
            print(msg(config, "dry_push_skipped"))
            print()
            return
        print(msg(config, reason_key))
        if prompt_yes_no(msg(config, "push_now"), default_yes=True):
            result = run_git("push", check=False)
            if result.returncode != 0:
                print(msg(config, "push_failed"))
                if result.stderr:
                    _print_git_error(result.stderr)
                print(msg(config, "push_failed_tag_still_works"))
                if not prompt_yes_no(msg(config, "push_continue_without"), default_yes=False):
                    print(msg(config, "push_manual"))
                    sys.exit(1)
            else:
                print(msg(config, "push_ok"))
        else:
            print(msg(config, warn_key))
            if not prompt_yes_no(msg(config, "push_continue_without"), default_yes=False):
                print(msg(config, "submission_cancelled"))
                sys.exit(0)
        print()


def extract_analysis_json(issue_body: str) -> dict[str, Any] | None:
    """Extract embedded analysis JSON from an issue description.

    Looks for a ``<details>`` section with summary "Analysis Data (JSON)"
    containing a JSON code block.

    Args:
        issue_body: The issue description markdown text.

    Returns:
        Parsed JSON dict, or None if not found or invalid.
    """
    # Look for: <details><summary>Analysis Data (JSON)</summary> ```json ... ``` </details>
    pattern = r"<details>\s*<summary>Analysis Data \(JSON\)</summary>\s*```json\s*(.*?)\s*```\s*</details>"
    match = re.search(pattern, issue_body, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def check_analysis_results(analysis_json: dict[str, Any], config: Any) -> bool:
    """Check analysis results for problems that may affect the hand-in.

    This is a placeholder that will be filled out later with specific
    logic for determining whether problems are severe enough to block
    a submission (komplettering-level) or warrant a warning.

    Args:
        analysis_json: Parsed analysis JSON from the issue.
        config: Loaded configuration (supplies the message language).

    Returns:
        True if the submission should proceed, False if it should be blocked.
    """
    stats = analysis_json.get("stats")
    if stats and len(stats) >= 2:
        # stats[1] = [count_total, showstoppers, severe, warning]
        counts = stats[1]
        if len(counts) >= 2:
            showstoppers = counts[1]
            if showstoppers > 0:
                print(msg(config, "analysis_critical", count=showstoppers,
                          plural="s" if showstoppers != 1 else ""))

    fatal = analysis_json.get("fatal_warnings")
    if fatal:
        print(msg(config, "analysis_fatal", count=len(fatal),
                  plural="s" if len(fatal) != 1 else ""))

    idea_warnings = analysis_json.get("idea_warnings")
    if idea_warnings:
        print(msg(config, "analysis_setup", count=len(idea_warnings),
                  plural="s" if len(idea_warnings) != 1 else ""))

    # The analysis informs; it never blocks the hand-in. Deciding what is
    # serious enough is the TA's job, with the analysis in front of them.
    return True


def step2b_check_analysis_issue(gl: Any, config: Any) -> None:
    """Step 2b: Check for a code analysis issue on the current commit.

    Queries the student's own GitLab project for an analysis issue
    matching the current HEAD commit.  The code analysis system creates
    issues automatically when commits are pushed.

    Only reached when the hand-in type has `check_analysis = true`; a course
    without a code-analysis system turns it off in its own `handin.toml`.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration (supplies the message language).
    """
    commit_hash = get_current_commit()
    project_path = get_project_path()

    if not project_path:
        return  # Can't determine project path; skip silently

    print(msg(config, "analysis_header"))
    print(msg(config, "analysis_commit", commit=commit_hash[:8]))

    try:
        # The project is fetched first, as in step9_create_issue: a lookup
        # that fails here says "no access" before any search is made.
        gl.get_project(project_path)
        # Search for issues mentioning this commit hash
        issues = gl.list_issues(project_path, search=commit_hash[:12], state="all")

        # Filter to issues that actually reference this commit
        matching = [i for i in issues if commit_hash[:12] in (i.get("description") or "")]
    except Exception as exc:
        print(msg(config, "analysis_unavailable", error=exc))
        print(msg(config, "analysis_continuing"))
        print()
        return

    if not matching:
        print(msg(config, "analysis_none_found"))
        print(msg(config, "analysis_none_why"))
        print(msg(config, "analysis_none_pending"))
        print(msg(config, "analysis_none_down"))
        print()
        if not prompt_yes_no(msg(config, "analysis_continue_q"), default_yes=False):
            print(msg(config, "analysis_cancelled"))
            sys.exit(0)
        print()
        return

    # Found an analysis issue — check for embedded analysis data
    issue = matching[0]  # Most recent match
    print(msg(config, "analysis_found", url=issue["web_url"]))

    analysis_json = extract_analysis_json(issue.get("description") or "")
    if analysis_json:
        proceed = check_analysis_results(analysis_json, config)
        if not proceed:
            print(msg(config, "analysis_problems"))
            sys.exit(1)
    else:
        print(msg(config, "analysis_no_data"))

    print()


def step3_select_handin_type(
    config: HandinConfig,
    preselected: HandinType | None = None,
    local_only_ids: list[str] | None = None,
) -> HandinType:
    """Step 3: Display menu and let the student select a hand-in type.

    Args:
        config: Loaded configuration.
        preselected: The type already named on the command line, or None. When
            given, the menu is skipped — but the same "Selected:" line is
            printed, so the run says out loud what it is about to hand in.
        local_only_ids: Hand-in ids that exist only in the student's own copy,
            listed last and labelled, so a hand-in the course has retired is not
            silently indistinguishable from a current one.

    Returns:
        The selected HandinType.
    """
    if preselected is not None:
        print(msg(config, "selected_handin", name=preselected.name))
        print()
        return preselected

    local_only = set(local_only_ids or [])
    options = []
    for ht in config.handins:
        if ht.id in local_only:
            options.append(msg(config, "handin_local_only", name=ht.name))
        else:
            options.append(ht.name)
    idx = prompt_choice(msg(config, "select_handin_prompt"), options, config)
    selected = config.handins[idx]
    print(msg(config, "selected_handin", name=selected.name))
    print()
    return selected


def step4_check_existing_issues(gl: Any, config: HandinConfig, handin_type: HandinType) -> int | None:
    """Step 4: Check for existing issues for this hand-in type.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        handin_type: The selected hand-in type.

    Returns:
        The IID of the existing issue to supersede, or None.
    """
    EXISTING_SEARCH.clear()
    EXISTING_SEARCH["searched"] = False

    if not config.resolve_check_existing(handin_type):
        return None

    print(msg(config, "existing_checking"))

    try:
        target_project_id = config.resolve_target_project(handin_type)
        username = gl.user["username"]
        labels = config.resolve_labels(handin_type)

        known_ids = {ht.id for ht in config.handins}
        found: dict[Any, dict[str, Any]] = {}
        for filters in ({"author_username": username}, {"assignee_username": [username]}):
            issues = gl.list_issues(target_project_id, state="all", **filters)
            # GitLab said there are MORE issues than the pages held. An
            # incomplete list is not "nothing found": the missing one may be
            # the earlier hand-in, and a new issue would then replace nothing.
            # It goes to the same question as a search that failed.
            if str(getattr(gl, "last_total_check", "")).startswith("mismatch"):
                raise IncompleteSearch(msg(config, "existing_incomplete"))
            for issue in issues:
                if issue_is_handin(issue, handin_type.id, labels, known_ids):
                    found[issue["iid"]] = issue

    except Exception as exc:
        # A search that FAILED is not a search that found nothing. Handing in
        # anyway may make a second issue for the same lab, so it is a question,
        # and the default is to stop.
        log_event("existing", f"search failed: {type(exc).__name__}: {exc}")
        print(msg(config, "existing_check_failed", details=exc))
        print()
        if not prompt_yes_no(msg(config, "existing_check_failed_question"), default_yes=False):
            print(msg(config, "submission_cancelled"))
            sys.exit(0)
        print()
        return None

    # Newest first, by iid: the author search and the assignee search are
    # concatenated, and in a pair the partner's newer issue came SECOND -- so
    # `[0]` would replace the older one. The iid grows monotonically within a
    # project.
    existing_issues = sorted(found.values(), key=lambda i: int(i["iid"]), reverse=True)

    # Only a search that COMPLETED may later be read as "this part is new".
    EXISTING_SEARCH["searched"] = True

    if not existing_issues:
        print(msg(config, "existing_none"))
        print()
        return None

    print()
    print(msg(config, "existing_found_header"))
    for issue in existing_issues:
        state_str = issue["state"].upper()
        print(f"    #{issue['iid']}: {issue['title']} [{state_str}]")
        print(f"    {issue['web_url']}")
    print()

    if not prompt_yes_no(
        msg(config, "existing_supersede_question"),
        default_yes=False,
    ):
        print(msg(config, "submission_cancelled"))
        sys.exit(0)

    # The most recent existing issue is the one replaced.
    superseded_iid = existing_issues[0]["iid"]
    EXISTING_SEARCH["previous_state"] = str(existing_issues[0].get("state") or "")
    print(msg(config, "existing_will_supersede", iid=superseded_iid))
    print()
    return superseded_iid


def step5_run_validation(
    repo_root: Path, config: HandinConfig, handin_type: HandinType, supersedes: int | None
) -> None:
    """Step 5: Run the validation script if it exists.

    Args:
        repo_root: Path to the repository root.
        config: Loaded configuration (supplies the message language).
        handin_type: The selected hand-in type.
        supersedes: IID of the issue being superseded, or None.
    """
    validate_script = repo_root / handin_type.code.validation_script

    if not validate_script.is_file():
        return

    if not os.access(validate_script, os.X_OK):
        return

    mode = "supersede" if supersedes else "new"
    print(msg(config, "validation_running"))
    print()

    try:
        result = subprocess.run(
            [str(validate_script), handin_type.id, mode],
            cwd=repo_root,
            check=False,
        )
    except OSError as exc:
        print(msg(config, "validation_cannot_run", details=exc))
        return

    if result.returncode == 0:
        print()
        print(msg(config, "validation_passed"))
        print()
    elif result.returncode == 1:
        print()
        print(msg(config, "validation_failed"))
        sys.exit(1)
    elif result.returncode == 2:
        print()
        if not prompt_yes_no(msg(config, "validation_warnings_question"), default_yes=False):
            print(msg(config, "submission_cancelled"))
            sys.exit(0)
        print()
    else:
        print()
        print(msg(config, "validation_unexpected_code", code=result.returncode))
        print()


def step6_checklist(config: HandinConfig, handin_type: HandinType) -> list[tuple[str, bool]]:
    """Step 6: Run through the pre-submission checklist.

    Args:
        config: Loaded configuration (supplies the message language).
        handin_type: The selected hand-in type.

    Returns:
        List of (question, answer) tuples.

    Raises:
        SystemExit: (exit 1) if a showstopper question (`stop_on_no = true`)
            is answered no. Nothing has been created at that point.
    """
    checklist = handin_type.checklist.items
    if not checklist:
        return []

    is_showstopper = getattr(handin_type.checklist, "is_showstopper", None)

    print(msg(config, "checklist_header"))
    print()
    answers: list[tuple[str, bool]] = []

    for index, question in enumerate(checklist):
        # Enter means NO (2.17.0). The questions are things the student must
        # actually have done ("Has a TA approved your demonstration?"), so an
        # answer has to be typed; until 2.17.0 the suffix was [Y/n] and a
        # reflexive Enter wrote "Yes" into the issue the TA reads.
        answer = prompt_yes_no(f"  {question}", default_yes=False)
        answers.append((question, answer))
        if not answer and is_showstopper is not None and is_showstopper(index):
            # The remaining questions are NOT asked: the run is over, and
            # answering more of them would suggest it is not.
            log_event("checklist", f"stop: no to {question!r}")
            print()
            print(msg(config, "checklist_stop", question=question))
            sys.exit(1)

    print()
    return answers


@dataclass
class ConfirmationOutcome:
    """What happened in the confirmation step, for the issue body and the caller.

      * ``confirmed`` -- the student typed the word. A clear yes.
      * ``declined``  -- the student typed the decline form and then described,
                         in ``description``, what they did. A clear no. It is
                         still a hand-in: it goes through, and the description
                         travels with it.
      * neither       -- an UNCLEAR answer. Nothing is handed in at all. This
                         state must never reach an issue body, because "not
                         confirmed" in a submission reads as an accusation
                         against a student who may only have pressed Enter.

    ``asked`` separates "the course does not ask" from all three. Without it a
    hand-in on a course with no confirmation would be reported to the TA
    as one where the student declined.
    """

    asked: bool = False
    confirmed: bool = False
    declined: bool = False
    description: str = ""
    question: str = ""
    word: str = ""

    @property
    def unclear(self) -> bool:
        """The question was asked and neither clear answer was given.

        The caller uses this to abandon the whole hand-in. It is a property
        rather than a fourth flag so that the three states cannot drift apart:
        there is exactly one place that decides what "unclear" means.
        """
        return self.asked and not self.confirmed and not self.declined


# Kept as a module-level name because the runtime comparison and the
# configuration guard in handin_config must normalise IDENTICALLY -- a guard
# that strips different characters from the runtime is not a guard at all. The
# implementation lives in handin_config for that reason; this is the old name.
_normalise_confirmation_answer = normalise_confirmation_answer


# How much of a student's description reaches the issue. A description is prose
# a person types under mild stress, not a log; these bounds exist so that a
# pasted transcript cannot push the rest of the submission out of the issue
# body, and they are generous enough that no honest answer meets them.
MAX_DESCRIPTION_LINES = 40
MAX_DESCRIPTION_CHARS = 4000


def _read_decline_description(config: HandinConfig, attempts: int) -> str | None:
    """Ask the student who answered "no" to describe what they did.

    Multi-line, ended by an empty line: what a student needs to say here rarely
    fits on one line, and a length limit on the answer to "what did you do"
    would be its own kind of pressure.
    """
    print(msg(config, "confirm_declined_intro"))
    for attempt in range(attempts):
        lines: list[str] = []
        truncated = False
        eof = False
        while True:
            try:
                line = input(msg(config, "confirm_declined_prompt"))
            except EOFError:
                # A closed terminal in the middle of the description. Whatever
                # was typed before it stands: the student did answer, and
                # throwing it away would punish a dropped connection. With
                # nothing typed it is simply an unclear answer, and re-asking a
                # terminal that has already ended would only print the question
                # `attempts` times into the void.
                eof = True
                line = ""
            if not line.strip():
                break
            if len(lines) >= MAX_DESCRIPTION_LINES:
                truncated = True
                break
            lines.append(line.rstrip())

        description = "\n".join(lines).strip()
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS].rstrip()
            truncated = True
        if truncated:
            description = description + "\n[...]"

        if description:
            print()
            print(msg(config, "confirm_declined_ok"))
            print()
            return description

        left = attempts - attempt - 1
        if eof or left <= 0:
            return None
        print()
        print(msg(config, "confirm_declined_empty", left=left))
        print()

    return None


def step6b_confirmation(config: HandinConfig) -> ConfirmationOutcome:
    """Ask the confirmation about the course rules for aids (one question, two clear answers).

    Four properties this function must keep:

      * A closed stdin (no terminal, a pipe, EOF) is an UNCLEAR answer, never an
        automatic yes. A tool that confirms on the student's behalf confirms
        nothing at all.
      * A wrong answer costs an attempt, not the session. Both words are shown
        again every time, so nobody can lose a hand-in to a typo.
      * Ctrl+C is the student's own cancel: it stops the whole submission with a
        plain sentence, and nothing is registered.
      * The decline form is read BEFORE the confirmation word. It cannot be a
        pool word (handin_config drops it, with a warning), but the order is
        what makes that guarantee independent of the configuration: even a
        malformed pool cannot turn "no" into a yes.

    Args:
        config: Loaded configuration (supplies language, word pool, attempts).

    Returns:
        The outcome. ``asked`` is False when the course has not enabled it, and
        ``unclear`` is True when the caller must abandon the hand-in.
    """
    if not config.confirmation.enabled:
        return ConfirmationOutcome(asked=False)

    words = config.resolve_confirmation_words()
    word = secrets.choice(words)
    decline_word = config.resolve_decline_word()
    question = msg(config, "confirm_question_short")
    attempts = max(1, config.confirmation.attempts)

    def unclear() -> ConfirmationOutcome:
        print()
        print(msg(config, "confirm_unclear", decline_word=decline_word))
        return ConfirmationOutcome(asked=True, question=question, word=word)

    print(msg(config, "confirm_header"))
    print()
    print(f"  {question}")
    if config.confirmation.rules_url:
        print(msg(config, "confirm_rules_line", rules_url=config.confirmation.rules_url))
    print()
    print(msg(config, "confirm_instruction_yes", word=word))
    print(msg(config, "confirm_instruction_no", decline_word=decline_word))
    print()

    for attempt in range(attempts):
        try:
            raw = input(msg(config, "confirm_prompt"))
        except EOFError:
            # No terminal to answer in. Not an answer, and not a refusal either.
            return unclear()
        except KeyboardInterrupt:
            print()
            print(msg(config, "confirm_cancelled"))
            print()
            sys.exit(0)

        # Decline first, deliberately: see the docstring.
        if is_decline_answer(raw):
            print()
            try:
                description = _read_decline_description(config, attempts)
            except KeyboardInterrupt:
                print()
                print(msg(config, "confirm_cancelled"))
                print()
                sys.exit(0)
            if description is None:
                return unclear()
            return ConfirmationOutcome(
                asked=True,
                declined=True,
                description=description,
                question=question,
                word=word,
            )

        if _normalise_confirmation_answer(raw).casefold() == word.casefold():
            print(msg(config, "confirm_ok"))
            print()
            return ConfirmationOutcome(asked=True, confirmed=True, question=question, word=word)

        left = attempts - attempt - 1
        if left > 0:
            print(msg(config, "confirm_retry", word=word, decline_word=decline_word, left=left))

    return unclear()


def _fence_for(text: str) -> str:
    """A code fence longer than any backtick run inside `text`.

    The description is the first student-typed prose this tool ever puts in an
    issue body. A fixed ``` fence would let a student's own backticks close it
    early and turn the rest of their text into Markdown -- headings, lists, or
    a broken body. Growing the fence is the standard fix and needs no escaping
    of the text itself.
    """
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def confirmation_body_lines(outcome: ConfirmationOutcome) -> list[str]:
    """Render the confirmation into the submission issue, for the TA.

    Part (b) of the ruling: the answer travels WITH the submission, where the
    code is -- no separate form, no second channel.

    Two things are deliberately NOT here, and both are part (d), "no new
    penalty": there is no label, and there is no record of what a student typed
    while getting the word wrong. The issue says which of the two clear answers
    was given, and on the "no" path what the student said about it. Anything
    beyond that turns a reminder into an accusation.
    """
    if not outcome.asked or outcome.unclear:
        return []

    lines = ["", "## Confirmation of course rules on aids", ""]
    lines.append(f"- **Question**: {outcome.question}")
    lines.append(f"- **Word shown**: `{outcome.word}`")
    if outcome.confirmed:
        lines.append(f"- **Answer**: `{outcome.word}` - **confirmed**")
        return lines

    lines.append("- **Answer**: **no** - the student states the course rules for aids were NOT followed")
    lines.append(
        "- This is the student's own clear answer, given voluntarily at hand-in, "
        "with the description below. Read the description before drawing any "
        "conclusion; the answer is a starting point for a conversation, not a "
        "finding."
    )
    lines.append("")
    lines.append("**The student's description of what they did:**")
    lines.append("")
    fence = _fence_for(outcome.description)
    lines.append(fence)
    lines.extend(outcome.description.splitlines() or [""])
    lines.append(fence)
    return lines


def step7_optional_message(
    config: HandinConfig,
    repo_name: str,
    handin_type: HandinType,
    checklist_answers: list[tuple[str, bool]],
    supersedes: int | None,
) -> str:
    """Step 7: Prompt for an optional message to the TA.

    Also saves draft state for recovery.

    Args:
        config: Loaded configuration (supplies the message language).
        repo_name: Name of the repository.
        handin_type: The selected hand-in type.
        checklist_answers: List of checklist (question, answer) pairs.
        supersedes: IID of the superseded issue, or None.

    Returns:
        The message string (empty string if none).
    """
    # Save draft before prompting
    draft_data: dict[str, Any] = {
        "repo_name": repo_name,
        "handin_id": handin_type.id,
        "handin_name": handin_type.name,
        "checklist": [{"question": q, "answer": a} for q, a in checklist_answers],
        "supersedes_iid": supersedes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    save_draft(repo_name, draft_data)

    print(msg(config, "message_header"))
    print()
    try:
        message = input(msg(config, "message_prompt")).strip()
    except EOFError:
        # No terminal to answer in (piped stdin).  "No message" is the right
        # reading, and the hand-in goes on.
        print()
        message = ""
    except KeyboardInterrupt:
        # Ctrl+C is NOT an answer.  Reading it as an empty message meant the
        # student who pressed it to stop the hand-in instead had it go through
        # -- tag pushed, issue created -- one step later.  Let it reach the
        # handler in __main__, which says "avbruten" and exits 0.
        print()
        raise

    # Update draft with message
    draft_data["message"] = message
    save_draft(repo_name, draft_data)

    if message:
        print(msg(config, "message_recorded", message=message))
    else:
        print(msg(config, "message_none"))
    print()

    return message


def handin_kind(supersedes: int | None) -> str:
    """The hand-in kind (see HANDIN_KIND_*), from what step 4 actually did."""
    if supersedes is not None:
        return HANDIN_KIND_RESUBMISSION
    if EXISTING_SEARCH.get("searched"):
        return HANDIN_KIND_NEW
    return HANDIN_KIND_UNKNOWN


def tag_annotation(handin_type: HandinType, kind: str, supersedes: int | None) -> str:
    """The message of the annotated submission tag: one subject, then trailers.

    The trailer lines are the machine-readable part and the contract with
    javacheck (TEACHER_GUIDE.md, "Hand-in kind in the tag"): `Key: value`, one
    per line, keys fixed, values without newlines. `git interpret-trailers
    --parse` reads them, and so does a plain line match.
    """
    lines = [
        f"Hand-in {handin_type.id} ({handin_type.name}): {kind}",
        "",
        f"Handin-Id: {handin_type.id}",
        f"Handin-Kind: {kind}",
    ]
    if supersedes is not None:
        lines.append(f"Handin-Supersedes: #{supersedes}")
        previous_state = EXISTING_SEARCH.get("previous_state")
        if previous_state:
            lines.append(f"Handin-Supersedes-State: {previous_state}")
    lines.append(f"Handin-Script-Version: {SCRIPT_VERSION}")
    return "\n".join(lines) + "\n"


def _create_local_tag(name: str, annotation: str, commit_hash: str) -> subprocess.CompletedProcess[str]:
    """Create the tag locally: annotated when there is an annotation.

    An annotated tag needs a committer identity, and honours `tag.gpgSign`
    (a signing prompt, or a missing key, in the middle of a hand-in). Signing is
    switched off for this one command. If the annotated tag still cannot be
    made, the hand-in falls back to the plain tag it always made: the
    annotation is information for javacheck, and losing it costs a possibly
    wrong "komplettering" note -- never the hand-in. The issue body still
    carries the kind for the TA.
    """
    if annotation:
        result = run_git("-c", "tag.gpgSign=false", "tag", "-a", name, commit_hash, "-m", annotation, check=False)
        if result.returncode == 0:
            return result
        log_event(
            "tag",
            f"annotated tag {name} failed, falling back to a plain tag: {(result.stderr or '').strip()}",
        )
    return run_git("tag", name, commit_hash, check=False)


def step8_create_tag(config: HandinConfig, repo_name: str, annotation: str = "", commit_hash: str | None = None) -> tuple[str, str]:
    """Step 8: Create and push a numbered tag.

    Fetches the remote's tags, determines the next tag name by finding the
    highest existing t<N> tag, creates the tag, and pushes it to origin.

    Args:
        config: Loaded configuration (supplies the message language).
        repo_name: Name of the repository (for error messages).
        annotation: Message for an annotated tag (see `tag_annotation`), or
            empty for a plain tag.

    Returns:
        Tuple of (tag_name, commit_hash).
    """
    print(msg(config, "tag_header"))

    commit_hash = commit_hash or get_current_commit()

    # The next free number is decided from the tags that exist AT GITLAB, not
    # only from the ones that happen to be in this clone.  A clone that has
    # never fetched tags -- a fresh clone, a partner's clone, the same student
    # on another machine -- knows nothing about t3 pushed from somewhere else,
    # picks t3 again, and the push is rejected.  The old code then deleted the
    # local t3 it had just made, so the very next run knew exactly as little as
    # this one and chose t3 again: the collision could not resolve itself, and
    # the student was locked out of handing in with no way to tell why.
    tags_are_known = fetch_tags_quietly(config)
    new_tag = next_free_tag_name()

    # Create tag locally
    result = _create_local_tag(new_tag, annotation, commit_hash)
    if result.returncode != 0:
        print(msg(config, "tag_create_failed", tag=new_tag))
        if result.stderr:
            _print_git_error(result.stderr)
        sys.exit(1)

    # Push tag to remote
    print(msg(config, "tag_pushing", tag=new_tag))
    result = run_git("push", "origin", new_tag, check=False)

    if result.returncode != 0:
        # Someone may have taken the name between the fetch above and this
        # push.  Ask GitLab once more: if the free number MOVED, the failure
        # was a collision and the retry is free.  If it did not move, the push
        # failed for some other reason (no network, no rights) and pushing a
        # second time would only cost the student another wait.  Deciding it
        # this way needs no parsing of git's stderr, which is translated.
        # Only worth asking again if the first ask worked. If GitLab could not
        # be reached a moment ago, a second wait of the same length buys the
        # student nothing but the wait -- the retry needs the free number to
        # have MOVED, and a fetch that fails cannot move it. Skipping it keeps
        # the worst case at one timeout instead of two.
        run_git("tag", "-d", new_tag, check=False)
        retry_tag = new_tag
        if tags_are_known:
            fetch_tags_quietly(config)
            retry_tag = next_free_tag_name()

        if retry_tag == new_tag:
            print(msg(config, "tag_push_failed", tag=new_tag))
            if result.stderr:
                _print_git_error(result.stderr)
            print(msg(config, "tag_resolve_and_retry"))
            sys.exit(1)

        new_tag = retry_tag
        create = _create_local_tag(new_tag, annotation, commit_hash)
        if create.returncode != 0:
            print(msg(config, "tag_create_failed", tag=new_tag))
            if create.stderr:
                _print_git_error(create.stderr)
            sys.exit(1)

        print(msg(config, "tag_pushing", tag=new_tag))
        result = run_git("push", "origin", new_tag, check=False)
        if result.returncode != 0:
            run_git("tag", "-d", new_tag, check=False)
            print(msg(config, "tag_push_failed", tag=new_tag))
            if result.stderr:
                _print_git_error(result.stderr)
            print(msg(config, "tag_resolve_and_retry"))
            sys.exit(1)

    SUBMISSION_PROGRESS["tag"] = new_tag
    print(msg(config, "tag_created", tag=new_tag, commit=commit_hash[:8]))
    print()

    return new_tag, commit_hash


def _exit_issue_create_failed(config: HandinConfig, exc: Exception, tag_name: str) -> None:
    """The issue was certainly NOT created: say so, and what the tag means."""
    print(msg(config, "issue_create_failed", details=exc))
    print()
    if tag_name:
        print(msg(config, "issue_tag_pushed_contact_ta", tag=tag_name))
    else:
        print(msg(config, "issue_contact_ta"))
    sys.exit(1)


def _find_issue_after_unclear_create(
    gl: Any, target_project_id: Any, handin_id: str, tag_name: str, submitted_at: str
) -> dict[str, Any] | None:
    """Look once for the issue a POST with a lost answer may have created.

    A match needs the student's OWN issue whose "Submission Information" carries
    this very run's `Submitted at` stamp (to the second, written by the tool,
    never by the student), this hand-in id and, for code, this tag. Anything
    else -- including a lookup that fails -- is "not found", which the caller
    reports as UNCLEAR, never as "not created".
    """
    time.sleep(UNCLEAR_CREATE_WAIT_SECONDS)
    try:
        username = gl.user["username"]
        candidates = gl.list_issues(
            target_project_id, author_username=username, state="all", order_by="created_at"
        )
    except Exception as exc:
        log_event("issue", f"lookup after unclear create failed: {type(exc).__name__}: {exc}")
        return None
    stamp = f"- **Submitted at**: {submitted_at}"
    tag_line = f"- **Tag**: `{tag_name}`"
    for issue in candidates:
        if not isinstance(issue, dict) or not issue.get("web_url"):
            continue
        body = str(issue.get("description") or "")
        if not issue_is_handin(issue, handin_id, []):
            continue
        # The tag sits in "## Submission Information"; the stamp in the
        # "## Script Metadata" section at the very end, after the student's
        # message -- so it is compared as a whole line, and the LAST such line
        # is the tool's own.
        section_lines = {line.strip() for line in (submission_section(body) or "").splitlines()}
        if tag_name and tag_line not in section_lines:
            continue
        stamps = [line.strip() for line in body.splitlines() if line.strip().startswith("- **Submitted at**:")]
        if stamps and stamps[-1] == stamp:
            return issue
    return None


CHECK_STATUS_IN_ISSUE = {
    "passed": "passed",
    "failed": "**FAILED**",
    "advice": "advice to review",
    "skipped": "could not be run here",
}


def check_results_body_lines(
    check_results: list[dict[str, str]] | None, missing_paths: list[str] | None = None,
    snapshot_failed: bool = False,
) -> list[str]:
    """The issue's "Pre-submission checks" section, for the TA (English, like the body)."""
    results = list(check_results or [])
    missing = list(missing_paths or [])
    if not results and not missing and not snapshot_failed:
        return []
    lines = ["", "## Pre-submission checks", ""]
    if snapshot_failed:
        lines.append("- None of the checks could be run: the committed code could not be copied.")
    for result in results:
        status = CHECK_STATUS_IN_ISSUE.get(result.get("status", ""), result.get("status", "?"))
        lines.append(f"- {result.get('name', '?')}: {status}")
    if missing:
        lines.append(f"- Required files: **MISSING** in the handed-in commit: {', '.join(missing)}")
    if any(r.get("status") == "failed" for r in results) or missing:
        lines.append("")
        lines.append(
            "The student was told about the problems above and chose to hand in anyway."
        )
    return lines


def step9_create_issue(
    gl: Any,
    config: HandinConfig,
    repo_name: str,
    handin_type: HandinType,
    tag_name: str,
    commit_hash: str,
    checklist_answers: list[tuple[str, bool]],
    message: str,
    supersedes: int | None,
    report_link: str = "",
    ai_review_markdown: str = "",
    group_members: list[str] | None = None,
    confirmation: ConfirmationOutcome | None = None,
    kind: str = "",
    check_results: list[dict[str, str]] | None = None,
    missing_paths: list[str] | None = None,
    snapshot_failed: bool = False,
) -> str:
    """Step 9: Create the confidential GitLab issue.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        repo_name: Name of the repository.
        handin_type: The selected hand-in type.
        tag_name: The tag name created in step 8 (empty for reports).
        commit_hash: The full commit hash (empty for reports).
        checklist_answers: List of checklist (question, answer) pairs.
        message: Optional message to the TA.
        supersedes: IID of the issue being superseded, or None.
        report_link: Markdown link to uploaded report file (empty for code).
        ai_review_markdown: Formatted AI review findings (empty if not run).
        group_members: List of group member GitLab usernames (empty for solo).
        confirmation: Outcome of the confirmation step (None on a course that
            does not ask for one).
        kind: The hand-in kind (HANDIN_KIND_*), or empty to leave the line out.
        check_results: The pre-submission checks of this run, as recorded in
            SUBMISSION_PROGRESS["check_results"] (None or empty: no section).
        missing_paths: Required paths the student was warned about and handed
            in without (None or empty: no line).
        snapshot_failed: The committed code could not be copied, so no check
            ran; said in the section, since an empty one looks like "no checks".

    Returns:
        The URL of the created issue.
    """
    print(msg(config, "submit_header"))

    gitlab_url = config.course.gitlab_url

    # Build tag URL
    result = run_git("remote", "get-url", "origin", check=False)
    remote_url = result.stdout.strip() if result.returncode == 0 else ""
    tag_url = ""
    if remote_url:
        # Third copy of the URL parsing, and the second that did not know
        # `ssh://` -- the TA then got an issue with no link to the tag,
        # silently. Same measurement as in `get_project_path`. The two branches
        # below preserve the behaviour the other two forms already had: an
        # http(s) remote keeps ITS OWN host (the student may be on a mirror),
        # while a scheme-less or ssh remote cannot say http or https and
        # therefore uses the course's own base URL.
        _host, web_path = parse_origin_url(remote_url)
        if web_path:
            if remote_url.startswith(("https://", "http://")):
                scheme = remote_url.split("://", 1)[0]
                tag_url = f"{scheme}://{_host}/{web_path}/-/tags/{tag_name}"
            else:
                tag_url = f"{gitlab_url.rstrip('/')}/{web_path}/-/tags/{tag_name}"

    # Build issue body
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    body_parts = []

    # Submission Information
    body_parts.append("## Submission Information")
    body_parts.append("")
    body_parts.append(f"- **Repository**: {repo_name}")
    body_parts.append(f"- **Hand-in**: `{handin_type.id}`")
    body_parts.append(f"- **Type**: {handin_type.type}")
    if kind:
        body_parts.append(f"- **Submission kind**: {kind}")
    if tag_name:
        body_parts.append(f"- **Tag**: `{tag_name}`")
    if commit_hash:
        body_parts.append(f"- **Commit**: `{commit_hash}`")
    if tag_url:
        body_parts.append(f"- **Tag URL**: {tag_url}")
    if group_members:
        members_str = ", ".join(f"@{m}" for m in group_members)
        body_parts.append(f"- **Group members**: {members_str}")

    # Report attachment
    if report_link:
        body_parts.append("")
        body_parts.append("## Report")
        body_parts.append("")
        body_parts.append(report_link)

    # AI pre-review results
    if ai_review_markdown:
        body_parts.append("")
        body_parts.append(ai_review_markdown)

    # Pre-submission checks (2.17.0). Until then the issue said nothing about
    # them, so a hand-in made after "Continue anyway?" looked complete to the
    # TA; only the student's own receipt named the failed checks.
    body_parts.extend(check_results_body_lines(check_results, missing_paths, snapshot_failed))

    # Checklist
    if checklist_answers:
        body_parts.append("")
        body_parts.append("## Checklist")
        body_parts.append("")
        for question, answer in checklist_answers:
            answer_str = "Yes" if answer else "No"
            body_parts.append(f"- {question}: **{answer_str}**")

    # Confirmation about the course rules for aids
    if confirmation is not None:
        body_parts.extend(confirmation_body_lines(confirmation))

    # Message to TA
    body_parts.append("")
    body_parts.append("## Message to TA")
    body_parts.append("")
    body_parts.append(message if message else "None")

    # Script Metadata
    body_parts.append("")
    body_parts.append("## Script Metadata")
    body_parts.append("")
    if supersedes is not None:
        body_parts.append(f"- **Supersedes issue**: #{supersedes}")
    body_parts.append(f"- **Script version**: {SCRIPT_VERSION}")
    body_parts.append(f"- **Submitted at**: {now_str}")

    description = "\n".join(body_parts)

    # Issue title
    title = f"[{handin_type.name}] {repo_name}"

    # Labels and confidentiality from config
    labels = config.resolve_labels(handin_type)
    confidential = config.resolve_confidential(handin_type)
    target_project_id = config.resolve_target_project(handin_type)

    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "confidential": confidential,
        "labels": labels,
    }

    target_project: Any = None
    try:
        # The project is fetched first even though creating the issue would
        # fail on its own: one extra GET is cheap next to a hand-in that
        # silently goes somewhere else, and its failure is a plain "not sent".
        target_project = gl.get_project(target_project_id)
    except ApiError as exc:
        # Nothing was sent yet: this is a plain failure, never an unclear one.
        log_event("issue", f"project lookup failed: {type(exc).__name__}: {exc}")
        _exit_issue_create_failed(config, exc, tag_name)

    # The one link the "unclear" messages send the student to. Without the
    # project's own web_url there is no reliable project path to build (the id
    # may be numeric), so it falls back to the student's own issue list, which
    # shows confidential issues they wrote.
    project_url = ""
    if isinstance(target_project, dict):
        project_url = str(target_project.get("web_url") or "")
    issues_url = (
        f"{project_url.rstrip('/')}/-/issues" if project_url else f"{gitlab_url.rstrip('/')}/dashboard/issues"
    )
    SUBMISSION_PROGRESS["issues_url"] = issues_url

    try:
        SUBMISSION_PROGRESS["post_sent"] = True
        issue = gl.create_issue(target_project_id, payload)
        if not isinstance(issue, dict) or not issue.get("web_url"):
            raise ApiTransport("GitLab answered without the created issue", None, "")
    except ApiError as exc:
        log_event("issue", f"create failed: {type(exc).__name__}: {exc}")
        unclear = isinstance(exc, ApiTransport) and (
            exc.status is None or (isinstance(exc.status, int) and exc.status >= 500)
        )
        if not unclear:
            _exit_issue_create_failed(config, exc, tag_name)
        recovered = _find_issue_after_unclear_create(
            gl, target_project_id, handin_type.id, tag_name, now_str
        )
        if recovered is None:
            print(msg(config, "issue_create_uncertain", details=exc, issues_url=issues_url))
            print()
            sys.exit(1)
        issue = recovered
        log_event("issue", f"recovered after unclear create: {issue.get('web_url')}")
        print(msg(config, "issue_create_recovered", url=issue.get("web_url", "")))

    SUBMISSION_PROGRESS["issue_url"] = str(issue["web_url"])

    if labels:
        returned_labels = issue.get("labels")
        if isinstance(returned_labels, list):
            missing_labels = [label for label in labels if label not in returned_labels]
        else:
            missing_labels = list(labels)
        if missing_labels:
            log_event("issue", f"labels dropped by GitLab: {missing_labels}")
            if not config.defaults.labels_set_later:
                warning = msg(config, "labels_dropped", labels=", ".join(missing_labels))
                print()
                print(warning)
                SUBMISSION_WARNINGS.append(warning)

    # Read the confidentiality flag BACK out of the response. A 2xx from GitLab
    # is not a receipt that a field was accepted — measured: labels and
    # assignee_ids are dropped silently for a Guest, with 201 Created. For
    # `confidential` a silent drop is a DISCLOSURE: the issue carries names and
    # code in a project every logged-in user at the university can open.
    #
    # The default semantics MUST match what `getattr(issue, "confidential",
    # False) is not True` did against an object. `.get("confidential")` returns
    # None when the field is absent, and `None is not True` is True — so an
    # answer without the field still enters the repair branch, exactly as
    # before. `if not issue["confidential"]` would raise KeyError instead, and
    # wrapping that in try/except would turn the whole guard into a no-op.
    if confidential and issue.get("confidential") is not True:
        # One remediation attempt before giving up, so the exposure window is as
        # short as possible. Then read it back again — same rule as above.
        repaired = False
        try:
            # PUT exactly the one field. A PUT of the whole object would send
            # description, title and labels back and overwrite whatever the
            # server-side bot had set between create and save.
            gl.update_issue(target_project_id, issue["iid"], {"confidential": True})
            issue = gl.get_issue(target_project_id, issue["iid"])
            repaired = issue.get("confidential") is True
        except ApiError:
            repaired = False

        if not repaired:
            print()
            print(msg(config, "confidential_failed", issue_url=issue["web_url"]))
            print()
            sys.exit(1)

        print(msg(config, "confidential_repaired"))

    SUBMISSION_PROGRESS["confirmed"] = True
    return issue["web_url"]


# ─── Report helpers ─────────────────────────────────────────────────────────


def _count_pdf_pages(file_path: Path) -> int | None:
    """Count pages in a PDF file using PyMuPDF (fitz).

    Returns:
        Page count, or None if PyMuPDF is not installed.
    """
    try:
        import fitz  # PyMuPDF  # noqa: PLC0415

        doc = fitz.open(str(file_path))
        count = len(doc)
        doc.close()
        return count
    except ImportError:
        return None
    except Exception:
        return None


def step_run_ai_review(config: HandinConfig, handin_type: HandinType, file_path: Path) -> str:
    """Run AI pre-review on a report file.

    Executes the configured AI review command as a subprocess and returns
    a Markdown-formatted summary of the findings.

    Args:
        config: Loaded configuration (supplies the message language).
        handin_type: The selected hand-in type.
        file_path: Path to the report file.

    Returns:
        Markdown-formatted review findings, or empty string if review
        is disabled or fails.
    """
    ai_cfg = handin_type.ai_review
    if not ai_cfg.enabled or not ai_cfg.command:
        return ""

    print(msg(config, "ai_review_header"))
    print()
    print(msg(config, "ai_review_running"))

    # Substitute {file} placeholder in command
    cmd = ai_cfg.command.replace("{file}", str(file_path))

    # Same runner as the lab tests (2.11.0): own process group, whole tree
    # killed on timeout, output decoded as UTF-8 with replacement. With
    # `subprocess.run(shell=True, capture_output=True, text=True)` a timeout
    # could hang on Windows and a non-cp1252 byte raised UnicodeDecodeError.
    try:
        result = _run_lint_command(cmd, Path.cwd(), 600)
    except subprocess.TimeoutExpired:
        print(msg(config, "ai_review_timeout"))
        print()
        return ""
    except Exception as exc:
        print(msg(config, "ai_review_failed", details=exc))
        print()
        return ""

    if result.returncode != 0:
        print(msg(config, "ai_review_error"))
        if result.stderr:
            print(f"  {RUN_LOG.mask(result.stderr)[:200]}")
        print()
        return ""

    # Parse JSON output
    output = result.stdout.strip()
    if not output:
        print(msg(config, "ai_review_no_findings"))
        print()
        return ""

    try:
        # Pipeline may prefix output with non-JSON text
        json_start = output.index("{")
        review_data = json.loads(output[json_start:])
    except ValueError:
        print(msg(config, "ai_review_unparsable"))
        print()
        return ""

    # Format findings as Markdown
    return _format_ai_review_markdown(review_data)


def _format_ai_review_markdown(review_data: dict[str, Any]) -> str:
    """Format AI review JSON output as Markdown for the issue body.

    Args:
        review_data: Parsed JSON from the AI review pipeline.

    Returns:
        Markdown-formatted review summary.
    """
    parts = ["## AI Pre-Review Results", ""]

    issue_count = 0
    for category in ("language_issues", "structure_issues", "content_issues"):
        issues = review_data.get(category, [])
        if not issues:
            continue

        cat_name = category.replace("_issues", "").capitalize()
        parts.append(f"### {cat_name}")
        parts.append("")

        for issue in issues:
            issue_type = issue.get("type", issue.get("issue_type", "unknown"))
            quote = issue.get("quote", "")
            explanation = issue.get("explanation", issue.get("description", ""))

            parts.append(f"- **{issue_type}**")
            if quote:
                parts.append(f'  > "{quote}"')
            if explanation:
                parts.append(f"  {explanation}")
            parts.append("")
            issue_count += 1

    if issue_count == 0:
        parts.append("No issues found.")
        parts.append("")
    else:
        parts.append(f"*{issue_count} issue(s) found.*")
        parts.append("")

    return "\n".join(parts)


# ─── Report step functions ──────────────────────────────────────────────────


def step_select_report_file(config: HandinConfig, handin_type: HandinType) -> Path | None:
    """Select and validate a report file for submission.

    Prompts the student for a file path, then validates format (by
    extension and magic bytes for PDF) and file size.

    Args:
        config: Loaded configuration.
        handin_type: The selected hand-in type.

    Returns:
        Path to the validated file, or None if cancelled.
    """
    print(msg(config, "report_header"))
    print()
    allowed = handin_type.report.allowed_formats
    allowed_str = ", ".join(f".{fmt}" for fmt in allowed)
    max_mb = handin_type.report.max_size_mb
    print(msg(config, "report_allowed_formats", formats=allowed_str))
    print(msg(config, "report_max_size", max_mb=max_mb))
    print()

    try:
        file_str = input(f"  {handin_type.report.file_prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not file_str:
        return None

    # Relative to where the student STOOD, which is not the current
    # directory if enter_script_repo_if_outside moved it.
    file_path = (ORIGINAL_CWD / Path(file_str).expanduser()).resolve()
    if not file_path.is_file():
        print(msg(config, "report_not_found", path=file_path))
        return None

    # Validate file extension
    ext = file_path.suffix.lower().lstrip(".")
    if ext not in allowed:
        print(msg(config, "report_bad_format", ext=ext))
        print(msg(config, "report_allowed_formats", formats=allowed_str))
        return None

    # Validate file size
    size_bytes = file_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    if size_mb > max_mb:
        print(msg(config, "report_too_large", size=f"{size_mb:.1f}", max_mb=max_mb))
        return None

    # Validate PDF magic bytes
    if ext == "pdf":
        try:
            with open(file_path, "rb") as f:
                header = f.read(5)
            if header != b"%PDF-":
                print(msg(config, "report_not_pdf"))
                return None
        except OSError as exc:
            print(msg(config, "report_unreadable", details=exc))
            return None

    # Validate page count (PDF only, requires PyMuPDF)
    min_pages = handin_type.report.min_pages
    max_pages = handin_type.report.max_pages
    if ext == "pdf" and (min_pages > 0 or max_pages > 0):
        page_count = _count_pdf_pages(file_path)
        if page_count is not None:
            if min_pages > 0 and page_count < min_pages:
                print(msg(config, "report_too_few_pages", pages=page_count, minimum=min_pages))
                return None
            if max_pages > 0 and page_count > max_pages:
                print(msg(config, "report_too_many_pages", pages=page_count, maximum=max_pages))
                return None
            print(msg(config, "report_page_count", pages=page_count))
        else:
            print(msg(config, "report_page_count_unknown"))

    print(msg(config, "report_selected", name=file_path.name, size=f"{size_mb:.1f}"))
    print()
    return file_path


def step_upload_report(gl: Any, config: HandinConfig, handin_type: HandinType, file_path: Path) -> str:
    """Upload a report file to GitLab and return the markdown link.

    Uses the GitLab project uploads API to attach the file, then returns
    a markdown-formatted link that can be embedded in the issue body.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        handin_type: The selected hand-in type.
        file_path: Path to the file to upload.

    Returns:
        Markdown link to the uploaded file.

    Raises:
        SystemExit: If the upload fails.
    """
    target_project_id = config.resolve_target_project(handin_type)

    print(msg(config, "upload_running"))
    try:
        gl.get_project(target_project_id)
        upload = gl.upload(target_project_id, file_path)
        markdown = upload["markdown"]
        url = upload.get("url", "")
        print(msg(config, "upload_ok", name=file_path.name))
        if url:
            print(f"    URL: {url}")
        print()
        return markdown
    except Exception as exc:
        # Deliberately broad, and it was broad before the rewrite. Besides an
        # Api* error this covers an OSError from reading the file and a
        # KeyError from a 200 whose body lacks `markdown`. Narrowing it to
        # ApiError would turn both into a traceback AFTER the student has
        # already tagged and pushed.
        print(msg(config, "upload_failed", details=exc))
        print()
        print(msg(config, "upload_retry"))
        sys.exit(1)


# How long one lab test may run. A test that is still running after this is
# counted as FAILED (a beginner's infinite loop is the usual cause), not skipped.
LINT_TIMEOUT_SECONDS = 120
LINT_STDOUT_LINES = 10
LINT_STDERR_LINES = 5

# The placeholder a lint command may use for "the Python that is running this
# tool". The courses tell Windows students to type `python`, and on Windows
# `python3` is either missing (exit 9009 from cmd.exe) or the Microsoft Store
# stub (which also exits 9009) -- so a command written `python3 test_x.py`
# could never run there. `{python}` is replaced by sys.executable, quoted for
# the platform's shell. Replaced with str.replace, NEVER str.format: a command
# is free shell text and may well contain `{}` of its own.
PYTHON_PLACEHOLDER = "{python}"


def expand_lint_command(command: str) -> str:
    """Substitute `{python}` in a lint command; everything else is untouched."""
    if PYTHON_PLACEHOLDER not in command:
        return command
    exe = sys.executable or ("python" if os.name == "nt" else "python3")
    # Always quoted on Windows: list2cmdline quotes only for spaces, and cmd.exe
    # also reads `&`, `^` and `%...%` in an unquoted path (a user folder named
    # "Tom&Jerry"). A Windows path cannot contain `"`, so plain quotes are safe.
    quoted = f'"{exe}"' if os.name == "nt" else shlex.quote(exe)
    return command.replace(PYTHON_PLACEHOLDER, quoted)


def _is_cannot_run_code(returncode: int) -> bool:
    """Exit codes that mean "the program is not there", not "the test failed".

    126/127 are what a POSIX shell answers. cmd.exe answers 9009 for a command
    it cannot find, and so does the Microsoft Store `python3` stub -- neither is
    126/127, so before 2.11.0 a Windows student without `uv` or `python3` got
    "[FAIL]" and a question whose default was not to hand in. Simulated in the
    tests; not yet measured on a Windows machine.
    """
    if returncode in (126, 127):
        return True
    return os.name == "nt" and returncode == 9009


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the shell AND everything it started. Never raises."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Collect what the killed process left, but never wait long for it."""
    try:
        out, err = proc.communicate(timeout=5)
        return out or b"", err or b""
    except (subprocess.TimeoutExpired, ValueError, OSError):
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        return b"", b""


def _run_lint_command(command: str, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one lab test through the shell, with a timeout that really holds.

    `subprocess.run(shell=True, capture_output=True, timeout=...)` does not hold
    on Windows: on timeout it kills cmd.exe and then waits for the pipes, which
    `uv` or `python` underneath still has open -- so a test that timed out hung
    the hand-in for good. The same class `run_git`'s `discard_output` exists
    for. Here the output is needed, so instead the command gets its own process
    group and the WHOLE TREE is killed, on timeout and on Ctrl+C alike (the
    student's Ctrl+C no longer reaches a child in its own group, so it must be
    passed on by hand).

    The child is told to write UTF-8 (PYTHONUTF8/PYTHONIOENCODING) and its
    output is decoded as UTF-8 with replacement: a Python child on Windows
    otherwise writes cp1252 to a pipe and crashes on the first character
    outside it, and uv writes UTF-8 regardless. stdin is closed, so a test that
    asks for input fails at once instead of waiting for the timeout.

    Raises:
        subprocess.TimeoutExpired: After the tree has been killed.
        OSError: When the shell itself cannot be started.
    """
    project_environment = {
        "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV",
        "CONDA_PREFIX", "UV_PROJECT", "UV_WORKING_DIR", "UV_CONFIG_FILE", "UV_PROJECT_ENVIRONMENT",
    }
    env = {key: value for key, value in os.environ.items()
           if key not in project_environment and not key.startswith("GIT_")}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )
    # Waited for in SHORT slices, not one long `communicate(timeout=120)`. The
    # child is in its own group and no longer gets the student's Ctrl+C, and on
    # Windows one long wait is a thread join that Ctrl+C cannot interrupt -- the
    # terminal would look dead for up to two minutes. Between slices the
    # KeyboardInterrupt gets through, and the tree is killed below.
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                out, err = proc.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        out, err = _drain(proc)
        raise subprocess.TimeoutExpired(command, timeout, output=out, stderr=err) from None
    except BaseException:
        _kill_process_tree(proc)
        _drain(proc)
        raise
    return subprocess.CompletedProcess(
        command,
        proc.returncode,
        (out or b"").decode("utf-8", "replace"),
        (err or b"").decode("utf-8", "replace"),
    )


LOG_OUTPUT_HEAD = 8000
LOG_OUTPUT_TAIL = 12000


def _as_text(data: bytes | str | None) -> str:
    """Output from a TimeoutExpired, which may be bytes, str or None."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def _clip_for_log(text: str) -> str:
    """The START and the END of a test's output, with a marker for the cut."""
    if len(text) <= LOG_OUTPUT_HEAD + LOG_OUTPUT_TAIL:
        return text
    cut = len(text) - LOG_OUTPUT_HEAD - LOG_OUTPUT_TAIL
    return (
        text[:LOG_OUTPUT_HEAD]
        + f"\n[... {cut} characters left out of the log ...]\n"
        + text[-LOG_OUTPUT_TAIL:]
    )


def _print_git_error(text: str) -> None:
    """Print what git wrote on stderr, without a user name or token in it.

    A failed push quotes the remote's address, and an HTTPS address can carry
    a token. Students show this output to TAs, often as a screenshot.
    """
    print(f"  {RUN_LOG.mask(text.strip())}")


def _print_tail(config: HandinConfig, text: str, count: int) -> None:
    """Print the last `count` lines of `text`, and say how many were left out.

    Masked as a whole BEFORE the lines are picked: `uv sync` and the checks can
    quote an address with a token in it, and a cut can split one in two.
    """
    lines = RUN_LOG.mask(text).strip().split("\n")
    if len(lines) > count:
        key = "lint_output_more" if RUN_LOG.exists else "lint_output_more_nolog"
        print(msg(config, key, count=len(lines) - count))
    for line in lines[-count:]:
        print(f"       {line}")


@contextmanager
def committed_snapshot(repo_root: Path, commit_hash: str):
    """Yield an isolated copy of one commit, cleaned up even on interruption."""
    directory = tempfile.mkdtemp(prefix="handin-check-")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", commit_hash],
            capture_output=True, check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(directory, filter="data")
        yield Path(directory)
    finally:
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception as exc:
            # ignore_errors covers OSError only; a very deep tree still raises
            # RecursionError on 3.12, and that must not replace the verdict or
            # an exception already on its way out.
            log_event("lint", f"snapshot left behind: {type(exc).__name__}")


def run_committed_checks(
    repo_root: Path, config: HandinConfig, handin_type: HandinType,
    commit_hash: str, *, interactive: bool = True,
) -> bool:
    """Check the exact commit that will be tagged; never inspect local edits."""
    print(msg(config, "checking_commit", commit=commit_hash))
    SUBMISSION_PROGRESS["check_unavailable"] = False
    SUBMISSION_PROGRESS["snapshot_failed"] = False
    SUBMISSION_PROGRESS["check_results"] = []
    SUBMISSION_PROGRESS["missing_paths"] = []
    # Only a failure to CREATE the copy may become "could not check". Once the
    # checks have run, their verdict stands: an error after that point must not
    # turn a declined "continue anyway?" into a hand-in.
    entered = False
    try:
        with committed_snapshot(repo_root, commit_hash) as snapshot:
            entered = True
            paths_ok = warn_if_required_paths_missing(
                snapshot, config, handin_type, interactive=interactive, worktree=repo_root
            )
            checks_ok = step_run_lint_checks(
                snapshot, config, handin_type, interactive=interactive, worktree=repo_root
            )
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        if entered:
            raise
        log_event("lint", f"cannot create commit snapshot: {type(exc).__name__}")
        print(msg(config, "snapshot_failed"))
        SUBMISSION_PROGRESS["check_unavailable"] = True
        SUBMISSION_PROGRESS["snapshot_failed"] = True
        return interactive
    return checks_ok and (paths_ok or interactive)


def check_exit_code(passed: bool) -> int:
    """Exit code for `--check`: 1 for findings, 2 when nothing could be judged.

    A finding wins over a check that could not run, so a failing test is never
    reported as "could not check" because some other tool was missing.
    """
    if SUBMISSION_PROGRESS.get("snapshot_failed"):
        return 2
    if not passed:
        return 1
    return 2 if SUBMISSION_PROGRESS.get("check_unavailable") else 0


def show_handin_status(gl: Any, config: HandinConfig, selected: HandinType | None = None) -> None:
    """List latest visible issues and server labels without interpreting grades."""
    username = gl.user["username"]
    found = {}
    for handin_type in ([selected] if selected else config.handins):
        project = config.resolve_target_project(handin_type)
        if project not in found:
            by_id = {}
            incomplete = False
            for filters in ({"author_username": username}, {"assignee_username": [username]}):
                for issue in gl.list_issues(project, state="all", **filters):
                    by_id[issue["iid"]] = issue
                if str(getattr(gl, "last_total_check", "")).startswith("mismatch"):
                    incomplete = True
            if incomplete:
                # Read-only, so no question: but "nothing found" must not be
                # the last word when GitLab said there was more.
                print(msg(config, "status_incomplete"))
            found[project] = list(by_id.values())
        matches = [issue for issue in found[project] if issue_is_handin(
            issue, handin_type.id, config.resolve_labels(handin_type), {h.id for h in config.handins}
        )]
        print(f"{handin_type.id}:")
        if not matches:
            print(msg(config, "status_none"))
            continue
        issue = max(matches, key=lambda item: int(item["iid"]))
        description = issue.get("description") or ""
        commit = re.search(r"\*\*Commit\*\*:\s*`([^`]+)`", description)
        print(f"  {issue.get('web_url', '')}")
        print(msg(config, "status_details", state=issue.get("state", "?"),
                  labels=", ".join(issue.get("labels") or []) or "-",
                  commit=commit.group(1) if commit else "-"))
        participants = re.search(r"\*\*Group members\*\*:\s*(.+)", description)
        if participants:
            print(msg(config, "status_partners", members=participants.group(1)))
    print(msg(config, "status_not_grade"))


def confirm_participants(gl: Any, config: HandinConfig, handin_type: HandinType, members: list[str]) -> bool:
    """Confirm the real participant list; an empty list explicitly means solo."""
    names = ", ".join(["@" + gl.user["username"], *("@" + member for member in members)])
    print(msg(config, "participants", members=names))
    question = "participants_confirm" if members else "participants_solo"
    if not prompt_yes_no(msg(config, question), default_yes=bool(members)):
        return False
    if handin_type.max_group_size == 2 and not DRY_RUN:
        if members:
            run_git("config", "--local", "handin.partner", members[0], check=False)
        else:
            run_git("config", "--local", "--unset", "handin.partner", check=False)
    return True


# ─── Preparing uv environments (2.17.0) ─────────────────────────────────────
#
# A check written `cd lab-b1 && uv run --frozen ...` runs in a fresh copy of
# the commit, with no .venv. Until 2.17.0 the first such check also had to
# install the packages, so a student without network (and without the packages
# in uv's cache) got "[FAIL]" for code that never ran, and a lockfile missing
# from the commit read as a failed doctest. Now `uv sync --frozen` runs once
# per folder BEFORE the checks, and a failure there makes the checks that need
# that folder "could not be checked" -- with the real reason -- instead of
# "failed". Only `uv run --frozen` commands are affected: a plain `uv run` may
# create its own lockfile, and `uv sync --frozen` would wrongly fail it.

_UV_FROZEN_COMMAND = re.compile(
    r"""^\s*(?:cd\s+(?P<dir>"[^"]+"|'[^']+'|[^\s&|;]+)\s*&&\s*)?uv\s+run\b(?P<rest>[^&|;]*)"""
)
# uv options that choose the project some other way than "the folder the
# command runs in". Such a command is left alone, exactly as before 2.17.0.
_UV_OWN_PROJECT_OPTION = re.compile(r"(^|\s)--(directory|project|no-project|script)(\s|=|$)")
# What uv says when it cannot READ a project file: the student's own file is
# broken (a merge conflict in uv.lock, a typo in pyproject.toml). That is a
# finding, not an environment problem, so the checks run and judge as before.
_UV_PARSE_ERROR = re.compile(r"parse error|failed to parse|invalid (toml|lock)|unknown field", re.IGNORECASE)

UV_READY = "ready"
UV_MISSING = "missing"  # a project file is not in the commit: nothing to sync
UV_FAILED = "failed"  # uv sync failed for another reason (network, cache, no uv)


def uv_frozen_dir(command: str) -> str | None:
    """The folder a `[cd <dir> &&] uv run --frozen ...` command runs in, else None."""
    match = _UV_FROZEN_COMMAND.match(command)
    if match is None:
        return None
    rest = match.group("rest")
    if not re.search(r"(^|\s)--frozen(\s|$)", rest) or _UV_OWN_PROJECT_OPTION.search(rest):
        return None
    folder = match.group("dir") or "."
    return folder.strip("\"'")


def worktree_state(worktree: Path | None, rel: str) -> str:
    """Why `rel` is missing from the commit: "absent", "uncommitted" or "ignored".

    "uncommitted" means the student's folder has it and git would take it with
    `git add`; "ignored" means it is there but a .gitignore keeps it out, so the
    plain `git add` advice would be refused. A folder counts as present when it
    holds a file git does not ignore, other than the placeholders.
    """
    if worktree is None:
        return "absent"
    local = worktree / rel
    if local.is_file():
        try:
            ignored = run_git("-C", str(worktree), "check-ignore", "-q", "--", rel, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ignored = False
        return "ignored" if ignored else "uncommitted"
    if local.is_dir():
        try:
            listed = run_git(
                "-C", str(worktree), "ls-files", "--cached", "--others", "--exclude-standard", "--", rel,
                check=False,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            listed = []
        if any(Path(name).name not in PLACEHOLDER_FILENAMES for name in listed if name.strip()):
            return "uncommitted"
        if any(x.is_file() and x.name not in PLACEHOLDER_FILENAMES for x in local.rglob("*")):
            return "ignored"
    return "absent"


def prepare_uv_environments(
    snapshot: Path, config: HandinConfig, folders: list[str], worktree: Path | None = None
) -> dict[str, str]:
    """Run `uv sync --frozen` once per uv project; return folder -> UV_* state.

    The project is the nearest folder, from the command's folder up to the
    snapshot root, that holds a pyproject.toml; with none there is nothing to
    sync and the check runs as before. Ctrl+C propagates; `_run_lint_command`
    has killed uv by then.
    """
    state: dict[str, str] = {}
    by_project: dict[Path, str] = {}
    root = snapshot.resolve()
    for folder in folders:
        if folder in state:
            continue
        target = (snapshot / folder).resolve()
        if not (target == root or root in target.parents) or not target.is_dir():
            state[folder] = UV_READY
            continue
        chain = [target, *[p for p in target.parents if p == root or root in p.parents]]
        project = next((p for p in chain if (p / "pyproject.toml").is_file()), None)

        def rel_of(path: Path, name: str) -> str:
            return name if path == root else f"{path.relative_to(root).as_posix()}/{name}"

        if project is None:
            # Not a uv project in the commit. Say so only if the student's own
            # folder has the file -- otherwise the command may not need one.
            why = worktree_state(worktree, rel_of(target, "pyproject.toml"))
            if why == "absent":
                state[folder] = UV_READY
                continue
            print(msg(config, f"uv_sync_file_{why}", path=rel_of(target, "pyproject.toml")))
            state[folder] = UV_MISSING
            continue
        if project in by_project:
            state[folder] = by_project[project]
            continue
        shown = "." if project == root else project.relative_to(root).as_posix()
        print(msg(config, "uv_sync_running", dir=shown))
        lock_chain = [project, *[p for p in project.parents if p == root or root in p.parents]]
        if not any((p / "uv.lock").is_file() for p in lock_chain):
            # `uv sync --frozen` would only say "Unable to find lockfile".
            # Name the file that is missing from the COMMIT, and say whether it
            # is sitting in the student's folder -- that is the fix.
            lock = rel_of(project, "uv.lock")
            print(msg(config, f"uv_sync_file_{worktree_state(worktree, lock)}", path=lock))
            state[folder] = by_project[project] = UV_MISSING
            continue
        log_event("lint", f"uv sync --frozen in {shown}")
        try:
            result = _run_lint_command("uv sync --frozen", project, LINT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log_event("lint", "uv sync: timeout")
            print(msg(config, "uv_sync_failed"))
            state[folder] = by_project[project] = UV_FAILED
            continue
        except OSError as exc:
            log_event("lint", f"uv sync: cannot run: {exc}")
            print(msg(config, "uv_sync_no_tool"))
            state[folder] = by_project[project] = UV_FAILED
            continue
        log_event("lint", f"uv sync exit {result.returncode}\n--- stderr ---\n{_clip_for_log(result.stderr)}")
        if result.returncode == 0:
            print(msg(config, "uv_sync_ok"))
            outcome = UV_READY
        elif _is_cannot_run_code(result.returncode):
            print(msg(config, "uv_sync_no_tool"))
            outcome = UV_FAILED
        elif _UV_PARSE_ERROR.search(result.stderr or ""):
            print(msg(config, "uv_sync_broken", dir=shown))
            _print_tail(config, result.stderr, LINT_STDERR_LINES)
            outcome = UV_READY  # the checks run and fail on it, as before 2.17.0
        else:
            print(msg(config, "uv_sync_failed"))
            if result.stderr.strip():
                _print_tail(config, result.stderr, LINT_STDERR_LINES)
            outcome = UV_FAILED
        state[folder] = by_project[project] = outcome
    return state


def step_run_lint_checks(
    repo_root: Path, config: HandinConfig, handin_type: HandinType, *, interactive: bool = True,
    worktree: Path | None = None,
) -> bool:
    """Run configured lint checks before submission.

    Executes each check command sequentially. Required checks that fail
    block the submission; optional checks show warnings but allow proceeding.

    Args:
        repo_root: Path to the repository root.
        config: Loaded configuration.
        handin_type: The selected hand-in type.

    Returns:
        True if all required checks passed (or none configured), False otherwise.
    """
    checks = handin_type.lint.checks
    if not checks:
        return True

    print(msg(config, "lint_header"))

    all_passed = True
    required_failed = False
    results: list[dict[str, str]] = []
    # Published now and filled in place, so an interrupted run still knows
    # which checks had already failed.
    SUBMISSION_PROGRESS["check_results"] = results

    commands = [expand_lint_command(check.command) for check in checks]
    uv_folders = [folder for folder in (uv_frozen_dir(c) for c in commands) if folder is not None]
    uv_state = prepare_uv_environments(repo_root, config, uv_folders, worktree) if uv_folders else {}

    for check, command in zip(checks, commands):
        print(msg(config, "lint_running", name=check.name))
        cannot_run = False
        result = None
        log_event("lint", f"{check.name}: {command}")
        folder = uv_frozen_dir(command)
        env_not_ready = folder is not None and uv_state.get(folder, UV_READY) != UV_READY
        try:
            result = _run_lint_command(command, repo_root, LINT_TIMEOUT_SECONDS)
            passed = result.returncode == 0
            log_event(
                "lint",
                f"exit {result.returncode}\n--- stdout ---\n{_clip_for_log(result.stdout)}"
                f"\n--- stderr ---\n{_clip_for_log(result.stderr)}",
            )
        except subprocess.TimeoutExpired as exc:
            log_event(
                "lint",
                f"timeout after {LINT_TIMEOUT_SECONDS} s"
                f"\n--- stdout ---\n{_clip_for_log(_as_text(exc.output))}"
                f"\n--- stderr ---\n{_clip_for_log(_as_text(exc.stderr))}",
            )
            print(msg(config, "lint_timeout"))
            passed = False
        except OSError as exc:
            print(msg(config, "lint_cannot_run", details=exc))
            passed = False
            cannot_run = True

        # AN ENVIRONMENT THAT CANNOT RUN THE CHECK SAYS NOTHING ABOUT THE CODE.
        # With shell=True a missing interpreter does NOT raise OSError -- the
        # shell runs fine and exits 127 ("command not found"). So `uv` absent
        # from PATH looked exactly like a failing test, and a failing optional
        # test ends in a question that DEFAULTS TO NOT HANDING IN. That is a
        # lockout at a deadline caused by the tool's own environment, which is
        # the one outcome every other check here is written to avoid. 126 is
        # the sibling case (found, not executable).
        # A code in `unavailable_exit_codes` comes from the CHECK ITSELF, which
        # ran and said why it could not judge the code (for example a file it
        # cannot parse with this Python). That reason is shown instead of
        # "the program is not available", which was false in that case.
        check_said_why = False
        if not passed and not cannot_run and result is not None and (
            _is_cannot_run_code(result.returncode)
            or result.returncode in getattr(check, "unavailable_exit_codes", [])
        ):
            cannot_run = True
            check_said_why = not _is_cannot_run_code(result.returncode)

        # The check still RAN, so a pass counts. A failure in an environment
        # that could not be prepared is most likely that same failure, and is
        # "could not be checked" rather than "[FAIL]" (2.17.0, see above).
        if not passed and env_not_ready:
            log_event("lint", f"{check.name}: failed in an unprepared environment ({folder})")
            print(msg(config, "lint_skipped_env", dir=folder))
            SUBMISSION_PROGRESS["check_unavailable"] = True
            results.append({"name": check.name, "status": "skipped"})
            continue

        if cannot_run:
            # Not appended to `results` and not counted: it is a note, not a
            # verdict, and it must not reach the confirmation below.
            if check_said_why and result is not None:
                print(msg(config, "lint_skipped_check_said", name=check.name))
                if result.stdout.strip():
                    _print_tail(config, result.stdout, LINT_STDOUT_LINES)
                if result.stderr.strip():
                    _print_tail(config, result.stderr, LINT_STDERR_LINES)
                print()
            else:
                print(msg(config, "lint_skipped_no_tool", name=check.name))
            SUBMISSION_PROGRESS["check_unavailable"] = True
            results.append({"name": check.name, "status": "skipped"})
            continue

        status = "passed" if passed else "advice" if getattr(check, "advisory", False) and not check.required else "failed"
        results.append({"name": check.name, "status": status})

        if passed:
            print(msg(config, "lint_passed"))
        else:
            # Advice keeps the tool's output below: "review this" is useless
            # without the file and line the tool pointed at.
            if status == "advice":
                print(msg(config, "lint_advice_line"))
            else:
                marker = msg(config, "lint_marker_required" if check.required else "lint_marker_optional")
                print(msg(config, "lint_failed_line", marker=marker))
            if result is not None:
                # The LAST lines, not the first: unittest, pytest and a bare
                # traceback all put the reason for a failure at the END. The
                # first ten lines were usually "...F.." and a header.
                if result.stdout.strip():
                    _print_tail(config, result.stdout, LINT_STDOUT_LINES)
                if result.stderr.strip():
                    _print_tail(config, result.stderr, LINT_STDERR_LINES)
            if not getattr(check, "advisory", False):
                all_passed = False
            if check.required:
                required_failed = True

    print()
    SUBMISSION_PROGRESS["check_results"] = results

    if required_failed:
        print(msg(config, "lint_required_failed"))
        return False

    if not all_passed:
        if not interactive:
            return False
        print(msg(config, "lint_optional_failed"))
        if not prompt_yes_no(msg(config, "lint_continue_anyway"), default_yes=False):
            return False

    return True


def step_supersede_issue(gl: Any, config: HandinConfig, handin_type: HandinType, old_iid: int, new_url: str) -> None:
    """Label an old issue as superseded and add a comment linking to the new one.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        handin_type: The selected hand-in type.
        old_iid: IID of the old issue to mark as superseded.
        new_url: URL of the new issue.
    """
    SUBMISSION_PROGRESS["supersedes"] = old_iid
    target_project_id = config.resolve_target_project(handin_type)

    label_stuck = False
    try:
        gl.get_project(target_project_id)
        old_issue = gl.get_issue(target_project_id, old_iid)

        current_labels = old_issue.get("labels") or []
        if "superseded" in current_labels:
            label_stuck = True
        else:
            updated = gl.update_issue(
                target_project_id, old_iid, {"labels": current_labels + ["superseded"]}
            )
            returned = updated.get("labels") if isinstance(updated, dict) else None
            label_stuck = isinstance(returned, list) and "superseded" in returned
    except ApiError as exc:
        # A real error (403, network), not the silent drop. Said once, and kept
        # out of the closing summary: the comment below is what the TA
        # reads, and it is still attempted.
        log_event("supersede", f"label update on #{old_iid} failed: {type(exc).__name__}: {exc}")
        print(msg(config, "supersede_failed", iid=old_iid, details=exc))
    if not label_stuck:
        log_event("supersede", f"label 'superseded' is not on #{old_iid}")

    try:
        gl.create_note(target_project_id, old_iid, f"Superseded by new submission: {new_url}")
    except ApiError as exc:
        warning = msg(config, "supersede_comment_failed", iid=old_iid, details=exc)
        print(warning)
        SUBMISSION_WARNINGS.append(warning)
        return

    print(msg(config, "supersede_marked" if label_stuck else "supersede_commented", iid=old_iid))


def _suggest_usernames(gl: Any, attempted: str, limit: int = 5) -> list[str]:
    """Look for GitLab usernames similar to one that does not exist.

    Used to turn "no such user" into "did you mean", which is what a student
    with a mistyped LiU-ID actually needs.

    Args:
        gl: Authenticated GitLab client.
        attempted: The username the student typed.
        limit: Maximum number of suggestions to return.

    Returns:
        A list of candidate usernames (possibly empty). Never raises.
    """
    # A LiU-ID is <letters><digits>; searching on the letter part finds the
    # plausible neighbours of a mistyped digit sequence.
    stem = re.match(r"^[A-Za-z]+", attempted)
    queries = [attempted]
    if stem and len(stem.group(0)) >= 3:
        queries.append(stem.group(0))

    candidates: list[str] = []
    for query in queries:
        try:
            for user in gl.list_users(search=query, per_page=50):
                # `.get()` and NOT `getattr()`: against a dict getattr always
                # returns the default, so every suggestion would be dropped —
                # and this loop sits inside `except Exception: continue`, so
                # the "did you mean" list would go permanently empty without a
                # single error message.
                name = user.get("username", "")
                if name and name != attempted and name not in candidates:
                    candidates.append(name)
        except Exception:
            # Suggestions are a courtesy. Never let them turn into a second
            # error on top of the one being reported.
            continue

    # Only genuinely similar names. A stem search happily returns dozens of
    # unrelated LiU-IDs, and "did you mean" is not a harmless suggestion here: if
    # the student picks a stranger, the bot later grants that stranger read
    # access to a confidential submission.
    return difflib.get_close_matches(attempted, candidates, n=limit, cutoff=0.8)


def step_collect_group_members(gl: Any, config: HandinConfig, handin_type: HandinType, *, suggested: str = "") -> list[str]:
    """Collect and validate group member usernames.

    Prompts the student for group member GitLab usernames, validates
    they exist on the GitLab instance, and returns the list.

    Args:
        gl: Authenticated GitLab client.
        config: Loaded configuration.
        handin_type: The selected hand-in type.

    Returns:
        List of validated GitLab usernames (may be empty).
    """
    max_members = handin_type.max_group_size - 1  # excluding the submitter
    if max_members <= 0:
        return []

    print(msg(config, "group_header"))
    print()
    print(
        msg(
            config,
            "group_intro",
            max_size=handin_type.max_group_size,
            max_additional=max_members,
        )
    )
    print()

    gitlab_url = config.course.gitlab_url
    members = []
    for i in range(max_members):
        # A mistyped username must not throw away a session in which the student
        # has already committed, pushed and run the checks. Ask again instead —
        # but never accept a name that could not be verified.
        attempts_left = MEMBER_NAME_ATTEMPTS
        username = ""
        while True:
            attempts_left -= 1
            try:
                if suggested:
                    username, suggested = suggested, ""
                else:
                    username = input(msg(config, "group_member_prompt", n=i + 1)).strip()
            except (EOFError, KeyboardInterrupt):
                # Same class again, and for the same reason both ways: reading
                # either as "no more partners" handed the lab in SOLO. The
                # partner is then not an assignee, cannot open the issue, and
                # is not on the submission the TA marks. An EMPTY LINE
                # still means "no more" -- that is a student who answered.
                print()
                raise

            if not username:
                break

            # Strip @ prefix if provided
            username = username.lstrip("@")

            # Validate that the user exists on GitLab. An unverifiable name is
            # never accepted: the partner's access is granted by being set as
            # assignee, and an assignee can only be a real account. This used to
            # be "Added anyway", which silently produced a submission the
            # partner could never open.
            try:
                users = gl.list_users(username=username)
            except Exception:
                print()
                print(msg(config, "partner_verify_failed", username=username))
                print()
                sys.exit(1)

            if users:
                user = users[0]
                canonical = str(user.get("username") or username)
                own_user = gl.user
                is_self = (
                    user.get("id") is not None and user.get("id") == own_user.get("id")
                ) or canonical.casefold() == str(own_user.get("username", "")).casefold()
                if not is_self and canonical.casefold() not in {m.casefold() for m in members}:
                    username = canonical
                    print(f"    [OK] {user['name']} (@{username})")
                    break
                print(msg(config, "partner_same_account"))
                if attempts_left <= 0:
                    print(msg(config, "partner_giving_up"))
                    sys.exit(1)
                continue

            print()
            print(msg(config, "partner_unknown_user", username=username, gitlab_url=gitlab_url))
            suggestions = _suggest_usernames(gl, username)
            if suggestions:
                print(msg(config, "partner_suggestions", suggestions=", ".join(suggestions)))
            print()

            if attempts_left <= 0:
                print(msg(config, "partner_giving_up"))
                print()
                sys.exit(1)
            print(msg(config, "partner_try_again", attempts=attempts_left))
            print()

        if not username:
            break
        members.append(username)

    if members:
        print(msg(config, "group_summary", members=", ".join(f"@{m}" for m in members)))
    print()
    return members


# ─── Main ───────────────────────────────────────────────────────────────────


# The banner's text field: at least this wide (the frame students have always
# seen), and never wider than what fits an 80-column terminal with the frame.
BANNER_MIN_WIDTH = 44
BANNER_MAX_WIDTH = 66


def _display_width(text: str) -> int:
    """Terminal columns `text` takes: combining marks 0, wide East Asian 2."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _wrap_to_width(text: str, limit: int) -> list[str]:
    """Greedy word wrap by display width; a word longer than `limit` is split."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        while _display_width(word) > limit:
            # Split an over-long word at the last character that still fits.
            cut = len(word)
            while cut > 1 and _display_width(word[:cut]) > limit:
                cut -= 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{current} {word}" if current else word
        if _display_width(candidate) <= limit:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_banner(texts: list[str]) -> list[str]:
    """The framed banner, wide enough for its text (2.17.0).

    Until 2.17.0 the field was a fixed 44 characters, and a course name of 50
    ("TDDE83 – Programmering i Python, fortsättningskurs") pushed the side of
    the frame out past its corners. The field now grows to fit, measured in
    terminal columns rather than characters, and wraps past BANNER_MAX_WIDTH.
    """
    rows: list[str] = []
    for text in texts:
        cleaned = " ".join(str(text or "").split())
        if cleaned:
            rows.extend(_wrap_to_width(cleaned, BANNER_MAX_WIDTH))
    inner = max([BANNER_MIN_WIDTH, *(_display_width(r) for r in rows)])
    border = "+" + "=" * (inner + 6) + "+"
    out = [border]
    for row in rows:
        gap = inner - _display_width(row)
        left = gap // 2
        out.append("|   " + " " * left + row + " " * (gap - left) + "   |")
    out.append(border)
    return out


def main() -> None:
    """Main entry point for the hand-in script."""
    # Parse arguments
    # Set before anything that can be interrupted, argument parsing included:
    # Ctrl+C anywhere in a --check run must not end in "cancelled, exit 0".
    # Read from argv first, then from what argparse actually accepted.
    SUBMISSION_PROGRESS["check_mode"] = "--check" in sys.argv[1:]
    args = parse_args()
    SUBMISSION_PROGRESS["check_mode"] = bool(getattr(args, "check", False))
    SUBMISSION_PROGRESS.pop("check_results", None)
    SUBMISSION_PROGRESS.pop("missing_paths", None)
    SUBMISSION_PROGRESS.pop("snapshot_failed", None)
    dry_run = args.dry_run
    global DRY_RUN
    DRY_RUN = bool(dry_run)
    global DEBUG
    DEBUG = bool(getattr(args, "debug", False))

    # --config is made absolute BEFORE any change of directory below, so a
    # relative path means what the student meant by it.
    config_arg = getattr(args, "config", None)
    if config_arg is not None:
        config_arg = str(Path(config_arg).expanduser().resolve())
    moved_to = enter_script_repo_if_outside()

    # Load configuration
    set_step("config")
    config_path = find_config_file(config_arg)
    log_event("config", f"file: {config_path}")
    config = load_config(config_path)
    global MESSAGE_LANGUAGE
    MESSAGE_LANGUAGE = config.course.language
    if moved_to is not None:
        print(msg(config, "moved_to_script_repo", path=moved_to))
        print()

    local_only_ids: list[str] = []
    if not args.no_config_update and not getattr(args, "check", False):
        update = apply_config_update(config, script_version=SCRIPT_VERSION)
        config = update.config
        MESSAGE_LANGUAGE = config.course.language
        local_only_ids = update.local_only_ids
        try:
            described = describe_config_update(update, config.course.language)
        except TypeError:
            # A handin_config.py older than 2.17.0 takes no language.
            described = describe_config_update(update)
        for line in described:
            print(line)
            log_event("config", line)

    # Judge the hand-in id (if one was given) against the configuration, before
    # git and authentication: a typo must never be discovered after the student
    # has pushed and tagged.
    lab_id = args.lab_id.strip() if args.lab_id is not None else None
    preselected = resolve_handin_type(config, lab_id) if lab_id is not None else None

    check_script_version(config, SCRIPT_VERSION)

    if getattr(args, "check", False):
        if preselected is None or not preselected.is_code:
            print(msg(config, "check_select_lab"))
            print(format_handin_ids(config))
            sys.exit(2)
        repo_root = get_repo_root(config)
        os.chdir(repo_root)
        require_commits(config)
        sha = get_current_commit()
        SUBMISSION_PROGRESS["check_mode"] = True
        passed = run_committed_checks(repo_root, config, preselected, sha, interactive=False)
        # Last, so it is on the screen the student reads: a green result for a
        # commit that is not what is in the folder is a green result for old
        # code (2.17.0). A note, not a verdict -- the exit code is unchanged.
        changed = count_local_changes()
        if changed:
            print(msg(config, "check_uncommitted_note", count=changed, commit=sha[:8]))
            print()
        sys.exit(check_exit_code(passed))

    print()
    for line in render_banner([config.course.display_name, msg(config, "banner_subtitle")]):
        print(line)
    print()

    if dry_run:
        print(msg(config, "dry_run_banner"))
        print()

    # Step 0: Startup checks
    set_step("startup")
    repo_root, repo_name, gl = step0_startup_checks(config)
    if dry_run:
        # The layer underneath the step-by-step checks: from here on the client
        # sends nothing but GET (see DRY_RUN). An older handin_api.py ignores
        # the attribute, and then the step checks are the only layer.
        gl.read_only = True
    if getattr(args, "status", False):
        show_handin_status(gl, config, preselected)
        return
    step_template_hint(gl, config, repo_root)
    RUN_LOG.refuse_if_inside(repo_root)
    log_event("repo", f"root {repo_root}, name {repo_name}")

    # Step 3: Select hand-in type (before git steps so we know the workflow)
    set_step("select-handin")
    handin_type = step3_select_handin_type(config, preselected, local_only_ids)

    # Step 0b: Verify the hand-in chain (account -> membership -> repo -> hand-in).
    # Runs after the type is known, because the target project depends on it, but
    # before any git work, so a broken setup is reported before the student has
    # committed, pushed and tagged for nothing.
    set_step("check-chain")
    step0b_check_handin_chain(gl, config, handin_type)

    # Show deadline info early
    deadlines = load_deadlines(config)
    display_deadlines(config, deadlines, handin_type)

    report_link: str = ""
    ai_review_markdown: str = ""
    # Chosen in the report branch below, uploaded only after the confirmation.
    report_file: Path | None = None

    if handin_type.is_code:
        # ─── Code submission pipeline ───────────────────────────────────


        # A repository with no commits at all is caught HERE, before step 1:
        # there is no "latest commit" to hand in without the uncommitted
        # changes, and the two git calls further down (`rev-parse
        # --abbrev-ref HEAD` in step 2, `rev-parse HEAD` in step 8) used to
        # raise CalledProcessError at the student. Only the code pipeline is
        # gated: a report hand-in creates no tag and needs no commit.
        set_step("commits")
        require_commits(config)
        submission_commit = get_current_commit()

        # Before step 1, for the same reason `require_commits` is: step 1 asks
        # whether to hand in the latest commit, and that question presumes
        # there is something to hand in. Cheap and local -- no network, no
        # GitLab -- so it costs nothing to ask first.

        # Step 1: Check for uncommitted changes
        if handin_type.code.check_uncommitted:
            set_step("uncommitted")
            step1_check_uncommitted(config)

        # Step 2: Push committed changes
        if handin_type.code.require_push:
            set_step("push")
            step2_push_changes(config, handin_type.code.check_analysis)

        # Step 2b: Check for code analysis on current commit
        if handin_type.code.check_analysis:
            step2b_check_analysis_issue(gl, config)

        # Lint checks (if configured)
        if handin_type.lint.checks or handin_type.required_paths:
            set_step("lab-tests")
            lint_ok = run_committed_checks(repo_root, config, handin_type, submission_commit)
            if not lint_ok:
                print(msg(config, "lint_checks_failed_exit"))
                sys.exit(1)

    elif handin_type.is_report:
        # ─── Report submission pipeline ─────────────────────────────────

        report_file = step_select_report_file(config, handin_type)
        if report_file is None:
            print(msg(config, "report_none_selected"))
            sys.exit(0)


        # AI pre-review (for prereview type)
        ai_review_markdown = ""
        if handin_type.ai_review.enabled:
            ai_review_markdown = step_run_ai_review(config, handin_type, report_file)
            if ai_review_markdown and handin_type.ai_review.block_on_errors:
                print(msg(config, "ai_review_blocking"))
                sys.exit(1)

    # Group members (if group submission)
    group_members: list[str] = []
    if handin_type.group_submission:
        set_step("group-members")
        saved = run_git("config", "--local", "--get", "handin.partner", check=False).stdout.strip()
        suggested = saved if saved and prompt_yes_no(msg(config, "partner_reuse", username=saved), default_yes=True) else ""
        group_members = step_collect_group_members(gl, config, handin_type, suggested=suggested)
        if not confirm_participants(gl, config, handin_type, group_members):
            print(msg(config, "submission_cancelled"))
            return

    # Step 4: Check for existing issues
    set_step("existing-hand-ins")
    supersedes = step4_check_existing_issues(gl, config, handin_type)

    # Step 5: Run validation script (code handins only)
    if handin_type.is_code:
        step5_run_validation(repo_root, config, handin_type, supersedes)

    # Step 6: Pre-submission checklist
    set_step("checklist")
    checklist_answers = step6_checklist(config, handin_type)

    # Step 6b: Confirmation about the course rules for aids (one varying word).
    #
    # Placed AFTER the checklist on purpose. The checklist is a run of yes/no
    # questions, and the ruling's whole point is that this one must not become
    # the last item in that run: it is a separate block, with a heading of its
    # own, answered by typing rather than by pressing Enter.
    #
    # It is asked BEFORE the dry-run summary returns, so that a teacher testing
    # the tool with --dry-run sees exactly what a student will meet.
    set_step("confirmation")
    confirmation = step6b_confirmation(config)
    if confirmation.unclear:
        sys.exit(1)

    if report_file is not None and dry_run:
        print(msg(config, "dry_report_not_uploaded", name=report_file.name))
        print()
    elif report_file is not None:
        report_link = step_upload_report(gl, config, handin_type, report_file)

    # Step 7: Optional message to TA
    message = step7_optional_message(config, repo_name, handin_type, checklist_answers, supersedes)

    if dry_run:
        print(msg(config, "dry_header"))
        print(msg(config, "dry_type", name=handin_type.name, id=handin_type.id))
        print(msg(config, "dry_repo", repo=repo_name))
        print(msg(config, "dry_target", target=config.resolve_target_project(handin_type)))
        print_dry_labels(config, handin_type)
        print(msg(config, "dry_confidential", value=yes_no_text(config, config.resolve_confidential(handin_type))))
        language = config.course.language
        print(msg(config, "dry_language", language=msg(config, f"language_name_{language}") or language))
        if handin_type.group_submission:
            policy = config.defaults.partner_assignee
            print(msg(config, "dry_partner_policy", policy=msg(config, f"partner_policy_{policy}") or policy))
        print_dry_bot_active(config)
        if confirmation.asked:
            state = msg(config, "dry_confirm_yes" if confirmation.confirmed
                        else "dry_confirm_no")
            print(msg(config, "dry_confirm", state=state, word=confirmation.word))
        else:
            print(msg(config, "dry_confirm_skipped"))
        if group_members:
            print(msg(config, "dry_group", members=", ".join(group_members)))
        if supersedes is not None:
            print(msg(config, "dry_supersedes", iid=supersedes))
        if message:
            print(msg(config, "dry_message", message=message))
        print()
        print(msg(config, "dry_no_issue"))
        print()
        return

    kind = handin_kind(supersedes)
    if handin_type.is_code:
        set_step("tag")
        tag_name, commit_hash = step8_create_tag(
            config, repo_name, annotation=tag_annotation(handin_type, kind, supersedes),
            commit_hash=submission_commit,
        )
    else:
        tag_name = ""
        commit_hash = ""

    # Step 9: Create the issue
    set_step("create-issue")
    issue_url = step9_create_issue(
        gl,
        config,
        repo_name,
        handin_type,
        tag_name,
        commit_hash,
        checklist_answers,
        message,
        supersedes,
        report_link=report_link,
        ai_review_markdown=ai_review_markdown,
        group_members=group_members,
        confirmation=confirmation,
        kind=kind,
        check_results=SUBMISSION_PROGRESS.get("check_results"),
        missing_paths=SUBMISSION_PROGRESS.get("missing_paths"),
        snapshot_failed=bool(SUBMISSION_PROGRESS.get("snapshot_failed")),
    )

    # Mark old issue as superseded
    if supersedes is not None:
        set_step("supersede")
        step_supersede_issue(gl, config, handin_type, supersedes, issue_url)

    # Success
    set_step("done")
    log_event("done", f"issue {issue_url}")
    delete_draft(repo_name)

    print(msg(config, "issue_created", url=issue_url))
    print()
    print(msg(config, "submitted_ok"))
    print(msg(config, "receipt", lab=handin_type.id,
              members=", ".join(["@" + (getattr(gl, "user", {}) or {}).get("username", "?"), *("@" + m for m in group_members)]),
              commit=commit_hash or "-", url=issue_url))
    for result in SUBMISSION_PROGRESS.get("check_results", []):
        verdict = msg(config, "receipt_check_" + result["status"])
        print(f"  {result['name']}: {verdict}")
    print()

    # Repeated at the end, where the student is actually looking. See
    # SUBMISSION_WARNINGS.
    if SUBMISSION_WARNINGS:
        print(msg(config, "summary_warnings_header"))
        for warning in SUBMISSION_WARNINGS:
            print(warning)
        print()
    print_bot_delay(config, group_members)


def print_dry_labels(config: HandinConfig, handin_type: Any) -> None:
    """The dry run's label line -- left out with labels_set_later, since the
    student is then told nothing about labels at all (2.12.1)."""
    if not config.defaults.labels_set_later:
        labels = config.resolve_labels(handin_type)
        shown = ", ".join(str(x) for x in labels) if isinstance(labels, (list, tuple)) else str(labels)
        print(msg(config, "dry_labels", labels=shown or "-"))


def print_dry_bot_active(config: HandinConfig) -> None:
    key = "dry_bot_active_access" if config.defaults.labels_set_later else "dry_bot_active"
    print(msg(config, key, value=yes_no_text(config, config.defaults.bot_active)))


def yes_no_text(config: HandinConfig | None, value: Any) -> str:
    """A truth value as the word a student reads ("ja"/"nej"), never `True`."""
    return msg(config, "value_yes" if value else "value_no")


def print_bot_delay(config: HandinConfig, group_members: list[str]) -> None:
    """Say what is completed after the hand-in (see the comments inside)."""
    # The student is looking at an issue with no labels and no assignees. Say
    # that this is expected and completed automatically — otherwise the partner
    # mails "I cannot see the submission" and the student hands in again.
    #
    # Gated on bot_active: while the bot is not deployed, nothing completes the
    # issue, and promising that it does would be promising automation that does
    # not exist. Gated on group_members for the partner clause, so a solo
    # submitter is not told about a partner they do not have.
    #
    # With labels_set_later the student is told nothing about labels at all:
    # a group hears only about the partner's access, and a solo submitter,
    # for whom the paragraph was ONLY about labels, hears nothing.
    if group_members and config.defaults.partner_assignee != "off" and not config.defaults.bot_active:
        print(msg(config, "partner_access_unverified"))
    if config.defaults.bot_active:
        partner_clause = bool(group_members) and config.defaults.partner_assignee != "off"
        if config.defaults.labels_set_later:
            if partner_clause:
                print(msg(config, "bot_delay_partner_only"))
        elif partner_clause:
            print(msg(config, "bot_delay"))
        else:
            print(msg(config, "bot_delay_solo"))


# At most this many files are named in the hint; the script lists them all.
TEMPLATE_HINT_MAX_FILES = 5
# The hint's own budget. Every student asks about the SAME template project,
# often in the same hour before a deadline, and a missed hint costs nothing:
# one short try, no retries, and a rate limit ends the check at once instead of
# waiting (the client's defaults could add minutes in front of a hand-in).
TEMPLATE_HINT_TIMEOUT = 5.0
TEMPLATE_HINT_MAX_PAGES = 3


def template_files_not_fetched(gl: Any, project: str, repo_root: Path) -> list[str]:
    """Template files whose current version the student has never had.

    "Never had" is measured as: the blob is not in the local object store at
    all. A fork has every blob of the template up to the fork, and
    `uppdatera.py` fetches the template itself, so after any run of it every
    template blob exists locally -- also for a file the student chose to keep
    their own version of. A missing blob is therefore exactly a course version
    that has not been fetched. One API listing and one git process.

    Raises ApiError or OSError/subprocess errors; the caller treats every
    failure as "no hint".
    """
    entries: list[dict[str, Any]] = []
    page = 1
    budget = (gl.timeout, gl.retries, gl.rate_limit_retries)
    gl.timeout, gl.retries, gl.rate_limit_retries = TEMPLATE_HINT_TIMEOUT, 1, 1
    try:
        # Not get_paged(): it deduplicates on `id`, and in a tree two identical
        # files share a blob id -- the X-Total check would then print a false
        # "may be incomplete" warning to a student who asked for none of this.
        for _ in range(TEMPLATE_HINT_MAX_PAGES):
            headers, data = gl.get_with_headers(
                f"projects/{encode_path(project)}/repository/tree",
                {"recursive": True, "per_page": 100, "page": page},
            )
            if not isinstance(data, list):
                raise ApiTransport("repository tree was not a list", None, "")
            entries.extend(item for item in data if isinstance(item, dict))
            nxt = (headers.get("x-next-page") or "").strip()
            if not nxt.isdigit():
                break
            page = int(nxt)
        else:
            log_event("template", f"stopped after {TEMPLATE_HINT_MAX_PAGES} pages")
    finally:
        gl.timeout, gl.retries, gl.rate_limit_retries = budget
    blobs = [
        (str(item.get("path", "")), str(item.get("id", "")))
        for item in entries
        if item.get("type") == "blob" and item.get("id") and item.get("path")
        and not str(item.get("path")).endswith(".gitkeep")
    ]
    if not blobs:
        return []
    result = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=repo_root,
        input="".join(f"{blob}\n" for _, blob in blobs),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "git cat-file failed")
    answers = result.stdout.splitlines()
    if len(answers) != len(blobs):
        raise OSError("git cat-file answered a different number of lines")
    return sorted(path for (path, _), answer in zip(blobs, answers) if answer.endswith(" missing"))


def step_template_hint(gl: Any, config: HandinConfig, repo_root: Path) -> None:
    """Say so, once, if the course has files the student has not fetched."""
    project = config.defaults.template_project
    if not project:
        return
    try:
        missing = template_files_not_fetched(gl, project, repo_root)
    except Exception as exc:  # noqa: BLE001 - a hint must never cost a hand-in
        log_event("template", f"check failed: {type(exc).__name__}: {exc}")
        return
    log_event("template", f"not fetched: {missing}")
    if not missing:
        return
    shown = missing[:TEMPLATE_HINT_MAX_FILES]
    files = "\n".join(f"    {path}" for path in shown)
    if len(missing) > len(shown):
        files += "\n    ..."
    python = "python" if os.name == "nt" else "python3"
    print()
    print(msg(config, "template_hint", files=files, python=python))
    print()


def _describe_system() -> str:
    try:
        return f"{platform.system()} {platform.release()}".strip() or sys.platform
    except Exception:
        return sys.platform


def start_run_log() -> None:
    """Open the run log and write what every report of "it does not work" lacks."""
    RUN_LOG.open()
    log_event("start", f"handin.py {SCRIPT_VERSION}")
    log_event("start", f"argv: {sys.argv[1:]}")
    log_event("start", f"python {sys.version.split()[0]} ({sys.executable}) on {_describe_system()}")
    log_event(
        "start",
        f"stdout encoding {getattr(sys.stdout, 'encoding', '?')}, "
        f"stdin is a terminal: {_isatty(sys.stdin)}",
    )
    log_event("start", f"cwd {ORIGINAL_CWD}")
    try:
        version = subprocess.run(
            ["git", "--version"], capture_output=True, encoding="utf-8", errors="replace", check=False
        ).stdout.strip()
    except OSError as exc:
        version = f"not runnable: {exc}"
    log_event("start", f"git: {version}")
    # Logged when the first HTTPS request builds the context, not here: a run
    # that never goes online must not start `security` on macOS.
    try:
        import handin_api

        handin_api.TLS_LOG_HOOK = lambda source: log_event("tls", f"root certificates: {source}")
    except Exception:
        pass


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def finish_run_log(code: Any) -> None:
    """Record how the run ended and, if it ended in an error, say where the log is.

    Never changes the exit code; the caller re-raises. A cancel (exit 0) and a
    successful run stay silent -- the pointer is for runs that went wrong.
    """
    log_event("exit", f"code {code!r} in step {CURRENT_STEP}")
    RUN_LOG.close()
    failed = code not in (0, None)
    if failed and RUN_LOG.exists:
        try:
            print()
            print(msg(None, "log_saved", path=RUN_LOG.path))
        except Exception:
            pass


def interrupted_outcome() -> tuple[str, dict[str, Any], int]:
    """What a Ctrl+C means, given how far the run got: message key, fields, exit code.

    Exit 0 only when nothing needs doing: nothing was handed in and nothing
    was left half-done, or the hand-in is registered and confirmed. Also used
    by the unexpected-error handler, which ignores the exit code (always 1).
    """
    progress = SUBMISSION_PROGRESS
    url = progress.get("issue_url")
    if url and progress.get("confirmed"):
        if CURRENT_STEP == "supersede" and progress.get("supersedes") is not None:
            return "interrupted_supersede", {"url": url, "iid": progress["supersedes"]}, 0
        return "interrupted_after_issue", {"url": url}, 0
    if url:
        return "interrupted_issue_unconfirmed", {"url": url}, 1
    if progress.get("post_sent"):
        return "interrupted_issue_uncertain", {"issues_url": progress.get("issues_url", "")}, 1
    if progress.get("tag"):
        return "interrupted_after_tag", {"tag": progress["tag"]}, 1
    if CURRENT_STEP == "tag":
        return "interrupted_before_issue", {}, 1
    if progress.get("check_mode"):
        # An interrupted --check judged nothing completely: never 0. A finding
        # already made still wins, as in check_exit_code.
        failed = any(r.get("status") == "failed" for r in progress.get("check_results") or [])
        return "check_interrupted", {}, 1 if failed else 2
    return "menu_cancelled", {}, 0


def _run_main() -> None:
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C är den vanligaste avbrottsvägen och nås utan `config` i scope.
        # MESSAGE_LANGUAGE bär språket sedan konfigurationen lästes; faller den tillbaka
        # på "en" är det för att avbrottet kom innan filen ens gick att läsa.
        log_event("exit", f"KeyboardInterrupt in step {CURRENT_STEP}")
        key, fields, code = interrupted_outcome()
        print()
        print(msg(None, key, **fields))
        sys.exit(code)
    except EOFError:
        # EOF måste stå FÖRE `except Exception`, som annars svalde den och
        # kallade den ett oväntat fel. Ctrl+D är inte ett avbrott studenten
        # valde på samma sätt som Ctrl+C, utan en fråga som inte gick att
        # ställa -- därför en egen text, och därför exitkod 1: ingenting
        # lämnades in, och ett skript som kör verktyget utan terminal ska se
        # det på exitkoden.
        log_event("exit", "EOFError: input ended")
        print()
        print(msg(None, "input_ended"))
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        # Type, step and versions, not only `str(exc)` (2.11.0). A KeyError
        # used to print as the bare key -- "'username'" -- which is all a
        # TA ever got to see.
        log_event("crash", traceback.format_exc())
        print()
        print(msg(None, "fatal_unexpected"))
        print(f"  {type(exc).__name__}: {exc}")
        print(
            msg(
                None,
                "fatal_where",
                step=CURRENT_STEP,
                version=SCRIPT_VERSION,
                python=sys.version.split()[0],
                system=_describe_system(),
            )
        )
        if DEBUG:
            print()
            traceback.print_exc()
        else:
            print(msg(None, "fatal_debug_hint"))
        print()
        print(msg(None, "fatal_contact"))
        if SUBMISSION_PROGRESS.get("post_sent") or SUBMISSION_PROGRESS.get("tag"):
            key, fields, _code = interrupted_outcome()
            print()
            print(msg(None, key, **fields))
        sys.exit(1)


if __name__ == "__main__":
    start_run_log()
    try:
        _run_main()
    except SystemExit as _exit:
        finish_run_log(_exit.code)
        raise
    except KeyboardInterrupt:
        # A SECOND Ctrl+C, while the handler above was printing. Close the log
        # and leave quietly instead of with a traceback.
        finish_run_log(130)
        sys.exit(130)
    finish_run_log(0)
