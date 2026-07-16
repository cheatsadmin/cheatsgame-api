from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from cheatgame.common.models import BaseModel


CANONICAL_CURRENCY = "IRR"


class PaymentCollectionStatus(models.TextChoices):
    OPEN = "open", "OPEN"
    PROCESSING = "processing", "PROCESSING"
    PARTIALLY_PAID = "partially_paid", "PARTIALLY_PAID"
    PAID_PENDING_FINALIZATION = "paid_pending_finalization", "PAID_PENDING_FINALIZATION"
    PAID = "paid", "PAID"
    REVIEW = "review", "REVIEW"
    CANCELED = "canceled", "CANCELED"


class PaymentRefundStatus(models.TextChoices):
    NOT_REFUNDED = "not_refunded", "NOT_REFUNDED"
    PARTIALLY_REFUNDED = "partially_refunded", "PARTIALLY_REFUNDED"
    REFUNDED = "refunded", "REFUNDED"
    REFUND_REVIEW = "refund_review", "REFUND_REVIEW"


class PaymentAttemptStatus(models.TextChoices):
    CREATED = "created", "CREATED"
    REQUIRES_CUSTOMER_ACTION = "requires_customer_action", "REQUIRES_CUSTOMER_ACTION"
    PROCESSING = "processing", "PROCESSING"
    SUCCEEDED = "succeeded", "SUCCEEDED"
    DEFINITIVE_FAILED = "definitive_failed", "DEFINITIVE_FAILED"
    OUTCOME_UNKNOWN = "outcome_unknown", "OUTCOME_UNKNOWN"
    REVIEW = "review", "REVIEW"


class PaymentTenderType(models.TextChoices):
    EXTERNAL_PROVIDER = "external_provider", "EXTERNAL_PROVIDER"
    GIFT_CARD = "gift_card", "GIFT_CARD"
    INSTALLMENT = "installment", "INSTALLMENT"
    INTERNAL_ADJUSTMENT = "internal_adjustment", "INTERNAL_ADJUSTMENT"


class PaymentTransactionOperation(models.TextChoices):
    SALE = "sale", "SALE"
    AUTHORIZE = "authorize", "AUTHORIZE"
    CAPTURE = "capture", "CAPTURE"
    VOID = "void", "VOID"
    REFUND = "refund", "REFUND"
    CHARGEBACK = "chargeback", "CHARGEBACK"


class PaymentTransactionStatus(models.TextChoices):
    CREATED = "created", "CREATED"
    REQUESTING = "requesting", "REQUESTING"
    PENDING_CUSTOMER = "pending_customer", "PENDING_CUSTOMER"
    PENDING_PROVIDER = "pending_provider", "PENDING_PROVIDER"
    CALLBACK_RECEIVED = "callback_received", "CALLBACK_RECEIVED"
    VERIFYING = "verifying", "VERIFYING"
    SUCCEEDED = "succeeded", "SUCCEEDED"
    DECLINED = "declined", "DECLINED"
    CANCELED = "canceled", "CANCELED"
    EXPIRED = "expired", "EXPIRED"
    OUTCOME_UNKNOWN = "outcome_unknown", "OUTCOME_UNKNOWN"
    REVIEW = "review", "REVIEW"


class FinancialAccountType(models.TextChoices):
    ASSET = "asset", "ASSET"
    LIABILITY = "liability", "LIABILITY"
    EQUITY = "equity", "EQUITY"
    REVENUE = "revenue", "REVENUE"
    EXPENSE = "expense", "EXPENSE"


class FinancialAccountStatus(models.TextChoices):
    ACTIVE = "active", "ACTIVE"
    FROZEN = "frozen", "FROZEN"
    CLOSED = "closed", "CLOSED"


class PostingDirection(models.TextChoices):
    DEBIT = "debit", "DEBIT"
    CREDIT = "credit", "CREDIT"


class FinancialActorType(models.TextChoices):
    CUSTOMER = "customer", "CUSTOMER"
    SYSTEM = "system", "SYSTEM"
    PROVIDER = "provider", "PROVIDER"
    ADMIN = "admin", "ADMIN"
    SUPPORT = "support", "SUPPORT"
    RECONCILIATION = "reconciliation", "RECONCILIATION"


