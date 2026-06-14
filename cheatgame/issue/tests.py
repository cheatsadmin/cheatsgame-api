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
        delivery_data.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, delivery_data.id)
        self.assertEqual(issue_report.status, IssueReportStatus.DURING)
        self.assertTrue(delivery_data.is_used)

    def test_customer_can_retrieve_own_issue_report_detail(self):
        delivery_data = self.create_delivery_data(self.address)
        issue_report = IssueReport.objects.create(
            user=self.user,
            explanation="Own repair",
            delivery_data=delivery_data,
            status=IssueReportStatus.DURING,
        )

        response = self.client.get(f"/api/issue/issue-report-detail/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], issue_report.id)
        self.assertEqual(response.data["public_tracking_code"], issue_report.public_tracking_code)
        self.assertTrue(response.data["public_tracking_code"].startswith("FX-"))
        self.assertEqual(response.data["delivery_data"]["id"], delivery_data.id)
        self.assertEqual(response.data["delivery_data"]["address"]["address_detail"], self.address.address_detail)
        self.assertEqual(response.data["status"], IssueReportStatus.DURING.value)
        self.assertFalse(response.data["is_paid"])

    def test_issue_reports_get_unique_public_tracking_codes(self):
        first_report = IssueReport.objects.create(user=self.user, explanation="First repair")
        second_report = IssueReport.objects.create(user=self.user, explanation="Second repair")

        self.assertTrue(first_report.public_tracking_code.startswith("FX-"))
        self.assertTrue(second_report.public_tracking_code.startswith("FX-"))
        self.assertNotEqual(first_report.public_tracking_code, second_report.public_tracking_code)
        self.assertNotEqual(first_report.public_tracking_code, str(first_report.id))

    def test_customer_issue_report_list_returns_public_tracking_code(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")

        response = self.client.get("/api/issue/issue-report-list/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["id"], issue_report.id)
        self.assertEqual(response.data[0]["public_tracking_code"], issue_report.public_tracking_code)
        self.assertTrue(response.data[0]["public_tracking_code"].startswith("FX-"))

    def test_customer_cannot_retrieve_another_users_issue_report_detail(self):
        issue_report = IssueReport.objects.create(user=self.other_user, explanation="Other repair")

        response = self.client.get(f"/api/issue/issue-report-detail/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_replace_issue_report_reservation(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        first_delivery_data = self.create_delivery_data(self.address)
        first_delivery_data.is_used = True
        first_delivery_data.save(update_fields=["is_used"])
        issue_report.delivery_data = first_delivery_data
        issue_report.status = IssueReportStatus.DURING
        issue_report.save(update_fields=["delivery_data", "status"])
        second_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="3234567890",
            address_detail="Second repair address",
        )
        second_delivery_data = self.create_delivery_data(second_address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": second_delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        second_delivery_data.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, first_delivery_data.id)
        self.assertFalse(second_delivery_data.is_used)

    def test_customer_can_resubmit_same_issue_report_reservation_without_duplicate(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.address)
        delivery_data.is_used = True
        delivery_data.save(update_fields=["is_used"])
        issue_report.delivery_data = delivery_data
        issue_report.status = IssueReportStatus.DURING
        issue_report.save(update_fields=["delivery_data", "status"])

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 1)

    def test_customer_cannot_schedule_issue_report_when_slot_is_full(self):
        self.schedule.capacity = 1
        self.schedule.save(update_fields=["capacity"])
        used_delivery_data = self.create_delivery_data(self.address)
        used_delivery_data.is_used = True
        used_delivery_data.save(update_fields=["is_used"])
        other_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="4234567890",
            address_detail="Another repair address",
        )
        candidate_delivery_data = self.create_delivery_data(other_address)
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": candidate_delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        candidate_delivery_data.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertFalse(candidate_delivery_data.is_used)

    def test_customer_can_schedule_issue_report_same_address_when_slot_has_capacity(self):
        used_delivery_data = self.create_delivery_data(self.address)
        used_delivery_data.is_used = True
        used_delivery_data.save(update_fields=["is_used"])
        existing_issue_report = IssueReport.objects.create(user=self.user, explanation="Existing repair")
        existing_issue_report.delivery_data = used_delivery_data
        existing_issue_report.status = IssueReportStatus.DURING
        existing_issue_report.save(update_fields=["delivery_data", "status"])
        issue_report = IssueReport.objects.create(user=self.user, explanation="New repair")

        book_time_response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(book_time_response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(book_time_response.data["id"], used_delivery_data.id)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": book_time_response.data["id"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        issue_report.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, book_time_response.data["id"])
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 2)

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
