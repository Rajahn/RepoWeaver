"""Repo-wide symbol resolution: turns raw parser references (CallRef/TypeRef/
ImportRef) into resolved EdgeRows, or — when more than one candidate is
equally valid — into UnresolvedReferenceRows. A resolved edge always points
at exactly one target; an ambiguous reference never silently picks one.

Priority order per receiver kind (see docs/adr/0002-m2-resolution-and-freshness.md):
  unqualified -> current owner, then supertypes/interfaces (BFS, nearest wins),
                 then static-imported members, then (last resort) global name+arity.
  this        -> current owner, then supertypes/interfaces. No static imports,
                 no global fallback (`this.x()` is never a free function).
  super       -> direct supertypes/interfaces only (never the class itself).
  variable    -> declared type (import/nested/package/wildcard/java.lang), then
                 that type's own owner-chain BFS. No global fallback: an
                 unresolvable declared type is almost always an external/JDK
                 type, and guessing a same-named method elsewhere is exactly
                 the kind of dispatch-guessing this module must not do.
  type        -> same type-name resolution as `variable`, no global fallback.
  object creation -> same type-name resolution, then owner+arity constructor
                 lookup. No global fallback (see `variable`).

Only the `unqualified` path ever reaches the global name+arity fallback,
because it is the only case with no explicit receiver at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from codecontextfabric.graph.store import EdgeRow, NodeRow, UnresolvedReferenceRow
from codecontextfabric.parser.java import CallRef, ParsedFile

_TYPE_KINDS = {"class", "interface", "enum", "annotation"}

_STRUCTURAL_CONFIDENCE = 1.00  # EXTENDS/IMPLEMENTS, unique target
_DECLARED_CONFIDENCE = 0.95  # CALLS, owner precisely known, unique target
_UNIQUE_UNTYPED_CONFIDENCE = 0.70  # CALLS, blind global name+arity fallback


def _split_member_signature(qname: str) -> tuple[str | None, str, list[str]]:
    """ "pkg.Owner#name(A,B)" -> ("pkg.Owner", "name", ["A", "B"]). Param type
    names are simple names with generics already stripped by the parser (see
    `_simple_type_name` in parser/java.py) before being joined with commas, so
    a plain comma-split is reliable — no "Map<String,Integer>"-style embedded
    commas ever reach this encoding."""
    if "#" not in qname:
        return None, "", []
    owner, rest = qname.split("#", 1)
    if "(" not in rest:
        return owner, rest, []
    name, params = rest.split("(", 1)
    params = params.rstrip(")")
    param_types = [] if params == "" else params.split(",")
    return owner, name, param_types


def _return_type_from_signature(signature: str, method_name: str) -> str:
    """Extract a simple declared return type from the parser's stable
    ``ReturnType method(Args)`` signature. Generic arguments and array suffixes
    are erased because overload scoring operates on simple Java types."""
    marker = f" {method_name}("
    raw = signature.split(marker, 1)[0].strip() if marker in signature else ""
    raw = re.sub(r"<.*>", "", raw).rstrip("[] ")
    return raw.rsplit(".", 1)[-1] if raw else "unknown"


def _ancestor_type_qnames(owner_qname: str, package: str) -> list[str]:
    """Innermost-first list of enclosing type qnames for nested-class simple-name
    resolution: "pkg.Outer.Inner" -> ["pkg.Outer.Inner", "pkg.Outer"]."""
    if package and owner_qname.startswith(package + "."):
        type_part = owner_qname[len(package) + 1 :]
    elif package and owner_qname == package:
        type_part = ""
    else:
        type_part = owner_qname
    segments = [s for s in type_part.split(".") if s]
    out = []
    for i in range(len(segments), 0, -1):
        prefix = ".".join(segments[:i])
        out.append(f"{package}.{prefix}" if package else prefix)
    return out


class SymbolTable:
    """Repo-wide indexes built once per build/incremental-batch from every
    NodeRow currently known to the graph (not just the changed files) — a
    call in an untouched file can still target a symbol in a changed one."""

    def __init__(self, all_nodes: list[NodeRow]) -> None:
        self.kind_by_id: dict[str, str] = {}
        self.qname_by_id: dict[str, str] = {}
        self.ids_by_qname: dict[str, list[str]] = {}
        self.ids_by_simple: dict[str, list[str]] = {}
        self.types_by_qname: dict[str, list[str]] = {}
        self.types_by_simple: dict[str, list[str]] = {}
        self.methods_by_key: dict[tuple[str, str, int], list[str]] = {}
        self.methods_by_name_arity: dict[tuple[str, int], list[str]] = {}
        self.ctors_by_key: dict[tuple[str, int], list[str]] = {}
        self.supertypes: dict[str, list[str]] = {}
        self.param_types_by_id: dict[str, list[str]] = {}
        self.return_type_by_id: dict[str, str] = {}

        for n in all_nodes:
            self.kind_by_id[n.id] = n.kind
            self.qname_by_id[n.id] = n.qualified_name
            self.ids_by_qname.setdefault(n.qualified_name, []).append(n.id)
            self.ids_by_simple.setdefault(n.simple_name, []).append(n.id)
            if n.kind in _TYPE_KINDS:
                self.types_by_qname.setdefault(n.qualified_name, []).append(n.id)
                self.types_by_simple.setdefault(n.simple_name, []).append(n.id)
            elif n.kind == "method":
                owner, name, param_types = _split_member_signature(n.qualified_name)
                if owner is not None:
                    arity = len(param_types)
                    self.param_types_by_id[n.id] = param_types
                    self.return_type_by_id[n.id] = _return_type_from_signature(
                        n.signature, name
                    )
                    self.methods_by_key.setdefault((owner, name, arity), []).append(
                        n.id
                    )
                    self.methods_by_name_arity.setdefault((name, arity), []).append(
                        n.id
                    )
            elif n.kind == "constructor":
                owner, _name, param_types = _split_member_signature(n.qualified_name)
                if owner is not None:
                    self.param_types_by_id[n.id] = param_types
                    self.ctors_by_key.setdefault((owner, len(param_types)), []).append(
                        n.id
                    )

    def declared_lookup(
        self, start_owners: list[str], name: str, arity: int
    ) -> list[str]:
        """BFS up the supertype graph from `start_owners`; returns the nearest
        depth's full candidate set (never guesses past ties at the same depth)."""
        visited: set[str] = set()
        frontier = [o for o in start_owners if o]
        while frontier:
            found: list[str] = []
            for owner in frontier:
                found.extend(self.methods_by_key.get((owner, name, arity), []))
            if found:
                return sorted(set(found))
            visited.update(frontier)
            nxt: list[str] = []
            for owner in frontier:
                for sup in self.supertypes.get(owner, []):
                    if sup not in visited:
                        nxt.append(sup)
            frontier = nxt
        return []


