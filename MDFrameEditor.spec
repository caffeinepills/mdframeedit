# -*- mode: python ; coding: utf-8 -*-

a = Analysis(['main.py'],
             pathex=[],
             binaries=[],
             datas=[],
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
app = BUNDLE(coll,
         name='MDFrameEditor.app',
         icon=None,
         bundle_identifier=None)
