#!/usr/bin/env python3
"""
Family Journal Review Server v2 — review/edit tool + read-only archive.

Deployed location: ~/live/journal-review/journal_review_server.py

Behavior:
  - Every page in TRANSCRIPT_DIR is browsable, reviewed or not.
  - Unreviewed pages: text is editable by default.
  - Reviewed pages: text is read-only by default, with an Edit button to
    unlock changes (for fixing something missed later).
  - Every overwrite of a reviewed page snapshots the prior version to
    HISTORY_DIR first (including the original AI draft, captured the very
    first time a page is reviewed) — so any page can be reverted to an
    earlier, more-trusted version.
  - Desktop: image left / text right, arrow-key + spacebar + on-screen
    side-arrow navigation between pages.
  - Mobile: swipe left/right switches between the image view and the text
    view (not between pages); explicit Prev/Next buttons change pages.

Run with waitress, not the Flask dev server:
    cd ~/live/journal-review
    source .venv/bin/activate
    python3 -m waitress --host=0.0.0.0 --port=5000 journal_review_server:app

Expose via Cloudflare Tunnel rather than port-forwarding:
    cloudflared tunnel --url http://localhost:5000
"""

import os
import json
import re
import shutil
from datetime import datetime, timezone
from io import BytesIO

from flask import Flask, request, redirect, url_for, session, send_file, Response
from PIL import Image

app = Flask(__name__)
app.secret_key = "CHANGE_ME_TO_SOMETHING_RANDOM"

# ---- Paths — consolidated under ~/live/, separate from the ~/dev/ pipeline ----
# Resolution order so the same file works in deployment AND for local testing:
#   1. JOURNAL_BASE_DIR env var, if set (explicit override)
#   2. the original deployment path, if it exists on this machine
#   3. otherwise the directory this script lives in (repo checkout — where the
#      images/ transcriptions/ reviewed/ history/ folders sit next to the code)
_DEFAULT_BASE = "/Users/adron/live/william_m_nielsen_mission_journal"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("JOURNAL_BASE_DIR") or (
    _DEFAULT_BASE if os.path.isdir(_DEFAULT_BASE) else _SCRIPT_DIR
)
TRANSCRIPT_DIR = os.path.join(BASE_DIR, "transcriptions")
JOURNAL_INPUTS = os.path.join(BASE_DIR, "images")
REVIEWED_DIR = os.path.join(BASE_DIR, "reviewed")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
REVIEW_PASSWORD = "Old Man Willie"  # share with family, change this to something real
MAX_IMAGE_DIMENSION = 1600
# -----------------------------------------------------------------

os.makedirs(REVIEWED_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)


# ---------- data helpers ----------

