from flask import Flask, render_template, request, redirect, url_for
from pymongo import MongoClient
from dotenv import load_dotenv
from bson.objectid import ObjectId
from datetime import datetime
import os

load_dotenv()

app = Flask(__name__)
client = MongoClient(os.getenv("MONGO_URI"))
db = client["traffic_db"]

vehicles_col = db["vehicles"]
violations_col = db["violations"]
logs_col = db["violation_logs"]  # LAB 6 - Trigger logs

FINE_CHART = {
    "Speeding": 1000,
    "Signal Jump": 500,
    "Wrong Parking": 300,
    "No Helmet": 200
}

# ─── LAB 6: TRIGGER FUNCTION ─────────────────────────────────────────────────
def trigger_log(action, plate_number, violation_type=None, fine_amount=None):
    """Simulates SQL Trigger — auto logs every violation event"""
    logs_col.insert_one({
        "action": action,
        "plate_number": plate_number,
        "violation_type": violation_type,
        "fine_amount": fine_amount,
        "timestamp": datetime.now()
    })

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    # LAB 6: Aggregation Pipeline (Subquery equivalent)
    pipeline = [
        {
            "$group": {
                "_id": "$status",
                "count": {"$sum": 1},
                "total_amount": {"$sum": "$fine_amount"}
            }
        }
    ]
    stats = {s["_id"]: s for s in violations_col.aggregate(pipeline)}

    total_vehicles = vehicles_col.count_documents({})
    total_violations = violations_col.count_documents({})
    pending_fines = stats.get("Pending", {}).get("count", 0)
    collected = stats.get("Paid", {}).get("total_amount", 0)

    # LAB 3: $lookup JOIN — get owner info with each violation
    recent = list(violations_col.aggregate([
        {"$sort": {"date": -1}},
        {"$limit": 5},
        {"$lookup": {
            "from": "vehicles",
            "localField": "plate_number",
            "foreignField": "plate_number",
            "as": "vehicle_info"
        }}
    ]))

    return render_template("dashboard.html",
        total_vehicles=total_vehicles,
        total_violations=total_violations,
        pending_fines=pending_fines,
        collected=collected,
        recent=recent)

# ─── VEHICLES ─────────────────────────────────────────────────────────────────
@app.route("/vehicles")
def vehicle_list():
    all_vehicles = list(vehicles_col.find())
    return render_template("vehicles.html", vehicles=all_vehicles)

@app.route("/vehicles/add", methods=["POST"])
def add_vehicle():
    # LAB 1: INSERT operation
    vehicles_col.insert_one({
        "plate_number": request.form["plate_number"].upper(),
        "owner_name": request.form["owner_name"],
        "contact": request.form["contact"],
        "registered_on": datetime.now()
    })
    return redirect(url_for("vehicle_list"))

@app.route("/vehicles/delete/<id>")
def delete_vehicle(id):
    # LAB 1: DELETE operation
    vehicles_col.delete_one({"_id": ObjectId(id)})
    return redirect(url_for("vehicle_list"))

# ─── VIOLATIONS ───────────────────────────────────────────────────────────────
@app.route("/violations")
def violation_list():
    # LAB 3: $lookup JOIN — attach owner name to each violation
    all_violations = list(violations_col.aggregate([
        {"$sort": {"date": -1}},
        {"$lookup": {
            "from": "vehicles",
            "localField": "plate_number",
            "foreignField": "plate_number",
            "as": "vehicle_info"
        }}
    ]))
    all_vehicles = list(vehicles_col.find())
    return render_template("violations.html",
        violations=all_violations,
        vehicles=all_vehicles,
        fine_chart=FINE_CHART)

@app.route("/violations/add", methods=["POST"])
def add_violation():
    vtype = request.form["violation_type"]
    plate = request.form["plate_number"].upper()
    fine = FINE_CHART[vtype]

    # LAB 1: INSERT
    violations_col.insert_one({
        "plate_number": plate,
        "violation_type": vtype,
        "location": request.form["location"],
        "date": datetime.now(),
        "fine_amount": fine,
        "status": "Pending"
    })

    # LAB 6: INSERT TRIGGER — auto log
    trigger_log("INSERT", plate, vtype, fine)

    return redirect(url_for("violation_list"))

@app.route("/violations/delete/<id>")
def delete_violation(id):
    v = violations_col.find_one({"_id": ObjectId(id)})
    if v:
        # LAB 6: DELETE TRIGGER — auto log before delete
        trigger_log("DELETE", v["plate_number"], v["violation_type"], v["fine_amount"])
        violations_col.delete_one({"_id": ObjectId(id)})
    return redirect(url_for("violation_list"))

# ─── FINES ────────────────────────────────────────────────────────────────────
@app.route("/fines")
def fine_list():
    all_fines = list(violations_col.find().sort("date", -1))
    return render_template("fines.html", fines=all_fines)

@app.route("/fines/pay/<id>")
def pay_fine(id):
    # LAB 8: TCL TRANSACTION — commit/rollback
    session = client.start_session()
    try:
        with session.start_transaction():
            v = violations_col.find_one({"_id": ObjectId(id)}, session=session)
            if v and v["status"] == "Pending":
                # LAB 1: UPDATE
                violations_col.update_one(
                    {"_id": ObjectId(id)},
                    {"$set": {"status": "Paid", "paid_on": datetime.now()}},
                    session=session
                )
                # LAB 6: UPDATE TRIGGER — log payment
                trigger_log("UPDATE-PAID", v["plate_number"], v["violation_type"], v["fine_amount"])
            # COMMIT
    except Exception as e:
        # ROLLBACK happens automatically on exception
        print(f"Transaction failed: {e}")
    finally:
        session.end_session()
    return redirect(url_for("fine_list"))

# ─── LOGS (Trigger History) ───────────────────────────────────────────────────
@app.route("/logs")
def logs():
    all_logs = list(logs_col.find().sort("timestamp", -1).limit(50))
    return render_template("logs.html", logs=all_logs)

# ─── SEARCH ───────────────────────────────────────────────────────────────────
@app.route("/search")
def search():
    query = request.args.get("q", "").upper()
    results = []
    vehicle_info = None
    if query:
        # LAB 3: $lookup JOIN — get violations + owner info together
        results = list(violations_col.aggregate([
            {"$match": {"plate_number": query}},
            {"$lookup": {
                "from": "vehicles",
                "localField": "plate_number",
                "foreignField": "plate_number",
                "as": "vehicle_info"
            }},
            {"$sort": {"date": -1}}
        ]))
        vehicle_info = vehicles_col.find_one({"plate_number": query})
    return render_template("search.html", results=results, query=query, vehicle_info=vehicle_info)

if __name__ == "__main__":
    app.run(debug=True)