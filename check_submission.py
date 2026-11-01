#!/usr/bin/env python3
"""Visa typannoteringar, ofärdiga kroppar och dokument som kan saknas.

Kör i labbrepots rot, till exempel:
    python3 check_submission.py lab-b5 --types

Kontrollen ger återkoppling. Handledaren bedömer själva lösningen.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from check_docstrings import (
    FULL_SYNTAX,
    is_uv_template_function,
    iter_structure_candidates,
    python_files,
)


def check_folder(
    folder: Path,
    *,
    types: bool = False,
    stubs: bool = False,
    documentation: bool = False,
) -> tuple[list[str], list[str]]:
    """Returnera problem och filer som kontrollen inte kunde läsa.

    Args:
        folder: Labbmappen.
        types: Kontrollera typannoteringar med handledarverktygets undantag.
        stubs: Leta efter tomma funktionskroppar utanför protokoll.
        documentation: Kontrollera slutprojektets README och arbetslogg.

    Returns:
        Problem att rätta och anteckningar om överhoppade filer.
    """
    problems, notes = [], []
    if not folder.is_dir():
        return [f"Mappen {folder} saknas."], notes
    if documentation:
        if not (folder / "README.md").is_file():
            problems.append("README.md saknas.")
        if not any(
            (folder / name).is_file() for name in ("PAIR_LOG.md", "WORK_LOG.md")
        ):
            problems.append("PAIR_LOG.md saknas (WORK_LOG.md om du arbetar ensam).")
    for path in python_files(folder):
        # Oanvända frivilliga alternativ ska inte ge råd om tomma kroppar.
        if path.name.endswith("_starter.py") and not types:
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            if sys.version_info < FULL_SYNTAX:
                notes.append(f"{path.name}: kunde inte läsas med denna Python-version.")
            else:
                problems.append(f"{path.name}: syntaxfel.")
            continue
        except OSError:
            notes.append(f"{path.name}: kunde inte läsas.")
            continue
        protocol_functions = {
            id(node)
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            and any(
                ast.unparse(base).split(".")[-1] == "Protocol" for base in cls.bases
            )
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node, _ in iter_structure_candidates(tree):
            if path.name.startswith("test_") and node.name.startswith("test_"):
                continue
            if is_uv_template_function(node):
                continue
            where = f"{path.relative_to(folder)}:{node.lineno}: {node.name}"
            if types:
                if node.returns is None:
                    problems.append(f"{where} saknar returtyp.")
                all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                all_args += [
                    arg
                    for arg in (node.args.vararg, node.args.kwarg)
                    if arg is not None
                ]
                for arg in all_args:
                    if arg.annotation is None and (
                        arg.arg not in {"self", "cls"}
                        or arg is node.args.vararg
                        or arg is node.args.kwarg
                    ):
                        problems.append(f"{where}: {arg.arg} saknar typ.")
            body = node.body[1:] if ast.get_docstring(node) is not None else node.body
            empty = not body or all(
                isinstance(item, ast.Pass)
                or isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and item.value.value is Ellipsis
                for item in body
            )
            abstract = any(
                ast.unparse(item).split(".")[-1] in {"abstractmethod", "overload"}
                for item in node.decorator_list
            )
            if (
                stubs
                and not path.name.endswith("_starter.py")
                and empty
                and id(node) not in protocol_functions
                and not abstract
            ):
                problems.append(
                    f"{where} har en tom kropp; kontrollera att den är färdig."
                )
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    """Kör kontrollen och ge exitkod 1 när något behöver ses över.

    Args:
        argv: Kommandoradsargument, eller None för sys.argv.

    Returns:
        Noll om inget hittades, ett vid problem och två om några filer inte
        kunde läsas men inget problem hittades i de andra.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--types", action="store_true")
    parser.add_argument("--stubs", action="store_true")
    parser.add_argument("--documentation", action="store_true")
    args = parser.parse_args(argv)
    problems, notes = check_folder(
        args.folder,
        types=args.types,
        stubs=args.stubs,
        documentation=args.documentation,
    )
    for line in notes + problems:
        print(line)
    things = "sak" if len(problems) == 1 else "saker"
    files = "fil" if len(notes) == 1 else "filer"
    print(
        f"{len(problems)} {things} att se över i {args.folder}; "
        f"{len(notes)} {files} kunde inte kontrolleras."
    )
    if problems:
        return 1
    return 2 if notes else 0


if __name__ == "__main__":
    raise SystemExit(main())
