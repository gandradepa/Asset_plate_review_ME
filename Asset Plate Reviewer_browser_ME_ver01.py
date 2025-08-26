import os
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify

# --- Resolve paths relative to this repository for templates/static ---
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "review_asset_templates"

# Prefer "review_asset_templates/static"; fall back to repo-level "static"; else disable Flask static
CANDIDATE_STATIC = [
    TEMPLATE_DIR / "static",
    BASE_DIR / "static",
]
STATIC_DIR = next((p for p in CANDIDATE_STATIC if p.exists()), None)

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR) if STATIC_DIR else None,  # None if served by Nginx
)

# --- Paths ---
JSON_DIR = r"/home/developer/Output_jason_api"
IMG_DIR  = r"/home/developer/Capture_photos_upload"

# --- SQLite DB ---
DB_PATH = r"/home/developer/asset_capture_app_dev/data/User_control.db"

# Tables/columns
QR_CODES_TABLE   = "QR_codes"
QR_CODE_ID_COL   = "QR_code_ID"
QR_APPROVED_COL  = "Approved"

SDI_TABLE = "sdi_dataset"
# Target column names (intersected with actual schema at runtime)
SDI_TARGET_COLS = [
    "QR Code",
    "Building",
    "Manufacturer",
    "Model",
    "Serial",
    "UBC Tag",
    "Asset Group",
    "Attribute",
    "Description",
    "Diameter",
    "Year",
    "Technical Safety BC",
    "Approved",  # SDI now stores '1' (approved) or '0' (not approved)
]

# Dropdown sources
ASSET_GROUP_TABLE = "Asset_Group"
ASSET_GROUP_COL   = "name"
ATTRIBUTE_TABLE   = "Attribute"
ATTRIBUTE_COL     = "Code"

VALID_IMAGE_EXTS = ['.jpg', '.JPG', '.jpeg', '.JPEG', '.png', '.PNG']

# Missed Photo check: missing among -0, -1, -2 => YES
SEQ_CHECK = ['-0', '-1', '-2']
# Review can show -3 if present
SEQ_SHOW  = ['-0', '-1', '-2', '-3']

# JSON filename pattern: "<QR>_ME_<Building>.json"
JSON_NAME_RE = re.compile(r"^(\d+)_([A-Za-z]+)_(\d+(?:-\d+)?)\.json$")


def find_image(qr: str, building: str, seq_tag: str):
    """Find image by pattern: '<QR> <Building> ME - <seq>.<ext>'."""
    seq = seq_tag.replace('-', '').strip()
    base = f"{qr} {building} ME - {seq}"
    for ext in VALID_IMAGE_EXTS:
        candidate = os.path.join(IMG_DIR, base + ext)
        if os.path.exists(candidate):
            return os.path.basename(candidate)
    return None


@lru_cache(maxsize=1)
def _connectable():
    """Check DB path once (cached)."""
    return os.path.exists(DB_PATH)


def _fetch_column_values(table: str, col: str):
    """Return sorted unique non-empty strings for dropdowns."""
    if not _connectable():
        return []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            query = f'SELECT "{col}" AS val FROM "{table}" WHERE "{col}" IS NOT NULL'
            cur.execute(query)
            vals = [str(r["val"]).strip() for r in cur.fetchall() if str(r["val"]).strip()]
            uniq = sorted(set(vals), key=lambda s: (s.lower(), s))
            return uniq
    except Exception as e:
        print(f"⚠️ DB fetch failed for {table}.{col}: {e}")
        return []


def get_asset_group_options():
    return _fetch_column_values(ASSET_GROUP_TABLE, ASSET_GROUP_COL)


def get_attribute_options():
    return _fetch_column_values(ATTRIBUTE_TABLE, ATTRIBUTE_COL)


def _compute_description(asset_group: str, ubc_tag: str) -> str:
    ag = (asset_group or "").strip()
    ubc = (ubc_tag or "").strip()
    if ag and ubc:
        return f"{ag} - {ubc}"
    return ag or ubc


def _is_me_filename(filename: str) -> bool:
    """True if the JSON encodes 'ME' in the filename."""
    m = JSON_NAME_RE.match(filename)
    if not m:
        return False
    _qr, asset_type_mid, _building = m.groups()
    return asset_type_mid.upper() == "ME"


