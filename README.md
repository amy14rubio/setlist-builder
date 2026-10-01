# Setlist Builder

A desktop app for macOS that turns a church's weekly (or monthly) song
list into a finished Logic Pro rehearsal project, a merged PDF of chord
charts, and an updated YouTube playlist. All automatically, on a
schedule you set, with your approval before anything actually runs.

I built this to replace a recurring chunk of manual weekly prep,
matching each song by hand against my audio library, Logic Pro
projects, our Drive chord charts, and a YouTube reference playlist,
then building everything myself, with a review pass and a click. It's
built specifically around my own setup, so it won't run for anyone else
as-is, but if you've got a similar recurring "match a list of items
against several of your own libraries and services, then automate the
repetitive part" problem, every piece (Drive, YouTube, Logic Pro, the
OCR/matching logic) lives in its own separate file specifically so it
can be a useful starting point rather than something you have to use
exactly as built.

## What it actually does

- **Reads a song list from a photo or a PDF.** Point it at a screenshot
  of one week's list, or a whole month's PDF at once, and it pulls out
  the song titles automatically (OCR).
- **Matches each song** against your own audio library, your own Logic
  Pro project library, a Google Drive folder of chord charts, and a
  YouTube video source, using fuzzy title matching, so "Dios Es Bueno"
  still matches a file named "Dios es Bueno (En Vivo).wav".
- **Lets you review and fix anything wrong** before committing to it.
  Nothing runs unattended without you having seen the matches first.
- **Builds the actual Logic Pro project** by driving Logic Pro directly
  (copying audio into place, generating a handoff manifest, and
  automating the track/tempo setup). No Keyboard Maestro or paid
  automation tool required.
- **Downloads and merges chord charts** from Drive into one PDF, in
  setlist order.
- **Updates a YouTube playlist** to match the week's songs, using an
  external site (of your choosing, configured in Settings) to look up
  each song's video. See the note below on this feature's limits.
- **Schedules any of the above for later**, with a real macOS
  notification you can approve or decline right from the banner, or
  browse a Task Manager to see everything that's pending, reschedule
  it, or retry something that failed.
- **Keeps your Google logins from quietly breaking a scheduled run.**
  Google expires a personal-use login about every 7 days. Shortly
  before a scheduled task that needs one, the app checks it and, if
  it's gone stale, sends a notification with a **Log In** button that
  runs the sign-in on the spot. It only asks when something is
  actually about to run, so it isn't a standing nag.
- **Handles a whole month at once**: import one PDF, review each week,
  and bulk-schedule everything you've reviewed in one step. Each week
  keeps its own destinations, so the Sunday songs go to the Sunday
  playlist and folder, the Wednesday songs to Wednesday's, and so on.

(The YouTube video lookup needs an external site configured in
Settings, blank by default, and only works if that site lists songs in
a specific way. It's the least portable feature; everything else works
independently of it.)

## Setup

See **[SETUP.md](SETUP.md)** for the full install walkthrough
(Homebrew packages, Python environment, Google Drive/YouTube access,
and app configuration).

## How to Run the App

**Day to day:**

```
source venv/bin/activate
python main.py
```

**Skip the Terminal entirely:** run `bash scripts/build_launcher.sh`
once, and it builds a small double-clickable "Setlist Builder.app" in
your Applications folder that runs the two lines above for you from
then on.

**Background reminders even when the app is closed:** scheduled tasks
only notify you while the app checks for them. To get notified even
when it's not open, set up `scheduler_runner.py` to run on a recurring
schedule via macOS's `launchd`. See the comments at the top of that
file for exactly what it does. Both kinds of notification work fully
from there, approving a task and logging back in to Google alike,
without the app ever being open. This is an optional, one-time,
slightly more advanced step.

## Running the test suite

```
pip install -r requirements-dev.txt
pytest
```

## Project structure, briefly

- `app/models/`: plain data definitions (a song entry, a setlist, a
  scheduled task).
- `app/services/`: one file per real-world integration (Drive,
  YouTube, OCR, Logic Pro automation, the SQLite database, the
  scheduling/approval pipeline).
- `app/ui/`: the PySide6 (Qt) desktop interface.
- `scheduler_runner.py` / `notification_action_listener.py`: the
  background pieces that check for due tasks, check the Google logins
  those tasks are about to need, and make both notifications
  actionable, meant to run independently of the main app window.
  `scheduler_runner.py` also clears out task rows that already ran, so
  the database doesn't grow forever.
- `tests/`: the automated test suite.

## License

_(Not yet decided, see the project's to-do list.)_
