"""Dormant launch adapters for immutable satisfaction evidence.

These commands normalize already-authoritative fulfillment completion.  They
do not recognize revenue, post journals, or mutate fulfillment/commercial
state.
"""

from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from cheatgame.digital_products.models import (
    DigitalEntitlementStatus,
    DigitalFulfillmentItem,
    DigitalFulfillmentStatus,
    InstalledGameClassification,
    InstalledGameRecord,
    InstalledGameRecordState,
)
from cheatgame.financial_core.models import (
    FinancialActorType,
    PerformanceObligation,
    PerformanceObligationType,
    SatisfactionEvidence,
    SatisfactionEvidenceAuthority,
    SatisfactionEvidenceClassification,
    StandardFulfillmentObligation,
)
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.users.models import BaseUser, UserTypes


STANDARD_DELIVERY_COMPLETED = "STANDARD_DELIVERY_COMPLETED"
DIGITAL_FULFILLMENT_COMPLETED = "DIGITAL_FULFILLMENT_COMPLETED"


class SatisfactionEvidenceError(Exception):
    pass


class SatisfactionEvidenceConflict(SatisfactionEvidenceError):
    pass


@dataclass(frozen=True)
class SatisfactionEvidenceResult:
    evidence: SatisfactionEvidence
    replayed: bool


def _uuid(value, label):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SatisfactionEvidenceError(f"{label} must be a UUID.") from exc


def _staff_operator(operator):
    if not isinstance(operator, BaseUser) or not operator.pk:
        raise PermissionDenied("An authenticated fulfillment operator is required.")
    persisted = BaseUser.objects.filter(pk=operator.pk).values("user_type", "is_active").first()
    if not persisted or not persisted["is_active"] or persisted["user_type"] not in (
        UserTypes.ADMIN,
        UserTypes.MANAGER,
    ):
        raise PermissionDenied("An active authorized fulfillment operator is required.")
    return operator.pk


def _component_for(*, fulfillment_field, fulfillment_id):
    matches = list(
        PerformanceObligation.objects.select_related(
            "finalization", "order", "recognition_policy_version"
        ).filter(**{f"components__{fulfillment_field}_id": fulfillment_id})[:2]
    )
    if len(matches) != 1:
        raise SatisfactionEvidenceConflict("Fulfillment must map to one performance obligation.")
    obligation = matches[0]
    components = list(obligation.components.select_related("order_item", "checkout_line")[:2])
    if len(components) != 1 or getattr(components[0], f"{fulfillment_field}_id") != fulfillment_id:
        raise SatisfactionEvidenceConflict("Performance-obligation component lineage is incoherent.")
    return obligation, components[0]


