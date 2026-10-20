GitHub Actions smoke tests for the supported operating systems. Commit this
folder along with the build workflow.

Run `python tests/runner/test_smoke.py` to check GUI startup, file watching, and
CLI conversion. These tests do not depend on the local `tests/local/` or `tests/cli/`
folders.

After building, GitHub Actions runs the CLI check again against the packaged
executable. It converts a tiny sprite in both directions and checks the output.
Set `MDFRAMEEDIT_TEST_EXECUTABLE` to the executable path to run that check locally:

```powershell
$env:MDFRAMEEDIT_TEST_EXECUTABLE = 'dist/MD Frame Editor/MDFrameEditor.exe'
python tests/runner/test_smoke.py EditorSmokeTest.test_cli_converts_both_formats
```
