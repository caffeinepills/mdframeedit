# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from shutil import copy2

icon = str(Path(SPECPATH) / 'icon.ico')

a = Analysis(['main.py'],
             pathex=[],
             binaries=[],
             datas=[
                 (str(Path(SPECPATH) / file_name), '.')
                 for file_name in ('icon.ico', 'LICENSE', 'README.md')
             ],
             hiddenimports=[],
             hookspath=[],
             hooksconfig={},
             runtime_hooks=[],
             excludes=[],
             noarchive=False)
pyz = PYZ(a.pure)

exe = EXE(pyz,
          a.scripts, 
          [],
          exclude_binaries=True,
          contents_directory='_internal',
          name='MDFrameEditor',
          icon=icon,
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          console=False,
          disable_windowed_traceback=False,
          target_arch=None,
          codesign_identity=None,
          entitlements_file=None)
coll = COLLECT(exe,
               a.binaries,
               a.datas, 
               strip=False,
               upx=True,
               upx_exclude=[],
               name='MD Frame Editor')

# Keep the documents visible beside the executable in one-folder builds.
for file_name in ('LICENSE', 'README.md'):
    copy2(Path(SPECPATH) / file_name, Path(coll.name) / file_name)

app = BUNDLE(coll,
         name='MDFrameEditor.app',
         icon=icon,
         bundle_identifier=None)
