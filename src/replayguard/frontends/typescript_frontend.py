"""TypeScript / JavaScript frontend, built on tree-sitter.

Lowers to the same IR as the Python frontend, which is the point of having an
IR at all — the two SDKs do not look alike:

    Python    @durable_execution           context.step(fn, name="x")
    JS/TS     withDurableExecution(fn)     context.step("x", fn)

The name and the callback are in opposite positions. Rules never learn this.

One genuine semantic difference does have to be encoded here rather than in the
rules: in Python a bare `x = 1` inside a nested function creates a *local*
binding, so it can never be an outer write. In JavaScript, assigning to a name
declared in an enclosing scope writes straight through to it. So RG003 has more
ways to fire in JS, and that lives in this file.
"""

from __future__ import annotations

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

#: Wrapper that marks a durable handler.
_DURABLE_WRAPPERS = {"withDurableExecution", "durableExecution"}

_STEP_METHODS = {"step", "waitForCallback", "wait_for_callback", "invoke", "parallel"}

_CONTEXT_NAMES = {"context", "ctx", "durableContext", "dc"}

#: In-place mutators. `receipts.push(x)` inside a step body is the exact case
#: AWS documents as silently lost on replay.
_MUTATORS = {
    "push",
    "pop",
    "shift",
    "unshift",
    "splice",
    "sort",
    "reverse",
    "fill",
    "copyWithin",
    "set",
    "add",
    "delete",
    "clear",
}

_AWS_MODULE_PREFIXES = ("@aws-sdk/", "aws-sdk")

_FUNCTION_NODES = {"arrow_function", "function_expression", "function_declaration"}


def _load_parser(dialect: str):
    """Build a parser lazily so the Python frontend needs no tree-sitter."""
    try:
        import tree_sitter_typescript as tst
        from tree_sitter import Language, Parser
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "TypeScript support needs the optional dependencies: "
            "pip install 'replayguard[typescript]'"
        ) from exc

    grammar = tst.language_tsx() if dialect == "tsx" else tst.language_typescript()
    return Parser(Language(grammar))


class _Src:
    """Source buffer plus the node helpers tree-sitter doesn't provide."""

    def __init__(self, source: bytes, path: str):
        self.b = source
        self.path = path

    def text(self, node) -> str:
        return self.b[node.start_byte : node.end_byte].decode("utf8", "replace")

    def loc(self, node) -> Location:
        return Location(
            file=self.path,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
        )

    def dotted(self, node, aliases: dict[str, str]) -> str | None:
        """Best-effort qualified name for an identifier or member expression."""
        if node is None:
            return None
        if node.type == "identifier":
            name = self.text(node)
            return aliases.get(name, name)
        if node.type in ("member_expression", "subscript_expression"):
            obj = self.dotted(node.child_by_field_name("object"), aliases)
            prop = node.child_by_field_name("property")
            if obj and prop is not None:
                return f"{obj}.{self.text(prop)}"
            return obj
        if node.type in ("call_expression", "new_expression"):
            target = node.child_by_field_name("function") or node.child_by_field_name(
                "constructor"
            )
            return self.dotted(target, aliases)
        if node.type == "await_expression":
            return self.dotted(node.children[-1] if node.children else None, aliases)
        return None

    def root_name(self, node, aliases: dict[str, str]) -> str | None:
        while node is not None and node.type in (
            "member_expression",
            "subscript_expression",
            "call_expression",
            "await_expression",
            "non_null_expression",
        ):
            nxt = (
                node.child_by_field_name("object")
                or node.child_by_field_name("function")
                or (node.children[-1] if node.type == "await_expression" else None)
            )
            if nxt is None:
                return None
            node = nxt
        return self.text(node) if node is not None and node.type == "identifier" else None


def _descend(node, types: set[str]):
    """Yield descendants of the given types, without leaving nested functions."""
    for child in node.children:
        if child.type in types:
            yield child
        yield from _descend(child, types)


class _ModuleContext:
    def __init__(self, src: _Src, root):
        self.aliases: dict[str, str] = {}
        self.module_names: set[str] = set()
        self.aws_names: set[str] = set()
        #: Top-level function/arrow declarations, so a handler or step body
        #: passed by reference can still be analysed.
        self.declared: dict[str, object] = {}

        for node in _descend(root, {"import_statement"}):
            source_node = node.child_by_field_name("source")
            module = src.text(source_node).strip("\"'") if source_node else ""
            for spec in _descend(node, {"import_specifier", "namespace_import"}):
                name_node = spec.child_by_field_name("alias") or spec.child_by_field_name(
                    "name"
                )
                if name_node is None:
                    continue
                local = src.text(name_node)
                origin = src.text(spec.child_by_field_name("name") or name_node)
                self.aliases[local] = f"{module}.{origin}" if module.startswith(".") is False and module else origin
                if module.startswith(_AWS_MODULE_PREFIXES):
                    self.aws_names.add(local)

        for node in _descend(root, {"variable_declarator", "function_declaration"}):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = src.text(name_node)
            self.module_names.add(name)
            value = node.child_by_field_name("value")
            if node.type == "function_declaration":
                self.declared[name] = node
            elif value is not None and value.type in _FUNCTION_NODES:
                self.declared[name] = value
            if value is not None and self._is_aws_derived(src, value):
                self.aws_names.add(name)

    def _is_aws_derived(self, src: _Src, value) -> bool:
        for node in [value, *_descend(value, {"identifier", "new_expression"})]:
            name = src.root_name(node, {}) or (
                src.text(node) if node.type == "identifier" else None
            )
            if name and name in self.aws_names:
                return True
            if name and name.endswith("Client"):
                return True
        return False


