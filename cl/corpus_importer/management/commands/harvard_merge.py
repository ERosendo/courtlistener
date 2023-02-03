import datetime
import itertools
import json
import logging
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup
from django.db import transaction
from juriscraper.lib.html_utils import get_html_from_element
from juriscraper.lib.string_utils import harmonize, titlecase
from lxml.html import fromstring

from cl.corpus_importer.management.commands.harvard_opinions import (
    clean_docket_number,
    parse_extra_fields,
    validate_dt,
)
from cl.corpus_importer.utils import match_lists
from cl.lib.command_utils import VerboseCommand
from cl.lib.string_diff import get_cosine_similarity
from cl.people_db.lookup_utils import extract_judge_last_name
from cl.search.models import Docket, Opinion, OpinionCluster


class AuthorException(Exception):
    """Error found in author merger."""

    def __init__(self, message: str) -> None:
        self.message = message


class JudgeException(Exception):
    """An exception for wrong judges"""

    def __init__(self, message: str) -> None:
        self.message = message


def read_json(cluster_id: int) -> Dict[str, Any] | None:
    """Helper method to read json into object

    :param cluster_id: the cluster to fetch the filepath for
    :return: Harvard data as a json object or None
    """
    cluster = OpinionCluster.objects.get(id=cluster_id)
    if cluster.filepath_json_harvard:
        return json.load(cluster.filepath_json_harvard)
    return None


def fetch_non_harvard_data(harvard_data: Dict[str, Any]) -> Dict[str, Any]:
    """Get data from harvard casebody and preprocess

    :param harvard_data:
    :return: dict with values extracted from casebody
    """
    soup = BeautifulSoup(harvard_data["casebody"]["data"], "lxml")

    # Some documents contain images in the HTML
    # Flag them for a later crawl by using the placeholder '[[Image]]'
    judge_list = [
        extract_judge_last_name(x.text) for x in soup.find_all("judges")
    ]
    author_list = [
        extract_judge_last_name(x.text) for x in soup.find_all("author")
    ]
    # Flatten and dedupe list of judges
    judges = ", ".join(
        sorted(
            list(set(itertools.chain.from_iterable(judge_list + author_list)))
        )
    )
    judges = titlecase(judges)
    all_data = {"judges": judges}
    short_fields = ["attorneys", "disposition", "otherdate", "seealso"]
    long_fields = [
        "syllabus",
        "summary",
        "history",
        "headnotes",
        "correction",
    ]
    short_data = parse_extra_fields(soup, short_fields, False)
    long_data = parse_extra_fields(soup, long_fields, True)
    all_data.update(short_data)
    all_data.update(long_data)
    all_data = {k: v for k, v in all_data.items() if v}
    return all_data


def combine_non_overlapping_data(
    cluster_id: int, harvard_data: Dict[str, Any]
) -> Dict[str, Tuple]:
    """Combine non overlapping data and return dictionary of data for merging

    :param cluster_id: Cluster id to merge
    :param harvard_data: The harvard data as json
    :return: Optional dictionary of data to continue to merge
    """
    opinion_cluster = OpinionCluster.objects.get(id=cluster_id)
    all_data = fetch_non_harvard_data(harvard_data)
    clean_dictionary = {}
    for key, value in all_data.items():
        cl_value = getattr(opinion_cluster, key)
        if not cl_value:
            OpinionCluster.objects.filter(id=cluster_id).update(**{key: value})
        else:
            if value != cl_value:
                clean_dictionary[key] = (value, cl_value)

    if "otherdate" in clean_dictionary.keys():
        clean_dictionary["other_dates"] = clean_dictionary.pop("otherdate")
    if "seealso" in clean_dictionary.keys():
        clean_dictionary["cross_reference"] = clean_dictionary.pop("seealso")

    return clean_dictionary


