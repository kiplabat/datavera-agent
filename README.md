# Datavera local agent

> **New (beta): the Datavera Agent desktop app.** A window instead of the command line: chats, projects, memory,
> skills, plugins, scheduled tasks, and models you download and run on your own computer (CPU or GPU).
> This dataset publisher is built in as the Datavera plugin. Developers: see [app/README.md](app/README.md) and
> [engine/README.md](engine/README.md). The steps below are for the command-line version, which keeps working.

A helper that runs **on your own computer**. Each time you start it, it:

1. picks a subject (for example "glacier mass balance"),
2. searches the web in a browser window and finds a public data table it is allowed to reuse,
3. downloads the table, checks it and works out the numbers,
4. makes charts and a cover picture,
5. publishes it to your Datavera account. The Datavera team checks it first if your account is moderated.

The AI that makes the choices runs on your computer (with a free program called Ollama). You don't pay for
an AI service. The agent still needs the internet to search, download data and talk to Datavera.

Full illustrated guide: **https://www.datavera.io/publish/agent**

---

## What you need

| | |
|---|---|
| Computer | Windows 10 or 11, **or** a Mac (Mac support is new and not tested yet) |
| Memory | 8 GB at least, 16 GB is better |
| Free disk space | 10 GB |
| Internet | Yes. The first setup downloads about 4 GB |
| Datavera account | Free, at https://www.datavera.io |

---

## Step 1 - Make your secret key on Datavera

1. Go to **https://www.datavera.io** and sign in.
2. Open **Account → API Keys** (https://www.datavera.io/account?section=apikey).
3. Tick **Read** and **Publish**.
4. Press **Generate key** (or **Regenerate key**), then **Copy key**.
5. Keep the page open. The key starts with `dv_sk_`, and you will paste it in Step 2.

> ⚠️ The key is like a password. Never post it anywhere.
> If you already had a key, making a new one switches the old one off, so other programs that used it need the new key.

---

## Step 2 - Install (one time, about 15-30 minutes)

### Windows

1. Press the **Windows key**, type **PowerShell**, and press **Enter**. A blue or black window opens.
2. Copy this whole line, then **right-click** inside the PowerShell window to paste it, and press **Enter**:

   ```
   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/kiplabat/datavera-agent/main/install-windows.ps1 | iex"
   ```

3. Wait. It installs Python, Node.js and Ollama if you don't have them.
   If Windows asks *"Do you want to allow this app to make changes?"*, click **Yes**.
4. It downloads the AI model (about 3.5 GB), which is the slow part.
5. When it asks for your **API key**, right-click to paste it and press **Enter**.
   **Nothing appears when you paste.** That is normal, because it keeps the key hidden.
6. Answer the two short questions (who to publish as, and which subjects). Pressing **Enter** picks the
   suggested answer.
7. At the end it opens a web page once as a test, then says **All done!**

### Mac (not tested yet)

1. Press **Cmd + Space**, type **Terminal**, and press **Enter**.
2. Paste this line and press **Enter**:

   ```
   curl -fsSL https://raw.githubusercontent.com/kiplabat/datavera-agent/main/install-mac.sh | bash
   ```

3. If it asks for your **Mac password** (the one you log in with), type it. Nothing appears while you type,
   and that is normal. Then press **Enter**.
4. Paste your API key when asked and answer the two questions, the same as on Windows.

---

## Step 3 - Run it

Double-click the **Datavera Agent** icon on your desktop.

- A window shows each step as it happens. A private browser window opens and closes by itself, so don't
  click inside it.
- One run takes about **10-20 minutes**. Keep the computer **awake, plugged in and online**.
- At the end it shows the title of the new dataset. Type **O** and press **Enter** to open it on Datavera.
- Sometimes it finds nothing usable. It says so, and the next run tries a different subject.

Your datasets are listed on Datavera under **Account → Datasets**. A moderated account shows them as
*Pending review* until the Datavera team approves them.

---

## Everyday questions

**Can I choose the subject?** Yes. Open PowerShell (or Terminal) and type:

Windows (PowerShell):

```
cd $HOME\DataveraAgent
powershell -ExecutionPolicy Bypass -File .\run-agent.ps1 --topic "honey bee colony losses"
```

Mac (Terminal):

```
cd ~/DataveraAgent
./run-agent.sh --topic "honey bee colony losses"
```

**Can I stop it?** Close its window. Nothing breaks, and the next run starts fresh.

**How do I update it?** Paste the same install line again. Your key, settings and history are kept.

**How do I change the key, the account or the subjects?** In PowerShell type
`cd $HOME\DataveraAgent` then `.\.venv\Scripts\python.exe setup_account.py`
(Mac: `cd ~/DataveraAgent && .venv/bin/python setup_account.py`).

**Can it publish as my company?** Yes. If your account belongs to a company, the setup asks which one.

**Does it publish without asking?** Yes, that is the default. Moderated accounts still go through
Datavera's review. If the agent sees something it is unsure about (for example a row that might be a
total, or years in the future), it stops at a **draft** and tells you. To always stop at a draft, add
`"stop_after_default": "draft"` to `agent_config.json`.

**How do I remove it?** Delete the `DataveraAgent` folder in your user folder and the desktop icon.
Python, Node.js and Ollama stay installed; remove them in *Settings → Apps* if you like. Delete your key
on datavera.io (Account → API Keys → Regenerate) if you won't use it again.

---

## If something goes wrong

| You see | Do this |
|---|---|
| `winget is missing` | Open the **Microsoft Store**, install **App Installer**, then paste the install line again. |
| `That key did not work` | Copy the key again (all of it), or make a new one with **Read** and **Publish** ticked. |
| `This key can't publish` | Make a new key with **both** Read and Publish ticked. |
| `Ollama did not start` | Restart the computer and paste the install line again. |
| `the browser helper did not start` | Check the internet connection, then double-click the icon again. |
| `No page offered a tabular file...` | Normal. Run again later; it remembers what it tried. |
| `Every usable file found was already tried` | It refused to make a duplicate. Run again for a new subject. |
| Anything else | Open `DataveraAgent\outputs\runs\` and the newest folder; send `SUMMARY.md` and `progress.log` to support. |

---

## Privacy and safety

- Your key is stored only on your computer, in the file `DataveraAgent\.env`.
- The AI model runs on your computer. Web searches go through DuckDuckGo or Bing in a private browser window.
- It only uses data whose page states a reuse licence (such as CC BY 4.0, CC0, ODbL or a public-domain
  statement). It names the original publisher and links to the source on every dataset.
- Every number in a dataset is calculated from the downloaded file. AI text with numbers that don't match
  the file is replaced with plain computed sentences.

Technical details: [TECHNICAL.md](TECHNICAL.md).
