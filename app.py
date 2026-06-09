import os
import json
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# --- CONFIGURATION ---
TBA_API_KEY = os.environ.get("TBA_API_KEY", "") # Loading the API key from .env for security
TEAMS_FILE = "teams.json"
MATCHES_FILE = "matches.json"

# --- LOGGING SYSTEM ---
server_logs = []

def add_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    server_logs.append(log_entry)
    if len(server_logs) > 500:
        server_logs.pop(0)

import threading

# --- DATA MANAGEMENT ---
def load_teams():
    if not os.path.exists(TEAMS_FILE) or os.path.getsize(TEAMS_FILE) == 0:
        save_teams({})
        return {}
    try:
        with open(TEAMS_FILE, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        add_log("WARNING: teams.json is corrupt, resetting to empty.")
        save_teams({})
        return {}

def save_teams(data):
    with open(TEAMS_FILE, "w") as file:
        json.dump(data, file, indent=4)

def load_matches():
    default = {"event_key": "", "event_name": "", "current_match": "", "event_matches": {}}
    if not os.path.exists(MATCHES_FILE) or os.path.getsize(MATCHES_FILE) == 0:
        save_matches(default)
        return default
    try:
        with open(MATCHES_FILE, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        add_log("WARNING: matches.json is corrupt, resetting to defaults.")
        save_matches(default)
        return default

def save_matches(data):
    with open(MATCHES_FILE, "w") as file:
        json.dump(data, file, indent=4)

def fetch_from_tba(endpoint):
    url = f"https://www.thebluealliance.com/api/v3/{endpoint}"
    headers = {"X-TBA-Auth-Key": TBA_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    return None

def fetch_statbotics_epa(team_key, year):
    """
    Fetch EPA from Statbotics v3 API.
    Falls back to prior years if current year has no data yet.
    """
    team_num = team_key.replace("frc", "")
    for check_year in [year, str(int(year) - 1), str(int(year) - 2)]:
        url = f"https://api.statbotics.io/v3/team_year/{team_num}/{check_year}"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data:
                    return data
        except requests.exceptions.RequestException as e:
            add_log(f"Statbotics error for {team_num}/{check_year}: {e}")
            continue
    return {}

def sync_event_data(event_key):
    add_log(f"Starting sync for event: {event_key}")
    
    add_log("Fetching full match schedule from TBA...")
    matches = fetch_from_tba(f"event/{event_key}/matches")
    add_log("Fetching team roster from TBA...")
    teams = fetch_from_tba(f"event/{event_key}/teams/simple")
    add_log("Fetching current rankings from TBA...")
    rankings_data = fetch_from_tba(f"event/{event_key}/rankings") 
    
    if matches is None or teams is None:
        add_log(f"ERROR: Failed to fetch TBA data for {event_key}.")
        return False, f"Failed to fetch data for {event_key}. Check your event key and TBA API key."
    
    year = event_key[:4]
    matches_data = load_matches()
    
    event_records = {}
    if rankings_data and "rankings" in rankings_data:
        for rank_info in rankings_data["rankings"]:
            tk = rank_info["team_key"]
            rec = rank_info["record"]
            event_records[tk] = f"{rec['wins']}-{rec['losses']}-{rec['ties']}"
    
    matches_data["event_key"] = event_key
    matches_data["event_matches"] = {m["key"]: m for m in matches}
    
    team_stats = {}
    for team in teams:
        tk = team["key"]
        team_stats[tk] = {"auto": 0, "teleop": 0, "match": 0, "count": 0}
        
    for m in matches:
        # FIX: Only count matches that have actually been played
        if not m.get("actual_time"):
            continue 
            
        for alliance in ["red", "blue"]:
            # FIX: Get the definitive match score from the alliance block directly
            total_pts = m["alliances"][alliance].get("score", 0)
            
            # Safely get the score breakdown
            score_data = (m.get("score_breakdown") or {}).get(alliance, {})
            
            # FIX: Fallback to multiple possible TBA keys if they change it this year
            auto_pts = score_data.get("totalAutoPoints", score_data.get("autoPoints", score_data.get("auto_points", 0)))
            teleop_pts = score_data.get("totalTeleopPoints", score_data.get("teleopPoints", score_data.get("teleop_points", 0)))
            
            for tk in m["alliances"][alliance]["team_keys"]:
                if tk in team_stats:
                    team_stats[tk]["auto"] += auto_pts
                    team_stats[tk]["teleop"] += teleop_pts
                    team_stats[tk]["match"] += total_pts
                    team_stats[tk]["count"] += 1
    
    add_log(f"Building local stats and fetching Statbotics EPA for {len(teams)} teams...")
    event_teams = {}
    
    total_teams = len(teams)
    for index, team in enumerate(teams, 1):
        tk = team["key"]
        
        # Live progress logging
        add_log(f"Fetching data for Team {team['team_number']} ({index}/{total_teams})...")
        
        sb_data = fetch_statbotics_epa(tk, year)
        
        # v3 API actual structure:
        #   epa.total_points.mean  <- the primary mean EPA value
        #   epa.stats.pre_champs   <- fallback end-of-season value
        #   record.wins/losses/ties
        epa_obj    = sb_data.get("epa") or {}
        record_obj = sb_data.get("record") or {}

        total_points = epa_obj.get("total_points") or {}
        stats        = epa_obj.get("stats") or {}
        epa = total_points.get("mean") or stats.get("pre_champs") or 0.0
        wins   = record_obj.get("wins",   0) or 0
        losses = record_obj.get("losses", 0) or 0
        ties   = record_obj.get("ties",   0) or 0
        
        t_stats = team_stats[tk]
        count = t_stats["count"]
        if count > 0:
            avg_auto = t_stats["auto"] / count
            avg_teleop = t_stats["teleop"] / count
            avg_match = t_stats["match"] / count
        else:
            avg_auto = avg_teleop = avg_match = 0.0
        
        event_teams[tk] = {
            "team_number": team["team_number"],
            "team_name": team["nickname"],
            "season_wlt": f"{wins}-{losses}-{ties}",
            "event_wlt": event_records.get(tk, "0-0-0"),
            "epa": round(float(epa), 1),
            "avg_auto_score": round(avg_auto, 1),
            "avg_teleop_score": round(avg_teleop, 1),
            "avg_match_score": round(avg_match, 1) 
        }
    
    if matches_data.get("current_match") not in matches_data["event_matches"]:
        matches_data["current_match"] = ""
    
    add_log("Saving fresh data to local JSON files...")
    matches_data["last_full_sync"] = datetime.now().strftime("%b %d %I:%M:%S %p")
    save_matches(matches_data)
    save_teams(event_teams)

    add_log("Sync Complete!")
    return True, "Data synchronized successfully!"

def calculate_h2h(team_a_key, team_b_key):
    wlt_2026 = [0, 0, 0]       
    wlt_since_2022 = [0, 0, 0]
    
    for year in range(2022, 2027):
        matches = fetch_from_tba(f"team/{team_a_key}/matches/{year}/simple")
        if not matches:
            continue
            
        for m in matches:
            if not m.get("actual_time"):
                continue
                
            red_alliance = m["alliances"]["red"]["team_keys"]
            
            team_a_alliance = "red" if team_a_key in red_alliance else "blue"
            opponent_alliance = "blue" if team_a_alliance == "red" else "red"
            
            if team_b_key in m["alliances"][opponent_alliance]["team_keys"]:
                winner = m.get("winning_alliance")
                is_tie = (winner == "" or winner is None)
                is_win = (winner == team_a_alliance)
                
                if is_tie:
                    wlt_since_2022[2] += 1
                    if year == 2026: wlt_2026[2] += 1
                elif is_win:
                    wlt_since_2022[0] += 1
                    if year == 2026: wlt_2026[0] += 1
                else:
                    wlt_since_2022[1] += 1
                    if year == 2026: wlt_2026[1] += 1

    return {
        "h2h_2026": f"{wlt_2026[0]}-{wlt_2026[1]}-{wlt_2026[2]}",
        "h2h_since_2022": f"{wlt_since_2022[0]}-{wlt_since_2022[1]}-{wlt_since_2022[2]}"
    }

# --- MATCH ORDERING ---

def sorted_match_keys(event_matches):
    """Return match keys in play order: quals → semis → finals, then by set/match number."""
    level_order = {"qm": 0, "sf": 1, "f": 2}
    return sorted(
        event_matches.keys(),
        key=lambda k: (
            level_order.get(event_matches[k].get("comp_level", ""), 9),
            event_matches[k].get("set_number", 0),
            event_matches[k].get("match_number", 0),
        )
    )

def find_next_match(matches_data, current_key):
    keys = sorted_match_keys(matches_data.get("event_matches", {}))
    try:
        idx = keys.index(current_key)
        return keys[idx + 1] if idx + 1 < len(keys) else None
    except ValueError:
        return None

# --- MATCH NAME FORMATTING ---

def format_match_name(match):
    """Return a human-readable match title, using custom_name override if set."""
    custom = (match.get("custom_name") or "").strip()
    if custom:
        return custom
    level = match.get("comp_level", "")
    set_num = match.get("set_number", 1)
    match_num = match.get("match_number", 1)
    if level == "qm":
        return f"Qualification Match {match_num}"
    elif level == "sf":
        return f"Semifinal {set_num} Match {match_num}"
    elif level == "f":
        return f"Finals Match {match_num}"
    return f"{level.upper()}{set_num}-{match_num}"

# --- LOGO HELPER ---

def get_logo(team_key, teams_data):
    """Return logo filename if the team has a logo and logo_enabled is True (default)."""
    t = teams_data.get(team_key, {})
    return t.get("logo", "") if t.get("logo_enabled", True) else ""

# --- CORE API ROUTES ---

@app.route("/")
def control_panel():
    matches_data = load_matches()
    teams_data = load_teams()
    data = {**matches_data, "event_teams": teams_data}
    return render_template("control.html", data=data)

@app.route("/teams")
def team_setup():
    teams_data = load_teams()
    return render_template("teams.html", teams=teams_data)

@app.route("/api/teams")
def api_teams():
    return jsonify(load_teams())

@app.route("/matches")
def matches_page():
    return render_template("matches.html")

@app.route("/api/sync_times")
def api_sync_times():
    matches_data = load_matches()
    return jsonify({
        "last_full_sync":  matches_data.get("last_full_sync",  "Never"),
        "last_score_sync": matches_data.get("last_score_sync", "Never"),
        "event_key":       matches_data.get("event_key", ""),
    })

@app.route("/api/matches")
def api_matches():
    matches_data = load_matches()
    return jsonify({
        "event_key":     matches_data.get("event_key", ""),
        "event_name":    matches_data.get("event_name", ""),
        "current_match": matches_data.get("current_match", ""),
        "matches":       matches_data.get("event_matches", {}),
    })

@app.route("/api/debug/match_breakdown")
def debug_match_breakdown():
    """Return the score_breakdown keys from the first played match — shows what TBA actually sends."""
    matches_data = load_matches()
    for key, m in matches_data.get("event_matches", {}).items():
        if m.get("actual_time") and m.get("score_breakdown"):
            return jsonify({
                "match_key": key,
                "red_keys": list((m["score_breakdown"].get("red") or {}).keys()),
                "blue_keys": list((m["score_breakdown"].get("blue") or {}).keys()),
                "red_sample": m["score_breakdown"].get("red"),
            })
    return jsonify({"error": "No played matches with score_breakdown found. TBA may not have posted breakdowns yet."})

@app.route("/api/debug/statbotics/<int:team_number>")
def debug_statbotics(team_number):
    """Hit Statbotics for a single team and return the raw response — useful for diagnosing EPA=0 issues."""
    matches_data = load_matches()
    year = (matches_data.get("event_key") or "2026")[:4]
    team_key = f"frc{team_number}"
    add_log(f"[DEBUG] Fetching raw Statbotics data for {team_number} (year={year})")
    raw = fetch_statbotics_epa(team_key, year)
    epa_obj = raw.get("epa") or {}
    return jsonify({
        "team": team_number,
        "year_tried": year,
        "raw_response": raw,
        "parsed_epa_mean": epa_obj.get("mean"),
        "parsed_epa_end": epa_obj.get("end"),
        "parsed_record": raw.get("record"),
    })

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": server_logs})

# --- SYNC STATE ---
sync_state = {"running": False, "success": None, "message": ""}

def _run_sync(event_key):
    global sync_state
    sync_state = {"running": True, "success": None, "message": ""}
    success, message = sync_event_data(event_key)
    sync_state = {"running": False, "success": success, "message": message}

@app.route("/api/sync_status")
def api_sync_status():
    return jsonify(sync_state)

@app.route("/api/sync", methods=["POST"])
def sync_tba_data():
    global sync_state
    if sync_state["running"]:
        return jsonify({"status": "error", "message": "Sync already in progress."}), 409
    event_key = request.form.get("event_key")
    if not event_key:
        add_log("ERROR: Attempted to sync without an event key.")
        return jsonify({"status": "error", "message": "Event key is required."}), 400
    threading.Thread(target=_run_sync, args=(event_key,), daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/set_active_match", methods=["POST"])
def set_active_match():
    match_key = request.form.get("match_key")
    matches_data = load_matches()
    
    if match_key in matches_data["event_matches"]:
        matches_data["current_match"] = match_key
        save_matches(matches_data)
        add_log(f"Active match updated to: {match_key}")
        return jsonify({"status": "success", "current_match": match_key})
        
    add_log(f"ERROR: Invalid match key attempted: {match_key}")
    return jsonify({"status": "error", "message": "Invalid match key"}), 400

# --- API ENDPOINTS ---

# Endpoint for the active match
@app.route("/api/active_match.json")
def api_active_match():
    matches_data = load_matches()
    teams_data = load_teams()
    active_key = matches_data.get("current_match")
    
    if not active_key or active_key not in matches_data.get("event_matches", {}):
        return jsonify({"error": "No active match set"})
    
    match = matches_data["event_matches"][active_key]
    
    def get_epa(team_key):
        return teams_data.get(team_key, {}).get("epa", 0.0)

    def get_team_name(team_key):
        return teams_data.get(team_key, {}).get("team_name", "")

    red_keys = match["alliances"]["red"]["team_keys"]
    blue_keys = match["alliances"]["blue"]["team_keys"]

    output = {
        "match_name": format_match_name(match),

        "red_1": red_keys[0].replace("frc", ""),
        "red_1_name": get_team_name(red_keys[0]),
        "red_1_logo": get_logo(red_keys[0], teams_data),
        "red_1_epa": get_epa(red_keys[0]),
        "red_2": red_keys[1].replace("frc", ""),
        "red_2_name": get_team_name(red_keys[1]),
        "red_2_logo": get_logo(red_keys[1], teams_data),
        "red_2_epa": get_epa(red_keys[1]),
        "red_3": red_keys[2].replace("frc", ""),
        "red_3_name": get_team_name(red_keys[2]),
        "red_3_logo": get_logo(red_keys[2], teams_data),
        "red_3_epa": get_epa(red_keys[2]),
        "red_score": match["alliances"]["red"].get("score", 0),

        "blue_1": blue_keys[0].replace("frc", ""),
        "blue_1_name": get_team_name(blue_keys[0]),
        "blue_1_logo": get_logo(blue_keys[0], teams_data),
        "blue_1_epa": get_epa(blue_keys[0]),
        "blue_2": blue_keys[1].replace("frc", ""),
        "blue_2_name": get_team_name(blue_keys[1]),
        "blue_2_logo": get_logo(blue_keys[1], teams_data),
        "blue_2_epa": get_epa(blue_keys[1]),
        "blue_3": blue_keys[2].replace("frc", ""),
        "blue_3_name": get_team_name(blue_keys[2]),
        "blue_3_logo": get_logo(blue_keys[2], teams_data),
        "blue_3_epa": get_epa(blue_keys[2]),
        "blue_score": match["alliances"]["blue"].get("score", 0),
    }
    
    output["red_total_epa"] = round(output["red_1_epa"] + output["red_2_epa"] + output["red_3_epa"], 1)
    output["blue_total_epa"] = round(output["blue_1_epa"] + output["blue_2_epa"] + output["blue_3_epa"], 1)

    return jsonify(output)

@app.route("/api/team_profile/<team_number>.json")
def api_team_profile(team_number):
    teams_data = load_teams()
    team_key = f"frc{team_number}"

    if team_key not in teams_data:
        return jsonify({"error": "Team not found in current event data"})

    result = dict(teams_data[team_key])
    # Computed logo field that respects the logo_enabled toggle
    result["logo_display"] = get_logo(team_key, teams_data)
    return jsonify(result)

@app.route("/api/h2h/<team_a_number>/<team_b_number>.json")
def api_h2h(team_a_number, team_b_number):
    team_a_key = f"frc{team_a_number}"
    team_b_key = f"frc{team_b_number}"
    
    teams_data = load_teams()
    
    profile_a = teams_data.get(team_a_key, {})
    profile_b = teams_data.get(team_b_key, {})
    
    h2h_stats = calculate_h2h(team_a_key, team_b_key)
    
    output = {
        "team_a_number": team_a_number,
        "team_a_name": profile_a.get("team_name", ""),
        "team_a_logo": get_logo(team_a_key, teams_data),
        "team_a_event_wlt": profile_a.get("event_wlt", ""),
        "team_a_epa": profile_a.get("epa", ""),

        "team_b_number": team_b_number,
        "team_b_name": profile_b.get("team_name", ""),
        "team_b_logo": get_logo(team_b_key, teams_data),
        "team_b_event_wlt": profile_b.get("event_wlt", ""),
        "team_b_epa": profile_b.get("epa", ""),

        "h2h_2026": h2h_stats["h2h_2026"],
        "h2h_since_2022": h2h_stats["h2h_since_2022"]
    }
    
    return jsonify(output)

# --- QUICK SYNC & IMPORT ---

@app.route("/api/sync_current", methods=["POST"])
def api_sync_current():
    global sync_state
    if sync_state["running"]:
        return jsonify({"status": "error", "message": "Sync already in progress."}), 409
    matches_data = load_matches()
    event_key = matches_data.get("event_key", "")
    if not event_key:
        return jsonify({"status": "error", "message": "No event key configured. Use Event Setup first."}), 400
    threading.Thread(target=_run_sync, args=(event_key,), daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/import/schedule", methods=["POST"])
def api_import_schedule():
    import csv, io
    csv_text = request.form.get("csv_data", "").strip()
    if not csv_text:
        return jsonify({"status": "error", "message": "No CSV data provided"}), 400

    matches_data = load_matches()
    event_key = matches_data.get("event_key", "")
    if not event_key:
        return jsonify({"status": "error", "message": "No event key set. Configure it in Event Setup first."}), 400

    def parse_match_id(s):
        s = s.strip().upper().replace("M", "-")
        if s.startswith("SF"):
            parts = s[2:].split("-")
            return "sf", int(parts[0]), int(parts[1]) if len(parts) > 1 else 1
        elif s.startswith("F"):
            parts = s[1:].split("-")
            return "f", int(parts[0]), int(parts[1]) if len(parts) > 1 else 1
        else:
            # Strip all non-digit characters — handles QM1, Q1, plain numbers,
            # and the "Q-1" artifact caused by the M→- replacement above
            num = ''.join(c for c in s if c.isdigit())
            return "qm", 1, int(num)

    def norm(t):
        t = t.strip()
        return None if not t else (t if t.startswith("frc") else f"frc{t}")

    reader = csv.DictReader(io.StringIO(csv_text))
    imported, errors = 0, []

    for i, row in enumerate(reader):
        row = {k.strip().lower(): v.strip() for k, v in row.items()}
        match_str = row.get("match", row.get("match_number", ""))
        if not match_str:
            errors.append(f"row {i+2}: missing match id")
            continue
        try:
            comp_level, set_num, match_num = parse_match_id(match_str)
        except Exception:
            errors.append(f"row {i+2}: bad match id '{match_str}'")
            continue

        red_keys  = [k for k in [norm(row.get("red1","")), norm(row.get("red2","")), norm(row.get("red3",""))] if k]
        blue_keys = [k for k in [norm(row.get("blue1","")), norm(row.get("blue2","")), norm(row.get("blue3",""))] if k]

        suffix = f"qm{match_num}" if comp_level == "qm" else f"{comp_level}{set_num}m{match_num}"
        match_key = f"{event_key}_{suffix}"

        existing = matches_data["event_matches"].get(match_key, {})
        existing_alliances = existing.get("alliances", {})
        matches_data["event_matches"][match_key] = {
            **existing,
            "key": match_key,
            "comp_level": comp_level,
            "set_number": set_num,
            "match_number": match_num,
            "alliances": {
                "red":  {**existing_alliances.get("red",  {}), "team_keys": red_keys,  "score": existing_alliances.get("red",  {}).get("score", -1)},
                "blue": {**existing_alliances.get("blue", {}), "team_keys": blue_keys, "score": existing_alliances.get("blue", {}).get("score", -1)},
            },
            "time": existing.get("time"),
            "actual_time": existing.get("actual_time"),
            "winning_alliance": existing.get("winning_alliance"),
            "score_breakdown": existing.get("score_breakdown"),
        }
        imported += 1

    save_matches(matches_data)
    add_log(f"CSV import: {imported} matches imported for {event_key}")
    msg = f"Imported {imported} match(es)."
    if errors:
        msg += " Errors: " + "; ".join(errors[:3])
    return jsonify({"status": "success", "message": msg, "imported": imported})

@app.route("/api/reset/schedule", methods=["POST"])
def api_reset_schedule():
    matches_data = load_matches()
    count = len(matches_data.get("event_matches", {}))
    matches_data["event_matches"] = {}
    matches_data["current_match"] = ""
    save_matches(matches_data)
    add_log(f"Schedule reset: cleared {count} matches")
    return jsonify({"status": "success", "cleared": count})

# --- MANUAL EDIT ENDPOINTS ---

@app.route("/api/sync_scores", methods=["POST"])
def api_sync_scores():
    matches_data = load_matches()
    event_key = matches_data.get("event_key", "")
    if not event_key:
        return jsonify({"status": "error", "message": "No event key configured."}), 400

    tba_matches = fetch_from_tba(f"event/{event_key}/matches/simple")
    if tba_matches is None:
        return jsonify({"status": "error", "message": "Failed to fetch scores from TBA."}), 502

    updated = 0
    for tm in tba_matches:
        key = tm.get("key")
        if key not in matches_data["event_matches"]:
            continue
        if not tm.get("actual_time"):
            continue
        m = matches_data["event_matches"][key]
        m["actual_time"]      = tm["actual_time"]
        m["winning_alliance"] = tm.get("winning_alliance")
        m["alliances"]["red"]["score"]  = tm["alliances"]["red"].get("score", -1)
        m["alliances"]["blue"]["score"] = tm["alliances"]["blue"].get("score", -1)
        updated += 1

    # Recalculate event W/L/T
    event_records = {}
    for mk, match in matches_data["event_matches"].items():
        w = match.get("winning_alliance")
        if w is None:
            continue
        for side in ["red", "blue"]:
            for tk in match["alliances"][side].get("team_keys", []):
                if tk not in event_records:
                    event_records[tk] = [0, 0, 0]
                if w == "":
                    event_records[tk][2] += 1
                elif w == side:
                    event_records[tk][0] += 1
                else:
                    event_records[tk][1] += 1

    teams_data = load_teams()
    for tk, rec in event_records.items():
        if tk in teams_data:
            teams_data[tk]["event_wlt"] = f"{rec[0]}-{rec[1]}-{rec[2]}"

    matches_data["last_score_sync"] = datetime.now().strftime("%b %d %I:%M:%S %p")
    save_matches(matches_data)
    save_teams(teams_data)
    add_log(f"Score sync: updated {updated} match result(s) from TBA.")
    return jsonify({"status": "success", "updated": updated})

@app.route("/api/set_match_result", methods=["POST"])
def api_set_match_result():
    payload = request.get_json()
    match_key = payload.get("match_key")
    winner = payload.get("winner", "")  # "red", "blue", or "" (tie)
    red_score  = payload.get("red_score")
    blue_score = payload.get("blue_score")

    if winner not in ("red", "blue", ""):
        return jsonify({"status": "error", "message": "winner must be red, blue, or empty string for tie"}), 400

    matches_data = load_matches()
    if match_key not in matches_data.get("event_matches", {}):
        return jsonify({"status": "error", "message": "Match not found"}), 404

    m = matches_data["event_matches"][match_key]
    m["winning_alliance"] = winner
    if not m.get("actual_time"):
        m["actual_time"] = int(datetime.now().timestamp())
    if red_score is not None:
        m["alliances"]["red"]["score"]  = int(red_score)
    if blue_score is not None:
        m["alliances"]["blue"]["score"] = int(blue_score)

    # Recalculate event W/L/T from all matches that have a result
    event_records = {}
    for mk, match in matches_data["event_matches"].items():
        w = match.get("winning_alliance")
        if w is None:
            continue
        for side in ["red", "blue"]:
            for tk in match["alliances"][side].get("team_keys", []):
                if tk not in event_records:
                    event_records[tk] = [0, 0, 0]
                if w == "":
                    event_records[tk][2] += 1
                elif w == side:
                    event_records[tk][0] += 1
                else:
                    event_records[tk][1] += 1

    teams_data = load_teams()
    for tk, rec in event_records.items():
        if tk in teams_data:
            teams_data[tk]["event_wlt"] = f"{rec[0]}-{rec[1]}-{rec[2]}"

    # Advance to next match
    next_key = find_next_match(matches_data, match_key)
    if next_key:
        matches_data["current_match"] = next_key

    save_matches(matches_data)
    save_teams(teams_data)

    label = "Tie" if winner == "" else f"{winner.capitalize()} wins"
    add_log(f"Result: {match_key} → {label}. Active match → {next_key or 'none'}")
    return jsonify({"status": "success", "next_match": next_key})

@app.route("/api/edit/match_title", methods=["POST"])
def api_edit_match_title():
    payload = request.get_json()
    match_key = payload.get("match_key")
    title = payload.get("title", "").strip()
    matches_data = load_matches()
    if match_key not in matches_data.get("event_matches", {}):
        return jsonify({"status": "error", "message": "Match not found"}), 404
    matches_data["event_matches"][match_key]["custom_name"] = title
    save_matches(matches_data)
    add_log(f"Match title updated: {match_key} → '{title or '(reset to default)'}'")
    return jsonify({"status": "success"})

@app.route("/api/edit/team", methods=["POST"])
def api_edit_team():
    payload = request.get_json()
    team_key = payload.get("team_key")
    updates = payload.get("updates", {})
    teams_data = load_teams()
    if team_key not in teams_data:
        return jsonify({"status": "error", "message": "Team not found"}), 404
    allowed = {"team_name", "logo", "logo_enabled", "notes", "epa", "avg_auto_score", "avg_teleop_score", "avg_match_score", "season_wlt", "event_wlt"}
    teams_data[team_key].update({k: v for k, v in updates.items() if k in allowed})
    save_teams(teams_data)
    add_log(f"Manual edit: updated {team_key}")
    return jsonify({"status": "success"})

@app.route("/api/edit/match_roster", methods=["POST"])
def api_edit_match_roster():
    payload = request.get_json()
    match_key = payload.get("match_key")
    red_keys = payload.get("red_keys", [])
    blue_keys = payload.get("blue_keys", [])
    matches_data = load_matches()
    if match_key not in matches_data.get("event_matches", {}):
        return jsonify({"status": "error", "message": "Match not found"}), 404
    def normalize(k):
        k = str(k).strip()
        return k if k.startswith("frc") else f"frc{k}"
    matches_data["event_matches"][match_key]["alliances"]["red"]["team_keys"] = [normalize(k) for k in red_keys]
    matches_data["event_matches"][match_key]["alliances"]["blue"]["team_keys"] = [normalize(k) for k in blue_keys]
    save_matches(matches_data)
    add_log(f"Manual edit: updated roster for {match_key}")
    return jsonify({"status": "success"})

@app.route("/api/rankings.json")
def api_rankings():
    teams_data = load_teams()

    def sort_key(item):
        wlt = item[1].get("event_wlt", "0-0-0")
        try:
            w, l, t = [int(x) for x in wlt.split("-")]
        except ValueError:
            w, l, t = 0, 0, 0
        return (w, -l, t)   # most wins → fewest losses → most ties

    ranked = sorted(teams_data.items(), key=sort_key, reverse=True)

    return jsonify([
        {
            "rank":        rank,
            "team_number": team["team_number"],
            "team_name":   team["team_name"],
            "logo":        get_logo(tk, teams_data),
            "record":      team.get("event_wlt", "0-0-0"),
            "epa":         team.get("epa", 0.0),
        }
        for rank, (tk, team) in enumerate(ranked, 1)
    ])

# --- STARTUP ROUTINE ---

def startup_routine():
    add_log("--- FRC API Initializing ---")
    matches_data = load_matches()
    event_key = matches_data.get("event_key")
    
    if event_key:
        add_log(f"Found saved event key: {event_key}. Syncing with TBA...")
        success, message = sync_event_data(event_key)
        if success:
            add_log("Startup sync complete. Data is fresh.")
        else:
            add_log(f"Startup sync failed: {message}")
    else:
        add_log("No previous event key found. Waiting for user configuration via the Web UI.")
    add_log("---------------------------------")

# Run startup sync in a background thread.
# - Under Flask dev server: WERKZEUG_RUN_MAIN guard prevents double-run from the reloader.
# - Under Gunicorn/Waitress: __name__ != "__main__", so this block is what triggers it.
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    threading.Thread(target=startup_routine, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)