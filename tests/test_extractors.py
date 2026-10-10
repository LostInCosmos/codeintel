"""What each extractor must get right, using shapes taken from the real
GSTApp code. The first two Dart failures here were found by measuring the
index, not by review: a third of the "functions" were `setState(...)` calls,
and every `dart format`-wrapped signature was missing.
"""

from codeintel.extractors import extract_dart, extract_javascript, extract_python, sanitize


def names(extracted, kind=None):
    return [s.qualname for s in extracted.symbols if kind is None or s.kind == kind]


# ------------------------------------------------------------- sanitize

def test_sanitize_blanks_strings_and_comments_but_keeps_columns():
    """String *contents* are blanked; the delimiters stay, which is harmless
    (a quote is not a bracket) and keeps every column where it was."""
    src = "a = 'x { y'; // } c\nb = \"q(\" /* ) */ + 1"
    out = sanitize(src)
    assert out[0] == "a = '     ';       "
    assert out[1] == 'b = "  "         + 1'
    assert [len(x) for x in out] == [len(x) for x in src.splitlines()]
    assert not any(ch in "".join(out) for ch in "{}()")


def test_sanitize_carries_block_comments_and_triple_quotes_across_lines():
    out = sanitize("x /* {\n } */ y\nz = '''a\n{\n''' + w")
    assert out[0].rstrip() == "x"
    assert out[1].strip() == "y"
    assert "{" not in out[3]
    assert out[4].strip() == "''' + w"                # closing delimiter kept, code after it live


def test_sanitize_template_literals_only_for_javascript():
    src = "const s = `a ${b} {`\nconst t = 1"
    assert "{" not in sanitize(src, backticks=True)[0]
    assert "{" in sanitize(src)[0]


# --------------------------------------------------------------- python

def test_python_functions_classes_methods_and_imports():
    src = '''
from ...core import queue
from .reconciler import JobSpec
import datetime as dt

class Spec:
    """A spec."""
    def method(self, a: int) -> None:
        return a

async def worker(db, job_id: int) -> None:
    """Runs a job."""
    pass
'''
    out = extract_python(src)
    assert names(out) == ["Spec", "Spec.method", "worker"]
    worker = out.symbols[-1]
    assert worker.signature == "db, job_id: int" and worker.doc == "Runs a job."
    assert [(i.target, i.level) for i in out.imports] == [("core", 3), ("reconciler", 1), ("datetime", 0)]


def test_python_renamed_copies_hash_the_same():
    a = extract_python("def f(x):\n    y = x + 1\n    return y * 2\n").symbols[0]
    b = extract_python("def g(n):\n    m = n + 1\n    return m * 2\n").symbols[0]
    c = extract_python("def h(n):\n    m = n - 1\n    return m * 2\n").symbols[0]
    assert a.body_hash == b.body_hash != c.body_hash


def test_python_syntax_error_is_recorded_not_raised():
    assert extract_python("def broken(:\n").error.startswith("SyntaxError")


# ----------------------------------------------------------------- dart

def test_dart_calls_are_not_functions():
    """The defect that put 143 `setState` entries in the index."""
    src = '''
class _S extends State<W> {
  void _go() {
    setState(() {
      busy = true;
    });
    Navigator.of(context).push(MaterialPageRoute(builder: (_) {
      return X();
    }));
  }

  @override
  Widget build(BuildContext context) {
    return TextButton(onPressed: () { _go(); }, child: Text('a'));
  }
}
'''
    assert names(extract_dart(src)) == ["_S", "_S._go", "_S.build"]


def test_dart_signature_wrapped_across_lines_is_found():
    """The defect that dropped every `dart format`-wrapped declaration."""
    src = '''
Future<String?> showGstinConfirmDialog(
  BuildContext context,
  VendorHint hint, {
  Uint8List? photo,
  String fallbackName = '',
}) async {
  return null;
}

class Api {
  Future<int> saveInvoice(
    InvoiceData invoice,
    ExtractionMeta meta, {
    bool overrideErrors = false,
  }) async {
    final res = await post();
    return 1;
  }
}
'''
    out = extract_dart(src)
    assert names(out) == ["showGstinConfirmDialog", "Api", "Api.saveInvoice"]
    dialog = out.symbols[0]
    assert (dialog.start_line, dialog.end_line) == (2, 9)
    assert dialog.signature.startswith("BuildContext context, VendorHint hint, {")


def test_dart_braces_inside_named_params_do_not_end_the_body():
    """`{ ... }` around named parameters must not be read as the body."""
    out = extract_dart("void f(int a, {int b = 1}) {\n  g();\n  h();\n}\nvoid next() {}\n")
    assert [(s.name, s.start_line, s.end_line) for s in out.symbols] == [("f", 1, 4), ("next", 5, 5)]


