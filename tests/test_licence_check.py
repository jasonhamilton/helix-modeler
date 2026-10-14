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


def test_classify_licence_text():
    assert lc.classify_licence_text("   Apache License\n  Version 2.0") == "Apache-2.0"
    assert lc.classify_licence_text("GNU LESSER GENERAL PUBLIC LICENSE v3") == "LGPL"
    assert lc.classify_licence_text("GNU GENERAL PUBLIC LICENSE") == "GPL"
    assert lc.classify_licence_text("MIT License\nPermission is hereby granted, free of charge") == "MIT"
    assert lc.classify_licence_text("nothing here") is None


def test_unknown_metadata_falls_back_to_licence_file(tmp_path, monkeypatch):
    from importlib import metadata
    di = tmp_path / "fakepkg-1.0.dist-info"
    (di / "licenses").mkdir(parents=True)
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.0\nLicense: UNKNOWN\n")
    (di / "RECORD").write_text("fakepkg-1.0.dist-info/licenses/LICENSE,,\n")
    (di / "licenses" / "LICENSE").write_text("                Apache License\n Version 2.0\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    metadata.distribution("fakepkg")  # sanity
    rows = {n: lic for n, _, lic in lc.dependency_rows()}
    assert rows["fakepkg"].startswith("Apache-2.0")


def _root_with_allowlist(tmp_path, allow):
    import json
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "licence-allowlist.json").write_text(json.dumps({"allow": allow}))
    return tmp_path


def test_allowlisted_is_warn_and_new_copyleft_fails(tmp_path, monkeypatch):
    root = _root_with_allowlist(tmp_path, [
        {"name": "lameenc", "match": "lgpl", "reason": "r", "ref": "DECISIONS"}])
    monkeypatch.setattr(lc, "dependency_rows", lambda: [("lameenc", "1", "LGPL-3.0-or-later")])
    text, _ = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: WARN" in text and "DECISIONS" in text
    monkeypatch.setattr(lc, "dependency_rows",
                        lambda: [("lameenc", "1", "LGPL-3.0"), ("newgpl", "2", "GPL-3.0")])
    text, _ = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: FAIL" in text and "NEW, unreviewed" in text


def test_allowlisted_pedalboard_fails_without_boundary(tmp_path, monkeypatch):
    root = _root_with_allowlist(tmp_path, [
        {"name": "pedalboard", "match": "gpl", "needs_boundary": True, "reason": "r", "ref": "D"}])
    _tree(root, {"src/tonematch/engines/native_host.py": "import pedalboard\n",
                 "src/tonematch/engines/native.py": "no declaration\n"})
    monkeypatch.setattr(lc, "dependency_rows", lambda: [("pedalboard", "1", "GPLv3")])
    text, _ = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: FAIL" in text
