from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Fix:
    rule: str
    line: int
    message: str
    before: str
    after: str


@dataclass(frozen=True)
class Diagnostic:
    rule: str
    line: int
    message: str
    severity: Severity = "warning"


@dataclass
class FileReport:
    path: Path
    changed: bool = False
    fixes: list[Fix] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    source_compile: str = "skipped"
    target_compile: str = "skipped"
    source_error: str | None = None
    target_error: str | None = None
    abi_equal: bool | None = None
    method_ids_equal: bool | None = None
    storage_layout_equal: bool | None = None


@dataclass(frozen=True)
class Config:
    paths: tuple[Path, ...]
    target_version: str = "0.4.3"
    source_version: str | None = None
    write: bool = False
    check: bool = False
    diff: bool = False
    report_json: Path | None = None
    select: frozenset[str] = frozenset()
    ignore: frozenset[str] = frozenset()
    aggressive: bool = False
    test_command: str | None = None
    source_vyper: str | None = None
    target_vyper: str | None = None
    compiler_search_paths: tuple[Path, ...] = ()
    enable_decimals: bool = False
    bump_pragma: bool = False
    format: str = "none"


@dataclass
class RunReport:
    source_version: str | None
    target_version: str
    files: list[FileReport]
    test_command: str | None = None
    test_status: str = "skipped"
    test_output: str | None = None

    @property
    def changed_count(self) -> int:
        return sum(1 for file in self.files if file.changed)

    @property
    def fix_count(self) -> int:
        return sum(len(file.fixes) for file in self.files)

    @property
    def diagnostic_count(self) -> int:
        return sum(len(file.diagnostics) for file in self.files)

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "files": [
                {
                    "path": str(file.path),
                    "changed": file.changed,
                    "fixes": [fix.__dict__ for fix in file.fixes],
                    "diagnostics": [diag.__dict__ for diag in file.diagnostics],
                    "validation": {
                        "source_compile": file.source_compile,
                        "target_compile": file.target_compile,
                        "abi_equal": file.abi_equal,
                        "method_ids_equal": file.method_ids_equal,
                        "storage_layout_equal": file.storage_layout_equal,
                    },
                    "source_error": file.source_error,
                    "target_error": file.target_error,
                }
                for file in self.files
            ],
            "test_command": self.test_command,
            "test_status": self.test_status,
            "test_output": self.test_output,
        }
