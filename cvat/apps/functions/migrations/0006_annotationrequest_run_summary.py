import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("functions", "0005_functionrunsummary"),
    ]

    operations = [
        migrations.AddField(
            model_name="annotationrequest",
            name="run_summary",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="annotation_requests",
                to="functions.functionrunsummary",
            ),
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(fields=["run_summary"], name="functions_a_run_sum_7d413e_idx"),
        ),
    ]
