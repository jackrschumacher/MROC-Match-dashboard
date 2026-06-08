import platform
import threading
from app import app, startup_routine

# Run startup sync in background so server is immediately ready
threading.Thread(target=startup_routine, daemon=True).start()

if platform.system() == "Windows":
    from waitress import serve
    print("Starting on http://0.0.0.0:5000")
    serve(app, host="0.0.0.0", port=5000)
else:
    # Gunicorn on Linux/ Raspberry Pi — launched via CLI, not from here
    # This branch won't be reached; gunicorn imports app directly
    pass