def _persist(*, obligation, fulfillment, contract, source_domain, source_type,
             source_public_id, operator_id, occurred_at, immutable_identity,
             idempotency_key, correlation_id, causation_id, standard):
    key = _uuid(idempotency_key, "idempotency_key")
    correlation = _uuid(correlation_id, "correlation_id")
    causation = None if causation_id is None else _uuid(causation_id, "causation_id")
    source_hash = canonical_request_hash({"contract": contract, "evidence": immutable_identity})
    fingerprint = canonical_request_hash({
        "contract": contract,
        "obligation": str(obligation.public_id),
        "fulfillment": str(fulfillment.public_id),
        "operator_id": operator_id,
        "source_evidence_hash": source_hash,
    })
    existing_key = SatisfactionEvidence.objects.filter(idempotency_key=key).first()
    if existing_key:
        if existing_key.request_fingerprint != fingerprint:
            raise SatisfactionEvidenceConflict("Idempotency key was reused for different evidence.")
        return SatisfactionEvidenceResult(existing_key, True)
    existing = SatisfactionEvidence.objects.filter(
        obligation=obligation,
        evidence_contract_version=contract,
        source_aggregate_id=str(source_public_id),
        source_event_id=str(source_public_id),
    ).first()
    if existing:
        if existing.source_evidence_hash != source_hash:
            raise SatisfactionEvidenceConflict("Authoritative completion evidence is contradictory.")
        return SatisfactionEvidenceResult(existing, True)
    values = dict(
        obligation=obligation,
        evidence_classification=SatisfactionEvidenceClassification.POINT_IN_TIME_SATISFIED,
        source_domain=source_domain,
        source_aggregate_type=source_type,
        source_aggregate_id=str(source_public_id),
        source_event_id=str(source_public_id),
        source_event_version=1,
        evidence_contract_version=contract,
        satisfied_quantity=obligation.quantity_basis,
        occurred_at=occurred_at,
        evidence_authority=SatisfactionEvidenceAuthority.STAFF,
        actor_type=FinancialActorType.ADMIN,
        actor_id=operator_id,
        source_evidence_hash=source_hash,
        request_fingerprint=fingerprint,
        idempotency_key=key,
        correlation_id=correlation,
        causation_id=causation,
    )
    values["standard_fulfillment_obligation" if standard else "digital_fulfillment_obligation"] = fulfillment
    try:
        with transaction.atomic():
            evidence = SatisfactionEvidence.objects.create(**values)
    except (IntegrityError, ValidationError):
        evidence = SatisfactionEvidence.objects.filter(
            obligation=obligation,
            evidence_contract_version=contract,
            source_aggregate_id=str(source_public_id),
            source_event_id=str(source_public_id),
        ).first()
        if not evidence or evidence.source_evidence_hash != source_hash:
            raise SatisfactionEvidenceConflict("Concurrent completion evidence is contradictory.")
        return SatisfactionEvidenceResult(evidence, True)
    return SatisfactionEvidenceResult(evidence, False)


@transaction.atomic
def complete_standard_fulfillment(*, fulfillment_obligation_public_id, operator,
                                  idempotency_key, correlation_id, causation_id=None):
    operator_id = _staff_operator(operator)
    fulfillment = StandardFulfillmentObligation.objects.select_for_update().select_related(
        "finalization", "order", "order_item", "reservation", "product"
    ).filter(public_id=_uuid(fulfillment_obligation_public_id, "fulfillment_obligation_public_id")).first()
    if not fulfillment:
        raise SatisfactionEvidenceError("Standard fulfillment obligation was not found.")
    obligation, component = _component_for(
        fulfillment_field="standard_fulfillment_obligation", fulfillment_id=fulfillment.pk
    )
    if (
        obligation.finalization_id != fulfillment.finalization_id
        or obligation.order_id != fulfillment.order_id
        or obligation.obligation_type != PerformanceObligationType.PHYSICAL_GOOD
        or obligation.commerce_authority != "standard_commerce"
        or component.order_id != fulfillment.order_id
        or component.order_item_id != fulfillment.order_item_id
        or component.quantity != fulfillment.quantity
        or operator_id == fulfillment.order.user_id
    ):
        raise SatisfactionEvidenceConflict("Standard completion lineage is incoherent.")
    identity = {
        "finalization": str(fulfillment.finalization.public_id),
        "order_id": fulfillment.order_id,
        "order_item_id": fulfillment.order_item_id,
        "fulfillment": str(fulfillment.public_id),
        "reservation_id": fulfillment.reservation_id,
        "product_id": fulfillment.product_id,
        "quantity": fulfillment.quantity,
        "operator_id": operator_id,
        "policy_id": obligation.recognition_policy_version_id,
    }
    from django.utils import timezone
    return _persist(
        obligation=obligation, fulfillment=fulfillment, contract=STANDARD_DELIVERY_COMPLETED,
        source_domain="standard_fulfillment", source_type="standard_fulfillment_obligation",
        source_public_id=fulfillment.public_id, operator_id=operator_id,
        occurred_at=timezone.now(), immutable_identity=identity,
        idempotency_key=idempotency_key, correlation_id=correlation_id,
        causation_id=causation_id, standard=True,
    )