@dataclass
class FileContext:
    package: str
    import_map: dict[str, str] = field(default_factory=dict)  # simple -> qname
    wildcard_import_packages: list[str] = field(default_factory=list)
    static_member_owner_hint: dict[str, str] = field(default_factory=dict)
    static_wildcard_hints: list[str] = field(default_factory=list)


def build_file_context(pf: ParsedFile, symtab: SymbolTable) -> FileContext:
    ctx = FileContext(package=pf.package)
    for imp in pf.imports:
        if imp.is_wildcard and imp.is_static:
            ctx.static_wildcard_hints.append(imp.imported_name)
        elif imp.is_wildcard:
            ctx.wildcard_import_packages.append(imp.imported_name)
        elif imp.is_static:
            if "." in imp.imported_name:
                owner_hint, member = imp.imported_name.rsplit(".", 1)
                ctx.static_member_owner_hint[member] = owner_hint
        elif imp.imported_name in symtab.types_by_qname:
            simple = imp.imported_name.rsplit(".", 1)[-1]
            ctx.import_map[simple] = imp.imported_name
    return ctx


def _resolve_type_name(
    simple_name: str,
    owner_qname: str,
    ctx: FileContext,
    symtab: SymbolTable,
    *,
    allow_global_fallback: bool,
) -> list[str]:
    if simple_name in ctx.import_map:
        hit = symtab.types_by_qname.get(ctx.import_map[simple_name])
        if hit:
            return sorted(set(hit))
    for ancestor in _ancestor_type_qnames(owner_qname, ctx.package):
        hit = symtab.types_by_qname.get(f"{ancestor}.{simple_name}")
        if hit:
            return sorted(set(hit))
    if ctx.package:
        hit = symtab.types_by_qname.get(f"{ctx.package}.{simple_name}")
        if hit:
            return sorted(set(hit))
    wildcard_hits: list[str] = []
    for prefix in ctx.wildcard_import_packages:
        wildcard_hits.extend(symtab.types_by_qname.get(f"{prefix}.{simple_name}", []))
    if wildcard_hits:
        return sorted(set(wildcard_hits))
    java_lang_hit = symtab.types_by_qname.get(f"java.lang.{simple_name}")
    if java_lang_hit:
        return sorted(set(java_lang_hit))
    if allow_global_fallback:
        return sorted(set(symtab.types_by_simple.get(simple_name, [])))
    return []


