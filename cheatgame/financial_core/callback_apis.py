from uuid import uuid4

from django.db import transaction
from django.utils.decorators import method_decorator
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackProcessingStatus,
    PaymentTransaction,
    ProviderEventResolutionStatus,
    CallbackReplayWindowStatus,
)
from cheatgame.financial_core.services.adapters import PRODUCTION_ADAPTER_REGISTRY
from cheatgame.financial_core.services.callbacks import (
    callback_transport_rejection,
    ingest_callback_delivery,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict


class ProviderCallbackAcknowledgementSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()
    receipt_id = serializers.UUIDField(required=False)
    duplicate = serializers.BooleanField(default=False)


class ProviderCallbackErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()


class FinancialCallbackAccountThrottle(SimpleRateThrottle):
    scope = "financial_callback"

    def get_cache_key(self, request, view):
        transaction_obj = (
            PaymentTransaction.objects.filter(
                public_id=view.kwargs.get("transaction_id"),
                provider=view.kwargs.get("provider_key"),
            )
            .only("merchant_account_version_id")
            .first()
        )
        if transaction_obj is None:
            ident = f"unknown:{view.kwargs.get('provider_key')}:{self.get_ident(request)}"
        else:
            ident = f"account:{transaction_obj.merchant_account_version_id}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


def _error(code, detail, http_status):
    return Response({"code": code, "detail": detail}, status=http_status)


def _transaction_policy(*, provider_key, transaction_id):
    return (
        PaymentTransaction.objects.select_related(
            "capability_version",
            "merchant_account_version",
            "merchant_account_version__provider",
        )
        .filter(
            public_id=transaction_id,
            provider=provider_key,
            merchant_account_version__provider__key=provider_key,
            merchant_account_version__recovery_enabled=True,
        )
        .first()
    )


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class ProviderCallbackIngestionApi(APIView):
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = (ScopedRateThrottle, FinancialCallbackAccountThrottle)
    throttle_scope = "financial_callback"
    http_method_names = ("post", "options")
    adapter_registry = PRODUCTION_ADAPTER_REGISTRY

    @extend_schema(
        operation_id="financial_provider_callback_ingest",
        parameters=[
            OpenApiParameter("provider_key", OpenApiTypes.STR, OpenApiParameter.PATH),
            OpenApiParameter("transaction_id", OpenApiTypes.UUID, OpenApiParameter.PATH),
        ],
        request=OpenApiTypes.OBJECT,
        responses={
            200: ProviderCallbackAcknowledgementSerializer,
            202: ProviderCallbackAcknowledgementSerializer,
            400: ProviderCallbackErrorSerializer,
            401: ProviderCallbackErrorSerializer,
            404: ProviderCallbackErrorSerializer,
            409: ProviderCallbackErrorSerializer,
            413: ProviderCallbackErrorSerializer,
            415: ProviderCallbackErrorSerializer,
            503: ProviderCallbackErrorSerializer,
        },
        auth=[],
        description=(
            "Authenticate, normalize, persist, and deduplicate provider callback evidence. "
            "This endpoint never verifies funds or mutates payment truth."
        ),
    )
    def post(self, request, provider_key, transaction_id):
        body = request.body
        headers = dict(request.headers)
        transport_reason = callback_transport_rejection(
            method=request.method,
            content_type=request.content_type or "",
            body=body,
            headers=headers,
        )
        transaction_obj = _transaction_policy(
            provider_key=provider_key,
            transaction_id=transaction_id,
        )
        if transaction_obj is None:
            return _error("callback_not_found", "Callback ingress was not found.", 404)

        try:
            result = ingest_callback_delivery(
                provider_key=provider_key,
                capability_version=transaction_obj.capability_version.version,
                account_key=transaction_obj.merchant_account_version.account_key,
                account_version=transaction_obj.merchant_account_version.version,
                method=request.method,
                content_type=request.content_type or "",
                body=body,
                headers=headers,
                delivery_idempotency_key=uuid4(),
                adapter_registry=self.adapter_registry,
                source_network=request.META.get("REMOTE_ADDR", ""),
                callback_transaction_public_id=transaction_id,
            )
        except IdempotencyConflict:
            return _error(
                "callback_evidence_conflict",
                "Callback evidence conflicts with an existing delivery identity.",
                409,
            )

        receipt = result.receipt
        payload = {
            "accepted": True,
            "receipt_id": str(receipt.public_id),
            "duplicate": bool(result.replayed),
        }
        if result.replayed:
            return Response(payload, status=status.HTTP_200_OK)
        if result.provider_event and (
            result.provider_event.resolution_status == ProviderEventResolutionStatus.CONTRADICTORY
        ):
            return _error(
                "callback_evidence_conflict",
                "Callback evidence contradicts previously persisted provider evidence.",
                409,
            )
        if transport_reason:
            transport_status = {
                "body_too_large": 413,
                "unsupported_content_type": 415,
            }.get(transport_reason, 400)
            return _error("callback_transport_rejected", "Callback transport was rejected.", transport_status)
        if receipt.authentication_status == CallbackAuthenticationStatus.INVALID:
            if receipt.quarantine_reason == "unsupported_adapter_version":
                return _error(
                    "callback_authentication_unavailable",
                    "Callback authentication is temporarily unavailable.",
                    503,
                )
            return _error("callback_authentication_failed", "Callback authentication failed.", 401)
        if receipt.replay_window_status == CallbackReplayWindowStatus.EXPIRED:
            return _error("callback_replay_rejected", "Callback replay window was rejected.", 401)
        if receipt.quarantine_reason in ("malformed_payload", "callback_authentication_failure"):
            return _error("callback_invalid", "Callback delivery is invalid.", 400)
        if receipt.processing_status == CallbackProcessingStatus.SECURITY_REJECTED:
            return _error("callback_authentication_failed", "Callback authentication failed.", 401)
        return Response(payload, status=status.HTTP_202_ACCEPTED)
