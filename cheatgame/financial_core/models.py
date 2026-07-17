from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from cheatgame.common.models import BaseModel


CANONICAL_CURRENCY = "IRR"


class MoneyUnit(models.TextChoices):
    IRR = "IRR", "IRR"
    IRT = "IRT", "IRT"


class PaymentObligationSourceKind(models.TextChoices):
    CHECKOUT_PLACEMENT = "checkout_placement", "CHECKOUT_PLACEMENT"
    LEGACY_ORDER_ADOPTION = "legacy_order_adoption", "LEGACY_ORDER_ADOPTION"


class CallbackAuthenticationStrength(models.TextChoices):
    NONE = "none", "NONE"
    SHARED_SECRET = "shared_secret", "SHARED_SECRET"
    ASYMMETRIC_SIGNATURE = "asymmetric_signature", "ASYMMETRIC_SIGNATURE"
    MTLS = "mtls", "MTLS"


class ProviderVerificationSemantics(models.TextChoices):
    REQUIRED = "required", "REQUIRED"
    REQUEST_RESPONSE_FINAL = "request_response_final", "REQUEST_RESPONSE_FINAL"


class ProviderRequestOutcome(models.TextChoices):
    CUSTOMER_ACTION_REQUIRED = "customer_action_required", "CUSTOMER_ACTION_REQUIRED"
    ACCEPTED_PENDING = "accepted_pending", "ACCEPTED_PENDING"
    CONFIRMED_SUCCESS = "confirmed_success", "CONFIRMED_SUCCESS"
    CONFIRMED_DECLINE = "confirmed_decline", "CONFIRMED_DECLINE"
    CONFIRMED_CANCELED = "confirmed_canceled", "CONFIRMED_CANCELED"
    CONFIRMED_EXPIRED = "confirmed_expired", "CONFIRMED_EXPIRED"
    NO_EFFECT_RETRYABLE = "no_effect_retryable", "NO_EFFECT_RETRYABLE"
    OUTCOME_UNKNOWN = "outcome_unknown", "OUTCOME_UNKNOWN"
    SECURITY_FAILURE = "security_failure", "SECURITY_FAILURE"
    CONFIGURATION_FAILURE = "configuration_failure", "CONFIGURATION_FAILURE"
    PROTOCOL_FAILURE = "protocol_failure", "PROTOCOL_FAILURE"


class CallbackAuthenticationStatus(models.TextChoices):
    AUTHENTICATED = "authenticated", "AUTHENTICATED"
    UNAUTHENTICATED_HINT = "unauthenticated_hint", "UNAUTHENTICATED_HINT"
    INVALID = "invalid", "INVALID"


class CallbackReplayWindowStatus(models.TextChoices):
    VALID = "valid", "VALID"
    EXPIRED = "expired", "EXPIRED"
    NOT_SUPPORTED = "not_supported", "NOT_SUPPORTED"


class CallbackProcessingStatus(models.TextChoices):
    NORMALIZED = "normalized", "NORMALIZED"
    DUPLICATE = "duplicate", "DUPLICATE"
    QUARANTINED = "quarantined", "QUARANTINED"
    SECURITY_REJECTED = "security_rejected", "SECURITY_REJECTED"


class ProviderEventResolutionStatus(models.TextChoices):
    VERIFICATION_REQUIRED = "verification_required", "VERIFICATION_REQUIRED"
    QUARANTINED = "quarantined", "QUARANTINED"
    CONTRADICTORY = "contradictory", "CONTRADICTORY"


class VerificationTriggerSource(models.TextChoices):
    CALLBACK = "callback", "CALLBACK"
    BROWSER_HINT = "browser_hint", "BROWSER_HINT"
    REQUEST_RESULT = "request_result", "REQUEST_RESULT"
    POLL = "poll", "POLL"
    UNKNOWN_OUTCOME = "unknown_outcome", "UNKNOWN_OUTCOME"
    RECONCILIATION = "reconciliation", "RECONCILIATION"
    ADMIN_RETRY = "admin_retry", "ADMIN_RETRY"


class VerificationOutcome(models.TextChoices):
    CONFIRMED_SUCCESS = "confirmed_success", "CONFIRMED_SUCCESS"
    CONFIRMED_DECLINE = "confirmed_decline", "CONFIRMED_DECLINE"
    CONFIRMED_CANCELED = "confirmed_canceled", "CONFIRMED_CANCELED"
    CONFIRMED_EXPIRED = "confirmed_expired", "CONFIRMED_EXPIRED"
    PENDING = "pending", "PENDING"
    NO_EFFECT_RETRYABLE = "no_effect_retryable", "NO_EFFECT_RETRYABLE"
    OUTCOME_UNKNOWN = "outcome_unknown", "OUTCOME_UNKNOWN"
    MISMATCH = "mismatch", "MISMATCH"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence", "CONTRADICTORY_EVIDENCE"
    SECURITY_FAILURE = "security_failure", "SECURITY_FAILURE"
    CONFIGURATION_FAILURE = "configuration_failure", "CONFIGURATION_FAILURE"
    PROTOCOL_FAILURE = "protocol_failure", "PROTOCOL_FAILURE"
    NOT_FOUND_FINAL = "not_found_final", "NOT_FOUND_FINAL"


class VerificationFinancialEffect(models.TextChoices):
    PAID = "paid", "PAID"
    UNPAID = "unpaid", "UNPAID"
    NONE = "none", "NONE"
    UNKNOWN = "unknown", "UNKNOWN"


class VerificationFinality(models.TextChoices):
    FINAL = "final", "FINAL"
    NON_FINAL = "non_final", "NON_FINAL"
    UNKNOWN = "unknown", "UNKNOWN"


