"""Phase 3b: the duplicate detector.

Two real bugs were found building this, both against the real codebase, not
synthetic cases — and both are reproduced here with small fixtures so they
cannot come back silently:

1. Plain union-find chaining: "A is close to B, B is close to C" does not
   mean A and C belong together. It merged 26 unrelated route handlers into
   one "duplicate" because of a few genuinely close pairs inside it.
2. `differences()` alone counts edit REGIONS, not their size: a tiny
   `Corner.__init__` (two fields) matched several unrelated, much longer
   exception constructors because the extra content collapsed into one
   oversized substitution. `compare()`'s shared-fraction check catches it.
"""

import textwrap
from pathlib import Path

import pytest

from codeintel import duplicates as D
from codeintel import store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture()
def conn(tmp_path):
    return store.connect(tmp_path / "i.db")


# ------------------------------------------------------- compare() itself

def test_identical_bodies_have_no_differences_and_full_sharing():
    toks = ["a", "=", "1"]
    diffs, shared = D.compare(toks, list(toks))
    assert diffs == [] and shared == 1.0


def test_the_same_substitution_repeated_counts_once():
    a = ["x", "=", "1", ";", "y", "=", "1"]
    b = ["x", "=", "2", ";", "y", "=", "2"]
    diffs, _ = D.compare(a, b)
    assert diffs == [("1", "2")]


def test_a_tiny_body_does_not_read_as_close_to_a_much_larger_one():
    """The Corner.__init__ / InsufficientStockError.__init__ bug: few edit
    regions is not the same as little actual difference."""
    tiny = ["self", ".", "x", "=", "x", "self", ".", "y", "=", "y"]
    big = (["self", ".", "x", "=", "x", "self", ".", "y", "=", "y"]
           + ["self", ".", "requested", "=", "requested"] * 6)   # pads the "extra" content
    diffs, shared = D.compare(tiny, big)
    assert shared < D.MIN_SHARED, "a body nearly all extra content must not pass the shared-fraction floor"


# ------------------------------------------------------------- find(): basics

