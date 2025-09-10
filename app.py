import os
import uuid
import zipfile
import shutil
import io
import requests
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
from sqlalchemy import create_engine, text
from datetime import datetime, timezone

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Database Configuration ---
db_url = os.environ.get('SUPABASE_DB_URL')
if not db_url:
    raise ValueError("SUPABASE_DB_URL must be set in environment variables.")
engine = create_engine(db_url)

# --- Supabase Client Initialization ---
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- CORS Configuration ---
allowed_origins = [
    "https://artypacks-frontend-prod.onrender.com",
    "https://artypacks.app",
    "https://www.artypacks.app",
    "http://127.0.0.1:5500"
]
CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True, expose_headers=["Content-Disposition"] )

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "License key is required."}), 401

    try:
        with engine.connect() as connection:
            trans = connection.begin()
            try:
                result = connection.execute(text("SELECT * FROM use_one_credit(:p_license_key)"), {'p_license_key': license_key}).fetchone()
                if not result or not result[0]:
                    message = result[1] if result and result[1] else 'Invalid license or no credits remaining.'
                    trans.rollback()
                    return jsonify({"message": message}), 403
                trans.commit()
            except Exception as db_exc:
                trans.rollback()
                raise db_exc
    except Exception as e:
        print(f"CRITICAL ERROR in /convert during credit use: {e}")
        return jsonify({"message": "A server error occurred during license validation."}), 500

    if 'file' not in request.files:
        return jsonify({"message": "No file was uploaded."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"message": "No selected file."}), 400

    temp_dir = os.path.join('temp', str(uuid.uuid4()))
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        if file and file.filename.endswith('.brushset'):
            original_filename = secure_filename(file.filename)
            filepath = os.path.join(temp_dir, original_filename)
            file.save(filepath)
            
            zip_buffer, error = process_brushset(filepath)
            if error:
                try:
                    with engine.connect() as connection:
                        connection.execute(text("UPDATE licenses SET credits_remaining = credits_remaining + 1 WHERE license_key = :key"), {'key': license_key})
                        connection.commit()
                        print(f"INFO: Credit refunded for {license_key} due to conversion failure.")
                except Exception as refund_e:
                    print(f"CRITICAL ERROR: Failed to refund credit for {license_key}. Error: {refund_e}")
                return jsonify({"message": error}), 400

            base_name = os.path.splitext(original_filename)[0]
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            zip_filename_for_storage = f"ArtyPacks.app_{base_name}_{timestamp}.zip"

            supabase.storage.from_("conversions").upload(
                file=zip_buffer.getvalue(),
                path=zip_filename_for_storage,
                file_options={"content-type": "application/zip"}
            )
            
            public_url = supabase.storage.from_("conversions").get_public_url(zip_filename_for_storage)

            with engine.connect() as connection:
                connection.execute(text(
                    "INSERT INTO conversions (license_key, original_filename, download_url) VALUES (:key, :orig_name, :url)"
                ), {'key': license_key, 'orig_name': original_filename, 'url': public_url})
                connection.commit()

            return jsonify({
                "downloadUrl": public_url,
                "originalFilename": original_filename
            })
        else:
            return jsonify({"message": "Invalid file type. Only .brushset files are allowed."}), 400
    except Exception as e:
        print(f"CRITICAL ERROR during file processing or upload: {e}")
        return jsonify({"message": "A critical error occurred while processing the file."}), 500
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- License Check Route ---
@app.route('/check-license', methods=['POST'])
def check_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({"message": "Invalid request: Missing license key."}), 400
    
    license_key = data['licenseKey']
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT * FROM get_license_status(:p_license_key)"), {'p_license_key': license_key}).fetchone()
            if not result:
                return jsonify({"isValid": False, "message": "License key not found."}), 404
            
            response_data = {
                "isValid": result[0],
                "sessions_remaining": result[1],
                "message": result[2],
                "user_type": result[3]
            }
            return jsonify(response_data), 200
    except Exception as e:
        print(f"CRITICAL ERROR in /check-license: {e}")
        return jsonify({"message": "A server error occurred while validating the license."}), 500

