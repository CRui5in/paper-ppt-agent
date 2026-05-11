# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(globals().get("SPECPATH", ".")).resolve()
frontend_dist = project_root / "frontend" / "dist"

datas = [
    (str(project_root / "assets"), "assets"),
    (str(project_root / ".env.example"), "."),
]

if frontend_dist.exists():
    datas.append((str(frontend_dist), "frontend/dist"))

hiddenimports = collect_submodules("backend")


a = Analysis(
    ["run_app.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PaperPPTAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PaperPPTAgent",
)
