from uuid import NAMESPACE_URL, UUID, uuid5

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalEntitlementStatus,
    DigitalFulfillmentItem,
    DigitalFulfillmentStatus,
    DigitalFulfillmentWaitingReason,
    DigitalInventoryReservationState,
    DigitalOfferCapacity,
    Entitlement,
    FulfillmentActivity,
    FulfillmentActivityActorType,
    FulfillmentActivityType,
    FulfillmentActivityVisibility,
    FulfillmentActorAuthority,
    InstalledGameClassification,
    InstalledGameCompletionSource,
    InstalledGameRecord,
    InstalledGameRecordState,
    validate_fulfillment_safe_text,
)
from cheatgame.financial_core.models import (
    DigitalFulfillmentObligation,
    IdempotencyRecord,
    IdempotencyStatus,
    PaymentCollectionStatus,
)
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.shop.models import FulfillmentStatus, OrderStatus
from cheatgame.users.models import UserTypes


class DigitalFulfillmentError(Exception):
    code = "digital_fulfillment_error"


class DigitalFulfillmentConflict(DigitalFulfillmentError):
    code = "digital_fulfillment_conflict"


class DigitalFulfillmentValidationError(DigitalFulfillmentError):
    code = "invalid_digital_fulfillment_command"


def _uuid(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise DigitalFulfillmentValidationError("A valid UUID is required.") from exc


def _derived_key(key, suffix):
    return uuid5(NAMESPACE_URL, f"digital-fulfillment:{_uuid(key)}:{suffix}")


def _safe_text(value, *, required=False, max_length=1000):
    value = str(value or "").strip()
    if required and not value:
        raise DigitalFulfillmentValidationError("A nonempty note is required.")
    if len(value) > max_length:
        raise DigitalFulfillmentValidationError("Text exceeds the permitted length.")
    try:
        validate_fulfillment_safe_text(value)
    except Exception as exc:
        raise DigitalFulfillmentValidationError("Credential-like material is prohibited.") from exc
    return value


def _graph(obligation):
    try:
        finalization = obligation.finalization
        payment = finalization.payment
        order = obligation.order
        order_item = obligation.order_item
        reservation = obligation.reservation
        snapshot = obligation.checkout_line.digital_snapshot
    except (AttributeError, ObjectDoesNotExist) as exc:
        raise DigitalFulfillmentConflict("The commercial ownership graph is incomplete.") from exc
    coherent = (
        finalization.order_id == order.id
        and payment.order_id == order.id
        and order_item.order_id == order.id
        and order.checkout_id == obligation.checkout_line.checkout_id
        and reservation.order_id == order.id
        and reservation.checkout_line_id == obligation.checkout_line_id
        and reservation.inventory_pool_id == obligation.inventory_pool_id
        and snapshot.inventory_pool_id == obligation.inventory_pool_id
        and snapshot.product_id == order_item.product_id
        and obligation.quantity == 1
        and snapshot.quantity == 1
        and reservation.state == DigitalInventoryReservationState.CONSUMED
    )
    if not coherent:
        raise DigitalFulfillmentConflict("The commercial ownership graph is contradictory.")
    if payment.collection_status != PaymentCollectionStatus.PAID or order.payment_status != OrderStatus.PAID:
        raise DigitalFulfillmentConflict("Commercial payment is not finalized as paid.")
    return payment, order, order_item, reservation, snapshot


def _fingerprint(command, item, actor=None, **payload):
    normalized = {
        "operation": f"digital-fulfillment.{command}.v2",
        "fulfillment_id": str(item.public_id),
        "actor_id": getattr(actor, "pk", None),
    }
    normalized.update(payload)
    return canonical_request_hash(normalized)


def _system_fingerprint(command, **payload):
    normalized = {"operation": f"digital-fulfillment.{command}.v2"}
    normalized.update(payload)
    return canonical_request_hash(normalized)


def _staff(item, actor, *, allow_admin_override=True):
    if not actor or not actor.is_active or actor.user_type not in (UserTypes.MANAGER, UserTypes.ADMIN):
        raise PermissionDenied("An active Manager or Admin is required.")
    if item.assigned_operator_id:
        if item.assigned_operator_id == actor.pk:
            return FulfillmentActorAuthority.ASSIGNED_OPERATOR
        if allow_admin_override and actor.user_type == UserTypes.ADMIN:
            return FulfillmentActorAuthority.ADMIN_OVERRIDE
        raise PermissionDenied("Only the assigned operator may perform this command.")
    return FulfillmentActorAuthority.UNASSIGNED_STAFF


def _customer_authority(item, actor):
    if (
        not actor
        or not actor.is_active
        or actor.pk != item.obligation.order.user_id
        or actor.user_type != UserTypes.CUSTOMER
    ):
        raise DigitalFulfillmentValidationError("Fulfillment item was not found.")
    return FulfillmentActorAuthority.CUSTOMER_OWNER


def _append_activity(
    *, item, activity_type, actor_type, actor=None, actor_authority, visibility, key,
    request_fingerprint, note="", previous=None, new=None, waiting=None,
):
    key = _uuid(key)
    existing = FulfillmentActivity.objects.filter(idempotency_key=key).first()
    semantics = (
        activity_type, actor_type, getattr(actor, "pk", None), actor_authority, item.pk,
        note, previous, new, waiting, visibility, request_fingerprint,
    )
    if existing:
        actual = (
            existing.activity_type, existing.actor_type, existing.actor_id,
            existing.actor_authority, existing.fulfillment_item_id, existing.note,
            existing.previous_status, existing.new_status, existing.waiting_reason,
            existing.visibility, existing.request_fingerprint,
        )
        if actual != semantics:
            raise DigitalFulfillmentConflict("Idempotency key was reused with different activity semantics.")
        return existing
    return FulfillmentActivity.objects.create(
        fulfillment_item=item,
        activity_type=activity_type,
        actor_type=actor_type,
        actor=actor,
        actor_authority=actor_authority,
        visibility=visibility,
        note=note,
        previous_status=previous,
        new_status=new,
        waiting_reason=waiting,
        idempotency_key=key,
        request_fingerprint=request_fingerprint,
    )


def _activity_replay(*, item, key, activity_type, request_fingerprint):
    existing = FulfillmentActivity.objects.filter(idempotency_key=_uuid(key)).first()
    if not existing:
        return None
    if (
        existing.fulfillment_item_id != item.pk
        or existing.activity_type != activity_type
        or existing.request_fingerprint != request_fingerprint
    ):
        raise DigitalFulfillmentConflict("Idempotency key was reused with different command semantics.")
    return existing


def _installed_replay(*, item, key, classification, request_fingerprint):
    existing = InstalledGameRecord.objects.filter(idempotency_key=_uuid(key)).first()
    if not existing:
        return None
    if (
        existing.fulfillment_item_id != item.pk
        or existing.classification != classification
        or existing.request_fingerprint != request_fingerprint
    ):
        raise DigitalFulfillmentConflict("Idempotency key was reused with different evidence semantics.")
    return existing


def _locked_item(public_id):
    try:
        base = DigitalFulfillmentItem.objects.all()
        item = base.only("pk").get(public_id=_uuid(public_id))
    except DigitalFulfillmentItem.DoesNotExist as exc:
        raise DigitalFulfillmentValidationError("Fulfillment item was not found.") from exc
    return lock_one(queryset=base, rank=LockRank.FULFILLMENT, pk=item.pk)


def _require_status(item, *statuses):
    if item.status not in statuses:
        raise DigitalFulfillmentValidationError("Command is not allowed in the current fulfillment state.")


def _has_activity(item, *activity_types):
    return item.activities.filter(activity_type__in=activity_types).exists()


def _current_purchased(item, *, lock=False):
    queryset = item.installed_games.filter(
        classification=InstalledGameClassification.PURCHASED,
        state=InstalledGameRecordState.RECORDED,
        superseded_by__isnull=True,
    )
    # The parent fulfillment row is always locked before this query. Selecting
    # through the nullable reverse supersession join cannot use FOR UPDATE on
    # PostgreSQL, and the parent lock already serializes evidence commands.
    records = list(queryset[:2])
    if len(records) > 1:
        raise DigitalFulfillmentConflict("Multiple current purchased evidence records exist.")
    return records[0] if records else None


@transaction.atomic
def provision_digital_fulfillment_obligation(*, obligation_public_id, idempotency_key):
    """Dormant explicit intake. No signal, task, API, or finalizer hook invokes it."""
    key = _uuid(idempotency_key)
    with ordered_lock_scope():
        try:
            candidate = DigitalFulfillmentObligation.objects.get(public_id=_uuid(obligation_public_id))
        except DigitalFulfillmentObligation.DoesNotExist as exc:
            raise DigitalFulfillmentValidationError("Digital fulfillment obligation was not found.") from exc
        obligation = lock_one(
            queryset=DigitalFulfillmentObligation.objects.all(),
            rank=LockRank.COMMERCIAL_RESOURCE,
            pk=candidate.pk,
        )
        _graph(obligation)
        request_hash = canonical_request_hash({"operation": "provision-v2", "obligation": str(obligation.public_id)})
        register_lock(LockRank.FULFILLMENT, f"idempotency:{key}")
        record = IdempotencyRecord.objects.select_for_update().filter(
            scope="digital_fulfillment.provision", key=str(key),
        ).first()
        if record and record.request_hash != request_hash:
            raise DigitalFulfillmentConflict("Idempotency key was reused for another obligation.")
        item = DigitalFulfillmentItem.objects.filter(obligation=obligation).first()
        entitlement = Entitlement.objects.filter(obligation=obligation).first()
        initials = list(FulfillmentActivity.objects.filter(
            fulfillment_item=item, activity_type=FulfillmentActivityType.PROVISIONED,
        )[:2]) if item else []
        if any((item, entitlement, initials)) and not (item and entitlement and len(initials) == 1):
            raise DigitalFulfillmentConflict("Contradictory partial operational state exists.")
        if item:
            if entitlement.fulfillment_item_id != item.pk or entitlement.customer_id != obligation.order.user_id:
                raise DigitalFulfillmentConflict("Existing operational ownership is contradictory.")
            if record is not None:
                if (
                    record.status != IdempotencyStatus.COMPLETED
                    or record.result_type != "digital_fulfillment_item"
                    or record.result_id != str(item.public_id)
                ):
                    raise DigitalFulfillmentConflict("Provisioning idempotency state is incomplete or contradictory.")
            else:
                IdempotencyRecord.objects.create(
                    scope="digital_fulfillment.provision",
                    key=str(key),
                    request_hash=request_hash,
                    status=IdempotencyStatus.COMPLETED,
                    result_type="digital_fulfillment_item",
                    result_id=str(item.public_id),
                    safe_response={"fulfillment_id": str(item.public_id)},
                    completed_at=timezone.now(),
                )
            return item
        if record is None:
            record = IdempotencyRecord.objects.create(
                scope="digital_fulfillment.provision", key=str(key), request_hash=request_hash,
            )
        elif record.status != IdempotencyStatus.IN_PROGRESS:
            raise DigitalFulfillmentConflict("Provisioning replay has no coherent result.")
        item = DigitalFulfillmentItem.objects.create(
            obligation=obligation,
            current_fulfillment_method=obligation.fulfillment_method,
            status=DigitalFulfillmentStatus.QUEUED,
            waiting_reason=DigitalFulfillmentWaitingReason.CONTACT_REQUIRED,
        )
        Entitlement.objects.create(
            obligation=obligation,
            fulfillment_item=item,
            customer=obligation.order.user,
            status=DigitalEntitlementStatus.PENDING_FULFILLMENT,
        )
        initial_fingerprint = _system_fingerprint("provisioned", obligation=str(obligation.public_id))
        _append_activity(
            item=item,
            activity_type=FulfillmentActivityType.PROVISIONED,
            actor_type=FulfillmentActivityActorType.SYSTEM,
            actor_authority=FulfillmentActorAuthority.SYSTEM,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
            key=_derived_key(key, "provisioned"),
            request_fingerprint=initial_fingerprint,
        )
        record.status = IdempotencyStatus.COMPLETED
        record.result_type = "digital_fulfillment_item"
        record.result_id = str(item.public_id)
        record.safe_response = {"fulfillment_id": str(item.public_id)}
        record.completed_at = timezone.now()
        record.save(update_fields=("status", "result_type", "result_id", "safe_response", "completed_at", "updated_at"))
        return item


def _transition(item, *, status, waiting, actor, actor_type, actor_authority, key, request_fingerprint):
    previous = item.status
    if previous == status and item.waiting_reason == waiting:
        return item
    item.status = status
    item.waiting_reason = waiting
    item.save(update_fields=("status", "waiting_reason", "updated_at"))
    _append_activity(
        item=item,
        activity_type=FulfillmentActivityType.STATUS_CHANGED,
        actor_type=actor_type,
        actor=actor,
        actor_authority=actor_authority,
        visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
        key=key,
        request_fingerprint=request_fingerprint,
        previous=previous,
        new=status,
        waiting=waiting,
    )
    return item


@transaction.atomic
def assign_fulfillment_operator(*, fulfillment_id, operator, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        fingerprint = _fingerprint("assign-operator", item, actor, operator_id=getattr(operator, "pk", None))
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.OPERATOR_ASSIGNED, request_fingerprint=fingerprint):
            return item
        authority = _staff(item, actor)
        if item.status == DigitalFulfillmentStatus.COMPLETED:
            raise DigitalFulfillmentValidationError("Completed fulfillment cannot be reassigned.")
        if not operator.is_active or operator.user_type not in (UserTypes.MANAGER, UserTypes.ADMIN):
            raise DigitalFulfillmentValidationError("Operator must be active staff.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.OPERATOR_ASSIGNED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.INTERNAL, key=idempotency_key,
            request_fingerprint=fingerprint, note=f"operator:{operator.pk}",
        )
        item.assigned_operator = operator
        item.save(update_fields=("assigned_operator", "updated_at"))
        return item


@transaction.atomic
def record_customer_contact(*, fulfillment_id, actor, idempotency_key, contacted=True):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("record-contact", item, actor, contacted=bool(contacted))
        activity_type = FulfillmentActivityType.CUSTOMER_CONTACTED if contacted else FulfillmentActivityType.CUSTOMER_CONTACT_ATTEMPTED
        if _activity_replay(item=item, key=idempotency_key, activity_type=activity_type, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.QUEUED, DigitalFulfillmentStatus.WAITING_CUSTOMER)
        _append_activity(
            item=item, activity_type=activity_type, actor_type=FulfillmentActivityActorType.STAFF,
            actor=actor, actor_authority=authority, visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
            key=idempotency_key, request_fingerprint=fingerprint,
        )
        waiting = (
            DigitalFulfillmentWaitingReason.WAITING_FOR_CONSOLE
            if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
            else DigitalFulfillmentWaitingReason.ADDITIONAL_INFORMATION_REQUIRED
        )
        return _transition(
            item, status=DigitalFulfillmentStatus.WAITING_CUSTOMER, waiting=waiting,
            actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
            key=_derived_key(idempotency_key, "contact-state"), request_fingerprint=fingerprint,
        )


@transaction.atomic
def change_fulfillment_method(*, fulfillment_id, fulfillment_method, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("change-method", item, actor, fulfillment_method=fulfillment_method)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.METHOD_CHANGED, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.QUEUED, DigitalFulfillmentStatus.WAITING_CUSTOMER)
        snapshot = item.obligation.checkout_line.digital_snapshot
        if fulfillment_method not in DigitalCartFulfillmentMethod.values:
            raise DigitalFulfillmentValidationError("Unsupported fulfillment method.")
        if snapshot.capacity == DigitalOfferCapacity.CAPACITY_1 and fulfillment_method != DigitalCartFulfillmentMethod.IN_STORE:
            raise DigitalFulfillmentValidationError("Capacity 1 requires in-store fulfillment.")
        if _has_activity(item, FulfillmentActivityType.CONSOLE_RECEIVED, FulfillmentActivityType.WORK_STARTED):
            raise DigitalFulfillmentConflict("Method cannot change after method-specific evidence exists.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.METHOD_CHANGED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE, key=idempotency_key,
            request_fingerprint=fingerprint, note=f"method:{fulfillment_method}",
        )
        item.current_fulfillment_method = fulfillment_method
        if item.status == DigitalFulfillmentStatus.WAITING_CUSTOMER:
            item.waiting_reason = (
                DigitalFulfillmentWaitingReason.WAITING_FOR_CONSOLE
                if fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
                else DigitalFulfillmentWaitingReason.ADDITIONAL_INFORMATION_REQUIRED
            )
        item.save(update_fields=("current_fulfillment_method", "waiting_reason", "updated_at"))
        return item


@transaction.atomic
def record_console_received(*, fulfillment_id, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("console-received", item, actor)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.CONSOLE_RECEIVED, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.WAITING_CUSTOMER)
        if item.current_fulfillment_method != DigitalCartFulfillmentMethod.IN_STORE:
            raise DigitalFulfillmentValidationError("Console receipt applies only to in-store fulfillment.")
        if not _has_activity(item, FulfillmentActivityType.CUSTOMER_CONTACTED, FulfillmentActivityType.CUSTOMER_CONTACT_ATTEMPTED):
            raise DigitalFulfillmentValidationError("Customer-contact evidence is required first.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.CONSOLE_RECEIVED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE, key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        return _transition(
            item, status=DigitalFulfillmentStatus.READY_FOR_STAFF, waiting=None,
            actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
            key=_derived_key(idempotency_key, "console-state"), request_fingerprint=fingerprint,
        )


@transaction.atomic
def start_fulfillment_work(*, fulfillment_id, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("start-work", item, actor, method=item.current_fulfillment_method)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.WORK_STARTED, request_fingerprint=fingerprint):
            return item
        if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE:
            _require_status(item, DigitalFulfillmentStatus.READY_FOR_STAFF)
            if not _has_activity(item, FulfillmentActivityType.CONSOLE_RECEIVED):
                raise DigitalFulfillmentValidationError("Console receipt evidence is required.")
        else:
            _require_status(item, DigitalFulfillmentStatus.WAITING_CUSTOMER, DigitalFulfillmentStatus.READY_FOR_STAFF)
            if not _has_activity(item, FulfillmentActivityType.CUSTOMER_CONTACTED, FulfillmentActivityType.CUSTOMER_CONTACT_ATTEMPTED):
                raise DigitalFulfillmentValidationError("Customer-contact evidence is required first.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.WORK_STARTED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE, key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if item.started_at is None:
            item.started_at = timezone.now()
            item.save(update_fields=("started_at", "updated_at"))
        return _transition(
            item, status=DigitalFulfillmentStatus.IN_PROGRESS, waiting=None,
            actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
            key=_derived_key(idempotency_key, "work-state"), request_fingerprint=fingerprint,
        )


@transaction.atomic
def record_purchased_game_installation(
    *, fulfillment_id, actor, idempotency_key,
    completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        snapshot = item.obligation.checkout_line.digital_snapshot
        fingerprint = _fingerprint(
            "record-purchased", item, actor, completion_source=completion_source,
            product_id=snapshot.product_id, delivered_version_id=snapshot.delivered_version_id,
        )
        replay = _installed_replay(
            item=item, key=idempotency_key, classification=InstalledGameClassification.PURCHASED,
            request_fingerprint=fingerprint,
        )
        if replay:
            return replay
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS)
        expected_source = (
            InstalledGameCompletionSource.STAFF_INSTALLED
            if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
            else InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE
        )
        if completion_source != expected_source:
            raise DigitalFulfillmentValidationError("Evidence source does not match the operational method.")
        if _current_purchased(item, lock=True):
            raise DigitalFulfillmentConflict("Current purchased installation evidence already exists.")
        try:
            record = InstalledGameRecord.objects.create(
                fulfillment_item=item,
                game_id=snapshot.product_id,
                delivered_version_id=snapshot.delivered_version_id,
                classification=InstalledGameClassification.PURCHASED,
                completion_source=completion_source,
                operator=actor,
                actor_authority=authority,
                state=InstalledGameRecordState.RECORDED,
                idempotency_key=_uuid(idempotency_key),
                request_fingerprint=fingerprint,
            )
        except IntegrityError as exc:
            raise DigitalFulfillmentConflict("Purchased installation evidence conflicts with current evidence.") from exc
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.INSTALLATION_PERFORMED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
            key=_derived_key(idempotency_key, "installation"), request_fingerprint=fingerprint,
        )
        return record


def _superseded_purchased_replay(*, item, idempotency_key, state, request_fingerprint):
    existing = _installed_replay(
        item=item,
        key=idempotency_key,
        classification=InstalledGameClassification.PURCHASED,
        request_fingerprint=request_fingerprint,
    )
    if existing and existing.state != state:
        raise DigitalFulfillmentConflict("Idempotency key was reused with different evidence semantics.")
    return existing


def _supersede_purchased(
    *, item, actor, authority, idempotency_key, state, completion_source, reason,
    request_fingerprint,
):
    current = _current_purchased(item, lock=True)
    if not current:
        raise DigitalFulfillmentValidationError("No current purchased evidence exists.")
    return InstalledGameRecord.objects.create(
        fulfillment_item=item,
        game=current.game,
        delivered_version=current.delivered_version,
        classification=InstalledGameClassification.PURCHASED,
        completion_source=completion_source,
        operator=actor,
        actor_authority=authority,
        state=state,
        corrects=current,
        correction_reason=reason,
        idempotency_key=_uuid(idempotency_key),
        request_fingerprint=request_fingerprint,
    )


@transaction.atomic
def correct_purchased_game_installation(
    *, fulfillment_id, actor, idempotency_key, completion_source, reason,
):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        reason = _safe_text(reason, required=True, max_length=500)
        fingerprint = _fingerprint(
            "correct-purchased-evidence", item, actor,
            completion_source=completion_source, reason=reason,
        )
        replay = _superseded_purchased_replay(
            item=item, idempotency_key=idempotency_key,
            state=InstalledGameRecordState.RECORDED, request_fingerprint=fingerprint,
        )
        if replay:
            return replay
        authority = _staff(item, actor)
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS, DigitalFulfillmentStatus.WAITING_CONFIRMATION)
        expected_source = (
            InstalledGameCompletionSource.STAFF_INSTALLED
            if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
            else InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE
        )
        if completion_source != expected_source:
            raise DigitalFulfillmentValidationError("Evidence source does not match the operational method.")
        return _supersede_purchased(
            item=item, actor=actor, authority=authority, idempotency_key=idempotency_key,
            state=InstalledGameRecordState.RECORDED, completion_source=completion_source, reason=reason,
            request_fingerprint=fingerprint,
        )


