"""Configuration loader for the hand-in submission tool.

Loads and validates handin.toml, providing typed access to course
configuration, hand-in type definitions, deadlines, and per-type overrides.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, time as time_of_day, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse
from urllib.request import Request

from handin_api import ApiTransport, _NoRedirect, https_opener


DEFAULT_CONFIRMATION_WORDS: dict[str, list[str]] = {
    "sv": [
        "lampa", "korg", "penna", "stol", "bord", "cykel", "nyckel", "klocka",
        "spegel", "hylla", "matta", "kudde", "tallrik", "gaffel", "sked",
        "mugg", "kruka", "stege", "sadel", "brygga",
    ],
    "en": [
        "lamp", "basket", "pencil", "chair", "table", "bicycle", "key", "clock",
        "mirror", "shelf", "carpet", "pillow", "plate", "fork", "spoon",
        "mug", "pot", "ladder", "saddle", "bridge",
    ],
}


DECLINE_WORDS: dict[str, str] = {
    "sv": "nej",
    "en": "no",
}


def normalise_confirmation_answer(raw: str) -> str:
    """Strip what a student can reasonably type around a one-word answer.

    The word is shown in the terminal and typed back. Course texts and chat
    clients write words in backticks or quotes, and a student who copies the
    word out of the guide brings the punctuation with it. None of that is a
    different answer, so none of it should cost an attempt.

    Lives here rather than in handin.py because `_parse_confirmation` has to
    apply the very same normalisation when it checks a configured word list --
    a guard that normalises differently from the runtime is not a guard.
    """
    return raw.strip().strip("`'\"" + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019)).strip()


def is_decline_answer(raw: str) -> bool:
    """True when the student clearly answered "no, I did not follow the rules".

    Exact match after normalisation, in either language. Never a substring
    match: a description that merely contains the word "no" is not an answer to
    this question, and a hand-in must not turn on a coincidence.
    """
    answer = normalise_confirmation_answer(raw).casefold()
    return any(answer == form.casefold() for form in DECLINE_WORDS.values())


# ─── Configuration dataclasses ──────────────────────────────────────────────


def _default_expected_origin_paths() -> list[str]:
    """Return an empty pattern list (a NEW one per call; see _default_report_formats)."""
    return []


@dataclass
class CourseConfig:
    """Top-level course identity."""

    name: str
    display_name: str
    gitlab_url: str
    language: str = "en"  # student-facing message language: "en" or "sv"
    # What `origin` is expected to look like, as glob patterns over the GitLab
    # project path (`namespace/name`, no host, no trailing .git). `{username}`
    # in a pattern is replaced by the logged-in GitLab username before
    # matching, so a course whose repositories are forked per student can write
    # "{username}/tdde82-student-base" and a course that forks per pair can
    # write "*/tdde83-student-base".
    #
    # EMPTY IS THE DEFAULT AND MEANS "DO NOT CHECK". A mismatch is a WARNING
    # with a confirmation, never a refusal -- see `check_origin_matches_course`
    # in handin.py for why.
    expected_origin_paths: list[str] = field(default_factory=_default_expected_origin_paths)
    upstream_project_id: int = 0


@dataclass
class AuthConfig:
    """Authentication settings."""

    oauth_client_id: str | None = None
    token_cache_dir: str = "~/.config/handin"

    @property
    def token_cache_path(self) -> Path:
        return Path(self.token_cache_dir).expanduser()

    @property
    def oauth_token_file(self) -> Path:
        return self.token_cache_path / "oauth_token.json"


@dataclass
class LintCheck:
    """A single pre-submission linting check."""

    name: str
    command: str
    required: bool = False
    advisory: bool = False
    unavailable_exit_codes: list[int] = field(default_factory=list)


@dataclass
class CodeConfig:
    """Code-submission-specific settings."""

    check_uncommitted: bool = True
    require_push: bool = True
    check_analysis: bool = True
    validation_script: str = "bin/validate-handin"


def _default_report_formats() -> list[str]:
    """Return the default list of accepted report formats.

    A NEW list must be returned on each call, which is why this is a function
    and not a shared constant: a mutable default would let one course's edit
    reach another course's configuration.
    """
    return ["pdf"]


@dataclass
class ReportConfig:
    """Report-submission-specific settings."""

    allowed_formats: list[str] = field(default_factory=_default_report_formats)
    max_size_mb: int = 20
    min_pages: int = 0
    max_pages: int = 0
    file_prompt: str = "Select your report file"


@dataclass
class AIReviewConfig:
    """AI pre-review settings."""

    enabled: bool = False
    command: str = ""
    include_in_issue: bool = True
    block_on_errors: bool = False


@dataclass
class ChecklistConfig:
    """Pre-submission checklist.

    ``items`` is the question text, in order, exactly as before 2.14.0, so a
    caller that only reads the questions sees no difference.

    ``stop_on_no`` (2.14.0) runs PARALLEL to ``items``: ``stop_on_no[i]`` is
    True when question ``i`` is a showstopper -- a "no" stops the hand-in
    before anything is created. It is a separate list rather than a new item
    type so that every all-string configuration, and every caller that builds
    a ``ChecklistConfig(items=[...])`` by hand, behaves exactly as it did. A
    list SHORTER than ``items`` means "the rest are informational": see
    ``is_showstopper``.
    """

    items: list[str] = field(default_factory=list)
    stop_on_no: list[bool] = field(default_factory=list)

    def is_showstopper(self, index: int) -> bool:
        """True when a "no" to question ``index`` must stop the hand-in."""
        return 0 <= index < len(self.stop_on_no) and self.stop_on_no[index] is True


@dataclass
class ConfirmationConfig:
    """The confirmation about the course rules for aids, asked at every hand-in.

      (a) WHEN — at every hand-in, so this is asked on every run, never once.
      (c) FORM — ONE short question, answered by typing a word the question
          shows, so that a blank Enter is not an answer. `words` is the pool the
          word is drawn from; an empty pool means the built-in list for the
          course language.
      (d) CONSEQUENCE — NO NEW PENALTY. Neither of the two CLEAR answers costs
          the student anything: a yes hands in, and a no hands in too, carrying
          the student's own description of what they did.

        "Inget ska lämnas in om man inte svarar tydligt. Då kan man få 'ej
         bekräftat' i inlämningen som *tolkas* som att man inte hade följt
         reglerna, bara för att man råkade trycka enter eller stänga
         terminalen. Ett 'nej' ska alltid vara tydligt."

    An unclear answer is therefore never recorded as a refusal -- nothing is
    created at all, and the student is told to run the script again. There is
    no setting for this, because a course that chose "record" would be choosing
    to publish that accusation.

    (b) CHANNEL is not configurable: the answer travels in the submission issue,
    where the code is. There is no separate form and no second channel.

    The question WORDING is not configurable either. It is student-facing text
    with a disciplinary purpose, it must match the course guide word for word,
    and a course that could retype it could weaken it by accident. It lives in
    MESSAGES in handin.py, in both languages.

    Disabled by default: a course that has not decided to ask must not start
    asking because it upgraded the script.
    """

    enabled: bool = False

    # Link shown with the question, so the student can read the rules they are
    # confirming. Empty means the question is shown without a link.
    rules_url: str = ""

    # The pool the word is drawn from. Empty = the built-in list for the
    # course's language (see DEFAULT_CONFIRMATION_WORDS).
    words: list[str] = field(default_factory=list)

    # How many times the student may answer before the tool gives up and treats
    # the answer as unclear. A typo must not cost a hand-in, and asking forever
    # is not an option in a non-interactive terminal. The same budget is used
    # for the follow-up description on the "no" path.
    attempts: int = 3


@dataclass
class LintConfig:
    """Pre-submission linting configuration."""

    checks: list[LintCheck] = field(default_factory=list)


def _default_required_paths() -> list[str]:
    """Return an empty path list (a NEW one per call; see _default_report_formats)."""
    return []


@dataclass
class HandinType:
    """A single hand-in type definition with all resolved settings."""

    id: str
    name: str
    type: str  # "code", "report-prereview", "report-final"
    labels: list[str] = field(default_factory=list)
    # Folders (relative to the repository root) that must exist and contain
    # something for this hand-in to be worth submitting. A local check with no
    # network in it, against the one mistake that is invisible to the student:
    # handing in an empty assignment and believing it worked.
    #
    # EMPTY IS THE DEFAULT AND MEANS "DO NOT CHECK". A miss is a WARNING with a
    # confirmation, never a refusal -- a course could name the folder wrongly,
    # and a hand-in that is legitimately empty (a resubmission of work that
    # lives elsewhere) must still be possible.
    required_paths: list[str] = field(default_factory=_default_required_paths)
    target_project_id: int | None = None
    check_existing: bool | None = None
    confidential: bool | None = None
    group_submission: bool = False
    max_group_size: int = 2

    code: CodeConfig = field(default_factory=CodeConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    ai_review: AIReviewConfig = field(default_factory=AIReviewConfig)
    checklist: ChecklistConfig = field(default_factory=ChecklistConfig)

    @property
    def is_code(self) -> bool:
        return self.type == "code"

    @property
    def is_report(self) -> bool:
        return self.type in ("report-prereview", "report-final")

    @property
    def is_prereview(self) -> bool:
        return self.type == "report-prereview"


@dataclass
class Deadline:
    """A single deadline entry."""

    date: str
    handins: list[str]
    message: str = ""


@dataclass
class DefaultsConfig:
    """Default settings that can be overridden per handin type."""

    target_project_id: int = 0
    confidential: bool = True
    check_existing: bool = True
    labels: list[str] = field(default_factory=list)
    config_update_url: str = ""
    deadlines_url: str = ""
    min_script_version: str = ""

    partner_assignee: str = "bot"

    # Is the server-side bot actually deployed and running? Governs whether the
    # student is told that labels and partner access are completed automatically.
    # Default False: never promise automation that does not exist. A dead bot
    # produces no error anywhere, so this must be set deliberately, together
    # with a liveness alarm.
    bot_active: bool = False

    labels_set_later: bool = False
    # The template repository the student's fork was made from, as
    # "namespace/path" (2.13.0). When set, the tool compares the template's
    # files with what the student has and, if the course has added or changed
    # files the student has never fetched, prints one hint to run the course's
    # `uppdatera.py`. Never a question, never a stop. Empty = off.
    #
    # Local-only, like config_update_url: it names a place the tool reads from,
    # and the central configuration must not be able to point it elsewhere.
    template_project: str = ""
    code: CodeConfig = field(default_factory=CodeConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    lint: LintConfig = field(default_factory=LintConfig)


@dataclass
class HandinConfig:
    """Complete loaded configuration."""

    course: CourseConfig
    auth: AuthConfig
    defaults: DefaultsConfig
    handins: list[HandinType]
    deadlines: list[Deadline]
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)

    def get_handin(self, handin_id: str) -> HandinType | None:
        """Look up a hand-in type by ID."""
        for ht in self.handins:
            if ht.id == handin_id:
                return ht
        return None

    def resolve_target_project(self, handin_type: HandinType) -> int:
        """Get the effective target project ID for a hand-in type."""
        if handin_type.target_project_id is not None:
            return handin_type.target_project_id
        return self.defaults.target_project_id

    def resolve_confidential(self, handin_type: HandinType) -> bool:
        """Get the effective confidential flag for a hand-in type."""
        if handin_type.confidential is not None:
            return handin_type.confidential
        return self.defaults.confidential

    def resolve_check_existing(self, handin_type: HandinType) -> bool:
        """Get the effective check_existing flag for a hand-in type."""
        if handin_type.check_existing is not None:
            return handin_type.check_existing
        return self.defaults.check_existing

    def resolve_confirmation_words(self) -> list[str]:
        """The pool the confirmation word is drawn from.

        The course's own list if it has one, otherwise the built-in list for the
        course language. Never empty when the confirmation is enabled: an empty
        pool would leave the question with no word to show, and the step would
        have to be skipped silently -- which is the one outcome the ruling does
        not allow, since a skipped question is indistinguishable from a
        confirmed one in the issue body.
        """
        if self.confirmation.words:
            return list(self.confirmation.words)
        return list(
            DEFAULT_CONFIRMATION_WORDS.get(self.course.language, DEFAULT_CONFIRMATION_WORDS["en"])
        )

    def resolve_decline_word(self) -> str:
        """The decline form SHOWN to the student, in the course language.

        Only the display form follows the language; `is_decline_answer` accepts
        every language's form, so a student who types the other one is still
        understood.
        """
        return DECLINE_WORDS.get(self.course.language, DECLINE_WORDS["en"])

    def resolve_labels(self, handin_type: HandinType) -> list[str]:
        """Get the effective labels (defaults + per-type)."""
        return list(self.defaults.labels) + list(handin_type.labels)



_SUBMISSION_HEADING = "## Submission Information"
_HANDIN_LINE = re.compile(r"^- \*\*Hand-in\*\*: `([^`\n]+)`[ \t\r]*$", re.MULTILINE)


def submission_section(body: str) -> str | None:
    """The "## Submission Information" section of an issue body, or None."""
    start = body.find(_SUBMISSION_HEADING)
    if start < 0:
        return None
    rest = body[start + len(_SUBMISSION_HEADING) :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def submission_handin_id(body: str) -> str | None:
    """The hand-in id the tool wrote into an issue body, or None if there is none."""
    section = submission_section(body or "")
    if section is None:
        return None
    match = _HANDIN_LINE.search(section)
    return match.group(1) if match else None


def issue_is_handin(
    issue: dict[str, Any],
    handin_id: str,
    labels: list[str],
    known_ids: set[str] | None = None,
) -> bool:
    """Is this issue a submission of `handin_id`?

    The body line decides when it is there AND names a hand-in the configuration
    knows (`known_ids`; None = trust every id). An id the configuration no
    longer has -- a hand-in renamed through the central file -- falls back to
    the labels, which is how such an issue was found before 2.12.0. The labels
    are a fallback only when there IS a label to require: "every one of no
    labels is present" is true of every issue in the project.
    """
    found = submission_handin_id(str(issue.get("description") or ""))
    if found is not None and (known_ids is None or found in known_ids):
        return found == handin_id
    if not labels:
        return False
    present = issue.get("labels") or []
    return all(label in present for label in labels)



SWEDISH_TZ_NAME = "Europe/Stockholm"

# The clock time a `[[deadline]]` row without one is given.
DEADLINE_DEFAULT_TIME = time_of_day(23, 59)

_CET = timedelta(hours=1)
_CEST = timedelta(hours=2)

_TIME_SEPARATOR = re.compile(r"\d[Tt ]\d")


def _last_sunday_day(year: int, month: int) -> int:
    """Day of month of the last Sunday, for a 31-day month.

    Only March and October are ever asked for, and both have 31 days, so the
    last day of the month is a constant.

    Args:
        year: Calendar year.
        month: 3 or 10.

    Returns:
        The day of month (25..31) that is the last Sunday.
    """
    # weekday(): Monday is 0, Sunday is 6.
    return 31 - ((date_type(year, month, 31).weekday() + 1) % 7)


@lru_cache(maxsize=128)
def _eu_summer_period(year: int) -> tuple[datetime, datetime]:
    """The UTC instants EU summer time starts and ends in a given year.

    Cached per year, which costs nothing in the tool (a handful of deadline
    rows) and keeps the cross-measurement against `zoneinfo` in the test suite
    affordable over four decades of wall clocks.

    Args:
        year: Calendar year.

    Returns:
        (start, end) as timezone-aware UTC datetimes; the period is [start, end).
    """
    return (
        datetime(year, 3, _last_sunday_day(year, 3), 1, 0, tzinfo=timezone.utc),
        datetime(year, 10, _last_sunday_day(year, 10), 1, 0, tzinfo=timezone.utc),
    )


def _eu_summer_time(utc_moment: datetime) -> bool:
    """True if EU summer time is in force at the given UTC instant.

    Args:
        utc_moment: A timezone-aware datetime in UTC.

    Returns:
        Whether the instant falls inside the summer-time period.
    """
    start, end = _eu_summer_period(utc_moment.year)
    return start <= utc_moment < end


def swedish_utc_offset(local_naive: datetime) -> timedelta:
    """Return the UTC offset a naive Swedish wall clock has.

    This is the EU-rule fallback, used when `zoneinfo` cannot be loaded. See the
    section comment above for the ambiguity rule, for the witness that this
    agrees with `zoneinfo` (fold=0) for every hour from 1996 through 2040, and
    for why 1996 is a floor and not an arbitrary start year.

    Args:
        local_naive: A naive datetime read as Swedish local time.

    Returns:
        `timedelta(hours=1)` for CET or `timedelta(hours=2)` for CEST.
    """
    as_utc = local_naive.replace(tzinfo=timezone.utc)
    summer_if_standard = _eu_summer_time(as_utc - _CET)
    summer_if_summer = _eu_summer_time(as_utc - _CEST)
    if summer_if_standard and summer_if_summer:
        return _CEST
    if not summer_if_standard and not summer_if_summer:
        return _CET
    if summer_if_standard:
        # Spring: this wall clock does not exist. Take the offset from before
        # the changeover, which is what fold=0 means.
        return _CET
    # Autumn: this wall clock happens twice. Take the first of the two.
    return _CEST


def _localise_swedish(local_naive: datetime) -> datetime:
    """Attach Swedish local time to a naive datetime, without ever raising.

    `zoneinfo` first, because it is right in every year and needs no maintenance
    when the rule changes. `ZoneInfoNotFoundError` (a `KeyError`) is what a
    Windows Python without `tzdata` raises, but the except is deliberately
    broad: a stripped-down build with no `zoneinfo` module raises `ImportError`,
    a broken tz database can raise `OSError` or `ValueError`, and none of those
    may be allowed to abort a hand-in.

    Args:
        local_naive: A naive datetime read as Swedish local time.

    Returns:
        The same wall clock, made timezone-aware.
    """
    try:
        from zoneinfo import ZoneInfo

        # The offset is MATERIALISED here, inside the try, rather than returning
        # a value that carries `ZoneInfo` as its tzinfo. A `ZoneInfo` object
        # looks up the database lazily, so a construction that succeeds against
        # a truncated database can still raise later -- and "later" is
        # `dt < now`, `sort()` and `(dt - now).days` in the callers, every one
        # of them outside a try. Materialising makes both paths return the same
        # shape (a fixed offset) and keeps every way this can fail inside the
        # one function that is allowed to fail.
        aware = local_naive.replace(tzinfo=ZoneInfo(SWEDISH_TZ_NAME))
        offset = aware.utcoffset()
        if offset is None:
            raise ValueError("zoneinfo gave no offset")
        return local_naive.replace(tzinfo=timezone(offset))
    except Exception:
        return local_naive.replace(tzinfo=timezone(swedish_utc_offset(local_naive)))


def parse_deadline_date(date_str: str) -> datetime | None:
    """Parse the `date` field of a `[[deadline]]` row.

    Every failure returns None rather than raising: the callers skip a row they
    cannot read, and a malformed date in a course configuration must not be able
    to stop a submission.

    Args:
        date_str: The date string from the configuration.

    Returns:
        A timezone-aware datetime, or None if the row cannot be read.
    """
    try:
        text = date_str.strip()
        if _TIME_SEPARATOR.search(text):
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.combine(date_type.fromisoformat(text), DEADLINE_DEFAULT_TIME)
        if parsed.tzinfo is None:
            return _localise_swedish(parsed)
        return parsed
    except Exception:
        return None


# ─── Config loading and parsing ─────────────────────────────────────────────


def _parse_lint_checks(raw: list[dict[str, Any]], diagnostics: list[str] | None = None) -> list[LintCheck]:
    """Parse lint check definitions from TOML.

    A check without a ``command`` is skipped with a warning rather than raising
    KeyError: this runs while a student is handing in, and a traceback there is
    worse than a missing check.
    """
    sink: list[str] = [] if diagnostics is None else diagnostics
    checks = []
    for item in raw:
        if "command" not in item:
            sink.append(
                "Warning: Skipping lint check without 'command': "
                f"{item.get('name', 'unnamed')}"
            )
            continue
        checks.append(
            LintCheck(
                name=item.get("name", "Unnamed check"),
                command=item["command"],
                required=item.get("required", False),
                advisory=item.get("advisory", False) is True,
                unavailable_exit_codes=[code for code in item.get("unavailable_exit_codes", [])
                                        if isinstance(code, int) and not isinstance(code, bool) and code > 0]
                if isinstance(item.get("unavailable_exit_codes", []), list) else [],
            )
        )
    if diagnostics is None:
        for line in sink:
            print(line)
    return checks


def _parse_code_config(raw: dict[str, Any], defaults: CodeConfig) -> CodeConfig:
    """Parse code config, falling back to defaults."""
    return CodeConfig(
        check_uncommitted=raw.get("check_uncommitted", defaults.check_uncommitted),
        require_push=raw.get("require_push", defaults.require_push),
        check_analysis=raw.get("check_analysis", defaults.check_analysis),
        validation_script=raw.get("validation_script", defaults.validation_script),
    )


def _parse_report_config(raw: dict[str, Any], defaults: ReportConfig) -> ReportConfig:
    """Parse report config, falling back to defaults."""
    return ReportConfig(
        allowed_formats=raw.get("allowed_formats", defaults.allowed_formats),
        max_size_mb=raw.get("max_size_mb", defaults.max_size_mb),
        min_pages=raw.get("min_pages", defaults.min_pages),
        max_pages=raw.get("max_pages", defaults.max_pages),
        file_prompt=raw.get("file_prompt", defaults.file_prompt),
    )


def _parse_lint_config(
    raw: dict[str, Any], defaults: LintConfig, diagnostics: list[str] | None = None
) -> LintConfig:
    """Parse lint config, falling back to defaults."""
    if "checks" in raw:
        return LintConfig(checks=_parse_lint_checks(raw["checks"], diagnostics))
    return LintConfig(checks=list(defaults.checks))


def _parse_defaults(raw: dict[str, Any], diagnostics: list[str] | None = None) -> DefaultsConfig:
    """Parse the [defaults] section.

    ``diagnostics`` collects warnings instead of printing them. Omitting it
    keeps the original behaviour -- the warnings are printed -- because callers
    outside this module rely on that.
    """
    sink: list[str] = [] if diagnostics is None else diagnostics
    code_raw = raw.get("code", {})
    report_raw = raw.get("report", {})
    lint_raw = raw.get("lint", {})

    code = CodeConfig(
        check_uncommitted=code_raw.get("check_uncommitted", True),
        require_push=code_raw.get("require_push", True),
        check_analysis=code_raw.get("check_analysis", True),
        validation_script=code_raw.get("validation_script", "bin/validate-handin"),
    )
    report = ReportConfig(
        allowed_formats=report_raw.get("allowed_formats", ["pdf"]),
        max_size_mb=report_raw.get("max_size_mb", 20),
        min_pages=report_raw.get("min_pages", 0),
        max_pages=report_raw.get("max_pages", 0),
        file_prompt=report_raw.get("file_prompt", "Select your report file"),
    )
    lint = LintConfig(checks=_parse_lint_checks(lint_raw.get("checks", []), sink))

    partner_assignee = str(raw.get("partner_assignee", "bot")).strip().lower()
    if partner_assignee not in ("bot", "off"):
        sink.append(
            "Warning: [defaults] partner_assignee = "
            f"'{raw.get('partner_assignee')}' is not one of 'bot', 'off'. "
            "Falling back to 'bot' (the safe default)."
        )
        partner_assignee = "bot"

    if diagnostics is None:
        for line in sink:
            print(line)

    return DefaultsConfig(
        target_project_id=raw.get("target_project_id", 0),
        confidential=raw.get("confidential", True),
        check_existing=raw.get("check_existing", True),
        labels=raw.get("labels", []),
        config_update_url=raw.get("config_update_url", ""),
        deadlines_url=raw.get("deadlines_url", ""),
        min_script_version=raw.get("min_script_version", ""),
        partner_assignee=partner_assignee,
        bot_active=bool(raw.get("bot_active", False)),
        labels_set_later=raw.get("labels_set_later", False) is True,
        template_project=str(raw.get("template_project", "") or "").strip(),
        code=code,
        report=report,
        lint=lint,
    )


def _parse_confirmation(raw: dict[str, Any], diagnostics: list[str] | None = None) -> ConfirmationConfig:
    """Parse the [confirmation] section.

    One asymmetry is deliberate: a bad `words` entry is dropped, but a course's
    surviving list is never silently replaced by the built-in one. A course that
    configures a list has made a decision; overriding it quietly would mean the
    students see one word pool and the teacher believes in another.
    """
    warn = diagnostics if diagnostics is not None else []

    enabled = bool(raw.get("enabled", False))
    rules_url = str(raw.get("rules_url", "") or "").strip()

    words_raw = raw.get("words", [])
    words: list[str] = []
    if not isinstance(words_raw, list):
        warn.append("Warning: [confirmation] words must be a list of words. Using the built-in list.")
    else:
        for entry in words_raw:
            if not isinstance(entry, str) or not entry.strip():
                warn.append(f"Warning: [confirmation] words contains an entry that is not a word: {entry!r}. Skipped.")
                continue
            # A "word" of pure punctuation is not a word, and it is not merely
            # useless: the tool strips quotes and backticks off the answer before
            # comparing, so a pool entry like "``" would normalise to the empty
            # string -- and a blank Enter would then CONFIRM. That is the exact
            # outcome the varying word exists to prevent, arriving through the
            # configuration file. Require at least one letter or digit.
            if not any(ch.isalnum() for ch in entry):
                warn.append(
                    f"Warning: [confirmation] words contains an entry with no letters or digits: "
                    f"{entry!r}. Skipped -- it could be matched by an empty answer."
                )
                continue
            # A pool word that IS the decline form would make "no" confirm --
            # the single worst outcome this feature can produce, and one a
            # course could reach by accident with a plausible word list. The
            # runtime reads the decline form first, so such a word could never
            # be typed successfully either; dropping it here is what keeps the
            # two ends consistent instead of merely safe.
            if is_decline_answer(entry):
                warn.append(
                    f"Warning: [confirmation] words contains the decline form: {entry!r}. "
                    "Skipped -- that word is how a student says the rules were NOT followed, "
                    "and it must never be able to confirm."
                )
                continue
            words.append(entry.strip())

    if enabled and len(set(w.casefold() for w in words)) == 1:
        # One word is not a varying word. The whole point of part (c) is that the
        # answer cannot become a reflex, and a single-word pool restores exactly
        # the reflex it was meant to remove.
        warn.append(
            "Warning: [confirmation] words lists only one word, so the same word is shown at "
            "every hand-in. That is the reflex the confirmation exists to prevent. Add more words."
        )

    attempts_raw = raw.get("attempts", 3)
    if not isinstance(attempts_raw, int) or isinstance(attempts_raw, bool) or attempts_raw < 1:
        if "attempts" in raw:
            warn.append(
                f"Warning: [confirmation] attempts = {attempts_raw!r} is not a positive whole number. Using 3."
            )
        attempts = 3
    else:
        attempts = attempts_raw

    if enabled and "on_missing" in raw:
        warn.append(
            "Warning: [confirmation] on_missing no longer has any effect and is ignored. "
            "An unclear answer now means nothing is handed in at all; a clear 'no' hands in "
            "with the student's own description. Remove the line."
        )

    return ConfirmationConfig(
        enabled=enabled,
        rules_url=rules_url,
        words=words,
        attempts=attempts,
    )


_CHECKLIST_ITEM_KEYS = frozenset({"question", "stop_on_no"})


def _checklist_entry_text(entry: Any) -> str:
    """The text to ASK for a malformed checklist entry.

    A malformed entry is still asked (see ``_parse_checklist``), so it needs a
    question text. For a table that lacks a usable ``question`` the first
    non-empty string in it is the best guess -- a misspelt key such as
    ``fraga = "..."`` then still shows the course's own sentence. Anything else
    falls back on ``str()``: ugly, but a visible question beats a lost one.
    """
    if isinstance(entry, dict):
        for value in entry.values():
            if isinstance(value, str) and value.strip():
                return value
    return str(entry)


def _parse_checklist(raw: Any, handin_id: str, diagnostics: list[str] | None = None) -> ChecklistConfig:
    """Parse ``[handin.checklist]``.

    Each entry in ``items`` is EITHER a plain string (informational: the
    answer is recorded in the issue and the run goes on, exactly as before
    2.14.0) OR an inline table ``{ question = "...", stop_on_no = true }``.
    The two forms may be mixed in one array.

    The rule for a malformed entry is that it is NEVER silently dropped: a
    question the course wrote must still reach the student. It is reported in
    the diagnostics and asked anyway. ``stop_on_no`` counts only when it is
    literally ``true`` -- a string "true", a 1 or a misspelt key never turns a
    question into a showstopper, and each of them is reported so the course
    sees that its intent did not take effect.
    """
    sink: list[str] = [] if diagnostics is None else diagnostics
    where = f"[handin.checklist] in '{handin_id}'"

    if isinstance(raw, list):
        sink.append(f"Warning: {where}: expected a table with 'items', got a list. Using the list as the items.")
        raw = {"items": raw}
    elif not isinstance(raw, dict):
        sink.append(f"Warning: {where}: expected a table with 'items', got {raw!r}. No checklist.")
        raw = {}

    items_raw = raw.get("items", [])
    if isinstance(items_raw, (str, dict)):
        sink.append(f"Warning: {where}: 'items' should be a list. Asking the single entry anyway.")
        items_raw = [items_raw]
    elif not isinstance(items_raw, list):
        sink.append(f"Warning: {where}: 'items' should be a list, got {items_raw!r}. Asking it anyway.")
        items_raw = [items_raw]

    questions: list[str] = []
    stops: list[bool] = []
    for entry in items_raw:
        if isinstance(entry, str):
            questions.append(entry)
            stops.append(False)
            continue

        if not isinstance(entry, dict):
            sink.append(
                f"Warning: {where}: entry {entry!r} is neither a string nor "
                "{ question = \"...\", stop_on_no = true }. It is asked as written."
            )
            questions.append(str(entry))
            stops.append(False)
            continue

        question = entry.get("question")
        if not (isinstance(question, str) and question.strip()):
            question = _checklist_entry_text(entry)
            sink.append(
                f"Warning: {where}: entry {entry!r} has no text in 'question'. "
                f"It is asked as: {question!r}"
            )

        unknown = sorted(str(k) for k in entry if k not in _CHECKLIST_ITEM_KEYS)
        if unknown:
            sink.append(
                f"Warning: {where}: entry {question!r} has unknown key(s) {', '.join(unknown)}. "
                "Only 'question' and 'stop_on_no' are read."
            )

        stop_raw = entry.get("stop_on_no", False)
        if not isinstance(stop_raw, bool):
            sink.append(
                f"Warning: {where}: entry {question!r} has stop_on_no = {stop_raw!r}, "
                "which is not true or false. The question is asked, but a 'no' does NOT stop the hand-in."
            )

        questions.append(question)
        stops.append(stop_raw is True)

    if diagnostics is None:
        for line in sink:
            print(line)
    return ChecklistConfig(items=questions, stop_on_no=stops)


def _parse_handin_type(
    raw: dict[str, Any], defaults: DefaultsConfig, diagnostics: list[str] | None = None
) -> HandinType:
    """Parse a single [[handin]] entry with default resolution."""
    ht_type = raw.get("type", "code")

    code = _parse_code_config(raw.get("code", {}), defaults.code) if ht_type == "code" else CodeConfig()
    report = (
        _parse_report_config(raw.get("report", {}), defaults.report)
        if ht_type in ("report-prereview", "report-final")
        else ReportConfig()
    )
    lint = _parse_lint_config(raw.get("lint", {}), defaults.lint, diagnostics)

    ai_raw = raw.get("ai_review", {})
    ai_review = AIReviewConfig(
        enabled=ai_raw.get("enabled", False),
        command=ai_raw.get("command", ""),
        include_in_issue=ai_raw.get("include_in_issue", True),
        block_on_errors=ai_raw.get("block_on_errors", False),
    )

    checklist = _parse_checklist(raw.get("checklist", {}), str(raw.get("id", "?")), diagnostics)

    return HandinType(
        id=raw["id"],
        name=raw["name"],
        type=ht_type,
        labels=raw.get("labels", []),
        required_paths=[
            str(x) for x in raw.get("required_paths", []) if isinstance(raw.get("required_paths"), list) and str(x).strip()
        ],
        target_project_id=raw.get("target_project_id"),
        check_existing=raw.get("check_existing"),
        confidential=raw.get("confidential"),
        group_submission=raw.get("group_submission", False),
        max_group_size=raw.get("max_group_size", 2),
        code=code,
        report=report,
        lint=lint,
        ai_review=ai_review,
        checklist=checklist,
    )


class ConfigError(Exception):
    """Configuration data could not be turned into a HandinConfig.

    Carries the diagnostics collected before the failure so the caller can
    print them in the same order they would have appeared on a successful
    load. Raised instead of calling sys.exit, so the same validation can run
    on a configuration fetched over the network without being able to end
    the process.
    """

    def __init__(self, message: str, hint: str = "", diagnostics: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.diagnostics = list(diagnostics or [])


def parse_config(raw: dict[str, Any], diagnostics: list[str]) -> HandinConfig:
    """Turn a parsed TOML mapping into a HandinConfig.

    Warnings are appended to ``diagnostics`` rather than printed, so a caller
    working on a REMOTE configuration can label or suppress them: a student
    must not be told to fix a file they cannot see.

    Args:
        raw: The mapping produced by ``tomllib``.
        diagnostics: Collector for non-fatal warnings, appended in order.

    Returns:
        Fully parsed HandinConfig.

    Raises:
        ConfigError: If the configuration is unusable.
    """
    course_raw = raw.get("course", {})
    if not course_raw.get("name"):
        raise ConfigError("Configuration file is missing [course] name.", diagnostics=diagnostics)

    language = str(course_raw.get("language", "en")).strip().lower()
    if language not in ("en", "sv"):
        diagnostics.append(
            f"Warning: [course] language = '{course_raw.get('language')}' is "
            "not a supported language ('en', 'sv'). Falling back to 'en'."
        )
        language = "en"

    expected_origin_raw = course_raw.get("expected_origin_paths", [])
    expected_origin_paths: list[str] = []
    if isinstance(expected_origin_raw, list):
        expected_origin_paths = [str(item) for item in expected_origin_raw if str(item).strip()]
    elif expected_origin_raw:
        diagnostics.append(
            "Warning: [course] expected_origin_paths must be a list of patterns; ignoring it."
        )

    upstream_raw = course_raw.get("upstream_project_id", 0)
    upstream_project_id = 0
    if isinstance(upstream_raw, int) and not isinstance(upstream_raw, bool):
        upstream_project_id = upstream_raw
    elif upstream_raw:
        diagnostics.append(
            "Warning: [course] upstream_project_id must be a number; ignoring it."
        )

    course = CourseConfig(
        name=course_raw["name"],
        display_name=course_raw.get("display_name", course_raw["name"]),
        gitlab_url=course_raw.get("gitlab_url", "https://gitlab.liu.se"),
        language=language,
        expected_origin_paths=expected_origin_paths,
        upstream_project_id=upstream_project_id,
    )

    auth_raw = raw.get("auth", {})
    oauth_id = auth_raw.get("oauth_client_id", "")
    auth = AuthConfig(
        oauth_client_id=oauth_id if oauth_id else None,
        token_cache_dir=auth_raw.get("token_cache_dir", "~/.config/handin"),
    )

    defaults = _parse_defaults(raw.get("defaults", {}), diagnostics)
    confirmation = _parse_confirmation(raw.get("confirmation", {}), diagnostics)

    handins = []
    for handin_raw in raw.get("handin", []):
        if "id" not in handin_raw or "name" not in handin_raw:
            diagnostics.append(f"Warning: Skipping handin entry without 'id' or 'name': {handin_raw}")
            continue
        valid_types = ("code", "report-prereview", "report-final")
        ht_type = handin_raw.get("type", "code")
        if ht_type not in valid_types:
            diagnostics.append(
                f"Warning: Unknown handin type '{ht_type}' for '{handin_raw.get('id')}'. Expected one of {valid_types}."
            )
        handins.append(_parse_handin_type(handin_raw, defaults, diagnostics))

    deadlines = []
    for dl_raw in raw.get("deadline", []):
        raw_date = dl_raw.get("date", "")
        if not isinstance(raw_date, str):
            diagnostics.append(
                f"Warning: [[deadline]] date must be a quoted string, "
                f"not {type(raw_date).__name__} "
                f"({raw_date!r}) — this deadline is ignored"
            )
            raw_date = ""
        deadlines.append(
            Deadline(
                date=raw_date,
                handins=dl_raw.get("handins", []),
                message=dl_raw.get("message", ""),
            )
        )

    if not handins:
        raise ConfigError(
            "No hand-in types defined in configuration file.",
            hint="  Add at least one [[handin]] section to handin.toml.",
            diagnostics=diagnostics,
        )

    return HandinConfig(
        course=course,
        auth=auth,
        defaults=defaults,
        handins=handins,
        deadlines=deadlines,
        confirmation=confirmation,
    )


def load_config(config_path: Path) -> HandinConfig:
    """Load and parse a handin.toml configuration file.

    Args:
        config_path: Path to the TOML configuration file.

    Returns:
        Fully parsed HandinConfig.

    Raises:
        SystemExit: If the config file is missing or invalid.
    """
    if not config_path.is_file():
        print(f"Error: Configuration file not found: {config_path}")
        print("  The hand-in script requires a handin.toml configuration file.")
        print("  Please contact your course instructor.")
        sys.exit(1)

    # TOML is UTF-8 by specification. Reading with the locale encoding instead
    # made a file with a/o/a-umlauts depend on which machine opened it.
    # UnicodeDecodeError is not a TOMLDecodeError and would otherwise escape
    # the handler below as a raw traceback.
    try:
        text = config_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        print(f"Error: Could not read configuration file: {config_path}")
        print(f"  {exc}")
        print("  The file must be saved as UTF-8. Please contact your course instructor.")
        sys.exit(1)

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        print(f"Error: Invalid configuration file: {config_path}")
        print(f"  {exc}")
        sys.exit(1)

    diagnostics: list[str] = []
    try:
        config = parse_config(raw, diagnostics)
    except ConfigError as exc:
        for line in exc.diagnostics:
            print(line)
        print(f"Error: {exc.message}")
        if exc.hint:
            print(exc.hint)
        sys.exit(1)

    for line in diagnostics:
        print(line)
    return config


def find_config_file(explicit_path: str | None = None) -> Path:
    """Find the handin.toml configuration file.

    Search order:
    1. Explicit path passed via ``--config`` (error if not found).
    2. ``.handin.toml`` in the git repository root.
    3. ``handin.toml`` in the script directory (original behaviour).
    4. ``~/.config/handin/handin.toml`` (user-level default).

    If none found, returns the script-dir path so ``load_config``
    produces its usual error message.

    Args:
        explicit_path: Optional path supplied via CLI ``--config``.

    Returns:
        Path to the configuration file.
    """
    if explicit_path is not None:
        p = Path(explicit_path).expanduser().resolve()
        if not p.is_file():
            print(f"Error: Specified config file not found: {p}")
            sys.exit(1)
        return p

    # Try .handin.toml at the git repo root
    try:
        # UTF-8 with surrogateescape: git prints UTF-8, and the platform codec
        # (cp1252 on Swedish Windows) turns C:\Users\Åsa into a path that
        # does not exist. surrogateescape never raises and keeps undecodable
        # bytes round-trippable instead of silently replacing them.
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=True,
        )
        repo_root = Path(result.stdout.strip())
        repo_config = repo_root / ".handin.toml"
        if repo_config.is_file():
            return repo_config
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # Not in a git repo or git not available

    # Script directory (original fallback)
    script_dir = Path(__file__).resolve().parent
    script_config = script_dir / "handin.toml"
    if script_config.is_file():
        return script_config

    # User-level default
    user_config = Path("~/.config/handin/handin.toml").expanduser()
    if user_config.is_file():
        return user_config

    # Return script-dir path to trigger the existing error in load_config
    return script_config


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison."""
    return tuple(int(x) for x in v.split("."))


