from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils.text import slugify

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination, get_paginated_response, PaginatedSerializer
from cheatgame.api.utils import inline_serializer
from cheatgame.common.utils import reformat_url
from cheatgame.product.models import ProductType, Product, ProductOrderBy, ProductStatus, Image, Category, CategoryType, Feature, ValuesList, \
    Attachment, Question, Label, ProductNote, Reviews, ReviewStatus
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.product.selectors.product import product_list, product_detail
from cheatgame.product.services.product import create_product, create_product_note, update_product_note, \
    delete_product_note, update_product, check_product_exists, delete_product, ProductDeleteProtectedError, \
    ProductDeleteDependencyError
from cheatgame.users.models import BaseUser, UserTypes


SELLABLE_CATEGORY_TYPES = (
    CategoryType.PRODUCT,
    CategoryType.GAME,
    CategoryType.GIFTCART,
)


def can_manage_products(request) -> bool:
    return (
        request.user
        and request.user.is_authenticated
        and request.user.user_type in (UserTypes.ADMIN, UserTypes.MANAGER)
    )


class ProductCategorySummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "category_type", "parent")


def get_product_categories(product: Product):
    return [product_category.category for product_category in product.categories.all()]


def validate_product_categories(attrs):
    categories = attrs.get("categories") or []
    allow_uncategorized = attrs.get("allow_uncategorized", False)
    if not categories and not allow_uncategorized:
        raise serializers.ValidationError({
            "categories": "حداقل یک دسته‌بندی محصول را انتخاب کنید یا گزینه بدون دسته‌بندی را آگاهانه فعال کنید."
        })
    return attrs


def validate_unique_slug(value: str, *, product_id: int = None):
    if not value:
        return value
    normalized_slug = slugify(value, allow_unicode=True)
    queryset = Product.objects.filter(slug=normalized_slug)
    if product_id:
        queryset = queryset.exclude(id=product_id)
    if queryset.exists():
        raise serializers.ValidationError("این اسلاگ قبلا برای محصول دیگری استفاده شده است.")
    return normalized_slug


class ProductDetailProductSerializer(serializers.ModelSerializer):
    main_image = serializers.SerializerMethodField()
    seo_title = serializers.SerializerMethodField()

    def get_main_image(self, obj):
        return reformat_url(url=obj.main_image.url)

    def get_seo_title(self, obj):
        return obj.seo_title or obj.title

    class Meta:
        model = Product
        fields = ("id", "product_type", "title", "slug", "status", "seo_title", "main_image",
                  "price", "off_price", "quantity", "device_model")


