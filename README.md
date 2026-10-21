# Sprite Animation Editor

Edit sprite animations in native JSON, with FrameData.xml and AnimData.xml import
and export. Open existing sprites or create one from an image.

## Getting started

* **New / Ctrl+N** - Import an image and choose its frame cell size.
* **Open / Ctrl+O** - Open sprite JSON or XML files in separate tabs.
* **Save / Save As** - Save the active sprite as native JSON and PNG.
* **Export** - Write a single-sheet or multi-sheet XML bundle.

Imported XML files are left untouched. Each sprite tab has its own Undo/Redo.

## Controls

Click the animation preview before using its keyboard controls.

| Key / gesture | Action |
| --- | --- |
| W / A / S / D | Change facing; combine keys for diagonals. |
| O | Toggle Action Points. |
| Mouse wheel | Zoom. |
| Ctrl + mouse wheel | Adjust sprite opacity. |
| Left-drag with Lock Camera unchecked | Pan. |

Use the timeline to play or target frames.

The Edit menu includes copying Action Points and animation settings, resizing
frame cells, and batch changes to durations and offsets. Copy/paste works across
tabs, and Action Points can be pasted to multiple selected sheet frames.

## Preview and settings

**View Layers** previews sprites together using Action Point relationships.
Play Together / Pause Together controls their playback. Save Preview / Load
Preview keeps the arrangement separate from sprite data.

Settings includes window size persistence, camera locking and automatic artwork reload. View
contains display options and sprite/palette information.

Sprite and shadow opacity affect the preview. The shadow size dropdown changes
the saved sprite data. For XML exports, **Trim Copies** writes shared actions as
copy references; **Collapse Singles** combines identical directions.

## Running from source

Use Python 3.10 or newer:

```shell
python -m pip install -e ".[gui]"
python main.py
```

Build an executable with `pyinstaller MDFrameEditor.spec`.

## Command line

```shell
python main.py convert sprites/AnimData.xml --to single --output converted
python main.py palette sprites/Anim.png
```

Use `python main.py convert --help` for conversion options.

## Known issues

Packaged builds can trigger antivirus false positives. You can build from source
if you prefer.

![Screenshot](https://github.com/user-attachments/assets/07bad09b-7c1d-46fc-aede-abad219e65ba)

<details>
<summary>Version history</summary>


# Version history


---

## 2.1

** New **

* Added a native format.
* Added Action Point marker size.
* Allow copy and pasting action points to multiple frames at once.
* WASD now selects sprite facing, S no longer toggles shadows.

## 2.0

** New **

* Can now open multiple sprites in separate tabs.
* Option to automatically reload changed artwork in single-sheet mode.
* Now able to modify the sprite action points.
* Option to copy/paste an action from one tab to another (if compatible).
* Can copy/paste action point values between frames.
* Dialog to view sheet information, including palette.
* Add option to resize and center frames to trim whitespace or extend it.
* Add Onion view to better see how the animation progresses at once.
* Add left and right rotate buttons.
* Undo and redo support.
* Able to adjust opacity for sprites, shadows, and action points.
* Now can set the shadow size within the tool, and preview the size.
* You can now unlock the camera for dragging.
* New app icon.
* CLI options for single-sheet and multi-sheet conversions.
* Remember window size, panel sizes, and preview preferences.

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

</details>
