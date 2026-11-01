#!/usr/bin/env python3
"""Hämta nya och rättade filer från kursens labbmall till ditt labbrepo.

Kör så här, i ditt labbrepos rotmapp, där uppdatera.py ligger:

    python3 uppdatera.py        (på Windows: python uppdatera.py)

Skriptet
  * lägger till filer som kursen har lagt till i labbmallen,
  * byter ut kursens filer som du inte har ändrat mot den nya versionen,
  * rör aldrig en labbfil som du har ändrat – kursens nya version sparas i
    stället bredvid, med ändelsen .kursens-version,
  * byter ALLTID ut kursens verktygsfiler (handin.py med tillhörande filer,
    kursens kontrollskript och ruff.toml) mot kursens version, också om du
    har ändrat dem – din version sparas då bredvid, med
    ändelsen .din-version. Det här skriptet självt byts ut utan att en kopia
    sparas,
  * raderar aldrig något,
  * committar det det har hämtat och pushar till ditt labbrepo på Gitlab.

Det visar vad det tänker göra och frågar innan det ändrar något.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = "1.1.3"

GITLAB_HOST = "gitlab.liu.se"
TEMPLATE_PATHS = {
    "tdde82": "python-d-u/tdde82-student-base.git",
    "tdde83": "python-d-u/tdde83-student-base.git",
}
TEMPLATE_BRANCH = "main"
TEMPLATE_REF = "refs/kursmall/main"

COURSE_OWNED = frozenset(
    {
        "handin.py",
        "handin_api.py",
        "handin_config.py",
        "handin.toml",
        "uppdatera.py",
        "check_docstrings.py",
        "check_submission.py",
        # The lint rules are the course's: the hand-in checks run Ruff with
        # this file, so a student's edited copy must not survive an update.
        "ruff.toml",
    }
)
SELF_NAME = "uppdatera.py"

# Placeholder files that only keep an empty folder in git. Never worth a word.
QUIET_NAMES = frozenset({".gitkeep"})

COURSE_SUFFIX = ".kursens-version"
STUDENT_SUFFIX = ".din-version"
COMMIT_MESSAGE = "Hämtar kursens nya filer ur labbmallen"

# Only the tests set this: a file:// URL to a local template repository.
TEST_URL_VARIABLE = "UPPDATERA_MALL_URL"

REGULAR_MODES = ("100644", "100755")

# Inside .git/, so it is never committed: which course versions have already
# been saved beside a student's file. Without it every run would offer the
# same side copy again, also after the student has dealt with it and removed it.
SHOWN_FILE = "kursmall-visade"


class Stop(Exception):
    """A situation the student has to handle; the message says how."""


class GitFailed(Exception):
    def __init__(self, command: list[str], error: str) -> None:
        super().__init__(error)
        self.command = command
        self.error = error


def hide_login(text: str) -> str:
    """Remove a user name or password from every URL in `text`.

    Students show the output to TAs, often as a screenshot, and an
    HTTPS address can carry a token. Cut at the LAST @ before the path, as
    git does: a user name that is an e-mail address
    (`https://name@liu.se:TOKEN@host/...`) would otherwise leave the token.
    """
    return re.sub(r"(\b[a-z][a-z0-9+.-]*://)[^/\s]*@", r"\1", text, flags=re.I)


def last_line(error: str) -> str:
    lines = [line for line in error.splitlines() if line.strip()]
    return hide_login(lines[-1]) if lines else "(inget felmeddelande)"


# ── git ──────────────────────────────────────────────────────────────────────


def git(repo: Path | None, *args: str, check: bool = True, binary: bool = False):
    """Run git and return stdout (text, or bytes with binary=True)."""
    command = ["git"]
    if repo is not None:
        command += ["-C", str(repo)]
    command += list(args)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            # English git messages: the script quotes them, and a student who
            # searches the web for one finds far more in English. Paths are
            # file names, never patterns: without GIT_LITERAL_PATHSPECS a
            # course file named `*.txt` also matches the student's `notes.txt`.
            env={**os.environ, "LC_ALL": "C", "GIT_LITERAL_PATHSPECS": "1"},
        )
    except FileNotFoundError:
        raise Stop(
            "Programmet git hittades inte. Installera Git enligt kursens\n"
            "Git-introduktion och kör skriptet igen."
        ) from None
    if check and result.returncode != 0:
        raise GitFailed(command, result.stderr.decode("utf-8", "replace").strip())
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", "replace")


def git_ok(repo: Path, *args: str) -> bool:
    try:
        git(repo, *args)
    except GitFailed:
        return False
    return True


def z_entries(data: bytes) -> list[str]:
    return [
        item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item
    ]


def tree_blobs(repo: Path, rev: str) -> dict[str, tuple[str, str]]:
    """Path → (mode, blob) for every regular file in `rev`'s tree."""
    files: dict[str, tuple[str, str]] = {}
    for entry in z_entries(
        git(repo, "ls-tree", "-r", "-z", "--full-tree", rev, binary=True)
    ):
        meta, _, name = entry.partition("\t")
        mode, kind, blob = meta.split()
        if kind == "blob":
            files[name] = (mode, blob)
    return files


