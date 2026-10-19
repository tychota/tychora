"""Compute the next membership page of one bucket for one incoming member."""

from collections.abc import Mapping

from tychora.contracts.snapshots import (
    MembershipPage,
    PageMember,
    bucket_of,
    compute_page_id,
)


def _kept(
    member: PageMember,
    incoming: PageMember,
    retained_editions: Mapping[str, str | None],
    incoming_edition: str | None,
) -> PageMember | None:
    """Return the base member as it stays in the next page, or None when dropped."""
    if member.source_id != incoming.source_id:
        return member
    if member.revision_id == incoming.revision_id:
        return None
    if retained_editions.get(member.revision_id) != incoming_edition:
        return member.model_copy(update={"is_default": False})
    return None


def next_page(
    base: MembershipPage | None,
    incoming: PageMember,
    retained_editions: Mapping[str, str | None],
    incoming_edition: str | None,
) -> MembershipPage:
    """Keep the bucket's other members; supersede or keep earlier editions."""
    bucket = bucket_of(incoming.source_id)
    members = [
        kept
        for member in (base.members if base is not None else ())
        if (kept := _kept(member, incoming, retained_editions, incoming_edition))
        is not None
    ]
    members.append(incoming.model_copy(update={"is_default": True}))
    ordered = tuple(sorted(members, key=lambda m: (m.source_id, m.revision_id)))
    return MembershipPage(
        bucket=bucket, members=ordered, page_id=compute_page_id(bucket, ordered)
    )


def member_count_after(
    base_count: int, base: MembershipPage | None, page: MembershipPage
) -> int:
    """Return the snapshot member count once ``page`` replaces ``base``."""
    return (
        base_count - (len(base.members) if base is not None else 0) + len(page.members)
    )
