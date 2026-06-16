# Installing NOCTURNE (ytm) on Windows — the no-experience guide

This is a music player that runs inside a black text window. It was built for
Linux, so on Windows we first turn on a small built-in Linux feature called
**WSL**, then install the player inside it. You only do this setup once.

**Be honest with yourself about time:** set aside about **30–40 minutes** the
first time. None of it is hard — you mostly copy a line, paste it, press Enter,
and wait. There's a restart in the middle. After this, opening the player is
two clicks.

You'll need: a Windows 10 or 11 computer, and (optional) to be logged into
**music.youtube.com** in your normal browser — that's only for pulling *your*
playlists and liked songs. You can skip it and still search and play anything.

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

You don't need to sign in to search and play music — press **n** and you're
done. Sign in only if you want **your** playlists and liked songs (you can
always do it later with `ytm --login`).

> **Important on Windows:** the "read my browser automatically" option can't
> see a browser that's installed on the Windows side — so on WSL you sign in by
> pasting one request from your browser. It sounds technical but takes about a
> minute.

If you want your library, press **Y**, choose **2) paste request headers**, and
follow along:

1. In your browser, open **music.youtube.com** (signed in) and click around
   once — e.g. open your **Library** — so the page loads fresh.
2. Press **F12** to open developer tools → click the **Network** tab → type
   `browse` in the filter box → click any **browse** row that appears in the
   list. (If the list is empty, click around the page again — it only records
   while it's open.)
3. Copy that request's headers as plain text:
   - **Firefox:** right-click the row → **Copy → Copy Request Headers**.
   - **Chrome / Edge:** in the panel on the right, open the **Headers** tab,
     scroll to the **Request Headers** section, and flip its little **Raw**
     toggle **on** — the text changes to `name: value` lines. Select all of it
     (click in, **Ctrl+A**) and **Ctrl+C**. (Newer Chrome removed the old
     one-click "Copy request headers", so the Raw toggle is the way.)
4. Back in the Ubuntu window, paste (**Ctrl+Shift+V**), press **Enter**, then
   press **Ctrl+D** on the empty line. It confirms "signed in".

Prefer not to copy cookies at all? Choose **3) Google sign-in** instead and
approve a code — a few more clicks, but it never touches your browser files.

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

## Optional — the full-quality visualizer

The player works fine in the default Ubuntu window, but you'll get the
block-character visualizer. Nocturne also has a **true pixel** visualizer —
crisp album art and a smooth plasma drawn as real pixels — that only appears in
a GPU terminal which speaks the "kitty graphics protocol". The easiest one on
Windows is **WezTerm**.

1. In the Ubuntu window, install it (this installs on the Windows side):
   ```sh
   winget.exe install --id wez.wezterm -e
   ```
   Click **Yes** if Windows asks. When it finishes, open **WezTerm** from the
   Start menu.
2. WezTerm opens a normal Windows shell. To get your Linux window, click the
   small **˅** arrow at the top-right and pick **Ubuntu** (WezTerm finds your
   WSL automatically).
3. In that tab, type `ytm`, press Enter, then press **p** to switch into pixel
   mode.

Run `ytm --doctor` any time — it tells you whether your current terminal
qualifies for the pixel renderer.

---

## If something goes wrong

- **"ytm: command not found"** — close the Ubuntu window and open a fresh one,
  then try `ytm` again. If it still says that, type the long version once:
  `~/.local/bin/ytm`

- **The installer said something was "missing"** — re-run the first paste block
  in Part 2 (the `sudo apt install` one), let it finish, then `cd ~/ytm-tui`
  and `./install.sh` again.

- **Sign-in couldn't find your login** — on Windows the automatic "read my
  browser" option doesn't work, because your browser lives on the Windows side,
  out of reach of the Linux player. Use the **paste request headers** method
  from Part 2 instead (works with any browser), or the **Google sign-in**
  option. Re-run it any time with `ytm --login`. And make sure you logged into
  **music.youtube.com** (not just youtube.com).

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
