"""Rust frontend, built on tree-sitter.

A fourth SDK shape, and a fourth set of language semantics, lowering to the same
IR. AWS ships no Rust SDK; this targets the community one,
[durable-rust](https://github.com/pgdad/durable-rust), whose own documentation
says its determinism rules are *documented, not enforced* -- violations produce
runtime errors or wrong replay behaviour rather than compile errors. That is
precisely the gap this tool exists to close.

It offers four API styles, all agreeing on the operation call:

    builder   durable_lambda_builder::handler(|event, mut ctx: BuilderContext| ...)
    closure   async fn handler(event, mut ctx: ClosureContext)
    macro     #[durable_execution] async fn handler(event, mut ctx: DurableContext)
    trait     impl DurableHandler { async fn handle(&self, event, mut ctx: TraitContext) }

    ctx.step("name", || async { ... })      -- name first, like JS

**Rust's outer-write model is the narrowest of the four.** Step closures are
`Send + 'static`, so capturing a borrowed reference does not compile at all --
the whole RG003 shape that JavaScript makes easy is rejected by the borrow
checker before this tool ever sees it. What remains legal, and therefore the
real hazard here, is interior mutability through a shared handle: an
`Arc<Mutex<_>>` locked and pushed to, a `RefCell` borrowed mutably, or a
`static mut`. Those compile, and they are lost on replay exactly like any other
write inside a step body.
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
    Read,
    Region,
    Step,
)

#: Attribute marking a durable handler in the macro style.
_DURABLE_ATTRS = {"durable_execution", "durable_handler"}

#: Context parameter types across the four API styles. A parameter of one of
#: these is the most reliable handler signal, as in the Java frontend -- the
#: attribute and the trait impl are style-specific, the context is not.
_CONTEXT_TYPES = {
    "DurableContext",
    "BuilderContext",
    "ClosureContext",
    "TraitContext",
    "StepContext",
}

_STEP_METHODS = {
    "step",
    "step_with_retry",
    "wait",
    "wait_for_callback",
    "create_callback",
    "invoke",
    "parallel",
    "map",
    "child_context",
    "run_in_child_context",
    "with_retry",
}

#: Methods that mutate a shared handle in place. `lock()`/`borrow_mut()` are the
#: Rust route to the write-inside-a-step bug, since a plain `&mut` capture will
#: not compile under the `'static` bound the SDK requires.
_MUTATORS = {
    "push",
    "push_str",
    "insert",
    "remove",
    "clear",
    "extend",
    "append",
    "pop",
    "push_back",
    "push_front",
    "replace",
    "set",
    "take",
    "get_or_insert",
}

#: Interior-mutability gateways. Seeing one of these on the receiver chain is
#: what turns a method call into a shared-state write.
_INTERIOR_MUT = {"lock", "borrow_mut", "write", "get_mut", "try_lock", "try_borrow_mut"}

_CLOSURE_NODES = {"closure_expression"}

#: Wrappers that sit between an expression and the call inside it.
_TRANSPARENT = {
    "await_expression",
    "try_expression",
    "unary_expression",
    "parenthesized_expression",
    "reference_expression",
}


def _load_parser():
    try:
        import tree_sitter_rust as tsr
        from tree_sitter import Language as TSLanguage
        from tree_sitter import Parser
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Rust support needs the optional dependencies: "
            "pip install 'replayguard[rust]'"
        ) from exc

    return Parser(TSLanguage(tsr.language()))


def _unwrap(node):
    """Strip `.await`, `?`, and reference wrappers from an expression."""
    while node is not None and node.type in _TRANSPARENT:
        named = [c for c in node.children if c.is_named]
        if not named:
            return node
        node = named[-1] if node.type != "reference_expression" else named[0]
    return node


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
        """Best-effort path for a callee.

        `SystemTime::now` and `chrono::Utc::now` both arrive as
        `scoped_identifier`; a method call arrives as `field_expression`. The
        catalog is written against the `::`-joined form with the crate path
        stripped, so `chrono::Utc::now` looks up as `Utc::now`.
        """
        if node is None:
            return None
        t = node.type
        if t in ("identifier", "type_identifier", "field_identifier"):
            return self.text(node)
        if t == "scoped_identifier":
            return self.text(node).replace(" ", "")
        if t == "field_expression":
            base = self.dotted(node.child_by_field_name("value"))
            field = node.child_by_field_name("field")
            if field is None:
                return base
            return f"{base}.{self.text(field)}" if base else self.text(field)
        if t in ("call_expression", "generic_function"):
            return self.dotted(node.child_by_field_name("function"))
        if t in _TRANSPARENT:
            return self.dotted(_unwrap(node))
        return None

    def root_name(self, node) -> str | None:
        """Leftmost identifier of a receiver chain."""
        node = _unwrap(node)
        while node is not None and node.type in (
            "field_expression",
            "call_expression",
            "index_expression",
            "generic_function",
        ):
            nxt = node.child_by_field_name("value") or node.child_by_field_name("function")
            if nxt is None:
                return None
            node = _unwrap(nxt)
        return self.text(node) if node is not None and node.type == "identifier" else None


def _descend(node, types: set[str]):
    for child in node.children:
        if child.type in types:
            yield child
        yield from _descend(child, types)


def _param_types(src: _Src, params) -> list[str]:
    out: list[str] = []
    if params is None:
        return out
    for p in _descend(params, {"parameter"}):
        t = p.child_by_field_name("type")
        if t is not None:
            out.append(src.text(t).lstrip("&").replace("mut ", "").strip())
    return out


def _is_context_type(name: str) -> bool:
    base = name.split("<")[0].split("::")[-1].strip()
    return base in _CONTEXT_TYPES or base.endswith("Context")


class _ModuleContext:
    def __init__(self, src: _Src, root):
        #: `use` aliases, so `chrono::Utc` resolves when written bare.
        self.aliases: dict[str, str] = {}
        self.statics: set[str] = set()
        for use in _descend(root, {"use_declaration"}):
            text = src.text(use)
            leaf = text.rstrip(";").split("::")[-1].strip()
            if leaf and leaf.isidentifier():
                self.aliases[leaf] = text.rstrip(";").replace("use ", "").strip()
        for item in _descend(root, {"static_item", "const_item"}):
            name = item.child_by_field_name("name")
            if name is not None:
                self.statics.add(src.text(name))


class _Walker:
    def __init__(self, src: _Src, ctx: _ModuleContext, handler: Handler):
        self.src = src
        self.ctx = ctx
        self.h = handler
        self.scopes: list[set[str]] = []
        self.current_step_id: int | None = None
        self._step_counter = 0
        self.via: tuple[str, ...] = ()
        #: One map per scope, parallel to `scopes`. local -> the name it aliases. `let r = Arc::clone(&outer)` inside a
        #: step closure rebinds the name locally, so a plain scope check says
        #: "not outer" -- but the Arc points at the caller's data and a write
        #: through it is visible outside and lost on replay. This is *the*
        #: idiomatic way to share state into a Rust step, so without it RG003
        #: would never fire on real code. Scoped rather than flat because a step
        #: closure commonly rebinds the same name, and a flat map turns that
        #: shadowing into a cycle that resolves outward to the wrong name.
        self.alias_scopes: list[dict[str, str]] = []
        #: Depth of clone-expression nesting. `Arc::clone(&log)` mentions `log`
        #: but reads the *handle*, not the contents behind it -- only `.lock()`
        #: does that. Counting it as a read makes RG003's read-back check think
        #: a write-only instrument is consumed, and report it.
        self._in_clone = 0

    # -- scope ------------------------------------------------------------

    def _bind(self, name: str) -> None:
        if self.scopes:
            self.scopes[-1].add(name)

    def _resolve_alias(self, name: str) -> tuple[str, int]:
        """Follow an Arc-clone chain out to the name a reader would recognise.

        Resolution moves strictly outward: a mapping found at scope level `i` is
        followed by searching only levels above `i`. That is what stops a
        shadowing rebind from resolving back into itself.

        Returns the resolved name and how many links were followed. The count
        matters: following even one link means the value reaches state owned
        further out, which a plain scope check cannot see once the step closure
        has shadowed the name.
        """
        hops = 0
        level = len(self.alias_scopes) - 1
        for _ in range(8):
            found = None
            while level >= 0:
                if name in self.alias_scopes[level]:
                    found = self.alias_scopes[level][name]
                    level -= 1
                    break
                level -= 1
            if found is None:
                break
            name = found
            hops += 1
        return name, hops

    def _is_outer(self, name: str) -> bool:
        if self.scopes and name in self.scopes[-1]:
            return False
        if any(name in s for s in self.scopes[:-1]):
            return True
        return name in self.ctx.statics

    def _is_static(self, name: str) -> bool:
        return name in self.ctx.statics

    # -- entry ------------------------------------------------------------

    def walk_body(self, node, region: Region) -> None:
        self.scopes.append(set())
        self.alias_scopes.append({})
        params = node.child_by_field_name("parameters")
        if params is not None:
            for ident in _descend(params, {"identifier"}):
                self._bind(self.src.text(ident))
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, region)
        self.scopes.pop()
        self.alias_scopes.pop()

    # -- dispatch ---------------------------------------------------------

    def visit(self, node, region: Region) -> None:
        fn = getattr(self, f"_v_{node.type}", None)
        if fn is not None:
            fn(node, region)
            return
        for child in node.children:
            self.visit(child, region)

    # -- declarations & reads ---------------------------------------------

    def _v_let_declaration(self, node, region: Region) -> None:
        value = node.child_by_field_name("value")
        if value is not None:
            cloning = self._clone_source(value) is not None
            self._in_clone += 1 if cloning else 0
            try:
                self.visit(value, region)
            finally:
                self._in_clone -= 1 if cloning else 0
        pattern = node.child_by_field_name("pattern")
        if pattern is not None:
            bound = (
                self.src.text(pattern)
                if pattern.type == "identifier"
                else None
            )
            if bound and value is not None:
                aliased = self._clone_source(value)
                if aliased:
                    if self.alias_scopes:
                        self.alias_scopes[-1][bound] = aliased
            for ident in _descend(pattern, {"identifier"}):
                self._bind(self.src.text(ident))
            if bound:
                self._bind(bound)

    def _clone_source(self, value) -> str | None:
        """The name behind `Arc::clone(&x)` or `x.clone()`, if that is the shape."""
        value = _unwrap(value)
        if value is None or value.type != "call_expression":
            return None
        callee = self.src.dotted(value.child_by_field_name("function")) or ""
        if not callee.endswith("clone"):
            return None
        args = value.child_by_field_name("arguments")
        if args is not None:
            for arg in args.children:
                if not arg.is_named:
                    continue
                root = self.src.root_name(arg)
                if root:
                    return root
        # `x.clone()` -- the receiver is the source.
        if value.child_by_field_name("function").type == "field_expression":
            return self.src.root_name(
                value.child_by_field_name("function").child_by_field_name("value")
            )
        return None

    def _v_identifier(self, node, region: Region) -> None:
        if self._in_clone:
            return
        self.h.reads.append(
            Read(
                name=self.src.text(node),
                loc=self.src.loc(node),
                region=region,
                step_id=self.current_step_id,
            )
        )

    def _v_assignment_expression(self, node, region: Region) -> None:
        right = node.child_by_field_name("right")
        if right is not None:
            self.visit(right, region)
        left = node.child_by_field_name("left")
        if region is Region.STEP_BODY and left is not None:
            root = self.src.root_name(left)
            # A plain local reassignment inside a `move` closure touches the
            # closure's own copy, not the caller's, so only statics reach out.
            if root and self._is_static(root):
                self._write(root, node, is_global=True)
        if left is not None:
            self.visit(left, region)

    _v_compound_assignment_expr = _v_assignment_expression

    # -- control flow -----------------------------------------------------

    def _v_if_expression(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_while_expression(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("condition"), region, node)
        for child in node.children:
            self.visit(child, region)

    def _v_match_expression(self, node, region: Region) -> None:
        self._record_branch(node.child_by_field_name("value"), region, node)
        for child in node.children:
            self.visit(child, region)

    # -- calls ------------------------------------------------------------

    def _v_call_expression(self, node, region: Region) -> None:
        if self._try_durable_operation(node, region):
            return

        func = node.child_by_field_name("function")
        dotted = self.src.dotted(func)
        if dotted:
            self.h.calls.append(
                Call(
                    dotted=self._canonical(dotted),
                    loc=self.src.loc(node),
                    region=region,
                    via=self.via,
                    display=dotted if self._canonical(dotted) != dotted else None,
                )
            )
            self._record_mutating_call(node, func, region)

        for child in node.children:
            self.visit(child, region)

    def _canonical(self, dotted: str) -> str:
        """Strip a crate prefix so `chrono::Utc::now` matches `Utc::now`."""
        parts = dotted.split("::")
        return "::".join(parts[-2:]) if len(parts) > 2 else dotted

    # -- recording --------------------------------------------------------

    def _write(self, target: str, node, is_global: bool) -> None:
        self.h.outer_writes.append(
            OuterWrite(
                target=target,
                loc=self.src.loc(node),
                region=Region.STEP_BODY,
                is_global=is_global,
                via=self.via,
                step_id=self.current_step_id,
            )
        )

    def _record_mutating_call(self, node, func, region: Region) -> None:
        """`shared.lock().unwrap().push(x)` inside a step body.

        Rust needs the interior-mutability gateway on the chain: a bare
        `vec.push(x)` on a moved-in `Vec` mutates the closure's own copy and is
        invisible outside it, so flagging that would be noise. An `Arc<Mutex<_>>`
        is different -- the write lands on state the caller can still see, and is
        lost on replay like any other.
        """
        if region is not Region.STEP_BODY or func is None:
            return
        if func.type != "field_expression":
            return
        field = func.child_by_field_name("field")
        if field is None or self.src.text(field) not in _MUTATORS:
            return
        receiver = func.child_by_field_name("value")
        chain = self.src.text(receiver) if receiver is not None else ""
        if not any(f"{gate}(" in chain for gate in _INTERIOR_MUT):
            return
        root = self.src.root_name(receiver)
        if not root:
            return
        resolved, hops = self._resolve_alias(root)
        # `hops > 0` means an Arc-clone chain was followed outward, so the write
        # lands on state the caller still holds -- even though the step closure
        # rebound the name locally and a scope check alone would say otherwise.
        if hops > 0 or self._is_outer(resolved):
            self._write(resolved, node, is_global=self._is_static(resolved))

    def _record_branch(self, condition, region: Region, node) -> None:
        if condition is None:
            return
        symbols: list[str] = []
        for sub in [
            condition,
            *_descend(condition, {"call_expression", "scoped_identifier", "field_expression"}),
        ]:
            d = self.src.dotted(sub)
            if d:
                symbols.append(self._canonical(d))
        self.h.branches.append(
            Branch(
                loc=self.src.loc(node),
                region=region,
                condition_symbols=symbols,
                via=self.via,
            )
        )

    def _try_durable_operation(self, node, region: Region) -> bool:
        func = node.child_by_field_name("function")
        if func is None or func.type != "field_expression":
            return False
        field = func.child_by_field_name("field")
        receiver = func.child_by_field_name("value")
        if field is None or self.src.text(field) not in _STEP_METHODS:
            return False
        if receiver is None or receiver.type != "identifier":
            return False
        name = self.src.text(receiver)
        if "ctx" not in name.lower() and "context" not in name.lower():
            return False

        args = node.child_by_field_name("arguments")
        positional = [c for c in args.children if c.is_named] if args is not None else []
        name_node = positional[0] if positional else None
        body = next((a for a in positional if a.type in _CLOSURE_NODES), None)

        literal, is_static, symbols = self._step_name(name_node)
        self.h.steps.append(
            Step(
                kind=self.src.text(field),
                loc=self.src.loc(node),
                name_literal=literal,
                name_is_static=is_static,
                name_symbols=symbols,
            )
        )

        for arg in positional:
            if arg is body:
                continue
            if arg is name_node and arg.type == "string_literal":
                continue
            self.visit(arg, region)

        if body is None:
            return True
        self._step_counter += 1
        saved = self.current_step_id
        self.current_step_id = self._step_counter
        try:
            self.scopes.append(set())
            self.alias_scopes.append({})
            params = body.child_by_field_name("parameters")
            if params is not None:
                for ident in _descend(params, {"identifier"}):
                    self._bind(self.src.text(ident))
            for child in body.children:
                if child is not params:
                    self.visit(child, Region.STEP_BODY)
            self.scopes.pop()
            self.alias_scopes.pop()
        finally:
            self.current_step_id = saved
        return True

    def _step_name(self, name_node) -> tuple[str | None, bool, list[str]]:
        if name_node is None:
            return None, True, []
        if name_node.type == "string_literal":
            return self.src.text(name_node).strip('"'), True, []

        # `format!("op-{}", Utc::now())` puts its arguments in a token_tree,
        # which tree-sitter does not parse -- there is no call_expression node
        # to find. Scanning the raw text is the only way to see inside a macro,
        # and step names built with `format!` are the common case.
        raw = self.src.text(name_node)
        if "!" in raw:
            from .. import catalog as _catalog

            hits = [
                key
                for key in _catalog.categories_for(Language.RUST)
                if key in raw
            ]
            if hits:
                return None, False, hits

        symbols: list[str] = []
        for sub in [
            name_node,
            *_descend(name_node, {"call_expression", "scoped_identifier", "field_expression"}),
        ]:
            d = self.src.dotted(sub)
            if d:
                symbols.append(self._canonical(d))
        return None, False, symbols


def _find_handlers(src: _Src, root):
    """Functions and closures taking a durable context parameter.

    The context parameter is the reliable signal; the `#[durable_execution]`
    attribute and the trait impl are style-specific, and this SDK ships four
    styles.
    """
    found = []
    for fn in _descend(root, {"function_item"}):
        params = fn.child_by_field_name("parameters")
        if any(_is_context_type(t) for t in _param_types(src, params)):
            name_node = fn.child_by_field_name("name")
            found.append((src.text(name_node) if name_node else "handler", fn))
    for closure in _descend(root, {"closure_expression"}):
        params = closure.child_by_field_name("parameters")
        types = []
        if params is not None:
            for p in _descend(params, {"parameter"}):
                t = p.child_by_field_name("type")
                if t is not None:
                    types.append(src.text(t))
        if any(_is_context_type(t) for t in types):
            found.append(("handler", closure))
    return found


def parse_source(source: str, path: str) -> Module:
    parser = _load_parser()
    data = source.encode("utf8")
    tree = parser.parse(data)
    src = _Src(data, path)
    ctx = _ModuleContext(src, tree.root_node)
    module = Module(path=path, language=Language.RUST)

    for name, node in _find_handlers(src, tree.root_node):
        handler = Handler(name=name, loc=src.loc(node), language=Language.RUST)
        _Walker(src, ctx, handler).walk_body(node, Region.DURABLE)
        module.handlers.append(handler)
    return module


def parse_file(path: str) -> Module:
    with open(path, encoding="utf-8") as fh:
        return parse_source(fh.read(), path)
