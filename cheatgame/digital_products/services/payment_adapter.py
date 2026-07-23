import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    FinancialActorType,
    IdempotencyRecord,
    IdempotencyStatus,
    MerchantAccountVersion,
    PaymentAttemptStatus,
    PaymentObligationSourceKind,
    PaymentTenderType,
    PaymentTransactionStatus,
    ProviderRequestOutcome,
)
from cheatgame.financial_core.services.adapters import (
    NormalizedProviderResult,
    PRODUCTION_ADAPTER_REGISTRY,
    execute_adapter_outside_transaction,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.placement import (
    PlacementNotEligible,
    place_order_and_create_payment_obligation,
)
from cheatgame.financial_core.services.provider_requests import (
    CollectionBlocked,
    RequestClaimConflict,
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
    create_or_replay_request_transaction,
)
from cheatgame.financial_core.models import PaymentTransactionOperation
from cheatgame.product.models import ProductCommerceAuthority
from cheatgame.shop.models import Checkout, CheckoutStatus, Order


class DigitalPaymentAdapterError(ValidationError):
    pass


class DigitalPaymentNotFound(DigitalPaymentAdapterError):
    pass


class DigitalPaymentNotReady(DigitalPaymentAdapterError):
    pass


class DigitalPaymentProviderUnavailable(DigitalPaymentAdapterError):
    pass


class DigitalPaymentRequestConflict(DigitalPaymentAdapterError):
    pass


class DigitalPaymentRequestInProgress(DigitalPaymentAdapterError):
    pass


@dataclass(frozen=True)
class DigitalPaymentRequestResult:
    checkout_public_id: UUID
    customer_action_url: str
    replayed: bool


def _stage_key(root_key, request_hash, stage):
    return uuid5(UUID(str(root_key)), f"digital-payment:{request_hash}:{stage}")


def _root_scope(checkout):
    return f"digital_api:payment_request:{checkout.pk}"


def _request_payload(*, checkout, actor, provider, account, commercial_revision, placement_checkout_version):
    return {
        "checkout_id": int(checkout.pk),
        "checkout_public_id": str(checkout.public_id),
        "customer_id": int(actor.pk),
        "placement_checkout_version": int(placement_checkout_version),
        "commercial_revision": int(commercial_revision),
        "provider": str(provider),
        "merchant_account_version_id": int(account.pk),
        "merchant_account_version": int(account.version),
        "provider_capability_version_id": int(account.capability_version_id),
        "provider_capability_version": int(account.capability_version.version),
    }


def _commercial_revision(checkout):
    revisions = set(checkout.lines.values_list("snapshot__commercial_revision", flat=True))
    if len(revisions) != 1 or None in revisions:
        raise DigitalPaymentNotReady("Checkout commercial revision is incoherent.")
    revision = revisions.pop()
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DigitalPaymentNotReady("Checkout commercial revision is invalid.")
    return revision


def _validate_checkout(checkout, *, user_id, allow_placed=False):
    if checkout.user_id != int(user_id):
        raise PermissionDenied("Checkout ownership is invalid.")
    authorities = set(checkout.lines.values_list("commerce_authority", flat=True))
    if authorities != {ProductCommerceAuthority.DIGITAL_PRODUCTS}:
        raise DigitalPaymentNotReady("Only homogeneous Digital Checkouts use this payment boundary.")
    if not checkout.lines.exists():
        raise DigitalPaymentNotReady("Checkout has no immutable commercial lines.")
    if checkout.status == CheckoutStatus.CANCELED:
        raise DigitalPaymentNotReady("Canceled Checkout cannot start payment.")
    if not allow_placed:
        if checkout.status != CheckoutStatus.CHECKOUT_DRAFT or checkout.expires_at <= timezone.now():
            raise DigitalPaymentNotReady("Checkout is not payment-ready.")
    elif checkout.status not in (CheckoutStatus.CHECKOUT_DRAFT, CheckoutStatus.PENDING_PAYMENT):
        raise DigitalPaymentNotReady("Checkout is outside the payment-request boundary.")
    return _commercial_revision(checkout)


def _resolve_provider(provider, *, adapter_registry):
    accounts = list(
        MerchantAccountVersion.objects.select_related("provider", "capability_version")
        .filter(
            provider__key=provider,
            provider__is_enabled=True,
            provider__new_requests_enabled=True,
            is_enabled=True,
            new_requests_enabled=True,
        )
        .order_by("pk")
    )
    accounts = [
        account
        for account in accounts
        if PaymentTransactionOperation.SALE in account.capability_version.supported_operations
    ]
    if len(accounts) != 1:
        raise DigitalPaymentProviderUnavailable("Payment provider is unavailable.")
    account = accounts[0]
    try:
        adapter = adapter_registry.resolve(
            adapter_key=account.capability_version.adapter_key,
            contract_version=account.capability_version.adapter_contract_version,
        )
    except ValidationError as exc:
        raise DigitalPaymentProviderUnavailable("Payment provider is unavailable.") from exc
    return account, adapter


def _resolve_frozen_provider(identity, *, adapter_registry):
    try:
        account = MerchantAccountVersion.objects.select_related("provider", "capability_version").get(
            pk=identity["merchant_account_version_id"]
        )
    except (KeyError, MerchantAccountVersion.DoesNotExist) as exc:
        raise DigitalPaymentRequestConflict("Frozen provider policy is unavailable.") from exc
    if (
        account.provider.key != identity.get("provider")
        or account.version != identity.get("merchant_account_version")
        or account.capability_version_id != identity.get("provider_capability_version_id")
        or account.capability_version.version != identity.get("provider_capability_version")
    ):
        raise DigitalPaymentRequestConflict("Frozen provider policy identity changed.")
    if (
        not account.provider.is_enabled
        or not account.provider.new_requests_enabled
        or not account.is_enabled
        or not account.new_requests_enabled
        or PaymentTransactionOperation.SALE not in account.capability_version.supported_operations
    ):
        raise DigitalPaymentRequestConflict("Frozen provider policy no longer permits this request.")
    try:
        adapter = adapter_registry.resolve(
            adapter_key=account.capability_version.adapter_key,
            contract_version=account.capability_version.adapter_contract_version,
        )
    except ValidationError as exc:
        raise DigitalPaymentProviderUnavailable("Payment provider is unavailable.") from exc
    return account, adapter


def _validate_customer_action_url(*, provider, url):
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise DigitalPaymentProviderUnavailable("Provider returned no usable customer handoff.")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DigitalPaymentProviderUnavailable("Provider returned an unsafe customer handoff.")
    allowlist = getattr(settings, "FINANCIAL_PROVIDER_CUSTOMER_ACTION_HOSTS", {})
    hosts = allowlist.get(provider, ()) if isinstance(allowlist, dict) else ()
    normalized_hosts = {str(host).lower().rstrip(".") for host in hosts}
    if parsed.hostname.lower().rstrip(".") not in normalized_hosts:
        raise DigitalPaymentProviderUnavailable("Provider returned an unapproved customer handoff.")
    return url


def _unknown_result(envelope, *, reason_code):
    evidence_hash = hashlib.sha256(
        f"{envelope.request_fingerprint}:{envelope.claim_token}:{reason_code}".encode("utf-8")
    ).hexdigest()
    return NormalizedProviderResult(
        outcome=ProviderRequestOutcome.OUTCOME_UNKNOWN,
        evidence_hash=evidence_hash,
        reason_code=reason_code,
        safe_metadata={"result_category": "transport_uncertain"},
    )


def _protocol_result(envelope):
    evidence_hash = hashlib.sha256(
        f"{envelope.request_fingerprint}:{envelope.claim_token}:protocol_failure".encode("utf-8")
    ).hexdigest()
    return NormalizedProviderResult(
        outcome=ProviderRequestOutcome.PROTOCOL_FAILURE,
        evidence_hash=evidence_hash,
        reason_code="provider_protocol_failure",
        safe_metadata={"result_category": "protocol_failure"},
    )


def _execute_provider(*, adapter, envelope):
    try:
        result = execute_adapter_outside_transaction(adapter=adapter, envelope=envelope)
    except TimeoutError:
        return _unknown_result(envelope, reason_code="provider_timeout")
    except ValidationError:
        return _protocol_result(envelope)
    except Exception:
        return _unknown_result(envelope, reason_code="provider_transport_failure")
    if not isinstance(result, NormalizedProviderResult):
        return _protocol_result(envelope)
    if not result.evidence_hash or len(str(result.evidence_hash)) != 64:
        return _protocol_result(envelope)
    if result.outcome == ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED:
        try:
            _validate_customer_action_url(provider=envelope.provider_key, url=result.customer_action_url)
        except DigitalPaymentProviderUnavailable:
            return _protocol_result(envelope)
    elif result.customer_action_url:
        return _protocol_result(envelope)
    return result


def _root_record(*, checkout, root_key):
    return IdempotencyRecord.objects.filter(scope=_root_scope(checkout), key=str(root_key)).first()


def _root_identity(record):
    identity = record.safe_response.get("request_identity") if isinstance(record.safe_response, dict) else None
    if not isinstance(identity, dict) or canonical_request_hash(identity) != record.request_hash:
        raise DigitalPaymentRequestConflict("Payment request identity is incomplete or contradictory.")
    return identity


def _assert_root_request(*, record, identity, checkout, actor, provider):
    if (
        identity.get("checkout_id") != checkout.pk
        or identity.get("checkout_public_id") != str(checkout.public_id)
        or identity.get("customer_id") != actor.pk
        or identity.get("provider") != str(provider)
        or canonical_request_hash(identity) != record.request_hash
    ):
        raise DigitalPaymentRequestConflict("Payment request key was reused with different input.")


def _begin_root(*, checkout, root_key, identity):
    request_hash = canonical_request_hash(identity)
    scope = _root_scope(checkout)
    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().filter(scope=scope, key=str(root_key)).first()
        if record is None:
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        scope=scope,
                        key=str(root_key),
                        request_hash=request_hash,
                        safe_response={"request_identity": identity},
                    )
                return record, True
            except (IntegrityError, ValidationError):
                record = IdempotencyRecord.objects.select_for_update().filter(
                    scope=scope, key=str(root_key)
                ).first()
                if record is None:
                    raise
        if record.request_hash != request_hash or _root_identity(record) != identity:
            raise DigitalPaymentRequestConflict("Payment request key was reused with different input.")
        return record, False


