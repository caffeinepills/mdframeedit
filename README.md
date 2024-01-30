Simple frame editor to edit FrameData.xml and see results live instead of manually editing XML data and importing into your game.

Notes:
For an easier time, copy an existing FrameData.xml to use and modify. Also make sure the FrameWidth, FrameHeight XML datas are accurate for the sheet you are using.

Save Options:

Trim Copies: Instead of writing full animation data, will instead write an Animation Action as CopyOf data. If your program doesn't handle this scenario, leave unchecked to write all frame data.

Collapse Singles: If there is only one sequence that is the same for all 8 directions, it will save it as 1 sequence. If your program doesn't handle this scenario, leave unchecked to write all 8 sequences.

If you want to build yourself, you can do so via Pyinstaller: pyinstaller MDFrameEditor.spec

Download for a Windows version is provided

----

1.3:
* Add: You can now load multi-sheet images and data.
* Add: You can now export new multi-sheets and single sheets.
* Fix: Crash when changing frame index numbers.
* Fix: Blank AnimSequences referring to the same set of data.

1.2:
* Fix: Collapsable accidentally combining if there was just 1 other direction that matched.

1.1: 
* Fix: The original of a copy is no longer set as a copy preventing copy loop.
* Fix: No longer writes -1 as the index ID if it does not exist. It is omitted now.
* Fix: Animation speed label no longer squashed on some display settings.
* Change: Trim/Collapse save options are now default.
* Add: Error message if OpenGL version is not supported.
* Add: Image to readme.
 
1.0:
* Initial Release

Known Issue
* False virus notification. Due to PyInstaller (the packaging utility) being used by people to package malicious 
 programs, many anti-virus programs may flag the program. Add an exception to the directory you will keep the program at.
 
  https://support.microsoft.com/en-us/windows/add-an-exclusion-to-windows-security-811816c0-4dfd-af4a-47e4-c301afe13b26
* If you don't trust the release versions, you can download the source and package it into an executable yourself.


![img.png](img.png)