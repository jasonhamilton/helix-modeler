import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "licence_check", Path(__file__).resolve().parents[1] / "scripts" / "licence_check.py")
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)


def _tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_pedalboard_boundary_flags_test_import(tmp_path):
    root = _tree(tmp_path, {
        "src/tonematch/engines/native_host.py": "import pedalboard\n",
        "tests/test_x.py": "import pytest\np = pytest.importorskip('pedalboard')\n",
        "src/tonematch/engines/native.py": "# LICENCE BOUNDARY\nimport subprocess\n"
                                           "subprocess.run(['py', '-c', 'import pedalboard'])\n",
    })
    hits = lc.pedalboard_imports(root)
    assert [h[0] for h in hits] == ["src/tonematch/engines/native_host.py", "tests/test_x.py"]
    status, notes = lc.section_boundary(root, [])
    assert status == "FAIL" and "tests/test_x.py" in notes[0]


def test_clean_tree_passes_and_assets_fail(tmp_path):
    root = _tree(tmp_path, {
        ".gitignore": "fixtures/golden/*.hlx\n",
        "src/tonematch/engines/native_host.py": "import pedalboard\n",
        "src/tonematch/engines/native.py": "# LICENCE BOUNDARY\n",
    })
    assert lc.section_boundary(root, [])[0] == "PASS"
    assert lc.section_golden(root)[0] == "PASS"
    assert lc.section_assets(root)[0] == "PASS"
    _tree(tmp_path, {"docs/x.hxf": "x", "data/models.json": '{"m": "HD2_AmpFoo"}'})
    status, notes = lc.section_assets(root)
    assert status == "FAIL" and len(notes) == 2