class _Walker:
    def __init__(self, src: _Src, ctx: _ModuleContext, handler: Handler):
        self.src = src
        self.ctx = ctx
        self.h = handler
        self.scopes: list[set[str]] = []

    # -- scope ------------------------------------------------------------

    def _bind(self, name: str) -> None:
        if self.scopes:
            self.scopes[-1].add(name)

    def _is_outer(self, name: str) -> bool:
        if self.scopes and name in self.scopes[-1]:
            return False
        if any(name in s for s in self.scopes[:-1]):
            return True
        return name in self.ctx.module_names

    def _is_module_level(self, name: str) -> bool:
        return name in self.ctx.module_names and not any(
            name in s for s in self.scopes[:-1]
        )

    # -- entry ------------------------------------------------------------

    def walk_function(self, fn, region: Region) -> None:
        self.scopes.append(set())
        params = fn.child_by_field_name("parameters")
        if params is not None:
            for ident in _descend(params, {"identifier"}):
                self._bind(self.src.text(ident))
        body = fn.child_by_field_name("body")
        if body is not None:
            self.visit(body, region)
        self.scopes.pop()

    # -- dispatch ---------------------------------------------------------

    def visit(self, node, region: Region) -> None:
        handler = getattr(self, f"_v_{node.type}", None)
        if handler is not None:
            handler(node, region)
            return
        for child in node.children:
            self.visit(child, region)

    # -- declarations & assignment ----------------------------------------

    def _v_variable_declarator(self, node, region: Region) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value, region)
        # Bind *after* visiting the initializer so `const x = x` reads the outer.
        if name_node is not None:
            for ident in _descend(name_node, {"identifier"}) or []:
                self._bind(self.src.text(ident))
            if name_node.type == "identifier":
                self._bind(self.src.text(name_node))

    def _v_assignment_expression(self, node, region: Region) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if right is not None:
            self.visit(right, region)
        if region is Region.STEP_BODY and left is not None:
            # Unlike Python, a bare assignment in JS writes through to an
            # enclosing binding rather than creating a local one.
            root = self.src.root_name(left, self.ctx.aliases)
            if root and self._is_outer(root):
                self.h.outer_writes.append(
                    OuterWrite(
                        target=root,
                        loc=self.src.loc(node),
                        region=region,
                        is_global=self._is_module_level(root),
                    )
                )
        if left is not None:
            self.visit(left, region)

    _v_augmented_assignment_expression = _v_assignment_expression

    # -- control flow -----------------------------------------------------

    def _v_if_statement(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_ternary_expression(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_while_statement(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    # -- calls ------------------------------------------------------------

    def _v_call_expression(self, node, region: Region) -> None:
        if self._try_durable_operation(node, region):
            return

        func = node.child_by_field_name("function")
        dotted = self.src.dotted(func, self.ctx.aliases)
        if dotted:
            root = self.src.root_name(func, self.ctx.aliases)
            self.h.calls.append(
                Call(
                    dotted=dotted,
                    loc=self.src.loc(node),
                    region=region,
                    external_client=bool(root and root in self.ctx.aws_names),
                )
            )
            self._record_mutating_call(node, func, region)

        for child in node.children:
            self.visit(child, region)

    def _v_new_expression(self, node, region: Region) -> None:
        ctor = node.child_by_field_name("constructor")
        args = node.child_by_field_name("arguments")
        name = self.src.dotted(ctor, self.ctx.aliases)
        # `new Date()` reads the clock; `new Date(isoString)` is deterministic.
        arg_count = (
            len([c for c in args.children if c.is_named]) if args is not None else 0
        )
        if name == "Date" and arg_count == 0:
            self.h.calls.append(
                Call(
                    dotted="Date",
                    loc=self.src.loc(node),
                    region=region,
                    display="new Date()",
                )
            )
        for child in node.children:
            self.visit(child, region)

    # -- recording --------------------------------------------------------

    def _record_mutating_call(self, node, func, region: Region) -> None:
        if region is not Region.STEP_BODY or func is None:
            return
        if func.type != "member_expression":
            return
        prop = func.child_by_field_name("property")
        if prop is None or self.src.text(prop) not in _MUTATORS:
            return
        root = self.src.root_name(func.child_by_field_name("object"), self.ctx.aliases)
        if root and self._is_outer(root):
            self.h.outer_writes.append(
                OuterWrite(
                    target=root,
                    loc=self.src.loc(node),
                    region=region,
                    is_global=self._is_module_level(root),
                )
            )

    def _record_branch(self, condition, region: Region, node) -> None:
        if condition is None:
            return
        symbols: list[str] = []
        for sub in [condition, *_descend(condition, {"call_expression", "member_expression", "new_expression"})]:
            d = self.src.dotted(sub, self.ctx.aliases)
            if d:
                symbols.append(d)
            if sub.type == "new_expression":
                ctor = self.src.dotted(sub.child_by_field_name("constructor"), self.ctx.aliases)
                if ctor:
                    symbols.append(ctor)
        self.h.branches.append(
            Branch(loc=self.src.loc(node), region=region, condition_symbols=symbols)
        )

    def _try_durable_operation(self, node, region: Region) -> bool:
        func = node.child_by_field_name("function")
        if func is None or func.type != "member_expression":
            return False
        prop = func.child_by_field_name("property")
        obj = func.child_by_field_name("object")
        if prop is None or self.src.text(prop) not in _STEP_METHODS:
            return False
        if obj is None or obj.type != "identifier" or self.src.text(obj) not in _CONTEXT_NAMES:
            return False

        args = node.child_by_field_name("arguments")
        positional = [c for c in args.children if c.is_named] if args is not None else []

        # JS puts the name first and the callback second — the reverse of Python.
        name_node = positional[0] if positional else None
        body_node = positional[1] if len(positional) > 1 else None

        name_literal, name_is_static, name_symbols = self._step_name(name_node)
        self.h.steps.append(
            Step(
                kind=self.src.text(prop),
                loc=self.src.loc(node),
                name_literal=name_literal,
                name_is_static=name_is_static,
                name_symbols=name_symbols,
            )
        )

        # The name expression itself evaluates in the durable region, so any
        # call inside it is a violation in its own right — not just a bad
        # step name. Matches the Python frontend.
        if name_node is not None and name_node.type != "string":
            self.visit(name_node, region)
        for extra in positional[2:]:
            self.visit(extra, region)

        if body_node is None:
            return True
        if body_node.type in _FUNCTION_NODES:
            self.walk_function(body_node, Region.STEP_BODY)
        elif body_node.type == "identifier" and self.src.text(body_node) in self.ctx.declared:
            self.walk_function(self.ctx.declared[self.src.text(body_node)], Region.STEP_BODY)
        else:
            self.h.unresolved.append(self.src.loc(body_node))
        return True

    def _step_name(self, name_node) -> tuple[str | None, bool, list[str]]:
        if name_node is None:
            return None, True, []
        if name_node.type == "string":
            return self.src.text(name_node).strip("\"'"), True, []
        symbols: list[str] = []
        for sub in [name_node, *_descend(name_node, {"call_expression", "member_expression", "new_expression"})]:
            d = self.src.dotted(sub, self.ctx.aliases)
            if d:
                symbols.append(d)
        return None, False, symbols


def _find_handlers(src: _Src, ctx: _ModuleContext, root):
    """Locate `withDurableExecution(fn)` and yield the wrapped function node."""
    out = []
    for call in _descend(root, {"call_expression"}):
        func = call.child_by_field_name("function")
        if func is None or func.type != "identifier":
            continue
        if src.text(func) not in _DURABLE_WRAPPERS:
            continue
        args = call.child_by_field_name("arguments")
        positional = [c for c in args.children if c.is_named] if args is not None else []
        if not positional:
            continue
        target = positional[0]
        if target.type in _FUNCTION_NODES:
            out.append((src.text(func), target))
        elif target.type == "identifier" and src.text(target) in ctx.declared:
            out.append((src.text(target), ctx.declared[src.text(target)]))
    return out


def parse_source(source: str, path: str, dialect: str = "typescript") -> Module:
    parser = _load_parser(dialect)
    data = source.encode("utf8")
    tree = parser.parse(data)
    src = _Src(data, path)
    ctx = _ModuleContext(src, tree.root_node)
    module = Module(path=path, language=Language.TYPESCRIPT)

    for name, fn_node in _find_handlers(src, ctx, tree.root_node):
        handler = Handler(
            name=name, loc=src.loc(fn_node), language=Language.TYPESCRIPT
        )
        _Walker(src, ctx, handler).walk_function(fn_node, Region.DURABLE)
        module.handlers.append(handler)

    return module


def parse_file(path: str) -> Module:
    dialect = "tsx" if path.endswith((".tsx", ".jsx")) else "typescript"
    with open(path, encoding="utf-8") as fh:
        return parse_source(fh.read(), path, dialect)
