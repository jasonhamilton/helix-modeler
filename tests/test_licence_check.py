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


def test_noncommercial_detection():
    for s in ("CC BY-NC 4.0", "CC-BY-NC-SA-4.0", "Non-Commercial use only", "noncommercial",
              "Research-only licence", "Creative Commons Attribution-NonCommercial"):
        assert lc.is_noncommercial(s), s
    for s in ("MIT", "Apache-2.0", "LGPL-3.0", "BSD-3-Clause", "Simple licence"):
        assert not lc.is_noncommercial(s), s
    assert lc.classify_licence_text("Attribution-NonCommercial 4.0 International") == "CC-BY-NC"


def test_noncommercial_listed_warns_unlisted_fails(tmp_path, monkeypatch):
    root = _root_with_allowlist(tmp_path, [
        {"name": "diffq", "match": "nc", "reason": "private research", "ref": "DECISIONS-NC"}])
    monkeypatch.setattr(lc, "dependency_rows", lambda: [("diffq", "1", "CC BY-NC 4.0")])
    text, _ = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: WARN" in text
    assert "blocks bundled/commercial release" in text and "DECISIONS-NC" in text
    assert "NEW, unreviewed" not in text and "unlisted" not in text
    monkeypatch.setattr(lc, "dependency_rows",
                        lambda: [("diffq", "1", "CC BY-NC 4.0"), ("other", "2", "non-commercial")])
    text, failed = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: FAIL" in text and "NON-COMMERCIAL, unlisted" in text and failed


def test_noncommercial_via_fake_dist_info(tmp_path, monkeypatch):
    di = tmp_path / "ncpkg-1.0.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: ncpkg\nVersion: 1.0\n"
                                 "License: CC BY-NC 4.0\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    rows = {n: lic for n, _, lic in lc.dependency_rows()}
    assert lc.is_noncommercial(rows["ncpkg"])


def test_mpl_and_soxr_allowlist_warn(tmp_path, monkeypatch):
    root = _root_with_allowlist(tmp_path, [
        {"name": "certifi", "match": "mpl", "reason": "weak copyleft", "ref": "D"},
        {"name": "soxr", "match": "lgpl", "reason": "r", "ref": "D"}])
    monkeypatch.setattr(lc, "dependency_rows",
                        lambda: [("certifi", "1", "MPL-2.0"), ("soxr", "1", "LGPL-2.1-or-later"),
                                 ("fine", "1", "Simple BSD")])
    text, _ = lc.build_report(root, "2026-01-01")
    assert "## Dependency licences: WARN" in text and "WARN certifi" in text and "WARN soxr" in text
    assert "fine 1" not in text.split("## All dependencies")[0]
    monkeypatch.setattr(lc, "dependency_rows", lambda: [("certifi2", "1", "MPL-2.0")])
    assert "## Dependency licences: FAIL" in lc.build_report(root, "2026-01-01")[0]
