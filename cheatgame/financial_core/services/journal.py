from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    FinancialAccount,
    FinancialAccountStatus,
    JournalEntry,
    JournalPosting,
    PostingDirection,
)
from cheatgame.financial_core.services.locks import LockRank, lock_many, ordered_lock_scope, register_lock


class UnbalancedJournalEntry(ValidationError):
    pass


def _validate_posting_specs(postings):
    if len(postings) < 2:
        raise UnbalancedJournalEntry("A journal entry requires at least two postings.")
    totals = defaultdict(lambda: {PostingDirection.DEBIT: Decimal("0"), PostingDirection.CREDIT: Decimal("0")})
    for posting in postings:
        amount = Decimal(str(posting["amount"]))
        direction = posting["direction"]
        currency = str(posting["currency"]).upper()
        if amount <= 0:
            raise UnbalancedJournalEntry("Posting amounts must be positive.")
        if direction not in PostingDirection.values:
            raise UnbalancedJournalEntry("Posting direction is invalid.")
        if currency != CANONICAL_CURRENCY:
            raise UnbalancedJournalEntry(
                "C1 journals accept canonical IRR only; the legacy IRT compatibility bridge is not implemented."
            )
        totals[currency][direction] += amount
    for currency, sides in totals.items():
        if sides[PostingDirection.DEBIT] != sides[PostingDirection.CREDIT]:
            raise UnbalancedJournalEntry(f"Journal entry is not balanced for {currency}.")


def post_balanced_journal_entry(
    *,
    source_type,
    source_id,
    idempotency_key,
    postings,
    correlation_id=None,
    occurred_at=None,
    description="",
):
    with transaction.atomic(), ordered_lock_scope():
        return post_balanced_journal_entry_under_lock(
            source_type=source_type,
            source_id=source_id,
            idempotency_key=idempotency_key,
            postings=postings,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            description=description,
        )


def post_balanced_journal_entry_under_lock(
    *,
    source_type,
    source_id,
    idempotency_key,
    postings,
    correlation_id=None,
    occurred_at=None,
    description="",
):
    """Post inside an existing atomic ordered-lock scope; never performs external I/O."""
    posting_specs = list(postings)
    _validate_posting_specs(posting_specs)
    existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.source_type != source_type or existing.source_id != str(source_id):
            raise ValidationError("Journal idempotency key conflicts with another source.")
        return existing

    account_ids = [int(spec["account_id"]) for spec in posting_specs]
    accounts = lock_many(
        queryset=FinancialAccount.objects.all(),
        rank=LockRank.JOURNAL_ACCOUNT,
        pks=account_ids,
    )
    account_by_id = {account.pk: account for account in accounts}
    for spec in posting_specs:
        account = account_by_id[int(spec["account_id"])]
        if account.status != FinancialAccountStatus.ACTIVE:
            raise ValidationError("Journal posting requires an active account.")
        if account.currency != str(spec["currency"]).upper():
            raise ValidationError("Posting currency must match its account.")

    register_lock(LockRank.JOURNAL_RECORD, str(idempotency_key))
    entry_kwargs = {
        "source_type": source_type,
        "source_id": str(source_id),
        "idempotency_key": idempotency_key,
        "description": str(description)[:500],
    }
    if correlation_id is not None:
        entry_kwargs["correlation_id"] = correlation_id
    if occurred_at is not None:
        entry_kwargs["occurred_at"] = occurred_at
    entry = JournalEntry.objects.create(**entry_kwargs)
    for line_number, spec in enumerate(posting_specs, start=1):
        JournalPosting.objects.create(
            entry=entry,
            line_number=line_number,
            account=account_by_id[int(spec["account_id"])],
            direction=spec["direction"],
            amount=Decimal(str(spec["amount"])),
            currency=str(spec["currency"]).upper(),
            memo=str(spec.get("memo", ""))[:500],
        )
    return entry
