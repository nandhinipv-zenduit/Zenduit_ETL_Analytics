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

ZOHO_MAX_BYTES_PER_IMPORT = 14 * 1024 * 1024

# ==========================================================
# GROUP RESOLUTION MODE
#
# A device record's GroupsIds holds only the groups DIRECTLY
# assigned to it. The admin portal's "Groups: N Selected" label
# counts those PLUS every descendant group (via each group's
# "Childrens" array).
#
# Worked example — device 12W7088:
#   GroupsIds (direct)                    -> 312
#   + descendants via Childrens           -> 980  ("980 Selected")
#
# False -> Group_Names lists the 312 directly-assigned groups
#          (recommended: matches what the device record stores)
# True  -> Group_Names lists all 980, matching the portal label
# ==========================================================
EXPAND_GROUP_DESCENDANTS = False


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
# API POST HELPER
# Bearer header is the primary auth. A few endpoints in this
# service are only reachable with the token as a query param
# (that's how the admin portal calls them), so on 401/403 we
# retry once with ?token=<token>.
# ==========================================================
def api_post(path, payload=None, timeout=900):
    url = f"{BASE_URL}{path}"
    res = session.post(url, json=payload or {}, timeout=timeout)
    if res.status_code in (401, 403):
        res = session.post(
            url,
            params={"token": token},
            json=payload or {},
            timeout=timeout,
        )
    res.raise_for_status()
    return res.json()


# ==========================================================
# GENERIC LIST EXTRACTOR (same shape used by all GetAll calls)
# ==========================================================
def _extract_list(data):
    return (
        data if isinstance(data, list)
        else data.get("Data")
        or data.get("data")
        or data.get("items")
        or data.get("Devices")
        or data.get("Groups")
        or []
    )


# ==========================================================
# HELPER: treat "" / whitespace / "None" as missing so that
# fillna()-style coalescing actually works. The Zenduit API
# returns "" (not null) for unset serial fields, which was
# silently defeating the old .fillna() chain.
# ==========================================================
def blank_to_na(s):
    return (
        s.astype("object")
        .where(s.notna(), None)
        .apply(
            lambda v:
            None
            if v is None
               or (isinstance(v, str) and v.strip() in ("", "None", "null"))
            else v
        )
    )


def coalesce(df, cols):
    """First non-blank value across cols, row by row."""
    out = pd.Series([None] * len(df), index=df.index, dtype="object")
    for c in cols:
        if c not in df.columns:
            continue
        vals = blank_to_na(df[c])
        out = out.where(out.notna(), vals)
    return out


# ==========================================================
# FETCH COMPANIES
# ==========================================================
def fetch_companies():
    print("\n🏢 Fetching Companies...")
    companies = _extract_list(api_post("/Company/GetAll", {}))
    df = pd.json_normalize(companies)
    print(f"✔ Total Companies: {len(df):,}")
    return df


# ==========================================================
# FETCH DEVICES
# ==========================================================
def fetch_devices():
    print("\n📡 Fetching Devices...")
    devices = _extract_list(api_post("/Device/GetAll", {}))
    df = pd.json_normalize(devices, sep="__")
    print(
        f"✔ Total Devices: {len(df):,} "
        f"| Columns: {len(df.columns):,}"
    )
    return df


# ==========================================================
# FETCH RESELLERS
# ==========================================================
def fetch_resellers():
    print("\n🏷️  Fetching Resellers...")
    try:
        resellers = _extract_list(api_post("/Reseller/GetAll", {}))
        df = pd.json_normalize(resellers)
        print(f"✔ Total Resellers: {len(df):,}")
        return df
    except Exception as e:
        print(f"⚠️  Could not fetch resellers ({e}) — Reseller_Name will be blank")
        return pd.DataFrame(columns=["Id", "Name"])