def _complete_root(*, checkout, root_key, request_hash, transaction_obj, customer_action_url):
    record = IdempotencyRecord.objects.select_for_update().get(
        scope=_root_scope(checkout), key=str(root_key)
    )
    if record.request_hash != request_hash:
        raise DigitalPaymentRequestConflict("Payment request key was reused with different input.")
    if record.status == IdempotencyStatus.COMPLETED:
        return record
    if record.status != IdempotencyStatus.IN_PROGRESS:
        raise DigitalPaymentRequestConflict("Payment request cannot be completed from its current state.")
    identity = _root_identity(record)
    record.status = IdempotencyStatus.COMPLETED
    record.result_type = transaction_obj._meta.label_lower
    record.result_id = str(transaction_obj.pk)
    record.safe_response = {
        "request_identity": identity,
        "checkout_public_id": str(checkout.public_id),
        "transaction_public_id": str(transaction_obj.public_id),
        "customer_action_url": customer_action_url,
    }
    record.completed_at = timezone.now()
    record.save(
        update_fields=(
            "status",
            "result_type",
            "result_id",
            "safe_response",
            "completed_at",
            "updated_at",
        )
    )
    return record


def _placed_graph(checkout):
    orders = list(Order.objects.filter(checkout=checkout).select_related("financial_payment").order_by("pk"))
    if not orders:
        return None, None
    if len(orders) != 1 or not hasattr(orders[0], "financial_payment"):
        raise DigitalPaymentNotReady("Checkout placement graph is contradictory.")
    return orders[0], orders[0].financial_payment


