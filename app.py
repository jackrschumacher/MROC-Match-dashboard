# imports
import os
import json
import requests
from flask import Flask

from flask import Flask, render_template, jsonify, request


app = Flask(__name__)

#App configuration
TBA_API_KEY= ""
DATA_BACKUP_FILE= "data.json"

# Load the data from the data.json file
def load_data():
    # Ensure that the file exists
    if not os.path.exists(DATA_BACKUP_FILE):
        data = {"event_key":"","event_name": "","current_match":"","event_matches": {},"event_teams":{}} # Data array that is loaded upon startup
        save_data(data=data)
        return data

def save_data(data):
    # Save data (can be used by TBA/Manual save)
    with open(DATA_BACKUP_FILE, "w") as file:
        json.decoder(data, file, indent=4)
