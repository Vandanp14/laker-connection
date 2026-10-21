# Laker Connection

Write your Laker Connection check-in notes once, in a spreadsheet or in a simple web page. This tool then types them into Starfish's "Add Appointment" form for you, one resident at a time, with the right date, time, location, reason, SpeedNotes and notes. You stop clicking through the same form over and over.

It runs on your own Mac. Your residents and notes stay on your Mac.

## Before you start

You need:

- A Mac.
- Google Chrome ([download it here](https://www.google.com/chrome/) if you don't have it).

That's it. The installer sets up everything else.

## Please read

- **This is an unofficial tool.** It is not made by, or connected to, SUNY Oswego or Starfish.
- **You're responsible for what it submits** under your Starfish account. Always do a **dry run** first (explained below) and look over what it's about to submit.
- If you're not sure your hall allows tools like this, **ask your RHD or supervisor** first.
- Your residents' names, emails and notes are saved only in a folder on your Mac (`LakerConnection/data`). They are never uploaded to GitHub or anywhere else.

## Install (one time, about 2 minutes)

1. Open **Terminal**. It's in **Applications → Utilities → Terminal**, or press **Cmd+Space**, type `Terminal` and press Enter.
2. Copy this whole line, paste it into Terminal, and press **Enter**:

   ```
   curl -fsSL https://raw.githubusercontent.com/Vandanp14/laker-connection/main/install.sh | bash
   ```

3. You'll see a few lines starting with `==>` while it sets things up. It doesn't ask for your Mac password.
4. A **new Chrome window** opens on the Starfish page. This is a separate Chrome just for this tool. Your normal Chrome, tabs and bookmarks aren't touched.
5. **Log into Starfish in that window**, including the Duo step, until you see your calendar.
6. Go back to Terminal and press **Enter**.
7. The Laker Connection dashboard opens in your browser.

A **Laker Connection** icon is also added to your Desktop.

## Every day after that

1. Double-click **Laker Connection** on your Desktop. (It's also in your home folder → `LakerConnection`.)
2. A Terminal window opens. It quickly checks for updates, then opens the automation Chrome and the dashboard.
3. **Leave that Terminal window open** while you use the tool.
4. When you're done, close the Terminal window. That stops everything.

If Starfish logs you out, log back in inside the automation Chrome window. That's the one that opened with Laker Connection.

## Add your residents

**Option A: in the dashboard.** Open **Manage roster**, type a name and their Oswego email (or ID), and click **Add**.

**Option B: from a spreadsheet (faster for a whole floor).**

1. In the dashboard, open **Import from a spreadsheet** and click **download template** next to *Residents*.
2. Open the file in Google Sheets, Numbers or Excel. Keep the first row (`full_name,email_or_id`) exactly as it is. Replace the example rows with your residents, one per row.
3. Save it as CSV:
   - **Google Sheets:** File → Download → Comma-separated values (.csv)
   - **Numbers:** File → Export To → CSV…
   - **Excel:** File → Save As… → File Format: CSV UTF-8
4. Back in the dashboard, choose that file under *Residents* and click **Import residents**.

If any line has a problem, **nothing is imported**. You'll see which line to fix, then you import again. Residents already on your list are skipped, so importing the same file twice is safe.

## Add your check-ins

This is the main part: one row per conversation you had with a resident.

**Option A: in the dashboard.** Use **Log a check-in** (Quick, Same-day batch, or Bulk paste).

**Option B: from a spreadsheet.** It works the same as residents, using the *Check-ins* template:

| Column | What to put | Example |
|---|---|---|
| `full_name` | The resident's name, exactly as on your roster | `Jane Doe` |
| `date` | Month-day-year with dashes | `10-01-2026` |
| `start_time` | 24-hour time, **or leave it blank** to start right after the previous conversation that day | `14:00` (= 2:00 pm) |
| `location` | One of your locations (see Settings) | `In- Residence Halls` |
| `notes` | What you talked about. This becomes the appointment note in Starfish | `Talked about midterms` |

Each conversation is 15 minutes long. The tool won't let two conversations overlap, and it allows **one check-in per resident per month**.

## Submit to Starfish

1. In **Ready to submit**, tick the check-ins you want to send.
2. Choose the **Reason** (Laker Connection #1, #2 or #3) and the **Default location**.
3. Under **SpeedNotes for this batch**, tick at least one topic. To give one resident different topics, click **Change** on their row.
4. Leave **Dry run** ticked and click **Run dry test**. The tool fills in each form in the automation Chrome window, then clicks *Never Mind*, so nothing is sent. Watch it work.
5. If it looked right, untick **Dry run** and click **Submit to Starfish**.

Before every real Submit, the tool checks that Sharing is set to *Shared*, that "Student missed appointment" and "Send a copy of note to student" are unticked, and that exactly the SpeedNotes you chose are ticked. If anything is off, that row is **not** submitted, and you'll see why.

## Settings

Click **Settings** at the top of the dashboard to change your list of locations or your default location. Each location must match a "Where" option in Starfish exactly. **Reset to the standard locations** puts the original two back.

## Where your data lives

Everything you enter is saved in **`LakerConnection/data`** in your home folder. To open it: in Finder, choose **Go → Go to Folder…**, type `~/LakerConnection/data`, and press Enter.

- `residents.csv`: your roster
- `checkins.csv`: your check-ins
- `submission_log.csv`: what has been submitted (don't edit this one)

To back up, copy that folder somewhere safe. Updates never change it.

## Something went wrong?

| You see | What to do |
|---|---|
| Google Chrome isn't installed… | Install Chrome from google.com/chrome, then paste the install line again. |
| Couldn't download Laker Connection. Check your internet connection… | Check your Wi-Fi and paste the install line again. |
| Couldn't install uv… / Couldn't set up Python… / Couldn't install libraries… | Usually Wi-Fi. Try again on a stable connection. |
| Couldn't download the latest version (are you online?). Starting the version you already have. | Nothing to do. You'll get the update next time you're online. |
| Laker Connection isn't fully installed… | Paste the install line from the top of this page into Terminal again. |
| Laker Connection is already running — opening it. | It was already open. Use the existing window. |
| Another app is using port 5050… / …port 9222… | Restart your Mac, then double-click Laker Connection. |
| Chrome didn't start in automation mode… | Find the Laker Connection Chrome window, press **Cmd+Q** in it, and double-click Laker Connection again. |
| The dashboard didn't start… / The dashboard stopped while starting… | Close the Terminal window and double-click Laker Connection again. If it keeps happening, send the person who shared this a screenshot of the Terminal window. |
| Your data was saved by a newer version of Laker Connection… | Paste the install line again to update. |
| Not logged in to Starfish… | Log into Starfish in the automation Chrome window, then submit again. |
| Could not connect to Chrome… | Close the Terminal window and double-click Laker Connection again. |
| SpeedNote '…' not found in Starfish / SpeedNotes mismatch | Starfish renamed a topic. Tell the person who shared this tool, so it can be updated for everyone. |
| Nothing was imported | Fix the lines listed (wrong name, date format, overlapping times, two check-ins in one month…) and import again. |
| A row says "not found in dropdown" | That name, time, or location doesn't match what Starfish has. Check spelling against Starfish. |

Screenshots of failed rows are saved in `~/LakerConnection/data/logs/failures/`.

Every run also writes a log file to `~/LakerConnection/data/logs/runs/` (the newest 20 are kept). If something goes wrong, send the newest `run-….log` file to the person who shared this tool. It shows what happened and the exact error.

## Updating

Updates are automatic. Each time you double-click Laker Connection, it gets the latest version first.

## Uninstall

Delete these three things:

- the `LakerConnection` folder in your home folder (this also deletes your data, so back it up first if you want it)
- the `laker-chrome-automation` folder in your home folder
- the **Laker Connection** icon on your Desktop
