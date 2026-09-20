"""Extract the API surface of a durable-execution SDK from source.

Parsing rather than importing, deliberately. Importing an SDK means running its
module-level code, and two versions of the same package cannot both be imported
into one interpreter -- which is precisely the comparison this tool exists to
make. `ast` reads both without either one winning.

What gets extracted is shaped by the question being asked. That question is not
"what changed" but "what could break an execution that is already in flight",
so the surface carries three things the usual API-diff does not:

  * the exception hierarchy, with base classes, because an `except` clause that
    stops matching is a silent behaviour change and not a build failure;
  * which module a symbol lives in, because a change inside the replay engine
    has different consequences from the same change in a helper;
  * whether a symbol is public, by the usual leading-underscore convention,
    since private churn is expected and not worth reporting.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

#: Modules that run on the replay path. A change in one of these can alter the
#: behaviour of an execution that checkpointed under the previous version, even
#: when every public signature is untouched -- which is the case that motivated
#: this tool. Matched as path suffixes against the package-relative module path.
REPLAY_PATH_MODULES = (
    "state.py",
    "serdes.py",
    "execution.py",
    "suspend.py",
    "concurrency/executor.py",
    "operation/step.py",
    "operation/wait.py",
    "operation/callback.py",
)

#: Modules that define how a checkpoint is written or read back. A change here
#: is the one that a pinned, vendored SDK cannot protect you from, because the
#: bytes already sitting in the platform's checkpoint store were written by the
#: other version.
#:
#: `execution.py` belongs here alongside the obvious two: it defines
#: InitialExecutionState and DurableExecutionInvocationInput together with their
#: to_dict/from_dict, which is the wire format between the platform and the
#: handler rather than an internal helper.
FORMAT_MODULES = ("serdes.py", "state.py", "execution.py")


@dataclass(frozen=True)
class Symbol:
    """One public callable or class in the SDK."""

    #: Package-relative module path, forward-slashed: "operation/step.py".
    module: str
    #: Dotted name within the module: "DurableContext.step", or "durable_step".
    name: str
    kind: str  # "class" | "function" | "method"
    #: Positional then keyword-only parameter names, or None for a class.
    params: tuple[str, ...] | None = None

    @property
    def key(self) -> str:
        return f"{self.module}::{self.name}"

    @property
    def on_replay_path(self) -> bool:
        return self.module.endswith(REPLAY_PATH_MODULES)

    @property
    def touches_format(self) -> bool:
        return self.module.endswith(FORMAT_MODULES)

    def signature(self) -> str:
        return "" if self.params is None else "(" + ", ".join(self.params) + ")"


@dataclass(frozen=True)
class ExceptionClass:
    """One exception class, with the bases an `except` clause can name."""

    module: str
    name: str
    bases: tuple[str, ...]


@dataclass
class Surface:
    """Everything extracted from one version of one SDK."""

    version: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    exceptions: dict[str, ExceptionClass] = field(default_factory=dict)
    #: module -> structural fingerprint. Not the source text: a reformat, a
    #: comment, or a corrected docstring is not drift, and a tool that reports
    #: it as drift trains its reader to skim past the findings that matter.
    fingerprints: dict[str, str] = field(default_factory=dict)
    #: Modules that could not be parsed by this interpreter, which is itself
    #: worth surfacing -- a version needing a newer Python is a drift risk of
    #: its own kind, not a blank result.
    unparsed: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.symbols)


class _StripDocstrings(ast.NodeTransformer):
    """Remove docstring nodes so prose edits do not read as behaviour changes."""

    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def fingerprint(tree: ast.AST) -> str:
    """A structural hash of a module: code only, no comments, no docstrings.

    `ast.dump` discards formatting and comments for free; docstrings survive as
    Constant nodes, so they are stripped first. What is left changes only when
    the code does.
    """
    stripped = _StripDocstrings().visit(ast.parse(ast.unparse(tree)))
    return hashlib.sha256(
        ast.dump(stripped, annotate_fields=False).encode("utf8")
    ).hexdigest()


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Parameter names, positional first then keyword-only.

    Names only, not annotations or defaults. An annotation change is a typing
    concern; a *name* change breaks callers that pass it by keyword, and a
    dropped or added parameter changes the call contract. Those are the two
    this tool reports on, so those are the two it records.
    """
    pos = [a.arg for a in node.args.posonlyargs] + [a.arg for a in node.args.args]
    kwonly = [a.arg for a in node.args.kwonlyargs]
    return tuple(pos + kwonly)


