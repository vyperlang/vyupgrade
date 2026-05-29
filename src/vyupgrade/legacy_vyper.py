from __future__ import annotations

import glob
import sys
import sysconfig
from pathlib import Path

import typed_ast.ast3 as ast3


def main() -> None:
    # Old Vyper beta releases dispatch on pre-3.8 ast.Num/ast.Str/ast.NameConstant
    # node classes. typed_ast.ast3 keeps that AST shape available on Python 3.8+.
    sys.modules["ast"] = ast3
    scripts = glob.glob(str(Path(sysconfig.get_path("scripts")) / "vyper"))
    if not scripts:
        raise SystemExit("could not locate legacy vyper console script")
    script = Path(scripts[0])
    sys.argv = ["vyper", *sys.argv[1:]]
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), {"__name__": "__main__", "__file__": str(script)})


if __name__ == "__main__":
    main()
