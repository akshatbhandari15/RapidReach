"""
lead_finder/tools/bigquery_utils.py
BigQuery helpers for persisting discovered leads.
Creates dataset/table if needed, uploads batched rows.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import Any, Optional

from common.config import (
    GOOGLE_CLOUD_PROJECT,
    BIGQUERY_DATASET,
    BIGQUERY_LEADS_TABLE,
)

logger = logging.getLogger(__name__)

LEADS_SCHEMA = [
    {"name": "place_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "business_name", "type": "STRING"},
    {"name": "address", "type": "STRING"},
    {"name": "city", "type": "STRING"},
    {"name": "phone", "type": "STRING"},
    {"name": "email", "type": "STRING"},
    {"name": "website", "type": "STRING"},
    {"name": "rating", "type": "FLOAT"},
    {"name": "total_ratings", "type": "INTEGER"},
    {"name": "business_type", "type": "STRING"},
    {"name": "has_website", "type": "BOOLEAN"},
    {"name": "lead_status", "type": "STRING"},
    {"name": "discovered_at", "type": "TIMESTAMP"},
    {"name": "notes", "type": "STRING"},
]

# Schema for the no-website leads table (subset for quick filtering)
NO_WEBSITE_LEADS_TABLE = f"{BIGQUERY_LEADS_TABLE}_no_website"


def _get_client():
    """Lazy-load BigQuery client."""
    try:
        from google.cloud import bigquery
        return bigquery.Client(project=GOOGLE_CLOUD_PROJECT)
    except Exception as e:
        logger.error(f"BigQuery client init failed: {e}")
        return None


def ensure_table_exists(table_name: Optional[str] = None) -> bool:
    """Create dataset and table if they don't exist."""
    client = _get_client()
    if not client:
        return False
    try:
        from google.cloud import bigquery

        dataset_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}"
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "US"
        client.create_dataset(dataset, exists_ok=True)

        target_table = table_name or BIGQUERY_LEADS_TABLE
        table_ref = f"{dataset_ref}.{target_table}"
        schema = [
            bigquery.SchemaField(f["name"], f["type"], mode=f.get("mode", "NULLABLE"))
            for f in LEADS_SCHEMA
        ]
        table = bigquery.Table(table_ref, schema=schema)
        client.create_table(table, exists_ok=True)
        logger.info(f"Table {table_ref} ready")
        return True
    except Exception as e:
        logger.error(f"ensure_table_exists failed: {e}")
        return False


def upload_leads(leads: list[dict[str, Any]]) -> str:
    """
    Upload a batch of leads to BigQuery.

    Args:
        leads: List of lead dicts matching LEADS_SCHEMA.

    Returns:
        JSON string with result summary.
    """
    if not leads:
        return json.dumps({"uploaded": 0, "errors": []})

    client = _get_client()
    if not client:
        return json.dumps({"uploaded": 0, "errors": ["BigQuery client unavailable"]})

    ensure_table_exists()
    table_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_LEADS_TABLE}"

    # Add discovered_at timestamp if missing
    for lead in leads:
        if "discovered_at" not in lead or not lead["discovered_at"]:
            lead["discovered_at"] = datetime.utcnow().isoformat()

    errors_list = []
    try:
        result = client.insert_rows_json(table_ref, leads)
        if result:
            errors_list = [str(e) for e in result]
            logger.warning(f"BQ insert errors: {errors_list}")
    except Exception as e:
        errors_list.append(str(e))
        logger.error(f"BQ upload failed: {e}")

    uploaded = len(leads) - len(errors_list)
    return json.dumps({"uploaded": uploaded, "errors": errors_list})


