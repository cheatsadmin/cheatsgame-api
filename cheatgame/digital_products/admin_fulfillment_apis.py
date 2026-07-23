from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination
from cheatgame.digital_products.fulfillment_selectors import (
    admin_fulfillment_item,
    admin_fulfillment_items,
)
from cheatgame.digital_products.fulfillment_serializers import (
    AdminDigitalFulfillmentListProjectionSerializer,
    AdminDigitalFulfillmentProjectionSerializer,
    admin_fulfillment_list_projection,
    admin_fulfillment_projection,
)
from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalFulfillmentStatus,
    DigitalFulfillmentWaitingReason,
    InstalledGameCompletionSource,
)
from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentConflict,
    DigitalFulfillmentValidationError,
    add_fulfillment_note,
    assign_fulfillment_operator,
    change_fulfillment_method,
    open_fulfillment_exception,
    record_bonus_game,
    record_console_received,
    record_customer_contact,
    record_purchased_game_installation,
    record_remote_handling,
    retry_fulfillment,
    staff_verify_fulfillment_completion,
    start_fulfillment_work,
)
from cheatgame.product.models import Product
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.users.models import BaseUser, UserTypes


def _error(*, code, detail, http_status, fields=None):
    payload = {"code": code, "detail": detail}
    if fields:
        payload["fields"] = fields
    return Response(payload, status=http_status)


