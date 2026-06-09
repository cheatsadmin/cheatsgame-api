from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.utils import inline_serializer
from cheatgame.common.utils import reformat_url
from cheatgame.product.apis.product import ProductDetailProductSerializer
from cheatgame.product.models import Attachment, Product, ProductType
from cheatgame.product.permissions import CustomerPermission, CartItemIsOwnerCustomer
from cheatgame.product.selectors.product import suggestions_product
from cheatgame.shop.models import CartItem, Order, Discount, DeliveryData, OrderItem, CartItemAttachment
from cheatgame.shop.selectors.cart import cart_item_list_user, cart_item_attachment_list, order_list_user, \
    check_order_exists, get_order, sell_report, bought_order_item
from cheatgame.shop.selectors.discount import check_discount_code, check_coupon_code
from cheatgame.shop.services.cart import check_product_limit, check_product_avaliablity, check_attachment, \
    check_cart_item_exists, add_to_cart, update_cart_item, delete_cart_item, check_attachment_order
from cheatgame.shop.services.order import submit_order, update_order


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
        suggestions = suggestions_product(product=product)
        return ProductDetailProductSerializer(suggestions, many=True).data

    class Meta:
        model = Product
        fields = ("id", "product_type", "title", "slug", "main_image", "price", "off_price", "quantity", "device_model",
                  "suggestion" )


