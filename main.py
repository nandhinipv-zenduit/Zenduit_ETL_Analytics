import os
import json
import requests
import pandas as pd

# ==========================================================
# ZENDUIT CONFIG
# ==========================================================
BASE_URL = "https://trax-admin-service.zenduit.com"

USERNAME = os.getenv("ZENDU_EMAIL")
PASSWORD = os.getenv("ZENDU_PASSWORD")

session = requests.Session()
token = None

OUTPUT_FILE = (
    r"C:\Users\suppo\PyCharmMiscProject\.venv\Billing_audit_engine\OP\zenduit_Master.xlsx"
)

# ==========================================================
# ZOHO ANALYTICS CONFIG
# ==========================================================
ZOHO_ANALYTICS = {
    "client_id": os.environ.get("ZOHO_CLIENT_ID_UNI"),
    "client_secret": os.environ.get("ZOHO_CLIENT_SECRET_UNI"),
    "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN_UNI"),
    "accounts_url": "https://accounts.zoho.com",
    "api_domain": "https://analyticsapi.zoho.com/restapi/v2",
}

ZOHO_ORG_ID = "67409019"
ZOHO_WORKSPACE_ID = "953790000013364003"

# Zenduit Master table
ZOHO_VIEW_ID = "953790000054827175"

ZOHO_MAX_BYTES_PER_IMPORT = 14 * 1024 * 1024


# ==========================================================
# AUTHENTICATE
# ==========================================================
def authenticate():
    global token

    url = f"{BASE_URL}/Auth/Authenticate"

    payload = {
        "Username": USERNAME,
        "Password": PASSWORD
    }

    res = session.post(url, json=payload)
    res.raise_for_status()

    token = res.json().get("Token")

    if not token:
        raise Exception("Token missing in response")

    session.headers.update({
        "Authorization": f"Bearer {token}"
    })

    print("🔑 Authentication Success")


# ==========================================================
# GENERIC LIST EXTRACTOR (same shape used by all GetAll calls)
# ==========================================================
def _extract_list(data):
    return (
        data if isinstance(data, list)
        else data.get("Data")
        or data.get("data")
        or data.get("items")
        or []
    )


# ==========================================================
# FETCH COMPANIES
# ==========================================================
def fetch_companies():

    print("\n🏢 Fetching Companies...")

    url = f"{BASE_URL}/Company/GetAll"

    res = session.post(url, json={})
    res.raise_for_status()

    companies = _extract_list(res.json())

    df = pd.json_normalize(companies)

    print(f"✔ Total Companies: {len(df):,}")

    return df


# ==========================================================
# FETCH DEVICES
# ==========================================================
def fetch_devices():

    print("\n📡 Fetching Devices...")

    url = f"{BASE_URL}/Device/GetAll"

    res = session.post(url, json={})
    res.raise_for_status()

    devices = _extract_list(res.json())

    df = pd.json_normalize(devices, sep="__")

    print(
        f"✔ Total Devices: {len(df):,} "
        f"| Columns: {len(df.columns):,}"
    )

    return df


# ==========================================================
# FETCH RESELLERS  (needed for the report's "Reseller" column)
# NOTE: endpoint assumed to follow the same /GetAll pattern.
# Adjust the path if your API uses a different route.
# ==========================================================
def fetch_resellers():

    print("\n🏷️  Fetching Resellers...")

    url = f"{BASE_URL}/Reseller/GetAll"

    try:
        res = session.post(url, json={})
        res.raise_for_status()
        resellers = _extract_list(res.json())
        df = pd.json_normalize(resellers)
        print(f"✔ Total Resellers: {len(df):,}")
        return df
    except Exception as e:
        print(f"⚠️  Could not fetch resellers ({e}) — Reseller_Name will be blank")
        return pd.DataFrame(columns=["Id", "Name"])


# ==========================================================
# HELPER: make sure expected columns exist so selection
# and coalescing don't KeyError when the API omits a field
# ==========================================================
def ensure_columns(df, cols, label):
    for c in cols:
        if c not in df.columns:
            print(f"⚠️  {label} field '{c}' not found in API response — adding empty column")
            df[c] = None
    return df


