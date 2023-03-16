# !/usr/bin/python
# -*- coding: utf-8 -*-

from datetime import datetime

from django.db.models import Q

from cl.lib.command_utils import VerboseCommand, logger
from cl.search.models import Docket, DocketEntry, RECAPDocument


def clean_up_duplicate_appellate_entries(
    courts_ids: list[str], after_date: str | None, clean: bool
) -> None:
    """Find and clean duplicated appellate entries after courts enabled
    document numbers.

    :param courts_ids: The list of court IDs to search for duplicate entries.
    :param after_date: Optional. Search for duplicate entries after this date.
    :param clean: True if a cleanup should be performed, or False to only
    report how many entries will be cleaned.
    :return: None
    """

    # Default dates when courts enabled document numbers, used to look for
    # duplicates after these dates.
    default_court_dates = {"ca5": "2023-01-08", "ca11": "2022-10-01"}
    for court in courts_ids:
        duplicated_entries_count = 0
        duplicated_entries = []
        if not default_court_dates.get(court) and not after_date:
            # No default after_date defined for court.
            logger.info(f"No default after_date defined for {court}.")
            continue

        if after_date:
            court_date = datetime.strptime(after_date, "%Y-%m-%d")
        else:
            court_date = datetime.strptime(
                default_court_dates[court], "%Y-%m-%d"
            )

        # Only check dockets with entries created after the courts enabled
        # numbers or the date provided.
        docket_with_entries = Docket.objects.filter(
            court_id=court, docket_entries__date_created__gte=court_date
        ).distinct()

        if not docket_with_entries:
            logger.info(
                f"Skipping {court}, no entries created after {court_date.date()} found."
            )
            continue

        for docket in docket_with_entries.iterator():
            # Look for docket entries that use the pacer_doc_id as number or
            # unnumbered entries.
            des = docket.docket_entries.filter(
                Q(entry_number__gt=10_000_000)
                | Q(entry_number=None and ~Q(description__exact=""))
            )
            for de in des.iterator():
                related_rd = de.recap_documents.filter(
                    document_type=RECAPDocument.PACER_DOCUMENT
                ).first()
                if related_rd.pacer_doc_id == "":
                    # If the pacer_doc_id is empty, look for duplicates by date
                    # and description.
                    duplicated_des = DocketEntry.objects.filter(
                        docket=docket,
                        description=de.description,
                        date_filed=de.date_filed,
                    ).exclude(pk=de.pk)
                    if not duplicated_des.exists():
                        continue
                    duplicated_entries_count += 1
                    duplicated_entries.append(de.pk)
                    if clean:
                        de.delete()
                else:
                    # Look for duplicates by pacer_doc_id if available.
                    duplicated_des = DocketEntry.objects.filter(
                        docket=docket,
                        recap_documents__pacer_doc_id=related_rd.pacer_doc_id,
                    ).exclude(pk=de.pk)
                    if not duplicated_des.exists():
                        continue
                    duplicated_entries.append(de.pk)
                    duplicated_entries_count += 1
                    if clean:
                        de.delete()

        print("List of duplicated entries:", duplicated_entries)
        action = "Found"
        if clean:
            action = "Cleaned"
        logger.info(
            f"{action} {duplicated_entries_count} entries in {court} "
            f"after {court_date.date()}."
        )


class Command(VerboseCommand):
    help = (
        "Find and clean duplicated appellate entries after courts enable "
        "document numbers."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--clean",
            action="store_true",
            help="Clean up duplicated entries.",
            default=False,
        )

        parser.add_argument(
            "--courts",
            required=True,
            help="A comma-separated list of courts to find duplicates.",
        )

        parser.add_argument(
            "--after_date",
            help="Look for duplicated entries after this date Y-m-d.",
            default=None,
        )

    def handle(self, *args, **options):
        courts = options["courts"].split(",")
        after_date = options["after_date"]
        if options["clean"]:
            clean_up_duplicate_appellate_entries(
                courts, after_date, clean=True
            )
        else:
            clean_up_duplicate_appellate_entries(
                courts, after_date, clean=False
            )