class VerificationTransportClassification(models.TextChoices):
    SUCCESS = "success", "SUCCESS"
    TIMEOUT = "timeout", "TIMEOUT"
    NETWORK_FAILURE = "network_failure", "NETWORK_FAILURE"
    PROTOCOL_FAILURE = "protocol_failure", "PROTOCOL_FAILURE"
    NOT_EXECUTED = "not_executed", "NOT_EXECUTED"


class VerificationApplicationState(models.TextChoices):
    UNAPPLIED = "unapplied", "UNAPPLIED"
    APPLIED_UNPAID = "applied_unpaid", "APPLIED_UNPAID"
    APPLIED_BLOCKING_SUCCESS = "applied_blocking_success", "APPLIED_BLOCKING_SUCCESS"
    REVIEW_REQUIRED = "review_required", "REVIEW_REQUIRED"
    SUPERSEDED = "superseded", "SUPERSEDED"
    FINANCIALLY_APPLIED = "financially_applied", "FINANCIALLY_APPLIED"


class VerificationWorkType(models.TextChoices):
    VERIFY_AFTER_CALLBACK = "verify_after_callback", "VERIFY_AFTER_CALLBACK"
    VERIFY_AFTER_BROWSER_HINT = "verify_after_browser_hint", "VERIFY_AFTER_BROWSER_HINT"
    POLL_PENDING_OPERATION = "poll_pending_operation", "POLL_PENDING_OPERATION"
    VERIFY_UNKNOWN_OUTCOME = "verify_unknown_outcome", "VERIFY_UNKNOWN_OUTCOME"
    RETRY_PROVIDER_QUERY = "retry_provider_query", "RETRY_PROVIDER_QUERY"
    ESCALATE_UNKNOWN_OUTCOME = "escalate_unknown_outcome", "ESCALATE_UNKNOWN_OUTCOME"
    APPLY_VERIFIED_FUNDS = "apply_verified_funds", "APPLY_VERIFIED_FUNDS"


class VerificationWorkStatus(models.TextChoices):
    PENDING = "pending", "PENDING"
    CLAIMED = "claimed", "CLAIMED"
    WAITING = "waiting", "WAITING"
    COMPLETED = "completed", "COMPLETED"
    CANCELED = "canceled", "CANCELED"


class VerificationEvidenceBasis(models.TextChoices):
    NONE = "none", "NONE"
    SERVER_TO_SERVER = "server_to_server", "SERVER_TO_SERVER"
    AUTHENTICATED_SETTLEMENT = "authenticated_settlement", "AUTHENTICATED_SETTLEMENT"


class FinalizationWorkStatus(models.TextChoices):
    PENDING = "pending", "PENDING"
    CLAIMED = "claimed", "CLAIMED"
    COMPLETED = "completed", "COMPLETED"
    CANCELED = "canceled", "CANCELED"


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
    VERIFIED_FUNDS_APPLICATION_FAILED = (
        "verified_funds_application_failed",
        "VERIFIED_FUNDS_APPLICATION_FAILED",
    )
    PROVIDER_RECEIPT_JOURNAL_FAILED = (
        "provider_receipt_journal_failed",
        "PROVIDER_RECEIPT_JOURNAL_FAILED",
    )
    PAID_PENDING_FINALIZATION = "paid_pending_finalization", "PAID_PENDING_FINALIZATION"
    DUPLICATE_FINANCIAL_ALLOCATION = (
        "duplicate_financial_allocation",
        "DUPLICATE_FINANCIAL_ALLOCATION",
    )
    OVERPAYMENT = "overpayment", "OVERPAYMENT"
    ACCOUNTING_POLICY_MISSING = "accounting_policy_missing", "ACCOUNTING_POLICY_MISSING"
    FINANCIAL_INVARIANT_VIOLATION = (
        "financial_invariant_violation",
        "FINANCIAL_INVARIANT_VIOLATION",
    )


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


