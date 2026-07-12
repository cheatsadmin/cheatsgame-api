from django.contrib import admin

from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartItemAttachment,
    Checkout,
    CheckoutLine,
    CheckoutLineAttachment,
    CheckoutShippingSnapshot,
    CommerceEvent,
    DeliveryData,
    DeliverySchedule,
    DeliveryType,
    Discount,
    Order,
    OrderItem,
    OrderItemAttachment,
    PaymentTransaction,
    StockReservation,
    UserDiscount,
)


class ReadOnlyCommerceAdmin(admin.ModelAdmin):
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    fields = ( "user", "public_tracking_code", "discount", "payment_status", "user_status", "total_price",
              "total_price_discount", "schedule", "shipping_address", "shipping_method", "checkout",
              "fulfillment_status",)
    readonly_fields = ("public_tracking_code", "checkout", "fulfillment_status")
    list_display = ("id", "public_tracking_code", "user", "discount", "payment_status", "user_status", "total_price",
                    "total_price_discount", "fulfillment_status", "schedule", "shipping_address", "shipping_method",)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    fields = ("product", "quantity", "price", "order")
    list_display = ("id","product", "quantity", "price", "order")


@admin.register(OrderItemAttachment)
class OrderItemAttachmentAdmin(admin.ModelAdmin):
    fields = ("order_item", "attachment")
    list_display = ("order_item", "attachment")


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    fields = ("user", "state", "lock_reason", "active_checkout", "locked_at", "lock_version")
    readonly_fields = ("state", "lock_reason", "active_checkout", "locked_at", "lock_version")
    list_display = ("user", "state", "lock_reason", "active_checkout", "locked_at")


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    fields = ("product", "quantity", "price", "cart")
    list_display = ("product", "quantity", "price", "cart")


@admin.register(CartItemAttachment)
class CartItemAttachmentAdmin(admin.ModelAdmin):
    fields = ("cart_item", "attachment")
    list_display = ("cart_item", "attachment")


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    fields = (
        "name", "code", "type", "value_type", "valid_from", "valid_until", "is_active", "min_purchase_amount", "amount",
        "percent", "admin_user", "usage_number")
    list_display = (
        "name", "code", "type", "value_type", "valid_from", "valid_until", "is_active", "min_purchase_amount", "amount",
        "percent", "admin_user", "usage_number"
    )


@admin.register(UserDiscount)
class UserDiscountAdmin(admin.ModelAdmin):
    fields = ("discount", "user", "is_used")
    list_display = ("discount", "user", "is_used")


@admin.register(DeliverySchedule)
class DeliveryScheduleAdmin(admin.ModelAdmin):
    fields = ("start", "end", "type", "capacity")
    list_display = ("start", "end", "type", "capacity")


@admin.register(DeliveryType)
class DeliveryTypeAdmin(admin.ModelAdmin):
    fields = ("name", "delivery_type", "side")
    list_display = ("name", "delivery_type", "side")

@admin.register(DeliveryData)
class DeliveryDataAdmin(admin.ModelAdmin):
    fields = ("type", "schedule", "address")
    list_display = ("type" ,"schedule" , "address")


@admin.register(Checkout)
class CheckoutAdmin(ReadOnlyCommerceAdmin):
    list_display = (
        "id",
        "public_id",
        "user",
        "status",
        "cart",
        "expires_at",
        "manual_review_reason",
        "created_at",
    )
    list_filter = ("status", "manual_review_reason")
    search_fields = ("public_id", "client_checkout_uuid", "cart_fingerprint")


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(ReadOnlyCommerceAdmin):
    list_display = (
        "id",
        "checkout",
        "order",
        "provider",
        "status",
        "amount",
        "manual_review_reason",
        "created_at",
    )
    list_filter = ("provider", "status", "manual_review_reason")
    search_fields = ("idempotency_key", "gateway_authority", "gateway_ref_id")


@admin.register(CheckoutLine)
class CheckoutLineAdmin(ReadOnlyCommerceAdmin):
    list_display = ("id", "checkout", "product_id", "product_name", "quantity", "line_payable_total")
    search_fields = ("product_name", "product_sku")


@admin.register(CheckoutLineAttachment)
class CheckoutLineAttachmentAdmin(ReadOnlyCommerceAdmin):
    list_display = ("id", "checkout_line", "attachment_id", "name", "unit_price", "total_price")


@admin.register(CheckoutShippingSnapshot)
class CheckoutShippingSnapshotAdmin(ReadOnlyCommerceAdmin):
    list_display = ("id", "checkout", "delivery_method_name", "delivery_cost", "schedule_start")


@admin.register(StockReservation)
class StockReservationAdmin(ReadOnlyCommerceAdmin):
    list_display = ("id", "checkout", "product", "quantity", "state", "expires_at")
    list_filter = ("state",)


@admin.register(CommerceEvent)
class CommerceEventAdmin(ReadOnlyCommerceAdmin):
    list_display = ("id", "checkout", "event_type", "actor_type", "payment_transaction", "created_at")
    list_filter = ("event_type", "actor_type")
    search_fields = ("request_id", "correlation_id", "idempotency_reference")