# --- Session Recovery Route ---
@app.route('/recover-session', methods=['POST'])
def recover_session():
    data = request.get_json()
    license_key = data.get('licenseKey')
    if not license_key:
        return jsonify({"message": "License key is required."}), 400

    try:
        with engine.connect() as connection:
            query = text("""
                SELECT original_filename, download_url 
                FROM conversions 
                WHERE license_key = :key 
                AND created_at >= NOW() - INTERVAL '60 minutes'
                ORDER BY created_at ASC
            """)
            results = connection.execute(query, {'key': license_key}).fetchall()

            if not results:
                return jsonify({"message": "No recent conversions found for this license."}), 404

            license_type_query = text("SELECT p.credit_count FROM licenses l JOIN products p ON l.product_id = p.id WHERE l.license_key = :key")
            type_result = connection.execute(license_type_query, {'key': license_key}).fetchone()
            is_multi_credit = type_result and type_result[0] > 1

            if is_multi_credit:
                files_data = [{"originalFilename": row[0], "downloadUrl": row[1]} for row in results]
                return jsonify({"session_type": "multi", "files": files_data}), 200
            else:
                return jsonify({"session_type": "single", "original_filename": results[0][0], "download_url": results[0][1]}), 200

    except Exception as e:
        print(f"CRITICAL ERROR in /recover-session: {e}")
        return jsonify({"message": "A server error occurred while recovering the session."}), 500

# --- Download All Route ---
@app.route('/download-all', methods=['POST'])
def download_all():
    data = request.get_json()
    urls = data.get('urls')
    batch_counter = data.get('batchCounter', 1)
    if not urls or not isinstance(urls, list):
        return jsonify({"message": "A list of URLs is required."}), 400

    master_zip_buffer = io.BytesIO()
    with zipfile.ZipFile(master_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as master_zf:
        for url in urls:
            try:
                response = requests.get(url, stream=True)
                response.raise_for_status()
                
                with zipfile.ZipFile(io.BytesIO(response.content)) as individual_zip:
                    for item in individual_zip.infolist():
                        master_zf.writestr(item, individual_zip.read(item.filename))
            except Exception as e:
                print(f"Warning: Could not process file from {url}. Error: {e}")
                continue

    master_zip_buffer.seek(0)
    master_zip_filename = f"ArtyPacks.app_Batch_{batch_counter}.zip"
    return send_file(
        master_zip_buffer,
        as_attachment=True,
        download_name=master_zip_filename,
        mimetype='application/zip'
    )

# --- Helper Functions ---
def process_brushset(filepath):
    temp_extract_dir = os.path.join('temp', f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            image_files = [name for name in brushset_zip.namelist() if name.lower().endswith('.png') and 'artwork.png' not in name.lower()]
            
            if not image_files:
                return None, "No valid stamp images were found in the brushset."

            original_brushset_name = os.path.splitext(os.path.basename(filepath))[0]
            root_folder_name = f"ArtyPacks.app_{original_brushset_name}"

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                for i, image_file_name in enumerate(image_files):
                    with brushset_zip.open(image_file_name) as img_file:
                        img_content = img_file.read()
                        image_filename_in_zip = f"{original_brushset_name}_{i + 1}.png"
                        full_path_in_zip = os.path.join(root_folder_name, image_filename_in_zip)
                        zf.writestr(full_path_in_zip, img_content)
            
            zip_buffer.seek(0)
            return zip_buffer, None
            
    except zipfile.BadZipFile:
        return None, "The provided file seems to be corrupted or isn't a valid .brushset."
    except Exception as e:
        print(f"Error in process_brushset: {e}")
        return None, "An unexpected error occurred while processing the brushset."
    finally:
        # This is the corrected line.
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

# --- Uptime Ping Route ---
@app.route('/ping', methods=['GET'])
def ping():
    """An endpoint for uptime monitoring services to keep the Render instance 'warm'."""
    return jsonify({"status": "alive"}), 200

# --- Root Route ---
@app.route('/')
def index():
    return "ArtyPacks Converter Backend is running."

if __name__ == "__main__":
    app.run(debug=True)
