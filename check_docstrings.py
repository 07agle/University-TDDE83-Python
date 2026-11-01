#!/usr/bin/env python3
"""Kontrollera docstringarna i en labbmapp innan ni lämnar in (TDDE83).

Kör så här, i labbrepots rotmapp:

    python3 check_docstrings.py lab-b5
    (på Windows: python check_docstrings.py lab-b5)

Skriptet letar i alla .py-filer i mappen efter publika funktioner och klasser
som saknar docstring, och efter docstringar som inte dokumenterar varje
parameter under Args: och returvärdet under Returns: (kapitel 02 i
studiematerialet för TDDE83). Med --presence-only kontrolleras bara att
docstringarna finns. Det ändrar ingenting. Exitkod 0 betyder att inget
hittades, 1 att något behöver rättas och 2 att inget hittades men att någon
fil inte kunde kontrolleras med den här Python-versionen.

Samma kontroll körs av inlämningsverktyget (handin.py) och av handledarens
labcheck.py.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


_GOOGLE_PARAM_HEADERS = {
    "args",
    "arguments",
    "parameters",
    "params",
    "keyword args",
    "keyword arguments",
    "other parameters",
}
# `Returns: text` på samma rad är Google-stil. `Return:`/`Yield:` godtas bara
# som ensam rubrikrad, så att en prosarad som "Return: värdet …" inte räknas.
_GOOGLE_RETURN_RE = re.compile(r"^(?:Returns\s*:.*|Return\s*:\s*)$")
_GOOGLE_YIELD_RE = re.compile(r"^(?:Yields\s*:.*|Yield\s*:\s*)$")
_GOOGLE_HEADER_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z ]*?)\s*:\s*$")
# `namn: text`, `namn (typ): text`, `*args: text`. Typen får innehålla nästlade
# parenteser (`p (tuple(int, int)):`): det giriga `\(.*\)` backar till den
# parentes som följs av kolon.
_GOOGLE_PARAM_LINE_RE = re.compile(r"^\*{0,2}(?P<name>\w+)\s*(?:\(.*\))?\s*:")
_NUMPY_RULE_RE = re.compile(r"^-{3,}$")
_REST_PARAM_RE = re.compile(
    r"^:(?:param|parameter|arg|argument|key|keyword|kwarg)\s+"
    r"(?:[^:]*\s)?\*{0,2}(?P<name>\w+)\s*:"
)
_REST_RETURN_RE = re.compile(r"^:returns?\s*:")
_REST_YIELD_RE = re.compile(r"^:yields?\s*:")
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _indent(line: str) -> int:
    """Antal inledande blanktecken."""
    return len(line) - len(line.lstrip())


def _is_numpy_header(lines: list[str], idx: int) -> bool:
    """Är rad idx en NumPy-rubrik, alltså följd av en streckrad på samma indrag?"""
    if idx + 1 >= len(lines) or not lines[idx].strip():
        return False
    rule = lines[idx + 1]
    return bool(_NUMPY_RULE_RE.match(rule.strip())) and _indent(rule) == _indent(
        lines[idx]
    )


def docstring_facts(doc: str) -> tuple[set[str], bool, bool]:
    """Läs ut vad en docstring dokumenterar.

    Args:
        doc: Docstringen, som ``ast.get_docstring`` ger den.

    Returns:
        (dokumenterade parameternamn, har retursektion, har yieldsektion).
    """
    lines = doc.expandtabs().splitlines()
    names: set[str] = set()
    has_return = has_yield = False

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        indent = _indent(raw)

        rest_param = _REST_PARAM_RE.match(stripped)
        if rest_param:
            names.add(rest_param.group("name"))
            continue
        if _REST_RETURN_RE.match(stripped):
            has_return = True
            continue
        if _REST_YIELD_RE.match(stripped):
            has_yield = True
            continue

        if _is_numpy_header(lines, idx):
            title = stripped.lower()
            if title == "returns":
                has_return = True
            elif title == "yields":
                has_yield = True
            elif title in {"parameters", "other parameters"}:
                # Parameterraderna står på rubrikens indrag; beskrivningarna
                # står djupare och räknas därför aldrig som namn.
                pos = idx + 2
                while pos < len(lines):
                    body = lines[pos]
                    text = body.strip()
                    if text:
                        if (
                            _indent(body) < indent
                            or text.startswith(">>>")
                            or _is_numpy_header(lines, pos)
                        ):
                            break
                        if _indent(body) == indent:
                            head = text.split(":", 1)[0]
                            for part in head.split(","):
                                part = part.strip().lstrip("*")
                                if re.fullmatch(r"\w+", part):
                                    names.add(part)
                    pos += 1
            continue

        if _GOOGLE_RETURN_RE.match(stripped):
            has_return = True
            continue
        if _GOOGLE_YIELD_RE.match(stripped):
            has_yield = True
            continue
        header = _GOOGLE_HEADER_RE.match(stripped)
        if header and header.group("name").lower() in _GOOGLE_PARAM_HEADERS:
            # Sektionens kropp = raderna med större indrag än rubriken.
            # Parameterraderna står på kroppens FÖRSTA indrag; en fortsättning
            # av en beskrivning står djupare och räknas inte.
            base: int | None = None
            for body in lines[idx + 1 :]:
                text = body.strip()
                if not text:
                    continue
                body_indent = _indent(body)
                if body_indent <= indent:
                    break
                if base is None:
                    base = body_indent
                if body_indent == base:
                    param = _GOOGLE_PARAM_LINE_RE.match(text)
                    if param:
                        names.add(param.group("name"))
    return names, has_return, has_yield


def _own_body_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef):
    """Noder i funktionens EGEN kropp – inte i nästlade def/lambda/class."""
    stack: list[ast.AST] = [s for s in func.body if not isinstance(s, _NESTED_SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, _NESTED_SCOPES)
        )


def _decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Dekoratorernas namn: `x`, `mod.x` och `mod.x(...)` ger alla "x"."""
    out: set[str] = set()
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            out.add(target.id)
        elif isinstance(target, ast.Attribute):
            out.add(target.attr)
    return out