def _original_placement_identity(*, checkout, payment):
    try:
        source = payment.obligation_source
    except AttributeError as exc:
        raise DigitalPaymentNotReady("Checkout placement evidence is missing.") from exc
    if (
        source.source_kind != PaymentObligationSourceKind.CHECKOUT_PLACEMENT
        or source.source_model != "shop.Checkout"
        or source.source_object_id != str(checkout.pk)
        or source.source_field != "computed_payable_total"
        or source.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS
    ):
        raise DigitalPaymentNotReady("Checkout placement evidence is contradictory.")

    matches = []
    records = IdempotencyRecord.objects.filter(scope=_root_scope(checkout)).only(
        "key", "request_hash", "safe_response"
    )
    for record in records:
        try:
            placement_key = _stage_key(record.key, record.request_hash, "placement")
        except (TypeError, ValueError):
            continue
        if placement_key != source.idempotency_key:
            continue
        identity = _root_identity(record)
        if (
            identity.get("checkout_id") != checkout.pk
            or identity.get("checkout_public_id") != str(checkout.public_id)
            or identity.get("customer_id") != checkout.user_id
        ):
            raise DigitalPaymentRequestConflict("Original placement identity is contradictory.")
        matches.append(identity)
    if len(matches) != 1:
        raise DigitalPaymentRequestConflict("Original placement identity is unavailable or ambiguous.")
    return matches[0]


