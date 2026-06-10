from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from storages.backends.s3boto3 import S3Boto3Storage

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination, get_paginated_response, PaginatedSerializer
from cheatgame.api.utils import inline_serializer
from cheatgame.common.utils import reformat_url, safe_file_url
from cheatgame.general.models import Story, Slider, BannerLocations, Banner, Blog, BlogCategory, Message, UserMessage, \
    CommonQuestionLocation, CommonQuestion, Comment
from cheatgame.general.selectors import get_stories, get_sliders, get_banners, blog_list, get_blog, \
    get_user_message_list, get_message_list, get_common_question_list, get_comment_list_blog
from cheatgame.general.services import create_story, update_story, delete_story, create_slider, update_slider, \
    delete_slider, create_banner, update_banner, delete_banner, create_blog, update_blog, delete_blog, \
    create_blog_category, update_blog_category, delete_blog_category, create_message, update_message, delete_message, \
    create_user_message, seen_user_message, create_common_question, update_common_question, delete_common_question, \
    create_blog_comment, check_comment_exists, check_commnent_exists_id, update_comment, delete_comment
from cheatgame.product.models import CategoryType, Category
from cheatgame.product.permissions import AdminOrManagerPermission, CustomerPermission, BlogCommentIsOwnerCustomer
from cheatgame.product.selectors.product import products_numbers
from cheatgame.users.models import BaseUser, UserTypes
from cheatgame.users.selectors import customers_numbers


class StoryAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class StoryInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=50)
        picture = serializers.FileField()
        link = serializers.URLField()
        content_picture = serializers.FileField()

    class StoryOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()
        content_picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url = obj.content_picture.url)

        def get_content_picture(self, obj):
            return reformat_url(url = obj.content_picture.url)

        class Meta:
            model = Story
            fields = ("id", "title", "picture", "link", "content_picture",)

    @extend_schema(request=StoryInPutSerializer, responses=StoryOutPutSerializer)
    def post(self, request):
        serializer = self.StoryInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            story = create_story(
                title=serializer.validated_data.get("title"),
                link=serializer.validated_data.get("link"),
                picture=request.FILES.get("picture"),
                content_picture=request.FILES.get("content_picture"),
            )
            return Response(self.StoryOutPutSerializer(story).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "ساخت استوری با مشکل مواجهه شد."}, status=status.HTTP_400_BAD_REQUEST)


class StoryListApi(APIView):
    class StoryListOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()
        content_picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url = obj.picture.url)

        def get_content_picture(self, obj):
            return reformat_url(url = obj.content_picture.url)
        class Meta:
            model = Story
            fields = ("id", "title", "picture", "link", "content_picture",)

    @extend_schema(responses=StoryListOutPutSerializer)
    def get(self, request):
        try:
            stories = get_stories()
            return Response(self.StoryListOutPutSerializer(stories, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"دریافت لیست استوری با مشکل مواجهه شد."}, status=status.HTTP_400_BAD_REQUEST)


class StoryDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class StoryDetailInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=50)
        picture = serializers.FileField()
        link = serializers.URLField()
        content_picture = serializers.FileField()

    class StoryDetailOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()
        content_picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.content_picture.url)

        def get_content_picture(self, obj):
            return reformat_url(url=obj.content_picture.url)
        class Meta:
            model = Story
            fields = ("id", "title", "picture", "link", "content_picture",)

    @extend_schema(request=StoryDetailInPutSerializer, responses={status.HTTP_200_OK: StoryDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.StoryDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            story = update_story(
                story_id=id,
                title=serializer.validated_data.get("title"),
                link=serializer.validated_data.get("link"),
                picture=request.FILES.get("picture", None),
                content_picture=request.FILES.get("content_picture", None),
            )
            return Response(self.StoryDetailOutPutSerializer(story).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش استوری رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id: int):
        try:
            delete_story(story_id=id)
            return Response({"message": "استوری مورد نظر حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "حذف استوری مورد نظر انجام نشد. "}, status=status.HTTP_400_BAD_REQUEST)


class SliderAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class SliderInPutSerializer(serializers.Serializer):
        laptop_picture = serializers.FileField()
        middle_picture = serializers.FileField()
        mobile_picture = serializers.FileField()
        link = serializers.URLField()

    class SliderOutPutSerializer(serializers.ModelSerializer):
        laptop_picture = serializers.SerializerMethodField()
        middle_picture = serializers.SerializerMethodField()
        mobile_picture = serializers.SerializerMethodField()

        def get_laptop_picture(self, obj):
            return reformat_url(url=obj.laptop_picture.url)

        def get_middle_picture(self, obj):
            return reformat_url(url=obj.middle_picture.url)

        def get_mobile_picture(self, obj):
            return reformat_url(url=obj.mobile_picture.url)
        class Meta:
            model = Slider
            fields = ("id", "laptop_picture", "link", "mobile_picture", "middle_picture")

    @extend_schema(request=SliderInPutSerializer, responses=SliderOutPutSerializer)
    def post(self, request):
        serializer = self.SliderInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            slider = create_slider(
                link=serializer.validated_data.get("link"),
                laptop_picture=request.FILES.get("laptop_picture"),
                middle_picture=request.FILES.get("middle_picture"),
                mobile_picture=request.FILES.get("mobile_picture")
            )
            return Response(self.SliderOutPutSerializer(slider).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "ساخت اسلایدر با مشکل مواجه شد."}, status=status.HTTP_400_BAD_REQUEST)


class SliderListApi(APIView):
    class SliderListOutPutSerializer(serializers.ModelSerializer):
        laptop_picture = serializers.SerializerMethodField()
        middle_picture = serializers.SerializerMethodField()
        mobile_picture = serializers.SerializerMethodField()


        def get_laptop_picture(self, obj):
            return reformat_url(url = obj.laptop_picture.url)

        def get_middle_picture(self, obj):
            return reformat_url(url=obj.middle_picture.url)

        def get_mobile_picture(self, obj):
            return reformat_url(url=obj.mobile_picture.url)

        class Meta:
            model = Slider
            fields = ("id", "laptop_picture", "link", "middle_picture", "mobile_picture")

    @extend_schema(responses=SliderListOutPutSerializer)
    def get(self, request):
        try:
            sliders = get_sliders()
            return Response(self.SliderListOutPutSerializer(sliders, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "خطایی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class SliderDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class SliderDetailInPutSerializer(serializers.Serializer):
        laptop_picture = serializers.FileField()
        middle_picture = serializers.FileField()
        mobile_picture = serializers.FileField()
        link = serializers.URLField()

    class SliderDetailOutPutSerializer(serializers.ModelSerializer):
        laptop_picture = serializers.SerializerMethodField()
        middle_picture = serializers.SerializerMethodField()
        mobile_picture = serializers.SerializerMethodField()

        def get_laptop_picture(self, obj):
            return reformat_url(url=obj.laptop_picture.url)

        def get_middle_picture(self, obj):
            return reformat_url(url=obj.middle_picture.url)

        def get_mobile_picture(self, obj):
            return reformat_url(url=obj.mobile_picture.url)
        class Meta:
            model = Slider
            fields = ("id", "laptop_picture", "link", "middle_picture", "mobile_picture")

    @extend_schema(request=SliderDetailInPutSerializer, responses={status.HTTP_200_OK: SliderDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.SliderDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            slider = update_slider(
                slider_id=id,
                link=serializer.validated_data.get("link"),
                laptop_picture=serializer.validated_data.get("laptop_picture", None),
                middle_picture=serializer.validated_data.get("middle_picture", None),
                mobile_picture=serializer.validated_data.get("mobile_picture", None)
            )
            return Response(self.SliderDetailOutPutSerializer(slider).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش اسایدر رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id):
        try:
            delete_slider(slider_id=id)
            return Response({"message": "اسلایدر حذف شد."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class BannerAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class BannerInPutSerializer(serializers.Serializer):
        picture = serializers.FileField()
        link = serializers.URLField()
        location = serializers.ChoiceField(choices=BannerLocations.choices())

    class BannerOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url = obj.picture.url)
        class Meta:
            model = Banner
            fields = ("id", "picture", "link", "location")

    @extend_schema(request=BannerInPutSerializer, responses=BannerOutPutSerializer)
    def post(self, request):
        serializer = self.BannerInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            banner = create_banner(
                link=serializer.validated_data.get("link"),
                picture=request.FILES.get("picture"),
                location=request.validated_data.get("location")
            )
            return Response(self.BannerOutPutSerializer(banner).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "ساخت بنر با مشکل مواجه شد."}, status=status.HTTP_400_BAD_REQUEST)


class BannerListApi(APIView):
    class BannerListOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return safe_file_url(file=obj.picture)
        class Meta:
            model = Banner
            fields = ("id", "picture", "link", "location")

    @extend_schema(responses=BannerListOutPutSerializer)
    def get(self, request):
        try:
            banners = get_banners()
            return Response(self.BannerListOutPutSerializer(banners, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "خطایی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class BannerApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class BannerChangeInPutSerializer(serializers.Serializer):
        picture = serializers.FileField()
        link = serializers.URLField()
        location = serializers.ChoiceField(choices=BannerLocations.choices())

    class BannerChangeOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.picture.url)
        class Meta:
            model = Banner
            fields = ("id", "picture", "link", "location")

    @extend_schema(request=BannerChangeInPutSerializer, responses={status.HTTP_200_OK: BannerChangeOutPutSerializer})
    def put(self, request, id):
        serializer = self.BannerChangeInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            banner = update_banner(
                banner_id=id,
                link=serializer.validated_data.get("link"),
                picture=request.FILES.get("picture", None),
                location=serializer.validated_data.get("location")
            )
            return Response(self.BannerChangeOutPutSerializer(banner).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش بنر رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id):
        try:
            delete_banner(banner_id=id)
            return Response({"message": "بنر حذف شد."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class BlogAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class BlogInPutSerializer(serializers.Serializer):
        picture = serializers.FileField()
        content = serializers.FileField()
        title = serializers.CharField(max_length=200)

    class BlogOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()
        content = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.picture.url)

        def get_content(self, obj):
            return reformat_url(url=obj.content.url)

        class Meta:
            model = Blog
            fields = ("id", "picture", "content", "title", "slug")

    @extend_schema(request=BlogInPutSerializer, responses=BlogOutPutSerializer)
    def post(self, request):
        serializer = self.BlogInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            blog = create_blog(
                picture=request.FILES.get("picture"),
                content=request.FILES.get("content"),
                title=serializer.validated_data.get("title")
            )
            return Response(self.BlogOutPutSerializer(blog).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            print(error)
            return Response({"error": "ساخت بلاگ با مشکل مواجه شد."}, status=status.HTTP_400_BAD_REQUEST)


class BlogCommentCreateApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class BlogCommentInPutSerializer(serializers.Serializer):
        content = serializers.CharField(max_length=1000)
        blog = serializers.PrimaryKeyRelatedField(queryset=Blog.objects.all())

    class BlogCommentOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Comment
            fields = ("id", "content", "blog", "user")

    @extend_schema(request=BlogCommentInPutSerializer, responses=BlogCommentOutPutSerializer)
    def post(self, request):
        serializer = self.BlogCommentInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        if check_comment_exists(blog=serializer.validated_data.get("blog"), user=request.user):
            return Response({"error": "شما قبلا برای این پست کامنت گذاشته اید."},
                            status=status.HTTP_400_BAD_REQUEST)
        comment = create_blog_comment(
            blog=serializer.validated_data.get("blog"),
            content=serializer.validated_data.get("content"),
            user=request.user
        )
        return Response(self.BlogCommentOutPutSerializer(comment).data, status=status.HTTP_201_CREATED)
        # except Exception as error:
        #     return Response({"error": "مشکلی در ساخت کامنت پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class BlogCommentDetailApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,BlogCommentIsOwnerCustomer)

    class BlogCommentDetailInPutSerializer(serializers.Serializer):
        content = serializers.CharField(max_length=1000)

    class BlogCommentDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Comment
            fields = ("id", "content", "blog", "created_at")
            
    @extend_schema(request=BlogCommentDetailInPutSerializer, responses=BlogCommentDetailOutPutSerializer)
    def put(self , request , id):
        serializer = self.BlogCommentDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception = True)
        try:
            if not check_commnent_exists_id(comment_id=id):
                return Response({"error":"کامنت وجود ندارد"} , status= status.HTTP_400_BAD_REQUEST)
            comment = update_comment(comment_id=id , content=serializer.validated_data.get("content"))
            return Response(self.BlogCommentDetailOutPutSerializer(comment).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error":"مشکلی در آپدیت کامنت پیش آمده است."} , status=status.HTTP_400_BAD_REQUEST )
        
    @extend_schema(responses=BlogCommentDetailOutPutSerializer)
    def delete(self, request , id):
        try:
            if not check_commnent_exists_id(comment_id=id):
                return Response({"error":"کامنت وجود ندارد"} , status= status.HTTP_400_BAD_REQUEST)
            delete_comment(comment_id=id)
            return Response({"message":  "کامنت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی در حذف کامنت پیش آمده است "} , status=status.HTTP_400_BAD_REQUEST)


# class SliderListApi(APIView):
#     class SliderListOutPutSerializer(serializers.ModelSerializer):
#         class Meta:
#             model = Slider
#             fields = ("id", "picture", "link")
#
#     @extend_schema(responses=SliderListOutPutSerializer)
#     def get(self, request):
#         try:
#             sliders = get_sliders()
#             return Response(self.SliderListOutPutSerializer(sliders), status=status.HTTP_200_OK)
#         except Exception as error:
#             return Response({"error": "خطایی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class BlogDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class BlogDetailInPutSerializer(serializers.Serializer):
        picture = serializers.FileField()
        content = serializers.FileField()
        title = serializers.CharField(max_length=200)

    class BlogDetailOutPutSerializer(serializers.ModelSerializer):
        picture = serializers.SerializerMethodField()
        content = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.picture.url)

        def get_content(self, obj):
            return reformat_url(url=obj.content.url)

        class Meta:
            model = Blog
            fields = ("id", "picture", "content", "title")

    @extend_schema(request=BlogDetailInPutSerializer, responses={status.HTTP_200_OK: BlogDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.BlogDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            blog = update_blog(
                blog_id=id,
                title=serializer.validated_data.get("title"),
                picture=serializer.validated_data.get("picture", None),
                content=serializer.validated_data.get("content", None),
            )
            return Response(self.BlogDetailOutPutSerializer(blog).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش بلاگ رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id):
        try:
            delete_blog(blog_id=id)
            return Response({"message": "بلاگ حذف شد."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class BlogListOutPutSerializer(serializers.ModelSerializer):
    comments_number = serializers.SerializerMethodField()
    picture = serializers.SerializerMethodField()

    def get_picture(self, obj):
        return reformat_url(url=obj.picture.url)


    def get_comments_number(self, blog: Blog) -> int:
        return Comment.objects.filter(blog=blog).count()

    class Meta:
        model = Blog
        fields = ("id", "slug", "title", "picture", "comments_number", "created_at")


class BlogListApi(APIView):
    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class FilterBlogSerializer(serializers.Serializer):
        categories__in = serializers.CharField(required=False, max_length=200)
        search = serializers.CharField(required=False, max_length=100)
        created_at__range = serializers.CharField(required=False, max_length=100)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)

    class PaginatedBlogListSerializer(PaginatedSerializer):
        results = BlogListOutPutSerializer(many=True)

    @extend_schema(parameters=[FilterBlogSerializer, PaginationParameterSerializer],
                   responses=PaginatedBlogListSerializer)
    def get(self, request):
        filters_serializer = self.FilterBlogSerializer(data=request.query_params)
        filters_serializer.is_valid(raise_exception=True)
        try:
            query_set = blog_list(filters=filters_serializer.validated_data)
        except Exception as error:
            return Response(
                {"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=BlogListOutPutSerializer,
            queryset=query_set,
            view=self,
            request=request
        )


class BlogCategoryOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogCategory
        fields = ("id", "blog", "category",)


class BlogDetailUserApi(APIView):
    class BlogDetailUserOutPutSerializer(serializers.ModelSerializer):
        category_list = serializers.SerializerMethodField()
        comments = serializers.SerializerMethodField()
        picture = serializers.SerializerMethodField()
        content = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.picture.url)

        def get_content(self, obj):
            return reformat_url(url=obj.content.url)

        class BlogCommentOutPutSerializer(serializers.Serializer):
            user = inline_serializer(fields={
                "firstname": serializers.CharField(),
                "lastname": serializers.CharField(),
            })
            content = serializers.CharField()
            id = serializers.IntegerField()

        def get_comments(self, blog: Blog):
            comment_list = get_comment_list_blog(blog=blog)
            return self.BlogCommentOutPutSerializer(comment_list, many=True).data

        def get_category_list(self, blog: Blog):
            categories = blog.categories.all()
            serializer = BlogCategoryOutPutSerializer(categories, many=True)
            return serializer.data

        class Meta:
            model = Blog
            fields = ("id", "title", "category_list", "slug", "content", "picture", "created_at", "comments")

    @extend_schema(responses=BlogDetailUserOutPutSerializer)
    def get(self, request, slug: str):
        try:
            blog = get_blog(slug=slug)
            return Response(self.BlogDetailUserOutPutSerializer(blog).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response(
                {"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class BlogCategoryAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class BlogCategoryInPutSerializer(serializers.Serializer):
        blog = serializers.PrimaryKeyRelatedField(required=True, queryset=Blog.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True,
                                                      queryset=Category.objects.filter(category_type=CategoryType.BLOG))

    class BlogCategoryOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = BlogCategory
            fields = ("blog", "category",)

    @extend_schema(request=BlogCategoryInPutSerializer, responses=BlogCategoryOutPutSerializer)
    def post(self, request):
        serializer = self.BlogCategoryInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:

            create_blog_category(blog=serializer.validated_data.get("blog"),
                                 category=serializer.validated_data.get("category"))
            return Response({"message": "دسته بندی بلاگ باموفقیت ساخته شد."},
                            status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class BlogCategoryDetailApi(ApiAuthMixin, APIView):
    class BlogCategoryDetailInPutSerializer(serializers.Serializer):
        blog = serializers.PrimaryKeyRelatedField(required=True, queryset=Blog.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type=CategoryType.BLOG))

    class BlogCategoryDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = BlogCategory
            fields = ("id", "blog", "category",)

    @extend_schema(request=BlogCategoryDetailInPutSerializer,
                   responses={status.HTTP_200_OK: BlogCategoryDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.BlogCategoryDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            blog_category = update_blog_category(
                blog_category_id=id,
                blog=serializer.validated_data.get("product"),
                category=serializer.validated_data.get("category")
            )
            return Response(self.BlogCategoryDetailOutPutSerializer(blog_category).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, reqeust, id: int):
        try:
            delete_blog_category(
                blog_category_id=id
            )
            return Response({"message": "دسته بندی بلاگ با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class UploadFileS3ApiView(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)
    parser_classes = (MultiPartParser, FormParser)

    class UploadFileInPutSerializer(serializers.Serializer):
        file = serializers.FileField()

    @extend_schema(request=UploadFileInPutSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.UploadFileInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            file = self.request.FILES.get("file")
            storage = S3Boto3Storage()
            file_path = storage.save(file.name, file)
            file_url = storage.url(file_path)
            file_url = reformat_url(url = file_url)
            return Response({"url": file_url}, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "فایل آپلود نشد."}, status=status.HTTP_400_BAD_REQUEST)


class HomePageReportApi(APIView):
    class HomePageReportOutPutSerializer(serializers.Serializer):
        products = serializers.IntegerField()
        users = serializers.IntegerField()

    @extend_schema(responses=HomePageReportOutPutSerializer)
    def get(self, request):
        try:
            product = products_numbers()
            user = customers_numbers()
            return Response({"products": product, "users": user}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class CreateMessageAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class MessageInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=200)
        passage = serializers.CharField(max_length=500)

    class MessageOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Message
            fields = ("id", "title", "passage")

    @extend_schema(request=MessageInPutSerializer, responses=MessageOutPutSerializer)
    def post(self, request):
        serializer = self.MessageInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = create_message(
                title=serializer.validated_data.get("title"),
                passage=serializer.validated_data.get("passage")
            )
            return Response(self.MessageOutPutSerializer(message).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی در ساخت پیام پیش آمد."}, status=status.HTTP_400_BAD_REQUEST)


class MessageDetailAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class MessageDetailInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=200)
        passage = serializers.CharField(max_length=500)

    class MessageDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Message
            fields = ("id", "title", "passage")

    @extend_schema(request=MessageDetailInPutSerializer, responses={status.HTTP_200_OK: MessageDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.MessageDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = update_message(
                message_id=id,
                title=serializer.validated_data.get("title"),
                passage=serializer.validated_data.get("passage")
            )
            return Response(self.MessageDetailOutPutSerializer(message).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش پیام به وجود آمده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id):
        try:
            delete_message(message_id=id)
            return Response({"message": "پیام با موفقیت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در حذف پیام به وجود آمد"}, status=status.HTTP_400_BAD_REQUEST)


class CreateUserMessageList(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class UserMessageInPutSerializer(serializers.Serializer):
        message = serializers.PrimaryKeyRelatedField(required=True, queryset=Message.objects.all())
        user = serializers.PrimaryKeyRelatedField(required=True,
                                                  queryset=BaseUser.objects.filter(user_type=UserTypes.CUSTOMER))

    @extend_schema(request=UserMessageInPutSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.UserMessageInPutSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        try:
            for user_message in serializer.validated_data:
                bulk_list = []
                bulk_list.append(
                    UserMessage(user=user_message.get("user"),
                                message=user_message.get("message"))
                )
                create_user_message(user_messages=bulk_list)
            return Response({"message": "پیام ها برای کاربر ها با موفقیت ارسال شد."},
                            status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class MessageListUserOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ("id", "title", "passage")


class MessageListApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class MessageListOutPutSerializer(serializers.ModelSerializer):

        class Meta:
            model = Message
            fields = ("id", "title", "passage")

    @extend_schema(responses=MessageListOutPutSerializer)
    def get(self, request):
        try:
            messages = get_message_list()
            return Response(self.MessageListOutPutSerializer(messages, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در نمایش لیست اعلانات وجود دارد."}, status=status.HTTP_400_BAD_REQUEST)


class UserMessageListApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class UserMessageOutPutSerializer(serializers.ModelSerializer):
        message = MessageListUserOutPutSerializer()

        class Meta:
            model = UserMessage
            fields = ("id", "message", "is_seen")

    @extend_schema(responses=UserMessageOutPutSerializer)
    def get(self, request):
        try:
            messages = get_user_message_list(user=request.user)
            return Response(self.UserMessageOutPutSerializer(messages, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در نمایش لیست اعلانات وجود دارد."}, status=status.HTTP_400_BAD_REQUEST)


class UserMessageSeenApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    @extend_schema(request=None, responses={status.HTTP_200_OK: dict})
    def put(self, request, id):
        try:
            seen_user_message(user_message_id=id)
            return Response({"message": "پیام کاربر دیده شد."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی پیش آمد "}, status=status.HTTP_400_BAD_REQUEST)


class CommonQuestionAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class CommonQuestionInPutSerializer(serializers.Serializer):
        question_location = serializers.ChoiceField(choices=CommonQuestionLocation.choices())
        question = serializers.CharField(max_length=300)
        answer = serializers.CharField(max_length=500)

    class CommonQuestionOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = CommonQuestion
            fields = ("id", "question_location", "question", "answer")

    @extend_schema(request=CommonQuestionInPutSerializer, responses=CommonQuestionOutPutSerializer)
    def post(self, request):
        serializer = self.CommonQuestionInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            question = create_common_question(
                question_location=serializer.validated_data.get("question_location"),
                question=serializer.validated_data.get("question"),
                answer=serializer.validated_data.get("answer"),
            )
            return Response(self.CommonQuestionOutPutSerializer(question).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ساخت سوال پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class CommonQuestionDetialAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class CommonQuestionDetailInPutSerializer(serializers.Serializer):
        question_location = serializers.ChoiceField(choices=CommonQuestionLocation.choices())
        question = serializers.CharField(max_length=300)
        answer = serializers.CharField(max_length=500)

    class CommonQuestionDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = CommonQuestion
            fields = ("id", "question_location", "question", "answer")

    @extend_schema(request=CommonQuestionDetailInPutSerializer,
                   responses={status.HTTP_200_OK: CommonQuestionDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.CommonQuestionDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            common_question = update_common_question(
                id=id,
                question_location=serializer.validated_data.get("question_location"),
                question=serializer.validated_data.get("question"),
                answer=serializer.validated_data.get("answer")
            )
            return Response(self.CommonQuestionDetailOutPutSerializer(common_question).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش  سوال به وجود آمد"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id: int):
        try:
            delete_common_question(id=id)
            return Response({"message": "سوال مورد نظر حذف شد"}, status=status.HTTP_200_OK)
        except Exception as errro:
            return Response({"error": "مشکلی در حذف سوال پیش آمد"})


class CommonQuestionListOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommonQuestion
        fields = ("id", "question_location", "question", "answer")


class CommonQuestionListApi(APIView):
    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class PaginatedCommonQuestionListOutPutSerializer(PaginatedSerializer):
        result = CommonQuestionListOutPutSerializer(many=True)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)

    @extend_schema(responses=PaginatedCommonQuestionListOutPutSerializer, parameters=[PaginationParameterSerializer])
    def get(self, request):
        try:

            common_questions = get_common_question_list()
        except Exception as error:
            return Response({"error": "مشکلی در دریافت لیست پیش آمده است"}, status=status.HTTP_200_OK)

        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=CommonQuestionListOutPutSerializer,
            queryset=common_questions,
            view=self,
            request=request
        )