@transaction.atomic
def remove_purchased_game_installation(*, fulfillment_id, actor, idempotency_key, reason):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        reason = _safe_text(reason, required=True, max_length=500)
        fingerprint = _fingerprint("remove-purchased-evidence", item, actor, reason=reason)
        replay = _superseded_purchased_replay(
            item=item, idempotency_key=idempotency_key,
            state=InstalledGameRecordState.REMOVED, request_fingerprint=fingerprint,
        )
        if replay:
            return replay
        authority = _staff(item, actor)
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS, DigitalFulfillmentStatus.WAITING_CONFIRMATION)
        current = _current_purchased(item, lock=True)
        if not current:
            raise DigitalFulfillmentValidationError("No current purchased evidence exists.")
        return _supersede_purchased(
            item=item, actor=actor, authority=authority, idempotency_key=idempotency_key,
            state=InstalledGameRecordState.REMOVED, completion_source=current.completion_source, reason=reason,
            request_fingerprint=fingerprint,
        )


@transaction.atomic
def record_remote_handling(*, fulfillment_id, actor, idempotency_key, await_confirmation=True):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("remote-handling", item, actor, await_confirmation=bool(await_confirmation))
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.REMOTE_HANDLING_PERFORMED, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS)
        if item.current_fulfillment_method != DigitalCartFulfillmentMethod.REMOTE:
            raise DigitalFulfillmentValidationError("Remote handling requires active remote work.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.REMOTE_HANDLING_PERFORMED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE, key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if await_confirmation:
            _append_activity(
                item=item, activity_type=FulfillmentActivityType.CUSTOMER_ACTION_REQUESTED,
                actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
                visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
                key=_derived_key(idempotency_key, "customer-action"), request_fingerprint=fingerprint,
            )
            return _transition(
                item, status=DigitalFulfillmentStatus.WAITING_CONFIRMATION,
                waiting=DigitalFulfillmentWaitingReason.CUSTOMER_CONFIRMATION_REQUIRED,
                actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
                key=_derived_key(idempotency_key, "remote-state"), request_fingerprint=fingerprint,
            )
        return item


def _complete_locked(
    item, *, actor, actor_type, actor_authority, key, evidence_type, request_fingerprint,
):
    if _activity_replay(
        item=item, key=key, activity_type=evidence_type, request_fingerprint=request_fingerprint,
    ):
        return item
    _graph(item.obligation)
    if item.status == DigitalFulfillmentStatus.COMPLETED:
        raise DigitalFulfillmentConflict("Fulfillment was completed by another command.")
    if not _has_activity(item, FulfillmentActivityType.WORK_STARTED):
        raise DigitalFulfillmentValidationError("Work-start evidence is required.")
    purchased = _current_purchased(item, lock=True)
    if not purchased:
        raise DigitalFulfillmentValidationError("Current purchased installation evidence is required.")
    expected_source = (
        InstalledGameCompletionSource.STAFF_INSTALLED
        if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
        else InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE
    )
    if purchased.completion_source != expected_source:
        raise DigitalFulfillmentValidationError("Purchased evidence does not match the fulfillment method.")
    if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE:
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS)
        if not _has_activity(item, FulfillmentActivityType.CONSOLE_RECEIVED):
            raise DigitalFulfillmentValidationError("Console receipt evidence is required.")
    else:
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS, DigitalFulfillmentStatus.WAITING_CONFIRMATION)
        if not _has_activity(item, FulfillmentActivityType.REMOTE_HANDLING_PERFORMED):
            raise DigitalFulfillmentValidationError("Remote handling evidence is required.")
    previous_status = item.status
    now = timezone.now()
    item.status = DigitalFulfillmentStatus.COMPLETED
    item.waiting_reason = None
    item.completed_at = now
    item.save(update_fields=("status", "waiting_reason", "completed_at", "updated_at"))
    entitlement = Entitlement.objects.select_for_update().get(obligation=item.obligation)
    if entitlement.fulfillment_item_id != item.pk or entitlement.customer_id != item.obligation.order.user_id:
        raise DigitalFulfillmentConflict("Entitlement ownership is contradictory.")
    if entitlement.status != DigitalEntitlementStatus.PENDING_FULFILLMENT:
        raise DigitalFulfillmentConflict("Entitlement lifecycle is contradictory.")
    entitlement.status = DigitalEntitlementStatus.ACTIVE
    entitlement.activated_at = now
    entitlement.save(update_fields=("status", "activated_at", "updated_at"))
    _append_activity(
        item=item, activity_type=evidence_type, actor_type=actor_type, actor=actor,
        actor_authority=actor_authority, visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
        key=key, request_fingerprint=request_fingerprint,
    )
    _append_activity(
        item=item, activity_type=FulfillmentActivityType.STATUS_CHANGED,
        actor_type=actor_type, actor=actor, actor_authority=actor_authority,
        visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
        key=_derived_key(key, "completed"), request_fingerprint=request_fingerprint,
        previous=previous_status, new=DigitalFulfillmentStatus.COMPLETED,
    )
    order = item.obligation.order
    if not order.digital_fulfillment_obligations.exclude(execution__status=DigitalFulfillmentStatus.COMPLETED).exists():
        order.fulfillment_status = FulfillmentStatus.DELIVERED
        order.save(update_fields=("fulfillment_status", "updated_at"))
    return item