@transaction.atomic
def normalize_digital_fulfillment_completion(*, fulfillment_item_public_id,
                                             idempotency_key, correlation_id,
                                             causation_id=None):
    item = DigitalFulfillmentItem.objects.select_for_update().filter(
        public_id=_uuid(fulfillment_item_public_id, "fulfillment_item_public_id")
    ).first()
    if not item:
        raise SatisfactionEvidenceError("Digital fulfillment item was not found.")
    item = DigitalFulfillmentItem.objects.select_related(
        "obligation__finalization", "obligation__order", "obligation__checkout_line__digital_snapshot",
        "entitlement",
    ).get(pk=item.pk)
    obligation, component = _component_for(
        fulfillment_field="digital_fulfillment_obligation", fulfillment_id=item.obligation_id
    )
    purchased = list(InstalledGameRecord.objects.filter(
        fulfillment_item=item,
        classification=InstalledGameClassification.PURCHASED,
        state=InstalledGameRecordState.RECORDED,
        superseded_by__isnull=True,
    ).select_related("operator", "delivered_version")[:2])
    if len(purchased) != 1 or purchased[0].operator_id is None:
        raise SatisfactionEvidenceConflict("One staff-authored current purchased installation is required.")
    record = purchased[0]
    operator_id = _staff_operator(record.operator)
    entitlement = item.entitlement
    snapshot = item.obligation.checkout_line.digital_snapshot
    if (
        item.status != DigitalFulfillmentStatus.COMPLETED
        or item.completed_at is None
        or entitlement.status != DigitalEntitlementStatus.ACTIVE
        or entitlement.activated_at is None
        or entitlement.obligation_id != item.obligation_id
        or entitlement.customer_id != item.obligation.order.user_id
        or obligation.finalization_id != item.obligation.finalization_id
        or obligation.order_id != item.obligation.order_id
        or obligation.obligation_type != PerformanceObligationType.DIGITAL_ACCESS_INSTALLATION
        or obligation.commerce_authority != "digital_products"
        or component.order_id != item.obligation.order_id
        or component.order_item_id != item.obligation.order_item_id
        or component.checkout_line_id != item.obligation.checkout_line_id
        or component.quantity != item.obligation.quantity
        or record.game_id != snapshot.product_id
        or record.delivered_version_id != snapshot.delivered_version_id
        or snapshot.inventory_pool_id != item.obligation.inventory_pool_id
        or snapshot.capacity is None
        or snapshot.customer_console is None
        or item.current_fulfillment_method not in ("in_store", "remote")
    ):
        raise SatisfactionEvidenceConflict("Digital completion lineage is incoherent.")
    identity = {
        "finalization": str(item.obligation.finalization.public_id),
        "order_id": item.obligation.order_id,
        "order_item_id": item.obligation.order_item_id,
        "fulfillment_obligation": str(item.obligation.public_id),
        "fulfillment_item": str(item.public_id),
        "installed_game_record_id": record.pk,
        "entitlement_id": entitlement.pk,
        "operator_id": operator_id,
        "inventory_pool_id": snapshot.inventory_pool_id,
        "capacity": snapshot.capacity,
        "console": snapshot.customer_console,
        "delivered_version_id": snapshot.delivered_version_id,
        "fulfillment_method": item.current_fulfillment_method,
        "completed_at": item.completed_at,
        "policy_id": obligation.recognition_policy_version_id,
    }
    return _persist(
        obligation=obligation, fulfillment=item.obligation, contract=DIGITAL_FULFILLMENT_COMPLETED,
        source_domain="digital_fulfillment", source_type="digital_fulfillment_item",
        source_public_id=item.public_id, operator_id=operator_id, occurred_at=item.completed_at,
        immutable_identity=identity, idempotency_key=idempotency_key,
        correlation_id=correlation_id, causation_id=causation_id, standard=False,
    )
