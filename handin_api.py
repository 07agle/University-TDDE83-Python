"""A minimal GitLab REST client built on the standard library only.

This module exists so that the hand-in tool can run as ``python3 handin.py``
from week one of the course. The tool used to reach GitLab through
``python-gitlab``, which is resolved by the PEP 723 header and therefore only
by ``uv run handin.py`` — but the first hand-in happens a week before uv is
taught, so the tool had to stop needing it.

Nothing here is general-purpose. It covers exactly the ten endpoints the tool
uses, and it is deliberately explicit about the four things ``requests`` and
``python-gitlab`` did for free and that go wrong silently when they are gone:

1. **Pagination.** ``get_paged`` follows ``x-next-page`` instead of counting
   pages, deduplicates on ``id``, checks the total against ``X-Total`` when the
   header is there, and raises when its page cap is hit rather than quietly
   returning a short list. A client that reads page 1 and stops looks correct
   in every test and only fails in production, once a project has more issues
   than one page.
2. **The error taxonomy.** "This does not exist" must stay apart from "I could
   not ask": a timeout reported to a student as a missing repository sends them
   chasing the wrong problem. Everything is decided on exception type and
   ``HTTPError.code`` — never on the text of an exception. ``urllib``'s
   ``URLError`` around a DNS miss says "Name or service not known", and a proxy
   error or an SSL failure can easily contain the words "not found".
3. **Redirects.** ``urlopen`` follows them silently, so an instance answering
   302 towards a login page yields ``200`` with HTML and a traceback out of
   ``json.loads``. This client refuses redirects and checks the content type.
4. **Timeouts.** ``urlopen`` without ``timeout=`` inherits the socket default,
   which is infinite.

Every method takes an explicit timeout, retries 429 with ``Retry-After`` up to
a cap, and never retries a POST that may already have been received.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

__all__ = [
    "ApiAuth",
    "ApiClient",
    "ApiError",
    "ApiNotFound",
    "ApiRateLimited",
    "ApiTransport",
    "encode_path",
]

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
# 429 gets its own, much larger budget. python-gitlab's baseline was
# `obey_rate_limit=True, max_retries=10`, and the scenario it exists for is the
# hour before a deadline when ~100 pairs run the tool against the same project.
# That is the one case where giving up early hits everyone at once, so the
# transport budget (3) must not be reused for it.
DEFAULT_RATE_LIMIT_RETRIES = 10
# A hard stop for get_paged. 200 pages of 100 is 20 000 issues; a hand-in
# project that large means something is wrong, and stopping loudly is the point.
MAX_PAGES = 200


# ─── Errors ─────────────────────────────────────────────────────────────────
#
# Four types, because the tool has four different things to say to a student.
# Do not collapse them: `except Exception` plus a string test is exactly the
# construction this module was written to remove.


class ApiError(Exception):
    """Base class for every failure this client reports."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ApiNotFound(ApiError):
    """404 — the thing genuinely is not there (or is masked as absent).

    GitLab masks "you may not see this" as 404 by design, so this also covers
    the case where the object exists but the caller has no visibility. Both
    have the same remedy from the student's side.
    """


class ApiAuth(ApiError):
    """401/403 — the token is invalid, expired, or lacks the scope."""


class ApiRateLimited(ApiError):
    """429 that survived every retry."""


class ApiTransport(ApiError):
    """Could not ask at all: DNS, TLS, timeout, redirect, 5xx, bad body.

    This is the class that must never be reported to a student as "missing".
    """


class ReadOnlyViolation(RuntimeError):
    """A write was attempted on a client that was made read-only.

    Deliberately NOT an ApiError: the callers catch ApiError and report it as
    "GitLab said no", which would let a dry run carry on as if the write had
    merely failed. A write on a read-only client is a bug in the caller, and it
    must stop the run before anything leaves the machine.
    """


def encode_path(value: str) -> str:
    """URL-encode one path component, slashes included.

    ``gl.projects.get("namespace/project")`` encoded the slash to ``%2F``. An
    f-string does not, and the result is a 404 — or, worse, a different
    project.
    """
    return quote(str(value), safe="")


# How the TLS context got its root certificates, for the run log.
TLS_SOURCE = ""
_TLS_CONTEXT: ssl.SSLContext | None = None

