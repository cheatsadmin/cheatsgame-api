from django.contrib import admin

from cheatgame.shop.models import Order, OrderItem, OrderItemAttachment, Cart, CartItem, CartItemAttachment, Discount, \
    UserDiscount, DeliverySchedule, DeliveryType, DeliveryData


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    fields = ( "user", "public_tracking_code", "discount", "payment_status", "user_status", "total_price",
              "total_price_discount", "schedule", "shipping_address", "shipping_method",)
    readonly_fields = ("public_tracking_code",)
    list_display = ("id", "public_tracking_code", "user", "discount", "payment_status", "user_status", "total_price",
                    "total_price_discount", "schedule", "shipping_address", "shipping_method",)


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
    fields = ("user",)
    list_display = ("user",)


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