def resolve_type_refs(
    pf: ParsedFile, ctx: FileContext, symtab: SymbolTable, src_hash: str
) -> tuple[list[EdgeRow], list[UnresolvedReferenceRow]]:
    edges: list[EdgeRow] = []
    unresolved: list[UnresolvedReferenceRow] = []
    for ref in pf.type_refs:
        from_ids = symtab.ids_by_qname.get(ref.subtype_qualified_name, [])
        if not from_ids:
            continue
        from_id = from_ids[0]

        candidates = _resolve_type_name(
            ref.supertype_simple_name,
            ref.subtype_qualified_name,
            ctx,
            symtab,
            allow_global_fallback=True,
        )
        if not candidates:
            continue  # external/JDK supertype — expected, not an error
        if len(candidates) == 1:
            edges.append(
                EdgeRow(
                    from_id=from_id,
                    to_id=candidates[0],
                    type=ref.edge_type,
                    provenance="tree_sitter_java",
                    confidence=_STRUCTURAL_CONFIDENCE,
                    file=pf.file,
                    line=ref.line,
                    source_hash=src_hash,
                )
            )
        else:
            unresolved.append(
                UnresolvedReferenceRow(
                    from_id=from_id,
                    type=ref.edge_type,
                    target_name=ref.supertype_simple_name,
                    candidates=candidates,
                    reason="ambiguous_supertype",
                    file=pf.file,
                    line=ref.line,
                )
            )
    return edges, unresolved


def resolve_type_uses(
    pf: ParsedFile, ctx: FileContext, symtab: SymbolTable, src_hash: str
) -> tuple[list[EdgeRow], list[UnresolvedReferenceRow]]:
    """Resolve field/return/param/local-var/generic/annotation/throws/cast/
    instanceof/class-literal/object-creation type uses into REFERENCES edges.
    Only a unique resolution (via explicit import, nested/ancestor type, same
    package, or wildcard import — never a blind global-name fallback) is
    written as an edge; multiple candidates go to unresolved_reference only,
    never the edge table."""
    edges: list[EdgeRow] = []
    unresolved: list[UnresolvedReferenceRow] = []
    for ref in pf.type_uses:
        from_ids = symtab.ids_by_qname.get(ref.caller_qualified_name, [])
        if not from_ids:
            continue
        from_id = from_ids[0]

        candidates = _resolve_type_name(
            ref.type_simple_name,
            ref.owner_qname,
            ctx,
            symtab,
            allow_global_fallback=False,
        )
        if not candidates:
            continue  # external/JDK type — expected, not an error
        if len(candidates) == 1:
            to_id = candidates[0]
            if to_id == from_id:
                continue  # a type referencing itself is not a useful edge
            edges.append(
                EdgeRow(
                    from_id=from_id,
                    to_id=to_id,
                    type="REFERENCES",
                    provenance="tree_sitter_java",
                    confidence=_DECLARED_CONFIDENCE,
                    file=pf.file,
                    line=ref.line,
                    source_hash=src_hash,
                )
            )
        else:
            unresolved.append(
                UnresolvedReferenceRow(
                    from_id=from_id,
                    type="REFERENCES",
                    target_name=ref.type_simple_name,
                    candidates=candidates,
                    reason="ambiguous_type_use",
                    file=pf.file,
                    line=ref.line,
                )
            )
    return edges, unresolved