def _is_none_annotation(node: ast.expr | None) -> bool:
    """`None` eller strängannotationen `"None"`."""
    return isinstance(node, ast.Constant) and node.value in (None, "None")


def docstring_structure_gaps(
    func: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool
) -> list[str] | None:
    """Vad funktionens docstring inte dokumenterar.

    Args:
        func: Funktionsnoden.
        is_method: True om funktionen står direkt i en klasskropp; då räknas
            första parametern (self/cls) inte, utom för @staticmethod.

    Returns:
        Luckorna i läsbar form (tom lista = allt som krävs finns), eller None
        om funktionen inte omfattas av kontrollen.
    """
    doc = ast.get_docstring(func)
    if not doc:
        return None
    decorators = _decorator_names(func)
    if decorators & {"setter", "deleter", "fixture"}:
        return None

    args = func.args
    positional = [*args.posonlyargs, *args.args]
    if is_method and "staticmethod" not in decorators and positional:
        positional = positional[1:]
    names = [a.arg for a in positional] + [a.arg for a in args.kwonlyargs]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)

    documented, has_return, has_yield = docstring_facts(doc)
    gaps = [
        f"parametern {name}"
        for name in names
        if not name.startswith("_") and name not in documented
    ]

    if decorators & {"property", "cached_property"}:
        return gaps
    body = list(_own_body_nodes(func))
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in body):
        if not (has_yield or has_return):
            gaps.append("det som genereras (Yields:)")
        return gaps
    if func.returns is not None:
        returns_value = not _is_none_annotation(func.returns)
    else:
        returns_value = any(
            isinstance(n, ast.Return)
            and n.value is not None
            and not _is_none_annotation(n.value)
            for n in body
        )
    if returns_value and not has_return:
        gaps.append("returvärdet (Returns:)")
    return gaps


def iter_structure_candidates(tree: ast.Module):
    """Funktioner och metoder som formkontrollen ska titta på.

    Args:
        tree: Den parsade modulen.

    Yields:
        (funktionsnod, är_metod) för varje funktion som inte är nästlad i en
        annan funktion. Namnfiltret (privata, `test_*`) står hos anroparen.
    """

    def visit(node: ast.AST, in_class: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, in_class
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, True)
            elif not isinstance(child, ast.Lambda):
                yield from visit(child, in_class)

    yield from visit(tree, False)


# --- Vilka filer och vilka definitioner ---------------------------------------

# Kataloger som aldrig är studentens kod. labcheck.py använder samma mängd.
EXCLUDED_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        ".ruff_cache",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def python_files(folder: Path) -> list[Path]:
    """Alla .py-filer under folder, utom i EXCLUDED_DIRS.

    Args:
        folder: Labbmappen.

    Returns:
        Filerna i sorterad ordning.
    """
    found = []
    for path in folder.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