def _is_newer_version(required: str, running: str) -> bool:
    """True if ``required`` is strictly newer than ``running``.

    Used for the ADVISORY note about a central min_script_version. A version
    string fetched over the network may be anything at all, so an unparsable
    value is treated as "no reason to nag" rather than raising: this runs on
    every hand-in, and a malformed number on the course server must never be
    able to interrupt one.
    """
    if not running:
        return False
    try:
        return _version_tuple(required) > _version_tuple(running)
    except ValueError:
        return False


def check_script_version(config: HandinConfig, script_version: str) -> None:
    """Check that the running script meets the minimum version required by config.

    Prints a clear message and exits if the script is too old.

    Args:
        config: Loaded configuration.
        script_version: The current ``SCRIPT_VERSION`` string.
    """
    min_ver = config.defaults.min_script_version
    if not min_ver:
        return
    try:
        if _version_tuple(script_version) < _version_tuple(min_ver):
            print(f"Error: This hand-in script is too old (v{script_version}).")
            print(f"  The course configuration requires at least v{min_ver}.")
            print("  Please download the latest version of the script from your course page.")
            sys.exit(1)
    except ValueError:
        # Malformed version string — skip check rather than crash
        pass



_UPDATE_TIMEOUT_SECONDS = 5.0
_UPDATE_MAX_BYTES = 1_048_576
_UPDATE_MAX_TEXT = 500
_UPDATE_CHUNK = 65_536

