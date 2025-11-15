from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("functions", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="function",
            name="min_pos_points",
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name="function",
            name="min_neg_points",
            field=models.IntegerField(default=-1),
        ),
        migrations.AddField(
            model_name="function",
            name="startswith_box",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="function",
            name="startswith_box_optional",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="function",
            name="help_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="function",
            name="animated_gif",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="function",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
