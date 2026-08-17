from __future__ import annotations

from pathlib import Path

from repoweaver.parser.java import JavaParser

FIXTURE = Path(__file__).parent / "fixtures" / "javademo"


def _parse_all():
    parser = JavaParser(FIXTURE)
    return {pf.file: pf for pf in parser.walk_repo()}


def test_walk_repo_finds_all_files():
    files = _parse_all()
    assert len(files) == 7
    assert "com/example/demo/App.java" in files


def test_package_and_class_extraction():
    files = _parse_all()
    pf = files["com/example/demo/Greeter.java"]
    assert pf.package == "com.example.demo"
    kinds = {n.kind for n in pf.nodes}
    assert "interface" in kinds
    iface = next(n for n in pf.nodes if n.kind == "interface")
    assert iface.qualified_name == "com.example.demo.Greeter"
    assert iface.simple_name == "Greeter"
    assert iface.span_start == 3  # 1-based, "public interface Greeter {"


def test_enum_extraction():
    files = _parse_all()
    pf = files["com/example/demo/Level.java"]
    enum_node = next(n for n in pf.nodes if n.kind == "enum")
    assert enum_node.qualified_name == "com.example.demo.Level"
    constants = {n.simple_name for n in pf.nodes if n.kind == "enum_constant"}
    assert constants == {"INFO", "WARN", "ERROR"}


def test_method_field_and_constructor_extraction():
    files = _parse_all()
    pf = files["com/example/demo/EnglishGreeter.java"]
    method = next(
        n for n in pf.nodes if n.kind == "method" and n.simple_name == "greet"
    )
    assert method.qualified_name.startswith("com.example.demo.EnglishGreeter#greet(")
    ctor = next(n for n in pf.nodes if n.kind == "constructor")
    assert ctor.qualified_name == "com.example.demo.EnglishGreeter#<init>()"
    fld = next(n for n in pf.nodes if n.kind == "field")
    assert fld.qualified_name == "com.example.demo.EnglishGreeter#formatter"


def test_extends_and_implements_type_refs():
    files = _parse_all()
    friendly = files["com/example/demo/FriendlyGreeter.java"]
    extends = [r for r in friendly.type_refs if r.edge_type == "EXTENDS"]
    assert any(r.supertype_simple_name == "AbstractGreeter" for r in extends)

    abstract = files["com/example/demo/AbstractGreeter.java"]
    implements = [r for r in abstract.type_refs if r.edge_type == "IMPLEMENTS"]
    assert any(r.supertype_simple_name == "Greeter" for r in implements)


def test_import_extraction():
    files = _parse_all()
    app = files["com/example/demo/App.java"]
    assert len(app.imports) == 1
    imp = app.imports[0]
    assert imp.imported_name == "com.example.demo.EnglishGreeter"
    assert not imp.is_wildcard
    assert not imp.is_static


def test_annotation_declaration_is_a_symbol(tmp_path: Path):
    source = tmp_path / "Marker.java"
    source.write_text(
        'package demo; public @interface Marker { String value() default ""; }',
        encoding="utf-8",
    )
    parsed = JavaParser(tmp_path).parse_file(source)
    annotation = next(node for node in parsed.nodes if node.kind == "annotation")
    assert annotation.qualified_name == "demo.Marker"
    assert annotation.simple_name == "Marker"


def test_calls_extraction():
    files = _parse_all()
    app = files["com/example/demo/App.java"]
    call_names = {c.method_simple_name for c in app.calls}
    assert "greet" in call_names
    assert (
        "<init>" in call_names
    )  # object_creation_expression for `new App(...)`/`new EnglishGreeter()`
