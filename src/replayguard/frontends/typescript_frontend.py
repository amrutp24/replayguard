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

#: How far to follow calls out of the handler. The cost here is depth, not
#: breadth: each level lengthens the path a reader has to hold in their head,
#: and real handlers reach their I/O in one or two hops. Five is generous and
#: still terminates on input designed to be pathological.
_MAX_CALL_DEPTH = 5


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


#: Wrappers that sit between a call and its callee and must be seen through.
_TRANSPARENT = {"await_expression", "parenthesized_expression", "non_null_expression"}


def _unwrap(node):
    """Strip wrappers from a callee.

    `await context.step<T>(...)` parses with the *await_expression* as the
    call's `function` field, wrapping the member expression -- a grammar quirk
    that only shows up when a generic type argument is present. Without this,
    every `await context.waitForCallback<T>(...)` went unrecognised and its body
    was analysed in the durable region, which is exactly the false-positive
    class this tool exists to avoid.
    """
    while node is not None and node.type in _TRANSPARENT:
        named = [c for c in node.children if c.is_named]
        if not named:
            return node
        node = named[-1]
    return node


def _is_top_level_function(node) -> bool:
    """True when nothing but the module encloses this function.

    This is what decides whether the caller's locals are visible inside a
    callee. A nested function is a closure and genuinely can see them; a
    top-level one cannot, and letting the caller's scope leak in would make an
    ordinary local of the helper look like a captured variable -- which inside
    a step body reads as RG003, on correct code.
    """
    parent = node.parent
    while parent is not None:
        if parent.type in _FUNCTION_NODES:
            return False
        parent = parent.parent
    return True


#: Contexts in which a function is *stored* rather than invoked. A closure put
#: into an object or an array runs wherever something later calls it, which is
#: not knowable here.
_STORED_PARENTS = {"pair", "array"}


def _is_stored_closure(node) -> bool:
    """True when a function is being saved into a data structure, not called.

    The saga pattern does exactly this: compensation closures are pushed into an
    array at handler top level and executed much later from inside a step. Taking
    the definition site as the execution context reported every nondeterministic
    call they reach -- five false positives in one file, while a structurally
    identical helper called directly from inside a step was correctly silent.
    """
    parent = node.parent
    return parent is not None and parent.type in _STORED_PARENTS


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
        """True only for a direct `new SomethingClient(...)` construction.

        Deliberately narrow. An earlier version treated any initializer
        *mentioning* a client as a client, which propagated the taint into
        response data: `response = await context.step(..., () => bedrock.send())`
        then `output = response.output.message` made `output.content.find(...)`
        -- an ordinary Array.prototype.find -- report as external I/O.

        A client is a client. Its response is data.
        """
        if value is None or value.type != "new_expression":
            return False
        ctor = src.dotted(value.child_by_field_name("constructor"), {})
        return bool(ctor) and ctor.split(".")[-1].endswith("Client")


