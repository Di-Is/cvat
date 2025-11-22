from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "functions",
            "0006_annotationrequest_run_summary",
        ),
    ]

    operations = [
        migrations.RenameModel(
            old_name="FunctionRunSummary",
            new_name="FunctionRunStatus",
        ),
        migrations.RenameField(
            model_name="annotationrequest",
            old_name="run_summary",
            new_name="run_status",
        ),
        migrations.RemoveIndex(
            model_name="annotationrequest",
            name="functions_a_run_sum_7d413e_idx",
        ),
        migrations.RemoveIndex(
            model_name="annotationrequest",
            name="functions_a_func_id_8c6b8b_idx",
        ),
        migrations.RemoveIndex(
            model_name="annotationrequest",
            name="functions_a_task_id_a61bf9_idx",
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(
                fields=["run_status"],
                name="functions_ar_run_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(
                fields=["function", "status"],
                name="functions_ar_fn_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="annotationrequest",
            index=models.Index(
                fields=["task"],
                name="functions_ar_task_idx",
            ),
        ),
        migrations.AddField(
            model_name="functionrunstatus",
            name="last_error",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="functionrunstatus",
            name="payload_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RemoveIndex(
            model_name="functionrunstatus",
            name="functions_f_owner__42d17c_idx",
        ),
        migrations.RemoveIndex(
            model_name="functionrunstatus",
            name="functions_f_functi_f76030_idx",
        ),
        migrations.RemoveIndex(
            model_name="functionrunstatus",
            name="functions_f_job_id_2f5341_idx",
        ),
        migrations.AddIndex(
            model_name="functionrunstatus",
            index=models.Index(
                fields=["owner", "run_id"],
                name="functions_frs_owner_run_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="functionrunstatus",
            index=models.Index(
                fields=["function", "status"],
                name="functions_frs_fn_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="functionrunstatus",
            index=models.Index(
                fields=["job", "status", "-updated_at"],
                name="functions_frs_job_status_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="function",
            name="functions_f_owner_c5c23a_idx",
        ),
        migrations.RemoveIndex(
            model_name="function",
            name="functions_f_kind_c4c73d_idx",
        ),
        migrations.AddIndex(
            model_name="function",
            index=models.Index(
                fields=["owner"],
                name="functions_fn_owner_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="function",
            index=models.Index(
                fields=["kind"],
                name="functions_fn_kind_idx",
            ),
        ),
    ]
