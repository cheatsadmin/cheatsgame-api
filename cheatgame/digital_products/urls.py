from django.urls import path

from cheatgame.digital_products.public_catalog_apis import (
    PublicDigitalGameDetailApi,
    PublicDigitalGameListApi,
)
from cheatgame.digital_products.customer_cart_apis import (
    CustomerDigitalCartFulfillmentMethodApi,
    CustomerDigitalCartItemCreateApi,
    CustomerDigitalCartItemDeleteApi,
)
from cheatgame.digital_products.customer_checkout_apis import (
    CustomerDigitalCheckoutActiveApi,
    CustomerDigitalCheckoutCancelApi,
    CustomerDigitalCheckoutDetailApi,
    CustomerDigitalCheckoutPrepareApi,
)
from cheatgame.digital_products.customer_payment_apis import (
    CustomerDigitalPaymentRequestApi,
    CustomerDigitalPaymentStatusApi,
)


app_name = "digital-products"

urlpatterns = [
    path("catalog/games/", PublicDigitalGameListApi.as_view(), name="public-game-list"),
    path("catalog/games/<str:slug>/", PublicDigitalGameDetailApi.as_view(), name="public-game-detail"),
    path(
        "customer/cart/items/",
        CustomerDigitalCartItemCreateApi.as_view(),
        name="customer-cart-item-add",
    ),
    path(
        "customer/cart/items/<int:cart_item_id>/",
        CustomerDigitalCartItemDeleteApi.as_view(),
        name="customer-cart-item-remove",
    ),
    path(
        "customer/cart/items/<int:cart_item_id>/fulfillment-method/",
        CustomerDigitalCartFulfillmentMethodApi.as_view(),
        name="customer-cart-fulfillment-method-change",
    ),
    path(
        "customer/checkout/prepare/",
        CustomerDigitalCheckoutPrepareApi.as_view(),
        name="customer-checkout-prepare",
    ),
    path(
        "customer/checkout/active/",
        CustomerDigitalCheckoutActiveApi.as_view(),
        name="customer-checkout-active",
    ),
    path(
        "customer/checkout/<uuid:checkout_id>/",
        CustomerDigitalCheckoutDetailApi.as_view(),
        name="customer-checkout-detail",
    ),
    path(
        "customer/checkout/<uuid:checkout_id>/cancel/",
        CustomerDigitalCheckoutCancelApi.as_view(),
        name="customer-checkout-cancel",
    ),
    path(
        "customer/checkout/<uuid:checkout_id>/payment/request/",
        CustomerDigitalPaymentRequestApi.as_view(),
        name="customer-payment-request",
    ),
    path(
        "customer/checkout/<uuid:checkout_id>/payment/",
        CustomerDigitalPaymentStatusApi.as_view(),
        name="customer-payment-status",
    ),
]
