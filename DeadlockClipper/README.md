# Deadlock Clipper

Turns your Deadlock recordings into vertical highlight clips (1080x1920) for YouTube Shorts and
Instagram Reels. Everything runs on your own PC: no accounts, no uploads, no paid services.

## First-time setup (about 5 minutes, once)

1. **Download the app.** On the GitHub page for this repository, make sure you're on the right
   branch, click the green **Code** button, then **Download ZIP**.
2. **Unzip it.** Right-click the downloaded ZIP and choose **Extract All...**. Put it somewhere
   permanent, for example `Documents\DeadlockClipper`. Don't run it from inside the ZIP.
3. **Run the installer.** Open the `DeadlockClipper` folder and double-click **`Install.bat`**.
   - If Windows says "Windows protected your PC", click **More info**, then **Run anyway**.
   - A black window shows the progress. It installs Python (if you don't have it), the app's
     parts, and ffmpeg (the video tool). Wait until it says **ALL DONE**.
4. A **Deadlock Clipper** shortcut now appears on your desktop. From now on, just double-click
   that. You never need the black window again.

To update or repair the app, download the new ZIP, extract it over the old folder, and run
`Install.bat` again.

## Using it

1. **Pick a video.** Drag a recording onto the window, click **Choose a video file...**, or paste
   a YouTube link (local files are better quality and faster).
2. **Settings.** Choose how many clips, how long, the vertical style (click the preview you like),
   and what counts as a highlight. Your choices are remembered for next time.
3. **Find highlights.** A 1-2 hour video takes a few minutes. Going back to the same video later
   is instant because the scan is saved.
4. **Review.** Each suggested clip shows a thumbnail, its time, a score and *why* it was picked.
   - **Play** opens a small player window with sound.
   - **Delete** removes clips you don't want.
   - Change **Start** / **End** to trim a clip.
   - Move **Fight sensitivity** and press **Re-pick clips** to get a different selection
     without re-scanning.
5. **Export.** Clips are saved as `deadlock_2026-09-25_clip01.mp4` and so on (the date comes from
   the OBS file name). The default folder is `Videos\Deadlock Clips`.

## How it picks highlights

| What | How it's detected |
|---|---|
| Your kill or assist | The red **KILL / KILL ASSIST** badge that pops up above your crosshair |
| Multikill | Two or more badges within 12 seconds |
| Teamfight | A kill badge during at least 5 seconds of loud game audio |
| Your death | The screen turning grey (death screen) |
| Creep farming | Ignored: loud, but no badge |

Loudness is compared with how loud *that* match normally is, so farming noise doesn't count
as a fight. By default only the louder parts of the video are scanned for kill badges, which
is much faster. Tick **Scan the whole video** if you think it misses quiet kills.

## If something goes wrong: logs

Every error message has an **Open logs folder** button. There's also one on the start screen.
The logs are in the **`logs`** folder inside the DeadlockClipper folder:

| File | What it's for |
|---|---|
| `logs\deadlock_clipper.log` | Main log. **Send this one when reporting a problem.** |
| `logs\last_analysis.txt` | What was detected in the last video (kill times, deaths, fights, chosen clips). Send this when a pick was good or bad, so detection can be tuned. |
| `logs\install.log` | Log from `Install.bat`, if setup failed. |
| `logs\crash.log` | Only exists if the app crashed hard. |

To share a log: open the file (it opens in Notepad), press **Ctrl+A** then **Ctrl+C**, and paste
it into the chat. If it's very long, the last 100 lines are usually enough.

## Not done yet

- **Big objective moments** (Guardian, Walker, Mid Boss) are greyed out until we have a
  screenshot of one to build detection from.
- The kill-badge position and size are estimated from a phone screenshot of a Short. They will
  be fine-tuned once we look at results from your real recordings.
