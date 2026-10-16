"""Exercise the published offline recipe and Python examples."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.assets import asset_path

ROOT = Path(__file__).resolve().parents[2]


def _bundle(tmp_path: Path) -> Path:
    source = asset_path("TYCHORA_TEST_TOKENIZER_ASSETS") / "cl100k_base.tiktoken"
    if not source.is_file():
        pytest.fail(
            f"TYCHORA_TEST_TOKENIZER_ASSETS has no cl100k_base.tiktoken: {source}"
        )
    bundle = tmp_path / "model-bundle" / "tiktoken" / "0.14.0"
    bundle.mkdir(parents=True)
    shutil.copyfile(source, bundle / source.name)
    return bundle.parents[1]


def _environment(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TYCHORA_MODEL_BUNDLE"] = str(_bundle(tmp_path))
    env["TYCHORA_MODEL_CACHE"] = str(tmp_path / "model-cache")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_literal_readme_offline_recipe(tmp_path: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    match = re.search(
        r"<!-- quickstart:start -->\n```bash\n(.*?)\n```\n<!-- quickstart:end -->",
        readme,
        re.DOTALL,
    )
    assert match, "README must expose one literal shell recipe"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", match.group(1)],
        cwd=tmp_path,
        env=_environment(tmp_path),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout
    assert (tmp_path / "knowledge" / "workspace.toml").is_file()


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_public_python_quickstart(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    workspace = tmp_path / "python-knowledge"
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "quickstart.py"), str(workspace)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_public_async_python_quickstart(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    workspace = tmp_path / "async-knowledge"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "quickstart.py"),
            str(workspace),
            "--async",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_generic_external_pack_installs_and_survives_reopen(tmp_path: Path) -> None:
    pack = tmp_path / "knowledge" / "profiles" / "workbench"
    shutil.copytree(ROOT / "examples" / "profiles" / "workbench", pack)
    workspace = tmp_path / "knowledge"
    settings = workspace / "workspace.toml"
    settings.write_text(
        'schema_version = 1\n[profiles.workbench]\npath = "profiles/workbench"\n'
        '[defaults]\nprofile = "workbench"\n'
    )
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    initialized = subprocess.run(
        [
            "tychora",
            "--workspace",
            str(workspace),
            "--offline",
            "init",
            "--provider",
            "configured",
            "--config",
            str(settings),
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    discovered = subprocess.run(
        [
            "tychora",
            "--workspace",
            str(workspace),
            "--offline",
            "inspect",
            "relations",
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert discovered.returncode == 0, discovered.stderr
    assert "clarifies@1" in discovered.stdout


def test_public_relative_markdown_links_resolve() -> None:
    for page in (
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
        ROOT / "examples" / "profiles" / "workbench" / "README.md",
    ):
        text = page.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative = target.split("#", 1)[0]
            assert (page.parent / relative).exists(), f"{page}: broken link {target}"


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_required_tokenizer_fixture_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYCHORA_TEST_TOKENIZER_ASSETS", str(tmp_path / "missing"))
    with pytest.raises(pytest.fail.Exception, match="TYCHORA_TEST_TOKENIZER_ASSETS"):
        _bundle(tmp_path)


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_literal_external_pack_offline_recipe(tmp_path: Path) -> None:
    pack = tmp_path / "examples" / "profiles" / "workbench"
    pack.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "examples" / "profiles" / "workbench", pack)
    guide = (ROOT / "docs" / "configuration.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
    assert len(blocks) >= 2
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", "\n".join(blocks[:2])],
        cwd=tmp_path,
        env=_environment(tmp_path),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." not in result.stderr
    assert (tmp_path / "knowledge" / "workspace.toml").is_file()


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_framework_adapter_keeps_complete_multiple_citations(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from examples.knowledge_adapter import retrieve_segments
    from tychora import Tychora

    class Counter:
        model = "synthetic"
        tokenizer_id = "cl100k_base"

        def count(self, value: str) -> int:
            return len(value)

    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "manual",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "text": "Record verification outcomes.",
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        result = kb.search({"question": "verification", "modalities": ["lexical"]})
        segment = result.segments[0]
        first = segment.citations[0]
        second = first.model_copy(
            update={
                "unit_id": "second-unit",
                "source_id": "second-source",
                "revision_id": "second-revision",
                "source_ref": "second-ref",
                "physical_page": 2,
            }
        )
        multi = segment.model_copy(update={"citations": (first, second)})
        enriched = result.model_copy(update={"segments": (multi,)})
        adapted = retrieve_segments(
            SimpleNamespace(search=lambda _: enriched), "verification"
        )
    assert adapted[0]["metadata"]["citations"] == [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    assert (
        adapted[0]["metadata"]["citations"][0]["unit_id"]
        != adapted[0]["metadata"]["citations"][1]["unit_id"]
    )


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_optimized_quickstart_reports_failed_setup(tmp_path: Path, mode: str) -> None:
    script = """
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from examples import quickstart

