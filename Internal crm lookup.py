"""
Internal CRM lookup for the Zenduit Devices pipeline.

Reads the Zoho Analytics table "Internal Billing Mapping" (fed from the billing-mapping
Google Sheet every 3 hours) and adds three columns to the device data BEFORE it is
uploaded to the Zenduit Devices table:

    Internal_crm_account_id    – looked up by Device_Id
    Internal_account_name      – looked up by Device_Id
    Internal_mapping_source    – "Mapping" if the device is in Internal Billing Mapping,
                                 "Default (admin)" if not yet (falls back to the ZenduOne
                                 AccountId / Company_Name, which is what the sheet will
                                 seed it with anyway)

Existing columns are never changed and the row count is guaranteed unchanged.

Fail-safe: if the mapping table can't be read or looks wrong, load_internal_mapping()
raises, so main() stops BEFORE the Zoho truncate – yesterday's Zenduit Devices table stays
intact instead of being replaced with blank/wrong internal values.

Uses the pipeline's existing Zoho OAuth token (needs ZohoAnalytics.data.read scope).
"""
from __future__ import annotations

import io
import json
import time

import pandas as pd
import requests

MAPPING_VIEW_ID = "953790000060710002"   # Internal Billing Mapping
MAP_KEY = "Device ID"
MAP_ID = "Internal_crm_account_id"
MAP_NAME = "Internal_account_name"
NEW_COLUMNS = ["Internal_crm_account_id", "Internal_account_name", "Internal_mapping_source"]
SOURCE_MAPPING = "Mapping"
SOURCE_DEFAULT = "Default (admin)"
MIN_EXPECTED_ROWS = 1000                  # guard against a half-empty / failed import


def _export_mapping_csv(api_domain, workspace_id, org_id, access_token, retries=3) -> str:
    url = f"{api_domain}/workspaces/{workspace_id}/views/{MAPPING_VIEW_ID}/data"
    config = {"responseFormat": "csv", "selectedColumns": [MAP_KEY, MAP_ID, MAP_NAME]}
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "ZANALYTICS-ORGID": org_id}
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params={"CONFIG": json.dumps(config)}, headers=headers, timeout=300)
            if r.status_code == 200:
                return r.content.decode("utf-8-sig")
            last = f"HTTP {r.status_code}: {r.text[:300]}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(10 * attempt)
    raise RuntimeError(f"Could not export Internal Billing Mapping after {retries} tries: {last}")


def mapping_from_csv(text: str) -> pd.DataFrame:
    """Validate the exported table and return Device_Id -> internal columns."""
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in (MAP_KEY, MAP_ID, MAP_NAME) if c not in df.columns]
    if missing:
        raise RuntimeError(f"Internal Billing Mapping is missing column(s) {missing}. Got: {list(df.columns)}")

    df = df[[MAP_KEY, MAP_ID, MAP_NAME]].apply(lambda s: s.str.strip())
    df = df[df[MAP_KEY] != ""]
    if len(df) < MIN_EXPECTED_ROWS:
        raise RuntimeError(f"Internal Billing Mapping has only {len(df)} rows – looks like a failed import.")
    dups = df.loc[df[MAP_KEY].duplicated(), MAP_KEY]
    if len(dups):
        raise RuntimeError(f"Duplicate Device IDs in Internal Billing Mapping: {dups.head(10).tolist()}")
    return df.rename(columns={MAP_KEY: "Device_Id"})


def load_internal_mapping(api_domain, workspace_id, org_id, access_token) -> pd.DataFrame:
    mapping = mapping_from_csv(_export_mapping_csv(api_domain, workspace_id, org_id, access_token))
    print(f"📒 Internal CRM mapping loaded from Zoho: {len(mapping):,} devices")
    return mapping


def attach_internal_crm(final_df: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """Left-join the internal columns onto final_df by Device_Id. Existing columns untouched."""
    clash = [c for c in NEW_COLUMNS if c in final_df.columns]
    if clash:
        raise RuntimeError(f"Columns already exist in device data: {clash}")

    left = final_df.copy()
    left["_dev_key"] = left["Device_Id"].astype(str).str.strip()
    right = mapping.rename(columns={"Device_Id": "_dev_key"})
    out = left.merge(right, on="_dev_key", how="left", validate="many_to_one").drop(columns="_dev_key")
    if len(out) != len(final_df):
        raise RuntimeError("Row count changed during internal CRM lookup – aborting upload.")

    found = out[MAP_ID].notna()
    out[MAP_ID] = out[MAP_ID].where(found, out["AccountId"] if "AccountId" in out else "")
    out[MAP_NAME] = out[MAP_NAME].where(found, out["Company_Name"] if "Company_Name" in out else "")
    out["Internal_mapping_source"] = found.map({True: SOURCE_MAPPING, False: SOURCE_DEFAULT})

    n = int(found.sum())
    print(f"🔗 Internal CRM lookup: {n:,} from mapping | {len(out) - n:,} defaulted to admin values")
    return out
