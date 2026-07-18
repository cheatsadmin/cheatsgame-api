from django.urls import path

from cheatgame.financial_core.callback_apis import ProviderCallbackIngestionApi


app_name = "financial-core"

urlpatterns = [
    path(
        "providers/<slug:provider_key>/callbacks/<uuid:transaction_id>/",
        ProviderCallbackIngestionApi.as_view(),
        name="provider-callback-ingest",
    ),
]
