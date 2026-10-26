GitHub Actions smoke tests for the supported operating systems. Commit this
folder along with the build workflow.

Run `python tests/runner/test_smoke.py` to check GUI startup, file watching, and
CLI conversion. These tests do not depend on the local `tests/local/` or `tests/cli/`
folders.

Run `python tests/runner/test_native_format.py` for native JSON/PNG round trips,
XML interoperability (including ignored `CutsceneIdle` flags and timed blank
multi-sheet cells), validation, save rollback and CLI conversion. The startup
smoke test also covers native saving, reopening, undo and XML export in the UI.

Run `python tests/runner/test_image_atlas.py` for image cell ordering, pixel
preservation, explicit edge padding and native saving before actions are added.

Run `python tests/runner/test_png_saving.py` for automatic indexed PNG saving,
1/2/4/8-bit palettes, exact RGBA preservation (including partial alpha and RGB
under transparent pixels), size-based fallback, and verification failure safety.
Native saves, XML exports and project build textures share this encoder. Images
with more than 256 RGBA colors retain truecolor; colors are never quantized.
CLI conversions and project builds accept `--no-png-verification` to skip the
encoder's pixel round-trip checks and `--no-png-optimization` to write ordinary
RGBA PNGs with default compression and no palette attempt. These independent
switches can be combined; both features remain enabled by default. Source and
format validation, staged bundle validation and transactional writes still run.
The PNG runner also covers option propagation through spawned workers/builds
and restoration of defaults after a conversion finishes or fails.

For manual verification-overhead measurements, run
`python tools/benchmarks/benchmark_png_saving.py SOURCE.png --synthetic`.
This compares the current writer with its `verify=False` option,
checks that their output bytes match, and reports median paired timing differences
over 31 alternating samples after warmup. Source loading is excluded; PNG encoding
and temporary file writes are included. It leaves application behavior and source
images unchanged. Use `--output PATH.json` to retain measurements. This benchmark
is separate from CI tests and has no timing assertions.

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
