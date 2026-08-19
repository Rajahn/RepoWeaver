"""Map SCIP symbol strings (maven/java scheme) onto Code Context Fabric `qualified_name`s.

SCIP symbol grammar (see https://github.com/sourcegraph/scip, `Symbol` /
`Descriptor` messages)::

    <scheme> <manager> <package-name> <package-version> <descriptor>+

Each descriptor carries its own suffix delimiter: ``name/`` (Namespace),
``name#`` (Type), ``name.`` (Term — a field), ``name(disambiguator).``
(Method — the disambiguator is the JVM parameter descriptor, e.g. ``(I)`` or
``(Ljava/lang/String;)``). Code Context Fabric only ever needs Namespace/Type/Term/
Method, so `Parameter` (``(name)``), `TypeParameter` (``[name]``) and `Meta`
(``name:``) descriptors are treated as unmappable — recorded, never guessed
(see docs/adr/0003-typed-overlay.md).

Overload alignment: when an owner+name pair has more than one candidate
method in the graph, the Method descriptor's JVM disambiguator is decoded to
simple erased type names (mirroring `_simple_type_name` in parser/java.py)
and matched against each candidate's parameter types. A unique match maps;
zero or ambiguous matches are recorded as skipped, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codecontextfabric.graph.store import GraphStore

_JVM_PRIMITIVES = {
    "B": "byte",
    "C": "char",
    "D": "double",
    "F": "float",
    "I": "int",
    "J": "long",
    "S": "short",
    "Z": "boolean",
    "V": "void",
}


@dataclass(frozen=True)
class Descriptor:
    name: str
    suffix: str  # "namespace" | "type" | "term" | "method"
    disambiguator: str = ""  # raw JVM param descriptor, "method" suffix only


@dataclass(frozen=True)
class ParsedSymbol:
    scheme: str
    manager: str
    package_name: str
    package_version: str
    descriptors: tuple[Descriptor, ...]

    @property
    def is_local(self) -> bool:
        return self.scheme == "local"


class SkipReason:
    MALFORMED_SYMBOL = "malformed_symbol"
    LOCAL_SYMBOL = "local_symbol"
    UNSUPPORTED_DESCRIPTOR = "unsupported_descriptor"
    OWNER_NOT_FOUND = "owner_not_found"
    MEMBER_NOT_FOUND = "member_not_found"
    AMBIGUOUS_OVERLOAD = "ambiguous_overload_unaligned"


@dataclass(frozen=True)
class MapResult:
    node: dict | None
    skip_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.node is not None


def _split_top_level_fields(symbol: str) -> tuple[list[str], str] | None:
    """Split the 4 leading space-separated fields (scheme, manager,
    package-name, package-version) from the trailing descriptor string.
    Fields containing spaces/backticks are backtick-quoted per the SCIP
    spec; none of our fixtures need that, so a plain split covers the
    real-world common case and anything else is reported unmapped rather
    than mis-parsed."""
    parts = symbol.strip().split(" ", 4)
    if len(parts) < 5:
        if len(parts) == 2 and parts[0] == "local":
            return parts, ""
        return None
    return parts[:4], parts[4]


def parse_symbol(symbol: str) -> ParsedSymbol | None:
    """Parse a SCIP symbol string into scheme/package/descriptors. Returns
    None if the string doesn't match the grammar this module understands."""
    split = _split_top_level_fields(symbol)
    if split is None:
        return None
    fields, descriptor_str = split
    if fields[0] == "local":
        return ParsedSymbol("local", "", "", "", ())
    scheme, manager, package_name, package_version = fields
    descriptors = _parse_descriptors(descriptor_str)
    if descriptors is None:
        return None
    return ParsedSymbol(
        scheme, manager, package_name, package_version, tuple(descriptors)
    )


def _parse_descriptors(text: str) -> list[Descriptor] | None:
    out: list[Descriptor] = []
    pos = 0
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == "[":
            end = text.find("]", pos + 1)
            if end == -1:
                return None
            out.append(Descriptor(name=text[pos + 1 : end], suffix="type_parameter"))
            pos = end + 1
            continue
        # Read a bare name up to the next suffix-introducing character.
        m = re.match(r"[^/#.(]*", text[pos:])
        name = m.group(0) if m else ""
        pos += len(name)
        if pos >= n:
            return None
        ch = text[pos]
        if ch == "/":
            out.append(Descriptor(name=name, suffix="namespace"))
            pos += 1
        elif ch == "#":
            out.append(Descriptor(name=name, suffix="type"))
            pos += 1
        elif ch == ".":
            out.append(Descriptor(name=name, suffix="term"))
            pos += 1
        elif ch == "(":
            end = text.find(")", pos + 1)
            if end == -1:
                return None
            disambiguator = text[pos + 1 : end]
            after = end + 1
            if after < n and text[after] == ".":
                out.append(
                    Descriptor(name=name, suffix="method", disambiguator=disambiguator)
                )
                pos = after + 1
            else:
                out.append(Descriptor(name=disambiguator, suffix="parameter"))
                pos = after
        else:
            return None
    return out


