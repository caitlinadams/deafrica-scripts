"""
# Generate a gap report between deafrica-landsat-dev and usgs-landsat bulk file

This DAG runs weekly and creates a gap report in the folowing location:
s3://deafrica-landsat-dev/status-report/<satellite_date.csv.gz>

"""

import csv
import gzip
import json
import time
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import click
import pandas as pd
from odc.aws import s3_dump, s3_client
from odc.aws.inventory import list_inventory
from urlpath import URL

from deafrica import __version__
from deafrica.utils import (
    slack_url,
    update_stac,
    send_slack_notification,
    setup_logging,
    download_file_to_tmp,
    convert_str_to_date,
    time_process,
)

FILES = {
    "landsat_8": "LANDSAT_OT_C2_L2.csv.gz",
    "landsat_7": "LANDSAT_ETM_C2_L2.csv.gz",
    "landsat_5": "LANDSAT_TM_C2_L2.csv.gz",
}

BASE_BULK_CSV_URL = URL(
    "https://landsat.usgs.gov/landsat/metadata_service/bulk_metadata_files/"
)
AFRICA_GZ_PATHROWS_URL = URL(
    "https://raw.githubusercontent.com/digitalearthafrica/deafrica-extent/master/deafrica-usgs-pathrows.csv.gz"
)

LANDSAT_INVENTORY_PATH = URL(
    "s3://deafrica-landsat-inventory/deafrica-landsat/deafrica-landsat-inventory/"
)
USGS_S3_BUCKET_PATH = URL("s3://usgs-landsat")


def get_and_filter_keys_from_files(file_path: Path):
    """
    Read scenes from the bulk GZ file and filter
    :param file_path:
    :return:
    """

    def build_path(file_row):
        # USGS changes - for _ when generates the CSV bulk file
        identifier = file_row["Sensor Identifier"].lower().replace("_", "-")
        year_acquired = convert_str_to_date(file_row["Date Acquired"]).year

        return (
            "collection02/level-2/standard/{identifier}/{year_acquired}/"
            "{target_path}/{target_row}/{display_id}/".format(
                identifier=identifier,
                year_acquired=year_acquired,
                target_path=file_row["WRS Path"].zfill(3),
                target_row=file_row["WRS Row"].zfill(3),
                display_id=file_row["Display ID"],
            )
        )

    africa_pathrows = set(
        pd.read_csv(
            AFRICA_GZ_PATHROWS_URL,
            header=None,
        ).values.ravel()
    )

    with gzip.open(file_path, "rt") as csv_file:
        return set(
            build_path(row)
            for row in csv.DictReader(csv_file)
            if (
                # Filter to skip all LANDSAT_4
                row.get("Satellite") is not None
                and row["Satellite"] != "LANDSAT_4"
                and row["Satellite"] != "4"
                # Filter to get just day
                and (
                    row.get("Day/Night Indicator") is not None
                    and row["Day/Night Indicator"].upper() == "DAY"
                )
                # Filter to get just from Africa
                and (
                    row.get("WRS Path") is not None
                    and row.get("WRS Row") is not None
                    and int(f"{row['WRS Path'].zfill(3)}{row['WRS Row'].zfill(3)}")
                    in africa_pathrows
                )
            )
        )


def get_and_filter_keys(landsat: str) -> set:
    """
    Retrieve key list from a inventory bucket and filter

    :param landsat:(str)
    :return:(set)
    """

    sat_prefix = None
    if landsat == "landsat_8":
        sat_prefix = "LC08"
    elif landsat == "landsat_7":
        sat_prefix = "LE07"
    elif landsat == "landsat_5":
        sat_prefix = "LT05"

    if not sat_prefix:
        raise Exception(f"Informed satellite {landsat} not supported")

    list_json_keys = list_inventory(
        manifest=str(LANDSAT_INVENTORY_PATH),
        prefix="collection02",
        suffix="_stac.json",
        contains=sat_prefix,
        n_threads=200,
    )
    return set(f"{key.Key.rsplit('/', 1)[0]}/" for key in list_json_keys)