@transaction.atomic
def customer_confirm_remote_completion(*, fulfillment_id, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _customer_authority(item, actor)
        fingerprint = _fingerprint("customer-confirm", item, actor)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.CUSTOMER_CONFIRMED, request_fingerprint=fingerprint):
            return item
        if item.current_fulfillment_method != DigitalCartFulfillmentMethod.REMOTE:
            raise DigitalFulfillmentValidationError("Customer confirmation is not currently allowed.")
        _require_status(item, DigitalFulfillmentStatus.WAITING_CONFIRMATION)
        return _complete_locked(
            item, actor=actor, actor_type=FulfillmentActivityActorType.CUSTOMER,
            actor_authority=authority, key=idempotency_key,
            evidence_type=FulfillmentActivityType.CUSTOMER_CONFIRMED,
            request_fingerprint=fingerprint,
        )


@transaction.atomic
def staff_verify_fulfillment_completion(*, fulfillment_id, actor, idempotency_key):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        fingerprint = _fingerprint("staff-verify", item, actor)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.STAFF_VERIFIED, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.IN_PROGRESS, DigitalFulfillmentStatus.WAITING_CONFIRMATION)
        return _complete_locked(
            item, actor=actor, actor_type=FulfillmentActivityActorType.STAFF,
            actor_authority=authority, key=idempotency_key,
            evidence_type=FulfillmentActivityType.STAFF_VERIFIED,
            request_fingerprint=fingerprint,
        )