def test_dart_constructors_factories_getters_and_expression_bodies():
    src = '''
class VendorHint {
  final bool needsConfirmation;
  const VendorHint({required this.needsConfirmation});
  factory VendorHint.fromJson(Map<String, dynamic> json) => VendorHint(
        needsConfirmation: json['needs_confirmation'] as bool? ?? true,
      );
  bool get isTerminal => status == 'done' || status == 'failed';
  set value(int v) { _v = v; }
  int twice(int x) => x * 2;
}
'''
    out = extract_dart(src)
    got = [(s.kind, s.qualname) for s in out.symbols]
    assert got == [
        ("class", "VendorHint"),
        ("constructor", "VendorHint"),
        ("constructor", "VendorHint.fromJson"),
        ("method", "VendorHint.isTerminal"),
        ("method", "VendorHint.value"),
        ("method", "VendorHint.twice"),
    ]
    from_json = out.symbols[2]
    assert (from_json.start_line, from_json.end_line) == (5, 7)


def test_dart_field_initialisers_and_function_typed_fields_are_not_functions():
    src = '''
class C {
  final void Function(String) onTap;
  late final Timer _t = Timer.periodic(d, (_) {});
  static const x = Foo(1);
  final List<int> xs = [];
}
'''
    assert names(extract_dart(src)) == ["C"]


def test_dart_code_inside_strings_and_comments_is_ignored():
    src = '''
// void ghost() {}
/* Widget alsoGhost() { } */
final s = 'void stringGhost() {}';
void real() {}
'''
    assert names(extract_dart(src)) == ["real"]


def test_dart_wrapped_class_header_and_doc_comments():
    src = '''
/// One bill in the capture grid.
class BatchItemCard extends StatelessWidget
    with Mixin {
  /// Builds it.
  @override
  Widget build(BuildContext c) {
    return X();
  }
}
'''
    out = extract_dart(src)
    card, build = out.symbols
    assert (card.qualname, card.end_line, card.doc) == ("BatchItemCard", 10, "One bill in the capture grid.")
    assert (build.qualname, build.doc) == ("BatchItemCard.build", "Builds it.")


def test_dart_imports():
    src = "import 'package:flutter/material.dart';\nimport '../models/invoice.dart';\npart 'x.g.dart';\nexport 'y.dart';\n"
    out = extract_dart(src)
    assert [(i.target, i.is_relative, i.kind) for i in out.imports] == [
        ("package:flutter/material.dart", False, "import"), ("../models/invoice.dart", True, "import"),
        ("x.g.dart", True, "part"), ("y.dart", True, "export")]


def test_dart_main_and_enum_and_mixin():
    out = extract_dart("enum Status { a, b }\nmixin Tick on State {}\nvoid main() {\n  run();\n}\n")
    assert names(out) == ["Status", "Tick", "main"]


# ----------------------------------------------------------- javascript

def test_js_component_with_props_destructured_across_lines():
    src = '''
import { useState } from 'react'
import api from '../api.js'

// Lists invoices.
export default function InvoicesPage({
  tenantId,
  onOpen,
}) {
  const handle = () => { setX(1) }
  return <div>{tenantId}</div>
}

export const Badge = ({ verified }) => (
  <span>{verified ? 'yes' : 'no'}</span>
)

const fmt = n => n.toFixed(2)
'''
    out = extract_javascript(src)
    assert [(s.name, s.start_line, s.end_line) for s in out.symbols] == [
        ("InvoicesPage", 6, 12), ("Badge", 14, 16), ("fmt", 18, 18)]
    assert out.symbols[0].doc == "Lists invoices."
    assert out.symbols[0].signature == "{ tenantId, onOpen, }"
    assert [(i.target, i.is_relative) for i in out.imports] == [("react", False), ("../api.js", True)]


def test_js_calls_and_hooks_are_not_functions():
    src = '''
export function Page() {
  useEffect(() => {
    load()
  }, [])
  return null
}
const value = compute(a, b)
const memo = useMemo(() => x, [x])
'''
    assert names(extract_javascript(src)) == ["Page"]


def test_js_jsx_braces_and_template_literals_do_not_break_bodies():
    src = '''
function Row({ r }) {
  const label = `id ${r.id} {`
  return <td style={{ color: 'red' }}>{label}</td>
}
function After() { return 1 }
'''
    out = extract_javascript(src)
    assert [(s.name, s.start_line, s.end_line) for s in out.symbols] == [("Row", 2, 5), ("After", 6, 6)]


# ------------------------------------------- regressions found on real code

def test_a_body_longer_than_sixty_lines_does_not_hide_what_follows():
    """A 60-line scan cap made a long body "end at end of file", hiding 26
    real classes in the mobile app — every declaration after it vanished."""
    body = "\n".join("  doThing();" for _ in range(150))
    src = f"void longOne() {{\n{body}\n}}\nclass After {{}}\n"
    out = extract_dart(src)
    assert [(s.name, s.end_line) for s in out.symbols] == [("longOne", 152), ("After", 153)]