def _pair(conn, root, body_a, body_b, name="shared_logic", path_a="app/a.py", path_b="app/b.py"):
    write(root, path_a, f"def {name}():\n{textwrap.indent(body_a, '    ')}\n")
    write(root, path_b, f"def {name}():\n{textwrap.indent(body_b, '    ')}\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    return D.find(conn, root.parent, include_tests=False)


def test_an_identical_pair_is_found_with_no_differences(conn, tmp_path):
    body = "a = 1\nb = 2\nc = 3\nreturn a + b + c\n"
    ds = _pair(conn, tmp_path / "backend", body, body)
    assert len(ds) == 1 and ds[0].identical
    assert {m.path for m in ds[0].members} == {"app/a.py", "app/b.py"}


def test_a_pair_differing_by_one_literal_reports_that_difference(conn, tmp_path):
    a = "x = 1\ny = 2\nz = 3\nreturn x + y + z\n"
    b = "x = 1\ny = 2\nz = 9\nreturn x + y + z\n"
    ds = _pair(conn, tmp_path / "backend", a, b)
    assert len(ds) == 1
    assert ds[0].differences == [("3", "9")]


def test_a_pair_differing_in_more_than_max_params_is_not_reported(conn, tmp_path):
    a = "p = 1\nq = 2\nr = 3\ns = 4\nreturn p + q + r + s\n"
    b = "p = 9\nq = 8\nr = 7\ns = 6\nreturn p + q + r + s\n"
    assert _pair(conn, tmp_path / "backend", a, b) == []


def test_bodies_shorter_than_the_line_floor_are_not_compared(conn, tmp_path):
    assert _pair(conn, tmp_path / "backend", "return 1\n", "return 2\n") == []


def test_same_name_in_different_files_has_no_size_floor(conn, tmp_path):
    """Two 4-line `formatTokens` — the real admin-panel case — must be found
    even though MIN_LINES (5) would otherwise exclude them."""
    body = "if n is None:\n    return '-'\nreturn str(n)\n"
    ds = _pair(conn, tmp_path / "backend", body, body, name="format_tokens")
    assert len(ds) == 1 and ds[0].identical


def test_tests_are_excluded_by_default_and_included_on_request(conn, tmp_path):
    root = tmp_path / "backend"
    body = "a = 1\nb = 2\nc = 3\nreturn a + b + c\n"
    write(root, "tests/test_a.py", f"def fixture():\n{textwrap.indent(body, '    ')}\n")
    write(root, "tests/test_b.py", f"def fixture():\n{textwrap.indent(body, '    ')}\n")
    scan(conn, [Repo("backend", root, "python", (), ("tests",))])
    assert D.find(conn, root.parent, include_tests=False) == []
    found = D.find(conn, root.parent, include_tests=True)
    assert len(found) == 1 and found[0].in_tests


# ---------------------------------------------- the clique fix (bug #1)

def test_a_chain_of_near_matches_is_not_merged_into_one_false_group(conn, tmp_path):
    """A close to B, B close to C, A NOT close to C. Plain union-find would
    report one 3-member group and claim A and C are alike — they are not."""
    root = tmp_path / "backend"
    # four independent "slots"; a differs from b in slots 3-4, b differs from
    # c in slots 1-2, so a and c differ in all four — over MAX_PARAMS.
    def body(s1, s2, s3, s4):
        return f"{s1}()\n{s2}()\n{s3}()\n{s4}()\nreturn 1\n"
    write(root, "app/a.py", f"def f():\n{textwrap.indent(body('s1','s2','s3','s4'), '    ')}\n")
    write(root, "app/b.py", f"def f():\n{textwrap.indent(body('s1','s2','X3','X4'), '    ')}\n")
    write(root, "app/c.py", f"def f():\n{textwrap.indent(body('Y1','Y2','X3','X4'), '    ')}\n")
    scan(conn, [Repo("backend", root, "app".replace("app", "backend") and "python", ("app",), ())])
    found = D.find(conn, root.parent, include_tests=False)

    groups = {frozenset(m.path for m in d.members) for d in found}
    assert frozenset({"app/a.py", "app/c.py"}) not in groups, "a and c do not actually match"
    assert frozenset({"app/a.py", "app/b.py"}) in groups
    assert frozenset({"app/b.py", "app/c.py"}) in groups, "the real b-c pair must not be dropped"
    for d in found:
        assert len(d.differences) <= D.MAX_PARAMS


def test_every_pair_within_a_reported_group_independently_holds(conn, tmp_path):
    """The general form of the clique guarantee: for ANY group found, no
    two of its members may actually differ by more than MAX_PARAMS, however
    many other members connect them."""
    root = tmp_path / "backend"
    import random
    random.seed(0)
    for n in range(6):
        slots = [f"s{n}_{i}" if random.random() > 0.4 else f"shared_{i}" for i in range(5)]
        body = "\n".join(f"{s}()" for s in slots) + "\nreturn 1\n"
        write(root, f"app/m{n}.py", f"def f():\n{textwrap.indent(body, '    ')}\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = D.find(conn, root.parent, include_tests=False)

    import itertools
    for d in found:
        def toks(m):
            return D.body_tokens((root / m.path).read_text(), m.start, m.end, "python")
        tk = {m: toks(m) for m in d.members}
        for a, b in itertools.combinations(d.members, 2):
            diffs, shared = D.compare(tk[a], tk[b])
            assert len(diffs) <= D.MAX_PARAMS and shared >= D.MIN_SHARED


# ---------------------------------------------------- the hub case

def test_a_hub_shared_by_two_unrelated_pairs_reports_both():
    """B matches A, and separately matches C — but A and C don't match each
    other (a star: b-a and b-c are edges, a-c is not). Single-assignment
    clustering claims B for whichever pair is grown first and silently drops
    the other; every node must be tried as a seed so both pairs survive."""
    adj = {0: {1}, 1: {0, 2}, 2: {1}}
    cliques = D.cliques_from(adj)
    assert {frozenset(c) for c in cliques} == {frozenset({0, 1}), frozenset({1, 2})}


def test_a_true_triangle_is_reported_as_one_group_not_three_overlapping_pairs():
    adj = {0: {1, 2}, 1: {0, 2}, 2: {0, 1}}
    cliques = D.cliques_from(adj)
    assert [frozenset(c) for c in cliques] == [frozenset({0, 1, 2})]


def test_a_chain_with_no_triangle_yields_only_the_real_pairs():
    """0-1, 1-2, 2-3 in a line, no shortcuts: no three of these are mutually
    connected, so nothing may claim three or more belong together."""
    adj = {0: {1}, 1: {0, 2}, 2: {1, 3}, 3: {2}}
    cliques = D.cliques_from(adj)
    assert {frozenset(c) for c in cliques} == {frozenset({0, 1}), frozenset({1, 2}), frozenset({2, 3})}
    assert all(len(c) == 2 for c in cliques), "no false triangle or larger group"


# ---------------------------------------------------- thin delegates
# DAS-89: the workers' claim_from_db / _run_loop / main were reported as
# duplicates to merge, though each is one call into code already shared
# (reconciler.claim_next, loop.run, loop.serve). Same for every API route.

def _lines(src: str) -> set[int]:
    return D.thin_delegates(textwrap.dedent(src))


def test_one_call_with_plain_arguments_is_a_thin_delegate():
    assert _lines('''
        def claim(db, worker_id):
            """Docstrings are not logic."""
            return reconciler.claim_next(db, SPEC, worker_id)

        async def fetch(x):
            return await svc.get(x, key=cfg.key, page=None)

        def run(worker_id):
            loop.run(worker_id, name="extraction", spec=SPEC, poll=settings.poll(), **extra)
        ''') == {2, 6, 9}


def test_any_logic_in_the_body_or_its_arguments_is_not_thin():
    assert _lines('''
        def operator(a, b):
            return f(a + b)

        def two_statements(x):
            y = x
            return f(y)

        def comprehension(xs):
            return f([x for x in xs])

        def subscript(x):
            return f(x)[0]

        def conditional(x):
            return f(x) if x else None

        def not_a_call(x):
            return x

        def docstring_only():
            """Nothing else."""
        ''') == set()


def test_worker_shaped_delegates_are_set_aside_not_reported(conn, tmp_path):
    root = tmp_path / "backend"
    for name in ("extraction", "voice"):
        write(root, f"app/{name}_worker.py", f'''
            SPEC = make_spec("{name}")

            def claim_from_db(db, worker_id):
                """Redis-is-down fallback."""
                return reconciler.claim_next(db, SPEC, worker_id)

            def _run_loop(worker_id):
                loop.run(worker_id, name="{name}", spec=SPEC, poll_seconds=settings.{name}_poll)
            ''')
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = D.find(conn, root.parent, include_tests=False)
    by_name = {d.members[0].qualname: d for d in found}
    assert {"claim_from_db", "_run_loop"} <= set(by_name)
    assert by_name["claim_from_db"].delegates and by_name["_run_loop"].delegates


def test_route_shaped_delegates_are_set_aside_and_never_a_review_finding(conn, tmp_path):
    from codeintel import review

    root = tmp_path / "backend"
    routes = "".join(f'''
        @router.patch("/{kind}s/{{{kind}_id}}")
        def update_{kind}(
            {kind}_id: int,
            body: dict,
            db=Depends(get_db),
        ):
            return handlers.update_{kind}(db, {kind}_id, body)
        ''' for kind in ("tenant", "vendor"))
    write(root, "app/admin.py", routes)
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = D.find(conn, root.parent, include_tests=False)
    assert len(found) == 1 and found[0].delegates
    assert review.duplicate_pairs(conn, root.parent, "backend") == []


def test_a_genuine_duplicate_is_still_reported_beside_delegates(conn, tmp_path):
    """Setting delegates aside must not hide real repeated logic."""
    root = tmp_path / "backend"
    body = "total = 0\nfor x in items:\n    if x.ok:\n        total += x.amount\nreturn total\n"
    for name in ("a", "b"):
        write(root, f"app/{name}.py",
              f"def summed(items):\n{textwrap.indent(body, '    ')}\n"
              f"def claim(db, w):\n    return reconciler.claim_next(db, SPEC, w)\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = {d.members[0].qualname: d for d in D.find(conn, root.parent, include_tests=False)}
    assert not found["summed"].delegates
    assert found["claim"].delegates


# ---------------------------------------------------- markup-only components
# DAS-89: the public site's SVG icons were reported as four pairs to merge,
# though each is literal markup that differs only in what it draws.

def _markup(src: str) -> bool:
    text = textwrap.dedent(src).strip("\n")
    return D.markup_only(D.body_tokens(text, 1, len(text.splitlines()), "javascript"))


def test_literal_jsx_markup_is_markup_only():
    assert _markup('''
        export const ShieldIcon = (p) => (
          <svg {...common} {...p}>
            <path d="M12 3l7 3v6Z" />
            <circle cx="12" cy="13" r="3.5" />
          </svg>
        )''')
    assert _markup('''
        export function Badge(p) {
          return (
            <svg {...p} strokeWidth={2.4}><rect x="1" y={-2} aria-hidden="true" /></svg>
          );
        }''')


@pytest.mark.parametrize("body", [
    "<ul>{items.map(x => <li>{x}</li>)}</ul>",            # a call
    '<button onClick={save}>Save</button>',                # wired to a handler
    "<div>{open ? <a /> : null}</div>",                    # a condition
    "<div>{children}</div>",                               # a variable
    "<div className={`x ${y}`} />",                        # a template literal
    "<div style={{ color: 'red' }} />",                    # an object
])
def test_any_expression_handler_or_variable_is_not_markup_only(body):
    assert not _markup(f"export const C = (p) => (\n  {body}\n)")


def test_a_statement_before_the_markup_is_not_markup_only():
    assert not _markup('''
        export function C(p) {
          const n = p.items.length
          return <span>{n}</span>
        }''')


def test_icon_components_differing_only_in_drawing_are_set_aside(conn, tmp_path):
    from codeintel import review

    root = tmp_path / "admin_panel"
    write(root, "src/icons.jsx", '''
        const common = { width: 20, height: 20, viewBox: '0 0 24 24' }

        export const AndroidIcon = (p) => (
          <svg {...common} {...p}>
            <rect x="6" y="4" width="12" height="16" rx="2.5" />
            <path d="M9 8h6M12 16v.01" />
          </svg>
        )

        export const AppleIcon = (p) => (
          <svg {...common} {...p}>
            <rect x="6" y="4" width="12" height="16" rx="2.5" />
            <path d="M12 16v.01" />
          </svg>
        )
        ''')
    scan(conn, [Repo("admin_panel", root, "javascript", ("src",), ())])
    found = D.find(conn, root.parent, include_tests=False)
    assert len(found) == 1 and found[0].markup and found[0].set_aside
    assert review.duplicate_pairs(conn, root.parent, "admin_panel") == []


def test_a_genuine_js_duplicate_is_still_reported(conn, tmp_path):
    """Setting markup aside must not hide components that carry logic."""
    from codeintel import review

    root = tmp_path / "admin_panel"
    write(root, "src/buttons.jsx", "".join(f'''
        export function {name}Button({{ onSave }}) {{
          const [busy, setBusy] = useState(false)
          if (busy) return null
          setBusy(true)
          onSave().finally(() => setBusy(false))
          return <button disabled={{busy}}>{name}</button>
        }}
        ''' for name in ("Save", "Send")))
    scan(conn, [Repo("admin_panel", root, "javascript", ("src",), ())])
    found = D.find(conn, root.parent, include_tests=False)
    assert found and not any(d.set_aside for d in found)
    assert review.duplicate_pairs(conn, root.parent, "admin_panel")
