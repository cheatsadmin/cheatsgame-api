from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.product.models import RatingChoices, Reviews, Product, ReviewStatus
from cheatgame.product.permissions import CustomerPermission, AdminOrManagerPermission
from cheatgame.product.services.reviews import create_or_update_review, moderate_review


class ReviewsCreateAPIView(ApiAuthMixin , APIView):
    permission_classes = [CustomerPermission , ]
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "review_submit"


    class ReviewsInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
        comment = serializers.CharField(required=False, allow_blank=True)
        rating = serializers.ChoiceField(RatingChoices.choices())

    class ReviewsOutPutSerializer(serializers.ModelSerializer):

        class Meta:
            model = Reviews
            fields = ("id", "user", "product", "comment", "rating", "status", "accepted")

    @extend_schema(request=ReviewsInPutSerializer , responses= ReviewsOutPutSerializer)
    def post(self , request):
        serializer = self.ReviewsInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rating = int(serializer.validated_data.get("rating"))
        comment = serializer.validated_data.get("comment" , "")
        product = serializer.validated_data.get("product")
        user = request.user
        review = create_or_update_review(user=user, product=product, rating=rating, comment=comment)
        return Response(self.ReviewsOutPutSerializer(review).data , status=status.HTTP_200_OK)


class ReviewDetailAdminAPIView(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class ReviewModerationInPutSerializer(serializers.Serializer):
        status = serializers.ChoiceField(choices=ReviewStatus.choices, required=False)
        accepted = serializers.BooleanField(required=False)

        def validate(self, attrs):
            if "status" in attrs:
                return attrs
            if "accepted" in attrs:
                attrs["status"] = ReviewStatus.APPROVED if attrs["accepted"] else ReviewStatus.REJECTED
                return attrs
            raise serializers.ValidationError({"status": "وضعیت نظر را مشخص کنید."})

    class ReviewModerationOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Reviews
            fields = ("id", "user", "product", "comment", "rating", "status", "accepted", "created_at", "updated_at")

    @extend_schema(request=ReviewModerationInPutSerializer, responses=ReviewModerationOutPutSerializer)
    def put(self, request, id: int):
        serializer = self.ReviewModerationInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            review = Reviews.objects.select_related("product").get(id=id)
        except Reviews.DoesNotExist:
            return Response({"error": "نظر مورد نظر یافت نشد."}, status=status.HTTP_404_NOT_FOUND)

        review = moderate_review(review=review, status=serializer.validated_data["status"])
        return Response(self.ReviewModerationOutPutSerializer(review).data, status=status.HTTP_200_OK)