def load_json_items():
    """Load ME-only items for the dashboard."""
    items = []
    for filename in os.listdir(JSON_DIR):
        if not filename.endswith(".json") or filename.endswith("_raw_ocr.json"):
            continue
        if not _is_me_filename(filename):
            continue

        m = JSON_NAME_RE.match(filename)
        if not m:
            continue

        qr, asset_type_mid, building = m.groups()
        doc_id = filename[:-5]  # strip ".json"

        try:
            with open(os.path.join(JSON_DIR, filename), 'r', encoding='utf-8') as f:
                raw = json.load(f)

            data = raw.get("structured_data") or {}
            if not isinstance(data, dict):
                print(f"⚠️ Skipped {filename}: 'structured_data' is not a dict")
                continue

            # Ensure keys
            data.setdefault("Manufacturer", "")
            data.setdefault("Model", "")
            data.setdefault("Serial Number", "")
            data.setdefault("Year", "")
            data.setdefault("UBC Tag", "")
            data.setdefault("Technical Safety BC", "")
            data.setdefault("Asset Group", "")
            data.setdefault("Attribute", "")
            data.setdefault("Diameter", "")
            data.setdefault("Flagged", "false")
            data.setdefault("Approved", "")  # blank = False

            # Derived
            data["Description"] = _compute_description(
                data.get("Asset Group"),
                data.get("UBC Tag")
            )

            # Missing photos (-0, -1, -2)
            missing_tags = [tag for tag in SEQ_CHECK if not find_image(qr, building, tag)]
            missing_photo = len(missing_tags) > 0
            friendly_map = {'-0': 'Asset Plate', '-1': 'UBC Tag', '-2': 'Main Picture'}
            missing_friendly = ", ".join(friendly_map.get(tag, tag) for tag in missing_tags)

            items.append({
                "doc_id": doc_id,
                "qr_code": qr,
                "building": building,
                "asset_type": "ME",  # enforced by filter
                "Flagged": data.get("Flagged", "false"),
                "Approved": data.get("Approved", ""),
                "Modified": raw.get("modified", False),
                "Missed Photo": "YES" if missing_photo else "NO",
                "Missing List": missing_friendly,
                "Photos Summary": f"{3 - len(missing_tags)}/3",
                **data
            })
        except Exception as e:
            print(f"❌ Error loading {filename}: {e}")
    return items


@app.route("/")
def index():
    flagged_filter = request.args.get("flagged")
    modified_filter = request.args.get("modified")
    missed_filter = request.args.get("missed")

    all_data = load_json_items()  # ME-only

    count_flagged = sum(1 for item in all_data if item.get("Flagged") == "true")
    count_modified = sum(1 for item in all_data if item.get("Modified"))
    count_missed = sum(1 for item in all_data if item.get("Missed Photo") == "YES")

    data = all_data
    if flagged_filter == "true" and modified_filter == "true":
        data = [item for item in data if item.get("Flagged") == "true" and item.get("Modified")]
    elif flagged_filter == "true":
        data = [item for item in data if item.get("Flagged") == "true"]
    elif modified_filter == "true":
        data = [item for item in data if item.get("Modified")]

    if missed_filter == "true":
        data = [item for item in data if item.get("Missed Photo") == "YES"]

    return render_template(
        "dashboard.html",
        title="Asset Review Dashboard - Mechanical",
        data=data,
        warn_missing=True,
        flagged_filter=flagged_filter,
        modified_filter=modified_filter,
        missed_filter=missed_filter,
        count_flagged=count_flagged,
        count_modified=count_modified,
        count_missed=count_missed
    )


@app.route("/review/<doc_id>")
def review(doc_id):
    # Block manual open for non-ME
    m = JSON_NAME_RE.match(f"{doc_id}.json")
    if not m:
        return "Bad ID", 400
    qr, asset_type_mid, building = m.groups()
    if asset_type_mid.upper() != "ME":
        return "Not found", 404

    json_path = os.path.join(JSON_DIR, f"{doc_id}.json")
    if not os.path.exists(json_path):
        return "Not found", 404

    with open(json_path, 'r', encoding='utf-8') as f:
        loaded = json.load(f)

    data = loaded.get("structured_data", {}) or {}
    data.setdefault("Asset Group", "")
    data.setdefault("Attribute", "")
    data.setdefault("UBC Tag", "")
    data.setdefault("Approved", "")
    data.setdefault("Diameter", "")
    data["Description"] = _compute_description(data.get("Asset Group"), data.get("UBC Tag"))

    # Images map
    images = {}
    for tag in SEQ_SHOW:
        filename = find_image(qr, building, tag)
        if filename:
            images[tag] = {"exists": True, "url": url_for('serve_image', filename=filename)}
        else:
            images[tag] = {"exists": False, "url": None}

    # Dropdown options
    asset_group_options = get_asset_group_options()
    attribute_options   = get_attribute_options()

    return render_template(
        "review.html",
        title="Asset Review - Mechanical",
        doc_id=doc_id,
        qr_code=qr,
        building=building,
        asset_type="ME",
        data=data,
        images=images,
        asset_group_options=asset_group_options,
        attribute_options=attribute_options
    )


