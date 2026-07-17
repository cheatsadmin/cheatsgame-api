from django.conf import settings
from django.urls import path, include

from cheatgame.shop.apis.payment import FakePaymentCallbackApi, ZarinpalPaymentCallbackApi

urlpatterns = [
    path('auth/', include('cheatgame.authentication.urls'), name='auth'),
    path('user/', include('cheatgame.users.urls'), name='user'),
    path('product/', include('cheatgame.product.urls'), name="product"),
    path("general/", include("cheatgame.general.urls"), name="general"),
    path("shop/", include("cheatgame.shop.urls"), name="shop"),
    path("issue/", include("cheatgame.issue.urls"), name="issue"),
    path("payment/callback/zarinpal/", ZarinpalPaymentCallbackApi.as_view(), name="zarinpal-payment-callback"),

]

if settings.PAYMENT_FAKE_PROVIDER_ENABLED:
    urlpatterns.append(
        path("payment/callback/fake/", FakePaymentCallbackApi.as_view(), name="fake-payment-callback")
    )
