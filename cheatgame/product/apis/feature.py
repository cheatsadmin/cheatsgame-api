from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.utils import inline_serializer
from cheatgame.product.models import FeatureType, Feature, ValuesList, Category, CategoryType, Product
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.product.selectors.feature import get_all_features
from cheatgame.product.services.feature import create_feature, create_product_feature, update_feature, delete_feature, \
    update_product_feature, delete_product_feature


class FeatureAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class FeatureInPutSerializer(serializers.Serializer):
        name = serializers.CharField(max_length=100)
        feature_type = serializers.ChoiceField(
            choices=FeatureType.choices()
        )
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type=CategoryType.FEATURE))

        def validate_name(self, value):
            clean_value = value.strip()
            if not clean_value:
                raise serializers.ValidationError("عنوان ویژگی را وارد کنید.")
            return clean_value

    class FeatureOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Feature
            fields = ("id", "name", "feature_type", "category")

    @extend_schema(request=FeatureInPutSerializer, responses=FeatureOutPutSerializer)
    def post(self, request):
        serializer = self.FeatureInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            feature = create_feature(
                name=serializer.validated_data.get("name"),
                feature_type=serializer.validated_data.get("feature_type"),
                category=serializer.validated_data.get("category")
            )
            return Response(self.FeatureOutPutSerializer(feature).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class FeatureDetailAdminApi(ApiAuthMixin, APIView):
    class FeatureDetailInPutSerializer(serializers.Serializer):
        name = serializers.CharField(max_length=100)
        feature_type = serializers.ChoiceField(
            choices=FeatureType.choices()
        )
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type=CategoryType.FEATURE))

    class FeatureDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Feature
            fields = ("id", "name", "feature_type", "category")

    @extend_schema(request=FeatureDetailInPutSerializer, responses={status.HTTP_200_OK:FeatureDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.FeatureDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            feature = update_feature(
                feature_id=id,
                name=serializer.validated_data.get("name"),
                category=serializer.validated_data.get("category"),
                feature_type=serializer.validated_data.get("feature_type")
            )
            return Response(self.FeatureDetailOutPutSerializer(feature).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK:dict})
    def delete(self, request, id: int):
        try:
            delete_feature(feature_id=id)
            return Response({"message": "آیتم مورد نظر با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProductFeatureAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class ProductFeatureInPutSerializer(serializers.Serializer):
        value = serializers.CharField(max_length=100, trim_whitespace=True)
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        feature = serializers.PrimaryKeyRelatedField(required=True, queryset=Feature.objects.all())

        def validate_value(self, value):
            if not value:
                raise serializers.ValidationError("مقدار ویژگی محصول را وارد کنید.")
            return value

    class ProductFeatureOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ValuesList
            fields = ("id", "product", "feature", "value")

    @extend_schema(request=ProductFeatureInPutSerializer, responses=ProductFeatureOutPutSerializer)
    def post(self, request):
        serializer = self.ProductFeatureInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product_value = create_product_feature(
                value=serializer.validated_data.get("value"),
                product=serializer.validated_data.get("product"),
                feature=serializer.validated_data.get("feature")
            )
            return Response(self.ProductFeatureOutPutSerializer(product_value).data, status=status.HTTP_201_CREATED)

        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class FeatureCategoryOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id" , "name" , "slug" , "category_type")
class FeatureListAdminApi(ApiAuthMixin ,APIView):
    permission_classes = [AdminOrManagerPermission ,]



    class ProductFeatureListOutPutSerializer(serializers.ModelSerializer):
        category  = FeatureCategoryOutPutSerializer()
        class Meta:
            model = Feature
            fields = ("id" , "feature_type" , "category"  , "name")

    @extend_schema(responses=ProductFeatureListOutPutSerializer(many=True))
    def get(self , request , *args, **kwargs):
        try:
            feature_list = get_all_features()
            return Response(self.ProductFeatureListOutPutSerializer(feature_list , many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در لیست فیچر ها وجود دارد."}, status=status.HTTP_400_BAD_REQUEST)

class ProductFeatureDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class ProductFeatureDetailInPutSerializer(serializers.Serializer):
        value = serializers.CharField(max_length=100, trim_whitespace=True)
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        feature = serializers.PrimaryKeyRelatedField(required=True, queryset=Feature.objects.all())

        def validate_value(self, value):
            if not value:
                raise serializers.ValidationError("مقدار ویژگی محصول را وارد کنید.")
            return value

    class ProductFeatureDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ValuesList
            fields = ("id", "product", "feature", "value")

    @extend_schema(request=ProductFeatureDetailInPutSerializer, responses={status.HTTP_200_OK:ProductFeatureDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.ProductFeatureDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product_feature = update_product_feature(
                product_feature_id=id,
                product=serializer.validated_data.get("product"),
                feature=serializer.validated_data.get("feature"),
                value=serializer.validated_data.get("value"),
            )
            return Response(self.ProductFeatureDetailOutPutSerializer(product_feature).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
    @extend_schema(responses={status.HTTP_200_OK:dict})
    def delete(self, request, id: int):
        try:
            delete_product_feature(product_feature_id=id)
            return Response({"message": "ویژگی محصول حذف گردید."}, status=status.HTTP_204_NO_CONTENT)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
