"""A workspace that exercises every eligibility rule the v1/v2 oracle compares.

The profile extends ``documents@1`` with three modes beside compose, audit and
lookup, which keep their proxy/original state split:

- ``verified`` admits accepted originals and accepted proxies only;
- ``sealed`` disables derived proxies;
- ``triage`` admits rejected originals, so a rejected revision's proxies are
  excluded by the revision's rejection alone.

It also adds a date range facet over ``publication_date``. The corpus holds
dated and undated editions, a source with an explicit global facet, a
repository of Python code (non-citable file roots, unit-bound language facets
and record-bound symbol kinds) and text vectors from a local embedder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tests.code.conftest import git, make_repo
from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_rows
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tychora import ProviderBindings, Tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.api import LocalTextBinding
from tychora.configuration.resolve import resolve_config
from tychora.contracts.collections import CollectionCreate
from tychora.contracts.evidence import TextLocator
from tychora.ports.execution import ExecutionContext

_LANES = '["authority", "standard", "feedback", "evidence", "example", "maintenance"]'
PROFILE = f"""schema_version = 1
id = "equivalence"
version = "1"
extends = ["builtin:documents@1"]

[modes.verified]
allowed_lanes = {_LANES}
allowed_uses = ["audit"]
original_review_states = ["accepted"]
derived_proxy_policy = "accepted_only"
allow_derived_proxies = true
derived_proxy_review_states = ["accepted"]
required_lanes = []

[modes.sealed]
allowed_lanes = {_LANES}
allowed_uses = ["lookup"]
original_review_states = ["unreviewed", "needs_review", "accepted"]
derived_proxy_policy = "disabled"
allow_derived_proxies = false
derived_proxy_review_states = []
required_lanes = []

[modes.triage]
allowed_lanes = {_LANES}
allowed_uses = ["audit"]
original_review_states = ["unreviewed", "needs_review", "accepted", "rejected"]
derived_proxy_policy = "permitted"
allow_derived_proxies = true
derived_proxy_review_states = ["unreviewed", "needs_review", "accepted"]
required_lanes = []

[facets."doc.published"]
key = "doc.published"
alias = "published"
value_type = "date"
operators = ["eq", "range"]
source_binding = "metadata.publication_date"
filter_scope = "all_units"
missing = "exclude"
allow_global = false
"""
MODES = ("compose", "audit", "lookup", "verified", "sealed", "triage")

_LAW = "Article 1\nThe operator inspects seals.\n\nArticle 2\nThe operator records results.\n"
_STANDARD = "STD-1 Seals are inspected quarterly.\nSTD-2 Results are recorded.\n"
_MANUAL = (
    "# Pumps\n\nInspect the pump seal.\n\n## Valves\n\nReplace worn valves.\n\n"
    "## Figure\n\nThe seal diagram shows the gasket.\n"
)
_LAW_META = {"publisher": "State", "jurisdiction": "EU", "publication_date": None}
SOURCES = (
    # (collection, key, kind, text, metadata); each kind's schema requires its keys.
    ("default", "law.txt", "law@1", _LAW, {**_LAW_META, "edition": "1", "effective_start": "2020-01-01", "effective_end": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "law.txt", "law@1", _LAW + "Article 3\nNew duty.\n", {**_LAW_META, "edition": "2", "effective_start": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "std.txt", "standard@1", _STANDARD, {"issuer": "ISO", "edition": "1", "publication_date": "2021-05-01", "approval": "accepted"}),
    ("default", "std.txt", "standard@1", _STANDARD + "STD-3 Added rule.\n", {"issuer": "ISO", "edition": "2", "publication_date": "2022-05-01"}),
    ("default", "manual.md", "manual@1", _MANUAL, {"publisher": "Acme", "edition": "1", "scope": {"project": "beta"}}),
    ("default", "undated.md", "manual@1", _MANUAL.replace("Pumps", "Fans"), {"publisher": "Acme", "edition": "1"}),
    ("second", "notes.md", "rationale@1", "# Why\n\nSeals fail early.\n", {"author": "Ops", "reference": "R-1", "scope": {"project": "alpha"}}),
    ("second", "guide.md", "guidance@1", "# Guide\n\nCheck seals.\n", {"publisher": "Acme", "edition": "1", "approval": "rejected"}),
    ("second", "manual.md", "manual@1", _MANUAL.replace("Valves", "Hoses"), {"publisher": "Acme", "edition": "1", "publication_date": "2024-02-01"}),
)  # fmt: skip
GLOBAL_KEY = "shared.md"
_GLOBAL_TEXT = "# Shared\n\nSeals apply to every project.\n"
_CODE = (
    "class Seal:\n    def inspect(self):\n        return 'seal'\n\n\n"
    "def record(result):\n    return result\n"
)


def global_metadata(edition: int, declared: bool) -> dict:
    """The shared source's metadata; ``declared`` binds project as a global facet."""
    scope = {"global_facets": ["scope.project"]} if declared else {"project": "gamma"}
    return {"publisher": "Acme", "edition": str(edition), "scope": scope}