def normalize_indentation(text):
    """
    Any line that already has leading whitespace (spaces and/or tabs — an
    indented paragraph start) gets locked to exactly two spaces, regardless
    of whether the original was one space, several spaces, a tab, or a mix.
    Lines with zero leading whitespace are left flush-left. Blank/whitespace-
    only lines are left untouched (they're spacing, not indented content).
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = []
    for line in lines:
        if line.strip() == "":
            normalized.append(line)
            continue
        match = re.match(r"^([ \t]+)", line)
        if not match:
            normalized.append(line)
            continue
        rest = line[match.end():]
        normalized.append("  " + rest)
    return "\n".join(normalized)


def find_jpg_for(stem):
    for directory in (JOURNAL_INPUTS, TRANSCRIPT_DIR):
        for ext in (".JPG", ".jpg"):
            candidate = os.path.join(directory, stem + ext)
            if os.path.exists(candidate):
                return candidate
    return None


def all_stems():
    if not os.path.exists(TRANSCRIPT_DIR):
        return []
    return sorted(
        f[:-3] for f in os.listdir(TRANSCRIPT_DIR) if f.endswith(".md")
    )


def is_reviewed(stem):
    return os.path.exists(os.path.join(REVIEWED_DIR, stem + ".md"))


def current_text(stem):
    reviewed_path = os.path.join(REVIEWED_DIR, stem + ".md")
    if os.path.exists(reviewed_path):
        with open(reviewed_path, "r") as f:
            return normalize_indentation(f.read())
    transcript_path = os.path.join(TRANSCRIPT_DIR, stem + ".md")
    if os.path.exists(transcript_path):
        with open(transcript_path, "r") as f:
            return normalize_indentation(f.read())
    return ""


def nav_info(stem):
    stems = all_stems()
    if stem not in stems:
        return None
    idx = stems.index(stem)
    return {
        "index": idx,
        "total": len(stems),
        "prev": stems[idx - 1] if idx > 0 else None,
        "next": stems[idx + 1] if idx < len(stems) - 1 else None,
    }


def first_unreviewed_or_first_stem():
    stems = all_stems()
    for s in stems:
        if not is_reviewed(s):
            return s
    return stems[0] if stems else None


def history_dir_for(stem):
    d = os.path.join(HISTORY_DIR, stem)
    os.makedirs(d, exist_ok=True)
    return d


def snapshot_content(stem, content, label):
    d = history_dir_for(stem)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    with open(os.path.join(d, ts + ".md"), "w") as f:
        f.write(content)
    with open(os.path.join(d, ts + ".json"), "w") as f:
        json.dump({"timestamp": ts, "label": label,
                    "iso": datetime.now(timezone.utc).isoformat()}, f)
    return ts


def list_history(stem):
    d = os.path.join(HISTORY_DIR, stem)
    if not os.path.exists(d):
        return []
    versions = []
    for f in os.listdir(d):
        if f.endswith(".json"):
            with open(os.path.join(d, f), "r") as jf:
                meta = json.load(jf)
            versions.append(meta)
    return sorted(versions, key=lambda m: m["timestamp"], reverse=True)


def require_login():
    return session.get("logged_in") is True


# ---------- auth routes ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == REVIEW_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("root"))
        return PAGE_LOGIN.replace("{{ERROR}}", "<p class='error'>Wrong password.</p>")
    return PAGE_LOGIN.replace("{{ERROR}}", "")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- main routes ----------

@app.route("/")
def root():
    if not require_login():
        return redirect(url_for("login"))
    stem = first_unreviewed_or_first_stem()
    if not stem:
        return "<h2>No pages found.</h2>"
    return redirect(url_for("page", stem=stem))


@app.route("/page/<stem>")
def page(stem):
    if not require_login():
        return redirect(url_for("login"))

    nav = nav_info(stem)
    if nav is None:
        return Response("Page not found", status=404)

    reviewed = is_reviewed(stem)
    text = current_text(stem)
    # JSON-encode the content and inject it via JS (see below) instead of
    # embedding it directly inside <textarea>...</textarea>. Two reasons:
    # 1. Per the HTML spec, a leading newline right after <textarea> gets
    #    silently stripped by the browser's parser — any page whose text
    #    starts with a blank line would lose it before JS ever runs.
    # 2. Avoids needing to hand-escape </textarea> sequences in the content.
    content_json = json.dumps(text).replace("</", "<\\/")
    version_count = len(list_history(stem))
    if reviewed and version_count == 0:
        # Reviewed but no history entries — this page was reviewed before
        # history tracking existed (e.g. via the old desktop tool), so it
        # was clearly edited at least once even though we have no record
        # of that specific edit. Floor at v0.1 rather than show v0.0.
        version_count = 1
    status_label = ("Reviewed" if reviewed else "Not yet reviewed") + f" v0.{version_count}"
    progress_pct = round((nav["index"] + 1) / nav["total"] * 100) if nav["total"] else 0

    html = (
        PAGE_REVIEW
        .replace("{{STEM}}", stem)
        .replace("{{CONTENT_JSON}}", content_json)
        .replace("{{INDEX}}", str(nav["index"] + 1))
        .replace("{{TOTAL}}", str(nav["total"]))
        .replace("{{STATUS}}", status_label)
        .replace("{{STATUS_CLASS}}", "reviewed" if reviewed else "pending")
        .replace("{{READONLY_ATTR}}", "readonly")
        .replace("{{EDIT_BTN_DISPLAY}}", "inline-flex")
        .replace("{{SAVE_BTN_DISPLAY}}", "none")
        .replace("{{PREV_STEM}}", nav["prev"] or "")
        .replace("{{NEXT_STEM}}", nav["next"] or "")
        .replace("{{PREV_DISABLED}}", "" if nav["prev"] else "disabled")
        .replace("{{NEXT_DISABLED}}", "" if nav["next"] else "disabled")
        .replace("{{PROGRESS_PCT}}", str(progress_pct))
    )
    return html


@app.route("/image/<stem>")
def image(stem):
    if not require_login():
        return Response(status=403)
    safe_stem = os.path.basename(stem)
    path = find_jpg_for(safe_stem)
    if not path:
        return Response(status=404)

    img = Image.open(path).convert("RGB")
    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


@app.route("/save/<stem>", methods=["POST"])
def save(stem):
    if not require_login():
        return redirect(url_for("login"))

    stem = os.path.basename(stem)
    new_text = normalize_indentation(request.form["content"])

    reviewed_path = os.path.join(REVIEWED_DIR, stem + ".md")
    transcript_path = os.path.join(TRANSCRIPT_DIR, stem + ".md")

    if os.path.exists(reviewed_path):
        # Editing an already-reviewed page — snapshot what it was before
        # this edit overwrites it.
        with open(reviewed_path, "r") as f:
            snapshot_content(stem, f.read(), label="pre_edit")
    else:
        # First-ever review of this page — snapshot the raw AI draft first,
        # so the original machine output is always recoverable.
        if os.path.exists(transcript_path):
            with open(transcript_path, "r") as f:
                snapshot_content(stem, f.read(), label="original_ai_draft")

    with open(reviewed_path, "w") as f:
        f.write(new_text.rstrip("\n") + "\n")
    with open(transcript_path, "w") as f:
        f.write(new_text.rstrip("\n") + "\n")

    # Ensure the image is copied into the reviewed archive too (first time only)
    jpg_path = find_jpg_for(stem)
    reviewed_jpg = os.path.join(REVIEWED_DIR, stem + ".jpg")
    if jpg_path and not os.path.exists(reviewed_jpg):
        shutil.copy2(jpg_path, reviewed_jpg)

    return redirect(url_for("page", stem=stem))


@app.route("/history/<stem>")
def history(stem):
    if not require_login():
        return redirect(url_for("login"))

    versions = list_history(stem)
    rows = ""
    for v in versions:
        ts = v["timestamp"]
        rows += HISTORY_ROW.replace("{{STEM}}", stem) \
                            .replace("{{TS}}", ts) \
                            .replace("{{LABEL}}", v["label"]) \
                            .replace("{{ISO}}", v["iso"])
    if not rows:
        rows = ("<p class='empty'>No revisions yet. The first time you save this page, "
                "its original transcription is kept here so you can always compare or revert.</p>")

    html = PAGE_HISTORY.replace("{{STEM}}", stem).replace("{{ROWS}}", rows)
    return html


@app.route("/history/<stem>/<ts>")
def history_view(stem, ts):
    if not require_login():
        return redirect(url_for("login"))
    stem = os.path.basename(stem)
    ts = os.path.basename(ts)
    path = os.path.join(HISTORY_DIR, stem, ts + ".md")
    if not os.path.exists(path):
        return Response("Version not found", status=404)
    with open(path, "r") as f:
        content = f.read()
    html = PAGE_HISTORY_VIEW.replace("{{STEM}}", stem) \
                            .replace("{{TS}}", ts) \
                            .replace("{{CONTENT}}", content)
    return html


@app.route("/revert/<stem>/<ts>", methods=["POST"])
def revert(stem, ts):
    if not require_login():
        return redirect(url_for("login"))
    stem = os.path.basename(stem)
    ts = os.path.basename(ts)
    hist_path = os.path.join(HISTORY_DIR, stem, ts + ".md")
    if not os.path.exists(hist_path):
        return Response("Version not found", status=404)

    reviewed_path = os.path.join(REVIEWED_DIR, stem + ".md")
    transcript_path = os.path.join(TRANSCRIPT_DIR, stem + ".md")

    # Snapshot the current version before overwriting it with the reverted one
    if os.path.exists(reviewed_path):
        with open(reviewed_path, "r") as f:
            snapshot_content(stem, f.read(), label="pre_revert")

    with open(hist_path, "r") as f:
        reverted_content = f.read()

    with open(reviewed_path, "w") as f:
        f.write(reverted_content)
    with open(transcript_path, "w") as f:
        f.write(reverted_content)

    return redirect(url_for("page", stem=stem))


# ---------- templates ----------

PAGE_LOGIN = """
<!DOCTYPE html><html><head><title>Old Man Willie Mission Journal — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<style>
  :root{
    --paper:#ece4d1; --surface:#fbf8f1; --border:#d9cdb0; --ink:#262019; --ink-soft:#7d7263;
    --accent:#2f4d68; --accent-dark:#203548; --danger:#a3402b;
    --shadow-md:0 20px 44px rgba(38,32,25,.16); --radius:16px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --paper:#1b1812; --surface:#242019; --border:#3a3327; --ink:#ece4d1; --ink-soft:#a89b83;
      --accent:#8fb4d1; --accent-dark:#b7d1e6; --danger:#e08469;
      --shadow-md:0 20px 44px rgba(0,0,0,.55);
    }
  }
  *{box-sizing:border-box}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--paper); color:var(--ink); margin:0; min-height:100vh; min-height:100dvh;
    display:flex; align-items:center; justify-content:center; padding:24px;
    -webkit-font-smoothing:antialiased;
  }
  @media (prefers-reduced-motion: reduce){*{transition-duration:.01ms !important}}
  .card{
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    box-shadow:var(--shadow-md); padding:40px 34px; width:100%; max-width:370px; text-align:center;
  }
  .tag{
    display:inline-block; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:11px; letter-spacing:.08em; color:var(--ink-soft); border:1px solid var(--border);
    border-radius:999px; padding:3px 11px; margin-bottom:18px;
  }
  h1{
    margin:0 0 6px; font-size:29px; line-height:1.15; font-weight:600;
    font-family:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  }
  .sub{color:var(--ink-soft); font-size:14px; margin:0 0 24px}
  input{
    padding:13px 14px; font-size:16px; margin:0 0 14px; width:100%; border:1px solid var(--border);
    border-radius:10px; background:var(--paper); color:var(--ink);
  }
  input:focus-visible{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(47,77,104,.15)}
  button{
    padding:14px 20px; font-size:15px; font-weight:600; background:var(--accent); color:#fff;
    border:none; border-radius:10px; cursor:pointer; width:100%; transition:background .15s;
  }
  button:hover{background:var(--accent-dark)}
  button:focus-visible{outline:none; box-shadow:0 0 0 3px rgba(47,77,104,.35)}
  .error{color:var(--danger); font-size:13px; margin:0 0 14px}
