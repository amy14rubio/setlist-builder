# Setup Guide

**Mac only.** This app drives Logic Pro and macOS notifications
directly, so it won't run on Windows or Linux.

## 1. Install the prerequisites

Open the Terminal app and install these with [Homebrew](https://brew.sh)
(if you don't have Homebrew yet, install it first by pasting the command
from that site):

```
brew install python@3.12 tesseract tesseract-lang terminal-notifier
```

| Package | What it's for |
|---|---|
| `python@3.12` | Runs the app itself (any Python 3.10+ works if you already have one). |
| `tesseract` + `tesseract-lang` | Reads the song titles off your photo/PDF (OCR), with Spanish support. |
| `terminal-notifier` | Sends the real macOS notification banners for scheduled tasks. |

## 2. Get the code and install its Python packages

```
git clone https://github.com/YOUR-USERNAME/setlist-builder.git
cd setlist-builder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(No `git`? Click the green "Code" button on the GitHub page, choose
"Download ZIP" instead, unzip it, and `cd` into that folder.) You'll
need to re-run `source venv/bin/activate` each time you open a new
Terminal window to work with this project.

## 3. Set up Google access (Drive + YouTube)

This is the one genuinely fiddly part, but it's a one-time setup.
Setlist Builder needs its own small, free Google Cloud "app
registration" to be allowed to read your Drive charts and update your
YouTube playlist:

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
   and sign in with the Google account whose Drive/YouTube you want to
   use.
2. Create a new project (top-left dropdown, then "New Project"). Any
   name works, e.g. "Setlist Builder."
3. Search for **"Google Drive API"** in the top search bar, open it,
   and click **Enable**. Do the same for **"YouTube Data API v3"**.
4. Go to **APIs & Services, Credentials** in the left sidebar, then
   **Create Credentials, OAuth client ID**.
   - First time only: it'll ask you to set up a "consent screen." Choose
     **External**, fill in an app name and your email, and save through
     the rest with default options.
   - For **Application type**, choose **Desktop app**, give it any
     name, and click **Create**.
5. Click **Download JSON** on the credential that appears. This is your
   "client secret" file. Save it somewhere you'll remember (e.g. a
   `SetlistBuilderCredentials` folder in Documents).
6. Repeat step 4 and 5 to create a **second** OAuth client ID and
   download a **second** JSON file. The app uses one for Drive and a
   separate one for YouTube.

> While your Google Cloud project is in "Testing" mode (the default,
> fine for personal use), Google requires you to sign in again about
> every 7 days. You don't have to track this: when a scheduled Drive
> or YouTube task is about half an hour from running and its login has
> gone stale, you get a notification with a **Log In** button that
> runs the sign-in right there, without opening the app.

## 4. Configure the app

Run the app once (see the "How to Run the App" section in `README.md`),
click the star icon to open Settings, and fill in:

- **Audio Library** / **Logic Library**: the folders containing your
  audio files and Logic Pro projects.
- **Desktop Output Folder**: where finished Logic Pro projects get
  built.
- **YouTube Client Secret File** / **Google Drive Client Secret File**:
  the two JSON files from Step 3.
- **Video Lookup Site**: an external website that lists songs alongside
  their YouTube videos, if you want the YouTube-matching feature to
  work. Blank by default. See the note in the main README about this
  feature's limits before spending time on it.
- **Google Drive Charts Folder ID**: the long ID at the end of your
  charts folder's Google Drive URL.
- **Charts Merged PDF Folder**: where the final merged setlist PDF gets
  saved.

The first time you use the Drive or YouTube features, a browser window
will open asking you to log in and approve access, a one-time step
(repeated every ~7 days in Testing mode, per the note above).

## 5. Grant macOS permissions

The first time the app tries to control Logic Pro or send a
notification, macOS will ask for permission under **System Settings,
Privacy & Security**, under **Accessibility**, **Automation**, and
**Notifications**. Approve each. Without these, Logic Pro automation
and scheduled notifications won't work.

---

See `README.md` for how to actually run the app day to day, and for
what to expect from each feature.