_UV_GREETING_RE = re.compile(r"^Hello from .+!$")


def is_uv_template_function(node: ast.AST) -> bool:
    """True om node är en orörd mallfunktion från `uv init`.

    Args:
        node: En nod ur den parsade modulen.

    Returns:
        True för `main()` som bara skriver ut "Hello from ...!" och för
        `hello()` som bara returnerar den texten; annars False.
    """
    if not isinstance(node, ast.FunctionDef) or node.name not in ("main", "hello"):
        return False
    args = node.args
    if (
        args.posonlyargs
        or args.args
        or args.vararg
        or args.kwonlyargs
        or args.kwarg
        or node.decorator_list
        or len(node.body) != 1
    ):
        return False
    # Ingen returtyp, `-> None` (en Constant) eller `-> str`.
    returns = node.returns
    if not (
        returns is None
        or (isinstance(returns, ast.Constant) and returns.value is None)
        or (isinstance(returns, ast.Name) and returns.id == "str")
    ):
        return False
    stmt = node.body[0]
    if isinstance(stmt, ast.Return):
        text = stmt.value
    elif (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "print"
        and len(stmt.value.args) == 1
        and not stmt.value.keywords
    ):
        text = stmt.value.args[0]
    else:
        return False
    return (
        isinstance(text, ast.Constant)
        and isinstance(text.value, str)
        and _UV_GREETING_RE.match(text.value) is not None
    )


def _definitions_outside_functions(tree: ast.Module):
    """Funktioner och klasser som inte står inuti en funktion.

    Args:
        tree: Den parsade modulen.

    Yields:
        Varje def och class på modulnivå, i klasskroppar och i satser som
        `if`/`try` på de nivåerna – men inte i en funktions kropp.
    """

    def visit(node: ast.AST):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child
            elif isinstance(child, ast.ClassDef):
                yield child
                yield from visit(child)
            elif not isinstance(child, ast.Lambda):
                yield from visit(child)

    yield from visit(tree)


