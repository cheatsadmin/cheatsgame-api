from django.contrib.auth import authenticate
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from cheatgame.users.models import BaseUser, UserTypes
from cheatgame.users.validators import check_phone_number


class InPutLoginSerializer(serializers.Serializer):
    phone_number = serializers.CharField(required=True)
    password = serializers.CharField(required=True)


class OutPutLoginSerializer(serializers.ModelSerializer):
    token = serializers.SerializerMethodField("get_token")

    class Meta:
        model = BaseUser
        fields = ('token',)

    def get_token(self, user) -> dict:
        data = dict()
        token_class = RefreshToken

        refresh = token_class.for_user(user)

        data["refresh"] = str(refresh)
        data["access"] = str(refresh.access_token)

        return data


def authenticate_user(request, user_type):
    serializer = InPutLoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    phone_number = serializer.validated_data.get('phone_number')
    password = serializer.validated_data.get('password')
    if not check_phone_number(serializer.validated_data.get("phone_number")):
        return Response({"error": "شماره فقط با حروف انگلیسی قابل قبول است."}, status=status.HTTP_400_BAD_REQUEST)
    user = authenticate(request=request, phone_number=phone_number, password=password)
    if not user:
        return Response({'error': 'رمز یا نام کاربری صحیح نمی باشد.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not user.user_type == user_type:
        return Response({'error': 'رمز یا نام کاربری صحیح نمی باشد.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not user.phone_verified:
        return Response({'error': 'ابتدا شماره خود را تایید کنید.'}, status=status.HTTP_400_BAD_REQUEST)


    return user


class CustomerLoginApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "login"

    @extend_schema(request=InPutLoginSerializer, responses=OutPutLoginSerializer)
    def post(self, request):
        try:
            user = authenticate_user(self.request, user_type=UserTypes.CUSTOMER)
            if isinstance(user, Response):
                return user
        except Exception as ex:
            return Response(
                {"error": "اطلاعات ورود صحیح نمی باشد."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(OutPutLoginSerializer(user, context={'request': request}).data, status=status.HTTP_200_OK)


class ManagerLoginApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "login"

    @extend_schema(request=InPutLoginSerializer, responses=OutPutLoginSerializer)
    def post(self, request):
        try:
            user = authenticate_user(self.request, user_type=UserTypes.MANAGER)
            if isinstance(user, Response):
                return user
        except Exception as ex:
            return Response(
                {"error": "اطلاعات ورود صحیح نمی باشد."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(OutPutLoginSerializer(user, context={'request': request}).data, status=status.HTTP_200_OK)


class AdminLoginApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "login"

    @extend_schema(request=InPutLoginSerializer, responses=OutPutLoginSerializer)
    def post(self, request):
        try:
            user = authenticate_user(self.request, user_type=UserTypes.ADMIN)
            if isinstance(user, Response):
                return user
        except Exception as ex:
            return Response(
                {"error": "اطلاعت ورود صحیح نمی باشد."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(OutPutLoginSerializer(user, context={'request': request}).data, status=status.HTTP_200_OK)