os.environ['TYCHORA_MODEL_BUNDLE'] = 'synthetic-bundle'
class SyncKB:
    management = SimpleNamespace(run=lambda request: SimpleNamespace(job=SimpleNamespace(state='failed')))
    def __enter__(self): return self
    def __exit__(self, *args): return None
class AsyncKB:
    async def run(self, request): return SimpleNamespace(job=SimpleNamespace(state='failed'))
    def __init__(self): self.management = self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
if sys.argv[1] == 'sync':
    quickstart.Tychora.open = lambda path: SyncKB()
    quickstart.main(Path('unused'))
else:
    quickstart.AsyncTychora.open = lambda path: AsyncKB()
    asyncio.run(quickstart.async_main(Path('unused')))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, mode],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "Output tokenizer setup did not succeed" in result.stderr


@pytest.mark.assets("TYCHORA_TEST_TOKENIZER_ASSETS")
def test_shipped_pack_project_global_history_and_hierarchy(tmp_path: Path) -> None:
    from tychora import OutputProfile, Tychora, format_result

    class Counter:
        model = "synthetic"
        tokenizer_id = "cl100k_base"

        def count(self, value: str) -> int:
            return len(value)

    workspace = tmp_path / "knowledge"
    shutil.copytree(
        ROOT / "examples" / "profiles" / "workbench",
        workspace / "profiles" / "workbench",
    )
    (workspace / "workspace.toml").write_text(
        'schema_version = 1\n[profiles.workbench]\npath = "profiles/workbench"\n'
        '[defaults]\nprofile = "workbench"\n'
    )
    config = subprocess.run(
        [
            str(Path(sys.executable).parent / "tychora"),
            "--workspace",
            str(workspace),
            "inspect",
            "config",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert config.returncode == 0, config.stderr
    output = json.loads(config.stdout)["values"]["output"]
    assert output["renderer"] == "hierarchical-evidence@1"
    assert output["title"] == "Workbench evidence"
    with Tychora.open(workspace, token_counter=Counter()) as kb:

        def add(key: str, body: str, kind: str, metadata: dict) -> None:
            job = kb.sources.add(
                {
                    "type": "text",
                    "source_key": key,
                    "collection_id": "default",
                    "kind": kind,
                    "format": "text",
                    "text": body,
                    "metadata": metadata,
                }
            )
            status = kb.jobs.wait(job.id, timeout=10)
            assert status.state == "succeeded", (key, status.error_code)

        base_metadata = {
            "title": "Synthetic",
            "publisher": "Example Team",
            "edition": "1",
        }
        add(
            "local",
            "cobalt local project",
            "manual@1",
            {**base_metadata, "scope": {"project": "alpha"}},
        )
        add(
            "global",
            "cobalt global reference",
            "manual@1",
            {**base_metadata, "scope": {"global_facets": ["project"]}},
        )
        add("unbound", "cobalt unbound reference", "manual@1", base_metadata)
        add("history", "heritage standard text", "legacy-standard@1", base_metadata)
        request = {
            "question": "cobalt",
            "modalities": ["lexical"],
            "filters": {"op": "eq", "field": "scope.project", "value": "alpha"},
        }
        local = kb.search(request)
        global_result = kb.search({**request, "include_global": True})
        assert {segment.text for segment in local.segments} == {"cobalt local project"}
        assert {segment.text for segment in global_result.segments} == {
            "cobalt local project",
            "cobalt global reference",
        }
        historical = kb.search({"question": "heritage", "modalities": ["lexical"]})
        assert any(
            segment.kind == "legacy-standard@1" and segment.lane == "standard"
            for segment in historical.segments
        )
        profile = OutputProfile(
            renderer=output["renderer"],
            lane_order=tuple(output["lane_order"]),
            include_empty_lanes=output["include_empty_lanes"],
            title=output["title"],
            envelope_tag=output["envelope_tag"],
        )
        rendered = json.loads(format_result(historical, profile, counter=Counter()))
        assert rendered["schema_version"] == "hierarchical-evidence@1"
        standard = next(
            group for group in rendered["hierarchy"] if group["lane_id"] == "standard"
        )
        assert any(
            segment["kind"] == "legacy-standard@1" for segment in standard["segments"]
        )