# ==========================================================
# FETCH GROUPS
#
# IMPORTANT: there is NO /Group/GetAll endpoint on this
# service (it 404s — which is why Group_Name came back blank).
# The real endpoint is:
#
#     POST /User/GetGroups   body: {}                -> ALL groups
#     POST /User/GetGroups   body: {"CompanyId": ..} -> one company
#
# and it responds {"Groups": [{Id, Name, CompanyId, ...}],
# "UsedGroupIds": [...]}.
#
# The unfiltered call returns ~99k groups (~26 MB) and takes
# 40-60s, so give it a generous timeout. If it fails we fall
# back to looping per company.
# ==========================================================
def fetch_groups(company_ids=None):
    print("\n🗂️  Fetching Groups (POST /User/GetGroups)...")
    try:
        data = api_post("/User/GetGroups", {}, timeout=900)
        groups = data.get("Groups", []) if isinstance(data, dict) else data
        df = pd.json_normalize(groups)
        if len(df):
            print(f"✔ Total Groups: {len(df):,}")
            return df
        print("⚠️  Global group fetch returned 0 rows — falling back to per-company")
    except Exception as e:
        print(f"⚠️  Global group fetch failed ({e}) — falling back to per-company")

    # ---- per-company fallback ----
    if not company_ids:
        print("⚠️  No company ids available — Group_Names will be blank")
        return pd.DataFrame(columns=["Id", "Name", "CompanyId"])

    frames = []
    for i, cid in enumerate(company_ids, 1):
        try:
            data = api_post(
                "/User/GetGroups",
                {"CompanyId": cid},
                timeout=180,
            )
            groups = data.get("Groups", []) if isinstance(data, dict) else data
            if groups:
                frames.append(pd.json_normalize(groups))
        except Exception as e:
            print(f"   ⚠️  groups failed for company {cid}: {e}")
        if i % 100 == 0:
            print(f"   …{i:,}/{len(company_ids):,} companies")

    df = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["Id", "Name", "CompanyId"])
    )
    df = df.drop_duplicates(subset=["Id"]) if "Id" in df.columns else df
    print(f"✔ Total Groups: {len(df):,}")
    return df


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

    company_ids = (
        df_company["Id"].dropna().astype(str).unique().tolist()
        if "Id" in df_company.columns else []
    )
    df_group = fetch_groups(company_ids)

    # ------------------------------------------------------
    # Guarantee device fields referenced below exist
    #   BillingTag           -> Billing Plan
    #   BillingSKU           -> Billing SKU   (was HWSKU — that
    #                           field does not exist on
    #                           /Device/GetAll, so BillingSKU
    #                           was importing blank)
    #   GeotabCustomDevice   -> Third Party Telematics Serial
    #                           (the field shown as "Third Party
    #                           Serial" in the admin portal grid;
    #                           was never fetched at all)
    # ------------------------------------------------------
    df_device = ensure_columns(
        df_device,
        [
            # coalesce sources
            "Serial", "GlobalstarESN", "SmartwitnessDRID",
            "SurfEdgeSerial", "GeotabCustomDevice",
            # report fields
            "Name", "UpdateDate",
            "BillingTag", "CreationDate",
            "BillingSKU", "BillingSKUId", "Type",
            "Odometer", "EngineHours", "GroupsIds",
            "ICCID", "SimType", "DevicePlan", "DataPlan",
            "DataUsageStatus", "ActivationDate",
            "LastCameraContact", "TerminationDate",
        ],
        "Device",
    )

    # ------------------------------------------------------
    # GROUP NAMES (resolve the GroupsIds list -> names)
    # A device carries a LIST of group ids (GroupsIds). Build an
    # id -> name map from /User/GetGroups and collapse each
    # device's list into one comma-separated Group_Names cell
    # (keeps one row per device). Group_Count is added so you can
    # spot devices whose name list got very long.
    # ------------------------------------------------------
    df_group = ensure_columns(df_group, ["Id", "Name"], "Group")
    group_map = {
        str(gid): gname
        for gid, gname in zip(df_group["Id"], df_group["Name"])
        if pd.notna(gid)
    }
    print(f"🗺️  Group id -> name map size: {len(group_map):,}")

    # parent -> children, used only when EXPAND_GROUP_DESCENDANTS
    child_map = {}
    if EXPAND_GROUP_DESCENDANTS and "Childrens" in df_group.columns:
        for gid, kids in zip(df_group["Id"], df_group["Childrens"]):
            if pd.notna(gid) and isinstance(kids, (list, tuple)):
                child_map[str(gid)] = [str(k) for k in kids]
        print(f"🌳 Group parents with children: {len(child_map):,}")
    elif EXPAND_GROUP_DESCENDANTS:
        print("⚠️  EXPAND_GROUP_DESCENDANTS is on but no 'Childrens' field found")

    def _expand(ids):
        seen, stack = set(), [str(i) for i in ids]
        while stack:
            gid = stack.pop()
            if gid in seen:
                continue
            seen.add(gid)
            for kid in child_map.get(gid, ()):
                if kid not in seen:
                    stack.append(kid)
        return seen

    def _resolve_group_ids(ids):
        if not isinstance(ids, (list, tuple)) or not ids:
            return []
        return sorted(_expand(ids)) if EXPAND_GROUP_DESCENDANTS \
            else [str(i) for i in ids]

    resolved_ids = df_device["GroupsIds"].apply(_resolve_group_ids)

    df_device["Group_Names"] = resolved_ids.apply(
        lambda ids: ", ".join(
            str(group_map[i]) for i in ids
            if i in group_map and pd.notna(group_map[i])
        )
    )
    df_device["Group_Count"] = resolved_ids.apply(len)

    matched = (df_device["Group_Names"].astype(str).str.len() > 0).sum()
    has_groups = (df_device["Group_Count"] > 0).sum()
    print(
        f"🗂️  Devices with group ids: {has_groups:,} "
        f"| resolved to names: {matched:,}"
    )
    if has_groups and not matched:
        print(
            "⚠️  Group ids present but none matched the group map — "
            "check that /User/GetGroups returned data."
        )

    # ------------------------------------------------------
    # THIRD PARTY TELEMATICS SERIAL
    # The admin portal labels GeotabCustomDevice as "Third Party
    # Telematics Serial" (device form) / "Third Party Serial"
    # (device grid). e.g. 12W7088 -> G85F20F0D954
    # ------------------------------------------------------
    df_device["Third_Party_Serial"] = blank_to_na(
        df_device["GeotabCustomDevice"]
    )

    # ------------------------------------------------------
    # SERIAL NUMBER
    # The API returns "" (empty string), not null, for unset
    # serials — so the old .fillna() chain never fell through and
    # devices like 12W7088 (Serial="" but SmartwitnessDRID set)
    # ended up with a blank serial_number. coalesce() below
    # treats "" as missing and also falls back to the third-party
    # telematics serial.
    # ------------------------------------------------------
    df_device["serial_number"] = coalesce(
        df_device,
        [
            "Serial",
            "GlobalstarESN",
            "SmartwitnessDRID",
            "SurfEdgeSerial",
            "GeotabCustomDevice",
        ],
    )

    blank_serials = df_device["serial_number"].isna().sum()
    print(
        f"🔢 serial_number resolved for "
        f"{len(df_device) - blank_serials:,}/{len(df_device):,} devices "
        f"({blank_serials:,} still blank)"
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
    existing_cols = [c for c in suspend_cols if c in df_device.columns]

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
    # DEVICE STATUS
    # Confirmed against /Device/GetAll: DeviceStatus is a PLAIN
    # STRING ("Active" / "Suspended" / "Terminated"), not a
    # nested object — so no DeviceStatus__* keys are produced by
    # json_normalize. The nested candidates are kept purely as a
    # safety net in case the API shape changes.
    # ------------------------------------------------------
    devicestatus_cols = [
        "DeviceStatus",
        "DeviceStatus__Status",
        "DeviceStatus__StatusName",
        "DeviceStatus__Name",
        "DeviceStatus__Description",
    ]
    existing_status_cols = [
        c for c in devicestatus_cols if c in df_device.columns
    ]

    if existing_status_cols:
        df_device["Device_Status"] = coalesce(df_device, existing_status_cols)
    else:
        df_device["Device_Status"] = None
        print(
            "⚠️  DeviceStatus field not found under any expected name "
            f"{devicestatus_cols} — Device_Status will be blank."
        )

    # ------------------------------------------------------
    # DEVICE COLUMNS
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
        "Device_Status",
        # ----- additional (finance report) -----
        "Name",                 # Device Name
        "Serial",               # SM Serial
        "SmartwitnessDRID",     # SW Serial
        "SurfEdgeSerial",       # SS Serial
        "Third_Party_Serial",   # NEW — Third Party Telematics Serial
        "UpdateDate",           # Telematics Communication Date
        "BillingTag",           # Billing Plan
        "CreationDate",         # Date Created
        "BillingSKU",           # Billing SKU (was HWSKU)
        "BillingSKUId",         # BillingSKUID / Promo code
        # ----- groups / usage -----
        "Odometer",
        "EngineHours",
        "Group_Names",
        "Group_Count",
    ]].copy()

    df_device_clean.rename(
        columns={
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
            "Name": "Device_Name",
            "BillingTag": "Billing_Plan",
            "BillingSKU": "BillingSKU",
            "EngineHours": "Engine_Hours",
        },
        inplace=True
    )

    # ------------------------------------------------------
    # COMPANY COLUMNS
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
            "Status": "Status",
            "Name": "Company_Name",
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
    # LAST ACTIVE
    # ------------------------------------------------------
    df_device_clean["Last_active"] = pd.to_datetime(
        df_device_clean["Last_active"],
        errors="coerce",
        utc=True
    )

    current_time = pd.Timestamp.utcnow()
    df_device_clean["sending_data"] = (
        df_device_clean["Last_active"]
        .apply(
            lambda x:
            "Yes"
            if pd.notnull(x) and (current_time - x).days <= 30
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
    # CLEANUP
    # ------------------------------------------------------
    Final_df["Termination_date"] = (
        pd.to_datetime(Final_df["Termination_date"], errors="coerce")
        .apply(lambda x: x.strftime("%Y-%m-%d") if pd.notnull(x) else "")
    )

    Final_df["Last_active"] = Final_df["Last_active"].dt.tz_localize(None)

    # ------------------------------------------------------
    # SAFETY CHECK
    # ------------------------------------------------------
    if Final_df.empty:
        raise Exception("No data returned.")

    Final_df = Final_df.fillna("")

    # ---- diagnostics ----
    if "Status" in Final_df.columns:
        print(f"\n📊 Status value counts:\n{Final_df['Status'].value_counts()}")

    if "Device_Status" in Final_df.columns:
        print(f"\n📊 Device_Status value counts:\n{Final_df['Device_Status'].value_counts()}")

    tps_filled = (Final_df["Third_Party_Serial"].astype(str).str.len() > 0).sum()
    print(f"\n📊 Third_Party_Serial populated: {tps_filled:,}/{len(Final_df):,}")

    sku_filled = (Final_df["BillingSKU"].astype(str).str.len() > 0).sum()
    print(f"📊 BillingSKU populated: {sku_filled:,}/{len(Final_df):,}")

    gn_len = Final_df["Group_Names"].astype(str).str.len()
    print(
        f"📊 Group_Names populated: {(gn_len > 0).sum():,}/{len(Final_df):,} "
        f"| longest cell: {gn_len.max():,} chars"
    )
    if gn_len.max() > 15000:
        print(
            "⚠️  Some Group_Names cells are very long — if Zoho rejects rows, "
            "consider truncating or moving groups to a separate device↔group table."
        )

    # ---- spot check ----
    spot = Final_df[Final_df["Device_Name"].astype(str).str.strip() == "12W7088"]
    if len(spot):
        r = spot.iloc[0]
        print(
            "\n🔍 Spot check 12W7088 → "
            f"serial_number={r['serial_number']!r}, "
            f"Third_Party_Serial={r['Third_Party_Serial']!r}, "
            f"Group_Count={r['Group_Count']}, "
            f"Group_Names[:80]={str(r['Group_Names'])[:80]!r}"
        )

    # ------------------------------------------------------
    # LOAD -> ZOHO ANALYTICS (truncate + chunked append)
    # ------------------------------------------------------
    access_token = zoho_get_access_token()
    zoho_truncate_add(Final_df, access_token)

    print(f"\n🚀 Uploaded {len(Final_df):,} rows to Zoho Analytics")


if __name__ == "__main__":
    main()
