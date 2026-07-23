from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.product.models import CategoryType, Category, ProductCategory, Product
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.product.selectors.category import get_category_list, get_all_categories
from cheatgame.product.services.category import create_category, create_product_categories, update_category, \
    delete_category, update_product_category, delete_product_category


class CategoryAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class CategoryInPutSerializer(serializers.Serializer):
        category_type = serializers.ChoiceField(choices=CategoryType.choices())
        name = serializers.CharField(max_length=50, required=True)
        slug = serializers.SlugField(max_length=50, required=False, allow_blank=True, allow_unicode=True)
        parent = serializers.PrimaryKeyRelatedField(required=False, allow_null=True, queryset=Category.objects.all())

    class CategoryOutPutSerializer(serializers.ModelSerializer):
        product_count = serializers.SerializerMethodField()
        total_product_count = serializers.SerializerMethodField()

        def get_product_count(self, obj):
            return ProductCategory.objects.filter(category=obj).count()

        def get_total_product_count(self, obj):
            category_ids = obj.get_descendants(include_self=True).values_list("id", flat=True)
            return ProductCategory.objects.filter(category_id__in=category_ids).count()

        class Meta:
            model = Category
            fields = ("id", "name", "slug", "category_type", "parent", "level", "product_count", "total_product_count",)

    @extend_schema(request=CategoryInPutSerializer, responses={status.HTTP_201_CREATED:CategoryOutPutSerializer})
    def post(self, request):
        serializer = self.CategoryInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            category = create_category(
                name=serializer.validated_data.get("name"),
                category_type=serializer.validated_data.get("category_type"),
                parent=serializer.validated_data.get("parent"),
                slug=serializer.validated_data.get("slug", ""),
            )
            return Response(self.CategoryOutPutSerializer(category).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": str(error) or "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class CategoryDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class CategoryDetailInPutSerializer(serializers.Serializer):
        category_type = serializers.ChoiceField(choices=CategoryType.choices())
        name = serializers.CharField(max_length=50, required=True)
        slug = serializers.SlugField(max_length=50, required=False, allow_blank=True, allow_unicode=True)
        parent = serializers.PrimaryKeyRelatedField(required=False, allow_null=True, queryset=Category.objects.all())

    class CategoryDetailOutPutSerializer(serializers.ModelSerializer):
        product_count = serializers.SerializerMethodField()
        total_product_count = serializers.SerializerMethodField()

        def get_product_count(self, obj):
            return ProductCategory.objects.filter(category=obj).count()

        def get_total_product_count(self, obj):
            category_ids = obj.get_descendants(include_self=True).values_list("id", flat=True)
            return ProductCategory.objects.filter(category_id__in=category_ids).count()

        class Meta:
            model = Category
            fields = ("id", "name", "slug", "category_type", "parent", "level", "product_count", "total_product_count",)

    @extend_schema(request=CategoryDetailInPutSerializer,
                   responses={status.HTTP_200_OK: CategoryDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.CategoryDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            category = update_category(
                category_id=id,
                category_type=serializer.validated_data.get("category_type"),
                parent=serializer.validated_data.get("parent"),
                name=serializer.validated_data.get("name"),
                slug=serializer.validated_data.get("slug", ""),
            )
            return Response(self.CategoryDetailOutPutSerializer(category).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": str(error) or "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: int):
        try:
            delete_category(category_id=id)
            return Response({"message": "آیتم مورد نظر با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": str(error) or "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class CategoryListOutPutSerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()
    product_count = serializers.SerializerMethodField()
    total_product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ("id", "name", "slug", "category_type", "parent", "level", "product_count", "total_product_count", "children")

    def get_children(self, obj) -> dict :
        children = obj.get_children()
        serializer = CategoryListOutPutSerializer(children, many=True)
        return serializer.data

    def get_product_count(self, obj):
        return ProductCategory.objects.filter(category=obj).count()

    def get_total_product_count(self, obj):
        category_ids = obj.get_descendants(include_self=True).values_list("id", flat=True)
        return ProductCategory.objects.filter(category_id__in=category_ids).count()


class CategoryListApi(APIView):

    @extend_schema(responses={status.HTTP_200_OK: CategoryListOutPutSerializer})
    def get(self, request, category_type):
        try:
            categories = get_category_list(category_type=category_type)
            return Response(CategoryListOutPutSerializer(categories, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

class CategoryListAdminApi(APIView):
    permission_classes = [AdminOrManagerPermission ,]

    @extend_schema(responses={status.HTTP_200_OK: CategoryListOutPutSerializer})
    def get(self , request):
        try:
            categories = get_all_categories()
            return Response(CategoryListOutPutSerializer(categories, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProductCategoryAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class ProductCategoryInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type__in=(CategoryType.PRODUCT, CategoryType.GAME, CategoryType.GIFTCART)))

    @extend_schema(
        request=ProductCategoryInPutSerializer(many=True),
        responses={status.HTTP_201_CREATED: dict}
    )
    def post(self, request):
        serializer = self.ProductCategoryInPutSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        try:
            bulk_list = []
            for product_category_data in serializer.validated_data:
                product = product_category_data['product']
                category = product_category_data['category']
                bulk_list.append(ProductCategory(product=product, category=category))
            create_product_categories(product_category=bulk_list)
            return Response({"message": "دسته بندی محصولات با موفقبت ساخته شد."}, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

class ProductCategoryDetailApi(ApiAuthMixin, APIView):
    class ProductCategoryDetailInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type__in=(CategoryType.PRODUCT, CategoryType.GAME, CategoryType.GIFTCART)))

    class ProuductCategoryDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ProductCategory
            fields = ("id", "product", "category",)

    @extend_schema(request=ProductCategoryDetailInPutSerializer, responses={status.HTTP_200_OK:ProuductCategoryDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.ProductCategoryDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product_category = update_product_category(
                product_category_id=id,
                product=serializer.validated_data.get("product"),
                category=serializer.validated_data.get("category")
            )
            return Response(self.ProuductCategoryDetailOutPutSerializer(product_category).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK:dict})
    def delete(self, reqeust, id: int):
        try:
            delete_product_category(
                product_category_id=id
            )
            return Response({"message": "دسته بندی محصول با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
