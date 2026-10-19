"""Build a one-member lexical publication plan with production planning code."""

from io import BytesIO

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.identity import (
    canonical_bytes,
    digest,
    family_id,
    new_family_nonce,
)
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    LexicalRecord,
    NewBatch,
    PublicationPlan,
    snapshot_root_document,
)
from tychora.contracts.snapshots import (
    IndexFamily,
    PageMember,
    SlotFamily,
    SnapshotBinding,
    bucket_of,
    snapshot_id_v2,
)
from tychora.indexing.planning import member_count_after, next_page
from tychora.ports.artifacts import Artifacts


def lexical_family(
    store: CorpusStore, bundle: ExtractionBundle, config_sha: str
) -> IndexFamily:
    """Return the configuration's lexical family, creating it once."""
    with store.uow.read() as tx:
        nonce = tx.families.nonce("lexical", config_sha) or new_family_nonce()
    family = IndexFamily(
        family_id=family_id(bundle.workspace.workspace_id, config_sha, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha=config_sha,
        config_json=FrozenJsonMap(
            {"tokenizer": "unicode61", "effective_config_sha": config_sha}
        ),
        backend_namespace="lexical",
        created_at=bundle.workspace.created_at,
    )
    with store.uow.write() as tx:
        tx.families.create(family, nonce, config_sha)
    return family


def lexical_batch(
    store: CorpusStore,
    artifacts: Artifacts,
    bundle: ExtractionBundle,
    family: IndexFamily,
) -> NewBatch:
    """Write the extraction's one-chunk lexical batch and mark it ready."""
    entries = (bundle.chunks[0].chunk_id,)
    manifest = artifacts.put_stream(
        BytesIO(
            canonical_bytes(
                {
                    "type": "index_input",
                    "schema_version": "1",
                    "data": {"entries": entries},
                }
            )
        ),
        "application/json",
    )
    batch = NewBatch(
        slot="lexical",
        batch_id=digest(
            "index-batch-v1",
            family.family_id,
            bundle.extraction.extraction_id,
            manifest.sha256,
        ),
        family_id=family.family_id,
        extraction_id=bundle.extraction.extraction_id,
        input_manifest=manifest,
        expected_count=1,
        receipt=LexicalBatchReceipt(
            family_id=family.family_id,
            count=1,
            entry_ids_sha=digest("index-entries-v1", entries),
        ),
    )
    with store.uow.write() as tx:
        stored = tx.batches.get(batch.batch_id)
        if stored is None or stored.state != "ready":
            tx.batches.begin(batch)
            tx.batches.write_lexical(
                batch.batch_id,
                (LexicalRecord(chunk_id=entries[0], body=bundle.chunks[0].text),),
            )
            tx.batches.mark_ready(batch.batch_id, 1)
    return batch


def lexical_plan(
    store: CorpusStore,
    bundle: ExtractionBundle,
    job_id: str,
    config_sha: str,
    base: str | None,
) -> PublicationPlan:
    """Plan publishing ``bundle``'s lexical batch over ``base`` for one job."""
    artifacts = store.artifacts
    family = lexical_family(store, bundle, config_sha)
    batch = lexical_batch(store, artifacts, bundle, family)
    view = store.resolve_snapshot(base) if base else None
    base_page = None
    if base is not None:
        with store.uow.read() as tx:
            base_page = tx.snapshots.page(base, bucket_of(bundle.source.source_id))
    member = PageMember(
        source_id=bundle.source.source_id,
        revision_id=bundle.revision.revision_id,
        extraction_id=bundle.extraction.extraction_id,
        batches=(batch.page_batch(),),
    )
    editions = {
        m.revision_id: bundle.revision.edition_key
        for m in (base_page.members if base_page else ())
        if m.source_id == member.source_id
    }
    page = next_page(base_page, member, editions, bundle.revision.edition_key)
    replaced = base_page.page_id if base_page else None
    reused = tuple(p for p in (view.page_ids if view else ()) if p != replaced)
    manifests = {}
    if reused:
        with store.uow.read() as tx:
            manifests = dict(tx.snapshots.page_manifests(reused))
    manifests[page.page_id] = artifacts.put_stream(
        BytesIO(page.manifest_bytes()), "application/json"
    )
    families = (SlotFamily(slot="lexical", family=family),)
    binding = SnapshotBinding(
        bundle.workspace.workspace_id, base, job_id, config_sha, families
    )
    draft = PublicationPlan(
        job_id=job_id,
        snapshot_id=snapshot_id_v2(binding, (*reused, page.page_id)),
        workspace_id=bundle.workspace.workspace_id,
        base_snapshot_id=base,
        config_sha=config_sha,
        families=families,
        member_count=member_count_after(
            view.member_count if view else 0, base_page, page
        ),
        new_pages=(page,),
        reused_page_ids=reused,
        new_batches=(batch,),
        manifest=batch.input_manifest,
    )
    root = artifacts.put_stream(
        BytesIO(canonical_bytes(snapshot_root_document(draft, manifests))),
        "application/json",
    )
    return draft.model_copy(update={"manifest": root})