def index_blobs(repo: Path) -> dict[str, str]:
    """Path → blob for every file in the index (the staging area)."""
    files: dict[str, str] = {}
    for entry in z_entries(git(repo, "ls-files", "-s", "-z", binary=True)):
        meta, _, name = entry.partition("\t")
        mode, blob, stage = meta.split()
        if stage == "0":
            files[name] = blob
    return files


# ── finding the repository ──────────────────────────────────────────────────


def toplevel(start: Path) -> Path | None:
    try:
        out = git(None, "-C", str(start), "rev-parse", "--show-toplevel").strip()
    except GitFailed:
        return None
    return Path(out) if out else None


def find_repository() -> Path:
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        root = toplevel(start)
        if root is not None:
            return root
    raise Stop(
        "Du står inte i ett labbrepo. Gå till mappen med ditt labbrepo (den\n"
        "mapp där handin.py ligger) och kör skriptet därifrån, till exempel:\n"
        "    cd tdde82-2026-labbar-42-03\n"
        "    python3 uppdatera.py"
    )


def git_dir(repo: Path) -> Path:
    path = Path(git(repo, "rev-parse", "--git-dir").strip())
    return path if path.is_absolute() else repo / path


def check_state(repo: Path) -> str:
    """Refuse states the script must not work in. Return the branch name."""
    gd = git_dir(repo)
    for marker, what in (
        ("MERGE_HEAD", "en sammanslagning (merge)"),
        ("rebase-merge", "en rebase"),
        ("rebase-apply", "en rebase"),
        ("CHERRY_PICK_HEAD", "en cherry-pick"),
        ("REVERT_HEAD", "en revert"),
    ):
        if (gd / marker).exists():
            raise Stop(
                f"Git är mitt i {what} i ditt labbrepo. Gör klart den, eller\n"
                "fråga en handledare, och kör sedan skriptet igen."
            )
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", "HEAD"):
        raise Stop(
            "Ditt labbrepo har inga commits alls. Det ska inte kunna hända med\n"
            "ett labbrepo från kursen – fråga en handledare."
        )
    branch = git(
        repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    ).strip()
    if not branch:
        raise Stop(
            'Ditt labbrepo står inte på någon gren ("detached HEAD"). Gå tillbaka\n'
            "till din vanliga gren med\n"
            "    git switch main\n"
            "och kör skriptet igen."
        )
    for key in ("user.name", "user.email"):
        if not git(repo, "config", key, check=False).strip():
            raise Stop(
                "Git vet inte vad du heter eller vilken e-post du har, och kan\n"
                "därför inte göra en commit. Ställ in det enligt avsnittet\n"
                '"Konfigurera ditt namn och e-post" i kursens Git-introduktion\n'
                "och kör skriptet igen."
            )
    return branch


# ── which course, and where is its template ─────────────────────────────────


def origin_url(repo: Path) -> str:
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    if not url:
        raise Stop(
            'Ditt labbrepo saknar kopplingen till Gitlab ("origin"). Har du\n'
            "klonat det enligt Git-introduktionen? Fråga annars en handledare."
        )
    return url


def detect_course(repo: Path, url: str) -> str:
    """A first guess. `check_is_fork` is what actually proves it."""
    lowered = url.lower()
    found = [course for course in TEMPLATE_PATHS if course in lowered]
    if len(found) == 1:
        return found[0]
    folders = {p.name.lower() for p in repo.iterdir() if p.is_dir()}
    if any(name.startswith("lab-a") for name in folders):
        return "tdde82"
    if any(name.startswith("lab-b") for name in folders):
        return "tdde83"
    raise Stop(
        "Skriptet kan inte avgöra vilken kurs labbrepot hör till. Kör det i\n"
        "ditt labbrepo från kursen, eller fråga en handledare."
    )


# git's own words (LC_ALL=C): Gitlab refused the HTTPS login, or git had no
# login to send and could not ask for one (no terminal, e.g. a run button).
LOGIN_REJECTED = re.compile(r"Authentication failed", re.I)
LOGIN_NOT_ASKED = re.compile(r"could not read (Username|Password)", re.I)

