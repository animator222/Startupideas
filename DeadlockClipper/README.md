# Deadlock Clipper

Turns your Deadlock recordings (horizontal 1920x1080 or vertical 1080x1920) into highlight clips:
vertical 1080x1920 for YouTube Shorts and Instagram Reels, or horizontal 1920x1080 for YouTube. Everything runs on your own PC: no accounts, no uploads, no paid services.

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

**Updating:** close the app and double-click **`Update.bat`** in the DeadlockClipper folder.
It downloads the latest version and keeps your settings. The version number is shown at the
top-right of the start screen. If something is broken, run `Install.bat` again to repair.

## Using it

1. **Pick a video.** Drag a recording onto the window, click **Choose a video file...**, or paste
   a YouTube link (local files are better quality and faster).
2. **Settings.** Choose how many clips (or tick **All highlights** for no limit), how long,
   **Export as** vertical (Shorts/Reels) or horizontal (YouTube), and the look (click a preview):
   **Full frame**, **Zoomed gameplay** (Zoom slider 110-250%) or **My layout**, and what counts as
   a highlight. Your choices are remembered for next time.
   - **My layout:** click **Edit my layout...** to build your own look once in the editor (see
     below) - e.g. zoomed gameplay, your webcam in a corner and your item bar at the bottom.
     Every clip then starts with it. Saved separately for each video shape and export shape.
   - **Kill badge area:** the app needs to know where your red KILL badge appears on screen. It
     has defaults for normal horizontal recordings and for your vertical layout. If kills are
     missed, press **Set kill badge area...**, find a moment with a kill, draw a box around the
     badge and press **Check this frame**. It's saved separately for vertical and horizontal
     videos, so you only do it once per layout.
3. **Find highlights.** The whole video is checked for your kill badge. The first scan of a
   1-2 hour video takes roughly 5-10 minutes; going back to the same video later is instant
   because the scan is saved.
4. **Review.** Each suggested clip shows a thumbnail of the finished look, its time, a score and
   *why* it was picked. Tick the clips you want (**Select all** / **Select none**); only ticked
   clips are exported. **Delete** removes a clip, **Re-pick clips** re-chooses with a different
   fight sensitivity without re-scanning, **Back to settings** goes back.
   - **Play / Edit** (or click the thumbnail) opens the **editor** in its own window (it can be
     maximised). Closing it (X, Esc or Cancel) just discards your changes.
     - **Canvas** (centre) shows the **finished clip**, live while it plays. **Source video**
       shows the original recording.
     - **Layers** (left; top of the list = in front, untick to hide):
       - **Gameplay** - drag it to move, drag a corner to resize, mouse wheel to zoom. Quick
         start buttons: *Full frame* / *Zoomed*.
       - **+ Video** adds another part of the recording (webcam, item bar, minimap...): mark
         *which part* on **Source video** by dragging/resizing its box, then on **Canvas** move it
         anywhere and resize it.
       - **+ Text** adds text: pick a font (Anton, Bebas Neue, Bangers, Luckiest Guy, Montserrat,
         Oswald, Poppins, Russo One, plus Impact/Arial Black if installed), colour, size, bold,
         **shadow** (colour, strength) and **outline** (colour, width). Drag it on the canvas.
       - Bring forward / send backward, duplicate, delete.
     - **Properties** (right) shows the settings of the selected layer.
     - Playback: play/pause, 5 s back/forward, frame by frame, mute. The **timeline** shows the
       clip as a yellow box: drag its edges to trim, or use **Set start here (I)** /
       **Set end here (O)**. **Play clip** plays just the trimmed part.
     - **Apply layout to all clips** gives every clip these layers (each keeps its own text);
       **Save as my layout** makes it the default for future clips (without this clip's text).
     - Keyboard: Space play/pause, arrows 1 s, Shift+arrows one frame, I/O start/end, Del delete layer.
5. **Export.** Clips look exactly like the editor preview. They are saved as `deadlock_2026-09-25_clip01.mp4` and so on (the date comes from
   the OBS file name). The default folder is `Videos\Deadlock Clips`. **Back to clips** returns to
   the list to export more.

## How it picks highlights

| What | How it's detected |
|---|---|
| Your kill or assist | The red **KILL / ASSIST** badge that pops up above your crosshair - the current badge is found by the mint-green souls amount ("$186") inside it, older badges by their white text |
| Multikill | Two or more badges within 12 seconds |
| Teamfight | A kill badge during at least 5 seconds of loud game audio |
| Your death | The screen turning grey (death screen) |
| Creep farming | Ignored: loud, but no badge |
| Buy menu | Ignored: the shop's pale panel is recognised, and badges within 4 seconds of an open shop don't count (some shop icons are red with white symbols, like the kill badge) |

**Clip length follows the action.** Each clip starts when the fight starts (the game audio
picks up before your kill) and ends when it calms down after your last kill. Kills up to about
20 seconds apart go into one clip. Your *Clip length* setting is the allowed range: quick
pick-offs are padded up to the minimum, and long fights are cut to the maximum.

By default **only moments with your kill/assist badge** become clips. Tick *Also include loud
fights where no kill was seen* if you also want big fights you weren't credited in (this can
pick up creep farming).

Loudness is compared with how loud *that* match normally is, so farming noise doesn't count
as a fight. **Quick scan** (off by default) only checks the louder parts of the video for kill
badges. It's faster, but it can miss kills during laning, so leave it off unless you're in a hurry.

**See what was detected** (on the review screen) opens a picture of every moment that looked
like your kill badge, with its time. Green numbers counted as kills; orange ones were close but
not counted. Paste that picture into the chat if kills are missing or wrong.

## If something goes wrong: logs

Every error message has an **Open logs folder** button. There's also one on the start screen.
The logs are in the **`logs`** folder inside the DeadlockClipper folder:

| File | What it's for |
|---|---|
| `logs\deadlock_clipper.log` | Main log. **Send this one when reporting a problem.** |
| `logs\detections.jpg` | Picture of everything that looked like your kill badge. Paste it into the chat when kills are missed or wrong. |
| `logs\last_analysis.txt` | What was detected in the last video (kill times, deaths, fights, chosen clips). Send this when a pick was good or bad, so detection can be tuned. |
| `logs\install.log` | Log from `Install.bat`, if setup failed. |
| `logs\update.log` | Log from `Update.bat`, if updating failed. |
| `logs\crash.log` | Only exists if the app crashed hard. |

To share a log: open the file (it opens in Notepad), press **Ctrl+A** then **Ctrl+C**, and paste
it into the chat. If it's very long, the last 100 lines are usually enough.

## Not done yet

- **Big objective moments** (Guardian, Walker, Mid Boss) are greyed out until we have a
  screenshot of one to build detection from.
- The kill-badge position and size are estimated from a phone screenshot of a Short. They will
  be fine-tuned once we look at results from your real recordings.