class ProviderDefinition(BaseModel):
    IMMUTABLE_FIELDS = ("key", "display_name")

    key = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128)
    is_enabled = models.BooleanField(default=False)
    new_requests_enabled = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("Provider identity is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)


class ProviderCapabilityVersion(BaseModel):
    IMMUTABLE_FIELDS = (
        "provider_id",
        "version",
        "adapter_key",
        "adapter_contract_version",
        "provider_unit",
        "conversion_policy_version",
        "supported_operations",
        "supports_request_idempotency",
        "supports_lookup",
        "callback_authentication",
        "verification_semantics",
        "finality_window_seconds",
        "authority_expiry_seconds",
        "supports_refund",
        "supports_void",
        "not_found_is_final_unpaid",
    )

    provider = models.ForeignKey(ProviderDefinition, on_delete=models.PROTECT, related_name="capability_versions")
    version = models.PositiveIntegerField()
    adapter_key = models.CharField(max_length=64)
    adapter_contract_version = models.CharField(max_length=32)
    provider_unit = models.CharField(max_length=16, choices=MoneyUnit.choices)
    conversion_policy_version = models.CharField(max_length=64)
    supported_operations = models.JSONField(default=list)
    supports_request_idempotency = models.BooleanField(default=False)
    supports_lookup = models.BooleanField(default=False)
    callback_authentication = models.CharField(
        max_length=32,
        choices=CallbackAuthenticationStrength.choices,
        default=CallbackAuthenticationStrength.NONE,
    )
    verification_semantics = models.CharField(
        max_length=32,
        choices=ProviderVerificationSemantics.choices,
        default=ProviderVerificationSemantics.REQUIRED,
    )
    finality_window_seconds = models.PositiveIntegerField()
    authority_expiry_seconds = models.PositiveIntegerField()
    supports_refund = models.BooleanField(default=False)
    supports_void = models.BooleanField(default=False)
    not_found_is_final_unpaid = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("provider", "version"), name="fin_provider_capability_version_uniq"),
            models.CheckConstraint(check=Q(version__gt=0), name="fin_provider_capability_version_gt_zero"),
            models.CheckConstraint(check=Q(provider_unit__in=MoneyUnit.values), name="fin_provider_unit_valid"),
            models.CheckConstraint(check=~Q(adapter_key=""), name="fin_provider_adapter_key_nonempty"),
            models.CheckConstraint(
                check=~Q(adapter_contract_version=""), name="fin_provider_adapter_version_nonempty"
            ),
            models.CheckConstraint(
                check=~Q(conversion_policy_version=""), name="fin_provider_conversion_version_nonempty"
            ),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.supported_operations, list) or not self.supported_operations:
            raise ValidationError({"supported_operations": "At least one supported operation is required."})
        unsupported = set(self.supported_operations) - set(PaymentTransactionOperation.values)
        if unsupported:
            raise ValidationError({"supported_operations": "Unsupported provider operation declaration."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("Provider capability versions are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Provider capability versions cannot be deleted.")


class MerchantAccountVersion(BaseModel):
    IMMUTABLE_FIELDS = (
        "provider_id",
        "capability_version_id",
        "account_key",
        "version",
        "owner_key",
        "credential_reference",
    )

    provider = models.ForeignKey(ProviderDefinition, on_delete=models.PROTECT, related_name="merchant_accounts")
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        related_name="merchant_accounts",
    )
    account_key = models.CharField(max_length=128)
    version = models.PositiveIntegerField()
    owner_key = models.CharField(max_length=128)
    credential_reference = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=False)
    new_requests_enabled = models.BooleanField(default=False)
    recovery_enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "account_key", "version"),
                name="fin_merchant_account_version_uniq",
            ),
            models.CheckConstraint(check=Q(version__gt=0), name="fin_merchant_account_version_gt_zero"),
            models.CheckConstraint(check=~Q(account_key=""), name="fin_merchant_account_key_nonempty"),
            models.CheckConstraint(check=~Q(owner_key=""), name="fin_merchant_owner_key_nonempty"),
            models.CheckConstraint(
                check=~Q(credential_reference=""), name="fin_merchant_credential_ref_nonempty"
            ),
        ]

    def clean(self):
        super().clean()
        if self.capability_version_id and self.provider_id != self.capability_version.provider_id:
            raise ValidationError({"capability_version": "Capability version must belong to the provider."})
        lowered = self.credential_reference.lower()
        if any(fragment in lowered for fragment in ("password=", "secret=", "token=", "api_key=")):
            raise ValidationError({"credential_reference": "Credential values must not be stored in the database."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("Merchant-account versions are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Merchant-account versions cannot be deleted.")


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


class PaymentObligationSource(AppendOnlyModel):
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="obligation_source")
    source_kind = models.CharField(max_length=32, choices=PaymentObligationSourceKind.choices)
    source_model = models.CharField(max_length=100)
    source_object_id = models.CharField(max_length=128)
    source_field = models.CharField(max_length=100)
    source_amount = models.DecimalField(max_digits=20, decimal_places=0)
    source_unit = models.CharField(max_length=16, choices=MoneyUnit.choices)
    canonical_amount = models.DecimalField(max_digits=20, decimal_places=0)
    canonical_currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    bridge_version = models.CharField(max_length=64)
    evidence_fingerprint = models.CharField(max_length=64, unique=True)
    commercial_snapshot_hash = models.CharField(max_length=64)
    commerce_authority = models.CharField(max_length=30)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source_model", "source_object_id", "source_field"),
                name="fin_obligation_source_identity_uniq",
            ),
            models.CheckConstraint(check=Q(source_amount__gt=0), name="fin_obligation_source_amount_gt_zero"),
            models.CheckConstraint(
                check=Q(canonical_amount__gt=0), name="fin_obligation_canonical_amount_gt_zero"
            ),
            models.CheckConstraint(
                check=Q(canonical_currency=CANONICAL_CURRENCY), name="fin_obligation_currency_irr"
            ),
            models.CheckConstraint(check=Q(source_unit__in=MoneyUnit.values), name="fin_obligation_unit_valid"),
            models.CheckConstraint(check=~Q(bridge_version=""), name="fin_obligation_bridge_nonempty"),
            models.CheckConstraint(check=~Q(commercial_snapshot_hash=""), name="fin_obligation_snapshot_nonempty"),
            models.CheckConstraint(
                check=(
                    Q(source_unit=MoneyUnit.IRR, canonical_amount=F("source_amount"))
                    | Q(source_unit=MoneyUnit.IRT, canonical_amount=F("source_amount") * 10)
                ),
                name="fin_obligation_conversion_exact",
            ),
        ]

    def clean(self):
        super().clean()
        if self.payment_id:
            if self.canonical_amount != self.payment.amount_due:
                raise ValidationError({"canonical_amount": "Canonical amount must equal the Payment obligation."})
            if self.canonical_currency != self.payment.currency:
                raise ValidationError({"canonical_currency": "Canonical currency must equal Payment currency."})


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
        "capability_version_id",
        "merchant_account_version_id",
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
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_attempts",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_attempts",
    )
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
            models.UniqueConstraint(
                fields=("payment",),
                condition=Q(
                    status__in=(
                        PaymentAttemptStatus.CREATED,
                        PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
                        PaymentAttemptStatus.PROCESSING,
                        PaymentAttemptStatus.SUCCEEDED,
                        PaymentAttemptStatus.OUTCOME_UNKNOWN,
                        PaymentAttemptStatus.REVIEW,
                    )
                ),
                name="fin_one_blocking_attempt_per_payment",
            ),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_attempt_sequence_gt_zero"),
            models.CheckConstraint(check=Q(requested_amount__gt=0), name="fin_attempt_amount_gt_zero"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_attempt_currency_irr"),
            models.CheckConstraint(check=~Q(request_hash=""), name="fin_attempt_hash_nonempty"),
            models.CheckConstraint(
                check=(
                    Q(capability_version__isnull=True, merchant_account_version__isnull=True)
                    | Q(capability_version__isnull=False, merchant_account_version__isnull=False)
                ),
                name="fin_attempt_provider_versions_together",
            ),
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
            if bool(self.capability_version_id) != bool(self.merchant_account_version_id):
                raise ValidationError("Provider capability and merchant-account versions must be recorded together.")
            if self.merchant_account_version_id:
                if self.provider != self.merchant_account_version.provider.key:
                    raise ValidationError({"provider": "Attempt provider must match the merchant account."})
                if self.merchant_account_ref != self.merchant_account_version.account_key:
                    raise ValidationError({"merchant_account_ref": "Attempt account key must match its version."})
                if self.capability_version_id != self.merchant_account_version.capability_version_id:
                    raise ValidationError({"capability_version": "Attempt capability version is inconsistent."})

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
        "capability_version_id",
        "merchant_account_version_id",
        "adapter_contract_version",
        "merchant_reference",
        "amount",
        "currency",
        "provider_amount",
        "provider_unit",
        "provider_conversion_policy_version",
        "provider_idempotency_reference",
        "request_fingerprint",
        "correlation_id",
        "causation_id",
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
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_transactions",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_transactions",
    )
    adapter_contract_version = models.CharField(max_length=32, blank=True)
    merchant_reference = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=20, decimal_places=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    provider_amount = models.DecimalField(max_digits=20, decimal_places=0)
    provider_unit = models.CharField(max_length=16)
    provider_conversion_policy_version = models.CharField(max_length=64, blank=True)
    provider_idempotency_reference = models.CharField(max_length=128, null=True, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True)
    correlation_id = models.UUIDField(default=uuid4, db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
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
    claim_expires_at = models.DateTimeField(null=True, blank=True)
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
                fields=("merchant_account_version", "merchant_reference"),
                condition=Q(merchant_account_version__isnull=False),
                name="fin_tx_account_version_merchant_ref_uniq",
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
            models.CheckConstraint(check=Q(provider_unit__in=MoneyUnit.values), name="fin_tx_provider_unit_valid"),
            models.CheckConstraint(check=~Q(provider=""), name="fin_tx_provider_nonempty"),
            models.CheckConstraint(check=~Q(merchant_reference=""), name="fin_tx_merchant_ref_nonempty"),
            models.CheckConstraint(
                check=(
                    Q(provider_unit=MoneyUnit.IRR, provider_amount=F("amount"))
                    | Q(provider_unit=MoneyUnit.IRT, amount=F("provider_amount") * 10)
                ),
                name="fin_tx_provider_amount_exact",
            ),
            models.CheckConstraint(
                check=(
                    Q(capability_version__isnull=True, merchant_account_version__isnull=True)
                    | Q(
                        capability_version__isnull=False,
                        merchant_account_version__isnull=False,
                    )
                    & ~Q(request_fingerprint="")
                    & ~Q(adapter_contract_version="")
                    & ~Q(provider_conversion_policy_version="")
                ),
                name="fin_tx_provider_versions_consistent",
            ),
            models.CheckConstraint(
                check=(
                    Q(capability_version__isnull=True)
                    |
                    Q(
                        status=PaymentTransactionStatus.REQUESTING,
                        claim_token__isnull=False,
                        claimed_at__isnull=False,
                        claim_expires_at__isnull=False,
                    )
                    | ~Q(status=PaymentTransactionStatus.REQUESTING)
                ),
                name="fin_tx_request_claim_present",
            ),
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
            if self.capability_version_id != self.attempt.capability_version_id:
                raise ValidationError({"capability_version": "Transaction capability must match PaymentAttempt."})
            if self.merchant_account_version_id != self.attempt.merchant_account_version_id:
                raise ValidationError({"merchant_account_version": "Transaction account must match PaymentAttempt."})
            if self.currency != self.attempt.currency:
                raise ValidationError({"currency": "Transaction currency must match PaymentAttempt."})
            if self.amount > self.attempt.requested_amount:
                raise ValidationError({"amount": "Transaction amount cannot exceed attempt amount."})
        if self.parent_id:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "Transaction cannot parent itself."})
            if self.parent.attempt_id != self.attempt_id:
                raise ValidationError({"parent": "Parent transaction must belong to the same attempt."})
        if bool(self.capability_version_id) != bool(self.merchant_account_version_id):
            raise ValidationError("Provider capability and merchant-account versions must be recorded together.")
        if self.capability_version_id:
            if self.adapter_contract_version != self.capability_version.adapter_contract_version:
                raise ValidationError({"adapter_contract_version": "Adapter contract version is inconsistent."})
            if self.provider_unit != self.capability_version.provider_unit:
                raise ValidationError({"provider_unit": "Provider unit is inconsistent with capability version."})
            if self.provider_conversion_policy_version != self.capability_version.conversion_policy_version:
                raise ValidationError(
                    {"provider_conversion_policy_version": "Provider conversion policy version is inconsistent."}
                )
            if not self.request_fingerprint:
                raise ValidationError({"request_fingerprint": "Versioned provider transactions require a fingerprint."})
            if (
                self.capability_version.supports_request_idempotency
                and not self.provider_idempotency_reference
            ):
                raise ValidationError(
                    {"provider_idempotency_reference": "Provider idempotency reference is required."}
                )

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


