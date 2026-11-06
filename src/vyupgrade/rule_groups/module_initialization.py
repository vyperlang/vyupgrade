"""Remove initialization of concrete stateless modules for Vyper 0.5.0b2."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Config, Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import TextEdit, apply_edits, code_mask, line_number
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
    offsets = [0] + [index + 1 for index, char in enumerate(source) if char == "\n"]
    statement: list[tokenize.TokenInfo] = []
    decorators: set[str] = set()
    indent = 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        # Vyper's pre-parser applies pragmas globally, including indented comments.
        nonreentrancy = any(
            token.type == tokenize.COMMENT
            and re.fullmatch(r"#\s*pragma\s+nonreentrancy\s+on\s*", token.string)
            for token in tokens
        )
        for token in tokens:
            if token.type == tokenize.INDENT:
                indent += 1
            elif token.type == tokenize.DEDENT:
                indent -= 1
            elif indent or token.type == tokenize.COMMENT:
                continue
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


def _expression(tokens: list[tokenize.TokenInfo]) -> ast.expr | None:
    try:
        return ast.parse(
            tokenize.untokenize([(t.type, t.string) for t in tokens]), mode="eval"
        ).body
    except SyntaxError:
        return None


def _record_statement(
    tokens: list[tokenize.TokenInfo],
    offsets: list[int],
    decorators: set[str],
    nonreentrancy: bool,
    facts: _ModuleFacts,
) -> None:
    words = [token.string for token in tokens]
    first = words[0]
    if first == "@":
        decorator = _expression(tokens[1:])
        if isinstance(decorator, ast.Call):
            decorator = decorator.func
        if isinstance(decorator, ast.Name):
            decorators.add(decorator.id)
        else:
            facts.has_state = True
        return
    if first == "def" and len(words) > 1:
        if unicodedata.normalize("NFKC", words[1]) == "__init__" or decorators & {
            "abstract",
            "nonreentrant",
        }:
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
    elif ":" in words and isinstance(target := _expression(tokens[: words.index(":")]), ast.Name):
        annotation_start = words.index(":") + 1
        if target.id == "initializes":
            annotation = _expression(tokens[annotation_start:])
            module = annotation.id if isinstance(annotation, ast.Name) else None
            facts.initializes.append(
                _Initialization(
                    module,
                    offsets[tokens[0].start[0] - 1] + tokens[0].start[1],
                    offsets[tokens[-1].end[0] - 1] + tokens[-1].end[1],
                )
            )
        elif target.id == "uses":
            facts.has_state = True
        elif target.id not in {"exports", "implements"}:
            end = words.index("=") if "=" in words else len(tokens)
            annotation = _expression(tokens[annotation_start:end])
            for _ in range(2):
                if not (
                    isinstance(annotation, ast.Call)
                    and isinstance(annotation.func, ast.Name)
                    and annotation.func.id in {"public", "reentrant"}
                    and len(annotation.args) == 1
                    and not annotation.keywords
                ):
                    break
                annotation = annotation.args[0]
            if not (
                isinstance(annotation, ast.Call)
                and isinstance(annotation.func, ast.Name)
                and annotation.func.id == "constant"
                and len(annotation.args) == 1
                and not annotation.keywords
            ):
                facts.has_state = True
    elif first not in {"event", "struct", "flag", "enum", "interface", "error"}:
        # Module docstrings are harmless. Unknown executable declarations aren't.
        docstring = _expression(tokens)
        if not isinstance(docstring, ast.Constant) or not isinstance(docstring.value, str):
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
        if module is None:
            return False
        paths = self._module_candidates(consumer, module)
        if not paths:
            return False
        for path in paths:
            if path in visiting:
                return False
            source = self.snapshot.contents[path].decode("utf-8")
            facts = _module_facts(source)
            if facts.has_state:
                return False
            if not facts.initializes:
                continue
            # A parent can lose its state only if its own declarations will be rewritten.
            migrating = self.config.include_dependencies or path in self.snapshot.roots
            crosses = MigrationContext.from_specs(
                infer_pragma(source) or self.config.source_version, self.config.target_version
            ).crosses("0.5.0b2")
            if not migrating or not crosses:
                return False
            if not all(
                declaration.module is not None
                and self.is_stateless(path, declaration.module, facts.imports, visiting | {path})
                for declaration in facts.initializes
            ):
                return False
        return True

    def _module_candidates(self, consumer: Path, module: str) -> set[Path]:
        level = len(module) - len(module.lstrip("."))
        if level:
            base = consumer.resolve().parent
            for _ in range(level - 1):
                base = base.parent
            bases = (base,)
        else:
            # Absolute imports use the entrypoint's search paths, not this module's
            # parent. Capture may include several candidates; require all to be stateless.
            bases = (*self.snapshot.import_roots, *(path.parent for path in self.snapshot.files))
        parts = module.lstrip(".").split(".")
        # Vyper tries concrete modules before .vyi or .json interfaces across all roots.
        return {
            path
            for base in bases
            if (path := base.joinpath(*parts).with_suffix(".vy").resolve())
            in self.snapshot.contents
        }


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
        # Comments can contain module-wide compiler pragmas, so retain them.
        after = "".join(
            char
            for char, is_code in zip(before, code_mask(before), strict=True)
            if not is_code or char in "\r\n"
        )
        edits.append(TextEdit(declaration.start, declaration.end, after))
        fixes.append(
            Fix(
                "VY126",
                line_number(context.source, declaration.start),
                "removed initialization of a stateless concrete module",
                before,
                after,
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
