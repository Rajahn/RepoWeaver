"""Turn a decoded SCIP `Index` into typed reference tuples.

A SCIP document is a flat list of occurrences with no explicit nesting. To
attribute a reference occurrence (a call, a field access, a supertype
mention) to the method/constructor/type it appears inside, this module
rebuilds nesting from each *definition* occurrence's `enclosing_range`: the
smallest enclosing definition whose range contains a reference's start
position is that reference's caller scope. References with no enclosing
definition (e.g. static-initializer-level code) are dropped — see
docs/adr/0003-typed-overlay.md for the documented boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoweaver.typed.scip_proto import Document, Index, Occurrence


def is_type_symbol(symbol: str) -> bool:
    """A SCIP symbol string denotes a type (class/interface/enum) iff its
    last descriptor is a bare Type suffix (`#`) with no trailing member —
    i.e. the whole string ends in `#`. Methods/fields/constructors always end
    their last descriptor in `.` (Term/Method suffix)."""
    return symbol.rstrip().endswith("#")


@dataclass(frozen=True)
class TypedOccurrenceRef:
    file: str
    caller_symbol: str
    target_symbol: str
    line: int  # 1-based
    is_header_line: (
        bool  # shares its source line with a type definition in the same document
    )


@dataclass
class _Scope:
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    symbol: str

    def contains(self, line: int, col: int) -> bool:
        if (line, col) < (self.start_line, self.start_col):
            return False
        return (line, col) <= (self.end_line, self.end_col)

    def size(self) -> tuple[int, int]:
        """Smaller (line span, col span) sorts first — used to pick the
        innermost of several containing scopes."""
        return (self.end_line - self.start_line, self.end_col - self.start_col)


def _definition_scopes(occurrences: list[Occurrence]) -> list[_Scope]:
    scopes = []
    for occ in occurrences:
        if not occ.is_definition:
            continue
        if not (occ.enclosing_range or occ.range):
            continue
        start_line, start_col, end_line, end_col = occ.normalized_enclosing_range()
        scopes.append(_Scope(start_line, start_col, end_line, end_col, occ.symbol))
    return scopes


def _innermost_scope(scopes: list[_Scope], line: int, col: int) -> _Scope | None:
    containing = [s for s in scopes if s.contains(line, col)]
    if not containing:
        return None
    return min(containing, key=_Scope.size)


def extract_typed_references(index: Index) -> list[TypedOccurrenceRef]:
    """Flatten every document in `index` into (caller, target) reference
    tuples ready for symbol_map resolution and graph merge."""
    refs: list[TypedOccurrenceRef] = []
    for doc in index.documents:
        refs.extend(_extract_document_references(doc))
    return refs


def _extract_document_references(doc: Document) -> list[TypedOccurrenceRef]:
    scopes = _definition_scopes(doc.occurrences)
    type_def_lines = {
        occ.normalized_range()[0]
        for occ in doc.occurrences
        if occ.is_definition and is_type_symbol(occ.symbol)
    }

    out: list[TypedOccurrenceRef] = []
    for occ in doc.occurrences:
        if occ.is_definition or not occ.symbol or not occ.range:
            continue
        start_line, start_col, _end_line, _end_col = occ.normalized_range()
        scope = _innermost_scope(scopes, start_line, start_col)
        if scope is None:
            continue
        out.append(
            TypedOccurrenceRef(
                file=doc.relative_path,
                caller_symbol=scope.symbol,
                target_symbol=occ.symbol,
                line=start_line + 1,
                is_header_line=start_line in type_def_lines,
            )
        )
    return out