</style>
</head><body>
<div class="card">
  <span class="tag">FAMILY ARCHIVE</span>
  <h1>Nielsen Mission Journal</h1>
  <p class="sub">Enter the family password to read and review pages.</p>
  {{ERROR}}
  <form method="POST">
    <input type="password" name="password" placeholder="Family password" autofocus>
    <button type="submit">Enter archive</button>
  </form>
</div>
</body></html>
"""

HISTORY_ROW = """
<div class="hrow">
  <div class="hrow-meta">
    <span class="hrow-label">{{LABEL}}</span>
    <span class="hrow-ts">{{ISO}}</span>
  </div>
  <div class="hrow-actions">
    <a class="btn ghost" href="/history/{{STEM}}/{{TS}}">View</a>
    <form method="POST" action="/revert/{{STEM}}/{{TS}}" style="display:inline"
          onsubmit="return confirm('Revert to this version? Current text will be saved to history first.');">
      <button type="submit" class="btn danger">Revert</button>
    </form>
  </div>
</div>
"""

PAGE_HISTORY = """
<!DOCTYPE html><html><head><title>History — {{STEM}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<style>
  :root{
    --paper:#ece4d1; --surface:#fbf8f1; --border:#d9cdb0; --ink:#262019; --ink-soft:#7d7263;
    --accent:#2f4d68; --danger:#a3402b; --danger-soft:#f5e2dc; --radius:14px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --paper:#1b1812; --surface:#242019; --border:#3a3327; --ink:#ece4d1; --ink-soft:#a89b83;
      --accent:#8fb4d1; --danger:#e08469; --danger-soft:#3a231d;
    }
  }
  *{box-sizing:border-box}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--paper); color:var(--ink); max-width:720px; margin:0 auto; padding:28px 20px 60px;
    -webkit-font-smoothing:antialiased;
  }
  @media (prefers-reduced-motion: reduce){*{transition-duration:.01ms !important}}
  a:focus-visible,button:focus-visible{outline:none; box-shadow:0 0 0 3px var(--border); border-radius:9px}
  .back{display:inline-flex; align-items:center; gap:6px; color:var(--ink-soft); text-decoration:none; font-size:14px; margin-bottom:18px}
  .back:hover{color:var(--ink)}
  h2{
    margin:0 0 6px; font-size:26px; font-weight:600;
    font-family:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  }
  .lede{color:var(--ink-soft); font-size:14px; margin:0 0 22px}
  .hrow{
    display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    padding:14px 16px; margin-bottom:10px;
  }
  .hrow-label{display:block; font-weight:600; font-size:14px}
  .hrow-ts{display:block; color:var(--ink-soft); font-size:12px; margin-top:2px}
  .hrow-actions{display:flex; gap:8px}
  .btn{
    display:inline-flex; align-items:center; padding:8px 14px; border-radius:9px; font-size:13px;
    font-weight:600; text-decoration:none; border:none; cursor:pointer;
  }
  .btn.ghost{background:var(--paper); color:var(--ink); border:1px solid var(--border)}
  .btn.ghost:hover{background:var(--border)}
  .btn.danger{background:var(--danger-soft); color:var(--danger)}
  .btn.danger:hover{opacity:.85}
  .empty{color:var(--ink-soft); font-size:14px}