# Called with TLS_SOURCE once the context has been built (the run log).
TLS_LOG_HOOK: Any = None

_MACOS_KEYCHAINS = ("/System/Library/Keychains/SystemRootCertificates.keychain",)


def _load_pem_bundle(context: ssl.SSLContext, pem: str) -> int:
    """Add each PEM certificate in ``pem`` to ``context``; return how many loaded.

    One at a time, because ``load_verify_locations(cadata=...)`` rejects the
    whole bundle when a single entry is malformed.
    """
    loaded = 0
    marker = "-----END CERTIFICATE-----"
    for part in pem.split(marker):
        start = part.find("-----BEGIN CERTIFICATE-----")
        if start < 0:
            continue
        try:
            context.load_verify_locations(cadata=part[start:] + marker + "\n")
            loaded += 1
        except (ssl.SSLError, ValueError):
            continue
    return loaded


def _macos_keychain_pem() -> str:
    """The root certificates of the macOS system keychains, as PEM text."""
    import subprocess

    chunks: list[str] = []
    for keychain in _MACOS_KEYCHAINS:
        try:
            done = subprocess.run(
                ["/usr/bin/security", "find-certificate", "-a", "-p", keychain],
                capture_output=True,
                text=True,
                encoding="ascii",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            chunks.append(done.stdout)
    return "".join(chunks)


def tls_context() -> ssl.SSLContext:
    """One verifying TLS context for every HTTPS request the tool makes."""
    global _TLS_CONTEXT, TLS_SOURCE
    if _TLS_CONTEXT is not None:
        return _TLS_CONTEXT
    context = ssl.create_default_context()
    TLS_SOURCE = "default"
    try:
        empty = context.cert_store_stats().get("x509_ca", 0) == 0
    except Exception:
        empty = False
    if empty and sys.platform == "darwin":
        try:
            count = _load_pem_bundle(context, _macos_keychain_pem())
        except Exception:
            count = 0
        if count:
            TLS_SOURCE = f"macOS keychain ({count} roots)"
            empty = False
    if empty:
        try:
            import certifi  # type: ignore[import-not-found]

            context.load_verify_locations(cafile=certifi.where())
            TLS_SOURCE = "certifi"
        except Exception:
            pass
    _TLS_CONTEXT = context
    if TLS_LOG_HOOK is not None:
        try:
            TLS_LOG_HOOK(TLS_SOURCE)
        except Exception:
            pass
    return context


def https_opener(*handlers: Any):
    """``build_opener`` with the tool's TLS context and the given handlers."""
    return build_opener(HTTPSHandler(context=tls_context()), *handlers)


def _tls_failure_text(base_url: str, detail: object) -> str:
    """What to say when the certificate of ``base_url`` could not be verified."""
    tried = f" (root certificates used: {TLS_SOURCE})" if TLS_SOURCE else ""
    return (
        f"Could not verify the TLS certificate for {base_url}{tried}.\n"
        "  On macOS with Python from python.org: open the folder\n"
        "  Applications/Python 3.x in Finder and double-click\n"
        "  'Install Certificates.command', then try again.\n"
        "  On a school, company or antivirus network that inspects HTTPS,\n"
        "  try another network. Never turn certificate checking off.\n"
        f"  Details: {detail}"
    )


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect instead of following it silently."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise ApiTransport(
            f"GitLab answered with a redirect ({code}) to {newurl}. "
            "This usually means the URL in the configuration is not a GitLab API, "
            "or that a proxy is intercepting the request.",
            status=code,
        )