class ReviewCaseStatus(models.TextChoices):
    OPEN = "open", "OPEN"
    INVESTIGATING = "investigating", "INVESTIGATING"
    APPROVAL_PENDING = "approval_pending", "APPROVAL_PENDING"
    RESOLVED = "resolved", "RESOLVED"
    CANCELED = "canceled", "CANCELED"


class ReviewCaseSeverity(models.TextChoices):
    LOW = "low", "LOW"
    MEDIUM = "medium", "MEDIUM"
    HIGH = "high", "HIGH"
    CRITICAL = "critical", "CRITICAL"


class ReviewCaseReason(models.TextChoices):
    PROVIDER_STATE_UNCLEAR = "provider_state_unclear", "PROVIDER_STATE_UNCLEAR"
    PAID_FINALIZATION_PENDING = "paid_finalization_pending", "PAID_FINALIZATION_PENDING"
    AMOUNT_MISMATCH = "amount_mismatch", "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "currency_mismatch", "CURRENCY_MISMATCH"
    DUPLICATE_PROVIDER_REFERENCE = "duplicate_provider_reference", "DUPLICATE_PROVIDER_REFERENCE"
    LATE_PAYMENT = "late_payment", "LATE_PAYMENT"
    INVENTORY_CONFLICT = "inventory_conflict", "INVENTORY_CONFLICT"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch", "RECONCILIATION_MISMATCH"
    FRAUD_RISK = "fraud_risk", "FRAUD_RISK"
    INVARIANT_VIOLATION = "invariant_violation", "INVARIANT_VIOLATION"


class IdempotencyStatus(models.TextChoices):
    IN_PROGRESS = "in_progress", "IN_PROGRESS"
    COMPLETED = "completed", "COMPLETED"
    FAILED = "failed", "FAILED"


class ReconciliationRunStatus(models.TextChoices):
    CREATED = "created", "CREATED"
    RUNNING = "running", "RUNNING"
    COMPLETED = "completed", "COMPLETED"
    FAILED = "failed", "FAILED"


class ReconciliationFindingStatus(models.TextChoices):
    OPEN = "open", "OPEN"
    REVIEWING = "reviewing", "REVIEWING"
    RESOLVED = "resolved", "RESOLVED"
    ACCEPTED = "accepted", "ACCEPTED"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Append-only financial records cannot be updated.")

    def delete(self):
        raise ValidationError("Append-only financial records cannot be deleted.")


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Append-only financial records cannot be updated.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Append-only financial records cannot be deleted.")


