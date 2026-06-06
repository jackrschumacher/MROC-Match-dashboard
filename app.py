import os
import json
import requests
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# --- CONFIGURATION ---
TBA_API_KEY = ""  # Keep your API key here
DATA_BACKUP_FILE = "data.json"

# --- DATA MANAGEMENT ---
def load_data():
    """Loads cached data from the local JSON file."""
    if not os.path.exists(DATA_BACKUP_FILE):
        data = {
            "event_key": "",
            "event_name": "",
            "current_match": "",
            "event_matches": {},
            "event_teams": {}
        }
        save_data(data)
        return data
    
    with open(DATA_BACKUP_FILE, "r") as file:
        return json.load(file)

def save_data(data):
    """Saves data to the local JSON file."""
    with open(DATA_BACKUP_FILE, "w") as file:
        json.dump(data, file, indent=4)

def fetch_from_tba(endpoint):
    """Helper function to request data from The Blue Alliance."""
    url = f"https://www.thebluealliance.com/api/v3/{endpoint}"
    headers = {"X-TBA-Auth-Key": TBA_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    return None

def fetch_statbotics_epa(team_key, year):
    """Fetches a team's EPA for a specific year from Statbotics API v3."""
    team_num = team_key.replace("frc", "")
    url = f"https://api.statbotics.io/v3/team_year/{team_num}/{year}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return {}

def sync_event_data(event_key):
    """Core logic to fetch and save event data."""
    matches = fetch_from_tba(f"event/{event_key}/matches/simple")
    teams = fetch_from_tba(f"event/{event_key}/teams/simple")
    rankings_data = fetch_from_tba(f"event/{event_key}/rankings") 
    
    if matches is None or teams is None:
        return False, f"Failed to fetch data for {event_key}. Check your key and TBA API key."
    
    year = event_key[:4]
    current_data = load_data()
    
    # Process MROC WLT from event rankings
    mroc_records = {}
    if rankings_data and "rankings" in rankings_data:
        for rank_info in rankings_data["rankings"]:
            tk = rank_info["team_key"]
            rec = rank_info["record"]
            mroc_records[tk] = f"{rec['wins']}-{rec['losses']}-{rec['ties']}"
    
    current_data["event_key"] = event_key
    current_data["event_matches"] = {m["key"]: m for m in matches}
    
    print(f"Building profiles for {len(teams)} teams...")
    event_teams = {}
    
    for team in teams:
        tk = team["key"]
        sb_data = fetch_statbotics_epa(tk, year)
        
        event_teams[tk] = {
            "team_number": team["team_number"],
            "team_name": team["nickname"],
            "season_wlt": f"{sb_data.get('wins', 0)}-{sb_data.get('losses', 0)}-{sb_data.get('ties', 0)}",
            "mroc_wlt": mroc_records.get(tk, "0-0-0"),
            "epa": round(sb_data.get("epa_end", 0.0), 1),
            "avg_auto_score": round(sb_data.get("auto_epa_end", 0.0), 1),
            "avg_teleop_score": round(sb_data.get("teleop_epa_end", 0.0), 1),
            "avg_match_score": round(sb_data.get("epa_end", 0.0), 1) 
        }
        
    current_data["event_teams"] = event_teams
    
    # If the active match is from an old event, clear it
    if current_data.get("current_match") not in current_data["event_matches"]:
        current_data["current_match"] = ""
    
    save_data(current_data)
    return True, "Data synchronized successfully!"

def calculate_h2h(team_a_key, team_b_key):
    """Calculates H2H WLT between two teams for 2026 and since 2022."""
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
    """Renders the web UI to control the graphics."""
    data = load_data()
    return render_template("control.html", data=data)

@app.route("/api/sync", methods=["POST"])
def sync_tba_data():
    """Triggered by the Web UI to fetch latest TBA data."""
    event_key = request.form.get("event_key")
    
    if not event_key:
        return jsonify({"status": "error", "message": "Event key is required."}), 400

    success, message = sync_event_data(event_key)
    
    if success:
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": message}), 500

@app.route("/api/set_active_match", methods=["POST"])
def set_active_match():
    """Updates which match key is currently displayed on screen."""
    match_key = request.form.get("match_key")
    data = load_data()
    if match_key in data["event_matches"]:
        data["current_match"] = match_key
        save_data(data)
        return jsonify({"status": "success", "current_match": match_key})
    return jsonify({"status": "error", "message": "Invalid match key"}), 400

# --- VMIX ENDPOINTS ---

@app.route("/vmix/active_match.json")
def vmix_active_match():
    """Returns data for the currently selected match."""
    data = load_data()
    active_key = data.get("current_match")
    
    if not active_key or active_key not in data.get("event_matches", {}):
        return jsonify({"error": "No active match set"})
    
    match = data["event_matches"][active_key]
    
    def get_epa(team_key):
        team_data = data.get("event_teams", {}).get(team_key, {})
        return team_data.get("epa", 0.0)

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
    """vMix endpoint for a single team's stats."""
    data = load_data()
    team_key = f"frc{team_number}"
    
    if team_key not in data.get("event_teams", {}):
        return jsonify({"error": "Team not found in current event data"})
        
    return jsonify(data["event_teams"][team_key])

@app.route("/vmix/h2h/<team_a_number>/<team_b_number>.json")
def vmix_h2h(team_a_number, team_b_number):
    """vMix endpoint that generates H2H stats on the fly."""
    team_a_key = f"frc{team_a_number}"
    team_b_key = f"frc{team_b_number}"
    
    data = load_data()
    teams = data.get("event_teams", {})
    
    profile_a = teams.get(team_a_key, {})
    profile_b = teams.get(team_b_key, {})
    
    h2h_stats = calculate_h2h(team_a_key, team_b_key)
    
    output = {
        "team_a_number": team_a_number,
        "team_a_name": profile_a.get("team_name", ""),
        "team_a_mroc_wlt": profile_a.get("mroc_wlt", ""),
        "team_a_epa": profile_a.get("epa", ""),
        
        "team_b_number": team_b_number,
        "team_b_name": profile_b.get("team_name", ""),
        "team_b_mroc_wlt": profile_b.get("mroc_wlt", ""),
        "team_b_epa": profile_b.get("epa", ""),
        
        "h2h_2026": h2h_stats["h2h_2026"],
        "h2h_since_2022": h2h_stats["h2h_since_2022"]
    }
    
    return jsonify(output)

# --- STARTUP ROUTINE ---

def startup_routine():
    """Runs when the script is first executed to ensure fresh data on boot."""
    print("--- FRC vMix API Initializing ---")
    data = load_data()
    event_key = data.get("event_key")
    
    if event_key:
        print(f"Found saved event key: {event_key}. Syncing with TBA...")
        success, message = sync_event_data(event_key)
        if success:
            print("Startup sync complete. Data is fresh.")
        else:
            print(f"Startup sync failed: {message}")
    else:
        print("No previous event key found. Waiting for user configuration via the Web UI.")
    print("---------------------------------")

if __name__ == "__main__":
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        startup_routine()
        
    app.run(host="0.0.0.0", port=5000, debug=True)