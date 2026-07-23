from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import  Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination, PaginatedSerializer, get_paginated_response
from cheatgame.product.models import Reviews, ReviewStatus
from cheatgame.product.permissions import AdminOrManagerPermission
from cheatgame.product.selectors.rating import review_list


class ReviewListOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reviews
        fields = "__all__"

class ReviewListAPIView(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class ReviewFilterSerializer(serializers.Serializer):
        is_accepted = serializers.CharField(required=False)
        status = serializers.ChoiceField(choices=ReviewStatus.choices, required=False)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)


    class PaginatedQuestionSerializer(PaginatedSerializer):
        results = ReviewListOutPutSerializer(many=True)


    @extend_schema(parameters=[ReviewFilterSerializer , PaginationParameterSerializer],
                   responses={PaginatedQuestionSerializer})
    def get(self , request):
        filter_serializer = self.ReviewFilterSerializer(data=request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        # try:
        query = review_list(filters = filter_serializer.validated_data)
        # except Exception as error:
        #     return Response({"error": "مشکلی پیش آمده است."} ,status=status.HTTP_400_BAD_REQUEST)
        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=ReviewListOutPutSerializer,
            queryset=query,
            view=self,
            request = request
        )