_URL_LIKE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<authority>[^/]+)/", re.I)
_SCP_LIKE = re.compile(r"^(?P<user>[^@/:]+@)?(?P<host>[^:/]+):(?!//)")


def template_url(origin: str, course: str) -> str:
    override = os.environ.get(TEST_URL_VARIABLE)
    if override:
        return override
    path = TEMPLATE_PATHS[course]
    match = _URL_LIKE.match(origin)
    if match and match["scheme"].lower() != "file":
        # Same scheme and host part (user name and port included) as origin,
        # so git logs in the same way as when the student pushes.
        return f"{match['scheme']}://{match['authority']}/{path}"
    match = _SCP_LIKE.match(origin)
    if match:
        # git@gitlab.liu.se:... or an alias from ~/.ssh/config.
        return f"{match['user'] or ''}{match['host']}:{path}"
    return f"https://{GITLAB_HOST}/{path}"


def fetch_template(repo: Path, url: str, origin: str = "") -> None:
    try:
        git(
            repo,
            "fetch",
            "--no-tags",
            "--quiet",
            url,
            f"+{TEMPLATE_BRANCH}:{TEMPLATE_REF}",
        )
    except GitFailed as exc:
        raise Stop(fetch_failed_text(url, origin, exc.error)) from None


def fetch_failed_text(url: str, origin: str, error: str) -> str:
    head = (
        "Det gick inte att hämta kursens labbmall från Gitlab:\n"
        f"    {last_line(error)}\n"
    )
    match = _URL_LIKE.match(url)
    if not match or match["scheme"].lower() != "https":
        return head + GENERAL_ADVICE
    # The bare git message is easily read as "git would have let me retype a
    # wrong password", which git never does within one run, so say what to do.
    userinfo = match["authority"].rpartition("@")[0]
    if LOGIN_REJECTED.search(error) and ":" in userinfo:
        # A password in the address itself: git never asks, and every run
        # fails the same way until the address changes.
        clean = without_login(origin)
        return head + (
            "Adressen till ditt labbrepo (origin) innehåller en inloggning, och\n"
            "Gitlab godtog den inte – till exempel en access-token som har gått\n"
            "ut. Git frågar då aldrig efter en ny. Ta bort inloggningen ur\n"
            "adressen:\n"
            f"    git remote set-url origin {clean}\n"
            "Kör sedan `git pull`, svara med ditt LiU-id och en giltig\n"
            "access-token när Git frågar, och kör skriptet igen."
        )
    if LOGIN_REJECTED.search(error):
        return head + (
            "Gitlab godtog inte inloggningen, och Git frågar inte en gång till.\n"
            "Med HTTPS ska lösenordet vara en access-token, inte ditt\n"
            "LiU-lösenord. Har datorn sparat en token som gått ut blir det\n"
            "samma fel. Kör `git pull` i labbrepot. När Git frågar är\n"
            "användarnamnet ditt LiU-id och lösenordet en giltig access-token –\n"
            'se avsnittet "Om du använder HTTPS i stället för SSH" i\n'
            "Git-introduktionen. Kör sedan skriptet igen."
        )
    if LOGIN_NOT_ASKED.search(error):
        return head + (
            "Git behövde fråga efter din inloggning men kunde inte fråga härifrån.\n"
            "Kör skriptet i en vanlig terminal (inte med en körknapp), så frågar\n"
            "Git. Användarnamnet är ditt LiU-id och lösenordet en access-token –\n"
            'se avsnittet "Om du använder HTTPS i stället för SSH" i\n'
            "Git-introduktionen."
        )
    return head + GENERAL_ADVICE


def without_login(origin: str) -> str:
    """origin without its user name and password, for a command to copy.

    Cut at the LAST @ of the host part, as git does, so that the tail of a
    password that contains an @ is not left in the command.
    """
    match = _URL_LIKE.match(origin)
    if not match:
        return f"https://{GITLAB_HOST}/<ditt labbrepo>.git"
    host = match["authority"].rpartition("@")[2]
    return f"{match['scheme']}://{host}/{origin[match.end() :]}"


GENERAL_ADVICE = (
    "Fungerar `git pull` i ditt labbrepo? Gör det inte är det samma\n"
    "problem – se Git-introduktionen. Fungerar det, fråga en handledare\n"
    "eller kontakta kursen."
)