def merge_long_fields(
    cluster_id: int, field_name: str, overlapping_data: Tuple[str, str]
) -> None:
    """Merge two long text fields

    :param cluster_id: Cluster id to update
    :param field_name: field name to update in opinion cluster
    :param overlapping_data: data to compare from harvard and courtlistener
    :return: None
    """
    harvard_data, cl_data = overlapping_data[0], overlapping_data[1]
    # Do some text comparison
    similarity = get_cosine_similarity(harvard_data, cl_data)
    if similarity < 0.9:
        # they are not too similar, choose the larger one
        if len(harvard_data) > len(cl_data):
            OpinionCluster.objects.filter(id=cluster_id).update(
                **{field_name: harvard_data}
            )
    else:
        pass
        # should we log long data not really being similar?


def merge_judges(
    cluster_id: int,
    overlapping_data: Tuple[str, str],
) -> None:
    """Merge overlapping judge values

    :param cluster_id: Cluster id to update
    :param overlapping_data: data to compare from harvard and courtlistener
    :return: None
    """
    harvard_data, cl_data = overlapping_data

    # Get last names from each source
    cl_clean = set(extract_judge_last_name(cl_data))
    harvard_clean = set(extract_judge_last_name(harvard_data))
    judges = titlecase(", ".join(extract_judge_last_name(harvard_data)))

    if harvard_clean.issuperset(cl_clean) and harvard_clean != cl_clean:
        OpinionCluster.objects.filter(id=cluster_id).update(judges=judges)
    elif not harvard_clean.intersection(cl_clean):
        raise JudgeException("Judges are completely different.")


def merge_dates(
    cluster_id: int,
    field_name: str,
    overlapping_data: Tuple[str, datetime.date],
) -> None:
    """Compare two dates and choose the best to update the opinion cluster
    the value if one value is better than the other

    :param cluster_id: Cluster id to update
    :param field_name: field name to update in opinion cluster
    :param overlapping_data: data to compare
    :return: None
    """
    harvard_data = overlapping_data[0]
    cluster = OpinionCluster.objects.filter(id=cluster_id).first()
    harvard_date, harvard_date_is_approximate = validate_dt(harvard_data)
    if cluster.date_filed_is_approximate and not harvard_date_is_approximate:
        # if harvard date is not approximate, it should be better
        OpinionCluster.objects.filter(id=cluster_id).update(
            **{field_name: harvard_date}
        )


def merge_strings(
    cluster_id: int, field_name: str, overlapping_data: Tuple[str, str]
) -> None:
    """Compare two strings and choose the largest

    :param cluster_id: Cluster id to update
    :param field_name: field name to update in opinion cluster
    :param overlapping_data: data to compare from harvard and courtlistener
    :return: None
    """
    harvard_data, cl_data = overlapping_data[0], overlapping_data[1]
    if len(harvard_data) > len(cl_data):
        OpinionCluster.objects.filter(id=cluster_id).update(
            **{field_name: harvard_data}
        )


def merge_docket_numbers(cluster_id: int, harvard_docket_number: str) -> None:
    """Merge Docket Numbers

    :param cluster_id: The cluster id of the merging item
    :param harvard_docket_number: The harvard docket number
    :return: None
    """
    cl_docket_number = OpinionCluster.objects.get(
        id=cluster_id
    ).docket.docket_number

    if cl_docket_number:
        # Check if docket number exists
        # e.g. CL docket id #3952066 doesn't have
        if cl_docket_number in harvard_docket_number:
            Docket.objects.update(docket_number=harvard_docket_number)
        else:
            cl_clean_docket = clean_docket_number(cl_docket_number)
            h_clean_docket = clean_docket_number(harvard_docket_number)

            # Check if their relatively similar and if so save the harvard one
            # if its longer
            similarity = get_cosine_similarity(cl_clean_docket, h_clean_docket)
            if similarity > 0.8:
                if len(harvard_docket_number) > len(cl_docket_number):
                    Docket.objects.update(docket_number=harvard_docket_number)


