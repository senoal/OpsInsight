from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, url_for
from pypdf import PdfReader
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "monitoring.db"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-for-production")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

FIELDS = {
    "report_date": ("Tanggal Laporan", ["report date", "tanggal laporan", "period", "periode"]),
    "unit": ("Unit", ["unit", "unit id", "equipment"]),
    "total_hours": ("Total Hours", ["total hours", "hours", "jam operasi", "hour meter"]),
    "fuel_consumption": ("Fuel Consumption", ["fuel consumption", "total fuel", "fuel", "konsumsi bahan bakar"]),
    "productivity": ("Productivity", ["productivity", "produktivitas", "output", "production"]),
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          report_date TEXT NOT NULL,
          unit TEXT NOT NULL,
          total_hours REAL NOT NULL DEFAULT 0,
          fuel_consumption REAL NOT NULL DEFAULT 0,
          productivity REAL NOT NULL DEFAULT 0,
          source_file TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()
    db.close()


init_db()


def extract_value(text: str, labels: list[str]) -> str:
    """Extract the value occurring after a known report label on the same line."""
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[:\-]?\s*([^\n\r|]+)"
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip(" :\t")
            # Avoid treating the label itself as a result from a table header.
            if value and value.lower() not in labels:
                return value[:120]
    return ""


def extract_report(pdf_path: Path) -> dict[str, str]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    values = {key: extract_value(text, aliases) for key, (_, aliases) in FIELDS.items()}
    # Common dates can be inferred if the report lacks an explicit label.
    if not values["report_date"]:
        date_match = re.search(r"\b\d{1,2}[\-/ ](?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|\d{1,2})[\-/ ,]+\d{2,4}\b", text, re.I)
        values["report_date"] = date_match.group(0) if date_match else datetime.now().strftime("%d %b %Y")
    return values


def as_number(value: str) -> float:
    cleaned = re.sub(r"[^0-9.,-]", "", value or "").replace(",", "")
    try:
        return float(cleaned) if cleaned else 0
    except ValueError:
        return 0


@app.route("/")
def dashboard():
    db = get_db()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 5
    query = request.args.get("q", "").strip()
    selected_unit = request.args.get("unit", "").strip()

    conditions = []
    params = []
    if query:
        conditions.append("(unit LIKE ? OR report_date LIKE ? OR source_file LIKE ?)")
        term = f"%{query}%"
        params.extend([term, term, term])
    if selected_unit:
        conditions.append("unit = ?")
        params.append(selected_unit)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    filtered_count = db.execute(f"SELECT COUNT(*) FROM reports{where}", params).fetchone()[0]
    total_pages = max(1, (filtered_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = db.execute(
        f"SELECT * FROM reports{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    totals = db.execute("SELECT COUNT(*) count, COALESCE(SUM(total_hours),0) hours, COALESCE(SUM(fuel_consumption),0) fuel, COALESCE(SUM(productivity),0) productivity FROM reports").fetchone()
    unit_rows = db.execute("SELECT unit, COUNT(*) count FROM reports GROUP BY unit ORDER BY count DESC LIMIT 5").fetchall()
    unit_options = db.execute("SELECT DISTINCT unit FROM reports ORDER BY unit").fetchall()
    trend_source = db.execute(
        "SELECT report_date, unit, total_hours, fuel_consumption, productivity "
        "FROM reports ORDER BY id DESC LIMIT 12"
    ).fetchall()[::-1]
    max_productivity = max((row["productivity"] for row in trend_source), default=0)
    trend_rows = []
    for row in trend_source:
        date_label = row["report_date"]
        for date_format in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                date_label = datetime.strptime(row["report_date"], date_format).strftime("%d %b")
                break
            except ValueError:
                continue
        trend_rows.append({
            "report_date": row["report_date"],
            "date_label": date_label,
            "unit": row["unit"],
            "productivity": row["productivity"],
            "height": max(8, round(row["productivity"] / max_productivity * 72)) if max_productivity else 8,
        })

    productivity_values = [row["productivity"] for row in trend_source]
    latest_productivity = productivity_values[-1] if productivity_values else 0
    average_productivity = sum(productivity_values) / len(productivity_values) if productivity_values else 0
    previous_productivity = productivity_values[-2] if len(productivity_values) > 1 else 0
    productivity_change = (
        (latest_productivity - previous_productivity) / previous_productivity * 100
        if previous_productivity else None
    )
    top_report = max(trend_source, key=lambda row: row["productivity"], default=None)
    total_trend_fuel = sum(row["fuel_consumption"] for row in trend_source)
    fuel_efficiency = sum(productivity_values) / total_trend_fuel if total_trend_fuel else 0
    total_trend_hours = sum(row["total_hours"] for row in trend_source)
    hours_average = total_trend_hours / len(trend_source) if trend_source else 0

    insights = []
    if productivity_change is not None:
        direction = "naik" if productivity_change >= 0 else "turun"
        tone = "positive" if productivity_change >= 0 else "warning"
        action = (
            "Pertahankan ritme produksi dan pastikan kenaikan tetap sejalan dengan konsumsi diesel."
            if productivity_change >= 0
            else "Tinjau downtime, waktu siklus, dan kondisi jalur angkut pada periode terakhir."
        )
        insights.append({
            "tone": tone,
            "icon": "↗" if productivity_change >= 0 else "↘",
            "title": f"Produktivitas {direction} {abs(productivity_change):.1f}%",
            "description": action,
        })
    elif productivity_values:
        insights.append({
            "tone": "neutral",
            "icon": "i",
            "title": "Data tren masih terbatas",
            "description": "Tambahkan minimal satu laporan lagi untuk menghitung perubahan produktivitas.",
        })
    if top_report:
        insights.append({
            "tone": "highlight",
            "icon": "★",
            "title": f"Output tertinggi: {top_report['unit']}",
            "description": f"Mencatat {top_report['productivity']:,.0f} ton pada {top_report['report_date']}.",
        })
    if total_trend_fuel:
        insights.append({
            "tone": "neutral",
            "icon": "◎",
            "title": f"Efisiensi agregat {fuel_efficiency:.2f} ton/liter",
            "description": f"Rata-rata jam operasi adalah {hours_average:,.1f} jam per laporan pada periode yang tersedia.",
        })
    return render_template(
        "dashboard.html",
        rows=rows,
        totals=totals,
        unit_rows=unit_rows,
        unit_options=unit_options,
        trend_rows=trend_rows,
        latest_productivity=latest_productivity,
        average_productivity=average_productivity,
        productivity_change=productivity_change,
        insights=insights,
        page=page,
        total_pages=total_pages,
        filtered_count=filtered_count,
        query=query,
        selected_unit=selected_unit,
    )


def get_report_or_404(report_id: int):
    report = get_db().execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        abort(404)
    return report


@app.route("/reports/<int:report_id>")
def view_report(report_id):
    return render_template("report_detail.html", report=get_report_or_404(report_id))


@app.route("/reports/<int:report_id>/file")
def view_report_file(report_id):
    report = get_report_or_404(report_id)
    filename = report["source_file"]
    if not filename or Path(filename).name != filename or not (UPLOAD_DIR / filename).is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename, mimetype="application/pdf")


@app.route("/reports/<int:report_id>/edit", methods=["GET", "POST"])
def edit_report(report_id):
    report = get_report_or_404(report_id)
    if request.method == "POST":
        db = get_db()
        db.execute(
            """UPDATE reports
               SET report_date = ?, unit = ?, total_hours = ?, fuel_consumption = ?, productivity = ?
               WHERE id = ?""",
            (
                request.form.get("report_date", "").strip() or "-",
                request.form.get("unit", "").strip() or "-",
                as_number(request.form.get("total_hours", "")),
                as_number(request.form.get("fuel_consumption", "")),
                as_number(request.form.get("productivity", "")),
                report_id,
            ),
        )
        db.commit()
        flash("Data laporan berhasil diperbarui.", "success")
        return redirect(url_for("view_report", report_id=report_id))
    return render_template("report_edit.html", report=report)


@app.route("/reports/<int:report_id>/delete", methods=["POST"])
def delete_report(report_id):
    report = get_report_or_404(report_id)
    filename = report["source_file"]
    db = get_db()
    db.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    db.commit()
    if filename and Path(filename).name == filename:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    flash("Laporan dan file sumber berhasil dihapus.", "success")
    return redirect(url_for("dashboard"))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")
    file = request.files.get("report")
    if not file or not file.filename:
        flash("Pilih file PDF terlebih dahulu.", "error")
        return redirect(url_for("upload"))
    if Path(file.filename).suffix.lower() != ".pdf":
        flash("Format file harus PDF.", "error")
        return redirect(url_for("upload"))
    filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    file_path = UPLOAD_DIR / filename
    file.save(file_path)
    try:
        data = extract_report(file_path)
    except Exception as exc:
        file_path.unlink(missing_ok=True)
        flash(f"PDF tidak dapat diekstrak: {exc}", "error")
        return redirect(url_for("upload"))
    return render_template("preview.html", data=data, source_file=filename, original_name=file.filename, fields=FIELDS)


@app.route("/save", methods=["POST"])
def save():
    source_file = request.form.get("source_file", "")
    if not source_file or Path(source_file).name != source_file:
        flash("Sesi upload tidak valid. Silakan unggah ulang.", "error")
        return redirect(url_for("upload"))
    db = get_db()
    db.execute("INSERT INTO reports (report_date, unit, total_hours, fuel_consumption, productivity, source_file) VALUES (?, ?, ?, ?, ?, ?)", (
        request.form.get("report_date", "").strip() or "-",
        request.form.get("unit", "").strip() or "-",
        as_number(request.form.get("total_hours", "")),
        as_number(request.form.get("fuel_consumption", "")),
        as_number(request.form.get("productivity", "")),
        source_file,
    ))
    db.commit()
    flash("Data hasil ekstraksi berhasil disimpan.", "success")
    return redirect(url_for("dashboard"))


@app.route("/cancel", methods=["POST"])
def cancel():
    filename = request.form.get("source_file", "")
    if filename and Path(filename).name == filename:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    flash("Proses dibatalkan. File sementara telah dihapus.", "info")
    return redirect(url_for("upload"))


@app.errorhandler(413)
def file_too_large(_error):
    flash("Ukuran file melebihi batas 16 MB.", "error")
    return redirect(url_for("upload"))


if __name__ == "__main__":
    app.run(debug=True)
