# Installing NOCTURNE (ytm) on Windows — the no-experience guide

This is a music player that runs inside a black text window. It was built for
Linux, so on Windows we first turn on a small built-in Linux feature called
**WSL**, then install the player inside it. You only do this setup once.

**Be honest with yourself about time:** set aside about **30–40 minutes** the
first time. None of it is hard — you mostly copy a line, paste it, press Enter,
and wait. There's a restart in the middle. After this, opening the player is
two clicks.

You'll need: a Windows 10 or 11 computer, and to be logged into
**music.youtube.com in Firefox** at some point (for your playlists — but you
can skip that and still play any song).

A note on the copy/paste lines below: to **paste** into the black window, click
it once and press **Ctrl+Shift+V** (or just right-click). Normal Ctrl+V doesn't
work there.

---

## Part 1 — Turn on Linux (WSL)

1. Click the **Start** menu (Windows logo, bottom-left).
2. Type the word **powershell**.
3. In the list, you'll see **Windows PowerShell**. **Right-click it** and choose
   **Run as administrator**. A window pops up asking "Do you want to allow this
   app to make changes?" — click **Yes**.
4. A dark blue window opens. Click in it, then type this line and press **Enter**:

   ```
   wsl --install
   ```

5. It downloads for a few minutes. When it finishes it will say something like
   "The requested operation is successful" and ask you to restart.
6. **Restart your computer** (Start → power icon → Restart).

7. After it restarts, a black window titled **Ubuntu** opens by itself and says
   "Installing, this may take a few minutes." Let it finish.
8. It then asks you to **create a username**: type a simple lowercase name (like
   your first name, no spaces) and press Enter.
9. It asks for a **password**: type one and press Enter. **The screen won't show
   anything as you type — that's normal, it's hidden.** Type it again to
   confirm. **Write this password down**, you'll need it occasionally.

You now have Linux. This black **Ubuntu** window is where everything happens
from here. If you ever close it, reopen it from Start → type **Ubuntu**.

---

## Part 2 — Install the music player

Copy the block below **all at once**, paste it into the Ubuntu window
(Ctrl+Shift+V), and press Enter. It installs the pieces the player needs.

```sh
sudo apt update && sudo apt install -y git mpv ffmpeg python3 python3-venv python3-pip pulseaudio-utils curl
```

- It will ask for your **password** (the one from Part 1). Type it (hidden) and
  press Enter.
- It prints a lot of text and takes a few minutes. That's normal. Wait until
  you get a fresh line that ends with your name and a `$`.

Next, get the player itself and run its installer — paste this block and Enter:

```sh
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
git clone https://github.com/jprouhana/nocturne ~/ytm-tui
cd ~/ytm-tui
./install.sh
```

The installer sets everything up and at the end asks **"sign in now? [Y/n]"**.

- To get **your** playlists and liked songs: first make sure you're **logged
  into music.youtube.com in Firefox on Windows**, then press **Y** and Enter,
  and choose **1) firefox**. It finds your login automatically.
- Don't care about that / not using Firefox? Just press **n**. You can still
  search and play any song. (You can sign in later anytime by typing
  `ytm --login`.)

---

## Part 3 — Listen

Close the Ubuntu window and open it again (Start → **Ubuntu**). Now just type:

```sh
ytm
```

and press Enter. The player opens.

- Press **/** then type a song name and press **Enter** to search.
- Use the **up/down arrows** to pick a result, **Enter** to play.
- **Spacebar** pauses. **v** changes the visualizer. **q** quits.

The full list of keys is along the bottom of the window and in the main
[README](../README.md).

---

## If something goes wrong

- **"ytm: command not found"** — close the Ubuntu window and open a fresh one,
  then try `ytm` again. If it still says that, type the long version once:
  `~/.local/bin/ytm`

- **The installer said something was "missing"** — re-run the first paste block
  in Part 2 (the `sudo apt install` one), let it finish, then `cd ~/ytm-tui`
  and `./install.sh` again.

- **Sign-in couldn't find your login** — make sure you actually logged into
  **music.youtube.com** (not just youtube.com) **in Firefox**, leave that tab
  open, then run `ytm --login` and pick firefox again.

- **The music plays but the visualizer bars don't dance** — Windows' Linux
  layer sometimes can't "hear" the audio for the spectrum. The player notices
  and switches the visualizer to its own built-in animation instead, so it
  still looks alive. The `drop` style (press **v** until you reach it) always
  moves regardless.

- **Sound doesn't play at all** — make sure you're on **Windows 11** or a fully
  updated **Windows 10**; audio in WSL needs a recent version. Restarting the
  computer once after setup also clears this up most of the time.

- **Totally stuck?** Send me a photo of the window and I'll tell you the next
  line to type.
