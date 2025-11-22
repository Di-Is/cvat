from django.db import migrations


def _enable_sam2_tracker_batching(apps, schema_editor):
    Function = apps.get_model("functions", "Function")
    Function.objects.filter(
        kind="tracker",
        provider="native",
        name__icontains="sam2",
    ).update(supports_batched_tracker=True)


def _disable_sam2_tracker_batching(apps, schema_editor):
    Function = apps.get_model("functions", "Function")
    Function.objects.filter(
        kind="tracker",
        provider="native",
        name__icontains="sam2",
    ).update(supports_batched_tracker=False)


class Migration(migrations.Migration):
    dependencies = [
        ("functions", "0003_function_supports_batched_tracker"),
    ]

    operations = [
        migrations.RunPython(
            _enable_sam2_tracker_batching,
            reverse_code=_disable_sam2_tracker_batching,
        ),
    ]