class ProviderRequestResult(AppendOnlyModel):
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="request_results",
    )
    outcome = models.CharField(max_length=32, choices=ProviderRequestOutcome.choices)
    claim_token = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    evidence_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=100, blank=True)
    safe_metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(outcome__in=ProviderRequestOutcome.values), name="fin_request_result_outcome_valid"
            ),
            models.CheckConstraint(
                check=~Q(outcome=ProviderRequestOutcome.CONFIRMED_SUCCESS),
                name="fin_c2a_request_result_no_success",
            ),
            models.CheckConstraint(check=~Q(request_fingerprint=""), name="fin_request_result_fingerprint_nonempty"),
            models.CheckConstraint(check=~Q(evidence_hash=""), name="fin_request_result_evidence_nonempty"),
        ]

    def clean(self):
        super().clean()
        if self.transaction_id and self.request_fingerprint != self.transaction.request_fingerprint:
            raise ValidationError({"request_fingerprint": "Result fingerprint must match the transaction."})


class ProviderRequestClaim(AppendOnlyModel):
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="request_claims",
    )
    sequence = models.PositiveIntegerField()
    claim_token = models.UUIDField(unique=True, editable=False)
    claimed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    idempotency_key = models.UUIDField(unique=True, editable=False)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("transaction", "sequence"), name="fin_request_claim_sequence_uniq"
            ),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_request_claim_sequence_gt_zero"),
            models.CheckConstraint(check=Q(expires_at__gt=F("claimed_at")), name="fin_request_claim_window_valid"),
        ]