class AddToCart(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

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

            if not len(serializer.validated_data.get("attachment")) > 0:
                return Response({"error": "انتخاب حداقل یک  گارانتی بیمه و یا ظرفیت اجباری است. "}, status = status.HTTP_400_BAD_REQUEST)
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
        except Exception as error:
            return Response({"erorr": "محصول به سبد اضافه نشد"}, status=status.HTTP_400_BAD_REQUEST)


class CartItemDetail(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, CartItemIsOwnerCustomer,)

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
            cart_item = CartItem.objects.get(id=id)
            if not check_product_limit(product=cart_item.product,
                                       quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "تعداد بیش از حد مجاز می باشد."}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_avaliablity(product=cart_item.product,
                                             quantity=serializer.validated_data.get("quantity")):
                return Response({"error": "این تعداد محصول موجود نمی باشد"}, status=status.HTTP_400_BAD_REQUEST)

            cart_item = update_cart_item(quantity=serializer.validated_data.get("quantity"),
                                         cart_item=cart_item)
            return Response(self.CartItemDetailOutPutSerializer(cart_item).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "خطایی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: int):
        try:
            delete_cart_item(cart_item_id=id)
            return Response({"message": "محصول از سبد حذف شد"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class CartItemListApi(ApiAuthMixin, APIView):
    class CartItemListOutPutSerializer(serializers.ModelSerializer):
        product = ProductDetailCartSerializer()
        attachment = serializers.SerializerMethodField()

        def get_attachment(self, obj):
            attchment_list = CartItemAttachment.objects.filter(cart_item_id=obj.id).values_list("attachment", flat=True)
            items = Attachment.objects.filter(id__in= attchment_list)
            return CartItemAttachmentInPutSerializer(items, many=True).data

        class Meta:
            model = CartItem
            fields =["id" , "product" , "price" , "cart" , "attachment"]

    @extend_schema(responses=CartItemListOutPutSerializer)
    def get(self, request):
        try:
            cart_items = cart_item_list_user(user=request.user)
            return Response(self.CartItemListOutPutSerializer(cart_items, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکل در دریافت اطلاعات سبد پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class SubmitOrderApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class OrderOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Order
            fields = ("id", "discount", "payment_status", "total_price", "total_price_discount", "schedule", "is_game")

    @extend_schema(request=None, responses=OrderOutPutSerializer)
    def post(self, request):
        product = []
        game = []
        cart_item_list = cart_item_list_user(user=request.user)
        type(cart_item_list)
        if len(cart_item_list) <= 0:
            return Response({"error": "کاربر سبد محصولات شما خالی است."}, status=status.HTTP_400_BAD_REQUEST)
        for cart_item in cart_item_list:
            if cart_item.product.product_type == ProductType.GAME or cart_item.product.product_type == ProductType.PACKAGE:
                game.append(cart_item)
            else:
                product.append(cart_item)
            attachments = cart_item_attachment_list(cart_item=cart_item)
            if not check_product_limit(product=cart_item.product,
                                       quantity=cart_item.quantity):
                return Response({"error": "تعداد بیش از حد مجاز می باشد."}, status=status.HTTP_400_BAD_REQUEST)
            if not check_product_avaliablity(product=cart_item.product,
                                             quantity=cart_item.quantity):
                return Response({"error": "این تعداد محصول موجود نمی باشد"}, status=status.HTTP_400_BAD_REQUEST)
            if not check_attachment_order(attachments=attachments):
                return Response({"error": "بیمه یا گارانتی یا ظرفیت تکراری است "}, status=status.HTTP_400_BAD_REQUEST)
        orders = submit_order(user=request.user,
                              total_price=0,
                              product=product,
                              game=game,
                              cart_items=cart_item_list
                              )
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
                "id", "discount", "payment_status", "user_status", "schedule", "total_price_discount", "total_price",
                "created_at", "product_images", "is_game")

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

    class OrderDetailInPutSerializer(serializers.Serializer):
        discount = serializers.PrimaryKeyRelatedField(queryset=Discount.objects.filter(is_active=True) , required=False)
        schedule = serializers.PrimaryKeyRelatedField(queryset=DeliveryData.objects.filter(is_used=False),
                                                      required=False)

    class OrderDetailOutPutSerializer(serializers.ModelSerializer):
        schedule = OrderScheduleOutPutSerializer(read_only=False, required=False)

        class Meta:
            model = Order
            fields = (
                "id", "discount", "payment_status", "user_status", "schedule", "total_price_discount", "total_price")

    @extend_schema(request=OrderDetailInPutSerializer,
                   responses=OrderDetailOutPutSerializer)
    def put(self, request, id):
        serializer = self.OrderDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        delivery_data = serializer.validated_data.get("schedule")
        # TODO: check created is passed more that ten min
        discount = serializer.validated_data.get("discount", None)
        order = Order.objects.filter(id=id).first()
        if order is None:
            return Response({"error": "سفارشی با این مشخصات یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)

        if discount is not None:
            dicount_code_result = check_discount_code(user=request.user, code=discount.code,
                                                      total_price=order.total_price)
            coupon_code_result = check_coupon_code(total_price=order.total_price, code=discount.code)
            if dicount_code_result == False and coupon_code_result == False:
                return Response({"error": "کد تخفیف  معتبر نیست."}, status=status.HTTP_400_BAD_REQUEST)
        if delivery_data is not None and Order.objects.filter(schedule=delivery_data).exclude(id=id).exists():
            return Response({"error": "این زمان قبلا برای سفارش دیگری ثبت شده است."},
                            status=status.HTTP_400_BAD_REQUEST)
        order = update_order(order_id=id,
                             schedule=delivery_data, discount=discount)
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
                "id", "discount", "payment_status", "user_status", "schedule", "total_price_discount", "total_price",
                "created_at", "product_images" , "is_game")

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

        class Meta:
            model = Order
            fields = (
                "id", "discount", "payment_status", "user_status", "schedule", "total_price_discount", "total_price",
                "created_at", "product_data", "is_game")

            extra_kwargs = {
                "total_price_discount": {"required": False},
                "total_price": {"required": False},
            }

    @extend_schema(responses=OrderDetailCusotmerOutPutSerializer)
    def get(self, request, id):
        try:
            if not check_order_exists(order_id=id):
                return Response({"error": "سفارش یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
            order = get_order(order_id=id)
            return Response(self.OrderDetailCusotmerOutPutSerializer(order).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی یافتن سفارش پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class SellReport(APIView):

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




