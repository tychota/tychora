"""Retain Docling's lazy converters, structured data and distribution metadata."""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

hiddenimports = (
    collect_submodules("docling")
    + collect_submodules("docling_core")
    + collect_submodules("docling_parse")
)
datas = (
    collect_data_files("docling")
    + collect_data_files("docling_core")
    + collect_data_files("docling_parse")
    + copy_metadata("docling")
    # The built-in model factory is registered by the slim wheel's [docling]
    # entry point, not by the umbrella docling distribution.
    + copy_metadata("docling-slim")
    + copy_metadata("docling-core")
    + copy_metadata("docling-ibm-models")
    + copy_metadata("docling-parse")
)