def request_digital_checkout_payment(
    *,
    checkout_public_id,
    actor,
    provider,
    idempotency_key,
    adapter_registry=PRODUCTION_ADAPTER_REGISTRY,
):
    try:
        root_key = UUID(str(idempotency_key))
    except (TypeError, ValueError) as exc:
        raise DigitalPaymentAdapterError("A valid idempotency UUID is required.") from exc
    checkout = Checkout.objects.filter(public_id=checkout_public_id, user=actor).first()
    if checkout is None:
        raise DigitalPaymentNotFound("Checkout was not found.")

    root = _root_record(checkout=checkout, root_key=root_key)
    if root is not None:
        identity = _root_identity(root)
        _assert_root_request(
            record=root,
            identity=identity,
            checkout=checkout,
            actor=actor,
            provider=provider,
        )
        if root.status != IdempotencyStatus.COMPLETED:
            revision = _validate_checkout(checkout, user_id=actor.pk, allow_placed=True)
            if revision != identity["commercial_revision"]:
                raise DigitalPaymentRequestConflict("Checkout commercial revision changed.")
            account, adapter = _resolve_frozen_provider(identity, adapter_registry=adapter_registry)
        else:
            return DigitalPaymentRequestResult(
                checkout_public_id=checkout.public_id,
                customer_action_url=str(root.safe_response.get("customer_action_url", "")),
                replayed=True,
            )
    else:
        revision = _validate_checkout(checkout, user_id=actor.pk, allow_placed=True)
        account, adapter = _resolve_provider(provider, adapter_registry=adapter_registry)
        order, payment = _placed_graph(checkout)
        if payment is None:
            placement_checkout_version = checkout.version
            placement_commercial_revision = revision
        else:
            placement_identity = _original_placement_identity(checkout=checkout, payment=payment)
            placement_checkout_version = placement_identity["placement_checkout_version"]
            placement_commercial_revision = placement_identity["commercial_revision"]
            if revision != placement_commercial_revision:
                raise DigitalPaymentRequestConflict("Checkout commercial revision changed after placement.")
        identity = _request_payload(
            checkout=checkout,
            actor=actor,
            provider=provider,
            account=account,
            commercial_revision=placement_commercial_revision,
            placement_checkout_version=placement_checkout_version,
        )
        root, created = _begin_root(checkout=checkout, root_key=root_key, identity=identity)
        if not created:
            _assert_root_request(
                record=root,
                identity=_root_identity(root),
                checkout=checkout,
                actor=actor,
                provider=provider,
            )
            if root.status == IdempotencyStatus.COMPLETED:
                return DigitalPaymentRequestResult(
                    checkout_public_id=checkout.public_id,
                    customer_action_url=str(root.safe_response.get("customer_action_url", "")),
                    replayed=True,
                )
        identity = _root_identity(root)

    request_hash = root.request_hash
    placement_key = _stage_key(root_key, request_hash, "placement")
    attempt_key = _stage_key(root_key, request_hash, "attempt")
    transaction_key = _stage_key(root_key, request_hash, "transaction")
    claim_key = _stage_key(root_key, request_hash, "claim")
    result_key = _stage_key(root_key, request_hash, "result")
    order, payment = _placed_graph(checkout)
    if payment is None:
        revision = _validate_checkout(checkout, user_id=actor.pk)
        if (
            revision != identity["commercial_revision"]
            or checkout.version != identity["placement_checkout_version"]
        ):
            raise DigitalPaymentRequestConflict("Checkout placement identity changed.")
        placement = place_order_and_create_payment_obligation(
            checkout_id=checkout.pk,
            expected_user_id=actor.pk,
            expected_checkout_version=identity["placement_checkout_version"],
            source_unit=settings.PAYMENT_AMOUNT_UNIT,
            idempotency_key=placement_key,
            actor_type=FinancialActorType.CUSTOMER,
            actor_id=actor.pk,
        )
        order, payment = placement.order, placement.payment

    attempts = list(payment.attempts.prefetch_related("transactions__request_results").order_by("sequence"))
    attempt = attempts[-1] if attempts else None
    transaction_obj = None
    matching_attempt = next((item for item in attempts if item.idempotency_key == attempt_key), None)
    if matching_attempt is not None:
        if matching_attempt is not attempt:
            raise DigitalPaymentRequestConflict("Payment request stage is no longer current.")
        attempt_result = create_or_replay_payment_attempt(
            payment_id=payment.pk,
            merchant_account_version_id=account.pk,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=payment.amount_due - payment.confirmed_amount,
            idempotency_key=attempt_key,
            actor_type=FinancialActorType.CUSTOMER,
            actor_id=actor.pk,
        )
        attempt = attempt_result.attempt
    elif attempt and attempt.status == PaymentAttemptStatus.PROCESSING:
        transactions = list(attempt.transactions.all())
        candidate = transactions[-1] if transactions else None
        if candidate and candidate.status == PaymentTransactionStatus.CREATED and candidate.request_results.filter(
            outcome=ProviderRequestOutcome.NO_EFFECT_RETRYABLE
        ).exists():
            transaction_obj = candidate
        else:
            raise DigitalPaymentRequestConflict("A blocking payment attempt already exists.")
    elif attempt and attempt.status in (
        PaymentAttemptStatus.CREATED,
        PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
        PaymentAttemptStatus.SUCCEEDED,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
        PaymentAttemptStatus.REVIEW,
    ):
        raise DigitalPaymentRequestConflict("A blocking payment attempt already exists.")

    if transaction_obj is None and matching_attempt is None:
        attempt_result = create_or_replay_payment_attempt(
            payment_id=payment.pk,
            merchant_account_version_id=account.pk,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=payment.amount_due - payment.confirmed_amount,
            idempotency_key=attempt_key,
            actor_type=FinancialActorType.CUSTOMER,
            actor_id=actor.pk,
        )
        attempt = attempt_result.attempt

    if transaction_obj is None:
        transaction_result = create_or_replay_request_transaction(
            attempt_id=attempt.pk,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=transaction_key,
            correlation_id=root_key,
            causation_id=checkout.public_id,
            actor_type=FinancialActorType.CUSTOMER,
            actor_id=actor.pk,
        )
        transaction_obj = transaction_result.transaction

    claim = claim_provider_request(
        transaction_id=transaction_obj.pk,
        claim_idempotency_key=claim_key,
        actor_type=FinancialActorType.CUSTOMER,
        actor_id=actor.pk,
    )
    if claim.replayed:
        root = _root_record(checkout=checkout, root_key=root_key)
        if root and root.status == IdempotencyStatus.COMPLETED:
            return DigitalPaymentRequestResult(
                checkout_public_id=checkout.public_id,
                customer_action_url=str(root.safe_response.get("customer_action_url", "")),
                replayed=True,
            )
        raise DigitalPaymentRequestInProgress("Payment provider request is already in progress.")

    provider_result = _execute_provider(adapter=adapter, envelope=claim.envelope)
    customer_action_url = (
        provider_result.customer_action_url
        if provider_result.outcome == ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED
        else ""
    )
    with transaction.atomic():
        apply_provider_request_result(
            transaction_id=transaction_obj.pk,
            claim_token=claim.claim.claim_token,
            outcome=provider_result.outcome,
            evidence_hash=provider_result.evidence_hash,
            result_idempotency_key=result_key,
            reason_code=provider_result.reason_code,
            safe_metadata=provider_result.safe_metadata,
            actor_type=FinancialActorType.CUSTOMER,
            actor_id=actor.pk,
        )
        _complete_root(
            checkout=checkout,
            root_key=root_key,
            request_hash=request_hash,
            transaction_obj=transaction_obj,
            customer_action_url=customer_action_url,
        )
    return DigitalPaymentRequestResult(
        checkout_public_id=checkout.public_id,
        customer_action_url=customer_action_url,
        replayed=False,
    )


__all__ = (
    "DigitalPaymentAdapterError",
    "DigitalPaymentNotFound",
    "DigitalPaymentNotReady",
    "DigitalPaymentProviderUnavailable",
    "DigitalPaymentRequestConflict",
    "DigitalPaymentRequestInProgress",
    "request_digital_checkout_payment",
)