class Payment(BaseModel):
    IMMUTABLE_FIELDS = ("public_id", "order_id", "amount_due", "currency")

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    order = models.OneToOneField(
        "shop.Order",
        on_delete=models.PROTECT,
        related_name="financial_payment",
    )
    amount_due = models.DecimalField(max_digits=20, decimal_places=0)
    confirmed_amount = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    refunded_amount = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    collection_status = models.CharField(
        max_length=32,
        choices=PaymentCollectionStatus.choices,
        default=PaymentCollectionStatus.OPEN,
        db_index=True,
    )
    refund_status = models.CharField(
        max_length=24,
        choices=PaymentRefundStatus.choices,
        default=PaymentRefundStatus.NOT_REFUNDED,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=("collection_status", "created_at"), name="fin_pay_collect_created"),
            models.Index(fields=("refund_status", "created_at"), name="fin_pay_refund_created"),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount_due__gt=0), name="fin_payment_amount_due_gt_zero"),
            models.CheckConstraint(check=Q(confirmed_amount__gte=0), name="fin_payment_confirmed_gte_zero"),
            models.CheckConstraint(check=Q(refunded_amount__gte=0), name="fin_payment_refunded_gte_zero"),
            models.CheckConstraint(
                check=Q(confirmed_amount__lte=F("amount_due")),
                name="fin_payment_confirmed_lte_due",
            ),
            models.CheckConstraint(
                check=Q(refunded_amount__lte=F("confirmed_amount")),
                name="fin_payment_refunded_lte_confirmed",
            ),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_payment_currency_irr"),
            models.CheckConstraint(
                check=Q(collection_status__in=PaymentCollectionStatus.values),
                name="fin_payment_collection_status_valid",
            ),
            models.CheckConstraint(
                check=Q(refund_status__in=PaymentRefundStatus.values),
                name="fin_payment_refund_status_valid",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(
                        collection_status__in=(
                            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
                            PaymentCollectionStatus.PAID,
                        )
                    )
                    | Q(confirmed_amount=F("amount_due"))
                ),
                name="fin_payment_paid_amount_complete",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("Payment obligation identity and amount are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class PaymentAttempt(BaseModel):
    IMMUTABLE_FIELDS = (
        "public_id",
        "payment_id",
        "sequence",
        "requested_amount",
        "currency",
        "tender_type",
        "provider",
        "merchant_account_ref",
        "idempotency_key",
        "request_hash",
    )

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="attempts")
    sequence = models.PositiveIntegerField()
    requested_amount = models.DecimalField(max_digits=20, decimal_places=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    tender_type = models.CharField(max_length=32, choices=PaymentTenderType.choices)
    provider = models.CharField(max_length=64, blank=True)
    merchant_account_ref = models.CharField(max_length=128, blank=True)
    status = models.CharField(
        max_length=32,
        choices=PaymentAttemptStatus.choices,
        default=PaymentAttemptStatus.CREATED,
        db_index=True,
    )
    idempotency_key = models.UUIDField(unique=True, editable=False)
    request_hash = models.CharField(max_length=64)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("payment", "sequence"), name="fin_attempt_payment_sequence_uniq"),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_attempt_sequence_gt_zero"),
            models.CheckConstraint(check=Q(requested_amount__gt=0), name="fin_attempt_amount_gt_zero"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_attempt_currency_irr"),
            models.CheckConstraint(check=~Q(request_hash=""), name="fin_attempt_hash_nonempty"),
            models.CheckConstraint(
                check=Q(tender_type__in=PaymentTenderType.values), name="fin_attempt_tender_valid"
            ),
            models.CheckConstraint(
                check=Q(status__in=PaymentAttemptStatus.values), name="fin_attempt_status_valid"
            ),
        ]
        indexes = [models.Index(fields=("payment", "status"), name="fin_attempt_payment_status")]

    def clean(self):
        super().clean()
        if self.payment_id:
            payment = self.payment
            if self.currency != payment.currency:
                raise ValidationError({"currency": "Attempt currency must match Payment currency."})
            if self.requested_amount > payment.amount_due:
                raise ValidationError({"requested_amount": "Attempt amount cannot exceed amount due."})
        if self.tender_type in (PaymentTenderType.EXTERNAL_PROVIDER, PaymentTenderType.INSTALLMENT):
            if not self.provider or not self.merchant_account_ref:
                raise ValidationError("External and installment attempts require provider and merchant account.")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("PaymentAttempt identity and requested terms are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class PaymentTransaction(BaseModel):
    IMMUTABLE_FIELDS = (
        "public_id",
        "attempt_id",
        "sequence",
        "operation_type",
        "parent_id",
        "provider",
        "merchant_account_ref",
        "merchant_reference",
        "amount",
        "currency",
        "provider_amount",
        "provider_unit",
        "idempotency_key",
    )

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT, related_name="transactions")
    sequence = models.PositiveIntegerField()
    operation_type = models.CharField(max_length=16, choices=PaymentTransactionOperation.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="child_transactions",
    )
    provider = models.CharField(max_length=64)
    merchant_account_ref = models.CharField(max_length=128)
    merchant_reference = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=20, decimal_places=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    provider_amount = models.DecimalField(max_digits=20, decimal_places=0)
    provider_unit = models.CharField(max_length=16)
    status = models.CharField(
        max_length=32,
        choices=PaymentTransactionStatus.choices,
        default=PaymentTransactionStatus.CREATED,
        db_index=True,
    )
    provider_authority = models.CharField(max_length=128, null=True, blank=True)
    provider_reference = models.CharField(max_length=128, null=True, blank=True)
    evidence_hash = models.CharField(max_length=64, blank=True)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("attempt", "sequence"), name="fin_tx_attempt_sequence_uniq"),
            models.UniqueConstraint(
                fields=("provider", "merchant_account_ref", "merchant_reference"),
                name="fin_tx_merchant_reference_uniq",
            ),
            models.UniqueConstraint(
                fields=("provider", "merchant_account_ref", "provider_authority"),
                condition=Q(provider_authority__isnull=False),
                name="fin_tx_provider_authority_uniq",
            ),
            models.UniqueConstraint(
                fields=("provider", "merchant_account_ref", "provider_reference"),
                condition=Q(provider_reference__isnull=False),
                name="fin_tx_provider_reference_uniq",
            ),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_tx_sequence_gt_zero"),
            models.CheckConstraint(check=Q(amount__gt=0), name="fin_tx_amount_gt_zero"),
            models.CheckConstraint(check=Q(provider_amount__gt=0), name="fin_tx_provider_amount_gt_zero"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_tx_currency_irr"),
            models.CheckConstraint(check=Q(provider_unit=CANONICAL_CURRENCY), name="fin_tx_provider_unit_irr"),
            models.CheckConstraint(check=~Q(provider=""), name="fin_tx_provider_nonempty"),
            models.CheckConstraint(check=~Q(merchant_reference=""), name="fin_tx_merchant_ref_nonempty"),
            models.CheckConstraint(
                check=Q(operation_type__in=PaymentTransactionOperation.values), name="fin_tx_operation_valid"
            ),
            models.CheckConstraint(
                check=Q(status__in=PaymentTransactionStatus.values), name="fin_tx_status_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status__in=(
                            PaymentTransactionStatus.SUCCEEDED,
                            PaymentTransactionStatus.DECLINED,
                            PaymentTransactionStatus.CANCELED,
                            PaymentTransactionStatus.EXPIRED,
                        ),
                        completed_at__isnull=False,
                    )
                    | Q(
                        status__in=(
                            PaymentTransactionStatus.CREATED,
                            PaymentTransactionStatus.REQUESTING,
                            PaymentTransactionStatus.PENDING_CUSTOMER,
                            PaymentTransactionStatus.PENDING_PROVIDER,
                            PaymentTransactionStatus.CALLBACK_RECEIVED,
                            PaymentTransactionStatus.VERIFYING,
                            PaymentTransactionStatus.OUTCOME_UNKNOWN,
                            PaymentTransactionStatus.REVIEW,
                        ),
                        completed_at__isnull=True,
                    )
                ),
                name="fin_tx_completed_at_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=("attempt", "status"), name="fin_tx_attempt_status"),
            models.Index(fields=("provider", "status", "created_at"), name="fin_tx_provider_status_time"),
        ]

    def clean(self):
        super().clean()
        if self.attempt_id:
            if self.provider != self.attempt.provider:
                raise ValidationError({"provider": "Transaction provider must match PaymentAttempt."})
            if self.merchant_account_ref != self.attempt.merchant_account_ref:
                raise ValidationError({"merchant_account_ref": "Merchant account must match PaymentAttempt."})
            if self.currency != self.attempt.currency:
                raise ValidationError({"currency": "Transaction currency must match PaymentAttempt."})
            if self.amount > self.attempt.requested_amount:
                raise ValidationError({"amount": "Transaction amount cannot exceed attempt amount."})
        if self.parent_id:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "Transaction cannot parent itself."})
            if self.parent.attempt_id != self.attempt_id:
                raise ValidationError({"parent": "Parent transaction must belong to the same attempt."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = (
                type(self).objects.filter(pk=self.pk)
                .values(
                    *self.IMMUTABLE_FIELDS,
                    "provider_authority",
                    "provider_reference",
                    "evidence_hash",
                    "completed_at",
                )
                .first()
            )
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("PaymentTransaction provider identity and money terms are immutable.")
            for field in ("provider_authority", "provider_reference", "evidence_hash", "completed_at"):
                if original and original[field] not in (None, "") and original[field] != getattr(self, field):
                    raise ValidationError({field: "Provider evidence fields are write-once."})
        self.full_clean()
        return super().save(*args, **kwargs)


class FinancialAccount(BaseModel):
    key = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=16, choices=FinancialAccountType.choices)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    status = models.CharField(
        max_length=16,
        choices=FinancialAccountStatus.choices,
        default=FinancialAccountStatus.ACTIVE,
        db_index=True,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(check=~Q(key=""), name="fin_account_key_nonempty"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_account_currency_irr"),
            models.CheckConstraint(
                check=Q(account_type__in=FinancialAccountType.values), name="fin_account_type_valid"
            ),
            models.CheckConstraint(
                check=Q(status__in=FinancialAccountStatus.values), name="fin_account_status_valid"
            ),
        ]
        indexes = [models.Index(fields=("account_type", "currency"), name="fin_account_type_currency")]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values("key", "account_type", "currency").first()
            if original and any(original[field] != getattr(self, field) for field in original):
                raise ValidationError("Financial account identity is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class JournalEntry(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    source_type = models.CharField(max_length=64)
    source_id = models.CharField(max_length=128)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    correlation_id = models.UUIDField(default=uuid4, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    posted_at = models.DateTimeField(default=timezone.now, db_index=True)
    description = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source_type", "source_id"), name="fin_journal_source_uniq"),
            models.CheckConstraint(check=~Q(source_type=""), name="fin_journal_source_type_nonempty"),
            models.CheckConstraint(check=~Q(source_id=""), name="fin_journal_source_id_nonempty"),
        ]
        indexes = [models.Index(fields=("source_type", "occurred_at"), name="fin_journal_source_time")]


class JournalPosting(AppendOnlyModel):
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="postings")
    line_number = models.PositiveIntegerField()
    account = models.ForeignKey(FinancialAccount, on_delete=models.PROTECT, related_name="postings")
    direction = models.CharField(max_length=8, choices=PostingDirection.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    memo = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("entry", "line_number"), name="fin_posting_entry_line_uniq"),
            models.CheckConstraint(check=Q(line_number__gt=0), name="fin_posting_line_gt_zero"),
            models.CheckConstraint(check=Q(amount__gt=0), name="fin_posting_amount_gt_zero"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_posting_currency_irr"),
            models.CheckConstraint(
                check=Q(direction__in=PostingDirection.values), name="fin_posting_direction_valid"
            ),
        ]
        indexes = [models.Index(fields=("account", "created_at"), name="fin_posting_account_time")]

    def clean(self):
        super().clean()
        if self.account_id and self.currency != self.account.currency:
            raise ValidationError({"currency": "Posting currency must match account currency."})


class ReviewCase(BaseModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    reason = models.CharField(max_length=48, choices=ReviewCaseReason.choices, db_index=True)
    severity = models.CharField(max_length=16, choices=ReviewCaseSeverity.choices, db_index=True)
    status = models.CharField(
        max_length=24,
        choices=ReviewCaseStatus.choices,
        default=ReviewCaseStatus.OPEN,
        db_index=True,
    )
    order = models.ForeignKey(
        "shop.Order", on_delete=models.PROTECT, null=True, blank=True, related_name="financial_review_cases"
    )
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="review_cases"
    )
    attempt = models.ForeignKey(
        PaymentAttempt, on_delete=models.PROTECT, null=True, blank=True, related_name="review_cases"
    )
    transaction = models.ForeignKey(
        PaymentTransaction, on_delete=models.PROTECT, null=True, blank=True, related_name="review_cases"
    )
    opened_by_type = models.CharField(max_length=20, choices=FinancialActorType.choices)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opened_financial_review_cases",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_financial_review_cases",
    )
    summary = models.CharField(max_length=1000)
    resolution_code = models.CharField(max_length=64, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(order__isnull=False)
                    | Q(payment__isnull=False)
                    | Q(attempt__isnull=False)
                    | Q(transaction__isnull=False)
                ),
                name="fin_review_aggregate_required",
            ),
            models.CheckConstraint(check=~Q(summary=""), name="fin_review_summary_nonempty"),
            models.CheckConstraint(check=Q(reason__in=ReviewCaseReason.values), name="fin_review_reason_valid"),
            models.CheckConstraint(
                check=Q(severity__in=ReviewCaseSeverity.values), name="fin_review_severity_valid"
            ),
            models.CheckConstraint(check=Q(status__in=ReviewCaseStatus.values), name="fin_review_status_valid"),
            models.CheckConstraint(
                check=Q(opened_by_type__in=FinancialActorType.values), name="fin_review_actor_type_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status=ReviewCaseStatus.RESOLVED,
                        resolved_at__isnull=False,
                    )
                    & ~Q(resolution_code="")
                    | (
                        ~Q(status=ReviewCaseStatus.RESOLVED)
                        & Q(resolved_at__isnull=True, resolution_code="")
                    )
                ),
                name="fin_review_resolution_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "severity", "created_at"), name="fin_review_queue"),
            models.Index(fields=("assigned_to", "status"), name="fin_review_assignee_status"),
        ]

    def clean(self):
        super().clean()
        if self.attempt_id and self.payment_id and self.attempt.payment_id != self.payment_id:
            raise ValidationError({"attempt": "Review attempt must belong to Review payment."})
        if self.transaction_id and self.attempt_id and self.transaction.attempt_id != self.attempt_id:
            raise ValidationError({"transaction": "Review transaction must belong to Review attempt."})
        if self.payment_id and self.order_id and self.payment.order_id != self.order_id:
            raise ValidationError({"payment": "Review payment must belong to Review order."})
        is_resolved = self.status == ReviewCaseStatus.RESOLVED
        if is_resolved != bool(self.resolved_at and self.resolution_code):
            raise ValidationError("Resolved ReviewCase requires resolution code and timestamp only when resolved.")

    def save(self, *args, **kwargs):
        if self.pk:
            immutable_fields = (
                "public_id",
                "reason",
                "order_id",
                "payment_id",
                "attempt_id",
                "transaction_id",
                "opened_by_type",
                "opened_by_id",
                "idempotency_key",
            )
            original = type(self).objects.filter(pk=self.pk).values(*immutable_fields).first()
            if original and any(original[field] != getattr(self, field) for field in immutable_fields):
                raise ValidationError("ReviewCase identity and aggregate ownership are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class ReviewAction(AppendOnlyModel):
    review_case = models.ForeignKey(ReviewCase, on_delete=models.PROTECT, related_name="actions")
    action_type = models.CharField(max_length=64)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="financial_review_actions"
    )
    reason_code = models.CharField(max_length=64)
    note = models.CharField(max_length=1000, blank=True)
    requires_approval = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_financial_review_actions",
    )
    idempotency_key = models.UUIDField(unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=~Q(action_type=""), name="fin_review_action_nonempty"),
            models.CheckConstraint(check=~Q(reason_code=""), name="fin_review_reason_nonempty"),
            models.CheckConstraint(
                check=Q(requires_approval=False, approved_by__isnull=True) | Q(requires_approval=True),
                name="fin_review_approval_consistent",
            ),
            models.CheckConstraint(
                check=Q(approved_by__isnull=True) | ~Q(approved_by=F("actor")),
                name="fin_review_maker_checker_distinct",
            ),
        ]

    def clean(self):
        super().clean()
        if self.approved_by_id and self.approved_by_id == self.actor_id:
            raise ValidationError({"approved_by": "Maker and checker must be different users."})


