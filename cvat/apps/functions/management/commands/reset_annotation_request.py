from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from cvat.apps.functions.models import AnnotationRequest, AnnotationRequestStatus


class Command(BaseCommand):
    help = "Reset an annotation request so it can be reprocessed by an agent."

    def add_arguments(self, parser):
        parser.add_argument("request_id", help="Annotation request UUID")

    def handle(self, *args, **options):
        try:
            request_uuid = uuid.UUID(options["request_id"])
        except (KeyError, ValueError) as exc:
            raise CommandError("request_id must be a valid UUID") from exc

        try:
            annotation_request = AnnotationRequest.objects.get(pk=request_uuid)
        except AnnotationRequest.DoesNotExist as exc:
            raise CommandError(f"Annotation request {request_uuid} does not exist") from exc

        annotation_request.status = AnnotationRequestStatus.PENDING
        annotation_request.agent_id = ""
        annotation_request.progress = 0.0
        annotation_request.result = {}
        annotation_request.save(update_fields=["status", "agent_id", "progress", "result", "updated_at"])

        self.stdout.write(self.style.SUCCESS(f"Annotation request {request_uuid} was reset"))
