from .backfill_run_status import Command as BackfillRunStatusCommand


class Command(BackfillRunStatusCommand):
    help = (
        "DEPRECATED: use `python manage.py backfill_run_status` instead. "
        "This alias will be removed in a future release."
    )
