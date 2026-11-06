"""Remove initialization of concrete stateless modules for Vyper 0.5.0b2."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Config, Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import TextEdit, apply_edits, line_number
from ..versions import MigrationContext, infer_pragma

if TYPE_CHECKING:
    from ..compiler import ImportClosure


@dataclass(frozen=True)
class _Initialization:
    module: str | None
    start: int
    end: int


@dataclass
class _ModuleFacts:
    imports: dict[str, str] = field(default_factory=dict)
    initializes: list[_Initialization] = field(default_factory=list)
    has_state: bool = False


def _module_facts(source: str) -> _ModuleFacts:
    """Read logical top-level statements; unknown forms keep initialization intact."""
    facts = _ModuleFacts()
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    statement: list[tokenize.TokenInfo] = []
    decorators: set[str] = set()
    indent = 0
    nonreentrancy = False
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.INDENT:
                indent += 1
            elif token.type == tokenize.DEDENT:
                indent -= 1
            elif indent:
                continue
            elif token.type == tokenize.COMMENT:
                if re.fullmatch(r"#\s*pragma\s+nonreentrancy\s+on\s*", token.string):
                    nonreentrancy = True
            elif token.type == tokenize.NEWLINE:
                if statement:
                    _record_statement(statement, offsets, decorators, nonreentrancy, facts)
                    statement = []
            elif token.type not in {tokenize.NL, tokenize.ENDMARKER}:
                statement.append(token)
    except (tokenize.TokenError, IndentationError):
        facts.has_state = True
        facts.initializes.clear()
    return facts


def _record_statement(
    tokens: list[tokenize.TokenInfo],
    offsets: list[int],
    decorators: set[str],
    nonreentrancy: bool,
    facts: _ModuleFacts,
) -> None:
    words = [token.string for token in tokens]
    first = words[0]
    if first == "@" and len(words) > 1:
        decorators.add(words[1])
        return
    if first == "def" and len(words) > 1:
        if words[1] == "__init__" or decorators & {"abstract", "nonreentrant"}:
            facts.has_state = True
        if nonreentrancy and "external" in decorators and not decorators & {"pure", "reentrant"}:
            facts.has_state = True
    elif first in {"import", "from"}:
        try:
            node = ast.parse(tokenize.untokenize([(t.type, t.string) for t in tokens])).body[0]
        except SyntaxError:
            facts.has_state = True
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                facts.imports[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            package = "." * node.level + (node.module + "." if node.module else "")
            for alias in node.names:
                facts.imports[alias.asname or alias.name] = package + alias.name
    elif len(words) > 1 and words[1] == ":":
        if first == "initializes":
            try:
                annotation = ast.parse(
                    tokenize.untokenize([(t.type, t.string) for t in tokens[2:]]), mode="eval"
                ).body
            except SyntaxError:
                annotation = None
            module = annotation.id if isinstance(annotation, ast.Name) else None
            facts.initializes.append(
                _Initialization(
                    module,
                    offsets[tokens[0].start[0] - 1] + tokens[0].start[1],
                    offsets[tokens[-1].end[0] - 1] + tokens[-1].end[1],
                )
            )
        elif first not in {"exports", "implements"} and words[2:4] != ["constant", "("]:
            facts.has_state = True
    elif first not in {"event", "struct", "flag", "enum", "interface", "error"}:
        # Module docstrings are harmless. Unknown executable declarations aren't.
        if len(tokens) != 1 or tokens[0].type != tokenize.STRING:
            facts.has_state = True
    decorators.clear()


@dataclass
class _InitializationAnalysis:
    snapshot: ImportClosure
    config: Config

    def is_stateless(
        self, consumer: Path, binding: str, imports: dict[str, str], visiting: set[Path]
    ) -> bool:
        module = imports.get(binding)
        path = self.snapshot.resolve_import(consumer, module) if module is not None else None
        if path is None or path.suffix != ".vy" or path in visiting:
            return False
        source = self.snapshot.contents[path].decode("utf-8")
        facts = _module_facts(source)
        if facts.has_state:
            return False
        if not facts.initializes:
            return True
        # A parent can lose its state only if its own declarations will be rewritten.
        migrating = self.config.include_dependencies or path in self.snapshot.roots
        crosses = MigrationContext.from_specs(
            infer_pragma(source) or self.config.source_version, self.config.target_version
        ).crosses("0.5.0b2")
        if not migrating or not crosses:
            return False
        return all(
            declaration.module is not None
            and self.is_stateless(path, declaration.module, facts.imports, visiting | {path})
            for declaration in facts.initializes
        )


def _drop_stateless_initializations(
    context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    snapshot = context.config.source_snapshot
    if snapshot is None or context.path is None:
        return context.source, [], []
    facts = _module_facts(context.source)
    analysis = _InitializationAnalysis(snapshot, context.config)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for declaration in facts.initializes:
        if declaration.module is None or not analysis.is_stateless(
            context.path, declaration.module, facts.imports, {context.path.resolve()}
        ):
            continue
        before = context.source[declaration.start : declaration.end]
        edits.append(TextEdit(declaration.start, declaration.end, ""))
        fixes.append(
            Fix(
                "VY126",
                line_number(context.source, declaration.start),
                "removed initialization of a stateless concrete module",
                before,
                "",
            )
        )
    return apply_edits(context.source, edits), fixes, []


RULES = (
    Rule(
        "stateless_initializations",
        _drop_stateless_initializations,
        changes=(crossing("VY126", "0.5.0b2"),),
    ),
)
