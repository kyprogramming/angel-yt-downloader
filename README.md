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
