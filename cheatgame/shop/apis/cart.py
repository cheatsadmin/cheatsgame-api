from drf_spectacular.utils import extend_schema, extend_schema_field
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.utils import inline_serializer
from cheatgame.common.utils import reformat_url
from cheatgame.digital_products.customer_cart import (
    DigitalCartProjectionIntegrityError,
    cart_authority_code,
    digital_cart_product_projection,
    digital_selection_projection,
)
from cheatgame.digital_products.customer_cart_selectors import owned_customer_cart_items
from cheatgame.digital_products.customer_cart_serializers import DigitalCartSelectionOutputSerializer
from cheatgame.digital_products.public_catalog_apis import digital_api_error
from cheatgame.product.apis.product import ProductDetailProductSerializer
from cheatgame.product.models import Attachment, Product, ProductCommerceAuthority, ProductType
from cheatgame.product.permissions import CustomerPermission, CartItemIsOwnerCustomer, AdminOrManagerPermission
from cheatgame.product.selectors.product import suggestions_product
from cheatgame.shop.models import (
    Cart,
    CartItem,
    DeliveryData,
    DeliverySide,
    DeliveryType,
    Discount,
    Order,
    OrderItem,
    CartItemAttachment,
    OrderItemAttachment,
)
from cheatgame.shop.payments.services import get_latest_order_transaction_summary
from cheatgame.shop.selectors.cart import cart_item_list_user, cart_item_attachment_list, order_list_user, sell_report, bought_order_item
from cheatgame.shop.selectors.discount import validate_discount_code
from cheatgame.shop.services.cart import CartMutationLocked, check_product_limit, check_product_avaliablity, check_attachment, \
    check_cart_item_exists, add_to_cart, update_cart_item, delete_cart_item, check_attachment_order, \
    validate_product_attachments, CartCommerceAuthorityConflict
from cheatgame.shop.services.delivery_schedule import DeliveryDataAlreadyUsedError, DeliverySlotFullError
from cheatgame.shop.services.order import StockUnavailableError, order_item_payable_total, submit_order, update_order
from cheatgame.users.models import Address, BaseUser


def cart_locked_response(cart):
    checkout = cart.active_checkout
    details = {}
    if checkout is not None:
        details = {
            "public_id": str(checkout.public_id),
            "status": checkout.status,
            "resume_route": f"/checkout/{checkout.public_id}",
        }
    return Response(
        {"code": "CART_LOCKED", "message": "سبد خرید در یک فرایند پرداخت فعال است.", "details": details},
        status=status.HTTP_409_CONFLICT,
    )


class ProductAttachmentInPutSerializer(serializers.Serializer):
    attachment = serializers.PrimaryKeyRelatedField(queryset=Attachment.objects.all())


class CartItemAttachmentInPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = '__all__'

class ProductDetailCartSerializer(serializers.ModelSerializer):
    suggestion = serializers.SerializerMethodField()
    main_image = serializers.SerializerMethodField()



    def get_main_image(self, obj):
        return reformat_url(url=obj.main_image.url)



    def get_suggestion(self, product: Product) -> dict:
        prefetched_links = getattr(product, "cart_suggestion_links", None)
        suggestions = (
            [link.suggested for link in prefetched_links]
            if prefetched_links is not None
            else suggestions_product(product=product)
        )
        return ProductDetailProductSerializer(suggestions, many=True).data

    class Meta:
        model = Product
        fields = ("id", "product_type", "title", "slug", "main_image", "price", "off_price", "quantity", "device_model",
                  "suggestion" )


class AuthorityAwareCartProductSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    product_type = serializers.IntegerField()
    title = serializers.CharField()
    slug = serializers.CharField()
    main_image = serializers.CharField(allow_blank=True)
    price = serializers.DecimalField(max_digits=15, decimal_places=0, required=False)
    off_price = serializers.DecimalField(max_digits=15, decimal_places=0, required=False)
    quantity = serializers.IntegerField(required=False)
    device_model = serializers.CharField(allow_null=True, required=False)
    suggestion = ProductDetailProductSerializer(many=True, required=False)


