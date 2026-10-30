# PyInstaller spec: one-dir console build -> dist/cue-upload/cue-upload[.exe]
# Build: pyinstaller packaging/cue-upload.spec --noconfirm --clean
# --onedir (not --onefile): starts in < 0.2 s, no unpacking to %TEMP% per run.
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hiddenimports = [
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.null",
    "truststore",
] + collect_submodules("keyring.backends")

# Package metadata: __version__ and keyring's entry-point backend discovery.
datas = copy_metadata("cue-provider-utility") + copy_metadata("keyring")

a = Analysis(
    ["cue_upload_entry.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cue-upload",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="cue-upload")