def _ids_to_qnames(symtab: SymbolTable, ids: list[str]) -> list[str]:
    """`_resolve_type_name` returns type *node ids* (correct for EXTENDS/IMPLEMENTS
    edge targets); `methods_by_key`/`ctors_by_key`/`declared_lookup` are keyed by
    owner *qname* strings. This bridges the two — never mix the two id spaces."""
    return sorted({symtab.qname_by_id[nid] for nid in ids if nid in symtab.qname_by_id})


# ----------------------------------------------------------------------------
# Overload scoring — disambiguates same-owner-same-arity candidates by
# argument-type compatibility. Never a hard guess: a candidate is dropped
# outright only when a hint makes it *definitely* impossible under the Java
# type system (a closed rule — null-to-primitive, or two distinct, fully-known
# primitives with no widening path). Everything else is either positive
# evidence (exact/boxing/widening/subtype) or neutral (no evidence either
# way) — resolution only happens when there's a single top scorer with a
# clear margin over the runner-up, or when scoring eliminates every candidate
# but one. See docs/adr/0002-m2-resolution-and-freshness.md.
# ----------------------------------------------------------------------------

_PRIMITIVES = {"int", "long", "short", "byte", "char", "boolean", "float", "double"}
_BOXING = {
    "int": "Integer",
    "long": "Long",
    "short": "Short",
    "byte": "Byte",
    "char": "Character",
    "boolean": "Boolean",
    "float": "Float",
    "double": "Double",
}
_UNBOXING = {v: k for k, v in _BOXING.items()}
_WIDENING = {  # JLS 5.1.2 numeric widening, restricted to the primitives we infer
    "byte": {"short", "int", "long", "float", "double"},
    "short": {"int", "long", "float", "double"},
    "char": {"int", "long", "float", "double"},
    "int": {"long", "float", "double"},
    "long": {"float", "double"},
    "float": {"double"},
}

_SCORE_EXACT = 3
_SCORE_WIDEN = 2
_SCORE_BOX = 2
_SCORE_SUBTYPE = 1
_SCORE_NEUTRAL = 0
_CLEAR_MARGIN = 2  # winner must beat the runner-up by at least this many points


def _is_supertype_simple(sub_simple: str, sup_simple: str, symtab: SymbolTable) -> bool:
    """Best-effort subtype check using only the repo-local EXTENDS/IMPLEMENTS
    graph (JDK/external supertypes are never indexed, so absence of a path
    here is not proof of non-subtyping — callers must treat a `False` result
    as "unknown", not "incompatible")."""
    sub_ids = symtab.types_by_simple.get(sub_simple, [])
    sup_qnames = {
        symtab.qname_by_id[i]
        for i in symtab.types_by_simple.get(sup_simple, [])
        if i in symtab.qname_by_id
    }
    if not sub_ids or not sup_qnames:
        return False
    visited: set[str] = set()
    frontier = [symtab.qname_by_id[i] for i in sub_ids if i in symtab.qname_by_id]
    while frontier:
        nxt: list[str] = []
        for q in frontier:
            if q in sup_qnames:
                return True
            if q in visited:
                continue
            visited.add(q)
            nxt.extend(symtab.supertypes.get(q, []))
        frontier = nxt
    return False


def _param_compat(hint: str, param_type: str, symtab: SymbolTable) -> int | None:
    """Score one (argument-hint, declared-parameter-type) pair. `None` means
    the pair is definitely impossible under the Java type system — the
    candidate is eliminated, not merely scored low. Everything else is a
    non-negative score; `unknown`/most cross-reference-type mismatches are
    neutral (0) because our type/subtype knowledge is incomplete, not because
    we've proven compatibility."""
    if hint == "unknown":
        return _SCORE_NEUTRAL
    if hint == param_type:
        return _SCORE_EXACT
    if hint == "null":
        # null can never satisfy a primitive parameter (a real JLS fact) but
        # must never be used to pick a winner among reference-type params —
        # that would be exactly the guessing the resolver must not do.
        return None if param_type in _PRIMITIVES else _SCORE_NEUTRAL
    if hint in _PRIMITIVES and param_type in _PRIMITIVES:
        return _SCORE_WIDEN if param_type in _WIDENING.get(hint, ()) else None
    if hint in _PRIMITIVES:  # param_type is a reference type
        boxed = _BOXING[hint]
        if boxed == param_type:
            return _SCORE_BOX
        if param_type == "Object" or _is_supertype_simple(boxed, param_type, symtab):
            return _SCORE_SUBTYPE
        return None  # a primitive can never satisfy an unrelated reference type
    if param_type in _PRIMITIVES:  # hint is a reference type, param is primitive
        return _SCORE_BOX if _UNBOXING.get(hint) == param_type else None
    # Both concrete reference types, no exact match: our subtype graph only
    # covers repo-local types, so an unmatched pair is unknown, not ruled out.
    if param_type == "Object" or _is_supertype_simple(hint, param_type, symtab):
        return _SCORE_SUBTYPE
    return _SCORE_NEUTRAL


