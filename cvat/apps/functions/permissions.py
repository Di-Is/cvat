from __future__ import annotations

from rest_framework.permissions import BasePermission


class IsFunctionOwner(BasePermission):
    """Restrict access to authenticated owners of functions."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        owner_id = getattr(obj, "owner_id", None)
        return owner_id == request.user.id
