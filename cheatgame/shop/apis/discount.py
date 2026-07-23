from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import get_paginated_response, LimitOffsetPagination, PaginatedSerializer
from cheatgame.product.models import DeliveryOption
from cheatgame.product.permissions import ManagerPermission, CustomerPermission
from cheatgame.shop.models import DiscountType, DiscountValueType, Discount, DeliverySide, DeliveryType, UserDiscount
from cheatgame.shop.selectors.discount import (
    discount_list_admin,
    discount_list_user,
    serialize_discount_validation,
    validate_discount_code,
)
from cheatgame.shop.services.delivery_type import create_delivery_type
from cheatgame.shop.services.discount import create_discount, update_discount, delete_discount


class DiscountAdminApi(ApiAuthMixin, APIView):
    permission_classes = (ManagerPermission,)

    class DiscountInPutSerializer(serializers.Serializer):
        name = serializers.CharField(max_length=100)
        type = serializers.ChoiceField(choices=DiscountType.choices())
        value_type = serializers.ChoiceField(choices=DiscountValueType.choices())
        valid_from = serializers.DateTimeField()
        valid_until = serializers.DateTimeField()
        is_active = serializers.BooleanField()
        amount = serializers.DecimalField(required=False, max_digits=16, decimal_places=0)
        percent = serializers.IntegerField(required=False)
        usage_number = serializers.IntegerField()
        min_purchase_amount = serializers.DecimalField(required=False, max_digits=16, decimal_places=0)

    class DiscountOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Discount
            fields = "__all__"

    @extend_schema(request=DiscountInPutSerializer, responses=DiscountOutPutSerializer)
    def post(self, request):
        serializer = self.DiscountInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            discount = create_discount(
                name=serializer.validated_data.get("name"),
                type=serializer.validated_data.get("type"),
                value_type=serializer.validated_data.get("value_type"),
                valid_from=serializer.validated_data.get("valid_from"),
                valid_until=serializer.validated_data.get("valid_until"),
                is_active=serializer.validated_data.get("is_active"),
                min_purchase_amount=serializer.validated_data.get("min_purchase_amount"),
                amount=serializer.validated_data.get("amount"),
                percent=serializer.validated_data.get("percent"),
                admin_user=request.user,
                usage_number=serializer.validated_data.get("usage_number")
            )
            return Response(self.DiscountOutPutSerializer(discount).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی در ساخت کد پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class DiscountDetailSerializer(ApiAuthMixin, APIView):
    permission_classes = (ManagerPermission,)

    class DiscountDetailInPutSerializer(serializers.Serializer):
        name = serializers.CharField(max_length=100)
        type = serializers.ChoiceField(choices=DiscountType.choices())
        value_type = serializers.ChoiceField(choices=DiscountValueType.choices())
        valid_from = serializers.DateTimeField()
        valid_until = serializers.DateTimeField()
        is_active = serializers.BooleanField()
        amount = serializers.DecimalField(required=False, max_digits=16, decimal_places=0)
        percent = serializers.IntegerField(required=False)
        usage_number = serializers.IntegerField()

    class DiscountDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Discount
            fields = "__all__"

    @extend_schema(request=DiscountDetailInPutSerializer, responses=DiscountDetailOutPutSerializer)
    def put(self, request, id: int):
        serializer = self.DiscountDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            discount = update_discount(
                name=serializer.validated_data.get("name"),
                type=serializer.validated_data.get("type"),
                value_type=serializer.validated_data.get("value_type"),
                valid_from=serializer.validated_data.get("valid_from"),
                valid_until=serializer.validated_data.get("valid_until"),
                is_active=serializer.validated_data.get("is_active"),
                min_purchase_amount=serializer.validated_data.get("min_purchase_amount"),
                amount=serializer.validated_data.get("amount"),
                percent=serializer.validated_data.get("percent"),
                admin_user=serializer.validated_data.get("admin_user"),
                usage_number=serializer.validated_data.get("usage_number"),
                discount_id=id
            )
            return Response(self.DiscountDetailOutPutSerializer(discount).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"errro": "ویرایش انجام نشد."}, status=status.HTTP_400_BAD_REQUEST)
    @extend_schema(responses={status.HTTP_200_OK:dict})
    def delete(self, request, id: int):
        try:
            delete_discount(discount_id=id)
            return Response({"message": "کد تخفیف با موفقیت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در حذف کدتخفیف پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)

class DiscountListOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Discount
        fields = "__all__"
class DiscountListAdmin(ApiAuthMixin, APIView):
    permission_classes = (ManagerPermission,)

    class Pagination(LimitOffsetPagination):
        default_limit = 10



    class PaginatedDiscountListOutPutSerializer(PaginatedSerializer):
        result = DiscountListOutPutSerializer(many=True)
        
        
    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)



    @extend_schema(responses=PaginatedDiscountListOutPutSerializer , parameters=[PaginationParameterSerializer])
    def get(self, request):
        try:
            discounts = discount_list_admin()
            return get_paginated_response(
                request=request,
                queryset=discounts,
                serializer_class=DiscountListOutPutSerializer,
                view=self,
                pagination_class=self.Pagination
            )
        except Exception as error:
            return Response({"error": "مشکلی در دریافت لیست پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)


class UserDiscountListOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Discount
        fields = (
            "id", "name", "code", "valid_from", "valid_until", "is_active", "min_purchase_amount", "amount", "percent",
            "value_type")

class UserDiscountOutPutSerializer(serializers.Serializer):
    discount = UserDiscountListOutPutSerializer()

class DiscountListUser(ApiAuthMixin, APIView):
    class Pagination(LimitOffsetPagination):
        default_limit = 10



        class Meta:
            model = UserDiscount
            fields = ("id", "discount" , )

    class PaginatedDiscountListOutPutSerializer(PaginatedSerializer):
        result = UserDiscountListOutPutSerializer(many=True)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)
            
    @extend_schema(responses=PaginatedDiscountListOutPutSerializer , parameters=[PaginationParameterSerializer])
    def get(self , request , *args , **kwargs):
        # try:
        discounts = discount_list_user(user = request.user)
        return get_paginated_response(
            request=request,
            queryset=discounts,
            serializer_class=UserDiscountOutPutSerializer,
            view=self,
            pagination_class=self.Pagination
        )
        # except Exception as error:
        #     return Response({"error": "مشکلی در دریافت لیست پیش آمد"}, status=status.HTTP_400_BAD_REQUEST)
        


class CheckUserDiscountApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class CheckDiscountInPutSerializer(serializers.Serializer):
        code = serializers.CharField(max_length=100)
        total_price = serializers.DecimalField(max_digits=16, decimal_places=0)

    @extend_schema(request=CheckDiscountInPutSerializer, responses=bool)
    def post(self, request):
        serializer = self.CheckDiscountInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = validate_discount_code(
            code=serializer.validated_data.get("code"),
            total_price=serializer.validated_data.get("total_price"),
            user=request.user,
        )
        return Response(serialize_discount_validation(result), status=status.HTTP_200_OK)


class CheckCouponApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class CheckCouponInPutSerializer(serializers.Serializer):
        code = serializers.CharField(max_length=100)
        total_price = serializers.DecimalField(max_digits=16, decimal_places=0)

    @extend_schema(request=CheckCouponInPutSerializer, responses=bool)
    def post(self, request):
        serializer = self.CheckCouponInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = validate_discount_code(
            code=serializer.validated_data.get("code"),
            total_price=serializer.validated_data.get("total_price"),
        )
        return Response(serialize_discount_validation(result), status=status.HTTP_200_OK)
