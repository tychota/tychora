"""Retain lazy public adapters and packaged evidence definitions."""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

hiddenimports = collect_submodules("tychora")
datas = collect_data_files("tychora") + copy_metadata("tychora")
