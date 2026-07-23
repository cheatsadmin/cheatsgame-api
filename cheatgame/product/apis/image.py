from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers, status
from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.common.utils import reformat_url
from cheatgame.product.models import Image, Product
from cheatgame.product.services.image import create_image, update_image, delete_image


class ImageAdminApi(ApiAuthMixin, APIView):
    parser_classes = (MultiPartParser, FormParser)

    class ImageInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        image = serializers.FileField(required=True)

    class ImageOutPutSerializer(serializers.ModelSerializer):
        file = serializers.SerializerMethodField()

        def get_file(self , obj):
            return reformat_url(url = obj.file.url)
        class Meta:
            model = Image
            fields = ("id", "product", "file")

    @extend_schema(request=ImageInPutSerializer, responses={status.HTTP_200_OK:ImageOutPutSerializer})
    def post(self, request):
        serializer = self.ImageInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            print(serializer.validated_data.get("product"))
            product = create_image(proudct=serializer.validated_data.get("product"),
                                   image=request.FILES.get("image"))
            return Response(self.ImageOutPutSerializer(product).data, status=status.HTTP_201_CREATED)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class ImageDetailAdminApi(ApiAuthMixin, APIView):
    parser_classes = (MultiPartParser, FormParser)

    class ImageDetailInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        image = serializers.FileField(read_only=True)

    class ImageDetailOutPutSerializer(serializers.ModelSerializer):
        file = serializers.SerializerMethodField()

        def get_file(self , obj):
            return reformat_url(url = obj.file.url)
        class Meta:
            model = Image
            fields = ("id", "product", "file")

    @extend_schema(request=ImageDetailInPutSerializer, responses={status.HTTP_200_OK:ImageDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.ImageDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            image = update_image(
                image_id=id,
                image=request.FILES.get("image" , None),
                product=serializer.validated_data.get("product")
            )
            return Response(self.ImageDetailOutPutSerializer(image).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
    @extend_schema(responses={status.HTTP_200_OK:dict})
    def delete(self, request, id: int):
        try:
            delete_image(
                image_id=id
            )
            return Response({"message": "عکس مورد نظر حذف گردید"})
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