class _Walker:
    def __init__(self, src: _Src, ctx: _ModuleContext, handler: Handler):
        self.src = src
        self.ctx = ctx
        self.h = handler
        self.scopes: list[set[str]] = []
        #: Helpers entered to reach the code being walked, outermost first.
        #: Stamped onto every finding, because a violation inside a helper is
        #: invisible at the call site the developer is looking at.
        self.via: tuple[str, ...] = ()
        #: (function, region) pairs already walked or currently being walked.
        #: Both halves matter: without the node, mutual recursion never
        #: terminates; without the region, a helper called from both sides of
        #: the replay boundary would only be analysed on the side reached
        #: first, and the two sides have opposite obligations.
        self._walked: set[tuple[int, Region]] = set()

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

    def _is_bound_local(self, name: str) -> bool:
        """True when the name is a variable in scope, i.e. data rather than a
        function passed by reference."""
        return any(name in scope for scope in self.scopes)

    def _is_module_level(self, name: str) -> bool:
        return name in self.ctx.module_names and not any(
            name in s for s in self.scopes[:-1]
        )

    # -- entry ------------------------------------------------------------

    def walk_function(self, fn, region: Region) -> None:
        # Idempotent per (function, region). No function legitimately needs
        # walking twice in one region -- reaching one twice means recursion, or
        # a body reachable both as a step callback and by a direct call -- and
        # walking it again only duplicates its findings.
        key = (fn.id, region)
        if key in self._walked:
            return
        self._walked.add(key)

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
                        via=self.via,
                    )
                )
        if left is not None:
            self.visit(left, region)

    _v_augmented_assignment_expression = _v_assignment_expression

    def _v_update_expression(self, node, region: Region) -> None:
        """`n++` and `n--` mutate, and tree-sitter gives them their own node.

        Handling only assignment_expression missed the increment form entirely,
        which hid a genuine RG003 -- a counter incremented in a step body and
        read back outside it into the handler's return value.
        """
        target = next((c for c in node.children if c.is_named), None)
        if region is Region.STEP_BODY and target is not None:
            root = self.src.root_name(target, self.ctx.aliases)
            if root and self._is_outer(root):
                self.h.outer_writes.append(
                    OuterWrite(
                        target=root,
                        loc=self.src.loc(node),
                        region=region,
                        is_global=self._is_module_level(root),
                        via=self.via,
                    )
                )
        for child in node.children:
            self.visit(child, region)

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

    def _v_arrow_function(self, node, region: Region) -> None:
        """A stored closure runs at an unknown time, so it gets an unknown region.

        Rules never fire on UNKNOWN, and the site is recorded as a coverage gap
        instead -- the checker says "I could not tell" rather than guessing, which
        is the whole point of RG900.
        """
        if region is not Region.UNKNOWN and _is_stored_closure(node):
            self.h.unresolved.append(self.src.loc(node))
            region = Region.UNKNOWN
        for child in node.children:
            self.visit(child, region)

    _v_function_expression = _v_arrow_function

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
                    via=self.via,
                )
            )
            self._record_mutating_call(node, func, region)

        resolved = self._resolve_callee(func)
        if resolved is not None:
            self._walk_helper(resolved[0], resolved[1], region)

        for child in node.children:
            self.visit(child, region)

    # -- following calls out of the handler -------------------------------

    def _resolve_callee(self, func):
        """Resolve a callee to a function defined in this same file, if it is one.

        Stopping at the handler body passes clean on a handler whose only job is
        to call `readConfig()` -- which is the shape most real handlers have, so
        the check was silent on exactly the code it exists for.
        """
        func = _unwrap(func)
        if func is None or func.type != "identifier":
            # A method call needs a receiver type, which this frontend does not
            # track. Following only bare names keeps resolution honest.
            return None
        name = self.src.text(func)
        target = self.ctx.declared.get(name)
        if target is None:
            return None
        # A local binding of the same name shadows a top-level declaration, and
        # what it holds -- a parameter, a callback off `event` -- is unknown.
        # Walking the top-level body would then report a function that this
        # call never reaches.
        if _is_top_level_function(target) and self._is_bound_local(name):
            return None
        return name, target

    def _walk_helper(self, name: str, fn, region: Region) -> None:
        """Walk a resolved callee's body in the *caller's* region.

        The region travels with the call. A helper invoked from the durable
        region inherits the durable obligation -- its `fetch` re-runs on every
        replay just as surely as one written inline -- and the same helper
        invoked from inside a step body inherits none of it.
        """
        if (fn.id, region) in self._walked:
            return
        if len(self.via) >= _MAX_CALL_DEPTH:
        # Past the cap the chain is not analysed. Record it as a coverage
        # gap rather than stopping quietly: a silently truncated walk implies
        # a clean bill of health the checker has not earned, which is the one
        # thing RG900 exists to prevent. Depth-5 chains are rare, so this
        # cannot reproduce the note-flood the RG900 tightening fixed.
            self.h.unresolved.append(self.src.loc(fn))
            return

        outer_scopes, outer_via = self.scopes, self.via
        # A top-level function is not a closure: it can see module scope and
        # its own locals, nothing of the caller's. A nested one is, so its
        # captured bindings must stay visible.
        if _is_top_level_function(fn):
            self.scopes = []
        self.via = (*self.via, name)
        try:
            self.walk_function(fn, region)
        finally:
            self.scopes, self.via = outer_scopes, outer_via

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
                    via=self.via,
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
                    via=self.via,
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
            Branch(
                loc=self.src.loc(node),
                region=region,
                condition_symbols=symbols,
                via=self.via,
            )
        )

    def _try_durable_operation(self, node, region: Region) -> bool:
        func = _unwrap(node.child_by_field_name("function"))
        if func is None or func.type != "member_expression":
            return False
        prop = func.child_by_field_name("property")
        obj = func.child_by_field_name("object")
        if prop is None or self.src.text(prop) not in _STEP_METHODS:
            return False
        if obj is None or obj.type != "identifier" or not _is_context_name(self.src.text(obj)):
            return False

        args = node.child_by_field_name("arguments")
        positional = [c for c in args.children if c.is_named] if args is not None else []

        # JS puts the name first and the callback second, the reverse of Python
        # -- but options objects and extra arguments shift it, so the body is
        # located by kind rather than by index, as in the other frontends.
        name_node = positional[0] if positional else None
        body_node = next(
            (
                a
                for a in positional
                if a.type in _FUNCTION_NODES
                or (a.type == "identifier" and self.src.text(a) in self.ctx.declared)
            ),
            None,
        )

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

        # Everything that is not the body evaluates in the durable region --
        # including a computed name expression, whose calls are violations in
        # their own right and not merely a bad step name.
        #
        # `arg is not body_node` matters: operations without a name, such as
        # `runInChildContext(fn)` and `context.step(fn)`, make positional[0]
        # the body itself. Visiting it here as well walked the whole body twice,
        # once in the wrong region, which reported every in-step call as a
        # durable-region violation.
        for arg in positional:
            if arg is body_node or arg.type == "array":
                continue
            if arg is name_node and arg.type == "string":
                continue
            self.visit(arg, region)

        # `parallel([fnA, fnB])` and `map(items, fn)` pass their branch bodies in
        # an array. Those elements execute *as* the durable operation, so they
        # are step bodies -- not stored closures of unknown timing, which is what
        # the stored-closure guard would otherwise make them.
        for arg in positional:
            if arg.type != "array":
                continue
            for element in arg.children:
                if element.is_named and element.type in _FUNCTION_NODES:
                    self.walk_function(element, Region.STEP_BODY)

        if body_node is None:
            # An identifier that is not a known declaration and not a local is
            # most likely a function from elsewhere; anything else is data.
            # A callable passed by reference is a declaration or an import, so
            # it is not bound as a local. A locally-bound name is data --
            # `context.invoke(fn, payload)` passes a payload, not a body.
            for arg in positional[1:]:
                if arg.type == "member_expression":
                    self.h.unresolved.append(self.src.loc(arg))
                    break
                if arg.type == "identifier" and not self._is_bound_local(
                    self.src.text(arg)
                ):
                    self.h.unresolved.append(self.src.loc(arg))
                    break
            return True
        if body_node.type in _FUNCTION_NODES:
            self.walk_function(body_node, Region.STEP_BODY)
        else:
            self.walk_function(self.ctx.declared[self.src.text(body_node)], Region.STEP_BODY)
        return True

    def _step_name(self, name_node) -> tuple[str | None, bool, list[str]]:
        """Resolve the operation name, if this overload has one at all.

        `parallel(branches)` and `map(items, fn)` take no name -- argument 0 is
        an array, not a string. Treating it as a name bound the whole array of
        branch bodies and scanned them for clocks, so RG005 reported a span
        covering 200 lines of a file whose author had in fact named every one of
        its 30 operations with a string literal. Only a string or a template
        literal is a name; anything else means the call is unnamed.
        """
        if name_node is None:
            return None, True, []
        if name_node.type == "string":
            return self.src.text(name_node).strip("\"'"), True, []
        if name_node.type != "template_string":
            return None, True, []
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
