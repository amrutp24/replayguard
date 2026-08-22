"""Java frontend, built on tree-sitter.

Third SDK shape, third set of language semantics, same IR:

    Python  @durable_execution          context.step(fn, name="x")
    JS/TS   withDurableExecution(fn)    context.step("x", fn)
    Java    extends DurableHandler<,>   ctx.step("x", Result.class, stepCtx -> ...)

Java puts the callback *third*, behind a Class token, and also has a two-argument
overload. So the body is located by finding the lambda among the arguments
rather than by position -- assuming a fixed index would silently miss half the
call sites.

The interesting difference is in RG003. Java requires captured locals to be
effectively final, so reassigning one inside a lambda is a compile error and
that entire class of violation cannot occur. What remains legal, and is
therefore the real bug here, is mutating a captured mutable object
(`receipts.add(x)`) and writing to instance or static fields. So Java's outer
write surface is narrower than JavaScript's and shaped differently from
Python's, and that knowledge lives in this file rather than in the rules.
"""

from __future__ import annotations

from ..catalog import JAVA_TAINTED_TYPES
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

#: Base class that marks a durable handler, plus the context parameter type,
#: which is the more reliable signal of the two.
_DURABLE_BASE = "DurableHandler"
_CONTEXT_TYPE = "DurableContext"

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

#: In-place mutators on the common collection types.
_MUTATORS = {
    "add",
    "addAll",
    "addFirst",
    "addLast",
    "put",
    "putAll",
    "putIfAbsent",
    "remove",
    "removeAll",
    "removeIf",
    "clear",
    "set",
    "offer",
    "push",
    "poll",
    "sort",
    "merge",
    "compute",
    "computeIfAbsent",
    "replaceAll",
}

_LAMBDA_NODES = {"lambda_expression", "method_reference"}


def _load_parser():
    try:
        import tree_sitter_java as tsj
        from tree_sitter import Language as TSLanguage
        from tree_sitter import Parser
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Java support needs the optional dependencies: "
            "pip install 'replayguard[java]'"
        ) from exc

    return Parser(TSLanguage(tsj.language()))


class _Src:
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

    def dotted(self, node) -> str | None:
        """Best-effort qualified name for an invocation or field access."""
        if node is None:
            return None
        t = node.type
        if t in ("identifier", "type_identifier", "scoped_type_identifier"):
            return self.text(node)
        if t == "field_access":
            obj = self.dotted(node.child_by_field_name("object"))
            fld = node.child_by_field_name("field")
            return f"{obj}.{self.text(fld)}" if obj and fld is not None else obj
        if t == "method_invocation":
            obj = node.child_by_field_name("object")
            name = node.child_by_field_name("name")
            if name is None:
                return None
            base = self.dotted(obj) if obj is not None else None
            return f"{base}.{self.text(name)}" if base else self.text(name)
        if t == "object_creation_expression":
            return self.dotted(node.child_by_field_name("type"))
        if t == "generic_type":
            return self.dotted(node.child(0))
        if t == "scoped_identifier":
            return self.text(node)
        return None

    def root_name(self, node) -> str | None:
        """Leftmost identifier of an access chain."""
        while node is not None and node.type in (
            "field_access",
            "method_invocation",
            "array_access",
            "parenthesized_expression",
        ):
            nxt = node.child_by_field_name("object") or node.child_by_field_name("array")
            if nxt is None:
                return None
            node = nxt
        return self.text(node) if node is not None and node.type == "identifier" else None


def _descend(node, types: set[str]):
    for child in node.children:
        if child.type in types:
            yield child
        yield from _descend(child, types)


def _base_type(src: _Src, type_node) -> str | None:
    """Strip generics: `List<String>` -> `List`, `Random` -> `Random`."""
    if type_node is None:
        return None
    if type_node.type == "generic_type":
        return src.text(type_node.child(0))
    return src.text(type_node)


