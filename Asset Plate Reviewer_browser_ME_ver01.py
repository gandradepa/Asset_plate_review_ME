import os
import json
import re
import sqlite3
from functools import lru_cache
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, abort

app = Flask(
    __name__,
    template_folder=r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\Git_control\Asset_plate_review_ME\review_asset_templates",
    static_folder=r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\Git_control\Asset_plate_review_ME\review_asset_templates\static"
)

# --- Paths ---
JSON_DIR = r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\QR_code_project\API\Output_jason_api"
IMG_DIR  = r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\QR_code_project\Capture_photos_upload"

# --- SQLite DB ---
DB_PATH = r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\QR_code_project\asset_capture_app\data\QR_codes.db"

# Tabela/colunas
QR_CODES_TABLE = "QR_codes"
QR_CODE_ID_COL = "QR_code_ID"
QR_APPROVED_COL = "Approved"

# Ajuste se o schema dos dropdowns for diferente:
ASSET_GROUP_TABLE = "Asset_Group"
ASSET_GROUP_COL   = "name"

ATTRIBUTE_TABLE   = "Attribute"
ATTRIBUTE_COL     = "Code"

VALID_IMAGE_EXTS = ['.jpg', '.JPG', '.jpeg', '.JPEG', '.png', '.PNG']

# Missed Photo check: falta entre -0, -1, -2 => YES
SEQ_CHECK = ['-0', '-1', '-2']
# Review pode mostrar -3 se existir
SEQ_SHOW  = ['-0', '-1', '-2', '-3']

# Nome de arquivo JSON: "<QR>_ME_<Building>.json"
# groups: (qr, asset_type_mid, building)
JSON_NAME_RE = re.compile(r"^(\d+)_([A-Za-z]+)_(\d+(?:-\d+)?)\.json$")


def find_image(qr: str, building: str, seq_tag: str):
    """Encontra imagem pelo padrão '<QR> <Building> ME - <seq>.<ext>'."""
    seq = seq_tag.replace('-', '').strip()
    base = f"{qr} {building} ME - {seq}"
    for ext in VALID_IMAGE_EXTS:
        candidate = os.path.join(IMG_DIR, base + ext)
        if os.path.exists(candidate):
            return os.path.basename(candidate)
    return None


@lru_cache(maxsize=1)
def _connectable():
    """Cache para verificar existência do DB."""
    return os.path.exists(DB_PATH)


def _fetch_column_values(table: str, col: str):
    """Lista única (ord.) para dropdowns."""
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
    """True se o JSON encode 'ME' no nome do arquivo."""
    m = JSON_NAME_RE.match(filename)
    if not m:
        return False
    _qr, asset_type_mid, _building = m.groups()
    return asset_type_mid.upper() == "ME"


