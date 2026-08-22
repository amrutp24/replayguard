"""Python frontend, built on the stdlib `ast` module.

Uses real AST + scope resolution rather than pattern matching on source text.
That is not gold-plating: RG003 has to know whether a mutated name belongs to
the step body or to an enclosing scope, and RG004 has to know whether a branch
condition derives from a nondeterministic symbol. Neither question can be
answered by a regex, which is why this checker is a different kind of tool from
the extractors that draw workflow diagrams.
"""

from __future__ import annotations

import ast

from ..ir import (
    Branch,
    Call,
    Handler,
    Language,
    Location,
    Module,
    OuterWrite,
    Region,
    Step,
)

#: Decorator marking a durable handler.
_DURABLE_DECORATORS = {"durable_execution", "durable"}

#: Every durable operation across the three SDKs, in both spellings. Derived
#: from the real SDK surface, not guessed -- an unrecognised operation is worse
#: than a missing rule, because its body then gets analysed in the wrong region
#: and every legitimate in-step call is reported as a violation.
_STEP_METHODS = {
    "step", "stepAsync", "step_async",
    "map", "mapAsync", "map_async",
    "wait", "waitAsync", "wait_async",
    "parallel",
    "invoke", "invokeAsync", "invoke_async",
    "runInChildContext", "runInChildContextAsync",
    "run_in_child_context", "run_in_child_context_async",
    "waitForCallback", "wait_for_callback",
    "createCallback", "create_callback",
    "withRetry", "withRetryAsync", "with_retry", "with_retry_async",
    "waitForCondition", "wait_for_condition",
}

def _is_context_name(name: str) -> bool:
    """Is this identifier a durable context?

    A fixed list of four names missed `childContext` from
    `runInChildContext(async (childContext) => ...)`, so the child's steps were
    not recognised and their bodies were analysed in the durable region. Child
    and step contexts are named freely, so match on shape instead.
    """
    lowered = name.lower()
    return "ctx" in lowered or "context" in lowered

#: Methods that mutate their receiver in place. A step body calling one of these
#: on a name from an enclosing scope is the silent-loss bug (RG003).
_MUTATORS = {
    "append",
    "appendleft",
    "extend",
    "insert",
    "add",
    "update",
    "pop",
    "remove",
    "clear",
    "discard",
    "setdefault",
    "sort",
    "__setitem__",
}

#: Roots that indicate a value came from an AWS SDK construction.
_AWS_ROOTS = ("boto3", "botocore")


def _loc(path: str, node: ast.AST) -> Location:
    return Location(
        file=path,
        line=getattr(node, "lineno", 0),
        col=getattr(node, "col_offset", 0),
        end_line=getattr(node, "end_lineno", None),
        end_col=getattr(node, "end_col_offset", None),
    )


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Best-effort qualified name for an expression."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return _dotted(node.func, aliases)
    return None


def _root_name(node: ast.AST) -> str | None:
    """The leftmost Name in an attribute/subscript/call chain."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


class _ModuleContext:
    """Module-level facts the walker needs but can't see from inside a handler."""

    def __init__(self, tree: ast.Module):
        self.aliases: dict[str, str] = {}
        self.module_names: set[str] = set()
        #: Names bound to an AWS client/resource, whose methods are all external
        #: I/O. Method names on these are unbounded (`put_item`, `invoke_model`,
        #: …) so they cannot be catalogued individually.
        self.aws_names: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            self.module_names.update(names)
            if node.value is not None and self._is_aws_derived(node.value):
                self.aws_names.update(names)

    def _is_aws_derived(self, value: ast.AST) -> bool:
        for sub in ast.walk(value):
            if isinstance(sub, ast.Name):
                canonical = self.aliases.get(sub.id, sub.id)
                if canonical.split(".")[0] in _AWS_ROOTS:
                    return True
        return False


