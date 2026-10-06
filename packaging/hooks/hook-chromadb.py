"""Retain Chroma's separate Rust import package and migration resources."""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

hiddenimports = collect_submodules("chromadb") + collect_submodules(
    "chromadb_rust_bindings"
)
binaries = collect_dynamic_libs("chromadb_rust_bindings")
datas = collect_data_files("chromadb") + copy_metadata("chromadb")