def check_is_fork(repo: Path) -> str:
    """Prove that the repository was made from the course's template.

    Return the fork point (the newest template commit the repository has).
    """
    # A repository with no commit in common with the template was not made
    # from it. (Any template commit brings its whole history, root included,
    # so a common commit is the whole test.)
    base = git(repo, "merge-base", "HEAD", TEMPLATE_REF, check=False).strip()
    if base:
        return base
    raise Stop(
        "Det här repot är inte ditt labbrepo från kursen – det är inte skapat\n"
        "ur kursens labbmall. Gå till mappen med labbrepot du fick av kursen\n"
        "och kör skriptet där. Är du säker på att det är rätt repo: fråga en\n"
        "handledare."
    )


class Remote:
    def __init__(self, tracking: str, push: list[str], ahead: int) -> None:
        self.tracking = tracking
        self.push = push
        self.ahead = ahead


def check_remote(repo: Path, branch: str) -> Remote:
    """Stop if the branch on Gitlab has commits this computer lacks."""
    try:
        git(repo, "fetch", "--quiet", "origin")
    except GitFailed as exc:
        raise Stop(
            "Det gick inte att kontakta ditt labbrepo på Gitlab:\n"
            f"    {last_line(exc.error)}\n"
            "Kontrollera att du är ansluten till internet och att `git pull`\n"
            "fungerar, och kör skriptet igen."
        ) from None
    upstream = git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    ).strip()
    if upstream:
        tracking, push = upstream, ["push", "--quiet"]
    else:
        # A branch that was never pushed with -u: push it so that it gets an
        # upstream, instead of leaving the student with "no upstream branch".
        tracking, push = f"origin/{branch}", ["push", "--quiet", "-u", "origin", branch]
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", tracking):
        return Remote(tracking, push, 0)
    behind = int(git(repo, "rev-list", "--count", f"HEAD..{tracking}").strip() or "0")
    if behind:
        raise Stop(
            "Ditt labbrepo på Gitlab har commits som inte finns på den här datorn\n"
            "(du har kanske arbetat från en annan dator). Hämta dem först:\n"
            "    git pull\n"
            "och kör sedan skriptet igen. Ingenting är ändrat."
        )
    ahead = int(git(repo, "rev-list", "--count", f"{tracking}..HEAD").strip() or "0")
    return Remote(tracking, push, ahead)


# ── deciding file by file ───────────────────────────────────────────────────


class Plan:
    def __init__(self) -> None:
        self.add: list[str] = []  # new files
        self.replace: list[str] = []  # course files the student never changed
        self.forced: list[str] = []  # course-owned files the student changed
        self.aside: list[str] = []  # student changed it: course version beside
        self.commit_only: list[str] = []  # already right on disk, not committed
        # Course-owned, right on disk and in HEAD, but the index (the staging
        # area) holds another version: restored from HEAD, nothing committed.
        self.unstage: list[str] = []
        self.removed: list[str] = []  # the course has removed it from the template
        self.skipped: list[tuple[str, str]] = []  # (path, reason)
        self.modes: dict[str, str] = {}
        self.blobs: dict[str, str] = {}
        # (disk, index) as measured when the plan was made. apply() measures
        # again just before writing: the student may save a file while the
        # question is shown.
        self.measured: dict[str, tuple[str | None, str | None]] = {}

    def writes(self) -> list[str]:
        return self.add + self.replace + self.forced

    def to_commit(self) -> list[str]:
        return self.writes() + self.commit_only

    def changes(self) -> bool:
        return bool(self.to_commit() or self.aside or self.unstage)


def template_versions(repo: Path, path: str, rev: str = TEMPLATE_REF) -> list[str]:
    """Every blob `path` has had in `rev`'s history, newest first."""
    blobs = []
    out = git(repo, "log", "--format=%H", "--no-renames", rev, "--", path)
    for commit in out.split():
        blob = git(
            repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False
        ).strip()
        if blob and blob not in blobs:
            blobs.append(blob)
    return blobs


def template_history(repo: Path, path: str, rev: str = TEMPLATE_REF) -> set[str]:
    """Every blob `path` has had in `rev`'s history (the template's by default)."""
    return set(template_versions(repo, path, rev))


def exists_exactly(repo: Path, path: str) -> bool:
    """True if the file exists with exactly this spelling.

    On Windows and macOS `Path.exists()` also answers True for `Lab-A5/x.py`
    when asked about `lab-a5/x.py`.
    """
    target = repo / path
    if not target.is_file():
        return False
    try:
        return target.name in os.listdir(target.parent)
    except OSError:
        return False


def read_shown(repo: Path) -> set[tuple[str, str]]:
    try:
        text = (git_dir(repo) / SHOWN_FILE).read_text(encoding="utf-8")
    except OSError:
        return set()
    shown = set()
    for line in text.splitlines():
        blob, _, path = line.partition("\t")
        if path:
            shown.add((blob, path))
    return shown


