from rest_framework import permissions
from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied
from cheatgame.users.models import UserTypes


class AdminOrManagerPermission(BasePermission):

    def has_permission(self, request, view) -> bool:
        if request.user.is_anonymous:
            return False
        if request.user.user_type == UserTypes.ADMIN or request.user.user_type == UserTypes.MANAGER:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")


class ManagerPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        if request.user.is_anonymous:
            return False
        if request.user.user_type == UserTypes.MANAGER:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")


class CustomerPermission(BasePermission):

    def has_permission(self, request, view) -> bool:
        if request.user.is_anonymous:
            raise PermissionDenied("ابتدا وارد شوید.")
        if request.user.user_type == UserTypes.CUSTOMER and request.user.phone_verified:
            return True
        else:
            raise PermissionDenied("ابتدا وارد شوید.")





class BlogCommentIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request, view, obj) -> bool:
        if request.user.id == obj.user.id:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")



class QuestionIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request, view, obj) -> bool:
        if request.user.id == obj.sender.id:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")


class AddressIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request, view, obj) -> bool:
        if request.user.id == obj.user.id:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")


class FavoriteProductIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request, view, obj) -> bool:
        if  request.user.id == obj.user.id:
            return True
        else:
            raise PermissionDenied("شمابه این بخش دسترسی ندارید.")


class CartItemIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request, view, obj) -> bool:
        if request.user.id == obj.cart.user.id:
            return True
        else:
            raise PermissionDenied("ابتدا وارد شوید.")

class IssueReportIsOwnerCustomer(BasePermission):

    def has_object_permission(self, request , view,obj) -> bool:
         if request.user.id == obj.user_id:
             return True
         else:
             raise PermissionDenied("شما به این بخش دسترسی ندارید.")