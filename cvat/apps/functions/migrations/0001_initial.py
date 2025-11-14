import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("engine", "0095_fix_related_names"),
    ]

    operations = [
        migrations.CreateModel(
            name="Function",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=256)),
                ("description", models.TextField(blank=True)),
                ("provider", models.CharField(choices=[("native", "Native")], default="native", max_length=32)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("detector", "detector"),
                            ("interactor", "interactor"),
                            ("reid", "reid"),
                            ("tracker", "tracker"),
                        ],
                        max_length=32,
                    ),
                ),
                ("supported_shape_types", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="functions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="FunctionLabel",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=256)),
                ("label_type", models.CharField(default="any", max_length=32)),
                ("attributes", models.JSONField(blank=True, default=list)),
                ("sublabels", models.JSONField(blank=True, default=list)),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "function",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="labels",
                        to="functions.function",
                    ),
                ),
            ],
            options={
                "ordering": ["position", "id"],
                "unique_together": {("function", "name")},
            },
        ),
        migrations.CreateModel(
            name="AnnotationRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("category", models.CharField(choices=[("batch", "Batch"), ("interactive", "Interactive")], max_length=16)),
                (
                    "type",
                    models.CharField(max_length=64),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("parameters", models.JSONField(blank=True, default=dict)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("progress", models.FloatField(default=0.0)),
                ("agent_id", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "function",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="annotation_requests",
                        to="functions.function",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="annotation_requests",
                        to="engine.job",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="annotation_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="annotation_requests",
                        to="engine.task",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="function",
            index=models.Index(fields=["owner"], name="functions_f_owner_c5c23a_idx"),
        ),
        migrations.AddIndex(
            model_name="function",
            index=models.Index(fields=["kind"], name="functions_f_kind_c4c73d_idx"),
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(fields=["function", "status"], name="functions_a_func_id_8c6b8b_idx"),
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(fields=["task"], name="functions_a_task_id_a61bf9_idx"),
        ),
    ]