@transaction.atomic
def open_fulfillment_exception(*, fulfillment_id, actor, idempotency_key, note):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        note = _safe_text(note, required=True)
        fingerprint = _fingerprint("open-exception", item, actor, note=note)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.FAILURE_RECORDED, request_fingerprint=fingerprint):
            return item
        if item.status in (DigitalFulfillmentStatus.COMPLETED, DigitalFulfillmentStatus.EXCEPTION):
            raise DigitalFulfillmentValidationError("Exception cannot be opened in the current state.")
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.FAILURE_RECORDED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.INTERNAL, key=idempotency_key,
            request_fingerprint=fingerprint, note=note,
        )
        return _transition(
            item, status=DigitalFulfillmentStatus.EXCEPTION,
            waiting=DigitalFulfillmentWaitingReason.ADDITIONAL_INFORMATION_REQUIRED,
            actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
            key=_derived_key(idempotency_key, "exception-state"), request_fingerprint=fingerprint,
        )


@transaction.atomic
def retry_fulfillment(*, fulfillment_id, actor, idempotency_key, reason):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        reason = _safe_text(reason, required=True)
        fingerprint = _fingerprint("retry", item, actor, reason=reason)
        if _activity_replay(item=item, key=idempotency_key, activity_type=FulfillmentActivityType.RETRY_STARTED, request_fingerprint=fingerprint):
            return item
        _require_status(item, DigitalFulfillmentStatus.EXCEPTION)
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.RETRY_STARTED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.INTERNAL, key=idempotency_key,
            request_fingerprint=fingerprint, note=reason,
        )
        if _has_activity(item, FulfillmentActivityType.CONSOLE_RECEIVED):
            target, waiting = DigitalFulfillmentStatus.READY_FOR_STAFF, None
        elif _has_activity(item, FulfillmentActivityType.CUSTOMER_CONTACTED, FulfillmentActivityType.CUSTOMER_CONTACT_ATTEMPTED):
            target = DigitalFulfillmentStatus.WAITING_CUSTOMER
            waiting = (
                DigitalFulfillmentWaitingReason.WAITING_FOR_CONSOLE
                if item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE
                else DigitalFulfillmentWaitingReason.ADDITIONAL_INFORMATION_REQUIRED
            )
        else:
            target, waiting = DigitalFulfillmentStatus.QUEUED, DigitalFulfillmentWaitingReason.CONTACT_REQUIRED
        return _transition(
            item, status=target, waiting=waiting,
            actor=actor, actor_type=FulfillmentActivityActorType.STAFF, actor_authority=authority,
            key=_derived_key(idempotency_key, "retry-state"), request_fingerprint=fingerprint,
        )