def upload_no_website_leads(leads: list[dict[str, Any]]) -> str:
    """
    Upload leads that have no website to the dedicated no-website table.
    These are the highest-priority prospects.

    Args:
        leads: List of lead dicts (should all have has_website=False).

    Returns:
        JSON string with result summary.
    """
    if not leads:
        return json.dumps({"uploaded": 0, "errors": []})

    client = _get_client()
    if not client:
        return json.dumps({"uploaded": 0, "errors": ["BigQuery client unavailable"]})

    ensure_table_exists(NO_WEBSITE_LEADS_TABLE)
    table_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}.{NO_WEBSITE_LEADS_TABLE}"

    for lead in leads:
        if "discovered_at" not in lead or not lead["discovered_at"]:
            lead["discovered_at"] = datetime.utcnow().isoformat()

    errors_list = []
    try:
        result = client.insert_rows_json(table_ref, leads)
        if result:
            errors_list = [str(e) for e in result]
    except Exception as e:
        errors_list.append(str(e))
        logger.error(f"BQ no-website upload failed: {e}")

    uploaded = len(leads) - len(errors_list)
    return json.dumps({"uploaded": uploaded, "errors": errors_list})


def query_leads(city: Optional[str] = None, limit: int = 100) -> str:
    """
    Query existing leads from BigQuery.

    Args:
        city: Optional city filter. If None, returns all leads.
        limit: Maximum number of rows to return.

    Returns:
        JSON string with list of lead dicts.
    """
    client = _get_client()
    if not client:
        return json.dumps({"leads": [], "error": "BigQuery client unavailable"})

    table_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_LEADS_TABLE}"

    try:
        if city:
            query = f"SELECT * FROM `{table_ref}` WHERE city = @city ORDER BY discovered_at DESC LIMIT @limit"
            job_config = _query_config([
                {"name": "city", "parameterType": {"type": "STRING"}, "parameterValue": {"value": city}},
                {"name": "limit", "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(limit)}},
            ])
        else:
            query = f"SELECT * FROM `{table_ref}` ORDER BY discovered_at DESC LIMIT {limit}"
            job_config = None

        rows = client.query(query, job_config=job_config).result()
        leads = [dict(row) for row in rows]

        # Serialize datetime objects
        for lead in leads:
            for k, v in lead.items():
                if hasattr(v, "isoformat"):
                    lead[k] = v.isoformat()

        return json.dumps({"leads": leads, "total": len(leads)})
    except Exception as e:
        logger.error(f"BQ query failed: {e}")
        return json.dumps({"leads": [], "error": str(e)})


def query_no_website_leads(city: Optional[str] = None, limit: int = 100) -> str:
    """
    Query leads that have no website — the highest-priority prospects.

    Args:
        city: Optional city filter.
        limit: Maximum number of rows.

    Returns:
        JSON string with list of lead dicts.
    """
    client = _get_client()
    if not client:
        return json.dumps({"leads": [], "error": "BigQuery client unavailable"})

    table_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_LEADS_TABLE}"

    try:
        if city:
            query = (
                f"SELECT * FROM `{table_ref}` "
                f"WHERE has_website = FALSE AND city = @city "
                f"ORDER BY rating DESC LIMIT @limit"
            )
            job_config = _query_config([
                {"name": "city", "parameterType": {"type": "STRING"}, "parameterValue": {"value": city}},
                {"name": "limit", "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(limit)}},
            ])
        else:
            query = (
                f"SELECT * FROM `{table_ref}` "
                f"WHERE has_website = FALSE "
                f"ORDER BY rating DESC LIMIT {limit}"
            )
            job_config = None

        rows = client.query(query, job_config=job_config).result()
        leads = [dict(row) for row in rows]

        for lead in leads:
            for k, v in lead.items():
                if hasattr(v, "isoformat"):
                    lead[k] = v.isoformat()

        return json.dumps({"leads": leads, "total": len(leads)})
    except Exception as e:
        logger.error(f"BQ no-website query failed: {e}")
        return json.dumps({"leads": [], "error": str(e)})


def _query_config(params: list[dict]):
    """Build a BigQuery QueryJobConfig with parameters."""
    try:
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(p["name"], p["parameterType"]["type"], p["parameterValue"]["value"])
                for p in params
            ]
        )
        return job_config
    except Exception:
        return None