</style>
</head><body>
<a class="back" href="/page/{{STEM}}">&larr; Back to page {{STEM}}</a>
<h2>Version history</h2>
<p class="lede">Every saved revision of page {{STEM}}, newest first. Revert restores an earlier version and keeps the current text in history.</p>
{{ROWS}}
</body></html>
"""

PAGE_HISTORY_VIEW = """
<!DOCTYPE html><html><head><title>Version {{TS}} — {{STEM}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<style>
  :root{
    --paper:#ece4d1; --surface:#fbf8f1; --border:#d9cdb0; --ink:#262019; --ink-soft:#7d7263; --radius:14px;
  }
  @media (prefers-color-scheme: dark){
    :root{ --paper:#1b1812; --surface:#242019; --border:#3a3327; --ink:#ece4d1; --ink-soft:#a89b83; }
  }
  *{box-sizing:border-box}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--paper); color:var(--ink); max-width:720px; margin:0 auto; padding:28px 20px 60px;
    -webkit-font-smoothing:antialiased;
  }
  @media (prefers-reduced-motion: reduce){*{transition-duration:.01ms !important}}
  a:focus-visible{outline:none; box-shadow:0 0 0 3px var(--border); border-radius:9px}
  .back{display:inline-flex; align-items:center; gap:6px; color:var(--ink-soft); text-decoration:none; font-size:14px; margin-bottom:18px}
  .back:hover{color:var(--ink)}
  h3{
    margin:0 0 14px; font-size:22px; font-weight:600;
    font-family:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  }
  pre{
    white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    background:var(--surface); border:1px solid var(--border); color:var(--ink);
    padding:18px; border-radius:var(--radius); line-height:1.5; font-size:14px;
  }
