from uuid import UUID, uuid4

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.financial_core.services.adapters import PRODUCTION_ADAPTER_REGISTRY
from cheatgame.financial_core.services.callbacks import ingest_callback_delivery


class DormantProviderCallbackView(APIView):
    """Fixed-provider callback boundary; intentionally absent from URL configuration in C2B1."""

    authentication_classes = ()
    permission_classes = ()
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "financial_callback"

    provider_key = ""
    capability_version = 0
    merchant_account_key = ""
    merchant_account_version = 0
    adapter_registry = PRODUCTION_ADAPTER_REGISTRY

    def post(self, request):
        try:
            delivery_key = UUID(request.headers.get("X-Delivery-Id", ""))
        except (TypeError, ValueError):
            delivery_key = uuid4()
        ingest_callback_delivery(
            provider_key=self.provider_key,
            capability_version=self.capability_version,
            account_key=self.merchant_account_key,
            account_version=self.merchant_account_version,
            method=request.method,
            content_type=request.content_type or "",
            body=request.body,
            headers=dict(request.headers),
            delivery_idempotency_key=delivery_key,
            adapter_registry=self.adapter_registry,
            source_network=request.META.get("REMOTE_ADDR", ""),
        )
        return Response({"accepted": True}, status=status.HTTP_202_ACCEPTED)