@transaction.atomic
def add_fulfillment_note(*, fulfillment_id, actor, idempotency_key, note, customer_safe=False):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        note = _safe_text(note, required=True)
        fingerprint = _fingerprint("add-note", item, actor, note=note, customer_safe=bool(customer_safe))
        existing = _activity_replay(
            item=item, key=idempotency_key, activity_type=FulfillmentActivityType.NOTE_ADDED,
            request_fingerprint=fingerprint,
        )
        if existing:
            return existing
        return _append_activity(
            item=item, activity_type=FulfillmentActivityType.NOTE_ADDED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=(FulfillmentActivityVisibility.CUSTOMER_SAFE if customer_safe else FulfillmentActivityVisibility.INTERNAL),
            key=idempotency_key, request_fingerprint=fingerprint, note=note,
        )


@transaction.atomic
def record_bonus_game(*, fulfillment_id, actor, idempotency_key, game=None, delivered_version=None, fallback_title=""):
    with ordered_lock_scope():
        item = _locked_item(fulfillment_id)
        authority = _staff(item, actor)
        title = _safe_text(fallback_title, max_length=200)
        fingerprint = _fingerprint(
            "record-bonus", item, actor, game_id=getattr(game, "pk", None),
            delivered_version_id=getattr(delivered_version, "pk", None), fallback_title=title,
        )
        replay = _installed_replay(
            item=item, key=idempotency_key, classification=InstalledGameClassification.BONUS,
            request_fingerprint=fingerprint,
        )
        if replay:
            return replay
        _require_status(
            item, DigitalFulfillmentStatus.IN_PROGRESS,
            DigitalFulfillmentStatus.WAITING_CONFIRMATION, DigitalFulfillmentStatus.COMPLETED,
        )
        if game is None and not title:
            raise DigitalFulfillmentValidationError("Bonus game identity is required.")
        record = InstalledGameRecord.objects.create(
            fulfillment_item=item, game=game, delivered_version=delivered_version,
            classification=InstalledGameClassification.BONUS,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            operator=actor, actor_authority=authority, fallback_title=title,
            state=InstalledGameRecordState.RECORDED, idempotency_key=_uuid(idempotency_key),
            request_fingerprint=fingerprint,
        )
        _append_activity(
            item=item, activity_type=FulfillmentActivityType.BONUS_RECORDED,
            actor_type=FulfillmentActivityActorType.STAFF, actor=actor, actor_authority=authority,
            visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE,
            key=_derived_key(idempotency_key, "bonus"), request_fingerprint=fingerprint, note=title,
        )
        return record