def merge_case_names(cluster_id: int, harvard_data: Dict[str, Any]) -> None:
    """Merge case names

    :param cluster_id: The cluster id of the merging item
    :param harvard_data: json data from harvard case
    :return: None
    """
    cluster = OpinionCluster.objects.get(id=cluster_id)
    harvard_case_name = harmonize(harvard_data["name_abbreviation"])
    harvard_case_name_full = harmonize(harvard_data["name"])
    # Make sure case names are harmonized
    cluster_case_name = harmonize(cluster.case_name)
    cluster_case_name_full = harmonize(cluster.case_name_full)

    update_dict = {}
    # Case with full case names
    if not cluster_case_name_full and harvard_case_name_full:
        update_dict["case_name_full"] = harvard_case_name_full
        # Change stored value to new
        cluster_case_name_full = harvard_case_name_full
    elif cluster_case_name_full and harvard_case_name_full:
        if len(harvard_case_name_full) > len(cluster_case_name_full):
            # Select best case name based on string length
            update_dict["case_name_full"] = harvard_case_name_full
            # Change stored value to new
            cluster_case_name_full = harvard_case_name_full
    else:
        # We don't care if harvard data is empty or both are empty
        pass

    # Case with abbreviated case names
    if not cluster_case_name and harvard_case_name:
        update_dict["case_name"] = harvard_case_name
        # Change stored value to new
        cluster_case_name = harvard_case_name
    elif cluster_case_name and harvard_case_name:
        if len(harvard_case_name) > len(cluster_case_name):
            # Select best case name based on string length
            update_dict["case_name"] = harvard_case_name
            # Change stored value to new
            cluster_case_name = harvard_case_name
    else:
        # We don't care if harvard data is empty or both are empty
        pass

    if cluster_case_name_full and cluster_case_name:
        if len(cluster_case_name) > len(cluster_case_name_full):
            # Swap field values
            update_dict["case_name"] = cluster_case_name_full
            update_dict["case_name_full"] = cluster_case_name

    if update_dict:
        OpinionCluster.objects.filter(id=cluster_id).update(**update_dict)


def merge_overlapping_data(
    cluster_id: int, clean_dictionary: Dict[str, Any]
) -> None:
    """Merge overlapping data

    :param cluster_id: the cluster id
    :param clean_dictionary: the dictionary of data to merge
    :return: None
    """
    if clean_dictionary != {}:
        logging.info(f"Merging complete for: {cluster_id}")
        return

    long_fields = [
        "syllabus",
        "summary",
        "history",
        "headnotes",
        "correction",
        "cross_reference",
        "disposition",
    ]

    for field_name in clean_dictionary.keys():
        if field_name in long_fields:
            merge_long_fields(
                cluster_id,
                field_name,
                clean_dictionary.get(field_name),
            )
        elif field_name in ["date_filed", "other_dates"]:
            merge_dates(
                cluster_id,
                field_name,
                clean_dictionary.get(field_name),
            )
        elif field_name == "judges":
            merge_judges(
                cluster_id,
                clean_dictionary.get(field_name),
            )
        elif field_name == "attorneys":
            merge_strings(
                cluster_id,
                field_name,
                clean_dictionary.get(field_name),
            )
        else:
            logging.info(f"Field not considered in the process: {field_name}")


def update_docket(cluster_id: int):
    """Update docket source and complete

    :param cluster_id: the cluster id
    :return: None
    """
    docket = OpinionCluster.objects.get(id=cluster_id).docket
    source = docket.source
    docket.source = Docket.HARVARD + source
    docket.save()


def merge_opinion_clusters(cluster_id: Optional[int]) -> None:
    """Merge opinion cluster, docket and opinion data from Harvard

    :param cluster_id: The cluster ID to merger
    :return: None
    """
    harvard_data = read_json(cluster_id)
    if harvard_data:
        try:
            with transaction.atomic():
                map_and_merge_opinions(cluster_id, harvard_data)
                clean_dictionary = combine_non_overlapping_data(
                    cluster_id, harvard_data
                )
                merge_docket_numbers(cluster_id, harvard_data["docket_number"])
                merge_case_names(cluster_id, harvard_data)
                merge_overlapping_data(cluster_id, clean_dictionary)
                update_docket(cluster_id=cluster_id)
                logging.info(msg=f"Finished merging cluster: {cluster_id}")

        except AuthorException:
            logging.warning(msg=f"Author Exception for cluster {cluster_id}")
        except JudgeException:
            logging.warning(msg=f"Judge exception for: {cluster_id}")
    else:
        logging.warning(msg=f"No Harvard json for cluster: {cluster_id}")