def _resolve_nested_call_hint(
    hint: str,
    owner_qname: str,
    ctx: FileContext,
    symtab: SymbolTable,
) -> str:
    if not hint.startswith("@call:"):
        return hint
    try:
        receiver, method, raw_arity = hint[len("@call:") :].rsplit(":", 2)
        arity = int(raw_arity)
    except (ValueError, TypeError):
        return "unknown"

    if receiver == "this":
        owner_qnames = [owner_qname]
    else:
        type_ids = _resolve_type_name(
            receiver,
            owner_qname,
            ctx,
            symtab,
            allow_global_fallback=False,
        )
        owner_qnames = _ids_to_qnames(symtab, type_ids)
    if not owner_qnames:
        return "unknown"

    candidates = symtab.declared_lookup(owner_qnames, method, arity)
    return_types = {
        symtab.return_type_by_id.get(candidate, "unknown") for candidate in candidates
    }
    return_types.discard("unknown")
    return_types.discard("void")
    return next(iter(return_types)) if len(return_types) == 1 else "unknown"


def _normalize_argument_hints(
    hints: list[str], owner_qname: str, ctx: FileContext, symtab: SymbolTable
) -> list[str]:
    return [_resolve_nested_call_hint(hint, owner_qname, ctx, symtab) for hint in hints]


def _score_overloads(
    candidate_ids: list[str], arg_hints: list[str], symtab: SymbolTable
) -> list[tuple[str, int]]:
    """Score each candidate by summed per-argument compatibility; a candidate
    with any definitely-impossible argument is dropped entirely. Candidates
    whose recorded param-type list doesn't match `arg_hints` in length (e.g.
    missing data) are kept at a neutral score rather than eliminated."""
    scored: list[tuple[str, int]] = []
    for cid in candidate_ids:
        param_types = symtab.param_types_by_id.get(cid)
        if param_types is None or len(param_types) != len(arg_hints):
            scored.append((cid, _SCORE_NEUTRAL))
            continue
        total = 0
        eliminated = False
        for hint, param_type in zip(arg_hints, param_types):
            pair_score = _param_compat(hint, param_type, symtab)
            if pair_score is None:
                eliminated = True
                break
            total += pair_score
        if not eliminated:
            scored.append((cid, total))
    return scored


def _narrow_by_overload_score(
    candidates: list[str], arg_hints: list[str], symtab: SymbolTable
) -> list[str]:
    """Attempt to narrow an ambiguous owner+name+arity candidate set using
    argument-type hints. Returns a single-element list when resolution is
    justified (either only one candidate survives elimination, or there's a
    unique top scorer with a clear margin); otherwise returns the surviving
    candidate set unchanged in size-order — never fewer than the original
    unless scoring positively eliminated some as impossible."""
    if len(candidates) <= 1:
        return candidates
    scored = _score_overloads(candidates, arg_hints, symtab)
    if not scored:
        return candidates  # scoring inconclusive (shouldn't normally happen)
    if len(scored) == 1:
        return [scored[0][0]]  # elimination-driven: not a guess, a deduction
    ranked = sorted(scored, key=lambda pair: -pair[1])
    best_score = ranked[0][1]
    winners = [cid for cid, s in ranked if s == best_score]
    if len(winners) == 1 and arg_hints and best_score == _SCORE_EXACT * len(arg_hints):
        # Every argument was an exact type match — decisive on its own (this
        # mirrors real Java overload resolution, which always prefers an
        # exact-match phase over boxing/widening candidates), independent of
        # the margin check below.
        return [winners[0]]
    runner_up_score = ranked[len(winners)][1] if len(winners) < len(ranked) else None
    if (
        len(winners) == 1
        and best_score > 0
        and (runner_up_score is None or best_score - runner_up_score >= _CLEAR_MARGIN)
    ):
        return [winners[0]]
    return [cid for cid, _ in ranked]


