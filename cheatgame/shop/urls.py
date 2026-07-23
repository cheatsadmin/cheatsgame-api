from django.urls import path

from cheatgame.shop.apis.cart import AddToCart, CartItemDetail, CartItemListApi, SubmitOrderApi, \
    OrderListCustomerAPIView, GameListCustomerAPIView, OrderDetailUserApi, OrderDetailCustomerAPIView, SellReport, \
    OrderListAdminAPIView, OrderDetailAdminAPIView
from cheatgame.shop.apis.delivery_schedule import DeliveryScheduleAdminApi, DeliveryScheduleDetailAdminApi, \
    DeliveryScheduleList, DeliveryDataApi, RepairDeliveryScheduleGeneratorAdminApi
from cheatgame.shop.apis.delivery_type import DeliveryTypeAdminApi, DeliveryTypeDetailApi, DeliveryTypeListApi
from cheatgame.shop.apis.discount import DiscountAdminApi, DiscountDetailSerializer, DiscountListAdmin, \
    CheckUserDiscountApi, CheckCouponApi, DiscountListUser
from cheatgame.shop.apis.payment import CreatePaymentRequestApi, PaymentTransactionDetailApi, VerifyPaymentApi
from cheatgame.shop.apis.checkout import (
    ActiveCheckoutApi,
    CheckoutAddressApi,
    CheckoutCancelApi,
    CheckoutCreateApi,
    CheckoutDetailApi,
    CheckoutScheduleApi,
    CheckoutShippingApi,
)

urlpatterns = [
    path("checkouts/", CheckoutCreateApi.as_view(), name="checkout-v2-create"),
    path("checkout/active/", ActiveCheckoutApi.as_view(), name="checkout-v2-active"),
    path("checkouts/<uuid:public_id>/", CheckoutDetailApi.as_view(), name="checkout-v2-detail"),
    path("checkouts/<uuid:public_id>/address/", CheckoutAddressApi.as_view(), name="checkout-v2-address"),
    path("checkouts/<uuid:public_id>/shipping/", CheckoutShippingApi.as_view(), name="checkout-v2-shipping"),
    path("checkouts/<uuid:public_id>/schedule/", CheckoutScheduleApi.as_view(), name="checkout-v2-schedule"),
    path("checkouts/<uuid:public_id>/cancel/", CheckoutCancelApi.as_view(), name="checkout-v2-cancel"),
    path("create-discount-code/", DiscountAdminApi.as_view(), name="create-discount"),
    path("discount-detail/<int:id>/", DiscountDetailSerializer.as_view(), name="discount-detail-manager"),
    path("discount-list-manager/", DiscountListAdmin.as_view(), name="discount-list-manager"),
    path("check-user-discount-code/", CheckUserDiscountApi.as_view(), name="check-discount-admin"),
    path("discount-list-user/", DiscountListUser.as_view(), name="discount-list-user"),
    path("check-coupon/", CheckCouponApi.as_view(), name="check-coupon"),
    path("create-delivery-type/", DeliveryTypeAdminApi.as_view(), name="create-delivery-type"),
    path("delivery-type-detail/<int:id>/", DeliveryTypeDetailApi.as_view(), name="delivery-type-detial"),
    path("delivery-type-list/", DeliveryTypeListApi.as_view(), name="delivery-type-list"),
    path("add-to-cart/", AddToCart.as_view(), name="add_to_cart"),
    path("udpate-cart-item/<int:id>/", CartItemDetail.as_view(), name="cart-item-detail"),
    path("cart-item-list/", CartItemListApi.as_view(), name="cart-item-list"),
    path("create-list-delivery-schedule/" , DeliveryScheduleAdminApi.as_view() , name = "create-delivery-schdule-list"),
    path("repair-delivery-schedule-generator/" , RepairDeliveryScheduleGeneratorAdminApi.as_view() , name="repair-delivery-schedule-generator"),
    path("delivery-schedule-detail/<int:id>/" , DeliveryScheduleDetailAdminApi.as_view() , name="delivery-schdule-detail"),
    path("delivery-schedule-list/" , DeliveryScheduleList.as_view() , name="delivery-list"),
    path("book-time/" , DeliveryDataApi.as_view() , name="book-time"),
    path("submit-order/"  , SubmitOrderApi.as_view() , name= "submit-order"),
    path("order-detail/<int:id>/" , OrderDetailUserApi.as_view() , name="order-detail"),
    path("order-list-admin/" , OrderListAdminAPIView.as_view(), name="order-list-admin"),
    path("order-list-user/" , OrderListCustomerAPIView.as_view(), name="order-list-user"),
    path("game-list-user/"  , GameListCustomerAPIView.as_view() ,name="game-list-user"),
    path("get-order-detail-admin/<int:id>/" , OrderDetailAdminAPIView.as_view() , name="get-order-detail-admin"),
    path("get-order-detail/<int:id>/" , OrderDetailCustomerAPIView.as_view() , name="get-order-detail"),
    path("orders/<int:order_id>/payment/request/" , CreatePaymentRequestApi.as_view() , name="create-payment-request"),
    path("payments/<int:transaction_id>/verify/" , VerifyPaymentApi.as_view() , name="verify-payment"),
    path("payments/<int:transaction_id>/" , PaymentTransactionDetailApi.as_view() , name="payment-detail"),
    path("sell-order-report/" ,SellReport.as_view() , name="sell-order-report")



]
