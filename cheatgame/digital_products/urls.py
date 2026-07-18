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
]