class AddToCart(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    class AddToCartInPutSerializer(serializers.Serializer):
        attachment = ProductAttachmentInPutSerializer(many=True)
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
        quantity = serializers.IntegerField()

    class AddToCartOutPutSerializer(serializers.ModelSerializer):
        product = ProductDetailCartSerializer()

        class Meta:
            model = CartItem
            fields = ("product", "id", "price", "quantity")

    @extend_schema(request=AddToCartInPutSerializer, responses=AddToCartOutPutSerializer)
    def post(self, request):
        serializer = self.AddToCartInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product = serializer.validated_data.get("product")
            if product.commerce_authority != ProductCommerceAuthority.STANDARD_COMMERCE:
                return Response(
                    {"code": "DIGITAL_CART_REQUIRES_DIGITAL_SERVICE", "message": "این محصول از مسیر دیجیتال انتخاب می‌شود."},
                    status=status.HTTP_409_CONFLICT,
                )
            attachments = serializer.validated_data.get("attachment")
            attachments_are_valid, attachment_error = validate_product_attachments(
                product=product,
                attachments=attachments,
            )
            if not attachments_are_valid:
                return Response({"error": attachment_error}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_limit(product=serializer.validated_data.get("product"),
                                       quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "تعداد بیش از حد مجاز می باشد."}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_avaliablity(product=serializer.validated_data.get("product"),
                                             quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "این تعداد محصول موجود نمی باشد"}, status=status.HTTP_400_BAD_REQUEST)
            if not check_attachment(attachments=serializer.validated_data.get("attachment")):
                return Response({"error": "گارانتی و بیمه انتخابی تکراری است"}, status=status.HTTP_400_BAD_REQUEST)
            if check_cart_item_exists(product=serializer.validated_data.get("product"), user=request.user):
                return Response({"error": "محصول قبلا به سبد اضافه شده"}, status=status.HTTP_400_BAD_REQUEST)
            cart_item = add_to_cart(attachment=serializer.validated_data.get("attachment"),
                                    product=serializer.validated_data.get("product"),
                                    user=request.user,
                                    quantity=serializer.validated_data.get("quantity"))
            return Response(self.AddToCartOutPutSerializer(cart_item).data, status=status.HTTP_200_OK)
        except CartMutationLocked as error:
            return cart_locked_response(error.cart)
        except CartCommerceAuthorityConflict as error:
            return Response({"code": error.code, "message": str(error)}, status=status.HTTP_409_CONFLICT)
        except Exception as error:
            return Response({"error": "محصول به سبد اضافه نشد"}, status=status.HTTP_400_BAD_REQUEST)


class CartItemDetail(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, CartItemIsOwnerCustomer,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    class CartItemDetailInPutSerializer(serializers.Serializer):
        quantity = serializers.IntegerField()

    class CartItemDetailOutPutSerializer(serializers.ModelSerializer):
        product = ProductDetailCartSerializer()

        class Meta:
            model = CartItem
            fields = ("product", "id", "price", "quantity")

    @extend_schema(request=CartItemDetailInPutSerializer, responses=CartItemDetailOutPutSerializer)
    def put(self, request, id):
        serializer = self.CartItemDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            cart_item = CartItem.objects.filter(id=id, cart__user=request.user).first()
            if cart_item is None:
                return Response({"error": "محصولی با این مشخصات در سبد خرید شما یافت نشد."},
                                status=status.HTTP_400_BAD_REQUEST)
            self.check_object_permissions(request, cart_item)
            if cart_item.commerce_authority != ProductCommerceAuthority.STANDARD_COMMERCE:
                return Response(
                    {"code": "DIGITAL_CART_REQUIRES_DIGITAL_SERVICE", "message": "این قلم از مسیر دیجیتال مدیریت می‌شود."},
                    status=status.HTTP_409_CONFLICT,
                )
            if not check_product_limit(product=cart_item.product,
                                       quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "تعداد بیش از حد مجاز می باشد."}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_avaliablity(product=cart_item.product,
                                             quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "این تعداد محصول موجود نمی باشد"}, status=status.HTTP_400_BAD_REQUEST)

            cart_item = update_cart_item(quantity=serializer.validated_data.get("quantity"),
                                         cart_item=cart_item)
            return Response(self.CartItemDetailOutPutSerializer(cart_item).data, status=status.HTTP_200_OK)
        except CartMutationLocked as error:
            return cart_locked_response(error.cart)
        except CartCommerceAuthorityConflict as error:
            return Response({"code": error.code, "message": str(error)}, status=status.HTTP_409_CONFLICT)
        except Exception as error:
            return Response({"error": "خطایی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: int):
        try:
            cart_item = CartItem.objects.filter(id=id, cart__user=request.user).first()
            if cart_item is None:
                return Response({"error": "محصولی با این مشخصات در سبد خرید شما یافت نشد."},
                                status=status.HTTP_400_BAD_REQUEST)
            self.check_object_permissions(request, cart_item)
            if cart_item.commerce_authority != ProductCommerceAuthority.STANDARD_COMMERCE:
                return Response(
                    {"code": "DIGITAL_CART_REQUIRES_DIGITAL_SERVICE", "message": "این قلم از مسیر دیجیتال مدیریت می‌شود."},
                    status=status.HTTP_409_CONFLICT,
                )
            delete_cart_item(cart_item_id=cart_item.id)
            return Response({"message": "محصول از سبد حذف شد"}, status=status.HTTP_200_OK)
        except CartMutationLocked as error:
            return cart_locked_response(error.cart)
        except CartCommerceAuthorityConflict as error:
            return Response({"code": error.code, "message": str(error)}, status=status.HTTP_409_CONFLICT)
        except Exception as error:
            return Response({"error": "مشکلی پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class CartItemListApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class CartItemListOutPutSerializer(serializers.ModelSerializer):
        product = serializers.SerializerMethodField()
        attachment = serializers.SerializerMethodField()
        commerce_authority = serializers.SerializerMethodField()
        digital_selection = serializers.SerializerMethodField()

        @extend_schema_field(AuthorityAwareCartProductSerializer)
        def get_product(self, obj):
            if obj.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS:
                return digital_cart_product_projection(obj)
            return ProductDetailCartSerializer(obj.product).data

        @extend_schema_field(CartItemAttachmentInPutSerializer(many=True))
        def get_attachment(self, obj):
            prefetched_links = getattr(obj, "owned_attachment_links", None)
            if prefetched_links is not None:
                items = [link.attachment for link in prefetched_links]
            else:
                attchment_list = CartItemAttachment.objects.filter(cart_item_id=obj.id).values_list("attachment", flat=True)
                items = Attachment.objects.filter(id__in=attchment_list)
            return CartItemAttachmentInPutSerializer(items, many=True).data

        @extend_schema_field(
            serializers.ChoiceField(choices=("STANDARD_COMMERCE", "DIGITAL_PRODUCTS"))
        )
        def get_commerce_authority(self, obj):
            return cart_authority_code(obj)

        @extend_schema_field(DigitalCartSelectionOutputSerializer(allow_null=True))
        def get_digital_selection(self, obj):
            if obj.commerce_authority == ProductCommerceAuthority.STANDARD_COMMERCE:
                return None
            return digital_selection_projection(obj)

        class Meta:
            model = CartItem
            fields = [
                "id",
                "product",
                "price",
                "quantity",
                "cart",
                "attachment",
                "commerce_authority",
                "digital_selection",
            ]

    @extend_schema(responses=CartItemListOutPutSerializer(many=True))
    def get(self, request):
        try:
            cart_items = owned_customer_cart_items(user=request.user)
            return Response(self.CartItemListOutPutSerializer(cart_items, many=True).data, status=status.HTTP_200_OK)
        except DigitalCartProjectionIntegrityError:
            return digital_api_error(
                code="digital_cart_integrity_conflict",
                detail="The Digital Cart selection is inconsistent and cannot be displayed safely.",
                http_status=status.HTTP_409_CONFLICT,
            )
        except Exception as error:
            return Response({"error": "مشکل در دریافت اطلاعات سبد پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class SubmitOrderApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    class OrderOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "total_price",
                "total_price_discount", "schedule", "is_game"
            )

    @extend_schema(request=None, responses=OrderOutPutSerializer)
    def post(self, request):
        product = []
        game = []
        cart_item_list = cart_item_list_user(user=request.user)
        cart = Cart.objects.filter(user=request.user).select_related("active_checkout").first()
        if cart is not None and cart.state == "locked":
            return cart_locked_response(cart)
        type(cart_item_list)
        if len(cart_item_list) <= 0:
            return Response({"error": "کاربر سبد محصولات شما خالی است."}, status=status.HTTP_400_BAD_REQUEST)
        authorities = {item.commerce_authority for item in cart_item_list}
        if len(authorities) > 1:
            return Response(
                {"code": "MIXED_COMMERCE_AUTHORITY_NOT_SUPPORTED", "message": "ترکیب سبد استاندارد و دیجیتال پشتیبانی نمی‌شود."},
                status=status.HTTP_409_CONFLICT,
            )
        if authorities != {ProductCommerceAuthority.STANDARD_COMMERCE}:
            return Response(
                {"code": "DIGITAL_CART_REQUIRES_DIGITAL_CHECKOUT", "message": "این سبد باید از مسیر خرید دیجیتال ادامه یابد."},
                status=status.HTTP_409_CONFLICT,
            )
        for cart_item in cart_item_list:
            if cart_item.product.product_type == ProductType.GAME or cart_item.product.product_type == ProductType.PACKAGE:
                game.append(cart_item)
            else:
                product.append(cart_item)
            attachments = cart_item_attachment_list(cart_item=cart_item)
            attachments_are_valid, attachment_error = validate_product_attachments(
                product=cart_item.product,
                attachments=attachments,
            )
            if not attachments_are_valid:
                return Response({"error": attachment_error}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_limit(product=cart_item.product,
                                       quantity=cart_item.quantity):
                return Response({"error": "تعداد بیش از حد مجاز می باشد."}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_avaliablity(product=cart_item.product,
                                             quantity=cart_item.quantity):
                return Response({"error": "این تعداد محصول موجود نمی باشد"}, status=status.HTTP_400_BAD_REQUEST)
            if not check_attachment_order(attachments=attachments):
                return Response({"error": "بیمه یا گارانتی یا ظرفیت تکراری است "}, status=status.HTTP_400_BAD_REQUEST)
        try:
            orders = submit_order(user=request.user,
                                  total_price=0,
                                  product=product,
                                  game=game,
                                  cart_items=cart_item_list
                                  )
        except StockUnavailableError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.OrderOutPutSerializer(orders, many=True).data, status=status.HTTP_200_OK)


class OrderScheduleOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    type = inline_serializer(fields={
        "name": serializers.CharField(),
        "delivery_type": serializers.IntegerField(),
        "side": serializers.IntegerField()
    })
    schedule = inline_serializer(fields={
        "type": serializers.CharField(),
        "start": serializers.DateTimeField(),
        "end": serializers.DateTimeField()
    })
    address = inline_serializer(fields={
        "address_detail": serializers.CharField(),
        "postal_code": serializers.CharField(),
    })


class OrderUserSummarySerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = BaseUser
        fields = ("id", "firstname", "lastname", "phone_number", "email", "full_name")

    def get_full_name(self, user: BaseUser):
        return " ".join([user.firstname, user.lastname]).strip()


class OrderListCustomerAPIView(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class OrderListCusotmerOutPutSerializer(serializers.ModelSerializer):
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)
        product_images = serializers.SerializerMethodField()


        def get_product_images(self, order: Order):
            order_items = OrderItem.objects.filter(order=order).prefetch_related("product")
            return self.ProductImageOutPutSerializer(order_items, many=True).data

        class ProductImageOutPutSerializer(serializers.Serializer):
            product = inline_serializer(fields={
                "id": serializers.IntegerField(),
                "main_image": serializers.FileField()
            })

            def to_representation(self, instance):
                representation = super().to_representation(instance)
                products_data = representation["product"]
                products_data["main_image"] = reformat_url(url=products_data["main_image"])
                representation["product"] = products_data
                return representation


            
            



        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price", "created_at",
                "product_images", "is_game")

            extra_kwargs = {
                "total_price_discount": {"required": False},
                "total_price": {"required": False},
            }

    @extend_schema(responses=OrderListCusotmerOutPutSerializer(many=True))
    def get(self, request):
        # try:
        orders = order_list_user(user=request.user, is_game=False)
        return Response(self.OrderListCusotmerOutPutSerializer(orders, many=True).data, status=status.HTTP_200_OK)
        # except Exception as e:
        # return Response({"error": "مشکلی پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class OrderDetailUserApi(ApiAuthMixin, APIView):
    permission_classes = [CustomerPermission, ]
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    class OrderDetailInPutSerializer(serializers.Serializer):
        discount = serializers.PrimaryKeyRelatedField(queryset=Discount.objects.filter(is_active=True) , required=False)
        coupon_code = serializers.CharField(required=False, allow_blank=True)
        schedule = serializers.PrimaryKeyRelatedField(queryset=DeliveryData.objects.all(), required=False)
        shipping_address = serializers.PrimaryKeyRelatedField(queryset=Address.objects.all(), required=False)
        shipping_method = serializers.PrimaryKeyRelatedField(queryset=DeliveryType.objects.all(), required=False)

    class OrderDetailOutPutSerializer(serializers.ModelSerializer):
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)

        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price")

    @extend_schema(request=OrderDetailInPutSerializer,
                   responses=OrderDetailOutPutSerializer)
    def put(self, request, id):
        serializer = self.OrderDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        delivery_data = serializer.validated_data.get("schedule")
        shipping_address = serializer.validated_data.get("shipping_address")
        shipping_method = serializer.validated_data.get("shipping_method")
        # TODO: check created is passed more that ten min
        discount = serializer.validated_data.get("discount", None)
        coupon_code = serializer.validated_data.get("coupon_code", "").strip()
        order = Order.objects.filter(id=id, user=request.user).first()
        if order is None:
            return Response({"error": "سفارشی با این مشخصات یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
        if hasattr(order, "financial_payment"):
            return Response(
                {
                    "code": "FINANCIAL_CORE_ORDER_IMMUTABLE",
                    "error": "این سفارش پس از ثبت توسط فرایند مالی قابل ویرایش نیست.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        if not order.is_game and delivery_data is not None:
            return Response(
                {"error": "برای سفارش محصول زمان ارسال انتخاب نمی‌شود. فقط روش ارسال را انتخاب کنید."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.is_game and (shipping_address is not None or shipping_method is not None):
            return Response(
                {"error": "اطلاعات ارسال فقط برای سفارش محصول ثبت می‌شود."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if delivery_data is not None and order.schedule_id is not None:
            if order.schedule_id == delivery_data.id:
                return Response(self.OrderDetailOutPutSerializer(order).data, status=status.HTTP_200_OK)
            return Response({"error": "برای این سفارش قبلا زمان رزرو شده است."}, status=status.HTTP_400_BAD_REQUEST)

        if coupon_code:
            discount_result = validate_discount_code(
                user=request.user,
                code=coupon_code,
                total_price=order_item_payable_total(order=order),
            )
            if not discount_result.is_valid:
                return Response({"error": discount_result.message}, status=status.HTTP_400_BAD_REQUEST)
            discount = discount_result.discount
        elif discount is not None:
            discount_result = validate_discount_code(
                user=request.user,
                code=discount.code,
                total_price=order_item_payable_total(order=order),
            )
            if not discount_result.is_valid:
                return Response({"error": discount_result.message}, status=status.HTTP_400_BAD_REQUEST)
        if delivery_data is not None and delivery_data.address_id is not None and delivery_data.address.user_id != request.user.id:
            return Response({"error": "آدرس زمان ارسال باید برای خود کاربر باشد."},
                            status=status.HTTP_400_BAD_REQUEST)
        if shipping_address is not None and shipping_address.user_id != request.user.id:
            return Response({"error": "آدرس ارسال باید برای خود کاربر باشد."},
                            status=status.HTTP_400_BAD_REQUEST)
        if shipping_method is not None and shipping_method.side != DeliverySide.SENDTOUSER.value:
            return Response({"error": "روش ارسال انتخاب شده برای سفارش محصول معتبر نیست."},
                            status=status.HTTP_400_BAD_REQUEST)
        if delivery_data is not None and Order.objects.filter(schedule=delivery_data).exclude(id=id).exists():
            return Response({"error": "این زمان قبلا برای سفارش دیگری ثبت شده است."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            order = update_order(order_id=order.id,
                                 schedule=delivery_data,
                                 discount=discount,
                                 shipping_address=shipping_address,
                                 shipping_method=shipping_method)
        except DeliverySlotFullError:
            return Response({"error": "ظرفیت این زمان تکمیل شده است."}, status=status.HTTP_400_BAD_REQUEST)
        except DeliveryDataAlreadyUsedError:
            return Response({"error": "این زمان قبلا رزرو شده است."}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "برای این سفارش قبلا زمان رزرو شده است."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.OrderDetailOutPutSerializer(order).data, status=status.HTTP_200_OK)
        # except Exception as e:
        #     return Response({"error": "مشکلی پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class GameListCustomerAPIView(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class GameListCusotmerOutPutSerializer(serializers.ModelSerializer):
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)
        product_images = serializers.SerializerMethodField()

        class ProductImageOutPutSerializer(serializers.Serializer):
            product = inline_serializer(fields={
                "id": serializers.IntegerField(),
                "main_image": serializers.FileField()
            })

            def to_representation(self, instance):
                representation = super().to_representation(instance)
                products_data = representation["product"]
                products_data["main_image"] = reformat_url(url=products_data["main_image"])
                representation["product"] = products_data
                return representation

        def get_product_images(self, order: Order):
            order_items = OrderItem.objects.filter(order=order).prefetch_related('product')
            return self.ProductImageOutPutSerializer(order_items, many=True).data

        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price", "created_at",
                "product_images" , "is_game")

    @extend_schema(responses=GameListCusotmerOutPutSerializer(many=True))
    def get(self, request):
        try:
            orders = order_list_user(user=request.user, is_game=True)
            return Response(self.GameListCusotmerOutPutSerializer(orders, many=True).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class OrderDetailCustomerAPIView(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class OrderDetailCusotmerOutPutSerializer(serializers.ModelSerializer):
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)
        product_data = serializers.SerializerMethodField()
        payment_transaction = serializers.SerializerMethodField()

        class OrderProductDetailOutPutSerializer(serializers.Serializer):
            attachments = serializers.SerializerMethodField()
            product = inline_serializer(fields={
                "id": serializers.IntegerField(),
                "main_image": serializers.FileField(),
                "product_type": serializers.IntegerField(),
                "title": serializers.CharField(),
                "slug": serializers.CharField(),
                "price": serializers.DecimalField(max_digits=25, decimal_places=0),
                "off_price": serializers.DecimalField(max_digits=25, decimal_places=0),

            })
            quantity = serializers.IntegerField()
            price = serializers.DecimalField(max_digits=16, decimal_places=0)

            def get_attachments(self, order_item: OrderItem):
                attachment_ids = OrderItemAttachment.objects.filter(
                    order_item_id=order_item.id
                ).values_list("attachment", flat=True)
                items = Attachment.objects.filter(id__in=attachment_ids)
                return CartItemAttachmentInPutSerializer(items, many=True).data

        def to_representation(self, instance):
            representation = super().to_representation(instance)
            product_data = representation["product_data"]
            for product in product_data:
                product['product']["main_image"] = reformat_url(url = product['product']["main_image"])
            representation["product"] = product_data
            return representation

        def get_product_data(self, order: Order):
            order_items = OrderItem.objects.filter(order=order).prefetch_related('product')
            return self.OrderProductDetailOutPutSerializer(order_items, many=True).data

        def get_payment_transaction(self, order: Order):
            return get_latest_order_transaction_summary(order=order)

        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price", "created_at",
                "product_data", "is_game",
                "payment_transaction")

            extra_kwargs = {
                "total_price_discount": {"required": False},
                "total_price": {"required": False},
            }

    @extend_schema(responses=OrderDetailCusotmerOutPutSerializer)
    def get(self, request, id):
        try:
            order = Order.objects.filter(id=id, user=request.user).first()
            if order is None:
                return Response({"error": "سفارش یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
            return Response(self.OrderDetailCusotmerOutPutSerializer(order).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی یافتن سفارش پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class OrderListAdminAPIView(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class OrderListAdminOutPutSerializer(serializers.ModelSerializer):
        user = OrderUserSummarySerializer(read_only=True)
        customer = OrderUserSummarySerializer(source="user", read_only=True)
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)
        product_images = serializers.SerializerMethodField()

        class ProductImageOutPutSerializer(serializers.Serializer):
            product = inline_serializer(fields={
                "id": serializers.IntegerField(),
                "main_image": serializers.FileField()
            })

            def to_representation(self, instance):
                representation = super().to_representation(instance)
                products_data = representation["product"]
                products_data["main_image"] = reformat_url(url=products_data["main_image"])
                representation["product"] = products_data
                return representation

        def get_product_images(self, order: Order):
            order_items = OrderItem.objects.filter(order=order).prefetch_related("product")
            return self.ProductImageOutPutSerializer(order_items, many=True).data

        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price", "created_at",
                "product_images", "is_game", "user", "customer")

            extra_kwargs = {
                "total_price_discount": {"required": False},
                "total_price": {"required": False},
            }

    @extend_schema(responses=OrderListAdminOutPutSerializer(many=True))
    def get(self, request):
        orders = (
            Order.objects.filter(is_game=False)
            .select_related(
                "user",
                "schedule__type",
                "schedule__schedule",
                "schedule__address",
                "shipping_address",
                "shipping_method",
            )
            .prefetch_related("order_items__product")
            .order_by("-created_at")
        )
        return Response(self.OrderListAdminOutPutSerializer(orders, many=True).data, status=status.HTTP_200_OK)


class OrderDetailAdminAPIView(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class OrderDetailAdminOutPutSerializer(serializers.ModelSerializer):
        user = OrderUserSummarySerializer(read_only=True)
        customer = OrderUserSummarySerializer(source="user", read_only=True)
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)
        product_data = serializers.SerializerMethodField()
        payment_transaction = serializers.SerializerMethodField()

        class OrderProductDetailOutPutSerializer(serializers.Serializer):
            product = inline_serializer(fields={
                "id": serializers.IntegerField(),
                "main_image": serializers.FileField(),
                "product_type": serializers.IntegerField(),
                "title": serializers.CharField(),
                "slug": serializers.CharField(),
                "price": serializers.DecimalField(max_digits=25, decimal_places=0),
                "off_price": serializers.DecimalField(max_digits=25, decimal_places=0),

            })

        def get_product_data(self, order: Order):
            order_items = OrderItem.objects.filter(order=order).prefetch_related("product")
            product_data = self.OrderProductDetailOutPutSerializer(order_items, many=True).data
            for product in product_data:
                product["product"]["main_image"] = reformat_url(url=product["product"]["main_image"])
            return product_data

        def get_payment_transaction(self, order: Order):
            return get_latest_order_transaction_summary(order=order)

        class Meta:
            model = Order
            fields = (
                "id", "public_tracking_code", "discount", "payment_status", "user_status", "schedule",
                "shipping_address", "shipping_method", "total_price_discount", "total_price", "created_at",
                "product_data", "is_game", "payment_transaction", "user", "customer")

            extra_kwargs = {
                "total_price_discount": {"required": False},
                "total_price": {"required": False},
            }

    @extend_schema(responses=OrderDetailAdminOutPutSerializer)
    def get(self, request, id):
        order = (
            Order.objects.select_related(
                "user",
                "schedule__type",
                "schedule__schedule",
                "schedule__address",
                "shipping_address",
                "shipping_method",
            )
            .prefetch_related("order_items__product")
            .filter(id=id, is_game=False)
            .first()
        )
        if order is None:
            return Response({"error": "سفارش یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.OrderDetailAdminOutPutSerializer(order).data, status=status.HTTP_200_OK)


class SellReport(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class OrderReportFitler(serializers.Serializer):
        updated_at__range = serializers.CharField(max_length=200 , required=False)

    @extend_schema(parameters=[OrderReportFitler] , responses={200 : dict })
    def get(self , request):
        filter_serializer = self.OrderReportFitler(data = request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        # try:
        report = sell_report(filters=filter_serializer.validated_data.get("updated_at__range"))
        return Response(report, status=status.HTTP_200_OK)
        # except Exception as e:
        #     return Response({"error": "مشکلی پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)

class IsBoughtProductAPIView(APIView):

    class IsBoughtInPutSerializer(serializers.Serializer):
        product = serializers.CharField(max_length=15 , required=False)

    class IsBoughtOutPutSerializer(serializers.Serializer):
        is_bought = serializers.CharField()

    @extend_schema(request=IsBoughtInPutSerializer , responses={200: IsBoughtOutPutSerializer})
    def post(self, request):
        serializer = self.IsBoughtInPutSerializer(data = request.data)
        serializer.is_valid(raise_exception=True)
        if not request.user.is_authenticated:
            return Response({"is_bought": "False"}, status=status.HTTP_200_OK)
        product = serializer.validated_data.get("product" ,None)
        if product is None:
            return Response({"is_bought": "False"}, status=status.HTTP_200_OK)
        is_bought = bought_order_item(user=request.user , product_id=product)
        if  is_bought== True:
            return Response({"is_bought": "True"}, status=status.HTTP_200_OK)
        if is_bought== False:
            return Response({"is_bought": "False"}, status=status.HTTP_200_OK)