def decode_jvm_param_types(disambiguator: str) -> list[str] | None:
    """Decode a JVM method-descriptor parameter list (e.g. ``ILjava/lang/String;``)
    into Code Context Fabric's simple, array/generics-erased type names (``int``,
    ``String``) — mirrors `_simple_type_name` in parser/java.py. Returns None
    if the disambiguator isn't a recognizable JVM parameter descriptor
    (Code Context Fabric never guesses in that case)."""
    if disambiguator == "":
        return []
    out: list[str] = []
    pos = 0
    n = len(disambiguator)
    while pos < n:
        ch = disambiguator[pos]
        array_depth = 0
        while ch == "[":
            array_depth += 1
            pos += 1
            if pos >= n:
                return None
            ch = disambiguator[pos]
        if ch == "L":
            end = disambiguator.find(";", pos)
            if end == -1:
                return None
            binary_name = disambiguator[pos + 1 : end]
            simple = re.split(r"[/$]", binary_name)[-1]
            out.append(simple)
            pos = end + 1
        elif ch in _JVM_PRIMITIVES:
            out.append(_JVM_PRIMITIVES[ch])
            pos += 1
        else:
            return None
    return out


def _owner_path(descriptors: tuple[Descriptor, ...]) -> tuple[str, int]:
    """Consume leading namespace/type descriptors into a dotted owner path.
    Returns (owner_qname, index of the first non-namespace/type descriptor)."""
    package_parts = [d.name for d in descriptors if d.suffix == "namespace"]
    idx = 0
    while idx < len(descriptors) and descriptors[idx].suffix == "namespace":
        idx += 1
    type_parts = []
    while idx < len(descriptors) and descriptors[idx].suffix == "type":
        type_parts.append(descriptors[idx].name)
        idx += 1
    package = ".".join(package_parts)
    type_path = ".".join(type_parts)
    if package and type_path:
        owner = f"{package}.{type_path}"
    else:
        owner = package or type_path
    return owner, idx


class SymbolMapper:
    """Resolves SCIP symbol strings against a repo's already-built graph.

    Stateless with respect to the SCIP index — every call re-reads
    `GraphStore`, which is cheap (indexed sqlite lookups) and keeps this
    class safe to reuse across an entire overlay run without caching
    staleness concerns."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self.skip_counts: dict[str, int] = {}

    def _skip(self, reason: str) -> MapResult:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1
        return MapResult(node=None, skip_reason=reason)

    def resolve(self, symbol: str) -> MapResult:
        parsed = parse_symbol(symbol)
        if parsed is None:
            return self._skip(SkipReason.MALFORMED_SYMBOL)
        if parsed.is_local:
            return self._skip(SkipReason.LOCAL_SYMBOL)
        if any(d.suffix in ("parameter", "type_parameter") for d in parsed.descriptors):
            return self._skip(SkipReason.UNSUPPORTED_DESCRIPTOR)

        owner, idx = _owner_path(parsed.descriptors)
        if not owner:
            return self._skip(SkipReason.MALFORMED_SYMBOL)

        if idx == len(parsed.descriptors):
            # The symbol denotes the type itself (last descriptor was Type).
            candidates = self._store.find_by_qualified_name(owner)
            if len(candidates) != 1:
                return self._skip(SkipReason.OWNER_NOT_FOUND)
            return MapResult(node=candidates[0])

        member = parsed.descriptors[idx]
        if idx != len(parsed.descriptors) - 1:
            return self._skip(SkipReason.UNSUPPORTED_DESCRIPTOR)

        if member.suffix == "term":
            qname = f"{owner}#{member.name}"
            candidates = self._store.find_by_qualified_name(qname)
            if len(candidates) != 1:
                return self._skip(SkipReason.MEMBER_NOT_FOUND)
            return MapResult(node=candidates[0])

        if member.suffix != "method":
            return self._skip(SkipReason.UNSUPPORTED_DESCRIPTOR)

        return self._resolve_method(owner, member)

    def _resolve_method(self, owner: str, member: Descriptor) -> MapResult:
        # Code Context Fabric stores a constructor's `simple_name` as the owning
        # class's simple name (e.g. "Circle"), not the JVM "<init>" the SCIP
        # descriptor uses — translate before the simple_name lookup.
        lookup_name = (
            owner.rsplit(".", 1)[-1] if member.name == "<init>" else member.name
        )
        expected_kind = "constructor" if member.name == "<init>" else "method"
        siblings = [
            n
            for n in self._store.find_by_simple_name(lookup_name)
            if n["kind"] == expected_kind
        ]
        candidates = [n for n in siblings if _owner_of(n["qualified_name"]) == owner]
        if not candidates:
            return self._skip(SkipReason.MEMBER_NOT_FOUND)
        if len(candidates) == 1:
            return MapResult(node=candidates[0])

        param_types = decode_jvm_param_types(member.disambiguator)
        if param_types is None:
            return self._skip(SkipReason.AMBIGUOUS_OVERLOAD)
        matches = [
            n for n in candidates if _param_types_of(n["qualified_name"]) == param_types
        ]
        if len(matches) != 1:
            return self._skip(SkipReason.AMBIGUOUS_OVERLOAD)
        return MapResult(node=matches[0])


def _owner_of(qualified_name: str) -> str:
    return qualified_name.split("#", 1)[0] if "#" in qualified_name else qualified_name


def _param_types_of(qualified_name: str) -> list[str]:
    if "#" not in qualified_name:
        return []
    _owner, rest = qualified_name.split("#", 1)
    if "(" not in rest:
        return []
    _name, params = rest.split("(", 1)
    params = params.rstrip(")")
    return [] if params == "" else params.split(",")
