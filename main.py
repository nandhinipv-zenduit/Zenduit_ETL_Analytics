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

OUTPUT_FILE = (
    r"C:\Users\suppo\PyCharmMiscProject\.venv\Billing_audit_engine\OP\zenduit_Master.xlsx"
)

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
# FETCH COMPANIES
# ==========================================================
def fetch_companies():

    print("\n🏢 Fetching Companies...")

    url = f"{BASE_URL}/Company/GetAll"

    res = session.post(url, json={})
    res.raise_for_status()

    data = res.json()

    companies = (
        data if isinstance(data, list)
        else data.get("Data")
        or data.get("data")
        or data.get("items")
        or []
    )

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

    data = res.json()

    devices = (
        data if isinstance(data, list)
        else data.get("Data")
        or data.get("data")
        or data.get("items")
        or []
    )

    df = pd.json_normalize(devices, sep="__")

    print(
        f"✔ Total Devices: {len(df):,} "
        f"| Columns: {len(df.columns):,}"
    )

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
        raise Exception(
            f"Failed getting Zoho token: {token_data}"
        )

    return token_data["access_token"]


# ==========================================================
# ZOHO IMPORT
# ==========================================================
def _zoho_import_chunk(
    csv_bytes,
    import_type,
    access_token
):

    url = (
        f"{ZOHO_ANALYTICS['api_domain']}"
        f"/workspaces/{ZOHO_WORKSPACE_ID}"
        f"/views/{ZOHO_VIEW_ID}/data"
    )

    config = {
        "importType": import_type,
        "fileType": "csv",
        "autoIdentify": "true",
        "onError": "setcolumnempty"
    }

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "ZANALYTICS-ORGID": ZOHO_ORG_ID,
    }

    files = {
        "FILE": (
            "zenduit_master.csv",
            csv_bytes,
            "text/csv"
        )
    }

    data = {
        "CONFIG": json.dumps(config)
    }

    r = requests.post(
        url,
        headers=headers,
        data=data,
        files=files,
        timeout=300
    )

    print(
        f"[{import_type}] Status: "
        f"{r.status_code}"
    )

    if r.status_code != 200:
        print(r.text)

    r.raise_for_status()

    return r.json()


def zoho_truncate_add(
    df,
    access_token
):

    header_bytes = len(
        df.iloc[0:0]
        .to_csv(index=False)
        .encode("utf-8")
    )

    full_bytes = len(
        df.to_csv(index=False)
        .encode("utf-8")
    )

    avg_row = max(
        1,
        (full_bytes - header_bytes)
        // max(1, len(df))
    )

    rows_per_chunk = max(
        1,
        (ZOHO_MAX_BYTES_PER_IMPORT - header_bytes)
        // avg_row
    )

    total_rows = len(df)

    print(
        f"\nUploading {total_rows:,} rows "
        f"in chunks of {rows_per_chunk:,}"
    )

    for i in range(
        0,
        total_rows,
        rows_per_chunk
    ):

        chunk = df.iloc[
            i:i + rows_per_chunk
        ]

        csv_bytes = (
            chunk.to_csv(index=False)
            .encode("utf-8")
        )

        import_type = (
            "truncateadd"
            if i == 0
            else "append"
        )

        _zoho_import_chunk(
            csv_bytes,
            import_type,
            access_token
        )

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

    # ------------------------------------------------------
    # SERIAL NUMBER
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
    # ------------------------------------------------------
    df_device_clean = df_device[[
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
        "IsSuspend_device"
    ]].copy()

    df_device_clean.rename(
        columns={
            "Id": "Device_Id",
            "Type": "Tracker_type",
            "DevicePlan": "Plan",
            "DataPlan": "Billing_Plan",
            "DataUsageStatus": "Data_Usage",
            "ICCID": "SIM",
            "SimType": "SIM_Type",
            "ActivationDate": "Activation_Date",
            "LastCameraContact": "Last_active",
            "TerminationDate": "Termination_date",
            "IsSuspend_device": "Is_Suspended"
        },
        inplace=True
    )

    # ------------------------------------------------------
    # COMPANY COLUMNS — Status (Active/Inactive) lives here
    # ------------------------------------------------------
    company_cols = ["Id", "ZohoAccountId"]

    if "Status" in df_company.columns:
        company_cols.append("Status")
    else:
        print("⚠️  'Status' column not found in Company data — check API response")

    df_company_clean = df_company[company_cols].copy()

    df_company_clean.rename(
        columns={
            "Id": "CompanyId",
            "ZohoAccountId": "AccountId",
            "Status": "Status"          # keep the name as-is
        },
        inplace=True
    )

    # ------------------------------------------------------
    # LAST ACTIVE
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
    # MERGE — Status joins from Company via CompanyId
    # ------------------------------------------------------
    Final_df = df_device_clean.merge(
        df_company_clean,
        on="CompanyId",
        how="left",
        suffixes=("", "_company")
    )

    print(f"\n🎯 Final Rows: {len(Final_df):,}")

    # ------------------------------------------------------
    # CLEANUP
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
        raise Exception(
            "No data returned. "
            "Skipping Zoho upload."
        )

    Final_df = Final_df.fillna("")

    # Debug: confirm Status values look correct before upload
    if "Status" in Final_df.columns:
        print(f"\n📊 Status value counts:\n{Final_df['Status'].value_counts()}")

    # ------------------------------------------------------
    # ZOHO UPLOAD
    # ------------------------------------------------------
    access_token = zoho_get_access_token()

    zoho_truncate_add(Final_df, access_token)
    print(ZOHO_ANALYTICS["refresh_token"])
    print(
        f"\n🚀 Uploaded "
        f"{len(Final_df):,} rows "
        f"to Zoho Analytics"
    )


if __name__ == "__main__":
    main()
