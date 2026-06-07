import os
import json
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# --- CONFIGURATION ---
TBA_API_KEY = ""  # Keep your API key here
TEAMS_FILE = "teams.json"
MATCHES_FILE = "matches.json"

# --- LOGGING SYSTEM ---
server_logs = []

def add_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    server_logs.append(log_entry)
    if len(server_logs) > 300:
        server_logs.pop(0)

# --- DATA MANAGEMENT ---
def load_teams():
    if not os.path.exists(TEAMS_FILE):
        save_teams({})
        return {}
    with open(TEAMS_FILE, "r") as file:
        return json.load(file)

def save_teams(data):
    with open(TEAMS_FILE, "w") as file:
        json.dump(data, file, indent=4)

def load_matches():
    if not os.path.exists(MATCHES_FILE):
        data = {
            "event_key": "",
            "event_name": "",
            "current_match": "",
            "event_matches": {}
        }
        save_matches(data)
        return data
    with open(MATCHES_FILE, "r") as file:
        return json.load(file)

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

def fetch_statbotics_epa(team_key, year, verbose=False):
    """
    Fetch EPA from Statbotics v3 API.
    v3 response shape: { "epa": { "mean": 45.2, "end": 47.0, ... }, "record": { "wins": 8, ... } }
    Falls back to prior years if current year has no data yet.
    """
    team_num = team_key.replace("frc", "")
    for check_year in [year, str(int(year) - 1), str(int(year) - 2)]:
        url = f"https://api.statbotics.io/v3/team_year/{team_num}/{check_year}"
        try:
            response = requests.get(url, timeout=5)
            if verbose:
                add_log(f"  [SB DEBUG] {url} -> HTTP {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                if verbose:
                    add_log(f"  [SB DEBUG] keys: {list(data.keys()) if isinstance(data, dict) else 'NOT A DICT'}")
                    add_log(f"  [SB DEBUG] epa field raw: {data.get('epa')}")
                if data:
                    return data
            elif verbose:
                add_log(f"  [SB DEBUG] body: {response.text[:300]}")
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
        return False, f"Failed to fetch data for {event_key}. Check your key and TBA API key."
    
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
            auto_pts = score_data.get("autoPoints", score_data.get("auto_points", 0))
            teleop_pts = score_data.get("teleopPoints", score_data.get("teleop_points", 0))
            
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
        
        # Run first team in verbose mode so logs expose the raw API response shape
        sb_data = fetch_statbotics_epa(tk, year, verbose=(index == 1))
        
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

# --- CORE API ROUTES ---

@app.route("/")
def control_panel():
    matches_data = load_matches()
    teams_data = load_teams()
    data = {**matches_data, "event_teams": teams_data}
    return render_template("control.html", data=data)

@app.route("/api/debug/statbotics/<int:team_number>")
def debug_statbotics(team_number):
    """Hit Statbotics for a single team and return the raw response — useful for diagnosing EPA=0 issues."""
    matches_data = load_matches()
    year = (matches_data.get("event_key") or "2026")[:4]
    team_key = f"frc{team_number}"
    add_log(f"[DEBUG] Fetching raw Statbotics data for {team_number} (year={year})")
    raw = fetch_statbotics_epa(team_key, year, verbose=True)
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

@app.route("/api/sync", methods=["POST"])
def sync_tba_data():
    event_key = request.form.get("event_key")
    if not event_key:
        add_log("ERROR: Attempted to sync without an event key.")
        return jsonify({"status": "error", "message": "Event key is required."}), 400

    success, message = sync_event_data(event_key)
    
    if success:
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": message}), 500

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

# --- VMIX ENDPOINTS ---

@app.route("/vmix/active_match.json")
def vmix_active_match():
    matches_data = load_matches()
    teams_data = load_teams()
    active_key = matches_data.get("current_match")
    
    if not active_key or active_key not in matches_data.get("event_matches", {}):
        return jsonify({"error": "No active match set"})
    
    match = matches_data["event_matches"][active_key]
    
    def get_epa(team_key):
        return teams_data.get(team_key, {}).get("epa", 0.0)

    red_keys = match["alliances"]["red"]["team_keys"]
    blue_keys = match["alliances"]["blue"]["team_keys"]
    
    output = {
        "match_name": match["comp_level"].upper() + str(match["match_number"]),
        
        "red_1": red_keys[0].replace("frc", ""),
        "red_1_epa": get_epa(red_keys[0]),
        "red_2": red_keys[1].replace("frc", ""),
        "red_2_epa": get_epa(red_keys[1]),
        "red_3": red_keys[2].replace("frc", ""),
        "red_3_epa": get_epa(red_keys[2]),
        "red_score": match["alliances"]["red"].get("score", 0),
        
        "blue_1": blue_keys[0].replace("frc", ""),
        "blue_1_epa": get_epa(blue_keys[0]),
        "blue_2": blue_keys[1].replace("frc", ""),
        "blue_2_epa": get_epa(blue_keys[1]),
        "blue_3": blue_keys[2].replace("frc", ""),
        "blue_3_epa": get_epa(blue_keys[2]),
        "blue_score": match["alliances"]["blue"].get("score", 0),
    }
    
    output["red_total_epa"] = round(output["red_1_epa"] + output["red_2_epa"] + output["red_3_epa"], 1)
    output["blue_total_epa"] = round(output["blue_1_epa"] + output["blue_2_epa"] + output["blue_3_epa"], 1)

    return jsonify(output)

@app.route("/vmix/team_profile/<team_number>.json")
def vmix_team_profile(team_number):
    teams_data = load_teams()
    team_key = f"frc{team_number}"
    
    if team_key not in teams_data:
        return jsonify({"error": "Team not found in current event data"})
        
    return jsonify(teams_data[team_key])

@app.route("/vmix/h2h/<team_a_number>/<team_b_number>.json")
def vmix_h2h(team_a_number, team_b_number):
    team_a_key = f"frc{team_a_number}"
    team_b_key = f"frc{team_b_number}"
    
    teams_data = load_teams()
    
    profile_a = teams_data.get(team_a_key, {})
    profile_b = teams_data.get(team_b_key, {})
    
    h2h_stats = calculate_h2h(team_a_key, team_b_key)
    
    output = {
        "team_a_number": team_a_number,
        "team_a_name": profile_a.get("team_name", ""),
        "team_a_event_wlt": profile_a.get("event_wlt", ""),
        "team_a_epa": profile_a.get("epa", ""),
        
        "team_b_number": team_b_number,
        "team_b_name": profile_b.get("team_name", ""),
        "team_b_event_wlt": profile_b.get("event_wlt", ""),
        "team_b_epa": profile_b.get("epa", ""),
        
        "h2h_2026": h2h_stats["h2h_2026"],
        "h2h_since_2022": h2h_stats["h2h_since_2022"]
    }
    
    return jsonify(output)

# --- STARTUP ROUTINE ---

def startup_routine():
    add_log("--- FRC vMix API Initializing ---")
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

if __name__ == "__main__":
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        startup_routine()
        
    app.run(host="0.0.0.0", port=5000, debug=True)