class _Walker:
    """Walks one handler, tracking region and lexical scope.

    Recursion is explicit rather than via `generic_visit` because region and
    scope both need to be pushed and popped around specific subtrees, and a
    generic visitor makes that ordering easy to get subtly wrong.
    """

    def __init__(
        self,
        path: str,
        ctx: _ModuleContext,
        handler: Handler,
        nested_defs: dict[str, ast.AST],
        step_body_names: set[str],
    ):
        self.path = path
        self.ctx = ctx
        self.h = handler
        #: Nested `def`s inside the handler, so a step body passed by reference
        #: can still be analysed. This is the dominant real-world shape —
        #: treating it as unresolvable would blind the checker to most code.
        self.nested_defs = nested_defs
        self.step_body_names = step_body_names
        #: Innermost-last stack of names bound in each enclosing function scope.
        self.scopes: list[set[str]] = []

    # -- scope helpers ----------------------------------------------------

    def _bind(self, name: str) -> None:
        if self.scopes:
            self.scopes[-1].add(name)

    def _is_outer(self, name: str) -> bool:
        """True when `name` resolves to something the current scope doesn't own."""
        if self.scopes and name in self.scopes[-1]:
            return False
        if any(name in s for s in self.scopes[:-1]):
            return True
        return name in self.ctx.module_names or name in self.ctx.aliases

    def _is_module_level(self, name: str) -> bool:
        return name in self.ctx.module_names and not any(
            name in s for s in self.scopes[:-1]
        )

    def _bind_targets(self, target: ast.AST) -> None:
        for n in ast.walk(target):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                self._bind(n.id)

    # -- entry ------------------------------------------------------------

    def walk_function(self, fn: ast.AST, region: Region) -> None:
        self.scopes.append(set())
        args = getattr(fn, "args", None)
        if args is not None:
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self._bind(a.arg)
            if args.vararg:
                self._bind(args.vararg.arg)
            if args.kwarg:
                self._bind(args.kwarg.arg)

        body = fn.body if isinstance(fn.body, list) else [ast.Expr(value=fn.body)]
        for stmt in body:
            self.visit(stmt, region)
        self.scopes.pop()

    # -- dispatch ---------------------------------------------------------

    def visit(self, node: ast.AST, region: Region) -> None:
        method = getattr(self, f"_v_{type(node).__name__}", None)
        if method is not None:
            method(node, region)
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child, region)

    # -- statements -------------------------------------------------------

    def _v_Assign(self, node: ast.Assign, region: Region) -> None:
        for t in node.targets:
            self._record_target_write(t, region)
        for t in node.targets:
            self._bind_targets(t)
        self.visit(node.value, region)

    def _v_AugAssign(self, node: ast.AugAssign, region: Region) -> None:
        # `x += 1` mutates, so an outer `x` is a write even without a subscript.
        self._record_target_write(node.target, region, augmented=True)
        self.visit(node.value, region)

    def _v_Global(self, node: ast.Global, region: Region) -> None:
        for name in node.names:
            if region is Region.STEP_BODY:
                self.h.outer_writes.append(
                    OuterWrite(
                        target=name,
                        loc=_loc(self.path, node),
                        region=region,
                        is_global=True,
                    )
                )

    _v_Nonlocal = _v_Global

    def _v_If(self, node: ast.If, region: Region) -> None:
        self._record_branch(node.test, region, node)
        self.visit(node.test, region)
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt, region)

    def _v_While(self, node: ast.While, region: Region) -> None:
        self._record_branch(node.test, region, node)
        self.visit(node.test, region)
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt, region)

    def _v_IfExp(self, node: ast.IfExp, region: Region) -> None:
        self._record_branch(node.test, region, node)
        for child in ast.iter_child_nodes(node):
            self.visit(child, region)

    def _v_FunctionDef(self, node: ast.FunctionDef, region: Region) -> None:
        self._bind(node.name)
        # Skip bodies that are used as step bodies elsewhere — they get walked
        # at the call site with the correct region. Walking them here too would
        # report every legitimate in-step I/O call as a durable-region
        # violation, which is the worst possible false positive for this tool.
        if node.name in self.step_body_names:
            return
        self.walk_function(node, region)

    _v_AsyncFunctionDef = _v_FunctionDef

    # -- expressions ------------------------------------------------------

    def _v_Call(self, node: ast.Call, region: Region) -> None:
        if self._try_durable_operation(node, region):
            return

        dotted = _dotted(node.func, self.ctx.aliases)
        if dotted:
            root = _root_name(node.func)
            self.h.calls.append(
                Call(
                    dotted=dotted,
                    loc=_loc(self.path, node),
                    region=region,
                    external_client=bool(root and root in self.ctx.aws_names),
                )
            )
            self._record_mutating_call(node, region)

        for child in ast.iter_child_nodes(node):
            self.visit(child, region)

    # -- recording --------------------------------------------------------

    def _record_target_write(
        self, target: ast.AST, region: Region, augmented: bool = False
    ) -> None:
        """Record writes that reach outside the current scope.

        A bare `x = 1` inside a function creates a *local* binding in Python, so
        it is not an outer write. Only subscript/attribute writes and augmented
        assignment can reach an enclosing binding without a global/nonlocal
        declaration — which is handled separately.
        """
        if region is not Region.STEP_BODY:
            return
        reaches_outward = isinstance(target, (ast.Subscript, ast.Attribute)) or augmented
        if not reaches_outward:
            return
        root = _root_name(target)
        if root and self._is_outer(root):
            self.h.outer_writes.append(
                OuterWrite(
                    target=root,
                    loc=_loc(self.path, target),
                    region=region,
                    is_global=self._is_module_level(root),
                )
            )

    def _record_mutating_call(self, node: ast.Call, region: Region) -> None:
        """`outer_list.append(x)` inside a step body — the AWS-documented bug."""
        if region is not Region.STEP_BODY:
            return
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _MUTATORS:
            return
        root = _root_name(node.func.value)
        if root and self._is_outer(root):
            self.h.outer_writes.append(
                OuterWrite(
                    target=root,
                    loc=_loc(self.path, node),
                    region=region,
                    is_global=self._is_module_level(root),
                )
            )

    def _record_branch(self, test: ast.AST, region: Region, node: ast.AST) -> None:
        symbols: list[str] = []
        for n in ast.walk(test):
            if isinstance(n, ast.Call):
                d = _dotted(n.func, self.ctx.aliases)
                if d:
                    symbols.append(d)
            elif isinstance(n, ast.Attribute):
                d = _dotted(n, self.ctx.aliases)
                if d:
                    symbols.append(d)
        self.h.branches.append(
            Branch(loc=_loc(self.path, node), region=region, condition_symbols=symbols)
        )

    def _try_durable_operation(self, node: ast.Call, region: Region) -> bool:
        """Handle `context.step(fn, name=...)` and friends.

        Returns True when the node was consumed, so the caller doesn't also
        record it as an ordinary call.
        """
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _STEP_METHODS:
            return False
        if not (isinstance(func.value, ast.Name) and _is_context_name(func.value.id)):
            return False

        name_literal, name_is_static, name_symbols = self._step_name(node)
        self.h.steps.append(
            Step(
                kind=func.attr,
                loc=_loc(self.path, node),
                name_literal=name_literal,
                name_is_static=name_is_static,
                name_symbols=name_symbols,
            )
        )

        # The body is found by KIND, not by position. `wait_for_callback` takes
        # its submitter as a keyword (`submitter=fn`) in AWS's own samples, and
        # assuming args[0] silently analysed those bodies in the durable region,
        # reporting every legitimate in-step call as a violation.
        candidates = [*node.args, *(kw.value for kw in node.keywords if kw.arg != "name")]
        body = next((a for a in candidates if self._is_callable_arg(a)), None)

        # Everything that is not the body evaluates in the durable region.
        for arg in candidates:
            if arg is not body:
                self.visit(arg, region)

        if body is None:
            # Something that looks like a callable passed by reference but could
            # not be resolved -- an import, a method, a name from an outer
            # module. Reported as RG900 so a clean run still means something.
            for arg in candidates:
                if not self._looks_like_unresolved_callable(arg):
                    continue
                self.h.unresolved.append(_loc(self.path, arg))
                break
            return True
        if isinstance(body, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            self.walk_function(body, Region.STEP_BODY)
        else:
            self.walk_function(self.nested_defs[body.id], Region.STEP_BODY)
        return True

    def _looks_like_unresolved_callable(self, node: ast.AST) -> bool:
        """Distinguish a by-reference function from an ordinary data argument.

        Many durable operations legitimately take no callable at all --
        `context.wait(duration)`, `context.invoke(name, payload)`. Treating
        every unresolved argument as a coverage gap produced far more notes
        than real findings and made the count meaningless.

        A callable passed by reference is a module-level function or an import,
        so it is *not* bound as a local. A name that is bound locally is data.
        """
        if isinstance(node, ast.Attribute):
            return True  # a method reference such as `self.handler`
        if not isinstance(node, ast.Name) or node.id in self.nested_defs:
            return False
        return not any(node.id in scope for scope in self.scopes)

    def _is_callable_arg(self, node: ast.AST) -> bool:
        """A lambda, an inline def, or a name bound to a nested def."""
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        return isinstance(node, ast.Name) and node.id in self.nested_defs

    def _step_name(self, node: ast.Call) -> tuple[str | None, bool, list[str]]:
        for kw in node.keywords:
            if kw.arg != "name":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value, True, []
            symbols: list[str] = []
            for n in ast.walk(kw.value):
                if isinstance(n, ast.Call):
                    d = _dotted(n.func, self.ctx.aliases)
                elif isinstance(n, ast.Attribute):
                    d = _dotted(n, self.ctx.aliases)
                else:
                    continue
                if d:
                    symbols.append(d)
            return None, False, symbols
        return None, True, []


def _collect_nested(handler_node: ast.AST) -> tuple[dict[str, ast.AST], set[str]]:
    """Nested defs in a handler, and which of them are used as step bodies."""
    nested: dict[str, ast.AST] = {}
    for node in ast.walk(handler_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not handler_node:
            nested[node.name] = node

    step_bodies: set[str] = set()
    for node in ast.walk(handler_node):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in _STEP_METHODS):
            continue
        if not (isinstance(f.value, ast.Name) and _is_context_name(f.value.id)):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords if kw.arg != "name")]:
            if isinstance(arg, ast.Name) and arg.id in nested:
                step_bodies.add(arg.id)
    return nested, step_bodies


def _is_durable_handler(node: ast.AST, aliases: dict[str, str]) -> bool:
    for dec in node.decorator_list:
        dotted = _dotted(dec, aliases) or ""
        if dotted.rsplit(".", 1)[-1] in _DURABLE_DECORATORS:
            return True
    return False


def parse_source(source: str, path: str) -> Module:
    tree = ast.parse(source, filename=path)
    ctx = _ModuleContext(tree)
    module = Module(path=path, language=Language.PYTHON)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_durable_handler(node, ctx.aliases):
            continue
        handler = Handler(name=node.name, loc=_loc(path, node), language=Language.PYTHON)
        nested, step_bodies = _collect_nested(node)
        _Walker(path, ctx, handler, nested, step_bodies).walk_function(
            node, Region.DURABLE
        )
        module.handlers.append(handler)

    return module


def parse_file(path: str) -> Module:
    with open(path, encoding="utf-8") as fh:
        return parse_source(fh.read(), path)