def _config(root: Path):
    pack = root / "packs" / "equivalence"
    pack.mkdir(parents=True)
    (pack / "profile.toml").write_text(PROFILE)
    settings = root / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.equivalence]\npath="packs/equivalence"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ndimensions=2\n'
        '[configuration.index.text]\nbackend="chroma"\n'
        f'[configuration.acquisition]\nroots=["{root.resolve()}"]\n'
    )
    return resolve_config(settings, "equivalence", {})


def add_text(kb, collection_id: str, key: str, kind: str, text: str, metadata: dict):
    """Add one text or markdown source and wait until it is published."""
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": collection_id,
            "kind": kind,
            "format": "markdown" if key.endswith(".md") else "text",
            "text": text,
            "metadata": {"title": key, **metadata},
        }
    )
    status = kb.jobs.wait(job.id)
    assert status.state == "succeeded", (key, status)


def _add_code(kb, root: Path, collection_id: str) -> None:
    repo = make_repo(root)
    (repo / "seals.py").write_text(_CODE)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Seal code")
    job = kb.sources.add(
        {
            "type": "repository",
            "path": str(repo),
            "source_key": "code",
            "collection_id": collection_id,
            "kind": "code_file@1",
            "metadata": {"title": "Seal repository"},
        }
    )
    status = kb.jobs.wait(job.id, timeout=60)
    assert status.state == "succeeded", status.error_code


@dataclass(frozen=True)
class Figure:
    """The injected asset chain: a source unit, its asset and the asset's proxy."""

    revision: str
    source: str
    asset: str
    proxy: str
    declared: str


def _add_figure(owner, collection_id: str) -> Figure:
    """Give the default ``manual.md`` extraction an image asset and its proxy.

    Native markdown yields no assets, so the canonical rows a parsed figure
    would carry are written once at setup: the asset, its ``source`` link, a
    proxy over one existing unit, and their dependency sets (asset on its
    source unit, proxy on its asset). The proxy is declared ``needs_review``
    and another original is declared ``needs_review`` too, which splits the
    proxy and original review states. Every later change goes through policy
    and service writes.
    """
    rows = owner_rows(
        owner,
        "SELECT e.extraction_id,e.revision_id,r.original_sha FROM extractions e "
        "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
        "WHERE s.source_key='manual.md' AND s.collection_id=? ",
        (collection_id,),
    )
    ((extraction, revision, sha),) = rows
    source, proxy, declared = (
        row[0]
        for row in owner_rows(
            owner,
            "SELECT unit_id FROM evidence_units WHERE extraction_id=? AND citable "
            "ORDER BY ordinal LIMIT 3",
            (extraction,),
        )
    )
    asset = "asset-seal-diagram"
    locator = TextLocator(
        artifact_ref=sha, native_ref="figure:seal", char_start=0, char_end=0
    ).model_dump_json()

    def write() -> None:
        db = owner.store.db
        with owner.store.immediate_transaction():
            db.execute(
                "INSERT INTO assets(asset_id,extraction_id,artifact_sha,asset_type,"
                "native_ref,locator_json,declared_review_state) "
                "VALUES (?,?,?,'image','figure:seal',?,'unreviewed')",
                (asset, extraction, sha, locator),
            )
            db.execute(
                "INSERT INTO unit_assets(unit_id,asset_id,role) VALUES (?,?,'source')",
                (source, asset),
            )
            db.execute(
                "INSERT INTO asset_proxies(proxy_unit_id,asset_id,origin_type,"
                "derivation_sha) VALUES (?,?,'caption_copy',?)",
                (proxy, asset, sha),
            )
            db.executemany(
                "INSERT INTO representation_dependencies(owner_id,dependency_id) "
                "VALUES (?,?)",
                [
                    (asset, revision),
                    (asset, asset),
                    (asset, source),
                    (proxy, asset),
                    (proxy, source),
                ],
            )
            db.execute(
                "UPDATE evidence_units SET declared_review_state='needs_review' "
                "WHERE unit_id IN (?,?)",
                (proxy, declared),
            )

    on_owner(owner, write)
    return Figure(
        revision=revision, source=source, asset=asset, proxy=proxy, declared=declared
    )