def remember_shown(repo: Path, entries: list[tuple[str, str]]) -> None:
    if entries:
        with open(git_dir(repo) / SHOWN_FILE, "a", encoding="utf-8") as handle:
            for blob, path in entries:
                handle.write(f"{blob}\t{path}\n")


def ever_in_history(repo: Path, path: str) -> bool:
    return bool(git(repo, "log", "-1", "--format=%H", "HEAD", "--", path).strip())


def same_name_other_case(repo: Path, path: str) -> str | None:
    """An existing file whose name differs from `path` only in case.

    On Windows and macOS writing `test_x.py` would overwrite `Test_x.py`, and
    an untracked file of the student's is in neither HEAD nor the index.
    """
    target = repo / path
    try:
        names = os.listdir(target.parent)
    except OSError:
        return None
    folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    for other in names:
        if other != target.name and other.lower() == target.name.lower():
            return folder + other
    return None


def blocked_by(repo: Path, path: str) -> str | None:
    """Why `path` cannot be written safely, or None.

    A symbolic link anywhere on the way would send the write to another file
    than the one the plan names, and a file where the path needs a folder
    would stop the run halfway, after other files are already written.
    """
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        target = repo / prefix
        if target.is_symlink():
            return f"{prefix} är en symbolisk länk"
        if depth < len(parts) and os.path.lexists(target) and not target.is_dir():
            return f"du har en fil som heter {prefix}"
    return None


def make_plan(repo: Path, fork_point: str) -> Plan:
    plan = Plan()
    shown = read_shown(repo)
    template = tree_blobs(repo, TEMPLATE_REF)
    head = {path: blob for path, (_, blob) in tree_blobs(repo, "HEAD").items()}
    index = index_blobs(repo)
    at_fork = set(tree_blobs(repo, fork_point))
    known_lower = {path.lower(): path for path in set(head) | set(index)}

    for path, (mode, blob) in sorted(template.items()):
        if mode not in REGULAR_MODES:
            continue  # symlinks and submodules: the course's templates have none
        plan.modes[path] = mode
        plan.blobs[path] = blob
        name = path.rsplit("/", 1)[-1]
        other = known_lower.get(path.lower())
        if other is not None and other != path:
            plan.skipped.append((path, f"du har en fil som heter {other}"))
            continue
        reason = blocked_by(repo, path)
        if reason is not None:
            plan.skipped.append((path, reason))
            continue
        if (repo / path).is_dir():
            plan.skipped.append((path, "det finns en mapp med det namnet"))
            continue

        on_disk = (
            git(repo, "hash-object", "--", path).strip()
            if exists_exactly(repo, path)
            else None
        )
        staged = index.get(path)
        committed = head.get(path)
        plan.measured[path] = (on_disk, staged)

        if on_disk is None:
            twin = same_name_other_case(repo, path)
            if twin is not None:
                plan.skipped.append((path, f"du har en fil som heter {twin}"))
            elif (
                path in at_fork
                or committed is not None
                or staged is not None
                or ever_in_history(repo, path)
            ):
                # The student has had it and removed it (also after an
                # earlier run of this script, which is why the fork point
                # alone is not enough).
                if name not in QUIET_NAMES:
                    plan.skipped.append((path, "du har tagit bort den"))
            else:
                plan.add.append(path)
            continue

        versions = template_versions(repo, path)
        history = set(versions)
        if path == SELF_NAME:
            # The running script: the template's copy wins, no side copy.
            if on_disk != blob:
                plan.replace.append(path)
            elif committed != blob:
                plan.commit_only.append(path)
            elif staged != blob:
                # Right on disk and in HEAD, but another version (or its
                # removal) is staged: restored like any other course-owned
                # file, or the next commit brings that version back.
                plan.unstage.append(path)
            continue

        # Replacing or committing a path rewrites its staged and disk versions.
        # Untouched means neither holds anything but the committed version
        # (one the course wrote) or a course version NEWER than it: a run of
        # this script that was interrupted before its commit, or a file the
        # student downloaded from the template, leaves one of those. An OLDER
        # template version is not enough: a student who went back to one has
        # changed the file. Without a committed version, any course version
        # is the course's file (an interrupted run that added it).
        if committed is None:
            allowed = history
        elif committed in history:
            allowed = set(versions[: versions.index(committed) + 1]) | {blob}
        else:
            allowed = set()
        untouched = (committed is None or committed in allowed) and all(
            v is None or v in allowed for v in (on_disk, staged)
        )

        if on_disk == blob:
            if path in COURSE_OWNED and committed == blob and staged != blob:
                # Right on disk and in HEAD, but the student has staged
                # another version (or its removal): the next commit would
                # bring that version back.
                plan.unstage.append(path)
            elif committed != blob:
                if untouched:
                    plan.commit_only.append(path)
                elif path in COURSE_OWNED:
                    plan.forced.append(path)
                else:
                    plan.skipped.append(
                        (path, "du har ändringar i den som inte är committade")
                    )
            continue
        if untouched:
            plan.replace.append(path)
        elif path in COURSE_OWNED:
            # Checked before the history test below: a course-owned file must
            # match the course's version even when the course did not change
            # it, or handin.py and handin.toml drift apart.
            plan.forced.append(path)
        elif blob in template_history(repo, path, "HEAD"):
            # The student has had this very version already and changed it
            # since: there is nothing new from the course in it.
            continue
        elif (blob, path) in shown or (
            # A side copy the course itself is about to overwrite is no proof.
            path + COURSE_SUFFIX not in template
            and same_file(repo, path + COURSE_SUFFIX, template_bytes(repo, path))
        ):
            continue  # this course version has been saved beside it before
        else:
            plan.aside.append(path)

    # Files the course has taken out of the template. Never deleted here; the
    # student is told, because a stale test file can still be run by pytest.
    ever = set(
        z_entries(
            git(
                repo,
                "log",
                "--format=",
                "--name-only",
                "-z",
                "--no-renames",
                TEMPLATE_REF,
                binary=True,
            )
        )
    )
    for path in sorted(ever - set(template)):
        if (
            path in head
            and exists_exactly(repo, path)
            and path.rsplit("/", 1)[-1] not in QUIET_NAMES
        ):
            if git(repo, "hash-object", "--", path).strip() in template_history(
                repo, path
            ):
                plan.removed.append(path)
    return plan


