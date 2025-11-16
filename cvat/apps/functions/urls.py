from __future__ import annotations

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("functions", views.FunctionViewSet, basename="functions")

urlpatterns = [
    path("api/", include(router.urls)),
    path(
        "api/functions/queues/<str:queue_id>/watch",
        views.FunctionQueueWatchView.as_view(),
        name="functions-queue-watch",
    ),
    path(
        "api/functions/queues/<str:queue_id>/requests/acquire",
        views.FunctionQueueAcquireView.as_view(),
        name="functions-queue-request-acquire",
    ),
    path(
        "api/functions/queues/<str:queue_id>/requests/<uuid:request_id>/complete",
        views.FunctionQueueCompleteView.as_view(),
        name="functions-queue-request-complete",
    ),
    path(
        "api/functions/queues/<str:queue_id>/requests/<uuid:request_id>/fail",
        views.FunctionQueueFailView.as_view(),
        name="functions-queue-request-fail",
    ),
    path(
        "api/functions/queues/<str:queue_id>/requests/<uuid:request_id>/update",
        views.FunctionQueueUpdateView.as_view(),
        name="functions-queue-request-update",
    ),
    path(
        "api/functions/requests/<uuid:request_id>",
        views.FunctionRequestDetailView.as_view(),
        name="functions-request-detail",
    ),
    path(
        "api/functions/runs/<uuid:run_id>",
        views.FunctionRunStatusView.as_view(),
        name="functions-run-status",
    ),
    path(
        "api/functions/runs/<uuid:run_id>/cancel",
        views.FunctionRunCancelView.as_view(),
        name="functions-run-cancel",
    ),
]