def _db_upsert_qr_approved(qr_code_id: str, approved_text: str):
    """
    Write into QR_codes:
      - approved_text = '1' when approved
      - approved_text = '' when not approved  (leave as-is unless you want '0' here too)
    """
    if not _connectable():
        print("⚠️ Database file not found; skipping QR_codes upsert.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO "{QR_CODES_TABLE}" ("{QR_CODE_ID_COL}", "{QR_APPROVED_COL}")
            VALUES (?, ?)
            ON CONFLICT("{QR_CODE_ID_COL}") DO UPDATE SET
                "{QR_APPROVED_COL}" = excluded."{QR_APPROVED_COL}";
        """, (qr_code_id, approved_text))
        conn.commit()


def _quote(name: str) -> str:
    return f'"{name}"'.replace('""', '"')  # minimal safety


def _db_get_columns(conn, table: str):
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info({_quote(table)})')
    return {row[1] for row in cur.fetchall()}  # row[1] = column name


def _db_upsert_row(conn, table: str, key_cols: list[str], row: dict):
    """
    Schema-aware upsert:
      - Intersects requested columns with actual table columns
      - UPDATE by key; if 0 rows, INSERT
    """
    existing_cols = _db_get_columns(conn, table)
    if not existing_cols:
        raise RuntimeError(f'Table "{table}" not found or has no columns.')

    # Filter to existing columns only
    filtered = {k: (row.get(k, "") or "") for k in row.keys() if k in existing_cols}

    # Ensure key columns are present (and exist in table)
    for key in key_cols:
        if key not in filtered:
            if key in existing_cols:
                filtered[key] = ""
            else:
                raise RuntimeError(f'Key column "{key}" not found in table "{table}".')

    # Build UPDATE (set all non-key columns that exist)
    set_cols = [c for c in filtered.keys() if c not in key_cols]
    cur = conn.cursor()
    if set_cols:
        set_clause = ", ".join(f'{_quote(c)} = ?' for c in set_cols)
        where_clause = " AND ".join(f'{_quote(k)} = ?' for k in key_cols)
        sql_upd = f'UPDATE {_quote(table)} SET {set_clause} WHERE {where_clause}'
        params_upd = [filtered[c] for c in set_cols] + [filtered[k] for k in key_cols]
        cur.execute(sql_upd, params_upd)
        updated = cur.rowcount
    else:
        updated = 0

    # If no row updated, INSERT the available columns
    if updated == 0:
        cols = list(filtered.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql_ins = f'INSERT INTO {_quote(table)} ({", ".join(_quote(c) for c in cols)}) VALUES ({placeholders})'
        params_ins = [filtered[c] for c in cols]
        cur.execute(sql_ins, params_ins)


def _db_upsert_sdi_dataset(qr: str, building: str, structured: dict):
    """
    Upsert into sdi_dataset (match by "QR Code" + "Building").
    Missing fields => blank string.
    Uses only columns that actually exist in the table.
    Approved => '1' when True, '0' otherwise.
    """
    if not _connectable():
        print("⚠️ Database file not found; skipping sdi_dataset upsert.")
        return

    # Convert structured["Approved"] -> '1' or '0'
    approved_flag = "1" if (structured.get("Approved", "") == "True") else "0"

    # Build desired row dict
    row = {
        "QR Code": qr or "",
        "Building": building or "",
        "Manufacturer": str(structured.get("Manufacturer", "") or ""),
        "Model": str(structured.get("Model", "") or ""),
        "Serial": str(structured.get("Serial Number", "") or ""),  # mapping
        "UBC Tag": str(structured.get("UBC Tag", "") or ""),
        "Asset Group": str(structured.get("Asset Group", "") or ""),
        "Attribute": str(structured.get("Attribute", "") or ""),
        "Description": str(_compute_description(structured.get("Asset Group", ""), structured.get("UBC Tag", "")) or ""),
        "Diameter": str(structured.get("Diameter", "") or ""),
        "Year": str(structured.get("Year", "") or ""),
        "Technical Safety BC": str(structured.get("Technical Safety BC", "") or ""),
        "Approved": approved_flag,  # now 1/0
    }

    with sqlite3.connect(DB_PATH) as conn:
        _db_upsert_row(conn, SDI_TABLE, key_cols=["QR Code", "Building"], row=row)
        conn.commit()


@app.route("/review/<doc_id>", methods=["POST"])
def save_review(doc_id):
    json_path = os.path.join(JSON_DIR, f"{doc_id}.json")
    if not os.path.exists(json_path):
        return "Not found", 404

    # parse qr/building for SDI upsert
    m = JSON_NAME_RE.match(f"{doc_id}.json")
    if not m:
        return "Bad ID", 400
    qr, asset_type_mid, building = m.groups()

    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    structured = json_data.get("structured_data", {})
    if not isinstance(structured, dict):
        structured = {}
        json_data["structured_data"] = structured

    # Ensure keys (and keep blanks if missing)
    structured.setdefault("Manufacturer", "")
    structured.setdefault("Model", "")
    structured.setdefault("Serial Number", "")
    structured.setdefault("Year", "")
    structured.setdefault("UBC Tag", "")
    structured.setdefault("Technical Safety BC", "")
    structured.setdefault("Asset Group", "")
    structured.setdefault("Attribute", "")
    structured.setdefault("Diameter", "")
    structured.setdefault("Approved", "")
    structured.setdefault("Flagged", "false")

    # Flagged
    new_flagged = "true" if request.form.get("Flagged") == "on" else "false"
    if structured.get("Flagged", "false") != new_flagged:
        json_data["modified"] = True
    structured["Flagged"] = new_flagged

    # Editable fields (skip Description/Approved)
    for field in list(structured.keys()):
        if field in ("Flagged", "Description", "Approved"):
            continue
        form_value = request.form.get(field, "")
        if structured.get(field, "") != form_value:
            json_data["modified"] = True
        structured[field] = form_value

    # Capture any brand-new fields
    for field, form_value in request.form.items():
        if field in ("Flagged", "action", "Description", "dashboard_query"):
            continue
        if field not in structured:
            structured[field] = form_value
            json_data["modified"] = True

    # Recompute Description
    structured["Description"] = _compute_description(
        structured.get("Asset Group"),
        structured.get("UBC Tag")
    )

    # Persist JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=4)

    # --- SDI upsert every save (will write Approved as 1/0) ---
    try:
        _db_upsert_sdi_dataset(qr=qr, building=building, structured=structured)
    except Exception as e:
        print(f"⚠️ sdi_dataset upsert failed: {e}")

    # Next/Prev navigation (ME-only)
    all_files = sorted(
        f for f in os.listdir(JSON_DIR)
        if f.endswith(".json")
        and not f.endswith("_raw_ocr.json")
        and JSON_NAME_RE.match(f)
        and _is_me_filename(f)
    )
    current_name = f"{doc_id}.json"
    try:
        current_index = all_files.index(current_name)
    except ValueError:
        dash_q = request.form.get("dashboard_query", "")
        if (dash_q or "").startswith("?"):
            return redirect(url_for("index") + dash_q)
        return redirect(url_for("index"))

    action = request.form.get("action")
    if action == "save_next" and current_index + 1 < len(all_files):
        next_doc = all_files[current_index + 1][:-5]
        return redirect(url_for("review", doc_id=next_doc))
    elif action == "save_prev" and current_index > 0:
        prev_doc = all_files[current_index - 1][:-5]
        return redirect(url_for("review", doc_id=prev_doc))

    dash_q = request.form.get("dashboard_query", "")
    if (dash_q or "").startswith("?"):
        return redirect(url_for("index") + dash_q)

    return redirect(url_for("index"))


@app.route("/toggle_approved/<doc_id>", methods=["POST"])
def toggle_approved(doc_id):
    """Toggle Approved in JSON and update QR_codes; also refresh sdi_dataset row with 1/0."""
    json_path = os.path.join(JSON_DIR, f"{doc_id}.json")
    if not os.path.exists(json_path):
        return jsonify({"success": False, "error": "Not found"}), 404

    # Parse QR/building
    m = JSON_NAME_RE.match(f"{doc_id}.json")
    if not m:
        return jsonify({"success": False, "error": "Bad ID"}), 400
    qr, asset_type_mid, building = m.groups()
    if asset_type_mid.upper() != "ME":
        return jsonify({"success": False, "error": "Not allowed"}), 403

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        structured = json_data.get("structured_data", {})
        if not isinstance(structured, dict):
            structured = {}
            json_data["structured_data"] = structured

        current = structured.get("Approved", "")
        new_val = "True" if current == "" else ""
        structured["Approved"] = new_val
        json_data["structured_data"] = structured

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=4)

        # Update QR_codes (1 / '')
        db_val = "1" if new_val == "True" else ""
        _db_upsert_qr_approved(qr_code_id=qr, approved_text=db_val)

        # Ensure sdi_dataset row exists/up-to-date (Approved as 1/0)
        try:
            _db_upsert_sdi_dataset(qr=qr, building=building, structured=structured)
        except Exception as e:
            print(f"⚠️ sdi_dataset upsert (from toggle) failed: {e}")

        return jsonify({"success": True, "new_value": structured["Approved"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMG_DIR, filename)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5002, debug=True)
