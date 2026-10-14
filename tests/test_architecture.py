"""The module boundaries of docs/architecture.md, checked so they stay true."""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "openvigie"


def imports() -> list[tuple[str, str, str]]:
    """(importing file, imported module, imported name) for every openvigie import."""
    found = []
    for path in SRC.rglob("*.py"):
        where = str(path.relative_to(SRC))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("openvigie"):
                found += [(where, node.module or "", a.name) for a in node.names]
            elif isinstance(node, ast.Import):
                found += [(where, a.name, "") for a in node.names if a.name.startswith("openvigie")]
    return found


def test_no_private_names_across_modules() -> None:
    private = [(f, m, n) for f, m, n in imports() if n.startswith("_") and not n.startswith("__")]
    assert private == []


def test_collector_and_adapters_never_import_the_hub() -> None:
    outside = ("collector/", "adapters/")
    crossing = [(f, m) for f, m, _ in imports()
                if f.startswith(outside) and m.startswith("openvigie.hub")]  # fmt: skip
    assert crossing == []


def test_hub_never_imports_the_collector() -> None:
    crossing = [(f, m) for f, m, _ in imports()
                if f.startswith("hub/") and m.startswith("openvigie.collector")]  # fmt: skip
    assert crossing == []