class FinancialEvent(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=128)
    aggregate_version = models.PositiveIntegerField()
    event_type = models.CharField(max_length=128, db_index=True)
    actor_type = models.CharField(max_length=20, choices=FinancialActorType.choices)
    actor_id = models.PositiveBigIntegerField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=200, unique=True)
    correlation_id = models.UUIDField(default=uuid4, db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("aggregate_type", "aggregate_id", "aggregate_version"),
                name="fin_event_aggregate_version_uniq",
            ),
            models.CheckConstraint(check=Q(aggregate_version__gt=0), name="fin_event_version_gt_zero"),
            models.CheckConstraint(check=~Q(aggregate_type=""), name="fin_event_aggregate_nonempty"),
            models.CheckConstraint(check=~Q(aggregate_id=""), name="fin_event_id_nonempty"),
            models.CheckConstraint(check=~Q(event_type=""), name="fin_event_type_nonempty"),
            models.CheckConstraint(
                check=Q(actor_type__in=FinancialActorType.values), name="fin_event_actor_type_valid"
            ),
        ]
        indexes = [
            models.Index(fields=("aggregate_type", "aggregate_id", "created_at"), name="fin_event_timeline"),
        ]


class IdempotencyRecord(BaseModel):
    scope = models.CharField(max_length=200)
    key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=IdempotencyStatus.choices,
        default=IdempotencyStatus.IN_PROGRESS,
        db_index=True,
    )
    result_type = models.CharField(max_length=64, blank=True)
    result_id = models.CharField(max_length=128, blank=True)
    safe_response = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("scope", "key"), name="fin_idempotency_scope_key_uniq"),
            models.CheckConstraint(check=~Q(scope=""), name="fin_idempotency_scope_nonempty"),
            models.CheckConstraint(check=~Q(key=""), name="fin_idempotency_key_nonempty"),
            models.CheckConstraint(check=~Q(request_hash=""), name="fin_idempotency_hash_nonempty"),
            models.CheckConstraint(
                check=Q(status__in=IdempotencyStatus.values), name="fin_idempotency_status_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(status=IdempotencyStatus.IN_PROGRESS, completed_at__isnull=True)
                    | Q(
                        status__in=(IdempotencyStatus.COMPLETED, IdempotencyStatus.FAILED),
                        completed_at__isnull=False,
                    )
                ),
                name="fin_idempotency_completed_at_consistent",
            ),
        ]
        indexes = [models.Index(fields=("status", "created_at"), name="fin_idempotency_status_time")]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values("scope", "key", "request_hash").first()
            if original and any(original[field] != getattr(self, field) for field in original):
                raise ValidationError("Idempotency command identity is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class ReconciliationRun(BaseModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    run_type = models.CharField(max_length=64)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=ReconciliationRunStatus.choices,
        default=ReconciliationRunStatus.CREATED,
        db_index=True,
    )
    idempotency_key = models.UUIDField(unique=True, editable=False)
    records_scanned = models.PositiveBigIntegerField(default=0)
    findings_count = models.PositiveBigIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(period_end__gt=F("period_start")), name="fin_recon_period_valid"),
            models.CheckConstraint(check=~Q(run_type=""), name="fin_recon_type_nonempty"),
            models.CheckConstraint(
                check=Q(status__in=ReconciliationRunStatus.values), name="fin_recon_run_status_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(status=ReconciliationRunStatus.CREATED, started_at__isnull=True, completed_at__isnull=True)
                    | Q(status=ReconciliationRunStatus.RUNNING, started_at__isnull=False, completed_at__isnull=True)
                    | Q(
                        status__in=(ReconciliationRunStatus.COMPLETED, ReconciliationRunStatus.FAILED),
                        started_at__isnull=False,
                        completed_at__isnull=False,
                    )
                ),
                name="fin_recon_run_times_consistent",
            ),
        ]
        indexes = [models.Index(fields=("run_type", "period_start"), name="fin_recon_type_period")]


