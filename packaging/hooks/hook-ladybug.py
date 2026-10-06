"""Retain the Ladybug graph extension and version metadata."""

from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata

hiddenimports = ["ladybug._lbug"]
binaries = collect_dynamic_libs("ladybug")
datas = copy_metadata("ladybug")