def missing_docstrings(
    tree: ast.Module, *, is_test_file: bool
) -> tuple[int, list[tuple[int, str]]]:
    """Publika funktioner och klasser utan docstring.

    Privata och speciella namn (`_x`, `__init__`) räknas inte, och inte heller
    pytest-testfall (`test_x`) i en test_*.py eller
    det som är definierat INUTI en funktion – en nästlad hjälpfunktion är inget
    gränssnitt någon annan anropar. Samma omfång används av formkontrollen.
    uv:s orörda mallfunktioner
    räknas inte heller (is_uv_template_function).

    Args:
        tree: Den parsade modulen.
        is_test_file: True om filnamnet börjar med test_.

    Returns:
        (antal kontrollerade definitioner, [(rad, namn)] för dem som saknar
        docstring).
    """
    total = 0
    missing = []
    for node in _definitions_outside_functions(tree):
        if node.name.startswith("_") or is_uv_template_function(node):
            continue
        if (
            is_test_file
            and node.name.startswith("test_")
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            continue
        total += 1
        if not ast.get_docstring(node):
            missing.append((node.lineno, node.name))
    return total, missing


def structure_gaps(
    tree: ast.Module, *, is_test_file: bool
) -> list[tuple[int, str, list[str]]]:
    """Funktioner vars docstring inte dokumenterar parametrar eller returvärde.

    Args:
        tree: Den parsade modulen.
        is_test_file: True om filnamnet börjar med test_.

    Returns:
        [(rad, namn, luckor)] för varje funktion med minst en lucka.
    """
    found = []
    for func, is_method in iter_structure_candidates(tree):
        # Samma namnfilter som missing_docstrings.
        if func.name.startswith("_"):
            continue
        if is_test_file and func.name.startswith("test_"):
            continue
        gaps = docstring_structure_gaps(func, is_method=is_method)
        if gaps:
            found.append((func.lineno, func.name, gaps))
    return found


# --- Kommandoraden -------------------------------------------------------------

# Inlämningsverktyget visar bara de SISTA tio raderna av utskriften, så
# sammanfattningen står sist och är kort.
HINT = (
    "Se kapitel 02 i studiematerialet för TDDE83, avsnittet om parametrar och\n"
    "returvärde i docstringen. Handledaren kör samma kontroll."
)

# Kursens kod är Python 3.14, men kontrollen kan köras av en äldre Python (den
# som kör handin.py, golv 3.12). `uv run ruff format` med mål 3.14 skriver om
# `except (A, B):` till `except A, B:` (PEP 758), som 3.12 inte kan läsa. Ett
# syntaxfel under en äldre Python är därför INGET fel i studentens kod – bara
# något den här kontrollen inte kan se. Det sägs, men räknas inte: annars hade
# giltig kod gett frågan "lämna in ändå?" med standardsvaret nej.
FULL_SYNTAX = (3, 14)


def check_folder(
    folder: Path, shown_as: str, *, presence_only: bool = False
) -> tuple[list[str], list[str]]:
    """Kontrollera en labbmapp.

    Args:
        folder: Mappen som ska kontrolleras.
        shown_as: Hur mappen skrivs i utskriften.
        presence_only: True = bara att docstringar finns, inte deras form.

    Returns:
        (problem, anteckningar): en rad per problem, och en rad per fil som
        inte kunde kontrolleras av skäl som inte är studentens fel.
    """
    problems = []
    notes = []
    for path in python_files(folder):
        rel = shown_as + "/" + path.relative_to(folder).as_posix()
        try:
            # Byte, inte text: ast.parse läser då en BOM och en
            # kodningsrad (PEP 263) precis som Python själv gör.
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as exc:
            if sys.version_info < FULL_SYNTAX:
                notes.append(
                    f"{rel}: kunde inte läsas med Python "
                    f"{sys.version_info[0]}.{sys.version_info[1]} (filen kan "
                    "använda nyare syntax) – handledaren kontrollerar den"
                )
            else:
                problems.append(
                    f"{rel} rad {exc.lineno}: syntaxfel – filen går inte att "
                    "köra som Python"
                )
            continue
        except OSError:
            problems.append(f"{rel}: filen går inte att läsa")
            continue
        is_test_file = path.name.startswith("test_")
        for line, name in missing_docstrings(tree, is_test_file=is_test_file)[1]:
            problems.append(f"{rel} rad {line}: {name} saknar docstring")
        if presence_only:
            continue
        for line, name, gaps in structure_gaps(tree, is_test_file=is_test_file):
            problems.append(
                f"{rel} rad {line}: {name} – docstringen dokumenterar inte "
                + ", ".join(gaps)
            )
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    """Kör kontrollen och returnera exitkoden.

    Args:
        argv: Argumenten utan programnamnet; None betyder sys.argv.

    Returns:
        0 om inget hittades, 1 om något behöver rättas eller mappen saknas,
        2 om inget hittades men någon fil inte kunde kontrolleras här
        (handin.toml läser 2 som "kunde inte kontrolleras", inte som godkänt).
    """
    # En sökväg med ett tecken utanför konsolens teckenkodning ska ge ett
    # frågetecken, inte en krasch. `reconfigure` finns på alla textströmmar
    # sedan 3.7; getattr för en omdirigerad ström som saknar den.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        description="Kontrollera docstringarna i en labbmapp (TDDE83).",
    )
    parser.add_argument(
        "labbmapp", help="mappen som ska kontrolleras, till exempel lab-b5"
    )
    parser.add_argument(
        "--presence-only",
        action="store_true",
        help="kontrollera bara att docstringar finns, inte Args:/Returns:",
    )
    args = parser.parse_args(argv)

    shown_as = args.labbmapp.rstrip("/\\") or args.labbmapp
    folder = Path(args.labbmapp)
    if not folder.is_dir():
        print(f"Mappen {shown_as} finns inte här. Kör kommandot i labbrepots rotmapp.")
        return 1

    problems, notes = check_folder(folder, shown_as, presence_only=args.presence_only)
    for note in notes:
        print(note)
    if not problems and notes:
        # Inget hittat i de filer som gick att läsa, men minst en fil
        # kontrollerades inte alls. Det får inte se ut som ett godkännande.
        noun = "fil" if len(notes) == 1 else "filer"
        print(
            f"Docstringarna i {shown_as}: {len(notes)} {noun} kunde inte "
            "kontrolleras här, inget att anmärka på i resten."
        )
        return 2
    if not problems:
        print(f"Docstringarna i {shown_as}: inget att anmärka på.")
        return 0

    for problem in problems:
        print(problem)
    print()
    count = len(problems)
    noun = "sak" if count == 1 else "saker"
    print(f"{count} {noun} att rätta i {shown_as}.")
    if count > 5:
        print(f"Hela listan: python3 check_docstrings.py {shown_as}")
    print(HINT)
    return 1


if __name__ == "__main__":
    sys.exit(main())
