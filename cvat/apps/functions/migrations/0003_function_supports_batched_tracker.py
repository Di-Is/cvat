from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("functions", "0002_function_interactor_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="function",
            name="supports_batched_tracker",
            field=models.BooleanField(default=False),
        ),
    ]