# ── doing it ────────────────────────────────────────────────────────────────


def same_file(repo: Path, name: str, data: bytes) -> bool:
    target = repo / name
    return target.is_file() and target.read_bytes() == data


def free_name(repo: Path, wanted: str, taken: set[str]) -> str:
    """`wanted`, or `wanted-2`, `wanted-3` … – never an existing file.

    Nor a name in `taken` (compared without case, as Windows and macOS do):
    the files the plan is about to write. A course file that happens to be
    called `handin.py.din-version` would otherwise overwrite the copy of the
    student's `handin.py` that was saved under that name a moment before.
    """
    taken_lower = {name.lower() for name in taken}
    candidate, number = wanted, 1
    while os.path.lexists(repo / candidate) or candidate.lower() in taken_lower:
        number += 1
        candidate = f"{wanted}-{number}"
    return candidate


def template_bytes(repo: Path, path: str) -> bytes:
    # --filters applies the same conversion as a checkout of `path` (line
    # endings on Windows, for example), so the file looks like any other.
    return git(repo, "cat-file", "--filters", f"{TEMPLATE_REF}:{path}", binary=True)


def write_file(repo: Path, name: str, data: bytes, mode: str = "100644") -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        # Writing through a link would change the file it points to, which
        # is not the file the plan names. Replace the link itself.
        target.unlink()
    target.write_bytes(data)
    if mode == "100755" and os.name != "nt":
        target.chmod(target.stat().st_mode | 0o111)


def measure(repo: Path, path: str) -> tuple[str | None, str | None]:
    """(disk, index) blobs of `path` right now; None where there is none."""
    on_disk = (
        git(repo, "hash-object", "--", path).strip()
        if exists_exactly(repo, path)
        else None
    )
    return on_disk, index_blobs(repo).get(path)


def recheck(repo: Path, plan: Plan) -> None:
    """Take out of the plan every file that changed after the plan was made.

    The plan is made before the question "Fortsätt?", and a file the student
    saves while it is shown is the student's work: it is never overwritten or
    committed. The course's version goes beside it instead, as for any file
    both have changed.

    A symbolic link put on the way while the question is shown is measured
    here too: `git hash-object` follows the link, so a link to a file with the
    same content would otherwise pass the comparison below.
    """
    # plan.aside too: its side copy is written beside the file, so a folder
    # swapped for a link would send that copy elsewhere.
    for bucket in (plan.add, plan.replace, plan.forced, plan.commit_only, plan.aside):
        for path in list(bucket):
            reason = blocked_by(repo, path)
            if reason is None:
                continue
            bucket.remove(path)
            plan.skipped.append((path, reason))
            print(f"Rörs inte: {path} ({reason})")
    for bucket in (plan.add, plan.replace, plan.commit_only):
        for path in list(bucket):
            if path == SELF_NAME or path not in plan.measured:
                continue
            now = measure(repo, path)
            if now == plan.measured[path]:
                continue
            bucket.remove(path)
            if now[0] is None:
                plan.skipped.append((path, "den ändrades medan frågan visades"))
                print(f"Rörs inte: {path} (den ändrades medan frågan visades)")
            else:
                plan.aside.append(path)
                print(f"Rörs inte: {path} (du sparade den medan frågan visades)")