class AdminFulfillmentApi(ApiAuthMixin, GenericAPIView):
    permission_classes = (AdminOrManagerPermission,)

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _error(
                code="authentication_required",
                detail="Authentication is required.",
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, PermissionDenied):
            return _error(
                code="fulfillment_permission_denied",
                detail="This fulfillment action is not permitted.",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        return super().handle_exception(exc)


class FulfillmentQueueFilterSerializer(serializers.Serializer):
    queue = serializers.ChoiceField(
        choices=(
            "new",
            "contact",
            "ready",
            "in_progress",
            "waiting_customer",
            "exception",
            "completed",
        ),
        required=False,
    )
    status = serializers.ChoiceField(
        choices=DigitalFulfillmentStatus.values,
        required=False,
    )
    waiting_reason = serializers.ChoiceField(
        choices=DigitalFulfillmentWaitingReason.values,
        required=False,
    )
    fulfillment_method = serializers.ChoiceField(
        choices=DigitalCartFulfillmentMethod.values,
        required=False,
    )
    capacity = serializers.CharField(required=False, max_length=16)
    customer_console = serializers.CharField(required=False, max_length=10)
    native_console = serializers.CharField(required=False, max_length=10)
    assigned_operator = serializers.IntegerField(required=False, min_value=1)
    assignment = serializers.ChoiceField(
        choices=("assigned", "unassigned"),
        required=False,
    )
    tracking_code = serializers.CharField(required=False, max_length=32)
    customer_search = serializers.CharField(required=False, max_length=100)
    game_search = serializers.CharField(required=False, max_length=200)
    ordering = serializers.ChoiceField(
        choices=("oldest", "newest", "recently_updated"),
        required=False,
        default="oldest",
    )


_QUEUE_STATUSES = {
    "new": (DigitalFulfillmentStatus.QUEUED,),
    "contact": (
        DigitalFulfillmentStatus.QUEUED,
        DigitalFulfillmentStatus.WAITING_CUSTOMER,
    ),
    "ready": (DigitalFulfillmentStatus.READY_FOR_STAFF,),
    "in_progress": (DigitalFulfillmentStatus.IN_PROGRESS,),
    "waiting_customer": (
        DigitalFulfillmentStatus.WAITING_CUSTOMER,
        DigitalFulfillmentStatus.WAITING_CONFIRMATION,
    ),
    "exception": (DigitalFulfillmentStatus.EXCEPTION,),
    "completed": (DigitalFulfillmentStatus.COMPLETED,),
}


def _filtered_queue(values):
    queryset = admin_fulfillment_items()
    if values.get("queue"):
        queryset = queryset.filter(status__in=_QUEUE_STATUSES[values["queue"]])
    if values.get("status"):
        queryset = queryset.filter(status=values["status"])
    if values.get("waiting_reason"):
        queryset = queryset.filter(waiting_reason=values["waiting_reason"])
    if values.get("fulfillment_method"):
        queryset = queryset.filter(
            current_fulfillment_method=values["fulfillment_method"]
        )
    if values.get("capacity"):
        queryset = queryset.filter(
            obligation__checkout_line__digital_snapshot__capacity=values["capacity"]
        )
    if values.get("customer_console"):
        queryset = queryset.filter(
            obligation__checkout_line__digital_snapshot__customer_console=values[
                "customer_console"
            ]
        )
    if values.get("native_console"):
        queryset = queryset.filter(
            obligation__checkout_line__digital_snapshot__native_console=values[
                "native_console"
            ]
        )
    if values.get("assigned_operator"):
        queryset = queryset.filter(
            assigned_operator_id=values["assigned_operator"]
        )
    if values.get("assignment") == "assigned":
        queryset = queryset.filter(assigned_operator__isnull=False)
    if values.get("assignment") == "unassigned":
        queryset = queryset.filter(assigned_operator__isnull=True)
    if values.get("tracking_code"):
        queryset = queryset.filter(
            obligation__order__public_tracking_code__icontains=values[
                "tracking_code"
            ]
        )
    if values.get("customer_search"):
        term = values["customer_search"]
        queryset = queryset.filter(
            Q(obligation__order__user__firstname__icontains=term)
            | Q(obligation__order__user__lastname__icontains=term)
            | Q(obligation__order__user__phone_number__icontains=term)
        )
    if values.get("game_search"):
        queryset = queryset.filter(
            obligation__checkout_line__digital_snapshot__product_name__icontains=values[
                "game_search"
            ]
        )
    ordering = values.get("ordering", "oldest")
    return queryset.order_by(
        *{
            "oldest": ("created_at", "pk"),
            "newest": ("-created_at", "-pk"),
            "recently_updated": ("-updated_at", "-pk"),
        }[ordering]
    )


class DigitalFulfillmentQueueApi(AdminFulfillmentApi):
    http_method_names = ("get", "head", "options")
    serializer_class = AdminDigitalFulfillmentListProjectionSerializer
    pagination_class = LimitOffsetPagination

    @extend_schema(operation_id="admin_digital_fulfillment_queue")
    def get(self, request):
        filters = FulfillmentQueueFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return _error(
                code="invalid_fulfillment_filters",
                detail="Fulfillment queue filters are invalid.",
                fields=filters.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(
            _filtered_queue(filters.validated_data),
            request,
            view=self,
        )
        return paginator.get_paginated_response(
            [
                admin_fulfillment_list_projection(item, actor=request.user)
                for item in page
            ]
        )


class DigitalFulfillmentDetailApi(AdminFulfillmentApi):
    http_method_names = ("get", "head", "options")
    serializer_class = AdminDigitalFulfillmentProjectionSerializer

    @extend_schema(operation_id="admin_digital_fulfillment_detail")
    def get(self, request, fulfillment_id):
        try:
            item = admin_fulfillment_item(fulfillment_id)
        except ObjectDoesNotExist:
            return _error(
                code="fulfillment_not_found",
                detail="Digital fulfillment was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        response = Response(
            admin_fulfillment_projection(item, actor=request.user)
        )
        response["Cache-Control"] = "no-store, private"
        return response


class AssignableOperatorDirectoryApi(AdminFulfillmentApi):
    http_method_names = ("get", "head", "options")
    queryset = BaseUser.objects.none()
    _output_serializer = inline_serializer(
        name="DigitalFulfillmentAssignableOperator",
        fields={
            "id": serializers.IntegerField(),
            "display_name": serializers.CharField(),
            "role": serializers.ChoiceField(
                choices=("ADMIN", "MANAGER")
            ),
        },
    )
    serializer_class = _output_serializer.__class__

    @extend_schema(
        operation_id="admin_digital_fulfillment_operators",
        responses=serializer_class(many=True),
    )
    def get(self, request):
        queryset = BaseUser.objects.filter(
            is_active=True,
            user_type__in=(UserTypes.ADMIN, UserTypes.MANAGER),
        ).order_by("firstname", "lastname", "pk")
        search = str(request.query_params.get("search", "")).strip()
        if search:
            queryset = queryset.filter(
                Q(firstname__icontains=search)
                | Q(lastname__icontains=search)
                | Q(phone_number__icontains=search)
            )
        return Response(
            [
                {
                    "id": operator.pk,
                    "display_name": str(operator),
                    "role": UserTypes(operator.user_type).name,
                }
                for operator in queryset[:50]
            ]
        )


class DigitalFulfillmentOptionsApi(AdminFulfillmentApi):
    http_method_names = ("get", "head", "options")
    _choice_serializer = inline_serializer(
        name="DigitalFulfillmentChoice",
        fields={
            "value": serializers.CharField(),
            "label": serializers.CharField(),
        },
    )
    _output_serializer = inline_serializer(
        name="DigitalFulfillmentOptions",
        fields={
            "statuses": _choice_serializer.__class__(many=True),
            "waiting_reasons": _choice_serializer.__class__(
                many=True
            ),
            "fulfillment_methods": _choice_serializer.__class__(
                many=True
            ),
            "actions": serializers.ListField(
                child=serializers.CharField()
            ),
        },
    )
    serializer_class = _output_serializer.__class__

    @extend_schema(operation_id="admin_digital_fulfillment_options")
    def get(self, request):
        return Response(
            {
                "statuses": [
                    {"value": value, "label": label}
                    for value, label in DigitalFulfillmentStatus.choices
                ],
                "waiting_reasons": [
                    {"value": value, "label": label}
                    for value, label in DigitalFulfillmentWaitingReason.choices
                ],
                "fulfillment_methods": [
                    {"value": value, "label": label}
                    for value, label in DigitalCartFulfillmentMethod.choices
                ],
                "actions": [
                    "assign_operator",
                    "record_contact",
                    "change_method",
                    "record_console_received",
                    "start_work",
                    "record_purchased_installation",
                    "record_remote_handling",
                    "staff_verify",
                    "open_exception",
                    "retry",
                    "add_note",
                    "record_bonus",
                ],
            }
        )


class IdempotentCommandSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()


class AssignOperatorSerializer(IdempotentCommandSerializer):
    operator_id = serializers.IntegerField(min_value=1)


class RecordContactSerializer(IdempotentCommandSerializer):
    contacted = serializers.BooleanField(default=True)


class ChangeMethodSerializer(IdempotentCommandSerializer):
    fulfillment_method = serializers.ChoiceField(
        choices=DigitalCartFulfillmentMethod.values
    )


class RemoteHandlingSerializer(IdempotentCommandSerializer):
    await_confirmation = serializers.BooleanField(default=True)


class NoteSerializer(IdempotentCommandSerializer):
    note = serializers.CharField(max_length=1000, trim_whitespace=True)


class RetrySerializer(IdempotentCommandSerializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)


class BonusSerializer(IdempotentCommandSerializer):
    game_id = serializers.IntegerField(required=False, min_value=1)
    fallback_title = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
    )

    def validate(self, attrs):
        if not attrs.get("game_id") and not attrs.get("fallback_title"):
            raise serializers.ValidationError(
                "A catalog game or fallback title is required."
            )
        return attrs


_COMMAND_SERIALIZERS = {
    "assign-operator": AssignOperatorSerializer,
    "record-contact": RecordContactSerializer,
    "change-method": ChangeMethodSerializer,
    "record-console-received": IdempotentCommandSerializer,
    "start-work": IdempotentCommandSerializer,
    "record-purchased-installation": IdempotentCommandSerializer,
    "record-remote-handling": RemoteHandlingSerializer,
    "staff-verify": IdempotentCommandSerializer,
    "open-exception": NoteSerializer,
    "retry": RetrySerializer,
    "add-note": NoteSerializer,
    "record-bonus": BonusSerializer,
}


def _execute_command(*, command, fulfillment_id, actor, values):
    common = {
        "fulfillment_id": fulfillment_id,
        "actor": actor,
        "idempotency_key": values["idempotency_key"],
    }
    if command == "assign-operator":
        operator = BaseUser.objects.filter(
            pk=values["operator_id"],
            is_active=True,
            user_type__in=(UserTypes.ADMIN, UserTypes.MANAGER),
        ).first()
        if operator is None:
            raise DigitalFulfillmentValidationError(
                "Operator must be active eligible staff."
            )
        return assign_fulfillment_operator(operator=operator, **common)
    if command == "record-contact":
        return record_customer_contact(
            contacted=values["contacted"],
            **common,
        )
    if command == "change-method":
        return change_fulfillment_method(
            fulfillment_method=values["fulfillment_method"],
            **common,
        )
    if command == "record-console-received":
        return record_console_received(**common)
    if command == "start-work":
        return start_fulfillment_work(**common)
    if command == "record-purchased-installation":
        item = admin_fulfillment_item(fulfillment_id)
        source = (
            InstalledGameCompletionSource.STAFF_INSTALLED
            if item.current_fulfillment_method
            == DigitalCartFulfillmentMethod.IN_STORE
            else InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE
        )
        return record_purchased_game_installation(
            completion_source=source,
            **common,
        )
    if command == "record-remote-handling":
        return record_remote_handling(
            await_confirmation=values["await_confirmation"],
            **common,
        )
    if command == "staff-verify":
        return staff_verify_fulfillment_completion(**common)
    if command == "open-exception":
        return open_fulfillment_exception(note=values["note"], **common)
    if command == "retry":
        return retry_fulfillment(reason=values["reason"], **common)
    if command == "add-note":
        return add_fulfillment_note(
            note=values["note"],
            customer_safe=False,
            **common,
        )
    if command == "record-bonus":
        game = (
            Product.objects.filter(pk=values.get("game_id")).first()
            if values.get("game_id")
            else None
        )
        if values.get("game_id") and game is None:
            raise DigitalFulfillmentValidationError(
                "Catalog game was not found."
            )
        return record_bonus_game(
            game=game,
            fallback_title=values.get("fallback_title", ""),
            **common,
        )
    raise DigitalFulfillmentValidationError(
        "Unsupported fulfillment command."
    )


class DigitalFulfillmentCommandApi(AdminFulfillmentApi):
    http_method_names = ("post", "options")
    serializer_class = AdminDigitalFulfillmentProjectionSerializer

    @extend_schema(
        operation_id="admin_digital_fulfillment_command",
        request=OpenApiTypes.OBJECT,
        responses=AdminDigitalFulfillmentProjectionSerializer,
    )
    def post(self, request, fulfillment_id, command):
        serializer_class = _COMMAND_SERIALIZERS.get(command)
        if serializer_class is None:
            return _error(
                code="unsupported_fulfillment_command",
                detail="This fulfillment command is not supported.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            return _error(
                code="invalid_fulfillment_command",
                detail="The fulfillment command is invalid.",
                fields=serializer.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            _execute_command(
                command=command,
                fulfillment_id=fulfillment_id,
                actor=request.user,
                values=serializer.validated_data,
            )
            item = admin_fulfillment_item(fulfillment_id)
            response = Response(
                admin_fulfillment_projection(item, actor=request.user)
            )
            response["Cache-Control"] = "no-store, private"
            return response
        except ObjectDoesNotExist:
            return _error(
                code="fulfillment_not_found",
                detail="Digital fulfillment was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        except DigitalFulfillmentConflict as exc:
            return _error(
                code="fulfillment_conflict",
                detail=str(exc),
                http_status=status.HTTP_409_CONFLICT,
            )
        except DigitalFulfillmentValidationError as exc:
            return _error(
                code="invalid_fulfillment_transition",
                detail=str(exc),
                http_status=status.HTTP_409_CONFLICT,
            )
        except PermissionDenied:
            return _error(
                code="fulfillment_permission_denied",
                detail="This fulfillment action is not permitted.",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return _error(
                code="invalid_fulfillment_command",
                detail="The fulfillment command is invalid.",
                fields=getattr(exc, "message_dict", None),
                http_status=status.HTTP_400_BAD_REQUEST,
            )
