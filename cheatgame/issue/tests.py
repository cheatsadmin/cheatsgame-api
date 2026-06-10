from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.issue.models import IssueReport, IssueReportStatus
from cheatgame.shop.models import (
    DeliveryData,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
)
from cheatgame.product.models import DeliveryOption
from cheatgame.users.models import Address, BaseUser, UserTypes


class IssueReportOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_verified_user("09128880001")
        self.other_user = self.create_verified_user("09128880002")
        self.address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="1234567890",
            address_detail="Repair address",
        )
        self.other_address = Address.objects.create(
            user=self.other_user,
            province="Tehran",
            city="Tehran",
            postal_code="2234567890",
            address_detail="Other repair address",
        )
        self.delivery_type = DeliveryType.objects.create(
            name="Repair pickup",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        start = timezone.now() + timedelta(days=5)
        self.schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=start,
            end=start + timedelta(hours=2),
            capacity=2,
        )
        self.client.force_authenticate(self.user)

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Issue",
            lastname="User",
            password="StrongPass123!",
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def create_delivery_data(self, address):
        return DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=address,
        )

    def test_customer_can_update_own_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        issue_report.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, delivery_data.id)
        self.assertEqual(issue_report.status, IssueReportStatus.DURING)

    def test_customer_cannot_update_another_users_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.other_user, explanation="Other repair")
        delivery_data = self.create_delivery_data(self.address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        issue_report.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertEqual(issue_report.status, IssueReportStatus.IMPERFECT)

    def test_customer_cannot_use_another_users_delivery_data_for_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.other_address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertEqual(issue_report.status, IssueReportStatus.IMPERFECT)


class GenerateHtmlPermissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = self.create_user("09128880003")
        self.manager = self.create_user("09128880004", user_type=UserTypes.MANAGER)

    def create_user(self, phone_number, user_type=UserTypes.CUSTOMER):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Html",
            lastname="User",
            password="StrongPass123!",
            user_type=user_type,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def test_anonymous_cannot_generate_html(self):
        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_generate_html(self):
        self.client.force_authenticate(self.customer)

        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_generate_html(self):
        self.client.force_authenticate(self.manager)

        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Disposition"], "attachment; filename=output.html")