</style>
</head><body>
<a class="back" href="/history/{{STEM}}">&larr; Back to history</a>
<h3>{{STEM}} — version {{TS}}</h3>
<pre>{{CONTENT}}</pre>
</body></html>
"""

PAGE_REVIEW = """
<!DOCTYPE html><html><head><title>Old Man Willie Mission Journal — {{STEM}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">

<style>
  :root{
    --paper:#ece4d1; --surface:#fbf8f1; --surface-2:#f2ead9; --border:#d9cdb0;
    --ink:#262019; --ink-soft:#7d7263;
    --accent:#2f4d68; --accent-dark:#203548; --accent-soft:#dfe6ec;
    --reviewed-bg:#dcefe1; --reviewed-text:#2f6b46;
    --pending-bg:#f3e6c2; --pending-text:#8a6a1c;
    --danger:#a3402b;
    --shadow-sm:0 1px 2px rgba(38,32,25,.10);
    --shadow-md:0 10px 28px rgba(38,32,25,.16);
    --radius:14px; --radius-sm:9px;
    --font-body:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --font-display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
    --font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --focus:0 0 0 3px var(--accent-soft);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --paper:#1b1812; --surface:#242019; --surface-2:#2b261d; --border:#3a3327;
      --ink:#ece4d1; --ink-soft:#a89b83;
      --accent:#8fb4d1; --accent-dark:#b7d1e6; --accent-soft:#2a3945;
      --reviewed-bg:#203a29; --reviewed-text:#7cd39a;
      --pending-bg:#3a2f16; --pending-text:#e3b563;
      --danger:#e08469;
      --shadow-sm:0 1px 2px rgba(0,0,0,.4);
      --shadow-md:0 10px 28px rgba(0,0,0,.5);
    }
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    height:100vh; height:100dvh;
    font-family:var(--font-body);
    background:var(--paper); color:var(--ink);
    display:flex; flex-direction:column; overflow:hidden;
    -webkit-font-smoothing:antialiased;
  }
  /* Modern baseline: visible keyboard focus + honor reduced-motion. */
  :focus-visible{outline:none; box-shadow:var(--focus); border-radius:var(--radius-sm)}
  @media (prefers-reduced-motion: reduce){
    *{transition-duration:.01ms !important; animation-duration:.01ms !important}
  }

  /* ---------- topbar ---------- */
  .topbar{
    background:var(--surface); border-bottom:1px solid var(--border);
    padding:12px 18px; display:flex; justify-content:space-between; align-items:center;
    gap:12px; flex-wrap:wrap;
  }
  .topbar-left{display:flex; align-items:baseline; gap:12px; min-width:0}
  .wordmark{
    font-family:var(--font-display); font-size:19px; font-weight:600; letter-spacing:.01em;
    color:var(--ink); white-space:nowrap; line-height:1;
  }
  .wordmark .sep{color:var(--ink-soft); font-weight:400; margin:0 2px}
  .tag{
    font-family:var(--font-mono);
    font-size:12px; letter-spacing:.03em; font-weight:600; color:var(--ink-soft);
    border:1px solid var(--border); border-radius:999px; padding:2px 9px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:38vw;
  }
  .pagecount{font-size:12px; color:var(--ink-soft); white-space:nowrap}
  .status{padding:3px 10px; border-radius:999px; font-size:11px; font-weight:700; letter-spacing:.02em; white-space:nowrap}
  .status.reviewed{background:var(--reviewed-bg); color:var(--reviewed-text)}
  .status.pending{background:var(--pending-bg); color:var(--pending-text)}
  .topbar-right{display:flex; align-items:center; gap:4px}
  .iconlink{
    display:inline-flex; align-items:center; gap:6px; padding:7px 12px;
    border-radius:var(--radius-sm); font-size:13px; color:var(--ink-soft);
    text-decoration:none; border:1px solid transparent; transition:.15s;
  }
  .iconlink:hover{background:var(--surface-2); color:var(--ink); border-color:var(--border)}

  .progress{height:3px; background:var(--border)}
  .progress-bar{height:100%; background:var(--accent); transition:width .3s ease}

  /* ---------- stage ---------- */
  .stage{flex:1; min-height:0; position:relative; overflow:hidden}
  .viewport{display:flex; height:100%; min-height:0; transition:transform .3s cubic-bezier(.4,0,.2,1)}
  .pane{flex-shrink:0; width:50%; height:100%; min-height:0}

  .pane-image{
    background:#131110; display:flex; flex-direction:column; min-height:0;
  }
  /* Shared media area: both panes use .pane-media so their heights match. */
  .pane-media{
    flex:1; min-height:0; position:relative;
    display:flex; align-items:center; justify-content:center;
    padding:18px; overflow:hidden;
  }
  .pane-image img{
    max-width:100%; max-height:100%; width:auto; height:auto; display:block;
    border-radius:6px; box-shadow:0 12px 32px rgba(0,0,0,.5);
  }
  /* Empty footer under the image mirrors the text pane's controls row so both
     panes reserve identical vertical space and their media areas stay equal. */
  .pane-footer{height:52px; flex-shrink:0}

  .nav-arrow{
    position:absolute; top:50%; transform:translateY(-50%);
    width:44px; height:44px; border-radius:50%; border:none; cursor:pointer;
    background:rgba(19,17,16,.55); color:#fff; font-size:20px; line-height:1;
    display:flex; align-items:center; justify-content:center;
    backdrop-filter:blur(3px); -webkit-backdrop-filter:blur(3px);
    transition:background .15s; z-index:5;
  }
  .nav-arrow:hover{background:rgba(19,17,16,.85)}
  .nav-arrow:disabled{opacity:0; pointer-events:none}
  .nav-arrow-left{left:12px}
  .nav-arrow-right{right:12px}

  .pane-text{display:flex; flex-direction:column; min-height:0; background:var(--paper)}
  /* Text media area mirrors the image media area (same height, centered
     content, same 18px padding) so a textarea whose height is set to the
     rendered image height lines up with the image. */
  .pane-text .pane-media{align-items:center; justify-content:center; padding:18px 14px}
  textarea{
    width:100%; height:100%; min-height:0;
    font-family:var(--font-mono);
    font-size:16px; line-height:1.35; padding:0 16px; resize:none; white-space:pre;
    overflow-x:auto; overflow-y:hidden;
    border:1px solid var(--border); border-radius:var(--radius);
    background:var(--surface); color:var(--ink); box-shadow:var(--shadow-sm);
  }
  textarea[readonly]{background:var(--surface-2); color:var(--ink-soft)}
  textarea:focus-visible{outline:none; border-color:var(--accent); box-shadow:var(--focus)}

  .controls{height:52px; flex-shrink:0; padding:0 18px 0 14px; display:flex; justify-content:space-between; align-items:center; gap:10px}
  .hint{font-size:12px; color:var(--ink-soft)}
  .btn{
    display:inline-flex; align-items:center; gap:6px; padding:10px 16px; font-size:14px; font-weight:600;
    border:none; border-radius:var(--radius-sm); cursor:pointer; transition:.15s; box-shadow:var(--shadow-sm);
  }
  .btn:active{transform:translateY(1px)}
  .btn.save{background:var(--accent); color:#fff}
  .btn.save:hover{background:var(--accent-dark)}
  .btn.edit{background:var(--surface); color:var(--ink); border:1px solid var(--border)}
  .btn.edit:hover{background:var(--surface-2)}
  .btn.cancel{background:transparent; color:var(--ink-soft); box-shadow:none}
  .btn.cancel:hover{color:var(--danger)}

  /* ---------- mobile tabs + bottom nav ---------- */
  .mobile-tabs{display:none}
  .mobile-nav{display:none}

  @media (max-width:800px){
    .viewport{width:200%}
    .pane{width:50%}
    .nav-arrow{display:none}
    .wordmark{display:none}       /* too wide for a phone topbar */
    .topbar{padding:10px 14px}

    .mobile-tabs{
      display:flex; justify-content:center; padding:8px 0; background:var(--surface);
      border-bottom:1px solid var(--border);
    }
    .tabpill{display:inline-flex; background:var(--surface-2); border-radius:999px; padding:3px; gap:2px}
    .tabpill button{
      border:none; background:transparent; padding:8px 26px; border-radius:999px; font-size:13px;
      font-weight:600; color:var(--ink-soft); cursor:pointer; transition:.15s;
      min-height:36px;
    }
    .tabpill button.active{background:var(--accent); color:#fff; box-shadow:var(--shadow-sm)}

    .mobile-nav{
      display:flex; justify-content:space-between; gap:10px; padding:10px 14px;
      padding-bottom:calc(10px + env(safe-area-inset-bottom));
      background:var(--surface); border-top:1px solid var(--border);
    }
    .mobile-nav button{
      flex:1; min-height:48px; padding:12px; font-size:15px; font-weight:600;
      border-radius:var(--radius-sm);
      border:1px solid var(--border); background:var(--surface-2); color:var(--ink); cursor:pointer;
    }
    .mobile-nav button:disabled{opacity:.35}
    .pane-media{padding:10px}
    textarea{padding:0 12px}
    .tag{max-width:40vw}
  }
</style>
</head><body>

<div class="topbar">
  <div class="topbar-left">
    <span class="wordmark">Nielsen Mission Journal</span>
    <span class="tag">p. {{STEM}}</span>
    <span class="pagecount">{{INDEX}} of {{TOTAL}}</span>
    <span class="status {{STATUS_CLASS}}">{{STATUS}}</span>
  </div>
  <div class="topbar-right">
    <a class="iconlink" href="/history/{{STEM}}">History</a>
    <a class="iconlink" href="/logout">Log out</a>
  </div>
</div>
<div class="progress"><div class="progress-bar" style="width:{{PROGRESS_PCT}}%"></div></div>

<div class="mobile-tabs">
  <div class="tabpill">
    <button type="button" id="tabImage" class="active" onclick="setView(0)">Scan</button>
    <button type="button" id="tabText" onclick="setView(1)">Transcription</button>
  </div>
</div>

<div class="stage">
  <div class="viewport" id="viewport">
    <div class="pane pane-image">
      <div class="pane-media">
        <button class="nav-arrow nav-arrow-left" onclick="goPrev()" aria-label="Previous page" {{PREV_DISABLED}}>&#8249;</button>
        <img src="/image/{{STEM}}" id="pageImage" alt="Journal page {{STEM}}">
        <button class="nav-arrow nav-arrow-right" onclick="goNext()" aria-label="Next page" {{NEXT_DISABLED}}>&#8250;</button>
      </div>
      <div class="pane-footer"></div>
    </div>
    <div class="pane pane-text">
      <form method="POST" action="/save/{{STEM}}" id="reviewForm" style="display:flex;flex-direction:column;height:100%;min-height:0">
        <div class="pane-media">
          <textarea name="content" id="textArea" wrap="off" {{READONLY_ATTR}}></textarea>
        </div>
        <div class="controls">
          <span class="hint">Use &larr; &rarr; or space to move between pages</span>
          <span>
            <button type="button" class="btn edit" id="editBtn"
                    style="display:{{EDIT_BTN_DISPLAY}}" onclick="enableEdit()">Edit</button>
            <button type="button" class="btn cancel" id="cancelBtn"
                    style="display:none" onclick="cancelEdit()">Cancel</button>
            <button type="submit" class="btn save" id="saveBtn"
                    style="display:{{SAVE_BTN_DISPLAY}}">Save</button>
          </span>
        </div>
      </form>
    </div>
  </div>
</div>

<div class="mobile-nav">
  <button onclick="goPrev()" {{PREV_DISABLED}}>&larr; Prev Page</button>
  <button onclick="goNext()" {{NEXT_DISABLED}}>Next Page &rarr;</button>
</div>

<script>
  const PREV_STEM = "{{PREV_STEM}}";
  const NEXT_STEM = "{{NEXT_STEM}}";

  function goPrev() {
    if (PREV_STEM) {
      if (isMobile()) localStorage.setItem("journalView", currentView);
      window.location = "/page/" + PREV_STEM;
    }
  }
  function goNext() {
    if (NEXT_STEM) {
      if (isMobile()) localStorage.setItem("journalView", currentView);
      window.location = "/page/" + NEXT_STEM;
    }
  }

  // ---- Edit lock toggle ----
  const textArea = document.getElementById('textArea');
  textArea.value = {{CONTENT_JSON}};  // set here, not embedded in HTML, to
                                       // preserve a leading blank line if
                                       // the page's transcription starts
                                       // with one (see server-side comment)
  const editBtn = document.getElementById('editBtn');
  const cancelBtn = document.getElementById('cancelBtn');
  const saveBtn = document.getElementById('saveBtn');
  let originalValue = textArea.value;

  function enableEdit() {
    originalValue = textArea.value;
    textArea.readOnly = false;
    textArea.focus();
    editBtn.style.display = 'none';
    cancelBtn.style.display = 'inline-flex';
    saveBtn.style.display = 'inline-flex';
  }
  function cancelEdit() {
    textArea.value = originalValue;
    textArea.readOnly = true;
    editBtn.style.display = 'inline-flex';
    cancelBtn.style.display = 'none';
    saveBtn.style.display = 'none';
  }

  /* --------------------------------------------------------------------------
   * TRANSCRIPTION FONT SIZING — why this exists and how it works
   * --------------------------------------------------------------------------
   * Goal: the transcription should read like the journal page it mirrors, so we
   * size the font such that LINES_PER_PAGE (30, the ruled lines on a physical
   * page) fill the available vertical space exactly. The measurement target
   * differs by layout because the two layouts show different things:
   *
   *   DESKTOP (panes side by side): the scan and the text are BOTH visible, so
   *   they must line up. We measure the image AS RENDERED — aspect-fit shrinks
   *   it below its pane on tall/narrow windows — set the textarea's border-box
   *   to that exact height, and fit 30 lines into it. Result: text line N sits
   *   at the same height as image line N.
   *
   *   MOBILE (one pane at a time): only ONE of image/text is on screen; the
   *   other is translated off-screen and can report height 0, so the image is
   *   NOT a reliable measurement target. Line-for-line alignment is also moot
   *   because you never see both at once. So we fit 30 lines to the on-screen
   *   TEXT PANE's own height instead, giving a stable, full-height read.
   *
   * Vertical padding on the textarea is 0 by design (see CSS) so its content
   * box equals its border-box height; horizontal padding is read live because
   * it differs between the desktop and mobile breakpoints.
   * ------------------------------------------------------------------------ */
  const pageImage = document.getElementById('pageImage');
  const LINE_HEIGHT_RATIO = 1.35;
  const LINES_PER_PAGE = 30;
  const MIN_FONT_PX = 10;
  const MAX_FONT_PX = 32;

  function fitText() {
    // Choose the measurement target for this layout (see block comment above).
    let targetHeight;
    if (isMobile()) {
      textArea.style.height = ''; // drop any desktop-set pixel height; CSS fills the pane
      targetHeight = textArea.getBoundingClientRect().height; // on-screen, fills its pane
    } else {
      targetHeight = pageImage.getBoundingClientRect().height; // rendered scan height
      if (targetHeight) textArea.style.height = targetHeight + 'px'; // match the scan
    }
    if (!targetHeight) return; // not laid out yet

    // Subtract the textarea's own border+padding so 30 lines fit the CONTENT box.
    const cs = getComputedStyle(textArea);
    const chrome = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth)
                 + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const contentHeight = targetHeight - chrome;

    let fontSizePx = (contentHeight / LINES_PER_PAGE) / LINE_HEIGHT_RATIO;
    fontSizePx = Math.max(MIN_FONT_PX, Math.min(MAX_FONT_PX, fontSizePx));

    textArea.style.fontSize = fontSizePx + 'px';
    textArea.style.lineHeight = (fontSizePx * LINE_HEIGHT_RATIO) + 'px';
  }

  // The image's rendered size isn't known until it decodes, so re-fit on its
  // load event — not just window load — plus resize/orientation changes.
  // setView() also calls fitText() when switching to the text tab on mobile.
  if (pageImage.complete) fitText();
  pageImage.addEventListener('load', fitText);
  window.addEventListener('load', fitText);
  window.addEventListener('resize', fitText);
  window.addEventListener('orientationchange', fitText);

  // ---- Keyboard navigation (desktop) — ignored while actively editing ----
  document.addEventListener('keydown', function(e) {
    const editing = (document.activeElement === textArea) && !textArea.readOnly;
    if (editing) return; // let normal typing/cursor movement happen

    if (e.key === 'ArrowLeft') { goPrev(); }
    else if (e.key === 'ArrowRight' || e.key === ' ') {
      e.preventDefault(); // stop space from scrolling the page
      goNext();
    }
  });

  // ---- Mobile tabs / swipe: toggles image/text view, does NOT change page ----
  const viewport = document.getElementById('viewport');
  const tabImage = document.getElementById('tabImage');
  const tabText = document.getElementById('tabText');
  let currentView = 0; // 0 = image, 1 = text
  let touchStartX = null;

  function isMobile() { return window.matchMedia('(max-width: 800px)').matches; }

  function setView(v) {
    currentView = Math.max(0, Math.min(1, v));
    if (isMobile()) {
      localStorage.setItem("journalView", currentView);
      viewport.style.transform = 'translateX(' + (-50 * currentView) + '%)';
      tabImage.classList.toggle('active', currentView === 0);
      tabText.classList.toggle('active', currentView === 1);
      if (currentView === 1) fitText(); // text pane just became visible — size it now
    }
  }

  // Restore last-viewed pane (image/text) on mobile after navigating pages
  function restoreMobileView() {
    const savedView = localStorage.getItem("journalView");
    if (savedView !== null) { setView(parseInt(savedView, 10)); }
  }
  window.addEventListener('load', function() {
    setTimeout(restoreMobileView, 50); // iOS Safari needs this
  });

  document.querySelector('.stage').addEventListener('touchstart', function(e) {
    touchStartX = e.changedTouches[0].screenX;
  }, {passive: true});

  document.querySelector('.stage').addEventListener('touchend', function(e) {
    if (touchStartX === null || !isMobile()) return;
    const deltaX = e.changedTouches[0].screenX - touchStartX;
    if (Math.abs(deltaX) > 50) {
      if (deltaX < 0) setView(currentView + 1); // swipe left -> next view
      else setView(currentView - 1);            // swipe right -> prev view
    }
    touchStartX = null;
  }, {passive: true});

  window.addEventListener('resize', function() { setView(currentView); });
</script>

</body></html>
"""

# Run with: python3 -m waitress --host=0.0.0.0 --port=5000 journal_review_server:app
