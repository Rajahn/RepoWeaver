"""Java parser — tree-sitter-based symbol/reference extractor for Java source.

Produces `NodeRecord`s (package/class/interface/enum/method/constructor/field
declarations) and *raw*, unresolved references (`CallRef`/`TypeRef`/`ImportRef`).
Resolving those references into `EdgeRecord`s with provenance/confidence requires
a repo-wide symbol table and lives in `repoweaver.indexer` — a single file's AST
never has enough information to disambiguate a call target on its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

PARSER_VERSION = f"tree-sitter-java {getattr(tree_sitter_java, '__version__', '0.23')}"

_LANGUAGE = Language(tree_sitter_java.language())

_TYPE_DECL_TYPES = {"class_declaration", "interface_declaration", "enum_declaration"}
_MEMBER_DECL_TYPES = {
    "method_declaration",
    "constructor_declaration",
    "field_declaration",
}

_KIND_BY_KEYWORD = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


@dataclass
class NodeRecord:
    """Represents a symbol node extracted from Java source."""

    kind: str  # class | interface | enum | enum_constant | method | constructor | field
    language: str = "java"
    repo: str = ""
    file: str = ""
    span_start: int = 0
    span_end: int = 0
    qualified_name: str = ""
    simple_name: str = ""
    signature: str = ""
    commit_hash: str = ""
    annotations: list[str] = field(default_factory=list)  # simple names, e.g. "GetMapping"


@dataclass
class ImportRef:
    imported_name: str  # fully-qualified if not wildcard
    is_wildcard: bool
    is_static: bool
    line: int


@dataclass
class TypeRef:
    """A raw `extends`/`implements` reference, not yet resolved."""

    subtype_qualified_name: str
    supertype_simple_name: str
    edge_type: str  # EXTENDS | IMPLEMENTS
    line: int


@dataclass
class CallRef:
    """A raw method-invocation reference, not yet resolved."""

    caller_qualified_name: str
    owner_qname: str  # qualified name of the type enclosing this call site
    method_simple_name: str
    receiver_hint: (
        str | None
    )  # simple type name if resolvable from local decls, else None
    receiver_kind: str  # unqualified | this | super | type | variable | chain
    argument_count: int
    line: int
    # Per-argument best-effort static type (simple name), "null" for the null
    # literal, or "unknown" when it cannot be determined from single-file
    # information alone. Never a guess: "unknown"/"null" are neutral signals
    # for the resolver's overload scoring, not positive evidence for any
    # candidate — see resolver._param_compat.
    argument_type_hints: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    file: str
    package: str = ""
    imports: list[ImportRef] = field(default_factory=list)
    nodes: list[NodeRecord] = field(default_factory=list)
    top_level_types: list[str] = field(default_factory=list)  # qualified names
    type_refs: list[TypeRef] = field(default_factory=list)
    calls: list[CallRef] = field(default_factory=list)
    source: str = ""


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _simple_type_name(raw: str) -> str:
    """Strip generics/arrays/qualifiers: "java.util.List<Foo>" -> "List"."""
    raw = raw.strip()
    raw = re.sub(r"<.*>", "", raw)
    raw = raw.rstrip("[] ")
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw.strip()


class JavaParser:
    """Parses Java source files and emits NodeRecord / raw-reference streams."""

    def __init__(self, repo_root: str | Path = ".") -> None:
        self.repo_root = Path(repo_root)
        self._parser = Parser(_LANGUAGE)

    def parse_file(self, path: str | Path) -> ParsedFile:
        abs_path = Path(path)
        try:
            rel = abs_path.resolve().relative_to(self.repo_root.resolve())
            rel_str = str(rel).replace("\\", "/")
        except ValueError:
            rel_str = str(abs_path)

        source_bytes = abs_path.read_bytes()
        source_text = source_bytes.decode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        pf = ParsedFile(file=rel_str, source=source_text)

        root = tree.root_node
        for child in root.children:
            if child.type == "package_declaration":
                name_node = child.child_by_field_name("name") or _first_of_type(
                    child, {"scoped_identifier", "identifier"}
                )
                if name_node is not None:
                    pf.package = _text(name_node, source_bytes)
            elif child.type == "import_declaration":
                pf.imports.append(_parse_import(child, source_bytes))

        for child in root.children:
            if child.type in _TYPE_DECL_TYPES:
                self._walk_type(child, source_bytes, pf, pf.package, enclosing=None)

        return pf

    # ------------------------------------------------------------------

    def _walk_type(
        self,
        node: Node,
        source: bytes,
        pf: ParsedFile,
        package: str,
        enclosing: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        simple_name = _text(name_node, source)
        qualified_name = (
            f"{enclosing}.{simple_name}"
            if enclosing
            else (f"{package}.{simple_name}" if package else simple_name)
        )
        kind = _KIND_BY_KEYWORD[node.type]

        span_start = node.start_point[0] + 1
        span_end = node.end_point[0] + 1
        signature = _type_signature(node, source, kind, simple_name)

        pf.nodes.append(
            NodeRecord(
                kind=kind,
                file=pf.file,
                span_start=span_start,
                span_end=span_end,
                qualified_name=qualified_name,
                simple_name=simple_name,
                signature=signature,
                annotations=_extract_annotations(node, source),
            )
        )
        if enclosing is None:
            pf.top_level_types.append(qualified_name)

        for super_node in node.children:
            if super_node.type == "superclass":
                type_node = _first_named_type(super_node)
                if type_node is not None:
                    pf.type_refs.append(
                        TypeRef(
                            subtype_qualified_name=qualified_name,
                            supertype_simple_name=_simple_type_name(
                                _text(type_node, source)
                            ),
                            edge_type="EXTENDS",
                            line=super_node.start_point[0] + 1,
                        )
                    )
            elif super_node.type in ("super_interfaces", "extends_interfaces"):
                for iface in _iter_type_list(super_node):
                    pf.type_refs.append(
                        TypeRef(
                            subtype_qualified_name=qualified_name,
                            supertype_simple_name=_simple_type_name(
                                _text(iface, source)
                            ),
                            edge_type="IMPLEMENTS"
                            if super_node.type == "super_interfaces"
                            else "EXTENDS",
                            line=super_node.start_point[0] + 1,
                        )
                    )

        body = node.child_by_field_name("body")
        if body is None:
            return

        local_var_types = _collect_field_types(body, source)

        for member in body.children:
            if member.type in _TYPE_DECL_TYPES:
                self._walk_type(member, source, pf, package, enclosing=qualified_name)
            elif member.type == "method_declaration":
                self._emit_method(member, source, pf, qualified_name, local_var_types)
            elif member.type == "constructor_declaration":
                self._emit_constructor(
                    member, source, pf, qualified_name, simple_name, local_var_types
                )
            elif member.type == "field_declaration":
                self._emit_fields(member, source, pf, qualified_name)
            elif member.type == "enum_body_declarations":
                for m in member.children:
                    if m.type == "method_declaration":
                        self._emit_method(m, source, pf, qualified_name, local_var_types)
                    elif m.type == "constructor_declaration":
                        self._emit_constructor(
                            m, source, pf, qualified_name, simple_name, local_var_types
                        )
            elif member.type == "enum_constant":
                name_n = member.child_by_field_name("name")
                if name_n is not None:
                    const_simple = _text(name_n, source)
                    pf.nodes.append(
                        NodeRecord(
                            kind="enum_constant",
                            file=pf.file,
                            span_start=member.start_point[0] + 1,
                            span_end=member.end_point[0] + 1,
                            qualified_name=f"{qualified_name}.{const_simple}",
                            simple_name=const_simple,
                            signature=f"{simple_name}.{const_simple}",
                        )
                    )

    def _emit_method(
        self,
        node: Node,
        source: bytes,
        pf: ParsedFile,
        owner_qname: str,
        scope_types: dict[str, str],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        simple_name = _text(name_node, source)
        params_node = node.child_by_field_name("parameters")
        param_types = _param_types(params_node, source) if params_node else []
        qualified_name = f"{owner_qname}#{simple_name}({','.join(param_types)})"
        return_node = node.child_by_field_name("type")
        return_type = _text(return_node, source) if return_node else "void"

        pf.nodes.append(
            NodeRecord(
                kind="method",
                file=pf.file,
                span_start=node.start_point[0] + 1,
                span_end=node.end_point[0] + 1,
                qualified_name=qualified_name,
                simple_name=simple_name,
                signature=f"{return_type} {simple_name}({', '.join(param_types)})",
                annotations=_extract_annotations(node, source),
            )
        )

        method_scope = dict(scope_types)
        if params_node is not None:
            method_scope.update(_formal_param_types(params_node, source))
        body = node.child_by_field_name("body")
        if body is not None:
            method_scope.update(_collect_field_types(body, source))
            self._collect_calls(body, source, pf, qualified_name, owner_qname, method_scope)

    def _emit_constructor(
        self,
        node: Node,
        source: bytes,
        pf: ParsedFile,
        owner_qname: str,
        owner_simple_name: str,
        scope_types: dict[str, str],
    ) -> None:
        params_node = node.child_by_field_name("parameters")
        param_types = _param_types(params_node, source) if params_node else []
        qualified_name = f"{owner_qname}#<init>({','.join(param_types)})"

        pf.nodes.append(
            NodeRecord(
                kind="constructor",
                file=pf.file,
                span_start=node.start_point[0] + 1,
                span_end=node.end_point[0] + 1,
                qualified_name=qualified_name,
                simple_name=owner_simple_name,
                signature=f"{owner_simple_name}({', '.join(param_types)})",
            )
        )

        method_scope = dict(scope_types)
        if params_node is not None:
            method_scope.update(_formal_param_types(params_node, source))
        body = node.child_by_field_name("body")
        if body is not None:
            method_scope.update(_collect_field_types(body, source))
            self._collect_calls(body, source, pf, qualified_name, owner_qname, method_scope)

    def _emit_fields(
        self, node: Node, source: bytes, pf: ParsedFile, owner_qname: str
    ) -> None:
        type_node = node.child_by_field_name("type")
        type_text = _text(type_node, source) if type_node else ""
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            if name_node is None:
                continue
            simple_name = _text(name_node, source)
            pf.nodes.append(
                NodeRecord(
                    kind="field",
                    file=pf.file,
                    span_start=node.start_point[0] + 1,
                    span_end=node.end_point[0] + 1,
                    qualified_name=f"{owner_qname}#{simple_name}",
                    simple_name=simple_name,
                    signature=f"{type_text} {simple_name}".strip(),
                )
            )

    def _collect_calls(
        self,
        node: Node,
        source: bytes,
        pf: ParsedFile,
        caller_qname: str,
        owner_qname: str,
        scope_types: dict[str, str],
    ) -> None:
        # Refresh scope with any local variable declarations inside this block.
        scope_types = dict(scope_types)
        scope_types.update(_collect_field_types(node, source))
        owner_simple_name = owner_qname.rsplit(".", 1)[-1]

        for child in node.children:
            if child.type in _TYPE_DECL_TYPES:
                continue  # local/anonymous classes get their own walk elsewhere
            if child.type == "method_invocation":
                name_node = child.child_by_field_name("name")
                object_node = child.child_by_field_name("object")
                if name_node is not None:
                    receiver_kind, receiver_hint = _classify_receiver(
                        object_node, source, scope_types
                    )
                    args_node = child.child_by_field_name("arguments")
                    pf.calls.append(
                        CallRef(
                            caller_qualified_name=caller_qname,
                            owner_qname=owner_qname,
                            method_simple_name=_text(name_node, source),
                            receiver_hint=receiver_hint,
                            receiver_kind=receiver_kind,
                            argument_count=_count_arguments(args_node),
                            line=child.start_point[0] + 1,
                            argument_type_hints=_infer_argument_types(
                                args_node, source, scope_types, owner_simple_name
                            ),
                        )
                    )
            elif child.type == "object_creation_expression":
                type_node = child.child_by_field_name("type")
                if type_node is not None:
                    args_node = child.child_by_field_name("arguments")
                    pf.calls.append(
                        CallRef(
                            caller_qualified_name=caller_qname,
                            owner_qname=owner_qname,
                            method_simple_name="<init>",
                            receiver_hint=_simple_type_name(_text(type_node, source)),
                            receiver_kind="type",
                            argument_count=_count_arguments(args_node),
                            line=child.start_point[0] + 1,
                            argument_type_hints=_infer_argument_types(
                                args_node, source, scope_types, owner_simple_name
                            ),
                        )
                    )
            self._collect_calls(child, source, pf, caller_qname, owner_qname, scope_types)

    def walk_repo(self) -> Iterator[ParsedFile]:
        """Walk all ``*.java`` files under ``repo_root`` (skipping build output dirs)."""
        skip_dirs = {".git", "target", "build", "out", "node_modules", ".repoweaver"}
        for java_file in sorted(self.repo_root.rglob("*.java")):
            if any(
                part in skip_dirs
                for part in java_file.relative_to(self.repo_root).parts
            ):
                continue
            yield self.parse_file(java_file)


# ------------------------------------------------------------------
# Small standalone tree helpers
# ------------------------------------------------------------------


def _first_of_type(node: Node, types: set[str]) -> Node | None:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _first_named_type(super_node: Node) -> Node | None:
    for child in super_node.children:
        if child.is_named and child.type != "extends" and child.type != "implements":
            return child
    return None


def _iter_type_list(super_node: Node) -> Iterator[Node]:
    for child in super_node.children:
        if child.type == "interface_type_list" or child.type == "type_list":
            for t in child.children:
                if t.is_named:
                    yield t
        elif child.is_named and child.type not in ("extends", "implements"):
            yield child


def _parse_import(node: Node, source: bytes) -> ImportRef:
    is_static = any(c.type == "static" for c in node.children)
    is_wildcard = any(c.type == "asterisk" for c in node.children)
    name_node = node.child_by_field_name("name") or _first_of_type(
        node, {"scoped_identifier", "identifier"}
    )
    name = _text(name_node, source) if name_node is not None else ""
    return ImportRef(
        imported_name=name,
        is_wildcard=is_wildcard,
        is_static=is_static,
        line=node.start_point[0] + 1,
    )


def _param_types(params_node: Node, source: bytes) -> list[str]:
    types = []
    for p in params_node.children:
        if p.type == "formal_parameter" or p.type == "spread_parameter":
            type_node = p.child_by_field_name("type")
            if type_node is not None:
                types.append(_simple_type_name(_text(type_node, source)))
    return types


def _formal_param_types(params_node: Node, source: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in params_node.children:
        if p.type in ("formal_parameter", "spread_parameter"):
            type_node = p.child_by_field_name("type")
            name_node = p.child_by_field_name("name")
            if type_node is not None and name_node is not None:
                out[_text(name_node, source)] = _simple_type_name(
                    _text(type_node, source)
                )
    return out


def _collect_field_types(node: Node, source: bytes) -> dict[str, str]:
    """Best-effort local-variable/field type map for receiver-type narrowing."""
    out: dict[str, str] = {}
    for child in node.children:
        if child.type in ("local_variable_declaration", "field_declaration"):
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            type_name = _simple_type_name(_text(type_node, source))
            for decl in child.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None:
                        out[_text(name_node, source)] = type_name
    return out


def _extract_annotations(decl_node: Node, source: bytes) -> list[str]:
    """Simple names of annotations on a type/method declaration, e.g.
    `@org.foo.GetMapping("/x")` -> "GetMapping". Used only for entry-point
    detection; never for call resolution."""
    names: list[str] = []
    modifiers = _first_of_type(decl_node, {"modifiers"})
    if modifiers is None:
        return names
    for child in modifiers.children:
        if child.type not in ("marker_annotation", "annotation"):
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        names.append(_simple_type_name(_text(name_node, source)))
    return names


def _count_arguments(args_node: Node | None) -> int:
    if args_node is None:
        return 0
    return sum(1 for c in args_node.children if c.is_named)


_INTEGER_LITERAL_TYPES = {
    "decimal_integer_literal",
    "hex_integer_literal",
    "octal_integer_literal",
    "binary_integer_literal",
}
_FLOATING_LITERAL_TYPES = {"decimal_floating_point_literal", "hex_floating_point_literal"}


def _literal_argument_type(node: Node, source: bytes) -> str | None:
    if node.type == "string_literal":
        return "String"
    if node.type == "character_literal":
        return "char"
    if node.type in ("true", "false"):
        return "boolean"
    if node.type == "null_literal":
        return "null"
    if node.type in _INTEGER_LITERAL_TYPES:
        return "long" if _text(node, source)[-1:] in ("l", "L") else "int"
    if node.type in _FLOATING_LITERAL_TYPES:
        return "float" if _text(node, source)[-1:] in ("f", "F") else "double"
    return None


def _infer_argument_type(
    node: Node, source: bytes, scope_types: dict[str, str], owner_simple_name: str
) -> str:
    """Best-effort static type of one argument expression: a simple type
    name, "null" for the null literal, or "unknown" when it cannot be safely
    determined from single-file information alone. "unknown" is always the
    honest fallback, never a guess — the resolver treats it as neutral."""
    literal = _literal_argument_type(node, source)
    if literal is not None:
        return literal
    if node.type == "identifier":
        return scope_types.get(_text(node, source), "unknown")
    if node.type == "this":
        return owner_simple_name
    if node.type in ("cast_expression", "object_creation_expression", "array_creation_expression"):
        type_node = node.child_by_field_name("type")
        return _simple_type_name(_text(type_node, source)) if type_node is not None else "unknown"
    if node.type == "class_literal":
        # `Foo.class`'s static type is always exactly java.lang.Class — never
        # its (JDK, unindexed) generic argument, and never java.lang.reflect.Type.
        return "Class"
    if node.type == "field_access":
        fa_object = node.child_by_field_name("object")
        fa_field = node.child_by_field_name("field")
        if fa_object is not None and fa_object.type == "this" and fa_field is not None:
            return scope_types.get(_text(fa_field, source), "unknown")
        return "unknown"
    if node.type == "method_invocation":
        # Return-type resolution needs the repo-wide symbol table, which is
        # not available during single-file parsing — always "unknown" here
        # rather than guess, matching the resolver's own no-guessing rule.
        return "unknown"
    return "unknown"


def _infer_argument_types(
    args_node: Node | None,
    source: bytes,
    scope_types: dict[str, str],
    owner_simple_name: str,
) -> list[str]:
    if args_node is None:
        return []
    return [
        _infer_argument_type(c, source, scope_types, owner_simple_name)
        for c in args_node.children
        if c.is_named
    ]


def _classify_receiver(
    object_node: Node | None, source: bytes, scope_types: dict[str, str]
) -> tuple[str, str | None]:
    """Returns (receiver_kind, receiver_hint) for a method_invocation's
    `object` field. `receiver_hint` is a simple type name for "variable"/
    "type" kinds, else None — never guessed for multi-hop chains."""
    if object_node is None:
        return "unqualified", None
    if object_node.type == "this":
        return "this", None
    if object_node.type == "super":
        return "super", None
    if object_node.type == "identifier":
        obj_text = _text(object_node, source)
        declared = scope_types.get(obj_text)
        if declared is not None:
            return "variable", declared
        return "type", _simple_type_name(obj_text)
    if object_node.type == "field_access":
        fa_object = object_node.child_by_field_name("object")
        fa_field = object_node.child_by_field_name("field")
        if fa_object is not None and fa_object.type == "this" and fa_field is not None:
            field_name = _text(fa_field, source)
            declared = scope_types.get(field_name)
            if declared is not None:
                return "variable", declared
        return "chain", None
    return "chain", None


def _type_signature(node: Node, source: bytes, kind: str, simple_name: str) -> str:
    parts = [kind, simple_name]
    for child in node.children:
        if child.type == "superclass":
            t = _first_named_type(child)
            if t is not None:
                parts.append(f"extends {_text(t, source)}")
        elif child.type in ("super_interfaces", "extends_interfaces"):
            names = [_text(t, source) for t in _iter_type_list(child)]
            if names:
                verb = "implements" if child.type == "super_interfaces" else "extends"
                parts.append(f"{verb} {', '.join(names)}")
    return " ".join(parts)