def _is_public(name: str) -> bool:
    # Dunders are public API in the sense that matters here (__call__ on a
    # context manager, say), but the leading-underscore convention otherwise
    # marks internals, and reporting their churn would bury the real findings.
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


#: Names that are exceptions without inheriting from anything in the package.
_BUILTIN_EXCEPTIONS = frozenset({"Exception", "BaseException"})


def _looks_like_exception_name(name: str) -> bool:
    return name.endswith(("Error", "Exception"))


def _resolve_exceptions(
    classes: dict[str, tuple[str, tuple[str, ...]]],
) -> dict[str, ExceptionClass]:
    """Decide which of the package's classes are exceptions, transitively.

    Resolving the real MRO would mean importing the SDK, which is the one thing
    this module will not do. So it is a fixpoint over the declared bases: a
    class is an exception if it is named like one, inherits a builtin exception,
    or inherits something already established as one.

    The transitive step matters. A subclass whose own name carries no suffix --
    and whose parent's name carries none either, two levels down -- is still an
    exception, and missing it would lose exactly the reparenting findings this
    tool is best at.
    """
    known = {
        name
        for name, (_module, bases) in classes.items()
        if _looks_like_exception_name(name)
        or any(b in _BUILTIN_EXCEPTIONS or _looks_like_exception_name(b) for b in bases)
    }

    changed = True
    while changed:
        changed = False
        for name, (_module, bases) in classes.items():
            if name in known:
                continue
            if any(b in known for b in bases):
                known.add(name)
                changed = True

    return {
        name: ExceptionClass(module=classes[name][0], name=name, bases=classes[name][1])
        for name in sorted(known)
        if name in classes
    }


def extract(package_root: Path, version: str) -> Surface:
    """Read every module under `package_root` into a Surface.

    `package_root` is the package directory itself -- the one containing
    `__init__.py` -- not the distribution root.
    """
    package_root = Path(package_root)
    if not package_root.is_dir():
        raise NotADirectoryError(f"not a package directory: {package_root}")

    surface = Surface(version=version)
    #: class name -> (module, declared bases), filled during the walk and
    #: resolved into the exception table afterwards. Exception-ness cannot be
    #: decided one class at a time, because a subclass may be visited before
    #: the parent that makes it an exception.
    classes: dict[str, tuple[str, tuple[str, ...]]] = {}

    for path in sorted(package_root.rglob("*.py")):
        module = path.relative_to(package_root).as_posix()
        text = path.read_text(encoding="utf8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # A module this interpreter cannot parse is recorded rather than
            # silently skipped: a version that needs a newer Python than the one
            # running the diff is itself worth knowing about, and a silently
            # empty surface would read as "nothing changed".
            surface.unparsed.append(module)
            continue

        surface.fingerprints[module] = fingerprint(tree)
        for node in tree.body:
            _visit_top_level(node, module, surface, classes)

    surface.exceptions = _resolve_exceptions(classes)
    return surface


def _visit_top_level(
    node: ast.AST,
    module: str,
    surface: Surface,
    classes: dict[str, tuple[str, tuple[str, ...]]],
) -> None:
    if isinstance(node, ast.ClassDef):
        if not _is_public(node.name):
            return
        surface.symbols[f"{module}::{node.name}"] = Symbol(
            module=module, name=node.name, kind="class"
        )
        classes[node.name] = (
            module,
            tuple(
                b.id if isinstance(b, ast.Name) else ast.unparse(b) for b in node.bases
            ),
        )
        for child in node.body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(
                child.name
            ):
                dotted = f"{node.name}.{child.name}"
                surface.symbols[f"{module}::{dotted}"] = Symbol(
                    module=module,
                    name=dotted,
                    kind="method",
                    params=_params(child),
                )

    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        if not _is_public(node.name):
            return
        surface.symbols[f"{module}::{node.name}"] = Symbol(
            module=module, name=node.name, kind="function", params=_params(node)
        )
