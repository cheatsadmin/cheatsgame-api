from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.product.models import AttachmentType, Attachment, Product
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.product.selectors.attachment import get_attachment, attachment_list_product
from cheatgame.product.services.attachment import create_attchement, update_attachment, delete_attachment


class AttachmentAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class AttachmentInPutSerializer(serializers.Serializer):
        attachment_type = serializers.ChoiceField(
            choices=AttachmentType.choices()
        )
        title = serializers.CharField(max_length=200)
        price = serializers.DecimalField(max_digits=15,
                                         decimal_places=0)
        is_force_attachment = serializers.BooleanField()
        product = serializers.PrimaryKeyRelatedField(required=True, queryset=Product.objects.all())
        description = serializers.CharField(max_length=250, required=False, allow_blank=True, allow_null=True)

    class AttachmentOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Attachment
            fields = ("id", "attachment_type", "title", "price", "is_force_attachment", "product" , "description")

    @extend_schema(request=AttachmentInPutSerializer, responses=AttachmentOutPutSerializer)
    def post(self, request):
        serializer = self.AttachmentInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            attachment = create_attchement(
                attachment_type=serializer.validated_data.get("attachment_type"),
                title=serializer.validated_data.get("title"),
                price=serializer.validated_data.get("price"),
                is_force_attachment=serializer.validated_data.get("is_force_attachment"),
                product=serializer.validated_data.get("product"),
                description=serializer.validated_data.get("description")
            )
            return Response(self.AttachmentOutPutSerializer(attachment).data, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class AttachmentDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class AttachementDetailInPutSerializer(serializers.Serializer):
        attachment_type = serializers.ChoiceField(
            choices=AttachmentType.choices()
        )
        title = serializers.CharField(max_length=200)
        price = serializers.DecimalField(max_digits=15,
                                         decimal_places=0)
        is_force_attachment = serializers.BooleanField()
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
        description = serializers.CharField(max_length=250, required=False, allow_blank=True, allow_null=True)

    class AttachmentDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Attachment
            fields = ("id", "attachment_type", "title", "price", "is_force_attachment", "product" , "description")

    @extend_schema(request=AttachementDetailInPutSerializer, responses={status.HTTP_200_OK:AttachmentDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.AttachementDetailInPutSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # try:
        attachment = update_attachment(
            attachment_id=id,
            attachment_type=serializer.validated_data.get("attachment_type"),
            title=serializer.validated_data.get("title"),
            price=serializer.validated_data.get("price"),
            is_force_attachment=serializer.validated_data.get("is_force_attachment"),
            product=serializer.validated_data.get("product"),
            description=serializer.validated_data.get("description")
        )
        return Response(self.AttachmentDetailOutPutSerializer(attachment).data, status=status.HTTP_200_OK)
        # except Exception as error:
        #     return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id):
        try:
            delete_attachment(attachment_id=id)
            return Response({"message": "آیتم با موفقیت حذف گردید"}, status=status.HTTP_204_NO_CONTENT)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses=AttachmentDetailOutPutSerializer)
    def get(self, reqeust, id):
        try:
            attachemnt = get_attachment(attachement_id=id)
            return Response(self.AttachmentDetailOutPutSerializer(attachemnt).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class AttachmentListProductApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class AttachmentListOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Attachment
            fields = ("id", "attachment_type", "title", "price", "is_force_attachment", "product", "description")

    @extend_schema(responses=AttachmentListOutPutSerializer)
    def get(self, request, product_id):
        try:
            attachments = attachment_list_product(product_id=product_id)
            return Response(self.AttachmentListOutPutSerializer(attachments, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