class ReconciliationFinding(BaseModel):
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="findings")
    finding_key = models.CharField(max_length=200)
    finding_type = models.CharField(max_length=100, db_index=True)
    severity = models.CharField(max_length=16, choices=ReviewCaseSeverity.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=ReconciliationFindingStatus.choices,
        default=ReconciliationFindingStatus.OPEN,
        db_index=True,
    )
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="reconciliation_findings"
    )
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_findings",
    )
    review_case = models.ForeignKey(
        ReviewCase,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_findings",
    )
    expected = models.JSONField(default=dict, blank=True)
    actual = models.JSONField(default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("run", "finding_key"), name="fin_recon_finding_key_uniq"),
            models.CheckConstraint(check=~Q(finding_key=""), name="fin_recon_key_nonempty"),
            models.CheckConstraint(check=~Q(finding_type=""), name="fin_recon_finding_type_nonempty"),
            models.CheckConstraint(
                check=Q(severity__in=ReviewCaseSeverity.values), name="fin_recon_severity_valid"
            ),
            models.CheckConstraint(
                check=Q(status__in=ReconciliationFindingStatus.values), name="fin_recon_finding_status_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status__in=(
                            ReconciliationFindingStatus.RESOLVED,
                            ReconciliationFindingStatus.ACCEPTED,
                        ),
                        resolved_at__isnull=False,
                    )
                    | Q(
                        status__in=(
                            ReconciliationFindingStatus.OPEN,
                            ReconciliationFindingStatus.REVIEWING,
                        ),
                        resolved_at__isnull=True,
                    )
                ),
                name="fin_recon_finding_time_consistent",
            ),
        ]
        indexes = [models.Index(fields=("status", "severity", "created_at"), name="fin_recon_finding_queue")]