class ProductAdminApi(ApiAuthMixin, APIView):
    parser_classes = (MultiPartParser, FormParser)

    permission_classes = (AdminOrManagerPermission,)

    class ProductCreateInputSerializer(serializers.Serializer):
        product_type = serializers.ChoiceField(choices=ProductType.choices())
        title = serializers.CharField(max_length=100)
        slug = serializers.CharField(max_length=120, required=False, allow_blank=True)
        status = serializers.ChoiceField(choices=ProductStatus.choices, required=False, default=ProductStatus.PUBLISHED)
        seo_title = serializers.CharField(max_length=120, required=False, allow_blank=True)
        meta_description = serializers.CharField(max_length=300, required=False, allow_blank=True)
        main_image = serializers.FileField(required=True)
        price = serializers.DecimalField(max_digits=15, decimal_places=0)
        off_price = serializers.DecimalField(max_digits=15, decimal_places=0)
        quantity = serializers.IntegerField(required=True)
        discount_end_time = serializers.DateTimeField(required=False)
        description = serializers.FileField()
        order_limit = serializers.IntegerField(required=False)
        device_model = serializers.CharField(max_length=100, required=False, allow_blank=True)
        categories = serializers.PrimaryKeyRelatedField(
            required=False,
            many=True,
            queryset=Category.objects.filter(category_type__in=SELLABLE_CATEGORY_TYPES),
        )
        allow_uncategorized = serializers.BooleanField(required=False, default=False, write_only=True)
        included_products = serializers.PrimaryKeyRelatedField(required=False, many=True,
                                                               queryset=Product.objects.filter(
                                                                   product_type=ProductType.GAME))

        def validate_slug(self, value):
            return validate_unique_slug(value)

        def validate(self, attrs):
            return validate_product_categories(attrs)

        def validate_included_products(self, included_products):
            if len(included_products) > 5:
                raise serializers.ValidationError("حداکثر تعداد محصول مجاز ۵ عدد می باشد.")
            return included_products

    class ProuductCreateOutputSerializer(serializers.ModelSerializer):
        main_image = serializers.SerializerMethodField()
        description = serializers.SerializerMethodField()
        seo_title = serializers.SerializerMethodField()
        categories = serializers.SerializerMethodField()

        def get_main_image(self, obj):
            return reformat_url(url=obj.main_image.url)

        def get_description(self, obj):
            return reformat_url(url=obj.description.url)

        def get_seo_title(self, obj):
            return obj.seo_title or obj.title

        def get_categories(self, obj):
            return ProductCategorySummarySerializer(get_product_categories(obj), many=True).data

        class Meta:
            model = Product
            fields = ("id", "product_type", "title", "slug", "status", "seo_title", "meta_description", "categories", "main_image",
                      "price", "off_price", "quantity", "discount_end_time",
                      "description", "included_products", "order_limit",
                      "device_model", "created_at", "updated_at")

    @extend_schema(request=ProductCreateInputSerializer,
                   responses={status.HTTP_201_CREATED: ProuductCreateOutputSerializer})
    def post(self, request):
        serializer = self.ProductCreateInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product = create_product(
                product_type=serializer.validated_data.get("product_type"),
                title=serializer.validated_data.get("title"),
                slug=serializer.validated_data.get("slug", ""),
                status=serializer.validated_data.get("status", ProductStatus.PUBLISHED),
                seo_title=serializer.validated_data.get("seo_title", ""),
                meta_description=serializer.validated_data.get("meta_description", ""),
                main_image=request.FILES.get("main_image"),
                price=serializer.validated_data.get("price"),
                off_price=serializer.validated_data.get("off_price"),
                quantity=serializer.validated_data.get("quantity"),
                discount_end_time=serializer.validated_data.get("discount_end_time", None),
                description=serializer.validated_data.get("description"),
                included_products=serializer.validated_data.get("included_products", None),
                order_limit=serializer.validated_data.get("order_limit", None),
                device_model=serializer.validated_data.get("device_model", None),
                categories=serializer.validated_data.get("categories", []),
            )
            return Response(self.ProuductCreateOutputSerializer(product).data, status=status.HTTP_201_CREATED)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProductDetailAdminApi(ApiAuthMixin, APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [AdminOrManagerPermission, ]

    class ProductDetailInPutSerializer(serializers.Serializer):
        product_type = serializers.ChoiceField(choices=ProductType.choices())
        title = serializers.CharField(max_length=100)
        slug = serializers.CharField(max_length=120, required=False, allow_blank=True)
        status = serializers.ChoiceField(choices=ProductStatus.choices, required=False)
        seo_title = serializers.CharField(max_length=120, required=False, allow_blank=True)
        meta_description = serializers.CharField(max_length=300, required=False, allow_blank=True)
        main_image = serializers.FileField(required=False)
        price = serializers.DecimalField(max_digits=15, decimal_places=0)
        off_price = serializers.DecimalField(max_digits=15, decimal_places=0)
        quantity = serializers.IntegerField(required=True)
        discount_end_time = serializers.DateTimeField(required=False)
        description = serializers.FileField(required=False)
        order_limit = serializers.IntegerField(required=False)
        device_model = serializers.CharField(max_length=100, required=False, allow_blank=True)
        categories = serializers.PrimaryKeyRelatedField(
            required=False,
            many=True,
            queryset=Category.objects.filter(category_type__in=SELLABLE_CATEGORY_TYPES),
        )
        allow_uncategorized = serializers.BooleanField(required=False, default=False, write_only=True)

        def validate_slug(self, value):
            return validate_unique_slug(value, product_id=self.context.get("product_id"))

        def validate(self, attrs):
            if "categories" in self.initial_data:
                return validate_product_categories(attrs)
            return attrs

    class ProductDetailOutPutSerializer(serializers.ModelSerializer):
        main_image = serializers.SerializerMethodField()
        description = serializers.SerializerMethodField()
        seo_title = serializers.SerializerMethodField()
        categories = serializers.SerializerMethodField()

        def get_main_image(self, obj):
            return reformat_url(url=obj.main_image.url)

        def get_description(self, obj):
            return reformat_url(url=obj.description.url)

        def get_seo_title(self, obj):
            return obj.seo_title or obj.title

        def get_categories(self, obj):
            return ProductCategorySummarySerializer(get_product_categories(obj), many=True).data

        class Meta:
            model = Product
            fields = (
                "id", "product_type", "title", "slug", "status", "seo_title", "meta_description", "categories",
                "main_image", "price", "off_price", "quantity", "discount_end_time",
                "description", "order_limit", "device_model")

    @extend_schema(request=ProductDetailInPutSerializer, responses={200: ProductDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.ProductDetailInPutSerializer(data=request.data, context={"product_id": id})
        serializer.is_valid(raise_exception=True)
        # try:
        if not check_product_exists(product_id=id):
            return Response({"error": "محصول موجود نیست"}, status=status.HTTP_400_BAD_REQUEST)
        current_product = Product.objects.get(id=id)
        main_image = request.FILES.get("main_image", None)
        description = request.FILES.get("description", None)
        product = update_product(
            product_id=id,
            product_type=serializer.validated_data.get("product_type"),
            title=serializer.validated_data.get("title"),
            slug=serializer.validated_data.get("slug", current_product.slug),
            status=serializer.validated_data.get("status", current_product.status),
            seo_title=serializer.validated_data.get("seo_title", current_product.seo_title),
            meta_description=serializer.validated_data.get("meta_description", current_product.meta_description),
            main_image=main_image,
            price=serializer.validated_data.get("price"),
            off_price=serializer.validated_data.get("off_price"),
            quantity=serializer.validated_data.get("quantity"),
            discount_end_time=serializer.validated_data.get("discount_end_time"),
            description=description,
            order_limit=serializer.validated_data.get("order_limit"),
            device_model=serializer.validated_data.get("device_model"),
            categories=serializer.validated_data.get("categories") if "categories" in serializer.validated_data else None,
        )
        return Response(self.ProductDetailOutPutSerializer(product).data, status=status.HTTP_200_OK)
        # except Exception as e:
        #     return Response({"error": "مشکلی در آپدیت محصول به وجود آمده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id):
        try:
            if not check_product_exists(product_id=id):
                return Response({"error": "محصول موجود نیست"}, status=status.HTTP_400_BAD_REQUEST)
            delete_product(product_id=id)
            return Response({"message": "حذف محصول تستی انجام شد."}, status=status.HTTP_200_OK)
        except ProductDeleteProtectedError as error:
            return Response({"error": str(error), "can_hide": True}, status=status.HTTP_409_CONFLICT)
        except ProductDeleteDependencyError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"error": "مشکلی در حذف محصول به وجود آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProudctOutPutSerializer(serializers.ModelSerializer):
    included_products = ProductDetailProductSerializer(many=True)
    main_image = serializers.SerializerMethodField()
    seo_title = serializers.SerializerMethodField()
    categories = serializers.SerializerMethodField()

    def get_main_image(self, obj):
        return reformat_url(url=obj.main_image.url)

    def get_seo_title(self, obj):
        return obj.seo_title or obj.title

    def get_categories(self, obj):
        return ProductCategorySummarySerializer(get_product_categories(obj), many=True).data

    attachments = inline_serializer(many=True,
                                    fields={
                                        "id": serializers.CharField(required=False),
                                        "title": serializers.CharField(required=False),
                                        "attachment_type": serializers.IntegerField(required=False),
                                        "price": serializers.DecimalField(max_digits=15, decimal_places=0,
                                                                          required=False),
                                        "is_force_attachment": serializers.BooleanField(required=False),
                                        "description": serializers.CharField(
                                            required=False, allow_blank=True, allow_null=True, max_length=250
                                        )
                                    })

    class Meta:
        model = Product
        fields = ("id", "product_type", "title", "slug", "status", "seo_title", "meta_description", "categories", "main_image",
                  "price", "off_price", "discount_end_time",
                  "included_products", "order_limit", "device_model", "attachments" , "score"
                  )


class ProudctApi(APIView):
    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class FilterProductSerializer(serializers.Serializer):
        product_type = serializers.ChoiceField(required=False, choices=ProductType.choices())
        search = serializers.CharField(required=False, max_length=100)
        off_price__range = serializers.CharField(required=False, max_length=100)
        created_at__range = serializers.CharField(required=False, max_length=100)
        has_discount = serializers.CharField(required=False)
        categories__in = serializers.CharField(required=False, max_length=200)
        labels__in = serializers.CharField(required=False, max_length=100)
        is_exists = serializers.CharField(required=False)
        status = serializers.ChoiceField(required=False, choices=ProductStatus.choices)
        visibility = serializers.ChoiceField(
            required=False,
            choices=(("active", "active"), ("hidden", "hidden"), ("all", "all")),
        )
        order_by = serializers.ChoiceField(required=False, choices=ProductOrderBy.choices())

    class PaginatedProductSerializer(PaginatedSerializer):
        results = ProudctOutPutSerializer(many=True)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)

    @extend_schema(parameters=[FilterProductSerializer, PaginationParameterSerializer],
                   responses=PaginatedProductSerializer, )
    def get(self, request):
        filters_serializer = self.FilterProductSerializer(data=request.query_params)
        filters_serializer.is_valid(raise_exception=True)
        try:
            query = product_list(
                filters=filters_serializer.validated_data,
                include_unpublished=can_manage_products(request),
            )
        except Exception as error:
            return Response(
                {"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=ProudctOutPutSerializer,
            queryset=query,
            view=self,
            request=request
        )


class ProductDetailCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "category_type", "parent")


class ProductDetailFeatureSerializer(serializers.ModelSerializer):
    category = ProductDetailCategorySerializer(read_only=True)

    class Meta:
        model = Feature
        fields = ("name", "category")


class ProductDetailLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ("name", "label_type",)


class ProductDetailUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = BaseUser
        fields = ("firstname", "lastname",)


class ProductDetailApi(APIView):
    class ProductDetailOutPutSerializer(serializers.Serializer):

        comments_count = serializers.SerializerMethodField()

        images = inline_serializer(many=True,
                                   fields={
                                       "id": serializers.CharField(required=False),
                                       "file": serializers.FileField(required=False)
                                   })
        included_products = inline_serializer(many=True,
                                              fields={
                                                  "id": serializers.CharField(required=False),
                                                  "product_type": serializers.IntegerField(required=False),
                                                  "title": serializers.CharField(required=False),
                                                  "main_image": serializers.FileField(required=False),
                                              })
        valueslist = inline_serializer(many=True,
                                       fields={
                                           "id": serializers.CharField(required=False),
                                           "feature": ProductDetailFeatureSerializer(required=False),
                                           "value": serializers.CharField(required=False),
                                       })
        attachments = inline_serializer(many=True,
                                        fields={
                                            "id": serializers.CharField(required=False),
                                            "title": serializers.CharField(required=False),
                                            "attachment_type": serializers.IntegerField(required=False),
                                            "price": serializers.DecimalField(max_digits=15, decimal_places=0,
                                                                              required=False),
                                            "is_force_attachment": serializers.BooleanField(required=False),
                                            "description": serializers.CharField(
                                                required=False, allow_blank=True, allow_null=True, max_length=250
                                            )
                                        })
        suggestions = inline_serializer(many=True, fields={
            "id": serializers.CharField(required=False),
            "suggested": ProductDetailProductSerializer(required=False),
        })
        labels = inline_serializer(many=True, fields={
            "id": serializers.CharField(required=False),
            "label": ProductDetailLabelSerializer(required=False)
        })
        categories = serializers.SerializerMethodField()

        reviews = inline_serializer(many=True, fields={
            "id": serializers.CharField(required=False),
            "user": ProductDetailUserSerializer(required=False),
            "comment": serializers.CharField(required=False),
            "rating": serializers.IntegerField(required=False),
            "status": serializers.CharField(required=False),
            "created_at": serializers.DateTimeField(required=False),
        })

        questions = inline_serializer(many=True, fields={
            "id": serializers.CharField(required=False),
            "sender": ProductDetailUserSerializer(required=False),
            "question": serializers.CharField(required=False),
            "answer": serializers.CharField(required=False)
        })

        notes = inline_serializer(many=True, fields={
            "id": serializers.CharField(required=False),
            "title": serializers.CharField(max_length=100),
        })
        product_type = serializers.IntegerField()
        title = serializers.CharField()
        slug = serializers.SlugField()
        status = serializers.CharField()
        seo_title = serializers.SerializerMethodField()
        meta_description = serializers.CharField(required=False)
        main_image = serializers.SerializerMethodField()
        price = serializers.DecimalField(decimal_places=0, max_digits=15)
        off_price = serializers.DecimalField(decimal_places=0, max_digits=15)
        quantity = serializers.IntegerField()
        order_limit = serializers.IntegerField(required=False)
        device_model = serializers.CharField()
        id = serializers.IntegerField()
        description = serializers.FileField()
        discount_end_time = serializers.DateTimeField()
        score = serializers.DecimalField(decimal_places=1, max_digits=4)
        created_at = serializers.DateTimeField(required=False)
        updated_at = serializers.DateTimeField(required=False)
        def to_representation(self, instance):
            representation = super().to_representation(instance)
            images_data = representation["images"]
            included_products_data = representation["included_products"]
            print(f"{images_data=}")
            for image_data in images_data:
                image_data["file"] = reformat_url(url =image_data["file"])

            for included_product in included_products_data:
                included_product["main_image"] = reformat_url(url = included_product["main_image"])
            representation["included_products"] = included_products_data
            representation["images"] = images_data
            return representation



        def get_comments_count(self, product: Product) -> int:
            return Reviews.objects.filter(
                status=ReviewStatus.APPROVED,
                accepted=True,
                product=product,
            ).count()

        def get_main_image(self , obj):
            return reformat_url(url = obj.main_image.url)

        def get_seo_title(self, obj):
            return obj.seo_title or obj.title

        def get_categories(self, obj):
            return ProductDetailCategorySerializer(get_product_categories(obj), many=True).data

        def get_description(self , obj):
            return reformat_url(url = obj.description.url)
        
        
        

    @extend_schema(responses=ProductDetailOutPutSerializer)
    def get(self, request, slug: str):
        try:
            product = product_detail(
                slug=slug,
                include_unpublished=can_manage_products(request),
            )
            if product is None:
                return Response({"error": "محصول موجود نیست"}, status=status.HTTP_404_NOT_FOUND)
            serializer = self.ProductDetailOutPutSerializer(instance=product)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProductNoteAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class ProductNoteInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=100)
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())

    class ProductNoteOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ProductNote
            fields = ("id", "title", "product")

    @extend_schema(request=ProductNoteInPutSerializer, responses=ProductNoteOutPutSerializer)
    def post(self, request):
        serializer = self.ProductNoteInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product_note = create_product_note(
                title=serializer.validated_data.get("title"),
                product=serializer.validated_data.get("product")
            )
            return Response(self.ProductNoteOutPutSerializer(product_note).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ProductNoteDetailApi(ApiAuthMixin, APIView):
    class ProductNoteDetailInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=100)
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())

    class ProductNoteDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ProductNote
            fields = ("id", "title", "product")

    @extend_schema(request=ProductNoteDetailInPutSerializer, responses=ProductNoteDetailOutPutSerializer)
    def put(self, request, id: int):
        serializer = self.ProductNoteDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            product_note = update_product_note(
                product_note_id=id,
                title=serializer.validated_data.get("title"),
                product=serializer.validated_data.get("product")
            )
            return Response(self.ProductNoteDetailOutPutSerializer(product_note).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: int):
        try:
            delete_product_note(product_note_id=id)
            return Response({"message": "آیتم مورد نظر حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