def _owner_candidates_for_call(
    call: CallRef, ctx: FileContext, symtab: SymbolTable
) -> tuple[list[str] | None, bool]:
    """Returns (declared-method candidates, was_ambiguous_type) for receiver
    kinds that carry explicit type information (variable/type). `None`
    candidates (as opposed to `[]`) means "type itself unresolved" — the
    caller must skip, never fall back to a blind global guess."""
    owner_candidates = _resolve_type_name(
        call.receiver_hint or "",
        call.owner_qname,
        ctx,
        symtab,
        allow_global_fallback=False,
    )
    if not owner_candidates:
        return None, False
    owner_qnames = _ids_to_qnames(symtab, owner_candidates)
    found = symtab.declared_lookup(
        owner_qnames, call.method_simple_name, call.argument_count
    )
    return found, len(owner_qnames) > 1


def resolve_calls(
    pf: ParsedFile, ctx: FileContext, symtab: SymbolTable, src_hash: str
) -> tuple[list[EdgeRow], list[UnresolvedReferenceRow]]:
    edges: list[EdgeRow] = []
    unresolved: list[UnresolvedReferenceRow] = []

    for call in pf.calls:
        from_ids = symtab.ids_by_qname.get(call.caller_qualified_name, [])
        if not from_ids:
            continue
        from_id = from_ids[0]
        name, arity = call.method_simple_name, call.argument_count

        candidates: list[str] = []
        confidence = _DECLARED_CONFIDENCE
        reason = "ambiguous_owner_chain"

        if call.method_simple_name == "<init>":
            owner_candidates = _resolve_type_name(
                call.receiver_hint or "",
                call.owner_qname,
                ctx,
                symtab,
                allow_global_fallback=False,
            )
            if not owner_candidates:
                continue  # external/JDK type — expected, not a guess target
            owner_qnames = _ids_to_qnames(symtab, owner_candidates)
            pool: list[str] = []
            for owner in owner_qnames:
                pool.extend(symtab.ctors_by_key.get((owner, arity), []))
            candidates = sorted(set(pool))
            reason = (
                "ambiguous_type" if len(owner_qnames) > 1 else "ambiguous_owner_chain"
            )

        elif call.receiver_kind == "this":
            candidates = symtab.declared_lookup([call.owner_qname], name, arity)

        elif call.receiver_kind == "super":
            candidates = symtab.declared_lookup(
                symtab.supertypes.get(call.owner_qname, []), name, arity
            )

        elif call.receiver_kind in ("variable", "type"):
            found, type_ambiguous = _owner_candidates_for_call(call, ctx, symtab)
            if found is None:
                continue  # receiver type unresolved (external/JDK) — no guessing
            candidates = found
            reason = "ambiguous_type" if type_ambiguous else "ambiguous_owner_chain"

        elif call.receiver_kind == "unqualified":
            candidates = symtab.declared_lookup([call.owner_qname], name, arity)
            if not candidates:
                static_owner_hint = ctx.static_member_owner_hint.get(name)
                if static_owner_hint is not None:
                    owners = _ids_to_qnames(
                        symtab,
                        _resolve_type_name(
                            static_owner_hint.rsplit(".", 1)[-1],
                            call.owner_qname,
                            ctx,
                            symtab,
                            allow_global_fallback=False,
                        ),
                    )
                    pool = []
                    for owner in owners:
                        pool.extend(symtab.methods_by_key.get((owner, name, arity), []))
                    candidates = sorted(set(pool))
                    confidence = _DECLARED_CONFIDENCE
                if not candidates and ctx.static_wildcard_hints:
                    pool = []
                    for hint in ctx.static_wildcard_hints:
                        owners = _ids_to_qnames(
                            symtab,
                            _resolve_type_name(
                                hint.rsplit(".", 1)[-1],
                                call.owner_qname,
                                ctx,
                                symtab,
                                allow_global_fallback=False,
                            ),
                        )
                        for owner in owners:
                            pool.extend(
                                symtab.methods_by_key.get((owner, name, arity), [])
                            )
                    candidates = sorted(set(pool))
                    confidence = _DECLARED_CONFIDENCE
                if not candidates:
                    candidates = sorted(
                        set(symtab.methods_by_name_arity.get((name, arity), []))
                    )
                    confidence = _UNIQUE_UNTYPED_CONFIDENCE
                    reason = "ambiguous_global_fallback"

        else:  # "chain" — multi-hop receiver, never guessed
            continue

        if len(candidates) > 1:
            argument_hints = _normalize_argument_hints(
                call.argument_type_hints, call.owner_qname, ctx, symtab
            )
            candidates = _narrow_by_overload_score(candidates, argument_hints, symtab)

        if not candidates:
            continue  # external/JDK/unindexed target — expected, not an error
        if len(candidates) == 1:
            edges.append(
                EdgeRow(
                    from_id=from_id,
                    to_id=candidates[0],
                    type="CALLS",
                    provenance="tree_sitter_java",
                    confidence=confidence,
                    file=pf.file,
                    line=call.line,
                    source_hash=src_hash,
                )
            )
        else:
            unresolved.append(
                UnresolvedReferenceRow(
                    from_id=from_id,
                    type="CALLS",
                    target_name=name,
                    candidates=candidates,
                    reason=reason,
                    file=pf.file,
                    line=call.line,
                )
            )
    return edges, unresolved