class FinancialOutboxMessage(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    topic = models.CharField(max_length=100, db_index=True)
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=128)
    idempotency_key = models.CharField(max_length=200, unique=True)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    safe_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=~Q(topic=""), name="fin_outbox_topic_nonempty"),
            models.CheckConstraint(check=~Q(aggregate_type=""), name="fin_outbox_aggregate_nonempty"),
            models.CheckConstraint(check=~Q(aggregate_id=""), name="fin_outbox_aggregate_id_nonempty"),
        ]


class CallbackReceipt(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    provider = models.ForeignKey(
        ProviderDefinition,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="callback_receipts",
    )
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="callback_receipts",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="callback_receipts",
    )
    provider_key_hint = models.CharField(max_length=64)
    adapter_version_hint = models.CharField(max_length=32)
    account_hint_hash = models.CharField(max_length=64, blank=True)
    http_method = models.CharField(max_length=8)
    content_type = models.CharField(max_length=100)
    body_length = models.PositiveIntegerField()
    raw_envelope_hash = models.CharField(max_length=64)
    header_evidence = models.JSONField(default=dict, blank=True)
    source_network_hash = models.CharField(max_length=64, blank=True)
    authentication_status = models.CharField(max_length=32, choices=CallbackAuthenticationStatus.choices)
    authentication_strength = models.CharField(
        max_length=32,
        choices=CallbackAuthenticationStrength.choices,
        default=CallbackAuthenticationStrength.NONE,
    )
    authentication_method = models.CharField(max_length=64, blank=True)
    authentication_version = models.CharField(max_length=32, blank=True)
    authentication_evidence_hash = models.CharField(max_length=64, blank=True)
    signing_key_reference_hash = models.CharField(max_length=64, blank=True)
    replay_window_status = models.CharField(max_length=24, choices=CallbackReplayWindowStatus.choices)
    processing_status = models.CharField(max_length=24, choices=CallbackProcessingStatus.choices)
    correlation_id = models.UUIDField(db_index=True)
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="duplicate_deliveries",
    )
    quarantine_reason = models.CharField(max_length=64, blank=True)
    safe_reason_code = models.CharField(max_length=64, blank=True)
    delivery_idempotency_key = models.UUIDField(unique=True, editable=False)
    retention_until = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(body_length__gte=0), name="fin_cb_body_length_nonnegative"),
            models.CheckConstraint(check=~Q(raw_envelope_hash=""), name="fin_cb_envelope_hash_nonempty"),
            models.CheckConstraint(check=~Q(http_method=""), name="fin_cb_method_nonempty"),
            models.CheckConstraint(
                check=Q(authentication_status__in=CallbackAuthenticationStatus.values),
                name="fin_cb_auth_status_valid",
            ),
            models.CheckConstraint(
                check=(
                    ~Q(authentication_status=CallbackAuthenticationStatus.AUTHENTICATED)
                    | ~Q(authentication_evidence_hash="")
                ),
                name="fin_cb_authenticated_evidence",
            ),
            models.CheckConstraint(
                check=Q(replay_window_status__in=CallbackReplayWindowStatus.values),
                name="fin_cb_replay_status_valid",
            ),
            models.CheckConstraint(
                check=Q(processing_status__in=CallbackProcessingStatus.values),
                name="fin_cb_processing_status_valid",
            ),
            models.CheckConstraint(
                check=Q(retention_until__gt=F("received_at")),
                name="fin_cb_retention_after_receipt",
            ),
        ]
        indexes = [
            models.Index(fields=("provider", "raw_envelope_hash"), name="fin_cb_provider_envelope"),
            models.Index(fields=("processing_status", "received_at"), name="fin_cb_status_received"),
        ]


class ProviderEvent(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    provider = models.ForeignKey(ProviderDefinition, on_delete=models.PROTECT, related_name="provider_events")
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        related_name="provider_events",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        related_name="provider_events",
    )
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provider_events",
    )
    adapter_contract_version = models.CharField(max_length=32)
    provider_event_id = models.CharField(max_length=128, blank=True)
    canonical_envelope_hash = models.CharField(max_length=64)
    merchant_reference = models.CharField(max_length=128, blank=True)
    provider_authority = models.CharField(max_length=128, blank=True)
    provider_reference = models.CharField(max_length=128, blank=True)
    operation_type_hint = models.CharField(max_length=16, blank=True)
    provider_amount_hint = models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)
    provider_unit_hint = models.CharField(max_length=16, blank=True)
    normalized_hint = models.CharField(max_length=64)
    provider_occurred_at = models.DateTimeField(null=True, blank=True)
    authentication_strength = models.CharField(max_length=32, choices=CallbackAuthenticationStrength.choices)
    deduplication_identity = models.CharField(max_length=64, unique=True)
    resolution_status = models.CharField(max_length=32, choices=ProviderEventResolutionStatus.choices)
    quarantine_reason = models.CharField(max_length=64, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=~Q(canonical_envelope_hash=""), name="fin_provider_event_hash_nonempty"
            ),
            models.CheckConstraint(
                check=~Q(normalized_hint=""), name="fin_provider_event_hint_nonempty"
            ),
            models.CheckConstraint(
                check=Q(resolution_status__in=ProviderEventResolutionStatus.values),
                name="fin_provider_event_resolution_valid",
            ),
            models.CheckConstraint(
                check=Q(provider_amount_hint__isnull=True) | Q(provider_amount_hint__gt=0),
                name="fin_provider_event_amount_positive",
            ),
            models.CheckConstraint(
                check=(
                    Q(provider_amount_hint__isnull=True, provider_unit_hint="")
                    | Q(provider_amount_hint__isnull=False, provider_unit_hint__in=MoneyUnit.values)
                ),
                name="fin_provider_event_money_together",
            ),
        ]
        indexes = [
            models.Index(
                fields=("merchant_account_version", "provider_event_id"),
                name="fin_provider_event_external",
            ),
            models.Index(fields=("merchant_reference", "created_at"), name="fin_provider_event_merchant"),
        ]


