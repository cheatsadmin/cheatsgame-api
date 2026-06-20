import datetime
from datetime import timedelta

from django.db import IntegrityError
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.product.permissions import AdminOrManagerPermission, CustomerPermission
from cheatgame.shop.models import DeliveryScheduleType, DeliverySchedule, DeliveryType, DeliveryData, DeliverySide, Order
from cheatgame.shop.selectors.delivery_schedule import get_list_of_delivery_schedule
from cheatgame.shop.services.delivery_schedule import create_delivery_schedule, update_delivery_schedule, \
    delete_delivery_schedule, create_schedule_data, is_delivery_schedule_full, generate_repair_delivery_schedules
from cheatgame.users.models import Address


def default_closed_weekdays():
    return [4]


class DeliveryScheduleAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class DeliveryScheduleInPutSerializer(serializers.Serializer):
        type = serializers.ChoiceField(choices=DeliveryScheduleType.choices())
        start = serializers.DateTimeField()
        end = serializers.DateTimeField()
        capacity = serializers.IntegerField()

    class DeliveryScheduleOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = DeliverySchedule
            fields = ("id", "type", "start", "end", "capacity")

    @extend_schema(request=DeliveryScheduleInPutSerializer, responses=DeliveryScheduleOutPutSerializer)
    def post(self, request):
        serializer = self.DeliveryScheduleInPutSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        try:
            bulk_list = []
            first_date = serializer.validated_data[0].get("start").date()
            for delivery_schedule in serializer.validated_data:
                start = delivery_schedule.get("start")
                end = delivery_schedule.get("end")
                capacity = delivery_schedule.get("capacity")
                # TODO: check if this exist or not
                if capacity <= 0:
                    return Response({"error": "مقدار ظرفیت پذیرش باید بیشتر از صفر باشد."},
                                    status=status.HTTP_400_BAD_REQUEST)
                if start.date() != first_date:
                    return Response({"error": "فقط می توانید برنامه یک روز را وارد کنید."},
                                    status=status.HTTP_400_BAD_REQUEST)
                if (start + datetime.timedelta(hours=1)) > end:
                    return Response({"error": "بازه های نوبت دهی باید حداقل یک ساعت فاصله داشته باشند."},
                                    status=status.HTTP_400_BAD_REQUEST)
                if start.date() != end.date():
                    return Response({"error": "فقط می توانید برنامه یک روز را وارد کنید."},
                                    status=status.HTTP_400_BAD_REQUEST)

                bulk_list.append(
                    DeliverySchedule(
                        type=delivery_schedule.get("type"),
                        start=start,
                        end=end,
                        capacity=delivery_schedule.get("capacity")
                    )
                )
            schedule_list = create_delivery_schedule(delivery_schedule=bulk_list)
            return Response(self.DeliveryScheduleOutPutSerializer(schedule_list, many=True).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "ساخت زمان بندی با مشکل روبه رو شد."}, status=status.HTTP_400_BAD_REQUEST)


class RepairDeliveryScheduleGeneratorAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class RepairDeliveryScheduleGeneratorInPutSerializer(serializers.Serializer):
        from_date = serializers.DateField()
        to_date = serializers.DateField()
        start_time = serializers.TimeField(default=datetime.time(11, 30))
        end_time = serializers.TimeField(default=datetime.time(19, 0))
        slot_minutes = serializers.IntegerField(default=120, min_value=1)
        capacity = serializers.IntegerField(default=15, min_value=1)
        closed_weekdays = serializers.ListField(
            child=serializers.IntegerField(min_value=0, max_value=6),
            required=False,
            default=default_closed_weekdays,
        )

        def validate(self, attrs):
            if attrs["from_date"] > attrs["to_date"]:
                raise serializers.ValidationError({"to_date": "تاریخ پایان نباید قبل از تاریخ شروع باشد."})
            if attrs["start_time"] >= attrs["end_time"]:
                raise serializers.ValidationError({"end_time": "ساعت پایان باید بعد از ساعت شروع باشد."})
            attrs["closed_weekdays"] = sorted(set(attrs.get("closed_weekdays", [])))
            return attrs

    class RepairDeliveryScheduleGeneratorOutPutSerializer(serializers.Serializer):
        created_count = serializers.IntegerField()
        skipped_duplicate_count = serializers.IntegerField()
        skipped_closed_day_count = serializers.IntegerField()
        partial_slot_count = serializers.IntegerField()

    @extend_schema(
        request=RepairDeliveryScheduleGeneratorInPutSerializer,
        responses=RepairDeliveryScheduleGeneratorOutPutSerializer,
    )
    def post(self, request):
        serializer = self.RepairDeliveryScheduleGeneratorInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = generate_repair_delivery_schedules(**serializer.validated_data)
        return Response(
            self.RepairDeliveryScheduleGeneratorOutPutSerializer(result).data,
            status=status.HTTP_200_OK,
        )


class DeliveryScheduleDetailAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class DeliveryScheduleDetailInPutSerializer(serializers.Serializer):
        type = serializers.ChoiceField(choices=DeliveryScheduleType.choices())
        start = serializers.DateTimeField()
        end = serializers.DateTimeField()
        capacity = serializers.IntegerField()

    class DeliveryScheduleDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = DeliverySchedule
            fields = ("id", "type", "start", "end", "capacity")

    @extend_schema(request=DeliveryScheduleDetailInPutSerializer, responses=DeliveryScheduleDetailOutPutSerializer)
    def put(self, request, id):
        serializer = self.DeliveryScheduleDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            start = serializer.validated_data.get("start")
            end = serializer.validated_data.get("end")
            capacity = serializer.validated_data.get("capacity")
            if capacity <= 0:
                return Response({"error": "مقدار ظرفیت پذیرش باید بیشتر از صفر باشد."},
                                status=status.HTTP_400_BAD_REQUEST)
            delivery_schdule = update_delivery_schedule(
                id=id,
                start=start,
                end=end,
                capacity=capacity,
                type=serializer.validated_data.get("type")
            )
            return Response(self.DeliveryScheduleDetailOutPutSerializer(delivery_schdule).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش به وجود آمده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, request, id):
        try:
            delete_delivery_schedule(delivery_schedule_id=id)
            return Response({"message": "زمان بندی با موفقیت حذف گردید"}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در حذف زمان بندی پیش آمد است"}, status=status.HTTP_400_BAD_REQUEST)


class DeliveryScheduleList(APIView):
    class FilterSerializer(serializers.Serializer):
        from_date = serializers.DateField()
        to_date = serializers.DateField()
        type = serializers.ChoiceField(choices=DeliveryScheduleType.choices())

    class DeliveryScheduleListOutPutSerializer(serializers.ModelSerializer):
        reserved_count = serializers.SerializerMethodField()
        remaining_capacity = serializers.SerializerMethodField()
        is_full = serializers.SerializerMethodField()

        class Meta:
            model = DeliverySchedule
            fields = ("id", "type", "capacity", "reserved_count", "remaining_capacity", "is_full", "start", "end")

        def get_reserved_count(self, obj):
            reserved_count = getattr(obj, "reserved_count", None)
            if reserved_count is not None:
                return reserved_count
            return DeliveryData.objects.filter(schedule=obj, is_used=True).count()

        def get_remaining_capacity(self, obj):
            return max(obj.capacity - self.get_reserved_count(obj), 0)

        def get_is_full(self, obj):
            return self.get_remaining_capacity(obj) <= 0

    @extend_schema(parameters=[FilterSerializer], responses=DeliveryScheduleListOutPutSerializer)
    def get(self, request):
        serializer = self.FilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        try:
            from_date = serializer.validated_data.get("from_date")
            to_date = serializer.validated_data.get("to_date")
            type = serializer.validated_data.get("type")
            schedule_delivery = get_list_of_delivery_schedule(from_date=from_date, to_date=to_date, type=type).annotate(
                reserved_count=Count("deliverydata", filter=Q(deliverydata__is_used=True))
            )
            return Response(self.DeliveryScheduleListOutPutSerializer(schedule_delivery, many=True).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در دریافت لیست به وجود آمده است"}, status=status.HTTP_400_BAD_REQUEST)


class DeliveryDataApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    class DeliveryDataInPutSerializer(serializers.Serializer):
        type = serializers.PrimaryKeyRelatedField(queryset=DeliveryType.objects.all())
        schedule = serializers.PrimaryKeyRelatedField(queryset=DeliverySchedule.objects.all(), required=False, allow_null=True)
        address = serializers.PrimaryKeyRelatedField(queryset=Address.objects.all(), required=False)

    class DeliveryDataOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = DeliveryData
            fields = ("id", "type", "schedule", "address",)

    @extend_schema(request=DeliveryDataInPutSerializer, responses=DeliveryDataOutPutSerializer)
    def post(self, request):
        serializer = self.DeliveryDataInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        address = serializer.validated_data.get("address", None)
        type_schedule = serializer.validated_data.get("type")
        schedule = serializer.validated_data.get("schedule")
        if address is not None and address.user_id != request.user.id:
            return Response({"error": "آدرس باید برای خود کاربر باشد."}, status=status.HTTP_400_BAD_REQUEST)
        if type_schedule.side == DeliverySide.SENDTOUSER:
            if address is None:
                return Response({"error": "وارد کردن آدرس ضروری است"}, status=status.HTTP_400_BAD_REQUEST)
        if address is not None:
            existing_delivery_data = DeliveryData.objects.filter(
                type=type_schedule,
                schedule=schedule,
                address=address,
            ).first()
            if (
                existing_delivery_data is not None
                and not existing_delivery_data.is_used
                and not Order.objects.filter(schedule=existing_delivery_data).exists()
            ):
                return Response(self.DeliveryDataOutPutSerializer(existing_delivery_data).data,
                                status=status.HTTP_200_OK)
        if schedule is None:
            delivery_data = create_schedule_data(type=type_schedule, address=address, schedule=None)
            return Response(self.DeliveryDataOutPutSerializer(delivery_data).data, status=status.HTTP_200_OK)
        if is_delivery_schedule_full(schedule=schedule):
            return Response({"error": "زمان انتخاب شده پر شده است "}, status=status.HTTP_400_BAD_REQUEST)
        if type_schedule.side != schedule.type:
            return Response({"error": "نوع ارسال و زمان بندی به درستی انتخاب نشده است"},
                            status=status.HTTP_400_BAD_REQUEST)
        if type_schedule.side == DeliverySide.SENDTOUSER or schedule.type == DeliveryScheduleType.ORDER:
            if schedule.start.date() < (timezone.now() + timedelta(days=4)).date():
                return Response({"error": "زمان انتخابی برای ارسال باید حداقل سه روز بعد از زمان رزرو باشد."},
                                status=status.HTTP_400_BAD_REQUEST)
        try:
            delivery_data = create_schedule_data(type=type_schedule, address=address, schedule=schedule)
        except IntegrityError:
            return Response({"error": "این زمان قبلا رزرو شده است."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.DeliveryDataOutPutSerializer(delivery_data).data, status=status.HTTP_200_OK)
        # except Exception as error:
        #     return Response({"error": "مشکلی در رزرو زمان پیش آمد است"}, status=status.HTTP_400_BAD_REQUEST)