def load_json_items():
    """Carrega SOMENTE itens ME para o dashboard."""
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

            # Garantir chaves
            data.setdefault("Manufacturer", "")
            data.setdefault("Model", "")
            data.setdefault("Serial Number", "")
            data.setdefault("Year", "")
            data.setdefault("UBC Tag", "")
            data.setdefault("Technical Safety BC", "")
            data.setdefault("Asset Group", "")
            data.setdefault("Attribute", "")
            data.setdefault("Flagged", "false")
            data.setdefault("Approved", "")  # blank = False

            # Derivado
            data["Description"] = _compute_description(
                data.get("Asset Group"),
                data.get("UBC Tag")
            )

            # Fotos faltantes (-0, -1, -2)
            missing_tags = [tag for tag in SEQ_CHECK if not find_image(qr, building, tag)]
            missing_photo = len(missing_tags) > 0
            friendly_map = {'-0': 'Asset Plate', '-1': 'UBC Tag', '-2': 'Main Picture'}
            missing_friendly = ", ".join(friendly_map.get(tag, tag) for tag in missing_tags)

            items.append({
                "doc_id": doc_id,
                "qr_code": qr,
                "building": building,
                "asset_type": "ME",  # imposto pelo filtro
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
    # Bloquear abertura manual para não-ME
    m = JSON_NAME_RE.match(f"{doc_id}.json")
    if not m:
        return "Bad ID", 400
    qr, asset_type_mid, building = m.groups()
    if asset_type_mid.upper() != "ME":
        # Não expor que existe: 404
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
    data["Description"] = _compute_description(data.get("Asset Group"), data.get("UBC Tag"))

    # Mapa de imagens
    images = {}
    for tag in SEQ_SHOW:
        filename = find_image(qr, building, tag)
        if filename:
            images[tag] = {"exists": True, "url": url_for('serve_image', filename=filename)}
        else:
            images[tag] = {"exists": False, "url": None}

    # Dropdowns
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
    Grava em SQLite:
      - approved_text = '1' quando aprovado
      - approved_text = '' quando não aprovado
    Usa UPSERT em (QR_code_ID).
    """
    if not _connectable():
        raise RuntimeError("Database file not found.")

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # UPSERT compatível com SQLite 3.24+
        cur.execute(f"""
            INSERT INTO "{QR_CODES_TABLE}" ("{QR_CODE_ID_COL}", "{QR_APPROVED_COL}")
            VALUES (?, ?)
            ON CONFLICT("{QR_CODE_ID_COL}") DO UPDATE SET
                "{QR_APPROVED_COL}" = excluded."{QR_APPROVED_COL}";
        """, (qr_code_id, approved_text))
        conn.commit()


@app.route("/review/<doc_id>", methods=["POST"])
def save_review(doc_id):
    json_path = os.path.join(JSON_DIR, f"{doc_id}.json")
    if not os.path.exists(json_path):
        return "Not found", 404

    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    structured = json_data.get("structured_data", {})
    if not isinstance(structured, dict):
        structured = {}
        json_data["structured_data"] = structured

    # Ensure keys
    structured.setdefault("Asset Group", "")
    structured.setdefault("Attribute", "")
    structured.setdefault("UBC Tag", "")
    structured.setdefault("Approved", "")
    structured.setdefault("Flagged", "false")

    # Flagged
    new_flagged = "true" if request.form.get("Flagged") == "on" else "false"
    if structured.get("Flagged", "false") != new_flagged:
        json_data["modified"] = True
    structured["Flagged"] = new_flagged

    # Campos editáveis (exceto Description/Approved)
    for field in list(structured.keys()):
        if field in ("Flagged", "Description", "Approved"):
            continue
        form_value = request.form.get(field, "")
        if structured.get(field, "") != form_value:
            json_data["modified"] = True
        structured[field] = form_value

    # Novos campos eventuais
    for field, form_value in request.form.items():
        if field in ("Flagged", "action", "Description", "dashboard_query"):
            continue
        if field not in structured:
            structured[field] = form_value
            json_data["modified"] = True

    # Recalcular Description
    structured["Description"] = _compute_description(
        structured.get("Asset Group"),
        structured.get("UBC Tag")
    )

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=4)

    # Navegação Next/Prev (ME-only)
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
        if dash_q.startswith("?"):
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
    if dash_q.startswith("?"):
        return redirect(url_for("index") + dash_q)

    return redirect(url_for("index"))


@app.route("/toggle_approved/<doc_id>", methods=["POST"])
def toggle_approved(doc_id):
    """Alterna Approved no JSON e grava em QR_codes (SQLite):
       - '1' quando aprovado
       - '' quando desmarcado
    """
    json_path = os.path.join(JSON_DIR, f"{doc_id}.json")
    if not os.path.exists(json_path):
        return jsonify({"success": False, "error": "Not found"}), 404

    # Extrair QR do doc_id
    m = JSON_NAME_RE.match(f"{doc_id}.json")
    if not m:
        return jsonify({"success": False, "error": "Bad ID"}), 400
    qr, asset_type_mid, building = m.groups()
    if asset_type_mid.upper() != "ME":
        # Proteção extra: só permite ME
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

        # Atualizar DB: '1' quando aprovado, '' quando não
        db_val = "1" if new_val == "True" else ""
        _db_upsert_qr_approved(qr_code_id=qr, approved_text=db_val)

        return jsonify({"success": True, "new_value": structured["Approved"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMG_DIR, filename)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