class ProviderEventReceipt(AppendOnlyModel):
    provider_event = models.ForeignKey(
        ProviderEvent,
        on_delete=models.PROTECT,
        related_name="receipt_links",
    )
    callback_receipt = models.OneToOneField(
        CallbackReceipt,
        on_delete=models.PROTECT,
        related_name="event_link",
    )
    linkage_fingerprint = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class VerificationWorkItem(BaseModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="verification_work_items",
    )
    provider_event = models.ForeignKey(
        ProviderEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verification_work_items",
    )
    work_type = models.CharField(max_length=40, choices=VerificationWorkType.choices)
    deterministic_identity = models.CharField(max_length=200, unique=True)
    status = models.CharField(
        max_length=16,
        choices=VerificationWorkStatus.choices,
        default=VerificationWorkStatus.PENDING,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=8)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_classification = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(attempt_count__lte=F("max_attempts")), name="fin_work_attempts_bounded"),
            models.CheckConstraint(check=Q(max_attempts__gt=0), name="fin_work_max_attempts_positive"),
            models.CheckConstraint(
                check=Q(work_type__in=VerificationWorkType.values), name="fin_work_type_valid"
            ),
            models.CheckConstraint(
                check=Q(status__in=VerificationWorkStatus.values), name="fin_work_status_valid"
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status=VerificationWorkStatus.CLAIMED,
                        claim_token__isnull=False,
                        claimed_at__isnull=False,
                        claim_expires_at__isnull=False,
                        completed_at__isnull=True,
                    )
                    | Q(
                        status__in=(VerificationWorkStatus.PENDING, VerificationWorkStatus.WAITING),
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                        claim_expires_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | Q(
                        status__in=(VerificationWorkStatus.COMPLETED, VerificationWorkStatus.CANCELED),
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                        claim_expires_at__isnull=True,
                        completed_at__isnull=False,
                    )
                ),
                name="fin_work_lease_state_consistent",
            ),
        ]
        indexes = [models.Index(fields=("status", "next_attempt_at"), name="fin_work_due")]


class VerificationClaim(AppendOnlyModel):
    work_item = models.ForeignKey(
        VerificationWorkItem,
        on_delete=models.PROTECT,
        related_name="claims",
    )
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="verification_claims",
    )
    sequence = models.PositiveIntegerField()
    claim_token = models.UUIDField(unique=True, editable=False)
    claimed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    request_fingerprint = models.CharField(max_length=64)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("work_item", "sequence"), name="fin_ver_claim_sequence_uniq"),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_ver_claim_sequence_positive"),
            models.CheckConstraint(check=Q(expires_at__gt=F("claimed_at")), name="fin_ver_claim_window_valid"),
            models.CheckConstraint(check=~Q(request_fingerprint=""), name="fin_ver_claim_hash_nonempty"),
        ]


