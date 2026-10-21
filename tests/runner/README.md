GitHub Actions smoke tests for the supported operating systems. Commit this
folder along with the build workflow.

Run `python tests/runner/test_smoke.py` to check GUI startup, file watching, and
CLI conversion. These tests do not depend on the local `tests/local/` or `tests/cli/`
folders.

Run `python tests/runner/test_native_format.py` for native JSON/PNG round trips,
XML interoperability, validation, save rollback and CLI conversion. The startup
smoke test also covers native saving, reopening, undo and XML export in the UI.

Run `python tests/runner/test_image_atlas.py` for image cell ordering, pixel
preservation, explicit edge padding and native saving before actions are added.

Run `python tests/runner/test_format_adapters.py` for the built-in/custom adapter
boundary, native byte compatibility, metadata ordering, output validation and
transaction rollback. No private local test fixtures are needed.

Run `python tests/runner/test_project_build.py` for YAML project and named rig validation,
derived animations/rush movement, palette output, packing settings, source
isolation, transactional rollback and CLI builds. These are deterministic checks,
without performance benchmarks or local sprite dependencies.

Run `python tests/runner/test_action_alias.py` to check ordered fallbacks,
preservation of existing actions and source files, both XML formats, logging,
write failures, output isolation, and the background dialog worker. GitHub Actions
runs these checks separately from the startup smoke tests.

After building, GitHub Actions runs the CLI check again against the packaged
executable. It converts a tiny sprite in both directions and checks the output.
Set `MDFRAMEEDIT_TEST_EXECUTABLE` to the executable path to run that check locally:

```powershell
$env:MDFRAMEEDIT_TEST_EXECUTABLE = 'dist/MD Frame Editor/MDFrameEditor.exe'
python tests/runner/test_smoke.py EditorSmokeTest.test_cli_converts_both_formats
```