# C0 and C1 control characters. `report.file_prompt` is interpolated into an
# input() prompt and `deadline.message` is printed raw, so an escape sequence
# from the network could repaint the screen and spoof "Paste your GitLab token:".
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass
class ConfigUpdate:
    """Outcome of consulting the course's central configuration."""

    config: HandinConfig
    status: str  # "disabled" | "applied" | "unchanged" | "rejected"
    reason: str = ""  # stable short code, "" when nothing was rejected
    detail: str = ""  # one sentence for the student
    notes: list[str] = field(default_factory=list)
    local_only_ids: list[str] = field(default_factory=list)


class _Rejected(Exception):
    """The remote configuration cannot be used. Carries a stable reason code."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _clean_text(value: str) -> str:
    """Strip control characters and cap the length of a string from the network."""
    return _CONTROL_CHARS.sub("", value)[:_UPDATE_MAX_TEXT]


def _remote_str(raw: dict[str, Any], key: str, default: str, field_name: str, errors: list[str]) -> str:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str):
        errors.append(f"{field_name} must be text, not {type(value).__name__}")
        return default
    return _clean_text(value)


def _remote_int(raw: dict[str, Any], key: str, default: int, field_name: str, errors: list[str]) -> int:
    if key not in raw:
        return default
    value = raw[key]
    # bool is a subclass of int; `confidential = true` in an int field is a
    # mistake worth reporting rather than silently reading as 1.
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{field_name} must be a whole number, not {type(value).__name__}")
        return default
    return value


def _remote_bool(raw: dict[str, Any], key: str, default: bool, field_name: str, errors: list[str]) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        errors.append(f"{field_name} must be true or false, not {type(value).__name__}")
        return default
    return value


def _remote_str_list(
    raw: dict[str, Any], key: str, default: list[str], field_name: str, errors: list[str]
) -> list[str]:
    if key not in raw:
        return list(default)
    value = raw[key]
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list of text values, not {type(value).__name__}")
        return list(default)
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            errors.append(f"{field_name} must contain only text values")
            return list(default)
        cleaned.append(_clean_text(item))
    return cleaned


def _remote_table(raw: dict[str, Any], key: str, field_name: str, errors: list[str]) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        errors.append(f"{field_name} must be a section, not {type(value).__name__}")
        return {}
    return value


def _scheme_allowed(url: str) -> bool:
    """https everywhere; plain http only against loopback.

    A configuration fetched over plain http can be rewritten in transit, and it
    decides where submissions are created. Loopback stays open so a teacher can
    test against a local mirror and so the test suite can use a real http.server
    rather than a transport fake.
    """
    parts = urlparse(url)
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return (parts.hostname or "") in ("127.0.0.1", "::1", "localhost")
    return False


def _fetch_blocking(url: str, timeout: float) -> str:
    """Fetch the remote configuration text. Raises _Rejected on any refusal."""
    if not _scheme_allowed(url):
        raise _Rejected(
            "not-https",
            f"the address {url} is not https (only https, or http to your own machine, is used)",
        )

    request = Request(
        url,
        method="GET",
        headers={
            # No transparent gzip from a proxy: read() would hand back the
            # compressed bytes and the file would look like broken TOML.
            "Accept-Encoding": "identity",
            # A caching proxy serving yesterday's file is the exact failure this
            # feature exists to prevent.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    # Refuse redirects instead of following them: urllib's own redirect handler
    # allows http, so https -> 302 -> http would be followed and a check on the
    # address we typed would prove nothing.
    opener = https_opener(_NoRedirect)

    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type == "text/html":
                raise _Rejected(
                    "html",
                    "the address returned a web page instead of a configuration file "
                    "(it may require a login, or a network portal answered instead)",
                )
            while True:
                if time.monotonic() > deadline:
                    raise _Rejected("too-slow", "the course server answered too slowly")
                chunk = response.read(_UPDATE_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > _UPDATE_MAX_BYTES:
                    # Reject, never truncate: a cut usually lands after a whole
                    # line, so tomllib would accept HALF a configuration and the
                    # last hand-ins and deadlines would vanish without a word.
                    raise _Rejected("too-large", "the configuration file is larger than 1 MiB")
                chunks.append(chunk)
    except _Rejected:
        raise
    except ApiTransport as exc:
        raise _Rejected("redirect", str(exc)) from exc
    except Exception as exc:
        raise _Rejected("unreachable", f"the course server could not be reached ({exc})") from exc

    try:
        # utf-8-sig: an editor on Windows may add a byte order mark, and a bare
        # utf-8 decode would leave it in front of the first key so tomllib fails
        # and the teacher's saved file becomes a silent no-op.
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _Rejected("not-utf8", "the configuration file is not valid UTF-8 text") from exc


def fetch_remote_config_text(url: str, timeout: float = _UPDATE_TIMEOUT_SECONDS) -> tuple[str | None, str, str]:
    """Fetch remote configuration text within a total time budget.

    The socket timeout is per operation, and neither it nor a monotonic deadline
    inside the read loop covers name resolution or a connect attempt per address
    (A and AAAA records double the connect phase). A daemon thread with a join
    deadline is what actually bounds the wall clock, so a hung server cannot
    stall a hand-in.

    Args:
        url: Address to fetch.
        timeout: Total budget in seconds.

    Returns:
        ``(text, reason, detail)``. ``text`` is None when nothing usable was
        fetched, and ``reason`` is then a stable short code.
    """
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            outcome["text"] = _fetch_blocking(url, timeout)
        except _Rejected as exc:
            outcome["reason"] = exc.reason
            outcome["detail"] = exc.detail
        except BaseException as exc:  # noqa: BLE001 - a hand-in must not die here
            outcome["reason"] = "unreachable"
            outcome["detail"] = f"the course server could not be reached ({exc})"

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None, "timeout", f"the course server did not answer within {timeout:.0f} seconds"

    text = outcome.get("text")
    if text is None:
        return None, str(outcome.get("reason", "unreachable")), str(outcome.get("detail", ""))
    return text, "", ""


def _remote_report(
    raw: dict[str, Any], fallback: ReportConfig, field_name: str, errors: list[str]
) -> ReportConfig:
    """Report settings are DATA and may come from the course. No commands here."""
    return ReportConfig(
        allowed_formats=_remote_str_list(
            raw, "allowed_formats", fallback.allowed_formats, f"{field_name}.allowed_formats", errors
        ),
        max_size_mb=_remote_int(raw, "max_size_mb", fallback.max_size_mb, f"{field_name}.max_size_mb", errors),
        min_pages=_remote_int(raw, "min_pages", fallback.min_pages, f"{field_name}.min_pages", errors),
        max_pages=_remote_int(raw, "max_pages", fallback.max_pages, f"{field_name}.max_pages", errors),
        file_prompt=_remote_str(raw, "file_prompt", fallback.file_prompt, f"{field_name}.file_prompt", errors),
    )


_COMMAND_KEYS = ("lint", "ai_review")


def _remote_command_signature(raw: dict[str, Any]) -> tuple[Any, ...] | None:
    """The executable strings this remote section declares, or None if it declares none.

    Used ONLY to decide whether to tell the student that something was ignored.
    The values are never applied -- that is settled structurally in
    _apply_remote, which builds every executed field from local objects.
    """
    if not _declares_command(raw):
        return None
    lint_raw = raw.get("lint")
    lint_cmds: tuple[str, ...] = ()
    if isinstance(lint_raw, dict) and isinstance(lint_raw.get("checks"), list):
        lint_cmds = tuple(
            str(c.get("command", "")) for c in lint_raw["checks"] if isinstance(c, dict)
        )
    ai_raw = raw.get("ai_review")
    ai_cmd = str(ai_raw.get("command", "")) if isinstance(ai_raw, dict) else ""
    code_raw = raw.get("code")
    script = str(code_raw.get("validation_script", "")) if isinstance(code_raw, dict) else ""
    return (lint_cmds, ai_cmd, script)


def _local_command_signature(code: CodeConfig, lint: LintConfig, ai: AIReviewConfig) -> tuple[Any, ...]:
    """The same shape as _remote_command_signature, for the local objects in force."""
    return (tuple(c.command for c in lint.checks), ai.command, code.validation_script)


def _declares_command(raw: dict[str, Any]) -> bool:
    """Does this section try to supply something the tool would execute?"""
    for key in _COMMAND_KEYS:
        if raw.get(key):
            return True
    code_raw = raw.get("code")
    if isinstance(code_raw, dict) and "validation_script" in code_raw:
        return True
    return False


def _apply_remote(local: HandinConfig, remote_raw: dict[str, Any], script_version: str = "") -> ConfigUpdate:
    """Build the effective configuration from the local one and a remote mapping.

    Every value taken from the network is type-checked. A wrong type rejects the
    WHOLE update with a reason rather than being coerced: a `display_name` that
    is a number survives every guard and then dies in the banner's
    ``f"{value:^44s}"`` with a raw traceback, outside any try/except.
    """
    errors: list[str] = []
    notes: list[str] = []

    course_raw = _remote_table(remote_raw, "course", "[course]", errors)
    remote_name = _remote_str(course_raw, "name", "", "[course] name", errors)
    if errors:
        return ConfigUpdate(local, "rejected", "bad-type", "; ".join(errors))
    if not remote_name:
        return ConfigUpdate(local, "rejected", "invalid-config", "the central configuration has no [course] name")
    if remote_name != local.course.name:
        return ConfigUpdate(
            local,
            "rejected",
            "wrong-course",
            f"the central configuration is written for course '{remote_name}', not '{local.course.name}'",
        )

    display_name = _remote_str(
        course_raw, "display_name", local.course.display_name, "[course] display_name", errors
    )
    language = _remote_str(course_raw, "language", local.course.language, "[course] language", errors).strip().lower()
    if language not in ("en", "sv"):
        notes.append(_note(
            f"The central configuration asks for language '{language}', which is not supported.",
            f"Kursens centrala inställningar anger språket '{language}', som inte stöds.",
        ))
        language = local.course.language

    defaults_raw = _remote_table(remote_raw, "defaults", "[defaults]", errors)
    partner_assignee = _remote_str(
        defaults_raw, "partner_assignee", local.defaults.partner_assignee, "[defaults] partner_assignee", errors
    ).strip().lower()
    if partner_assignee not in ("bot", "off"):
        notes.append(_note(
            f"The central configuration asks for partner_assignee '{partner_assignee}', which is not a known value.",
            f"Kursens centrala inställningar anger partner_assignee '{partner_assignee}', som inte är ett känt värde.",
        ))
        partner_assignee = local.defaults.partner_assignee

    remote_report_defaults = _remote_report(
        _remote_table(defaults_raw, "report", "[defaults.report]", errors),
        local.defaults.report,
        "[defaults.report]",
        errors,
    )

    # Local-only: the submission target. See the trust-boundary comment above.
    if "target_project_id" in defaults_raw and defaults_raw["target_project_id"] != local.defaults.target_project_id:
        notes.append(_note(
            "The central configuration asks for a different hand-in project "
            "([defaults] target_project_id). That is never taken from the network "
            "and was ignored; your hand-in goes to the project your own "
            "configuration names.",
            "Kursens centrala inställningar anger ett annat inlämningsprojekt "
            "([defaults] target_project_id). Det hämtas aldrig över nätet och "
            "användes inte; din inlämning går till projektet i ditt eget repo.",
        ))

    remote_confidential = _remote_bool(
        defaults_raw, "confidential", local.defaults.confidential, "[defaults] confidential", errors
    )
    if local.defaults.confidential and not remote_confidential:
        notes.append(_note(
            "The central configuration asks for non-confidential hand-in issues "
            "([defaults] confidential = false). Confidentiality is never loosened "
            "over the network and the request was ignored; your hand-ins stay "
            "confidential.",
            "Kursens centrala inställningar ber om inlämningsärenden som inte är "
            "konfidentiella ([defaults] confidential = false). Det görs aldrig över "
            "nätet; dina inlämningar förblir konfidentiella.",
        ))
        remote_confidential = True

    defaults = DefaultsConfig(
        target_project_id=local.defaults.target_project_id,
        confidential=remote_confidential,
        check_existing=_remote_bool(
            defaults_raw, "check_existing", local.defaults.check_existing, "[defaults] check_existing", errors
        ),
        labels=_remote_str_list(defaults_raw, "labels", local.defaults.labels, "[defaults] labels", errors),
        # Local-only: no chaining to another address, no second unchecked fetch.
        config_update_url=local.defaults.config_update_url,
        deadlines_url=local.defaults.deadlines_url,
        # Local-only: a number fetched over the network must not end the process.
        min_script_version=local.defaults.min_script_version,
        partner_assignee=partner_assignee,
        bot_active=_remote_bool(defaults_raw, "bot_active", local.defaults.bot_active, "[defaults] bot_active", errors),
        labels_set_later=_remote_bool(
            defaults_raw,
            "labels_set_later",
            local.defaults.labels_set_later,
            "[defaults] labels_set_later",
            errors,
        ),
        # Local-only: names a place to read from.
        template_project=local.defaults.template_project,
        # Local-only: executed.
        code=local.defaults.code,
        lint=local.defaults.lint,
        report=remote_report_defaults,
    )

    remote_min_version = _remote_str(
        defaults_raw, "min_script_version", "", "[defaults] min_script_version", errors
    )

    handins_raw = remote_raw.get("handin", [])
    if not isinstance(handins_raw, list):
        errors.append("[[handin]] must be a list of sections")
        handins_raw = []

    # Note a DIFFERENCE, not mere presence. The obvious deployment is a course
    # publishing its own handin.toml as the central file, in which case the
    # commands are identical to the local ones and nothing was really ignored;
    # warning anyway put a permanent, frightening and untrue line in front of
    # every student on every run.
    remote_defaults_cmds = _remote_command_signature(defaults_raw)
    ignored_commands = remote_defaults_cmds is not None and remote_defaults_cmds != _local_command_signature(
        local.defaults.code, local.defaults.lint, AIReviewConfig()
    )
    handins: list[HandinType] = []
    seen_ids: set[str] = set()
    for entry in handins_raw:
        if not isinstance(entry, dict):
            errors.append("every [[handin]] must be a section")
            continue
        handin_id = _remote_str(entry, "id", "", "[[handin]] id", errors)
        handin_name = _remote_str(entry, "name", "", "[[handin]] name", errors)
        if not handin_id or not handin_name:
            notes.append(_note(
                "The central configuration has a hand-in without an id or a name; it was skipped.",
                "Kursens centrala inställningar har en inlämning utan id eller namn; den hoppades över.",
            ))
            continue
        if handin_id in seen_ids:
            notes.append(_note(
                f"The central configuration lists '{handin_id}' more than once; the first was kept.",
                f"Kursens centrala inställningar har '{handin_id}' mer än en gång; den första gäller.",
            ))
            continue
        seen_ids.add(handin_id)

        handin_type = _remote_str(entry, "type", "code", "[[handin]] type", errors)
        if handin_type not in ("code", "report-prereview", "report-final"):
            notes.append(_note(
                f"The central configuration gives '{handin_id}' an unknown type '{handin_type}'.",
                f"Kursens centrala inställningar ger '{handin_id}' den okända typen '{handin_type}'.",
            ))
            handin_type = "code"

        # Executed settings come from the LOCAL twin, or from the local
        # defaults for a hand-in the fork has never seen. The course can add or
        # rename a hand-in; it can never introduce a command.
        twin = local.get_handin(handin_id)
        if twin is not None:
            code_cfg, lint_cfg, ai_cfg = twin.code, twin.lint, twin.ai_review
            checklist_cfg = twin.checklist
        else:
            code_cfg, lint_cfg, ai_cfg = local.defaults.code, local.defaults.lint, AIReviewConfig()
            checklist_cfg = ChecklistConfig()

        if "target_project_id" in entry and entry["target_project_id"] != (
            twin.target_project_id if twin is not None else None
        ):
            notes.append(_note(
                f"The central configuration asks for a different hand-in project for "
                f"'{handin_id}' ([[handin]] target_project_id). That is never taken from "
                f"the network and was ignored.",
                f"Kursens centrala inställningar anger ett annat inlämningsprojekt för "
                f"'{handin_id}' ([[handin]] target_project_id). Det hämtas aldrig över "
                f"nätet och användes inte.",
            ))

        remote_entry_cmds = _remote_command_signature(entry)
        if remote_entry_cmds is not None and remote_entry_cmds != _local_command_signature(
            code_cfg, lint_cfg, ai_cfg
        ):
            ignored_commands = True

        handins.append(
            HandinType(
                id=handin_id,
                name=handin_name,
                type=handin_type,
                # A field the central file does not mention falls back to the
                # LOCAL twin, not to a hard-coded default. Otherwise a minimal
                # central file -- "just fix the id" -- silently turned a group
                # hand-in into an individual one and dropped its labels.
                labels=_remote_str_list(
                    entry, "labels", list(twin.labels) if twin is not None else [], "[[handin]] labels", errors
                ),
                # Local-only, same reason as [defaults] above: a per-hand-in
                # override is the same relocation one level down, and it is the
                # one that resolves FIRST (`resolve_target_project`).
                target_project_id=(twin.target_project_id if twin is not None else None),
                # Local-only: a fetched file that could empty this list would
                # turn the emptiness check off without anyone noticing.
                required_paths=(list(twin.required_paths) if twin is not None else []),
                check_existing=(
                    _remote_bool(entry, "check_existing", True, "[[handin]] check_existing", errors)
                    if "check_existing" in entry
                    else None
                ),
                confidential=(
                    True
                    if (
                        "confidential" in entry
                        and _remote_bool(entry, "confidential", True, "[[handin]] confidential", errors)
                    )
                    or (twin is not None and twin.confidential is True)
                    else None
                ),
                group_submission=_remote_bool(
                    entry,
                    "group_submission",
                    twin.group_submission if twin is not None else False,
                    "[[handin]] group_submission",
                    errors,
                ),
                max_group_size=_remote_int(
                    entry,
                    "max_group_size",
                    twin.max_group_size if twin is not None else 2,
                    "[[handin]] max_group_size",
                    errors,
                ),
                code=code_cfg,
                checklist=checklist_cfg,
                report=_remote_report(
                    _remote_table(entry, "report", "[[handin]] report", errors),
                    remote_report_defaults,
                    "[[handin]] report",
                    errors,
                ),
                lint=lint_cfg,
                ai_review=ai_cfg,
            )
        )

    if errors:
        return ConfigUpdate(local, "rejected", "bad-type", "; ".join(errors))
    if not handins:
        return ConfigUpdate(
            local, "rejected", "invalid-config", "the central configuration defines no hand-ins"
        )

    # A hand-in that exists only in the fork is kept, listed last, and named.
    # A course that renames an id must not make a student's hand-in impossible;
    # the remote entry wins on a collision because it is the current one.
    local_only_ids: list[str] = []
    for handin_type_local in local.handins:
        if handin_type_local.id not in seen_ids:
            handins.append(handin_type_local)
            local_only_ids.append(handin_type_local.id)

    # A section the central file does not mention is not changed: a file with no
    # [[deadline]] must not erase the student's deadline table.
    if "deadline" in remote_raw:
        deadlines_raw = remote_raw.get("deadline", [])
        if not isinstance(deadlines_raw, list):
            return ConfigUpdate(local, "rejected", "bad-type", "[[deadline]] must be a list of sections")
        deadlines: list[Deadline] = []
        for entry in deadlines_raw:
            if not isinstance(entry, dict):
                return ConfigUpdate(local, "rejected", "bad-type", "every [[deadline]] must be a section")
            deadlines.append(
                Deadline(
                    date=_remote_str(entry, "date", "", "[[deadline]] date", errors),
                    handins=_remote_str_list(entry, "handins", [], "[[deadline]] handins", errors),
                    message=_remote_str(entry, "message", "", "[[deadline]] message", errors),
                )
            )
        if errors:
            return ConfigUpdate(local, "rejected", "bad-type", "; ".join(errors))
    else:
        deadlines = list(local.deadlines)

    if ignored_commands:
        notes.append(_note(
            "The central configuration also sets check or validation commands. "
            "Those are never taken from the network and were ignored.",
            "Kursens centrala inställningar anger också kommandon för kontroller. "
            "Sådana hämtas aldrig över nätet och användes inte.",
        ))
    # Compare against the version actually RUNNING, not against the local pin.
    # The shipped handin.toml leaves the pin empty, so comparing to it made any
    # central value at all -- including one the running script already satisfies
    # -- print an unactionable "download the latest version" on every run. A nag
    # that is always on is a nag nobody reads when it finally matters.
    if remote_min_version and _is_newer_version(remote_min_version, script_version):
        notes.append(_note(
            f"The course asks for hand-in script version {remote_min_version} or later. "
            "Please download the latest version from your course page.",
            f"Kursen kräver version {remote_min_version} eller senare av inlämningsskriptet. "
            "Hämta den senaste versionen enligt kurssidan.",
        ))
    if local_only_ids:
        notes.append(_note(
            "Only in your local copy, and possibly removed from the course: "
            + ", ".join(local_only_ids),
            "Finns bara i din lokala kopia och kan ha tagits bort ur kursen: "
            + ", ".join(local_only_ids),
        ))

    effective = HandinConfig(
        course=CourseConfig(
            # Local-only: identity, and the host the student's token is sent to.
            name=local.course.name,
            display_name=display_name,
            gitlab_url=local.course.gitlab_url,
            language=language,
            # Local-only: it says which repository is the student's own, and a
            # fetched file must not be able to widen that.
            expected_origin_paths=list(local.course.expected_origin_paths),
            # Local-only for the same reason: a fetched file that could point
            # this at another project could bless any repository as "yours".
            upstream_project_id=local.course.upstream_project_id,
        ),
        # Local-only: token cache and OAuth client.
        auth=local.auth,
        defaults=defaults,
        handins=handins,
        deadlines=deadlines,
        # Local-only, and the reason is not symmetry with the two above. The
        # confirmation is a COURSE RULE with a disciplinary purpose; letting a
        # fetched file switch it off (or say `enabled` was simply absent from
        # the central file) would silently remove a step the guide promises the
        # student. A central file that omits [confirmation] must not disable it.
        confirmation=local.confirmation,
    )

    status = "unchanged" if effective == local else "applied"
    return ConfigUpdate(effective, status, "", "", notes, local_only_ids)


def apply_config_update(
    config: HandinConfig,
    timeout: float = _UPDATE_TIMEOUT_SECONDS,
    script_version: str = "",
) -> ConfigUpdate:
    """Consult the course's central configuration and return the effective one.

    Never raises, never exits, and never returns None: on any failure the local
    configuration is returned with a reason. A hand-in tool that stops working
    in the deadline hour is worse than one that ignores a configuration.

    Args:
        config: The configuration loaded from the student's own copy.
        timeout: Total budget for the fetch, in seconds.

    Returns:
        A ConfigUpdate whose ``config`` is the configuration to use.
    """
    url = config.defaults.config_update_url
    if not url:
        return ConfigUpdate(config, "disabled")

    text, reason, detail = fetch_remote_config_text(url, timeout)
    if text is None:
        return ConfigUpdate(config, "rejected", reason, detail)

    try:
        remote_raw = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 - see below; a hand-in must not die here
        # NOT just TOMLDecodeError. tomllib parses nested structures recursively,
        # so ~2000 nested brackets -- about 4 KB, far under the 1 MiB cap --
        # raise RecursionError, which is a RuntimeError and not a
        # TOMLDecodeError. Before this was widened, a truncated or corrupted
        # central file killed the tool with a traceback before the banner, on
        # every run, for every student, including in the deadline hour. That is
        # the one failure mode the whole design says cannot happen.
        return ConfigUpdate(config, "rejected", "invalid-toml", f"the central configuration is not valid TOML ({exc})")

    try:
        return _apply_remote(config, remote_raw, script_version)
    except Exception as exc:  # noqa: BLE001 - a hand-in must not die on a fetched file
        return ConfigUpdate(config, "rejected", "invalid-config", f"the central configuration could not be used ({exc})")


# The student-facing frame of `describe_config_update`, per course language
# (2.17.0; until then it was English in a Swedish run). The DETAIL of a
# rejection is technical and English (an exception text, a TOML error), so a
# Swedish run gives one plain sentence per reason code and keeps the English
# detail in parentheses for whoever helps the student.
_UPDATE_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "rejected": "  Note: could not use the course's central configuration: {detail}",
        "continuing": "  Continuing with the configuration in your own repository.",
        "applied": "  Using the course's current configuration (fetched from the course server).",
    },
    "sv": {
        "rejected": "  Obs: kursens centrala inställningar gick inte att använda: {detail}",
        "continuing": "  Inlämningen fortsätter med inställningarna i ditt eget repo.",
        "applied": "  Använder kursens aktuella inställningar (hämtade från kursens server).",
    },
}

_REJECTED_REASON_SV: dict[str, str] = {
    "unreachable": "kursens server gick inte att nå",
    "timeout": "kursens server svarade inte i tid",
    "too-slow": "kursens server svarade för långsamt",
    "too-large": "inställningsfilen är för stor",
    "redirect": "kursens server skickade vidare till en annan adress",
    "not-https": "adressen till inställningarna är inte https",
    "html": "adressen gav en webbsida i stället för en inställningsfil",
    "not-utf8": "inställningsfilen är inte giltig text",
    "invalid-toml": "inställningsfilen innehåller ett fel",
    "invalid-config": "inställningsfilen innehåller ett fel",
    "bad-type": "inställningsfilen innehåller ett fel",
    "wrong-course": "inställningarna gäller en annan kurs",
}


class _Note(str):
    """A note for the student: English as the string itself, Swedish alongside.

    A `str`, so every caller that reads `update.notes` as text keeps working.
    """

    sv: str

    def __new__(cls, en: str, sv: str) -> Self:
        obj = super().__new__(cls, en)
        obj.sv = sv
        return obj


def _note(en: str, sv: str) -> _Note:
    return _Note(en, sv)


def describe_config_update(update: ConfigUpdate, language: str = "en") -> list[str]:
    """Lines to show the student about the central configuration.

    Args:
        update: What consulting the central configuration came to.
        language: The course language ("en" or "sv"); anything else is English.
    """
    table = _UPDATE_TEXT.get(language, _UPDATE_TEXT["en"])
    lines: list[str] = []
    if update.status == "rejected":
        detail = update.detail
        if language == "sv":
            reason = _REJECTED_REASON_SV.get(update.reason, "inställningsfilen gick inte att använda")
            detail = f"{reason} ({update.detail})" if update.detail else reason
        lines.append(table["rejected"].format(detail=detail))
        lines.append(table["continuing"])
    elif update.status == "applied":
        lines.append(table["applied"])
    for note in update.notes:
        text = getattr(note, "sv", "") if language == "sv" else ""
        lines.append(f"  {text or note}")
    return lines