def start_merger(cluster_id=None) -> None:
    """Start the merger

    Query the database for a list of opinions that have not been merged yet
    :param cluster_id: Cluster ID if available
    :return: None
    """
    if cluster_id:
        cluster_ids = [cluster_id]
    else:
        sources_without_harvard = [
            source[0]
            for source in Docket.SOURCE_CHOICES
            if "Harvard" not in source[1]
        ]
        cluster_ids = OpinionCluster.objects.filter(
            docket__source__in=sources_without_harvard,
            filepath_json_harvard__isnull=False,
        ).values_list("id", flat=True)

    for cluster_id in cluster_ids:
        merge_opinion_clusters(cluster_id=cluster_id)


def fetch_cl_opinion_content(sub_opinions: [Opinion]) -> [str]:
    """Fetch CL opinion Content

    This is a simple helper function to grab an opinion content to compare
    against the harvard xml
    :param sub_opinions: Sub opinions for a cluster
    :return: Opinion text as a list
    """
    cl_opinions = []
    for sub_opinion in sub_opinions:
        for name in (
            "html_columbia",
            "html_with_citations",
            "html",
            "html_lawbox",
            "plain_text",
        ):
            op_type = name
            opinion_content = getattr(sub_opinion, name)
            if not opinion_content:
                continue
            break
        if "html" in op_type:
            html = fromstring(opinion_content)
            opinion_content = html.text_content()
        cl_opinions.append(opinion_content)
    return cl_opinions


def map_and_merge_opinions(cluster: int, harvard_data: Dict[str, Any]) -> None:
    """Map and merge opinion data

    :param cluster: Cluster ID
    :param harvard_data: json data from harvard case
    :return: None
    """
    used_combined_opinions = False
    case_body = harvard_data["casebody"]["data"]
    sub_opinions = Opinion.objects.filter(cluster__id=cluster)
    harvard_html = fromstring(case_body.encode()).xpath(".//opinion")
    harvard_opinions = [op for op in harvard_html]
    cl_opinions = fetch_cl_opinion_content(sub_opinions=sub_opinions)
    if len(harvard_opinions) != len(cl_opinions):
        used_combined_opinions = True
    else:
        # crashes without a sub opinion ... that makes sense
        matches = match_lists(harvard_opinions, cl_opinions)
        if not matches:
            used_combined_opinions = True

        if not used_combined_opinions:
            for k, v in matches.items():
                op = sub_opinions[k]
                if op.author_str != "" and op.author is None:
                    hvd = harvard_opinions[v].xpath(".//author/text()")[0]
                    if extract_judge_last_name(
                        op.author_str
                    ) != extract_judge_last_name(hvd):
                        raise AuthorException(
                            "Authors don't match - log error"
                        )

                    op.author_str = harvard_opinions[v].xpath(
                        ".//author/text()"
                    )[0]
                op.xml_harvard = str(
                    get_html_from_element(harvard_opinions[v])
                )
                op.save()

    if used_combined_opinions:
        # If we cant quite merge the opinions. Create combined opinion.
        # This occurs when the harvard data or CL data is slightly askew.
        Opinion.objects.create(
            xml_harvard=case_body,
            cluster=OpinionCluster.objects.get(id=cluster),
            type="010combined",
        )


class Command(VerboseCommand):
    help = "Merge harvard opinions into CL opinions"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--cluster-id",
            type=str,
            help="The cluster id to merge",
            required=False,
        )
        parser.add_argument(
            "--no-debug",
            action="store_true",
            help="Turn off debug logging",
        )

    def handle(self, *args, **options) -> None:
        if options["no_debug"]:
            logging.disable(logging.DEBUG)
        start_merger(cluster_id=options["cluster_id"])