def resolve_imports(
    pf: ParsedFile, symtab: SymbolTable, src_hash: str
) -> list[EdgeRow]:
    """Unchanged from M1: only explicit, non-wildcard, non-static imports get an
    IMPORTS edge. Wildcard/static imports are consumed for call/type resolution
    above but never materialize as an IMPORTS edge (they don't name one type)."""
    out: list[EdgeRow] = []
    for imp in pf.imports:
        imported_type = imp.imported_name
        if imp.is_static:
            # `import static pkg.Type.MEMBER` references the owning type even
            # though the imported symbol is a field/method. For wildcard
            # imports the parser already records `pkg.Type` as imported_name;
            # for explicit members strip the final segment.
            if not imp.is_wildcard and "." in imported_type:
                imported_type = imported_type.rsplit(".", 1)[0]
        elif imp.is_wildcard:
            continue

        to_candidates = symtab.ids_by_qname.get(imported_type, [])
        if not to_candidates:
            continue
        for top_level_qname in pf.top_level_types:
            from_candidates = symtab.ids_by_qname.get(top_level_qname, [])
            if not from_candidates:
                continue
            out.append(
                EdgeRow(
                    from_id=from_candidates[0],
                    to_id=to_candidates[0],
                    type="IMPORTS",
                    provenance="tree_sitter_java",
                    confidence=_STRUCTURAL_CONFIDENCE,
                    file=pf.file,
                    line=imp.line,
                    source_hash=src_hash,
                )
            )
    return out


def build_supertypes(
    type_edges_by_file: dict[str, list[EdgeRow]], symtab: SymbolTable
) -> None:
    """Populate `symtab.supertypes` from already-resolved EXTENDS/IMPLEMENTS
    edges — must run after `resolve_type_refs` for every file and before any
    `resolve_calls` call, since inheritance-aware lookup depends on it."""
    for edges in type_edges_by_file.values():
        for e in edges:
            if e.type not in ("EXTENDS", "IMPLEMENTS"):
                continue
            from_qname = symtab.qname_by_id.get(e.from_id)
            to_qname = symtab.qname_by_id.get(e.to_id)
            if not from_qname or not to_qname:
                continue
            existing = symtab.supertypes.setdefault(from_qname, [])
            if to_qname not in existing:
                existing.append(to_qname)


__all__ = [
    "FileContext",
    "SymbolTable",
    "build_file_context",
    "build_supertypes",
    "resolve_calls",
    "resolve_imports",
    "resolve_type_refs",
    "resolve_type_uses",
]