class Verification(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="verifications",
    )
    claim = models.OneToOneField(
        VerificationClaim,
        on_delete=models.PROTECT,
        related_name="verification",
    )
    work_item = models.ForeignKey(
        VerificationWorkItem,
        on_delete=models.PROTECT,
        related_name="verifications",
    )
    provider_event = models.ForeignKey(
        ProviderEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verifications",
    )
    provider = models.ForeignKey(ProviderDefinition, on_delete=models.PROTECT, related_name="verifications")
    capability_version = models.ForeignKey(
        ProviderCapabilityVersion,
        on_delete=models.PROTECT,
        related_name="verifications",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        related_name="verifications",
    )
    sequence = models.PositiveIntegerField()
    trigger_source = models.CharField(max_length=32, choices=VerificationTriggerSource.choices)
    adapter_contract_version = models.CharField(max_length=32)
    merchant_reference = models.CharField(max_length=128)
    provider_authority = models.CharField(max_length=128, blank=True)
    provider_reference = models.CharField(max_length=128, blank=True)
    operation_type = models.CharField(max_length=16, choices=PaymentTransactionOperation.choices)
    requested_provider_amount = models.DecimalField(max_digits=20, decimal_places=0)
    requested_provider_unit = models.CharField(max_length=16, choices=MoneyUnit.choices)
    observed_provider_amount = models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)
    observed_provider_unit = models.CharField(max_length=16, blank=True)
    canonical_allocation_amount = models.DecimalField(max_digits=20, decimal_places=0)
    canonical_currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    normalized_outcome = models.CharField(max_length=40, choices=VerificationOutcome.choices)
    normalized_financial_effect = models.CharField(
        max_length=16,
        choices=VerificationFinancialEffect.choices,
    )
    finality = models.CharField(max_length=16, choices=VerificationFinality.choices)
    provider_occurred_at = models.DateTimeField(null=True, blank=True)
    transport_classification = models.CharField(
        max_length=24,
        choices=VerificationTransportClassification.choices,
    )
    evidence_basis = models.CharField(
        max_length=32,
        choices=VerificationEvidenceBasis.choices,
        default=VerificationEvidenceBasis.NONE,
    )
    evidence_hash = models.CharField(max_length=64)
    request_evidence_reference = models.CharField(max_length=128, blank=True)
    response_evidence_reference = models.CharField(max_length=128, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    verified_at = models.DateTimeField(default=timezone.now, db_index=True)
    application_state = models.CharField(max_length=32, choices=VerificationApplicationState.choices)
    error_classification = models.CharField(max_length=64, blank=True)
    retryable = models.BooleanField(default=False)
    result_idempotency_key = models.UUIDField(unique=True, editable=False)
    result_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("transaction", "sequence"), name="fin_ver_tx_sequence_uniq"),
            models.CheckConstraint(check=Q(sequence__gt=0), name="fin_ver_sequence_positive"),
            models.CheckConstraint(check=Q(requested_provider_amount__gt=0), name="fin_ver_requested_positive"),
            models.CheckConstraint(check=Q(canonical_allocation_amount__gt=0), name="fin_ver_canonical_positive"),
            models.CheckConstraint(check=Q(canonical_currency=CANONICAL_CURRENCY), name="fin_ver_currency_irr"),
            models.CheckConstraint(
                check=(
                    Q(observed_provider_amount__isnull=True, observed_provider_unit="")
                    | Q(observed_provider_amount__gt=0, observed_provider_unit__in=MoneyUnit.values)
                ),
                name="fin_ver_observed_money_together",
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        normalized_outcome=VerificationOutcome.CONFIRMED_SUCCESS,
                        normalized_financial_effect=VerificationFinancialEffect.PAID,
                        finality=VerificationFinality.FINAL,
                        application_state=VerificationApplicationState.APPLIED_BLOCKING_SUCCESS,
                        observed_provider_amount__isnull=False,
                    )
                    & ~Q(provider_reference="")
                    | ~Q(normalized_outcome=VerificationOutcome.CONFIRMED_SUCCESS)
                ),
                name="fin_ver_success_evidence_complete",
            ),
            models.CheckConstraint(check=~Q(evidence_hash=""), name="fin_ver_evidence_hash_nonempty"),
            models.CheckConstraint(check=~Q(result_fingerprint=""), name="fin_ver_result_hash_nonempty"),
        ]
        indexes = [
            models.Index(fields=("transaction", "normalized_outcome"), name="fin_ver_tx_outcome"),
            models.Index(fields=("application_state", "verified_at"), name="fin_ver_application"),
        ]

    def clean(self):
        super().clean()
        if self.transaction_id:
            transaction_obj = self.transaction
            if self.provider_id != transaction_obj.capability_version.provider_id:
                raise ValidationError({"provider": "Verification provider must match the Transaction."})
            if self.capability_version_id != transaction_obj.capability_version_id:
                raise ValidationError({"capability_version": "Verification capability must match the Transaction."})
            if self.merchant_account_version_id != transaction_obj.merchant_account_version_id:
                raise ValidationError({"merchant_account_version": "Verification account must match the Transaction."})
            if self.merchant_reference != transaction_obj.merchant_reference:
                raise ValidationError({"merchant_reference": "Verification merchant reference must match."})

    @property
    def projected_application_state(self):
        if self.pk and FinancialAllocation.objects.filter(verification_id=self.pk).exists():
            return VerificationApplicationState.FINANCIALLY_APPLIED
        return self.application_state


class ProviderReferenceAllocation(AppendOnlyModel):
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        related_name="provider_reference_allocations",
    )
    transaction = models.OneToOneField(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="provider_reference_allocation",
    )
    verification = models.ForeignKey(
        Verification,
        on_delete=models.PROTECT,
        related_name="provider_reference_allocations",
    )
    provider_reference = models.CharField(max_length=128)
    allocation_fingerprint = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("merchant_account_version", "provider_reference"),
                name="fin_provider_reference_account_uniq",
            ),
            models.CheckConstraint(check=~Q(provider_reference=""), name="fin_provider_reference_nonempty"),
        ]


