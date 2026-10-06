"""Freeze the installed wheel: TYCHORA_BUILD_MODE=onedir or onefile."""

import os
from pathlib import Path

import tychora
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH)
source_package = root.parent / "src" / "tychora"
installed_package = Path(tychora.__file__).resolve().parent
if installed_package == source_package.resolve() or "site-packages" not in installed_package.parts:
    raise RuntimeError(f"Freeze requires the installed release wheel, got {installed_package}")

mode = os.environ.get("TYCHORA_BUILD_MODE", "onefile")
if mode not in {"onedir", "onefile"}:
    raise ValueError("TYCHORA_BUILD_MODE must be onedir or onefile")
notices = root.parent / "dist" / "tychora-notices"
if not (notices / "manifest.json").is_file():
    raise RuntimeError("Run packaging/build_notices.py before freezing")

# Native packages and data-only converter resources, without model caches.
hiddenimports = [
    "ladybug",
    "chromadb",
    "chromadb_rust_bindings",
    "docling",
    "docling_core",
    "tree_sitter",
    "tree_sitter_python",
    "tree_sitter_javascript",
    "tree_sitter_typescript",
    "tokenizers",
    "rapidocr",
    "onnxruntime",
    "torchvision",
    "docx",
    "mcp",
    "fastapi",
    "uvicorn",
    "voyageai",
    "anthropic",
]
hiddenimports += collect_submodules("mcp")
datas = []
binaries = []
for package in ("tree_sitter", "tree_sitter_python", "tree_sitter_javascript", "tree_sitter_typescript", "tokenizers", "rapidocr", "onnxruntime", "torchvision"):
    exclusions = ["models/**"] if package == "rapidocr" else ["datasets/**"] if package == "onnxruntime" else []
    datas += collect_data_files(package, excludes=exclusions)
    binaries += collect_dynamic_libs(package)
# Torchvision loads its custom operators by filename through torch.ops rather
# than importing an extension module; PyInstaller does not discover these .so
# files as imports or via collect_dynamic_libs.
torchvision_native = list((installed_package.parent / "torchvision").glob("*.so"))
if not any(path.name.startswith("_C") for path in torchvision_native):
    raise RuntimeError("Installed torchvision custom operators are missing")
binaries += [(str(path), "torchvision") for path in torchvision_native]
datas += collect_data_files("docx", includes=["templates/**/*"])
# python-docx resolves templates from docx/parts/../templates with a raw OS
# path. A physical parts directory must exist for that traversal to work.
datas.append((str(installed_package.parent / "docx/parts/__init__.py"), "docx/parts"))
for distribution in ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript", "tree-sitter-typescript", "tokenizers", "mcp", "rapidocr", "onnxruntime", "tiktoken", "torchvision", "python-docx"):
    datas += copy_metadata(distribution)
datas += [(str(path), str(Path("third_party_notices") / path.relative_to(notices).parent)) for path in notices.rglob("*") if path.is_file()]

a = Analysis(
    [str(root / "frozen_main.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(root / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# These upstream package samples/default weights are not Tychora's verified
# external model assets. The parser and OCR adapter take explicit model paths.
a.datas = type(a.datas)(entry for entry in a.datas if not entry[0].startswith(("rapidocr/models/", "onnxruntime/datasets/")))
pyz = PYZ(a.pure)
if mode == "onefile":
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="tychora", console=True, strip=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="tychora", console=True, strip=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="tychora-onedir")
