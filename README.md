```
from pytubefix import YouTube

# Simple and reliable
url = "https://www.youtube.com/watch?v=XegbAVpwE5o"
yt = YouTube(url)
stream = yt.streams.get_highest_resolution()
stream.download()

print(f"Downloaded: {yt.title}")

```

```
python.exe -m pip install --upgrade pip
pip install flask pytubefix flask-cors
pip freeze > requirements.txt
```



### Step for installation

# 1. Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# 2. Install your packages
pip install flask pytubefix flask-cors

# 3. Generate requirements.txt
pip freeze > requirements.txt

# 4. View the file
cat requirements.txt  # or type requirements.txt on Windows

# 5. To install on another machine
pip install -r requirements.txt



#### YT Downloader
A YouTube video downloader with a web UI. Powered by pytubefix + FFmpeg + Flask.

Local Development
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install FFmpeg (needed for merged quality presets)
#    Mac:     brew install ffmpeg
#    Linux:   sudo apt install ffmpeg
#    Windows: https://ffmpeg.org/download.html  (add to PATH)

# 3. Run with gunicorn (production server, no WARNING message)
gunicorn app:app --bind 0.0.0.0:5000 --workers 1 --threads 4 --worker-class gthread --timeout 600

# Or for quick local testing only:
python app.py
Open http://localhost:5000

Deploy to Render
Option A — One-click with render.yaml (recommended)
Push this folder to a GitHub repository
Go to render.com → New → Blueprint
Connect your GitHub repo
Render reads render.yaml and deploys automatically
Option B — Manual Web Service
Push to GitHub
Render → New → Web Service → connect repo
Set:
Runtime: Docker
Dockerfile path: ./Dockerfile
Plan: Free (or higher)
Add environment variable:
DOWNLOAD_DIR = /tmp/downloads
Click Deploy
Important notes for Render Free Tier
Thing	Detail
Storage	/tmp is used — files are ephemeral and wiped on restart/redeploy
Persistent storage	Upgrade to a paid plan and add a Render Disk, then set DOWNLOAD_DIR to the disk mount path
FFmpeg	Installed via Dockerfile — works on all Render tiers
Timeout	Gunicorn timeout is 600s (10 min) — enough for most videos
Workers	Forced to 1 worker so the in-memory progress store is shared correctly
Threads	4 threads per worker — handles concurrent download + polling requests
File structure
├── app.py            # Flask backend
├── index.html        # Frontend UI
├── requirements.txt  # Python dependencies
├── Dockerfile        # Docker image with FFmpeg
├── render.yaml       # Render Blueprint config
└── .gitignore