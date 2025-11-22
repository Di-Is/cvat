from __future__ import annotations

import uuid
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cvat.apps.functions.models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    FunctionRunStatus,
)
from cvat.apps.functions.run_status import (
    calculate_progress,
    derive_status,
    estimate_request_frame_span,
)


class Command(BaseCommand):
    help = "Populate FunctionRunStatus rows for existing tracker runs."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--run-id",
            action="append",
            dest="run_ids",
            help="Specific run_id UUID to backfill (can be passed multiple times).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of distinct runs to process.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without writing to the database.",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Number of distinct run IDs to fetch per iterator chunk (default: 500).",
        )
        parser.add_argument(
            "--skip-locked",
            action="store_true",
            help="Use SELECT ... FOR UPDATE SKIP LOCKED when acquiring annotation requests.",
        )
        parser.add_argument(
            "--include-interactive",
            action="store_true",
            help="Backfill interactor requests that never had a run_status assigned.",
        )

    def handle(self, *args, **options) -> None:
        run_ids_opt: list[str] | None = options.get("run_ids")
        limit: int | None = options.get("limit")
        dry_run: bool = options.get("dry_run", False)
        chunk_size: int = options.get("chunk_size") or 500
        skip_locked: bool = options.get("skip_locked", False)
        include_interactive: bool = options.get("include_interactive", False)
        if limit is not None and limit <= 0:
            limit = None
        if chunk_size <= 0:
            raise CommandError("chunk-size must be a positive integer")

        base_qs = AnnotationRequest.objects.filter(run_status__isnull=True).exclude(
            parameters__function_run_id__isnull=True,
        )
        if run_ids_opt:
            normalized = []
            for value in run_ids_opt:
                try:
                    normalized.append(str(uuid.UUID(str(value))))
                except (TypeError, ValueError):
                    self.stderr.write(self.style.ERROR(f"Invalid run_id supplied: {value!r}"))
            if not normalized:
                self.stderr.write(self.style.WARNING("No valid run_id provided, exiting."))
                return
            base_qs = base_qs.filter(parameters__function_run_id__in=normalized)

        run_ids = (
            base_qs.values_list("parameters__function_run_id", flat=True)
            .order_by("parameters__function_run_id")
            .distinct()
        )

        processed = 0
        for run_id in run_ids.iterator(chunk_size=chunk_size):
            if limit is not None and processed >= limit:
                break
            try:
                run_uuid = uuid.UUID(str(run_id))
            except (TypeError, ValueError):
                self.stderr.write(self.style.WARNING(f"Skipping malformed run_id value {run_id!r}"))
                continue

            if FunctionRunStatus.objects.filter(run_id=run_uuid).exists():
                continue

            with transaction.atomic():
                run_requests = (
                    AnnotationRequest.objects.select_for_update(skip_locked=skip_locked)
                    .filter(
                        parameters__function_run_id=str(run_uuid),
                        run_status__isnull=True,
                    )
                    .order_by("created_at")
                )
                if not run_requests.exists():
                    continue

                summary = self._build_summary(run_uuid=run_uuid, requests=run_requests)
                if not summary:
                    continue

                processed += 1
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[dry-run] would backfill run {run_uuid} "
                            f"(requests={summary.total_requests}, function_id={summary.function_id})"
                        )
                    )
                    continue

                summary.save()
                locked_request_ids = list(run_requests.values_list("id", flat=True))
                AnnotationRequest.objects.filter(id__in=locked_request_ids).update(
                    run_status=summary
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Backfilled run {run_uuid} "
                    f"(requests={summary.total_requests}, status={summary.status})"
                )
            )

        if processed == 0:
            self.stdout.write("No runs required backfilling.")

        if include_interactive:
            interactive_processed = self._backfill_interactive_requests(
                dry_run=dry_run,
                chunk_size=chunk_size,
                skip_locked=skip_locked,
            )
            if interactive_processed == 0:
                self.stdout.write("No interactor requests required backfilling.")

    def _build_summary(
        self,
        *,
        run_uuid: uuid.UUID,
        requests: Iterable[AnnotationRequest],
    ) -> FunctionRunStatus | None:
        first_request = requests.order_by("created_at").first()
        if not first_request:
            return None

        job = first_request.job
        if job is None:
            self.stderr.write(
                self.style.WARNING(f"Skipping run {run_uuid}: first request has no job reference"),
            )
            return None

        total_requests = requests.count()
        completed_qs = requests.filter(status=AnnotationRequestStatus.DONE)
        failed_qs = requests.filter(status=AnnotationRequestStatus.FAILED)
        cancelled_qs = requests.filter(status=AnnotationRequestStatus.CANCELLED)
        completed_count = completed_qs.count()
        failed_count = failed_qs.count()
        cancelled_count = cancelled_qs.count()

        expected_frames = self._extract_expected_frames(first_request)
        completed_frames = 0
        if expected_frames is not None:
            for ar in completed_qs.iterator():
                completed_frames += estimate_request_frame_span(ar)
            completed_frames = min(completed_frames, expected_frames)

        running_request = (
            requests.filter(status=AnnotationRequestStatus.RUNNING).order_by("-updated_at").first()
        )
        failed_request = failed_qs.order_by("-updated_at").first()

        summary = FunctionRunStatus(
            run_id=run_uuid,
            owner=first_request.owner,
            function=first_request.function,
            task=first_request.task,
            job=job,
            total_requests=total_requests,
            completed_requests=completed_count,
            failed_requests=failed_count,
            cancelled_requests=cancelled_count,
            expected_frames=expected_frames,
            completed_frames=completed_frames,
            active_request_id=running_request.id if running_request else None,
            active_request_type=running_request.type if running_request else "",
            active_request_updated_at=running_request.updated_at if running_request else None,
            active_request_progress=running_request.progress if running_request else 0.0,
            active_request_frame_span=estimate_request_frame_span(running_request),
            failed_request_id=failed_request.id if failed_request else None,
        )
        summary.status = derive_status(summary)
        summary.progress = calculate_progress(summary)
        return summary

    def _extract_expected_frames(self, init_request: AnnotationRequest) -> int | None:
        params = init_request.parameters or {}
        start_frame = params.get("start_frame")
        target_frame = params.get("target_frame")
        if isinstance(start_frame, int) and isinstance(target_frame, int) and target_frame >= start_frame:
            return (target_frame - start_frame) + 1
        return None

    def _backfill_interactive_requests(
        self,
        *,
        dry_run: bool,
        chunk_size: int,
        skip_locked: bool,
    ) -> int:
        interactive_ids = (
            AnnotationRequest.objects.filter(
                category=AnnotationRequestCategory.INTERACTIVE,
                run_status__isnull=True,
            )
            .order_by("created_at")
            .values_list("id", flat=True)
        )

        processed = 0
        for request_id in interactive_ids.iterator(chunk_size=chunk_size):
            with transaction.atomic():
                annotation_request = (
                    AnnotationRequest.objects.select_for_update(skip_locked=skip_locked)
                    .filter(pk=request_id, run_status__isnull=True)
                    .first()
                )
                if not annotation_request:
                    continue
                if not annotation_request.job:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Skipping interactor request {annotation_request.id}: missing job reference",
                        )
                    )
                    continue

                params = dict(annotation_request.parameters or {})
                existing_run_id = params.get("function_run_id")
                try:
                    run_uuid = uuid.UUID(str(existing_run_id)) if existing_run_id else annotation_request.id
                except (TypeError, ValueError):
                    run_uuid = annotation_request.id

                summary = FunctionRunStatus(
                    run_id=run_uuid,
                    owner=annotation_request.owner,
                    function=annotation_request.function,
                    task=annotation_request.task,
                    job=annotation_request.job,
                    total_requests=1,
                    completed_requests=1
                    if annotation_request.status == AnnotationRequestStatus.DONE
                    else 0,
                    failed_requests=1
                    if annotation_request.status == AnnotationRequestStatus.FAILED
                    else 0,
                    cancelled_requests=1
                    if annotation_request.status == AnnotationRequestStatus.CANCELLED
                    else 0,
                    expected_frames=1,
                    completed_frames=1
                    if annotation_request.status == AnnotationRequestStatus.DONE
                    else 0,
                    active_request_id=annotation_request.id
                    if annotation_request.status
                    in {AnnotationRequestStatus.PENDING, AnnotationRequestStatus.RUNNING}
                    else None,
                    active_request_type=annotation_request.type
                    if annotation_request.status
                    in {AnnotationRequestStatus.PENDING, AnnotationRequestStatus.RUNNING}
                    else "",
                    active_request_updated_at=annotation_request.updated_at
                    if annotation_request.status
                    in {AnnotationRequestStatus.PENDING, AnnotationRequestStatus.RUNNING}
                    else None,
                    active_request_progress=annotation_request.progress
                    if annotation_request.status
                    in {AnnotationRequestStatus.PENDING, AnnotationRequestStatus.RUNNING}
                    else 0.0,
                    active_request_frame_span=1,
                )
                summary.status = derive_status(summary)
                summary.progress = calculate_progress(summary)

                processed += 1
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[dry-run] would backfill interactor request {annotation_request.id}",
                        )
                    )
                    continue

                summary.save()
                params["function_run_id"] = str(summary.run_id)
                annotation_request.parameters = params
                annotation_request.run_status = summary
                annotation_request.save(update_fields=["parameters", "run_status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Backfilled interactor request {annotation_request.id} (status={annotation_request.status})",
                    )
                )

        return processed
