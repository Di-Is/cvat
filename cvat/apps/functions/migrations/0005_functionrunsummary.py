import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("engine", "0095_fix_related_names"),
        ("functions", "0004_enable_sam2_tracker_batching"),
    ]

    operations = [
        migrations.CreateModel(
            name="FunctionRunSummary",
            fields=[
                ("run_id", models.UUIDField(editable=False, primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("total_requests", models.PositiveIntegerField(default=0)),
                ("completed_requests", models.PositiveIntegerField(default=0)),
                ("failed_requests", models.PositiveIntegerField(default=0)),
                ("cancelled_requests", models.PositiveIntegerField(default=0)),
                ("expected_frames", models.PositiveIntegerField(blank=True, null=True)),
                ("completed_frames", models.PositiveIntegerField(default=0)),
                ("progress", models.FloatField(default=0.0)),
                ("active_request_id", models.UUIDField(blank=True, null=True)),
                ("active_request_type", models.CharField(blank=True, max_length=64)),
                ("active_request_updated_at", models.DateTimeField(blank=True, null=True)),
                ("active_request_progress", models.FloatField(default=0.0)),
                ("active_request_frame_span", models.PositiveIntegerField(default=0)),
                ("failed_request_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "function",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="run_summaries",
                        to="functions.function",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="function_run_summaries",
                        to="engine.job",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="function_run_summaries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="function_run_summaries",
                        to="engine.task",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="functionrunsummary",
            index=models.Index(fields=["owner", "run_id"], name="functions_f_owner__42d17c_idx"),
        ),
        migrations.AddIndex(
            model_name="functionrunsummary",
            index=models.Index(fields=["function", "status"], name="functions_f_functi_f76030_idx"),
        ),
        migrations.AddIndex(
            model_name="functionrunsummary",
            index=models.Index(fields=["job"], name="functions_f_job_id_2f5341_idx"),
        ),
    ]
