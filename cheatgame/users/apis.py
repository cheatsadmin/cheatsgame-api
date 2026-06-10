from django.conf import settings
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers
from rest_framework.throttling import ScopedRateThrottle
from .services import generate_otp, confirm_email, confirm_phone, update_user, change_password, create_address, \
    update_address, delete_address, create_favorite_product, delete_favorite_product, create_contact_form, \
    update_contact_form
from .selectors import get_user, verify_email_otp, verify_phone_otp, verify_password_otp, user_address_list, \
    number_of_user_address, number_of_favorite_product, user_favorite_product_list, favoirte_product_exists, \
    get_contact_form_list, user_list, user_number_register, check_user_exists
from .models import VerifyType, Address, FavoriteProduct
from django.core.validators import MinLengthValidator
from .validators import number_validator, special_char_validator, letter_validator, phone_number_validator, \
    check_phone_number
from cheatgame.users.models import BaseUser, UserTypes
from cheatgame.users.services import create_user
from rest_framework_simplejwt.tokens import RefreshToken

from drf_spectacular.utils import extend_schema
from cheatgame.utils.notification.sms import send_sms
from ..api.mixins import ApiAuthMixin
from ..api.pagination import PaginatedSerializer, get_paginated_response, LimitOffsetPagination
from ..common.utils import reformat_url
from ..general.models import ContactForm
from ..product.models import Product
from ..product.permissions import CustomerPermission, AddressIsOwnerCustomer, FavoriteProductIsOwnerCustomer, \
    AdminOrManagerPermission


class UserApi(APIView, ApiAuthMixin):
    parser_classes = (MultiPartParser, FormParser)

    class UserOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = BaseUser
            fields = (
                "firstname", "lastname", "phone_number", "profile_image", "email", "email_verified", "birthdate",
                "created_at",
                "updated_at")

    class UserInputSerializer(serializers.Serializer):
        firstname = serializers.CharField(max_length=100, required=True, )
        lastname = serializers.CharField(max_length=100, required=True)
        email = serializers.EmailField(required=False)
        birthdate = serializers.DateField(required=False)
        profile_image = serializers.FileField(required=False)

    @extend_schema(request=UserInputSerializer, responses=UserOutPutSerializer)
    def put(self, request):
        serializer = self.UserInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email_status = bool
        if 'email' in serializer.validated_data and serializer.validated_data['email'] != request.user.email:
            email_status = False
        else:
            email_status = request.user.email_verified

        try:
            profile_image = request.FILES.get('profile_image', None)
            user = update_user(user=request.user, firstname=serializer.validated_data.get('firstname'),
                               lastname=serializer.validated_data.get('lastname'),
                               birthdate=serializer.validated_data.get('birthdate'),
                               email=serializer.validated_data.get('email'),
                               email_status=email_status, profile_image=profile_image)

            return Response(self.UserOutPutSerializer(user).data, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی پیش آمده است. "}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses=UserOutPutSerializer)
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'ابتدا باید احراز هویت انجام دهید'}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(self.UserOutPutSerializer(request.user, context={'request': request}).data)


class RegisterApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "register"

    class InputRegisterSerializer(serializers.Serializer):
        phone_number = serializers.CharField(max_length=13, validators=[
            phone_number_validator
        ])
        firstname = serializers.CharField(max_length=255)
        lastname = serializers.CharField(max_length=255)
        password = serializers.CharField(
            validators=[
                number_validator,
                letter_validator,
                special_char_validator,
                MinLengthValidator(limit_value=8)
            ]
        )
        confirm_password = serializers.CharField(max_length=255)

        def validate_phone_number(self, phone_number):
            if BaseUser.objects.filter(phone_number=phone_number).exists():
                raise serializers.ValidationError("این شماره قبلا وجود دارد")
            return phone_number

        def validate(self, data):
            if not data.get("password") or not data.get("confirm_password"):
                raise serializers.ValidationError("لطفا پسورد و تکرار آن را وارد کنید.")

            if data.get("password") != data.get("confirm_password"):
                raise serializers.ValidationError("پسورد وتکرار آن برابر نیست.")
            return data

    class OutPutRegisterSerializer(serializers.ModelSerializer):

        token = serializers.SerializerMethodField("get_token")

        class Meta:
            model = BaseUser
            fields = ("firstname", "lastname", "phone_number", "token", "created_at", "updated_at")

        def get_token(self, user) -> dict:
            data = dict()
            token_class = RefreshToken

            refresh = token_class.for_user(user)

            data["refresh"] = str(refresh)
            data["access"] = str(refresh.access_token)

            return data

    @extend_schema(request=InputRegisterSerializer, responses=OutPutRegisterSerializer)
    def post(self, request):
        serializer = self.InputRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not check_phone_number(serializer.validated_data.get("phone_number")):
                return Response({"error": "شماره فقط با حروف انگلیسی قابل قبول است."}, status=status.HTTP_400_BAD_REQUEST)
            user = create_user(
                firstname=serializer.validated_data.get('firstname'),
                lastname=serializer.validated_data.get('lastname'),
                phone_number=serializer.validated_data.get('phone_number'),
                password=serializer.validated_data.get('password'),
                user_type=UserTypes.CUSTOMER
            )
        except Exception as ex:
            return Response(
                {"error": "مشکلی رخ داده است"},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(self.OutPutRegisterSerializer(user, context={"request": request}).data)


class VerifyPhoneRequestApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "otp_request"

    class InputPhoneOtpSerializer(serializers.Serializer):
        phone_number = serializers.CharField(max_length=11, required=True , validators=[phone_number_validator])

    @extend_schema(request=InputPhoneOtpSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputPhoneOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not check_user_exists(phone_number=serializer.validated_data.get("phone_number")):
                return Response({"message": "این کاربر وجود ندارد"} , status=status.HTTP_400_BAD_REQUEST)
            user = get_user(phone_number=serializer.validated_data.get('phone_number'))
            otp = generate_otp(user=user, verify_type=VerifyType.PHONENUMBER)
            if settings.IS_SEND_SMS:
                send_sms(to=user.phone_number, pattern=settings.VERIFY_PATTERN, otp=otp)
            return Response({"message": "کد با موفقیت ارسال گردید"}, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class ChangePasswordRequestApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "password_reset_request"

    class InputPasswordOtpSerializer(serializers.Serializer):
        phone_number = serializers.CharField(max_length=11, required=True, validators=[
            phone_number_validator
        ])

    @extend_schema(request=InputPasswordOtpSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputPasswordOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not check_user_exists(phone_number=serializer.validated_data.get("phone_number")):
                return Response({"message": "این کاربر وجود ندارد"}, status=status.HTTP_400_BAD_REQUEST)
            user = get_user(phone_number=serializer.validated_data.get('phone_number'))
            otp = generate_otp(user=user, verify_type=VerifyType.PASSWORD)
            if settings.IS_SEND_SMS:
                send_sms(to=user.phone_number, pattern=settings.FORGET_PASSWORD_PATTERN, otp=otp)
            return Response({"message": "کد با موفقیت ارسال گردید"}, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class VerifyEmailRequestApi(ApiAuthMixin, APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "otp_request"

    class InputEmailOtpSerializer(serializers.Serializer):
        email = serializers.EmailField(required=True)

    @extend_schema(request=InputEmailOtpSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputEmailOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            generate_otp(user=request.user, verify_type=VerifyType.EMAIL)
            # TODO: send otp via email
            return Response({'message': "کد با موفقیت ارسال گردید"}, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class VerfiyPhoneApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "otp_verify"

    class InputVerifyPhoneSerilazer(serializers.Serializer):
        phone_number = serializers.CharField(max_length=11, required=True, validators=[
            phone_number_validator
        ])
        otp = serializers.CharField(required=True)

    @extend_schema(request=InputVerifyPhoneSerilazer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputVerifyPhoneSerilazer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not verify_phone_otp(phone_number=serializer.validated_data.get('phone_number'),
                                    otp=serializer.validated_data.get('otp')):
                return Response({'error': 'اطلاعات مورد شد معتبر نمی باشد.'}, status=status.HTTP_400_BAD_REQUEST)
            confirm_phone(phone_number=serializer.validated_data.get('phone_number'))
            return Response({'Message': "شماره تلفن کاربر احراز گردید."}, status=status.HTTP_200_OK)

        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class ChangePasswordApi(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "password_reset_confirm"

    class InputChangePasswordSerializer(serializers.Serializer):
        otp = serializers.IntegerField(required=True)
        new_password = serializers.CharField(
            validators=[
                number_validator,
                letter_validator,
                special_char_validator,
                MinLengthValidator(limit_value=8)
            ]
        )
        confirm_new_password = serializers.CharField(max_length=255)
        phone_number = serializers.CharField(max_length=11, validators=[
            phone_number_validator
        ])

        def validate(self, data):
            if not data.get("new_password") or not data.get("confirm_new_password"):
                raise serializers.ValidationError("لطفا پسورد و تکرار آن را وارد نمایید.")
            if data.get("new_password") != data.get("confirm_new_password"):
                raise serializers.ValidationError("رمز و تکرار آن مشابه نیست.")
            return data

    @extend_schema(request=InputChangePasswordSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not verify_password_otp(phone_number=serializer.validated_data.get('phone_number'),
                                       otp=serializer.validated_data.get('otp')):
                return Response({"error": "اطلاعات وارد شده معتبر نیست."}, status=status.HTTP_400_BAD_REQUEST)
            user = get_user(phone_number=serializer.validated_data.get('phone_number'))
            change_password(user=user, password=serializer.validated_data.get('new_password'))
            return Response({'message': 'رمز با موفقت تغییر پیدا کرد.'}, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class VerifyEmailApi(APIView, ApiAuthMixin):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "otp_verify"

    class InputVerifyEmailSerializer(serializers.Serializer):
        email = serializers.EmailField(required=True)
        otp = serializers.IntegerField(required=True)

    @extend_schema(request=InputVerifyEmailSerializer, responses={status.HTTP_200_OK: dict})
    def post(self, request):
        serializer = self.InputVerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not verify_email_otp(user=request.user, otp=serializer.validated_data.get('otp')):
                return Response({'error': 'اطلاعات وارد شده صحیح نمی باشد.'}, status=status.HTTP_400_BAD_REQUEST)
            confirm_email(user=request.user)
            return Response({'message': 'ایمیل شما احراز گردید.'}, status=status.HTTP_200_OK)
        except Exception as ex:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class AddressApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class AddressInPutSerializer(serializers.Serializer):
        province = serializers.CharField(max_length=100)
        city = serializers.CharField(max_length=200)
        postal_code = serializers.CharField(max_length=15)
        address_detail = serializers.CharField(max_length=400)

    class AddressOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Address
            fields = (
                "id", "province", "city", "user", "postal_code", "address_detail",
            )

    @extend_schema(request=AddressInPutSerializer, responses=AddressOutPutSerializer)
    def post(self, request):
        serializer = self.AddressInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if number_of_user_address(user=request.user) > 3:
                return Response({"error": "حداکثر تعداد آدرس برای هر کاربر سه می باشد."},
                                status=status.HTTP_400_BAD_REQUEST)
            address = create_address(
                province=serializer.validated_data.get("province"),
                city=serializer.validated_data.get("city"),
                postal_code=serializer.validated_data.get("postal_code"),
                address_detail=serializer.validated_data.get("address_detail"),
                user=request.user
            )
            return Response(self.AddressOutPutSerializer(address).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class AddressDetailApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, AddressIsOwnerCustomer,)

    class AddressDetailInPutSerializer(serializers.Serializer):
        province = serializers.CharField(max_length=100)
        city = serializers.CharField(max_length=200)
        postal_code = serializers.CharField(max_length=15)
        address_detail = serializers.CharField(max_length=400)

    class AddressDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Address
            fields = (
                "id", "province", "city", "user", "postal_code", "address_detail",
            )

    @extend_schema(request=AddressDetailInPutSerializer, responses=AddressDetailOutPutSerializer)
    def put(self, request, id: int):
        serializer = self.AddressDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            address = update_address(
                province=serializer.validated_data.get("province"),
                city=serializer.validated_data.get("city"),
                postal_code=serializer.validated_data.get("postal_code"),
                address_detail=serializer.validated_data.get("address_detail"),
                address_id=id,
                user=request.user,
            )
            return Response(self.AddressDetailOutPutSerializer(address).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: int):
        try:
            delete_address(address_id=id, user=request.user)
            return Response({"message": "آدرس با موفقیت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکل در حذف آدرس پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class AddressListApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, AddressIsOwnerCustomer,)

    class AddressListOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Address
            fields = (
                "id", "province", "city", "user", "postal_code", "address_detail",
            )

    @extend_schema(responses=AddressListOutPutSerializer)
    def get(self, request):
        try:
            addresses = user_address_list(user=request.user)
            return Response(self.AddressListOutPutSerializer(addresses, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکل در دریافت لیست  آدرس پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class FavoriteProductApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class FavoriteProductInPutSerializer(serializers.Serializer):
        product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())

    class FavoriteProductOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = FavoriteProduct
            fields = (
                "product", "user",
            )

    @extend_schema(request=FavoriteProductInPutSerializer, responses=FavoriteProductOutPutSerializer)
    def post(self, request):
        serializer = self.FavoriteProductInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if number_of_favorite_product(user=request.user) > 5:
                return Response({"error": "حداکثر تعداد محصولات برای هر کاربر پنج می باشد."},
                                status=status.HTTP_400_BAD_REQUEST)
            if favoirte_product_exists(user=request.user,
                                       product=serializer.validated_data.get("product")):
                return Response({"error": "محصول قبلا اضافه شده است"}, status=status.HTTP_400_BAD_REQUEST)
            if not request.user.user_type == UserTypes.CUSTOMER:
                return Response({"error": "فقط کاربران عادی می توانند از این ویژگی استفاده کنند"},
                                status=status.HTTP_400_BAD_REQUEST)
            favorite_product = create_favorite_product(
                product=serializer.validated_data.get("product"),
                user=request.user
            )
            return Response(self.FavoriteProductOutPutSerializer(favorite_product).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class FavoriteProductDetailApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, FavoriteProductIsOwnerCustomer,)

    class FavoriteProductDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = FavoriteProduct
            fields = (
                "product", "user",
            )

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id: id):
        try:
            delete_favorite_product(id=id, user=request.user)
            return Response({"message": "محصول با موفقیت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکل در حذف محصول پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class favoriteProductDetailSerializer(serializers.ModelSerializer):
    main_image = serializers.SerializerMethodField()

    def get_main_image(self , obj):
        return reformat_url(url = obj.main_image.url)
    class Meta:
        model = Product
        fields = ("id", "product_type", "title", "slug", "main_image", "price", "off_price", "quantity", "device_model")


class FavoriteProductListApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission, FavoriteProductIsOwnerCustomer,)

    class FavoriteProductListOutPutSerializer(serializers.ModelSerializer):
        product = favoriteProductDetailSerializer()

        class Meta:
            model = FavoriteProduct
            fields = ("id", "product",)

    @extend_schema(responses=FavoriteProductListOutPutSerializer)
    def get(self, request):
        try:
            addresses = user_favorite_product_list(user=request.user)
            return Response(self.FavoriteProductListOutPutSerializer(addresses, many=True).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکل در دریافت لیست  محصولات پیش آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class ContactFormApi(APIView):
    class ContactFormInPutSerializer(serializers.Serializer):
        firstname = serializers.CharField(max_length=100)
        lastname = serializers.CharField(max_length=100)
        subject = serializers.CharField(max_length=100)
        phone_number = serializers.CharField(max_length=11)
        description = serializers.CharField(max_length=500)

    @extend_schema(request=ContactFormInPutSerializer,
                   responses={status.HTTP_200_OK: dict, status.HTTP_400_BAD_REQUEST: dict})
    def post(self, request):
        serializer = self.ContactFormInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            create_contact_form(
                firstname=serializer.validated_data.get("firstname"),
                lastname=serializer.validated_data.get("lastname"),
                subject=serializer.validated_data.get("subject"),
                phone_number=serializer.validated_data.get("phone_number"),
                description=serializer.validated_data.get("description")
            )
            return Response({"message": "فرم در خواست تماس  شما ارسال شد."}, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "فرم ارسال نشد."})


class ContactFormDetailAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    @extend_schema(request=None, responses={status.HTTP_200_OK: None})
    def put(self, request, id: int):
        try:
            update_contact_form(contact_form_id=id)
            return Response({"message": "پیام به حالت بررسی شده درآمد"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class ContactFormListAdminApi(ApiAuthMixin, APIView):
    class ContactFormFilterSerializer(serializers.Serializer):
        is_checked = serializers.BooleanField()

    class ContactFormOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = ContactForm
            fields = ("id", "firstname", "lastname", "phone_number", "description", "is_checked")

    @extend_schema(responses=ContactFormOutPutSerializer, parameters=[ContactFormFilterSerializer])
    def get(self, request):
        serializer = self.ContactFormFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        try:
            filter_parameter = serializer.validated_data.get("is_checked")
            contact_form_list = get_contact_form_list(is_checked=filter_parameter)
            return Response(self.ContactFormOutPutSerializer(contact_form_list, many=True).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"message": "مشکلی در دریافت لیست پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class UserListOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = BaseUser
        fields = ("id", "firstname", "lastname", "phone_number", "email", "birthdate")
class UserListApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)


    class Pagination(LimitOffsetPagination):
        default_limit = 10


    class UserListFilterSerializer(serializers.Serializer):
        search = serializers.CharField(required=False , max_length=100)
        created_at__range = serializers.CharField(required=False , max_length=200)
        birthdate__range = serializers.CharField(required=False , max_length=200)
        phone_number = serializers.CharField(required=False , max_length=13)
        email = serializers.CharField(required=False , max_length=100)


    class PaginatedUserListOutPutSerializer(PaginatedSerializer):
        results = UserListOutPutSerializer(many=True)

    @extend_schema(parameters=[UserListFilterSerializer],responses=PaginatedUserListOutPutSerializer)
    def get(self, request, *args, **kwargs):
        filter_serializer = self.UserListFilterSerializer(data = request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        try:
            users = user_list(filters = filter_serializer.validated_data)
        except Exception as e:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=UserListOutPutSerializer,
            queryset=users,
            view = self,
            request=request
        )


class UserRegisterReport(ApiAuthMixin , APIView):
    permission_classes =  (AdminOrManagerPermission ,)

    class UserListReportFilterSerializer(serializers.Serializer):
        created_at__range = serializers.CharField(required=True , max_length=200)



    @extend_schema(parameters=[UserListReportFilterSerializer] , responses= {200:dict})
    def get(self, request, *args, **kwargs):
        filter_serializer  = self.UserListReportFilterSerializer(data = request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        try:
            user_numbers  =user_number_register(value= filter_serializer.validated_data.get("created_at__range"))
            return Response({"user_register_number":user_numbers})
        except Exception as e:
            return Response({"error": "مشکلی در این ای پی آی وجود دارید."}, status=status.HTTP_400_BAD_REQUEST)




