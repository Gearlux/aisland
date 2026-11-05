"""What the package's code USES, read from its syntax tree: the part of a rule an import statement does not show.

An import statement shows only part of a rule: after `import click`, the use `click.echo(...)` is an attribute of
a module, which no import names. These helpers resolve every name a module
binds to click or sys — `import click`, `import click as c`, `import click.termui`, `from click import echo as say`
— so a use is found however it is spelled.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import aisland

PACKAGE = Path(aisland.__file__).parent


def library_modules() -> Iterator[Tuple[str, ast.Module]]:
    """Every module of the package but the CLI (``aisland/cli/``, where a command's output and exit status are
    decided) and ``harness/`` (scripts a workspace runs, no part of aisland's code), as its path and its tree."""
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(PACKAGE).parts
        if parts[0] in {"cli", "harness"}:
            continue
        yield path.relative_to(PACKAGE.parent).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


@dataclass
class Bindings:
    """The names a module binds to a module it imports (``modules``: local name -> module) and to a name imported
    from one (``names``: local name -> ``module.name``)."""

    modules: Dict[str, str] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)


def bindings(tree: ast.Module) -> Bindings:
    """What ``tree`` binds by its imports, wherever they stand (a function's own import binds the same way)."""
    bound = Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound.modules[alias.asname] = alias.name
                else:  # `import click.termui` binds `click`
                    first = alias.name.split(".")[0]
                    bound.modules[first] = first
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bound.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def dotted(node: ast.expr, bound: Bindings) -> Optional[str]:
    """The full name ``node`` refers to — ``click.echo`` for ``click.echo``, ``c.echo``, ``click.termui.echo`` read
    as ``click.termui.echo``, or ``say`` imported as ``click.echo`` — or ``None`` for a name no import bound."""
    attributes: List[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    if node.id in bound.modules:
        base = bound.modules[node.id]
    elif node.id in bound.names:
        base = bound.names[node.id]
    else:
        return None
    return ".".join([base, *reversed(attributes)])


def references(tree: ast.Module, package: str) -> Iterator[Tuple[int, str]]:
    """Every use in ``tree`` of a name of ``package`` (``click``), outermost first: the line and the full name."""
    bound = bindings(tree)
    inner: Set[int] = set()  # the `click` of `click.echo`, already reported as part of it
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Attribute, ast.Name)) or id(node) in inner:
            continue
        name = dotted(node, bound)
        if name is None or not (name == package or name.startswith(package + ".")):
            continue
        value: ast.expr = node
        while isinstance(value, ast.Attribute):
            value = value.value
            inner.add(id(value))
        yield node.lineno, name


def entry_point_lines(tree: ast.Module, *functions: str) -> Set[int]:
    """The lines that are a module's own entry point: its ``if __name__ == "__main__":`` block, and the module-level
    functions named in ``functions`` (``aisland-mcp``'s ``main``)."""
    lines: Set[int] = set()
    for node in tree.body:
        is_main_block = (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators)
        )
        is_named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions
        if is_main_block or is_named:
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines
