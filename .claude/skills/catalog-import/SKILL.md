---
name: catalog-import
description: Import the Helix model catalog (model ids, categories, parameter names, ranges, defaults) from the user's own HX Edit install into a local cache, without copying any Line 6 file into the repo. Use on first run, after a firmware or HX Edit update, or when a model or parameter name is unknown.
---

# catalog-import

Script: `scripts/catalog_import.py` (owner: `preset`).

## Steps

1. Locate HX Edit's resource folder. Candidate locations differ per OS and version; look in the usual application and application-support directories for a folder belonging to HX Edit that contains JSON describing models. If more than one is found, prefer the newest. If none is found, stop and ask the person for the path; never guess and never download anything.
2. Read the model JSON in place. Extract per model: id (the `@model` string used in `.hlx`), category (comp, drive, amp, cab, eq, mod, delay, reverb, other), parameter names with min, max, default and whether they are continuous or stepped.
3. Write `cache/catalog/<hxedit-version>.json` and `cache/catalog/current.json` (a copy). Both are gitignored. Record the source path and version in the file's `_source` key.
4. Cross-check against `fixtures/golden/*.hlx` when present: every `@model` and parameter name in the templates must resolve. Report any that do not.
5. Print a summary: version, model count per category, parameter count. Append a note to `cache/catalog/IMPORTS.log`.

## Never

Copy, commit, or redistribute any file from the HX Edit install. The cache is derived data for this machine only.
