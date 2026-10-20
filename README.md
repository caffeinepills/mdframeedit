Frame editor to edit FrameData.xml/AnimData.xml sheets and see results live instead of manually editing XML data and importing into your game.

**Note:**
For an easier time, use existing FrameData or AnimData to use and modify. Also make sure the FrameWidth, FrameHeight XML datas are accurate for the sheet you are using.

# Controls
* **Open / Ctrl+O** - Select one or several sprite XML files. Each sprite opens
  in its own tab, named after its containing folder.
* **Save / Save As / Export** - Apply to the active sprite tab. Each tab keeps
  its edits, selection, zoom, and shadow preview independently.
* **Undo / Redo** - Available under Edit or standard keyboard shortcuts. (History works per tab)
* **O** - Toggle Offset Display (Action Points), if loaded.
* **S** - Toggle Shadow Display.
* **Mouse Scroll** - Zoom in and Out of Preview. Zooming in follows the sprite's offsets as it animates.
* **Hold Ctrl + Mouse Scroll** - Adjust opacity of loaded sprite.

# Settings

**Auto Reload Artwork** is available under Settings. When enabled, changes to `Anim.png` show up automatically without losing your frame or action point edits. If the image dimensions change, reopen the sheet to load the new layout.

# Save Options

**Trim Copies**: Instead of writing full animation data, will instead write an Animation Action as CopyOf data. If your program doesn't handle this scenario, leave unchecked to write all frame data.

**Collapse Singles**: If there is only one sequence that is the same for all 8 directions, it will save it as 1 sequence. If your program doesn't handle this scenario, leave unchecked to write all 8 sequences.

# Running from source

Use Python 3.10 or newer. From the project directory, activate your virtual
environment and install the project and its dependencies:

```shell
python -m pip install -e ".[gui]"
python main.py
```

Dependencies are declared in `pyproject.toml`.

# Building

If you want to build yourself, you can do so via Pyinstaller: `pyinstaller MDFrameEditor.spec` or Nuitka.

Downloads for Windows, MacOs, and Linux (untested) are provided.

# Version history


---

## 2.0

**New**

* Can now open multiple sprites in separate tabs.
* Undo and redo changes to actions, frames, offsets, action points, and shadow size.
* Option to automatically reload changed artwork in single-sheet mode.
* Now able to modify the sprite action points.
* Able to adjust opacity for sprites, shadows, and action points.
* Now can set the shadow size within and preview the size.
* You can now unlock the camera for dragging.
* Remember window size, panel sizes, and preview preferences.
* New app icon.
* CLI options for single-sheet and multi-sheet conversions.

**Fixed**

* A bug for loading multi-sheet sprites, should be much faster now.
* Single direction animations stay that way after editing and exporting.
* Zoom not following the offset of a sprite when animating.
* A couple inconsistencies when saving that were reverting settings.

**Changed**

* Now saves to a temporary folder before overwriting the target, in-case it fails to save causing loss of the original file.
* Updated the UI a bit, should resize better and buttons are clearer across platforms.

---

## 1.4.3

**New**

* Specfile support for MacOS.

**Fixed**

* ActionPoints factory issue on newer Pythons.
* Change arrow keys to be unicode versions, otherwise they show as invalid characters in MacOS.
* Update Batch Add Layout.

**Changed**

* From PyQt5 to PySide6.

---

## 1.4.2

**New**

* Frame information when failing to export multi-sheets due to unmatching frames.
* Warning if frames are evenly divisible when loading a single sheet.

**Fixed**

* Shadow saving as brown instead of black.

**Changed**

* Now checks for multi-sheet export eligibility before outputting frames.

---

## 1.4.1

**Fixed**

* Shadow positions when previewing and exporting.
* Shadows being flipped when exporting.

---

## 1.4

**New**

* Offsets (Action Point) preview markers. Use O to toggle on or off. (Only works if offsets were loaded.)
* Toggle Shadows option.

**Fixed**

* Shadow positions being off by 1 when importing multi and exporting single.

**Changed**

* Pausing an animation will now set the frame data of the frame it paused on.
* Ignore extra empty frames if action points were loaded.

---

## 1.3

**New**

* You can now load multi-sheet images and data.
* You can now export new multi-sheets and single sheets.

**Fixed**

* Crash when changing frame index numbers.
* Blank AnimSequences referring to the same set of data.

---

## 1.2

**Fixed**

* Collapsable accidentally combining if there was just 1 other direction that matched.

---

## 1.1

**New**

* Error message if OpenGL version is not supported.
* Image to readme.

**Fixed**

* The original of a copy is no longer set as a copy preventing copy loop.
* No longer writes -1 as the index ID if it does not exist. It is omitted now.
* Animation speed label no longer squashed on some display settings.

**Changed**

* Trim/Collapse save options are now default.

---

## 1.0

**New**

* Initial Release

# Known Issues
* **False virus notification**: Due to PyInstaller (the packaging utility) being used by people to package malicious 
 programs, many anti-virus programs may flag this packaged program. To get around this you will have to add an exception with your antivirus to the directory you will keep the program at.
 [Click here for an article on adding exceptions for Microsoft Defender](https://support.microsoft.com/en-us/windows/add-an-exclusion-to-windows-security-811816c0-4dfd-af4a-47e4-c301afe13b26)

If you don't trust the release versions, you can download the source and package it into an executable yourself with pyinstaller:
`pyinstaller MDFrameEditor.spec`

![Screenshot](https://github.com/user-attachments/assets/07bad09b-7c1d-46fc-aede-abad219e65ba)

# Command-line conversion

You can convert between single and multi sheets without opening the editor:

```shell
python main.py convert sprites/FrameData.xml --to multi --output converted/multi
python main.py convert sprites/AnimData.xml --to single --output converted/single
```

You can also convert a whole folder. Use `--recursive` to include its subfolders:

```shell
python main.py convert sprites --to single --output converted --recursive --jobs 4
```

When converting a folder, it looks for `AnimData.xml` for `--to single` and
`FrameData.xml` for `--to multi`. The output keeps the same folder structure.
Without `--recursive`, it only checks the folder you selected. If your output
folder is inside that folder, it won't search through the output again.

If you select an XML file directly, it can have a different name. Just keep its
PNGs in the same folder.

By default, it converts up to 4 sprites at once. Use `--jobs` to change that, or
`--executor serial` to convert them one at a time.

Your original files aren't changed. Pick a separate folder for the output.
If that folder already contains files, add `--overwrite` to replace them.
Existing subfolders are fine. If one sprite fails, it will show an error and
continue with the others. It uses the same save checks as the editor and won't
change your editor settings.

Trim Copies and Collapse Singles are enabled by default. Use `--no-trim-copies`
to write the full animation instead of a copy reference. Use
`--no-collapse-singles` to save all 8 directions even if they're the same.

For a list of options, run `python main.py convert --help`.
If you're using this in a script, exit codes are 0 for success, 1 if a conversion
fails, and 2 if the arguments or input aren't valid.

Once installed, you can also use `mdframeedit-convert`:

```shell
mdframeedit-convert sprites --to multi --output converted --recursive --jobs 4
```

If you only want the CLI, install with `python -m pip install -e .`. This only
needs Pillow. Use `python -m pip install -e ".[gui]"` if you also want the editor.