class ReceiptAccountingPolicyVersion(BaseModel):
    IMMUTABLE_FIELDS = (
        "public_id",
        "merchant_account_version_id",
        "policy_key",
        "version",
        "provider_clearing_account_id",
        "customer_unapplied_funds_account_id",
        "currency",
    )

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        related_name="receipt_accounting_policies",
    )
    policy_key = models.CharField(max_length=128)
    version = models.PositiveIntegerField()
    provider_clearing_account = models.ForeignKey(
        "FinancialAccount",
        on_delete=models.PROTECT,
        related_name="provider_receipt_policies",
    )
    customer_unapplied_funds_account = models.ForeignKey(
        "FinancialAccount",
        on_delete=models.PROTECT,
        related_name="customer_unapplied_receipt_policies",
    )
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    active_for_new_applications = models.BooleanField(default=False, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("merchant_account_version", "policy_key", "version"),
                name="fin_receipt_policy_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("merchant_account_version",),
                condition=Q(active_for_new_applications=True),
                name="fin_one_active_receipt_policy",
            ),
            models.CheckConstraint(check=Q(version__gt=0), name="fin_receipt_policy_version_positive"),
            models.CheckConstraint(check=~Q(policy_key=""), name="fin_receipt_policy_key_nonempty"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_receipt_policy_currency_irr"),
            models.CheckConstraint(
                check=~Q(provider_clearing_account=F("customer_unapplied_funds_account")),
                name="fin_receipt_policy_accounts_distinct",
            ),
        ]

    def clean(self):
        super().clean()
        if self.provider_clearing_account_id:
            if self.provider_clearing_account.currency != CANONICAL_CURRENCY:
                raise ValidationError({"provider_clearing_account": "Clearing account must use IRR."})
            if self.provider_clearing_account.account_type != FinancialAccountType.ASSET:
                raise ValidationError({"provider_clearing_account": "Clearing account must be an asset."})
        if self.customer_unapplied_funds_account_id:
            if self.customer_unapplied_funds_account.currency != CANONICAL_CURRENCY:
                raise ValidationError({"customer_unapplied_funds_account": "Unapplied-funds account must use IRR."})
            if self.customer_unapplied_funds_account.account_type != FinancialAccountType.LIABILITY:
                raise ValidationError({"customer_unapplied_funds_account": "Unapplied-funds account must be a liability."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("Receipt accounting policy identity is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Receipt accounting policy history cannot be deleted.")


class FinancialAllocation(AppendOnlyModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="financial_allocations")
    attempt = models.ForeignKey(
        PaymentAttempt,
        on_delete=models.PROTECT,
        related_name="financial_allocations",
    )
    transaction = models.OneToOneField(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name="financial_allocation",
    )
    verification = models.OneToOneField(
        Verification,
        on_delete=models.PROTECT,
        related_name="financial_allocation",
    )
    merchant_account_version = models.ForeignKey(
        MerchantAccountVersion,
        on_delete=models.PROTECT,
        related_name="financial_allocations",
    )
    accounting_policy_version = models.ForeignKey(
        ReceiptAccountingPolicyVersion,
        on_delete=models.PROTECT,
        related_name="financial_allocations",
    )
    journal_entry = models.OneToOneField(
        "JournalEntry",
        on_delete=models.PROTECT,
        related_name="financial_allocation",
    )
    provider_reference = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=20, decimal_places=0)
    currency = models.CharField(max_length=3, default=CANONICAL_CURRENCY)
    application_idempotency_key = models.UUIDField(unique=True, editable=False)
    application_fingerprint = models.CharField(max_length=64)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    applied_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("merchant_account_version", "provider_reference"),
                name="fin_allocation_provider_reference_uniq",
            ),
            models.CheckConstraint(check=Q(amount__gt=0), name="fin_allocation_amount_positive"),
            models.CheckConstraint(check=Q(currency=CANONICAL_CURRENCY), name="fin_allocation_currency_irr"),
            models.CheckConstraint(check=~Q(provider_reference=""), name="fin_allocation_provider_ref_nonempty"),
            models.CheckConstraint(check=~Q(application_fingerprint=""), name="fin_allocation_hash_nonempty"),
        ]
        indexes = [models.Index(fields=("payment", "applied_at"), name="fin_allocation_payment_time")]

    def clean(self):
        super().clean()
        if not self.transaction_id:
            return
        transaction_obj = self.transaction
        if self.attempt_id != transaction_obj.attempt_id:
            raise ValidationError({"attempt": "Allocation Attempt must own the Transaction."})
        if self.payment_id != transaction_obj.attempt.payment_id:
            raise ValidationError({"payment": "Allocation Payment must own the Attempt."})
        if self.verification_id and self.verification.transaction_id != self.transaction_id:
            raise ValidationError({"verification": "Allocation Verification must belong to the Transaction."})
        if self.merchant_account_version_id != transaction_obj.merchant_account_version_id:
            raise ValidationError({"merchant_account_version": "Allocation account must match the Transaction."})
        if self.accounting_policy_version_id:
            if self.accounting_policy_version.merchant_account_version_id != self.merchant_account_version_id:
                raise ValidationError({"accounting_policy_version": "Receipt policy must belong to the merchant account."})
        if self.amount != self.verification.canonical_allocation_amount or self.currency != self.verification.canonical_currency:
            raise ValidationError("Allocation money must equal immutable Verification money.")
        if self.provider_reference != self.verification.provider_reference:
            raise ValidationError({"provider_reference": "Allocation reference must equal Verification evidence."})


class CommercialFinalizationWorkItem(BaseModel):
    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="finalization_work_items")
    finalizer_version = models.CharField(max_length=64)
    deterministic_identity = models.CharField(max_length=200, unique=True)
    status = models.CharField(
        max_length=16,
        choices=FinalizationWorkStatus.choices,
        default=FinalizationWorkStatus.PENDING,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=12)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error_classification = models.CharField(max_length=64, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("payment", "finalizer_version"),
                name="fin_finalization_work_identity_uniq",
            ),
            models.CheckConstraint(check=~Q(finalizer_version=""), name="fin_finalizer_version_nonempty"),
            models.CheckConstraint(check=Q(max_attempts__gt=0), name="fin_finalization_max_attempts_positive"),
            models.CheckConstraint(
                check=Q(attempt_count__lte=F("max_attempts")),
                name="fin_finalization_attempts_bounded",
            ),
            models.CheckConstraint(
                check=Q(status__in=FinalizationWorkStatus.values),
                name="fin_finalization_work_status_valid",
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        status=FinalizationWorkStatus.CLAIMED,
                        claim_token__isnull=False,
                        claimed_at__isnull=False,
                        claim_expires_at__isnull=False,
                        completed_at__isnull=True,
                    )
                    | Q(
                        status=FinalizationWorkStatus.PENDING,
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                        claim_expires_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | Q(
                        status__in=(FinalizationWorkStatus.COMPLETED, FinalizationWorkStatus.CANCELED),
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                        claim_expires_at__isnull=True,
                        completed_at__isnull=False,
                    )
                ),
                name="fin_finalization_work_state_consistent",
            ),
        ]
        indexes = [models.Index(fields=("status", "next_attempt_at"), name="fin_finalization_work_due")]

    def save(self, *args, **kwargs):
        if self.pk:
            immutable_fields = (
                "public_id",
                "payment_id",
                "finalizer_version",
                "deterministic_identity",
                "max_attempts",
                "correlation_id",
                "causation_id",
            )
            original = type(self).objects.filter(pk=self.pk).values(*immutable_fields).first()
            if original and any(original[field] != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Commercial-finalization work identity is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Commercial-finalization work history cannot be deleted.")


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