def apply(repo: Path, plan: Plan) -> list[str]:
    """Write every file. Return the names of the side copies written."""
    recheck(repo, plan)
    sides = []
    # The files the plan writes, and every folder they need.
    taken = set(plan.writes())
    for path in plan.writes():
        parts = path.split("/")
        taken.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    for path in plan.unstage:
        if path in index_blobs(repo):  # an empty staged file is still a version
            side = free_name(repo, path + STUDENT_SUFFIX, taken)
            write_file(repo, side, git(repo, "show", f":{path}", binary=True))
            sides.append(side)
        git(repo, "reset", "--quiet", "--", path)
    for path in plan.forced:
        # The file on disk can already be the course's version, when only
        # the committed or staged version differs: then there is nothing of
        # the student's in it to save.
        if git(repo, "hash-object", "--", path).strip() != plan.blobs[path]:
            side = free_name(repo, path + STUDENT_SUFFIX, taken)
            write_file(repo, side, (repo / path).read_bytes())
            sides.append(side)
        # A staged version that differs from the file on disk is saved too:
        # `git add` below replaces it, and no commit has it.
        staged = git(repo, "show", f":{path}", check=False, binary=True)
        if (
            path in index_blobs(repo)  # an empty staged file is still a version
            and staged != (repo / path).read_bytes()
            and staged != git(repo, "show", f"HEAD:{path}", check=False, binary=True)
        ):
            side = free_name(repo, path + STUDENT_SUFFIX, taken)
            write_file(repo, side, staged)
            sides.append(side)
    for path in plan.writes():
        write_file(repo, path, template_bytes(repo, path), plan.modes[path])
    for path in plan.aside:
        data = template_bytes(repo, path)
        wanted = path + COURSE_SUFFIX
        existing = repo / wanted
        if (
            not existing.is_symlink()
            and wanted not in taken
            and existing.is_file()
            and existing.read_bytes() == data
        ):
            sides.append(wanted)
        else:
            side = free_name(repo, wanted, taken)
            write_file(repo, side, data)
            sides.append(side)
    remember_shown(repo, [(plan.blobs[path], path) for path in plan.aside])
    return sides


def commit(repo: Path, paths: list[str]) -> bool:
    """Commit `paths`. False if they already were as in HEAD."""
    if not paths:
        return False
    # -f: the course's files are added even if the student's own .gitignore
    # happens to cover them (".vscode/", for example).
    git(repo, "add", "-f", "--", *paths)
    # A course-owned file that differed only on disk is now back to HEAD's
    # version: restored, and an empty commit would only be a git error.
    if git_ok(repo, "diff", "--cached", "--quiet", "HEAD", "--", *paths):
        return False
    # Only these paths: work the student has staged is not swept in.
    git(repo, "commit", "--quiet", "-m", COMMIT_MESSAGE, "--", *paths)
    return True


def push(repo: Path, remote: Remote) -> bool:
    try:
        git(repo, *remote.push)
    except GitFailed as exc:
        print()
        print("Pushen till Gitlab misslyckades:")
        print(f"    {last_line(exc.error)}")
        print("Filerna är hämtade och committade på den här datorn.")
        print("Kör de här två kommandona, så hamnar de i ditt labbrepo på Gitlab:")
        print("    git pull")
        print("    git push")
        return False
    return True


# ── talking to the student ──────────────────────────────────────────────────


def show_plan(plan: Plan, remote: Remote) -> None:
    def block(title: str, paths: list[str]) -> None:
        if paths:
            print(title)
            for path in paths:
                print(f"    {path}")

    block("Nya filer som läggs till:", plan.add)
    block("Kursens filer som byts mot en nyare version:", plan.replace)
    block(
        "Kursens filer som du har ändrat. Kursens version används, och din\n"
        f"sparas som <namn>{STUDENT_SUFFIX}:",
        plan.forced,
    )
    block(
        "Filer som du har ändrat, och som kursen också har ändrat. Din fil\n"
        f"RÖRS INTE; kursens version sparas bredvid som <namn>{COURSE_SUFFIX}:",
        plan.aside,
    )
    block(
        "Kursens filer där du har stagat en annan version (git add eller git rm).\n"
        "Kursens version stagas i stället, och en stagad version sparas som\n"
        f"<namn>{STUDENT_SUFFIX}:",
        plan.unstage,
    )
    block(
        "Filer som redan är rätt men inte committade (committas nu):", plan.commit_only
    )
    if plan.to_commit():
        print("Det hämtade committas och pushas till ditt labbrepo på Gitlab.")
        if remote.ahead:
            print(
                f"Dina egna {remote.ahead} commits som inte är pushade än pushas samtidigt."
            )