def generate_buckets_diff(
    bucket_name: str,
    satellite_name: str,
    file_name: str,
    update_stac: bool = False,
    notification_url: str = None,
):
    """
    Compare USGS bulk files and Africa inventory bucket detecting differences
    A report containing missing keys will be written to AFRICA_S3_BUCKET_PATH
    """

    log = setup_logging()

    start_timer = time.time()

    log.info("Task started")

    landsat_status_report_path = URL(f"s3://{bucket_name}/status-report/")
    landsat_status_report_url = URL(
        f"https://{bucket_name}.s3.af-south-1.amazonaws.com/status-report/"
    )
    environment = "DEV" if "dev" in bucket_name else "PDS"
    log.info(f"Environment {environment}")
    log.info(f"Bucket Name {bucket_name}")
    log.info(f"Satellite Name {satellite_name}")
    log.info(f"File Name {file_name}")
    log.info(f"Update all ({update_stac})")
    log.info(f"Notification URL all ({notification_url})")

    # Create connection to the inventory S3 bucket
    log.info(f"Retrieving keys from inventory bucket {LANDSAT_INVENTORY_PATH}")
    dest_paths = get_and_filter_keys(landsat=satellite_name)

    log.info(f"INVENTORY bucket number of objects {len(dest_paths)}")
    log.info(f"INVENTORY 10 first {list(dest_paths)[0:10]}")
    date_string = datetime.now().strftime("%Y-%m-%d")

    # Download bulk file
    log.info("Download Bulk file")
    file_path = download_file_to_tmp(url=str(BASE_BULK_CSV_URL), file_name=file_name)

    # Retrieve keys from the bulk file
    log.info("Filtering keys from bulk file")
    source_paths = get_and_filter_keys_from_files(file_path)

    log.info(f"BULK FILE number of objects {len(source_paths)}")
    log.info(f"BULK 10 First {list(source_paths)[0:10]}")

    output_filename = "No missing scenes were found"

    if update_stac:
        log.info("FORCED UPDATE ACTIVE!")
        missing_scenes = source_paths
        orphaned_scenes = []

    else:
        # Keys that are missing, they are in the source but not in the bucket
        log.info("Filtering missing scenes")
        missing_scenes = [
            str(USGS_S3_BUCKET_PATH / path)
            for path in source_paths.difference(dest_paths)
        ]

        # Keys that are orphan, they are in the bucket but not found in the files
        log.info("Filtering orphan scenes")
        orphaned_scenes = [
            str(URL(f"s3://{bucket_name}") / path)
            for path in dest_paths.difference(source_paths)
        ]

        log.info(f"Found {len(missing_scenes)} missing scenes")
        log.info(f"missing_scenes 10 first keys {list(missing_scenes)[0:10]}")
        log.info(f"Found {len(orphaned_scenes)} orphaned scenes")
        log.info(f"orphaned_scenes 10 first keys {list(orphaned_scenes)[0:10]}")

    landsat_s3 = s3_client(region_name="af-south-1")

    if len(missing_scenes) > 0 or len(orphaned_scenes) > 0:
        output_filename = (
            f"{satellite_name}_{date_string}_gap_report.json"
            if not update_stac
            else URL(f"{date_string}_gap_report_update.json")
        )

        log.info(
            f"Report file will be saved in {landsat_status_report_path / output_filename}"
        )
        missing_orphan_scenes_json = json.dumps(
            {"orphan": orphaned_scenes, "missing": missing_scenes}
        )

        s3_dump(
            data=missing_orphan_scenes_json,
            url=str(landsat_status_report_path / output_filename),
            s3=landsat_s3,
            ContentType="application/json",
        )

    report_output = (
        str(landsat_status_report_url / output_filename)
        if len(missing_scenes) > 0 or len(orphaned_scenes) > 0
        else output_filename
    )
    message = dedent(
        f"*{satellite_name.upper()} GAP REPORT - {environment}*\n "
        f"Missing Scenes: {len(missing_scenes)}\n"
        f"Orphan Scenes: {len(orphaned_scenes)}\n"
        f"Report: {report_output}\n"
    )

    log.info(message)

    log.info(
        f"File {file_name} processed and sent in {time_process(start=start_timer)}"
    )

    if not update_stac and (len(missing_scenes) > 200 or len(orphaned_scenes) > 200):
        if notification_url is not None:
            send_slack_notification(
                notification_url, f"{satellite_name} Gap Report", message
            )
        raise Exception(f"More than 200 scenes were found \n {message}")


@click.argument(
    "bucket_name",
    type=str,
    nargs=1,
    required=True,
    default="Bucket where the gap report is",
)
@click.argument(
    "satellite",
    type=str,
    nargs=1,
    required=True,
    default="satellite to be compared, supported ones (landsat_8, landsat_7, landsat_5)",
)
@update_stac
@slack_url
@click.option("--version", is_flag=True, default=False)
@click.command("landsat-gap-report")
def cli(
    bucket_name: str,
    satellite: str,
    update_stac: bool = False,
    slack_url: str = None,
    version: bool = False,
):
    """
    Publish missing scenes
    """

    if version:
        click.echo(__version__)

    generate_buckets_diff(
        bucket_name=bucket_name,
        satellite_name=satellite,
        file_name=FILES.get(satellite, None),
        update_stac=update_stac,
        notification_url=slack_url,
    )