class ApiClient:
    """Everything the hand-in tool needs from the GitLab REST API.

    Args:
        base_url: Instance root, e.g. ``https://gitlab.liu.se``.
        token: The access token.
        token_kind: ``"private"`` for a personal/project access token,
            ``"oauth"`` for a token obtained through the device flow. This
            decides the header, and the two are NOT interchangeable — see
            ``_auth_header``.
        timeout: Seconds per request. Never None: the socket default is
            infinite.
        retries: Attempts for a retriable failure, the first one included.
        trace: Write an outgoing-request log to stderr. Off by default. The
            token is masked, but note that this is the client's own account of
            what it decided to send — it is not a witness of the bytes on the
            wire. For that, use ``HTTPSHandler(debuglevel=1)``.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        token_kind: str = "private",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        trace: bool = False,
    ) -> None:
        if token_kind not in ("private", "oauth"):
            raise ValueError(f"unknown token_kind: {token_kind!r}")
        self.base_url = base_url.rstrip("/")
        self.api_root = f"{self.base_url}/api/v4"
        self.token = token
        self.token_kind = token_kind
        self.timeout = timeout
        self.retries = max(1, retries)
        self.rate_limit_retries = max(1, rate_limit_retries)
        self.trace = trace
        self._opener = https_opener(_NoRedirect)
        # Filled by authenticate(); the tool reads gl.user.username / .id in
        # five places and those must keep working.
        self.user: dict[str, Any] = {}
        # Set when a paginated read could not verify itself against X-Total.
        self.last_total_check: str = "not-run"
        # Called as event_hook(method, url_without_query, status) after every
        # attempt, status being the HTTP code or the exception's class name.
        # The hand-in tool writes it to its run log. Never allowed to raise
        # into a request.
        self.event_hook: Any = None
        # True for a dry run: every request other than GET is refused in
        # `_open`, the one place every attempt passes through, before a socket
        # is opened. The tool also avoids the writes step by step; this is the
        # guarantee underneath, so that a step that forgets cannot write.
        self.read_only = False

    # ── Headers ─────────────────────────────────────────────────────────────

    def _auth_header(self) -> tuple[str, str]:
        """Pick the authentication header from the token's SOURCE.

        A personal or project access token goes in ``PRIVATE-TOKEN``; an OAuth
        token goes in ``Authorization: Bearer``. The old code sent
        ``private_token=`` for both, including for tokens that came out of the
        device flow.

        That measurement does NOT license sending an OAuth token in
        ``PRIVATE-TOKEN``: it says nothing about the other direction, which
        cannot be measured without a browser login. GitLab documents
        ``Authorization: Bearer`` for OAuth tokens, choosing per source costs
        nothing, and getting it wrong is a silent 401 in front of a student. So
        the choice stays, as a construction decision.
        """
        if self.token_kind == "oauth":
            return ("Authorization", f"Bearer {self.token}")
        return ("PRIVATE-TOKEN", self.token)

    def _log(self, method: str, url: str, body: bytes | None) -> None:
        if not self.trace:
            return
        shown = ""
        if body is not None:
            shown = body[:400].decode("utf-8", "replace")
            if self.token and self.token in shown:
                shown = shown.replace(self.token, "[MASKED]")
        safe_url = url.replace(self.token, "[MASKED]") if self.token else url
        print(f"[api] {method} {safe_url} {shown}", file=sys.stderr)

    def _event(self, method: str, url: str, status: Any) -> None:
        hook = self.event_hook
        if hook is None:
            return
        try:
            hook(method, _strip_query(url), status)
        except Exception:
            pass

    # ── One request ─────────────────────────────────────────────────────────

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{self.api_root}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{self._encode_params(params)}"
        return url

    @staticmethod
    def _encode_params(params: dict[str, Any]) -> str:
        """Encode a query string the way the old client did.

        * ``labels`` went out COMMA-JOINED and percent-encoded
          (``labels=stdlib-scratch%2Clab-a3``), because python-gitlab types it
          as a comma-separated attribute. Callers here pass a string and join
          it themselves.
        * ``assignee_username=[name]`` went out as a plain repeated parameter,
          i.e. ``assignee_username=<name>`` for one element — NOT
          ``assignee_username[]=``. The plan predicted brackets; the wire says
          otherwise, and the wire wins, because the size of the returned set
          decides whether an old hand-in is superseded or duplicated.

        ``doseq=True`` reproduces both: a list becomes repeated keys, a string
        stays one key.
        """
        # Booleans go out lowercase. `urlencode` renders True as "True", which
        # GitLab does not accept; python-gitlab normalised this and no call
        # site relies on it today, which is exactly when it is cheap to fix.
        cleaned: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                cleaned[key] = "true" if value else "false"
            else:
                cleaned[key] = value
        return urlencode(cleaned, doseq=True)

    def _open(
        self,
        method: str,
        url: str,
        body: bytes | None,
        content_type: str | None,
    ) -> tuple[int, dict[str, str], bytes]:
        """One attempt. Translates every failure into an Api* error.

        Raises:
            ReadOnlyViolation: If the client is read-only and ``method`` is
                anything but GET. Nothing has been sent when it is raised.
        """
        if self.read_only and method != "GET":
            raise ReadOnlyViolation(
                f"refused {method} {_strip_query(url)}: this run is read-only (dry run)"
            )
        req = Request(url, data=body, method=method)
        name, value = self._auth_header()
        req.add_header(name, value)
        if content_type:
            req.add_header("Content-Type", content_type)
        req.add_header("Accept", "application/json")
        self._log(method, url, body)

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                self._event(method, url, resp.status)
                return resp.status, headers, resp.read()
        except HTTPError as exc:
            self._event(method, url, exc.code)
            # An HTTPError is also a file-like object. GitLab's own message is
            # in the body, it disappears if it is not read, and it can only be
            # read once.
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            text = raw.decode("utf-8", "replace")[:500]
            headers = {}
            try:
                headers = {k.lower(): v for k, v in exc.headers.items()}
            except Exception:
                pass
            code = exc.code
            if code == 404:
                raise ApiNotFound(f"404 for {method} {_strip_query(url)}", 404, text) from exc
            if code in (401, 403):
                raise ApiAuth(f"{code} for {method} {_strip_query(url)}", code, text) from exc
            if code == 429:
                err = ApiRateLimited(f"429 for {method} {_strip_query(url)}", 429, text)
                err.retry_after = _retry_after(headers)  # type: ignore[attr-defined]
                raise err from exc
            # Keep GitLab's own sentence in the message, not only in `.body`.
            # The tool prints this straight to the student (chain_fork_unknown
            # passes `details=str(exc)[:120]`), and the old client relayed the
            # server's explanation there. Dropping it would be a real
            # regression in what the student is told.
            # GitLab's sentence goes FIRST. `chain_fork_unknown` prints
            # `str(exc)[:120]`, so a long "HTTP 400 for GET <url>" prefix would
            # push the only useful part past the cut and hand the student a
            # truncated URL instead of an explanation.
            detail = _gitlab_message(text)
            where = f"HTTP {code} for {method} {_strip_query(url)}"
            raise ApiTransport(f"{detail} ({where})" if detail else where, code, text) from exc
        except ApiError:
            raise
        except ssl.SSLCertVerificationError as exc:
            # Not reachable on the lab machines (IDA runs Linux, whose CPython
            # uses the system store), but very reachable on a student's own
            # macOS where `Install Certificates.command` was never run. Say so
            # instead of dying on a raw traceback.
            raise ApiTransport(_tls_failure_text(self.base_url, exc)) from exc
        except http.client.HTTPException as exc:
            self._event(method, url, f"{type(exc).__name__}")
            raise ApiTransport(
                f"The answer from {self.base_url} was cut off ({type(exc).__name__}).", None, ""
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._event(method, url, f"{type(exc).__name__}: {reason}")
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ApiTransport(_tls_failure_text(self.base_url, reason)) from exc
            raise ApiTransport(
                f"Could not reach {self.base_url}: {reason}", None, ""
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        """A request with retries, and the parsed body.

        Retry policy: GET and PUT are idempotent and are retried on 429, on a
        5xx and on a transport failure. POST is retried ONLY when the failure
        demonstrably happened before anything was sent — a retried POST that
        actually arrived creates a duplicate issue, which is worse than the
        error it was trying to hide.
        """
        url = self._build_url(path, params)
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = content_type or "application/json"

        last: ApiError | None = None
        transport_left = self.retries
        rate_left = self.rate_limit_retries
        while True:
            try:
                status, headers, raw = self._open(method, url, body, content_type)
                return status, headers, _parse_body(headers, raw, method, url)
            except ApiRateLimited as exc:
                last = exc
                rate_left -= 1
                if rate_left <= 0:
                    raise
                wait = min(getattr(exc, "retry_after", 5.0), 60.0)
                # Say it out loud. A silent 60-second sleep in front of a
                # student who is submitting minutes before a deadline reads as
                # a hung tool, and the next thing they do is Ctrl-C.
                print(
                    f"  GitLab is rate limiting requests; waiting {wait:.0f}s "
                    f"and trying again ({rate_left} attempts left).",
                    file=sys.stderr,
                )
                time.sleep(wait)
            except ApiTransport as exc:
                last = exc
                transport_left -= 1
                if transport_left <= 0 or not _retriable(method, exc):
                    raise
                time.sleep(2.0 * (self.retries - transport_left))
        assert last is not None
        raise last

    # ── Public surface ──────────────────────────────────────────────────────

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)[2]

    def get_with_headers(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, str], Any]:
        _, headers, data = self._request("GET", path, params=params)
        return headers, data

    def post(self, path: str, json_body: Any) -> Any:
        return self._request("POST", path, json_body=json_body)[2]

    def put(self, path: str, json_body: Any) -> Any:
        """PUT exactly the fields given — nothing else."""
        return self._request("PUT", path, json_body=json_body)[2]

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def get_paged(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
    ) -> list[Any]:
        """Read every page, following ``x-next-page``.

        Three properties matter, and each one exists because its absence is
        silent:

        * The loop follows the header instead of incrementing a counter, so it
          stops when GitLab says there is nothing more.
        * Results are deduplicated on ``id``. Offset pagination over a set that
          changes while it is being read gives both duplicates and gaps; the
          dedup removes half of that and the ``X-Total`` check below reports
          the other half.
        * ``X-Total`` is a free oracle when it is there — but GitLab omits it
          above 10 000 records and keyset pagination answers with ``Link`` and
          no ``X-Total`` at all. A missing header is logged and the check is
          skipped. It is NEVER read as zero.
        """
        query = dict(params or {})
        query["per_page"] = per_page
        query["page"] = 1

        out: list[Any] = []
        seen: set[Any] = set()
        expected: int | None = None
        pages = 0

        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise ApiTransport(
                    f"get_paged({path}) passed {MAX_PAGES} pages without an end. "
                    "Refusing to continue and refusing to return a partial list."
                )
            headers, data = self.get_with_headers(path, query)
            if not isinstance(data, list):
                raise ApiTransport(f"Expected a list from {path}, got {type(data).__name__}")
            if expected is None:
                raw_total = headers.get("x-total")
                expected = int(raw_total) if raw_total and raw_total.isdigit() else None

            for item in data:
                key = item.get("id") if isinstance(item, dict) else item
                if key is None:
                    out.append(item)
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)

            nxt = (headers.get("x-next-page") or "").strip()
            if not nxt:
                break
            if not nxt.isdigit():
                # Anything else is a broken or proxied answer. A bare
                # ValueError here would escape the Api* taxonomy entirely and
                # surface as a traceback inside an `except ApiError` handler.
                raise ApiTransport(
                    f"get_paged({path}) got a non-numeric x-next-page: {nxt!r}"
                )
            query["page"] = int(nxt)

        if expected is None:
            self.last_total_check = "skipped: no X-Total header"
        elif expected != len(out):
            self.last_total_check = f"mismatch: X-Total={expected}, unique read={len(out)}"
            # This is the oracle that catches a lost page, and a lost page here
            # means a duplicate hand-in instead of a supersede — silently, with
            # 200 OK. A detector that writes only to an attribute nobody reads
            # is not a detector, so it says so. stderr, not stdout, so an
            # output diff still measures the tool and not the warning.
            print(
                f"  Warning: read {len(out)} items from {path} but GitLab reported "
                f"{expected}. The list below may be incomplete.",
                file=sys.stderr,
            )
        else:
            self.last_total_check = f"ok: {len(out)} == X-Total"
        return out

    def upload(self, project_id: Any, file_path: Path) -> dict[str, Any]:
        """POST a file to ``/projects/:id/uploads`` as multipart/form-data."""
        data = file_path.read_bytes()
        boundary = secrets.token_hex(16)
        name = file_path.name.replace('"', "").replace("\r", "").replace("\n", "")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"

        parts: list[bytes] = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode("utf-8"),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        return self._request(
            "POST",
            f"projects/{encode_path(project_id)}/uploads",
            raw_body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )[2]

    # ── Convenience wrappers the tool actually calls ────────────────────────

    def authenticate(self) -> dict[str, Any]:
        """``GET /user``: validate the token and cache the identity.

        Replaces ``gl.auth()`` plus the ``gl.user`` attribute. Raises
        ``ApiAuth`` when the token is bad and ``ApiTransport`` when the
        question could not be asked at all — the tool tells the student two
        different things in the two cases.
        """
        self.user = self.get("user")
        return self.user

    def get_project(self, project: Any) -> dict[str, Any]:
        """A project by numeric id or by ``namespace/path``."""
        return self.get(f"projects/{encode_path(project)}")

    def list_issues(self, project: Any, **filters: Any) -> list[dict[str, Any]]:
        return self.get_paged(f"projects/{encode_path(project)}/issues", filters)

    def get_issue(self, project: Any, iid: Any) -> dict[str, Any]:
        return self.get(f"projects/{encode_path(project)}/issues/{encode_path(iid)}")

    def create_issue(self, project: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"projects/{encode_path(project)}/issues", payload)

    def update_issue(self, project: Any, iid: Any, delta: dict[str, Any]) -> dict[str, Any]:
        return self.put(f"projects/{encode_path(project)}/issues/{encode_path(iid)}", delta)

    def create_note(self, project: Any, iid: Any, body: str) -> dict[str, Any]:
        return self.post(
            f"projects/{encode_path(project)}/issues/{encode_path(iid)}/notes", {"body": body}
        )

    def list_users(self, **filters: Any) -> list[dict[str, Any]]:
        """``GET /users``.

        Deliberately NOT paginated: the two call sites both want a bounded
        first page (a username lookup returns at most one hit, and the
        "did you mean" search is a courtesy with a per_page cap). Reading every
        user on the instance would be a different, much more expensive
        question.
        """
        return self.get("users", filters)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _strip_query(url: str) -> str:
    """A URL safe to print: no query string, so no usernames in an error."""
    return url.split("?", 1)[0]


def _gitlab_message(text: str) -> str:
    """GitLab's own explanation out of an error body, if there is one.

    GitLab answers errors as ``{"message": …}`` or ``{"error": …}``; the value
    is sometimes a string and sometimes a dict of field errors. Anything that
    does not parse is dropped rather than pasted raw into a student's terminal.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        plain = " ".join(text.split())
        if plain and len(plain) <= 200 and "<" not in plain:
            return plain
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("message", "error", "error_description"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and value:
            return "; ".join(f"{k}: {v}" for k, v in value.items())
    return ""


def _retry_after(headers: dict[str, str]) -> float:
    raw = headers.get("retry-after", "")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 5.0


def _retriable(method: str, exc: ApiTransport) -> bool:
    """Whether this failure may be tried again.

    GET and PUT are idempotent: retry a 5xx or a transport error.

    POST is the dangerous one. A POST that reached GitLab and created an issue,
    and whose RESPONSE was lost, looks exactly like a POST that never arrived.
    Retrying it produces two issues for one hand-in, and the TA then sees two
    submissions with no way to tell which counts. So POST is retried only for
    failures that provably happened before any byte was sent: name resolution
    and connection refused.
    """
    if method in ("GET", "PUT", "DELETE"):
        return exc.status is None or exc.status >= 500
    if method == "POST":
        cause = exc.__cause__
        reason = getattr(cause, "reason", None)
        return isinstance(reason, (socket.gaierror, ConnectionRefusedError))
    return False


def _parse_body(headers: dict[str, str], raw: bytes, method: str, url: str) -> Any:
    """Parse a JSON body, after checking that it IS one.

    Without the content-type check, an instance that answers with an HTML login
    page — or a captive portal on a student's network — produces a traceback
    out of ``json.loads`` instead of a sentence the student can act on.
    """
    if not raw:
        return None
    ctype = headers.get("content-type", "")
    if "json" not in ctype.lower():
        raise ApiTransport(
            f"Expected JSON from {method} {_strip_query(url)} but got "
            f"'{ctype or 'no content type'}'. This usually means the request was "
            "answered by something other than the GitLab API — a login page or a "
            "network portal.",
            None,
            raw[:300].decode("utf-8", "replace"),
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiTransport(f"Could not parse the answer from {_strip_query(url)}: {exc}") from exc


def client_from_env(
    base_url: str, token: str, token_kind: str = "private", **kwargs: Any
) -> ApiClient:
    """Small helper so scripts and tests build the client the same way."""
    trace = bool(os.environ.get("HANDIN_API_TRACE"))
    return ApiClient(base_url, token, token_kind=token_kind, trace=trace, **kwargs)