class _ClassContext:
    """Class-level facts: fields, and which names carry a tainted type."""

    def __init__(self, src: _Src, root):
        self.fields: set[str] = set()
        #: name -> Category, for declared types that are nondeterministic
        #: wholesale (a `Random` instance, an `HttpClient`).
        self.tainted: dict[str, object] = {}
        #: Names holding an AWS SDK client. Java clients are conventionally
        #: typed `SomethingClient`, which is a reliable enough signal.
        self.aws_names: set[str] = set()

        for decl in _descend(root, {"field_declaration"}):
            base = _base_type(src, decl.child_by_field_name("type"))
            for var in _descend(decl, {"variable_declarator"}):
                name_node = var.child_by_field_name("name")
                if name_node is None:
                    continue
                name = src.text(name_node)
                self.fields.add(name)
                self._classify(name, base)

    def _classify(self, name: str, base: str | None) -> None:
        if not base:
            return
        if base in JAVA_TAINTED_TYPES:
            self.tainted[name] = JAVA_TAINTED_TYPES[base]
        if base.endswith("Client"):
            self.aws_names.add(name)


class _Walker:
    def __init__(self, src: _Src, ctx: _ClassContext, handler: Handler):
        self.src = src
        self.ctx = ctx
        self.h = handler
        self.scopes: list[set[str]] = []
        #: Locals whose declared type is tainted, layered over class fields.
        self.local_tainted: dict[str, object] = {}
        self.local_aws: set[str] = set()

    # -- scope ------------------------------------------------------------

    def _bind(self, name: str) -> None:
        if self.scopes:
            self.scopes[-1].add(name)

    def _is_outer(self, name: str) -> bool:
        if self.scopes and name in self.scopes[-1]:
            return False
        if any(name in s for s in self.scopes[:-1]):
            return True
        return name in self.ctx.fields

    def _is_field(self, name: str) -> bool:
        return name in self.ctx.fields and not any(name in s for s in self.scopes)

    def _is_aws(self, name: str | None) -> bool:
        return bool(name) and (name in self.ctx.aws_names or name in self.local_aws)

    # -- entry ------------------------------------------------------------

    def walk_callable(self, node, region: Region) -> None:
        self.scopes.append(set())
        params = node.child_by_field_name("parameters")
        if params is not None:
            for p in _descend(params, {"formal_parameter", "inferred_parameters"}):
                for ident in _descend(p, {"identifier"}):
                    self._bind(self.src.text(ident))
            # `x -> ...` has a bare identifier parameter, no formal_parameter node.
            if params.type == "identifier":
                self._bind(self.src.text(params))
        elif node.type == "lambda_expression":
            first = node.child(0)
            if first is not None and first.type == "identifier":
                self._bind(self.src.text(first))

        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, region)
        self.scopes.pop()

    # -- dispatch ---------------------------------------------------------

    def visit(self, node, region: Region) -> None:
        fn = getattr(self, f"_v_{node.type}", None)
        if fn is not None:
            fn(node, region)
            return
        for child in node.children:
            self.visit(child, region)

    # -- declarations -----------------------------------------------------

    def _v_local_variable_declaration(self, node, region: Region) -> None:
        base = _base_type(self.src, node.child_by_field_name("type"))
        for var in _descend(node, {"variable_declarator"}):
            value = var.child_by_field_name("value")
            if value is not None:
                self.visit(value, region)
            name_node = var.child_by_field_name("name")
            if name_node is None:
                continue
            name = self.src.text(name_node)
            self._bind(name)
            if base and base in JAVA_TAINTED_TYPES:
                self.local_tainted[name] = JAVA_TAINTED_TYPES[base]
            if base and base.endswith("Client"):
                self.local_aws.add(name)

    def _v_assignment_expression(self, node, region: Region) -> None:
        right = node.child_by_field_name("right")
        if right is not None:
            self.visit(right, region)
        left = node.child_by_field_name("left")
        if region is Region.STEP_BODY and left is not None:
            self._record_assignment(left, node)
        if left is not None:
            self.visit(left, region)

    # -- control flow -----------------------------------------------------

    def _v_if_statement(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_while_statement(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_ternary_expression(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    # -- calls ------------------------------------------------------------

    def _v_method_invocation(self, node, region: Region) -> None:
        if self._try_durable_operation(node, region):
            return

        dotted = self.src.dotted(node)
        obj = node.child_by_field_name("object")
        root = self.src.root_name(obj) if obj is not None else None

        if dotted:
            # A method on a `Random`/`HttpClient` instance is nondeterministic
            # regardless of its name, so surface it under the catalogued key.
            tainted = self.local_tainted.get(root) or self.ctx.tainted.get(root)
            effective = dotted
            if tainted is not None and root is not None:
                effective = _TAINT_KEY[tainted]
            self.h.calls.append(
                Call(
                    dotted=effective,
                    loc=self.src.loc(node),
                    region=region,
                    external_client=self._is_aws(root),
                    display=dotted if effective != dotted else None,
                )
            )
            self._record_mutating_call(node, region)

        for child in node.children:
            self.visit(child, region)

    def _v_object_creation_expression(self, node, region: Region) -> None:
        name = self.src.dotted(node.child_by_field_name("type"))
        args = node.child_by_field_name("arguments")
        argc = len([c for c in args.children if c.is_named]) if args is not None else 0
        # `new Date()` reads the clock; `new Date(millis)` does not. Same for
        # `new Random()` versus a seeded `new Random(42)`.
        if name in ("Date", "Random") and argc > 0:
            pass
        elif name:
            self.h.calls.append(
                Call(
                    dotted=name,
                    loc=self.src.loc(node),
                    region=region,
                    display=f"new {name}()",
                )
            )
        for child in node.children:
            self.visit(child, region)

    # -- recording --------------------------------------------------------

    def _record_assignment(self, left, node) -> None:
        """Field, qualified, and array writes reach outside the lambda.

        A bare captured-local reassignment cannot appear here: Java requires
        captured locals to be effectively final, so javac rejects it long before
        this checker sees it. That is a whole violation class JavaScript has and
        Java does not.

        The three shapes are handled separately because `this.x` has a `this`
        node rather than an identifier at the root of its access chain, so a
        generic chain walk returns nothing for it.
        """
        def emit(target: str, is_global: bool) -> None:
            self.h.outer_writes.append(
                OuterWrite(
                    target=target,
                    loc=self.src.loc(node),
                    region=Region.STEP_BODY,
                    is_global=is_global,
                )
            )

        if left.type == "identifier":
            name = self.src.text(left)
            if self._is_field(name):
                emit(name, True)
            return

        if left.type == "field_access":
            obj = left.child_by_field_name("object")
            fld = left.child_by_field_name("field")
            # `this.x` / `super.x` is unambiguously an instance field write.
            if obj is not None and obj.type in ("this", "super") and fld is not None:
                emit(self.src.text(fld), True)
                return
            root = self.src.root_name(left)
            if root and self._is_outer(root):
                emit(root, self._is_field(root))
            return

        if left.type == "array_access":
            root = self.src.root_name(left)
            if root and self._is_outer(root):
                emit(root, self._is_field(root))

    def _record_mutating_call(self, node, region: Region) -> None:
        if region is not Region.STEP_BODY:
            return
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None or obj is None:
            return
        if self.src.text(name_node) not in _MUTATORS:
            return
        root = self.src.root_name(obj)
        if root and self._is_outer(root):
            self.h.outer_writes.append(
                OuterWrite(
                    target=root,
                    loc=self.src.loc(node),
                    region=region,
                    is_global=self._is_field(root),
                )
            )

    def _record_branch(self, condition, region: Region, node) -> None:
        if condition is None:
            return
        symbols: list[str] = []
        for sub in [
            condition,
            *_descend(condition, {"method_invocation", "field_access", "object_creation_expression"}),
        ]:
            d = self.src.dotted(sub)
            if d:
                symbols.append(d)
            root = self.src.root_name(sub)
            tainted = self.local_tainted.get(root) or self.ctx.tainted.get(root)
            if tainted is not None:
                symbols.append(_TAINT_KEY[tainted])
        self.h.branches.append(
            Branch(loc=self.src.loc(node), region=region, condition_symbols=symbols)
        )

    def _try_durable_operation(self, node, region: Region) -> bool:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None or obj is None:
            return False
        if self.src.text(name_node) not in _STEP_METHODS:
            return False
        if obj.type != "identifier" or not _is_context_name(self.src.text(obj)):
            return False

        args = node.child_by_field_name("arguments")
        positional = [c for c in args.children if c.is_named] if args is not None else []

        # Java has both `step(name, lambda)` and `step(name, Type.class, lambda)`,
        # so the body is found by kind rather than by index.
        body_node = next((a for a in positional if a.type in _LAMBDA_NODES), None)
        name_node_arg = positional[0] if positional else None

        name_literal, name_is_static, name_symbols = self._step_name(name_node_arg)
        self.h.steps.append(
            Step(
                kind=self.src.text(name_node),
                loc=self.src.loc(node),
                name_literal=name_literal,
                name_is_static=name_is_static,
                name_symbols=name_symbols,
            )
        )

        for arg in positional:
            if arg is body_node:
                continue
            # Everything that isn't the body evaluates in the durable region.
            if arg is name_node_arg and arg.type == "string_literal":
                continue
            self.visit(arg, region)

        if body_node is None:
            # Plenty of durable operations take no callable at all -- `wait`,
            # `createCallback`. Reporting those as coverage gaps buried the real
            # findings, so absence of a lambda is not by itself a gap.
            return True
        if body_node.type == "method_reference":
            # `this::doWork` -- resolving it needs interprocedural analysis.
            self.h.unresolved.append(self.src.loc(body_node))
            return True
        self.walk_callable(body_node, Region.STEP_BODY)
        return True

    def _step_name(self, name_node) -> tuple[str | None, bool, list[str]]:
        if name_node is None:
            return None, True, []
        if name_node.type == "string_literal":
            return self.src.text(name_node).strip('"'), True, []
        symbols: list[str] = []
        for sub in [
            name_node,
            *_descend(name_node, {"method_invocation", "field_access", "object_creation_expression"}),
        ]:
            d = self.src.dotted(sub)
            if d:
                symbols.append(d)
        return None, False, symbols


#: Catalog key to report a tainted-instance call under.
_TAINT_KEY: dict[object, str] = {}


def _init_taint_keys() -> None:
    from ..catalog import Category

    _TAINT_KEY.update(
        {
            Category.RANDOM: "Random",
            Category.NETWORK: "HttpClient.newHttpClient",
            Category.CLOCK: "Instant.now",
            Category.FILESYSTEM: "Files.write",
            Category.IDENTITY: "UUID.randomUUID",
            Category.PROCESS: "Thread.currentThread",
        }
    )


_init_taint_keys()


def _find_handlers(src: _Src, root):
    """Methods taking a DurableContext parameter, or overriding DurableHandler."""
    found = []
    for method in _descend(root, {"method_declaration"}):
        params = method.child_by_field_name("parameters")
        if params is None:
            continue
        has_context = any(
            _base_type(src, p.child_by_field_name("type")) == _CONTEXT_TYPE
            for p in _descend(params, {"formal_parameter"})
        )
        if has_context:
            name_node = method.child_by_field_name("name")
            found.append((src.text(name_node) if name_node else "handleRequest", method))
    return found


def parse_source(source: str, path: str) -> Module:
    parser = _load_parser()
    data = source.encode("utf8")
    tree = parser.parse(data)
    src = _Src(data, path)
    ctx = _ClassContext(src, tree.root_node)
    module = Module(path=path, language=Language.JAVA)

    for name, method in _find_handlers(src, tree.root_node):
        handler = Handler(name=name, loc=src.loc(method), language=Language.JAVA)
        walker = _Walker(src, ctx, handler)
        walker.scopes.append(set())
        params = method.child_by_field_name("parameters")
        if params is not None:
            for p in _descend(params, {"formal_parameter"}):
                n = p.child_by_field_name("name")
                if n is not None:
                    walker._bind(src.text(n))
        body = method.child_by_field_name("body")
        if body is not None:
            walker.visit(body, Region.DURABLE)
        walker.scopes.pop()
        module.handlers.append(handler)

    return module


def parse_file(path: str) -> Module:
    with open(path, encoding="utf-8") as fh:
        return parse_source(fh.read(), path)