def test_an_unterminated_body_fails_safe_instead_of_swallowing_the_file():
    """If a block never closes, claim one line — not everything after it."""
    out = extract_dart("void broken() {\n  oops(\nclass StillSeen {}\n")
    assert "StillSeen" in names(out)


def test_js_one_line_arrow_without_semicolon_mid_file():
    """Ended correctly only when it was the last line of the file."""
    src = "const fmt = n => n.toFixed(2)\nconst other = x => x\n"
    out = extract_javascript(src)
    assert [(s.name, s.start_line, s.end_line) for s in out.symbols] == [("fmt", 1, 1), ("other", 2, 2)]


def test_js_long_component_does_not_hide_the_next_one():
    rows = "\n".join(f"      <td>{i}</td>" for i in range(120))
    src = f"export function Big() {{\n  return (\n    <tr>\n{rows}\n    </tr>\n  )\n}}\nexport function Next() {{ return null }}\n"
    assert names(extract_javascript(src)) == ["Big", "Next"]


def test_dart_operator_overloads():
    """None exist in the app yet; the first model with == / hashCode must not vanish."""
    src = """
class Money {
  @override
  bool operator ==(Object other) => other is Money && other.v == v;
  @override
  int get hashCode => v.hashCode;
  Money operator +(Money o) => Money(v + o.v);
}
"""
    assert names(extract_dart(src)) == ["Money", "Money.operator==", "Money.hashCode", "Money.operator+"]


def test_js_apostrophe_in_jsx_text_is_not_a_string():
    """`can't` in markup was read as opening a string, which blanked the
    closing `}` of its expression and left a whole component unmeasured."""
    src = """function Drawer({ log }) {
  function loadLog() { return 1 }
  return <div>{!log && <p style={{ marginTop: 6 }}>No image — can't retry.</p>}</div>
}
function Next() { return null }
"""
    out = extract_javascript(src)
    assert [(s.name, s.start_line, s.end_line) for s in out.symbols] == [("Drawer", 1, 4), ("Next", 5, 5)]


def test_dart_raw_strings_still_sanitised():
    """The JSX apostrophe rule must not reach Dart, where r'...' is a raw string."""
    out = extract_dart("final p = RegExp(r'^[0-9]{2}(x)');\nvoid f() {}\n")
    assert names(out) == ["f"]


def test_python_import_scope_is_recorded():
    """A function-local import is still a dependency, but one usually placed
    there to break a cycle — the graph must be able to tell them apart."""
    src = """
import os
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.models import Tenant
try:
    import redis
except ImportError:
    redis = None

def handle():
    from ...workers import reconciler
    if True:
        from . import deep

class C:
    def m(self):
        import json
"""
    got = [(i.target or ".", i.name, i.scope) for i in extract_python(src).imports]
    assert got == [
        ("os", None, "module"), ("typing", "TYPE_CHECKING", "module"),
        ("app.models", "Tenant", "type_checking"), ("redis", None, "try"),
        ("workers", "reconciler", "function"), (".", "deep", "function"),
        ("json", None, "function"),
    ]


# ---- file-level docs (phase 3: the module registry draws on these) ----

def test_python_module_docstring_is_the_file_doc():
    assert extract_python('"""Background worker for bill extraction.\n\nMore."""\nimport os\n').doc \
        == "Background worker for bill extraction."
    assert extract_python("import os\n").doc is None


def test_dart_opening_comment_is_the_file_doc():
    src = "// Saves the capture batch so it survives an app kill.\n\nimport 'x.dart';\nclass A {}\n"
    assert extract_dart(src).doc == "Saves the capture batch so it survives an app kill."


def test_a_comment_directly_on_the_first_declaration_belongs_to_it_not_the_file():
    """`/// One bill in the grid.` above `class Card` documents Card — it is
    already on that symbol, and must not be credited to the file."""
    out = extract_dart("/// One bill in the grid.\nclass Card {}\n")
    assert out.doc is None
    assert out.symbols[0].doc == "One bill in the grid."


def test_lint_pragmas_and_generated_banners_are_not_docs():
    assert extract_dart("// ignore_for_file: avoid_print\nimport 'x.dart';\n").doc is None
    assert extract_dart("// GENERATED CODE - DO NOT MODIFY\n// coverage:ignore-file\nimport 'x.dart';\n").doc is None
    assert extract_javascript("/* eslint-disable */\nimport x from 'y'\n").doc is None


def test_js_block_comment_opening_a_file():
    src = "/**\n * Thin wrapper over fetch with the admin token.\n */\nimport x from 'y'\n"
    assert extract_javascript(src).doc == "Thin wrapper over fetch with the admin token."
