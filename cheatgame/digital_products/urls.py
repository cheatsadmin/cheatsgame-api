from django.urls import path

from cheatgame.digital_products.public_catalog_apis import (
    PublicDigitalGameDetailApi,
    PublicDigitalGameListApi,
)


app_name = "digital-products"

urlpatterns = [
    path("catalog/games/", PublicDigitalGameListApi.as_view(), name="public-game-list"),
    path("catalog/games/<str:slug>/", PublicDigitalGameDetailApi.as_view(), name="public-game-detail"),
]