def show_notes(plan: Plan) -> None:
    for path, reason in plan.skipped:
        print(f"Rörs inte: {path} ({reason})")
    if plan.removed:
        print("Kursen har tagit bort de här filerna ur labbmallen. De ligger kvar hos")
        print("dig; du kan ta bort dem om du vill:")
        for path in plan.removed:
            print(f"    {path}")


def confirm(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input("Fortsätt? [J/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "j", "ja", "y", "yes")


def harden_console() -> None:
    """Never crash on a character the console cannot show.

    On Windows, output that is not a real console (Git Bash in some setups,
    or output sent to a file) uses cp1252, and a folder name like
    C:\\Users\\Łukasz would otherwise end the run with a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        # Two clauses, not `except (A, B)`: a formatter that targets Python
        # 3.14 rewrites a parenthesized tuple into the 3.14-only form without
        # parentheses, which is a SyntaxError on older Python versions, and
        # this script must run on them.
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
        except ValueError:
            pass


def main(argv: list[str] | None = None) -> int:
    harden_console()
    parser = argparse.ArgumentParser(
        prog="uppdatera.py",
        description="Hämta nya och rättade filer från kursens labbmall.",
    )
    parser.add_argument(
        "--ja", action="store_true", help="fråga inte innan ändringarna görs"
    )
    parser.add_argument(
        "--version", action="version", version=f"uppdatera.py {VERSION}"
    )
    args = parser.parse_args(argv)

    written = False
    try:
        repo = find_repository()
        branch = check_state(repo)
        url = origin_url(repo)
        course = detect_course(repo, url)
        print(f"Labbrepo: {repo}")
        print(f"Hämtar kursens labbmall ({course.upper()}) ...")
        fetch_template(repo, template_url(url, course), url)
        fork_point = check_is_fork(repo)
        remote = check_remote(repo, branch)

        plan = make_plan(repo, fork_point)
        show_notes(plan)
        if not plan.changes():
            print("Ditt labbrepo har redan allt från kursens labbmall. Inget att göra.")
            return 0

        print()
        show_plan(plan, remote)
        print()
        if not confirm(args.ja):
            print("Avbrutet. Ingenting är ändrat.")
            return 1

        written = True
        sides = apply(repo, plan)
        committed = commit(repo, plan.to_commit())
        pushed = push(repo, remote) if committed else True
        print()
        if committed and pushed:
            print("Klart. Filerna är hämtade, committade och pushade till Gitlab.")
        elif not committed and (plan.unstage or plan.to_commit()):
            print("Klart. Kursens filer är rätt igen; inget behövde committas.")
        if sides:
            print()
            print("Sparade bredvid (committas inte):")
            for side in sides:
                print(f"    {side}")
        if plan.aside:
            print()
            print(
                f"Filerna som slutar på {COURSE_SUFFIX} är kursens nya version av filer"
            )
            print("som du redan har ändrat. Labbsidan säger vad du ska göra med dem.")
            print("Är du osäker, fråga en handledare.")
        return 0 if pushed else 1
    except Stop as stop:
        print()
        print(stop)
        return 1
    except GitFailed as exc:
        print()
        print("Git gav ett fel som skriptet inte väntade sig:")
        print(f"    {hide_login(' '.join(exc.command))}")
        print(f"    {hide_login(exc.error) or '(inget felmeddelande)'}")
        if written:
            print(
                "Skriptet hann skriva filer men inte göra klart. Kör `git status` och"
            )
            print("visa det här och utskriften för en handledare.")
        else:
            print("Ingenting är ändrat. Visa det här för en handledare.")
        return 1
    except OSError as exc:
        print()
        print("Det gick inte att skriva eller läsa en fil:")
        print(f"    {exc}")
        if written:
            print(
                "Skriptet hann skriva filer men inte göra klart. Kör `git status` och"
            )
            print("visa det här och utskriften för en handledare.")
        else:
            print("Ingenting är ändrat. Visa det här för en handledare.")
        return 1
    except KeyboardInterrupt:
        print()
        print(
            "Avbrutet."
            if not written
            else "Avbrutet mitt i. Kör `git status` och fråga en handledare om något ser konstigt ut."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