# ==========================================================
# ZOHO ACCESS TOKEN
# ==========================================================
def zoho_get_access_token():

    r = requests.post(
        f"{ZOHO_ANALYTICS['accounts_url']}/oauth/v2/token",
        data={
            "refresh_token": ZOHO_ANALYTICS["refresh_token"],
            "client_id": ZOHO_ANALYTICS["client_id"],
            "client_secret": ZOHO_ANALYTICS["client_secret"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )

    r.raise_for_status()

    token_data = r.json()

    if "access_token" not in token_data:
        raise Exception(f"Failed getting Zoho token: {token_data}")

    return token_data["access_token"]


# ==========================================================
# ZOHO IMPORT (single chunk)
# ==========================================================
def _zoho_import_chunk(csv_bytes, import_type, access_token):

    url = (
        f"{ZOHO_ANALYTICS['api_domain']}"
        f"/workspaces/{ZOHO_WORKSPACE_ID}"
        f"/views/{ZOHO_VIEW_ID}/data"
    )

    config = {
        "importType": import_type,
        "fileType": "csv",
        "autoIdentify": "true",
        "onError": "setcolumnempty",
    }

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "ZANALYTICS-ORGID": ZOHO_ORG_ID,
    }

    files = {
        "FILE": ("zenduit_master.csv", csv_bytes, "text/csv")
    }

    data = {
        "CONFIG": json.dumps(config)
    }

    r = requests.post(
        url,
        headers=headers,
        data=data,
        files=files,
        timeout=300,
    )

    print(f"[{import_type}] Status: {r.status_code}")

    if r.status_code != 200:
        print(r.text)

    r.raise_for_status()

    return r.json()


# ==========================================================
# ZOHO TRUNCATE + ADD (chunked to stay under import size cap)
# ==========================================================
def zoho_truncate_add(df, access_token):

    header_bytes = len(
        df.iloc[0:0].to_csv(index=False).encode("utf-8")
    )

    full_bytes = len(
        df.to_csv(index=False).encode("utf-8")
    )

    avg_row = max(
        1,
        (full_bytes - header_bytes) // max(1, len(df))
    )

    rows_per_chunk = max(
        1,
        (ZOHO_MAX_BYTES_PER_IMPORT - header_bytes) // avg_row
    )

    total_rows = len(df)

    print(
        f"\nUploading {total_rows:,} rows "
        f"in chunks of {rows_per_chunk:,}"
    )

    for i in range(0, total_rows, rows_per_chunk):

        chunk = df.iloc[i:i + rows_per_chunk]

        csv_bytes = chunk.to_csv(index=False).encode("utf-8")

        import_type = "truncateadd" if i == 0 else "append"

        _zoho_import_chunk(csv_bytes, import_type, access_token)

        print(
            f"✔ Uploaded "
            f"{min(i + len(chunk), total_rows):,}"
            f"/{total_rows:,}"
        )

    print("✅ Zoho Upload Complete")


# ==========================================================
# MAIN
# ==========================================================
def main():

    authenticate()

    df_company = fetch_companies()
    df_device = fetch_devices()
    df_reseller = fetch_resellers()

    # ------------------------------------------------------
    # Guarantee device fields referenced below exist
    # (existing coalesce fields + new finance-report fields)
    # ------------------------------------------------------
    df_device = ensure_columns(
        df_device,
        [
            "Serial", "GlobalstarESN", "SmartwitnessDRID",   # coalesce sources
            "Name", "SurfEdgeSerial", "UpdateDate",          # new report fields
            "BillingTag", "CreationDate",
            "BillingSKU", "BillingSKUId",
            "PromoCode",                                     # new
        ],
        "Device",
    )

    # ------------------------------------------------------
    # SERIAL NUMBER (existing coalesced field — kept as-is)
    # ------------------------------------------------------
    df_device["serial_number"] = (
        df_device.get("Serial")
        .fillna(df_device.get("GlobalstarESN"))
        .fillna(df_device.get("SmartwitnessDRID"))
    )

    # ------------------------------------------------------
    # IS_SUSPENDED BOOLEAN (device-level boolean fields only)
    # Status string is on Company — handled below
    # ------------------------------------------------------
    suspend_cols = [
        "IsSuspend",
        "IsSuspended",
        "DeviceStatus__IsSuspend",
        "DeviceStatus__IsSuspended",
    ]

    existing_cols = [
        c for c in suspend_cols
        if c in df_device.columns
    ]

    if existing_cols:
        df_device["IsSuspend_device"] = (
            df_device[existing_cols]
            .astype(str)
            .replace("nan", None)
            .bfill(axis=1)
            .iloc[:, 0]
        )
    else:
        df_device["IsSuspend_device"] = None

    df_device["IsSuspend_device"] = (
        df_device["IsSuspend_device"]
        .apply(
            lambda x:
            True
            if str(x).lower() in ["true", "1", "yes", "suspended"]
            else False
            if str(x).lower() in ["false", "0", "no", "active"]
            else None
        )
    )

    # ------------------------------------------------------
    # DEVICE COLUMNS
    #   - Existing columns keep their existing names.
    #   - Additional finance-report fields appended below
    #     under their raw API names (only "Name" is
    #     disambiguated -> "Device_Name" to avoid clashing
    #     with the Company / Reseller name columns).
    # ------------------------------------------------------
    df_device_clean = df_device[[
        # ----- existing -----
        "Id",
        "CompanyId",
        "Type",
        "serial_number",
        "DevicePlan",
        "DataPlan",
        "DataUsageStatus",
        "ICCID",
        "SimType",
        "ActivationDate",
        "LastCameraContact",
        "TerminationDate",
        "IsSuspend_device",
        # ----- additional (finance report) -----
        "Name",              # Device Name
        "Serial",            # SM Serial
        "SmartwitnessDRID",  # SW Serial
        "SurfEdgeSerial",    # SS Serial
        "UpdateDate",        # Telematics Communication Date
        "BillingTag",        # Billing Plan (report definition)
        "CreationDate",      # Date Created
        "BillingSKU",        # BillingSKU
        "BillingSKUId",      # BillingSKUID
        "PromoCode",         # Promo code
    ]].copy()

    df_device_clean.rename(
        columns={
            # ----- existing renames (unchanged) -----
            "Id": "Device_Id",
            "Type": "Tracker_type",
            "DevicePlan": "Plan",
            "DataPlan": "Data_Plan",
            "DataUsageStatus": "Data_Usage",
            "ICCID": "SIM",
            "SimType": "SIM_Type",
            "ActivationDate": "Activation_Date",
            "LastCameraContact": "Last_active",
            "TerminationDate": "Termination_date",
            "IsSuspend_device": "Is_Suspended",
            # ----- only disambiguation for the new field -----
            "Name": "Device_Name",
            "BillingTag": "Billing_Plan",
        },
        inplace=True
    )

    # ------------------------------------------------------
    # COMPANY COLUMNS — Status (Active/Inactive) lives here.
    #   Additional: Name (-> Company_Name) and ResellerId
    #   (raw, kept for the reseller join).
    # ------------------------------------------------------
    company_cols = ["Id", "ZohoAccountId"]

    if "Status" in df_company.columns:
        company_cols.append("Status")
    else:
        print("⚠️  'Status' column not found in Company data — check API response")

    has_company_name = "Name" in df_company.columns
    if has_company_name:
        company_cols.append("Name")
    else:
        print("⚠️  'Name' column not found in Company data — Company_Name will be blank")

    has_reseller_id = "ResellerId" in df_company.columns
    if has_reseller_id:
        company_cols.append("ResellerId")
    else:
        print("⚠️  'ResellerId' not found in Company data — Reseller_Name will be blank")

    df_company_clean = df_company[company_cols].copy()

    df_company_clean.rename(
        columns={
            "Id": "CompanyId",
            "ZohoAccountId": "AccountId",
            "Status": "Status",          # keep the name as-is
            "Name": "Company_Name",      # new (Database in the report)
            # ResellerId kept raw
        },
        inplace=True
    )

    if not has_company_name:
        df_company_clean["Company_Name"] = ""

    # ------------------------------------------------------
    # RESELLER COLUMNS
    # ------------------------------------------------------
    df_reseller = ensure_columns(df_reseller, ["Id", "Name"], "Reseller")
    df_reseller_clean = df_reseller[["Id", "Name"]].copy()
    df_reseller_clean.rename(
        columns={
            "Id": "ResellerId",
            "Name": "Reseller_Name",
        },
        inplace=True
    )

    # ------------------------------------------------------
    # LAST ACTIVE (existing logic)
    # ------------------------------------------------------
    df_device_clean["Last_active"] = (
        pd.to_datetime(
            df_device_clean["Last_active"],
            errors="coerce",
            utc=True
        )
    )

    current_time = pd.Timestamp.utcnow()

    df_device_clean["sending_data"] = (
        df_device_clean["Last_active"]
        .apply(
            lambda x:
            "Yes"
            if pd.notnull(x)
               and (current_time - x).days <= 30
            else "No"
        )
    )

    # ------------------------------------------------------
    # MERGE — Company via CompanyId, then Reseller via ResellerId
    # ------------------------------------------------------
    Final_df = df_device_clean.merge(
        df_company_clean,
        on="CompanyId",
        how="left",
        suffixes=("", "_company")
    )

    if has_reseller_id:
        Final_df = Final_df.merge(
            df_reseller_clean,
            on="ResellerId",
            how="left",
            suffixes=("", "_reseller")
        )
    else:
        Final_df["Reseller_Name"] = ""

    print(f"\n🎯 Final Rows: {len(Final_df):,}")

    # ------------------------------------------------------
    # CLEANUP (existing)
    # ------------------------------------------------------
    Final_df["Termination_date"] = (
        pd.to_datetime(
            Final_df["Termination_date"],
            errors="coerce"
        )
        .apply(
            lambda x:
            x.strftime("%Y-%m-%d")
            if pd.notnull(x)
            else ""
        )
    )

    Final_df["Last_active"] = (
        Final_df["Last_active"]
        .dt.tz_localize(None)
    )

    # ------------------------------------------------------
    # SAFETY CHECK
    # ------------------------------------------------------
    if Final_df.empty:
        raise Exception("No data returned.")

    Final_df = Final_df.fillna("")

    # Debug: confirm Status values look correct
    if "Status" in Final_df.columns:
        print(f"\n📊 Status value counts:\n{Final_df['Status'].value_counts()}")

    # ------------------------------------------------------
    # OUTPUT TO EXCEL (local audit copy)
    # ------------------------------------------------------
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    Final_df.to_excel(OUTPUT_FILE, index=False)

    print(f"\n💾 Saved {len(Final_df):,} rows -> {OUTPUT_FILE}")

    # ------------------------------------------------------
    # LOAD -> ZOHO ANALYTICS (truncate + chunked append)
    # ------------------------------------------------------
    access_token = zoho_get_access_token()
    zoho_truncate_add(Final_df, access_token)

    print(f"\n🚀 Uploaded {len(Final_df):,} rows to Zoho Analytics")


if __name__ == "__main__":
    main()