@dataclass(frozen=True)
class Corpus:
    """An open workspace, its collections and the injected figure."""

    kb: Tychora
    collections: dict[str, str]
    figure: Figure


def build(root: Path) -> Corpus:
    """Open and fill the equivalence workspace; the caller closes ``kb``."""
    counter = RecordingCounter()
    bindings = ProviderBindings(
        local_text=LocalTextBinding(embedder=RecordingEmbedder(counter)),
        text_index=ChromaVectorIndex(root / "vectors"),
        text_counter=counter,
    )
    kb = Tychora.open(
        root / "knowledge",
        config=_config(root),
        providers=bindings,
        token_counter=OutputCounter(),
    )
    workspace = kb._workspace
    second = workspace.bridge.call(
        workspace.services.collections.create(
            CollectionCreate(title="Second", slug="second"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
    )
    collections = {
        "default": str(workspace.default_collection_id),
        "second": str(second.collection_id),
    }
    for collection, key, kind, text, metadata in SOURCES:
        add_text(kb, collections[collection], key, kind, text, metadata)
    add_text(
        kb,
        collections["second"],
        GLOBAL_KEY,
        "manual@1",
        _GLOBAL_TEXT,
        global_metadata(1, True),
    )
    _add_code(kb, root, collections["default"])
    return Corpus(kb, collections, _add_figure(workspace.owner, collections["default"]))


def snapshot_facts(tx, snapshot_id: str) -> dict:
    """Facts of every unit and asset, and of the snapshot's windows and records."""
    db = tx.eligibility._db

    def ids(sql: str, *args) -> tuple[str, ...]:
        return tuple(row[0] for row in db.execute(sql, args))

    owners = (
        *ids("SELECT unit_id FROM evidence_units"),
        *ids("SELECT asset_id FROM assets"),
        *ids(
            "SELECT r.chunk_id FROM snapshot_batches sb JOIN lexical_records r "
            "ON r.batch_id=sb.batch_id WHERE sb.snapshot_id=? AND sb.slot='lexical'",
            snapshot_id,
        ),
        *ids(
            "SELECT v.record_id FROM snapshot_batches sb JOIN vector_records v "
            "ON v.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
            "AND sb.slot IN ('text','image')",
            snapshot_id,
        ),
    )
    return {fact.owner_id: fact for fact in tx.eligibility.owner_facts(owners)}


def own_states(owner, figure: Figure) -> dict[str, str]:
    """The own review state of the figure's revision, units and asset."""
    rows = owner_rows(
        owner,
        "SELECT r.revision_id,COALESCE(rc.state,r.declared_review_state) FROM revisions r "
        "LEFT JOIN review_current rc ON rc.target_kind='revision' "
        "AND rc.target_id=r.revision_id WHERE r.revision_id=? UNION ALL "
        "SELECT u.unit_id,COALESCE(rc.state,u.declared_review_state) FROM evidence_units u "
        "LEFT JOIN review_current rc ON rc.target_kind='unit' AND rc.target_id=u.unit_id "
        "WHERE u.unit_id IN (SELECT value FROM json_each(?)) "
        "UNION ALL SELECT a.asset_id,COALESCE(rc.state,a.declared_review_state) "
        "FROM assets a LEFT JOIN review_current rc ON rc.target_kind='asset' "
        "AND rc.target_id=a.asset_id WHERE a.asset_id=?",
        (figure.revision, json.dumps([figure.source, figure.proxy]), figure.asset),
    )
    return dict(rows)
