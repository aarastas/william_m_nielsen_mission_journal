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
BASE_DIR = "/Users/adron/live/william_m_nielsen_mission_journal"
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
        return PAGE_LOGIN.replace("{{ERROR}}", "<p style='color:red'>Wrong password.</p>")
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

    html = (
        PAGE_REVIEW
        .replace("{{STEM}}", stem)
        .replace("{{CONTENT_JSON}}", content_json)
        .replace("{{INDEX}}", str(nav["index"] + 1))
        .replace("{{TOTAL}}", str(nav["total"]))
        .replace("{{STATUS}}", status_label)
        .replace("{{STATUS_CLASS}}", "reviewed" if reviewed else "pending")
        .replace("{{READONLY_ATTR}}", "readonly")
        .replace("{{EDIT_BTN_DISPLAY}}", "inline-block")
        .replace("{{SAVE_BTN_DISPLAY}}", "none")
        .replace("{{PREV_STEM}}", nav["prev"] or "")
        .replace("{{NEXT_STEM}}", nav["next"] or "")
        .replace("{{PREV_DISABLED}}", "" if nav["prev"] else "disabled")
        .replace("{{NEXT_DISABLED}}", "" if nav["next"] else "disabled")
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
        rows = "<p>No saved history yet for this page.</p>"

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
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:sans-serif;max-width:400px;margin:100px auto;text-align:center}
input{padding:8px;font-size:16px;margin:10px 0;width:100%;box-sizing:border-box}
button{padding:10px 20px;font-size:16px;background:green;color:white;border:none;cursor:pointer}</style>
</head><body>
<h2>Nielsen Journal — Family Review</h2>
{{ERROR}}
<form method="POST">
<input type="password" name="password" placeholder="Enter the family password" autofocus>
<button type="submit">Enter</button>
</form>
</body></html>
"""

HISTORY_ROW = """
<div class="hrow">
  <div>
    <b>{{LABEL}}</b><br>
    <span class="ts">{{ISO}}</span>
  </div>
  <div>
    <a href="/history/{{STEM}}/{{TS}}">View</a>
    <form method="POST" action="/revert/{{STEM}}/{{TS}}" style="display:inline"
          onsubmit="return confirm('Revert to this version? Current text will be saved to history first.');">
      <button type="submit">Revert to this</button>
    </form>
  </div>
</div>
"""

PAGE_HISTORY = """
<!DOCTYPE html><html><head><title>History — {{STEM}}</title>
<style>
  body{font-family:sans-serif;max-width:700px;margin:30px auto;padding:0 20px}
  .hrow{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #ddd;padding:12px 0}
  .ts{color:#666;font-size:13px}
  a.back{display:inline-block;margin-bottom:20px}
</style>
</head><body>
<a class="back" href="/page/{{STEM}}">&larr; Back to page {{STEM}}</a>
<h2>Version history — {{STEM}}</h2>
{{ROWS}}
</body></html>
"""

PAGE_HISTORY_VIEW = """
<!DOCTYPE html><html><head><title>Version {{TS}} — {{STEM}}</title>
<style>
  body{font-family:sans-serif;max-width:700px;margin:30px auto;padding:0 20px}
  pre{white-space:pre-wrap;font-family:Consolas,monospace;background:#f7f7f7;padding:15px;border-radius:6px}
  a.back{display:inline-block;margin-bottom:20px}
</style>
</head><body>
<a class="back" href="/history/{{STEM}}">&larr; Back to history</a>
<h3>{{STEM}} — version {{TS}}</h3>
<pre>{{CONTENT}}</pre>
</body></html>
"""

PAGE_REVIEW = """
<!DOCTYPE html><html><head><title>Old Man Willie Mission Journal — {{STEM}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">

<style>
  * { box-sizing: border-box; }
  html, body { height: -webkit-fill-available;; margin: 0; }
  body{font-family:sans-serif;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .topbar{background:#f0f0f0;padding:10px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
  .topbar .status{padding:2px 10px;border-radius:10px;font-size:12px;font-weight:bold}
  .status.reviewed{background:#d4edda;color:#155724}
  .status.pending{background:#fff3cd;color:#856404}
  .topbar a{margin-left:12px}

  .swipe-outer{flex:1;min-height:0;position:relative;overflow:hidden}
  .swipe-container{display:flex;height:100%;min-height:0;transition:transform .25s ease}
  .pane{flex-shrink:0;width:50%;height:100%;min-height:0;overflow:auto}
  .pane-image{background:#333;display:flex;align-items:flex-start;justify-content:center;padding:10px}
  .pane-image img{max-width:100%;height:auto}
  .pane-text{display:flex;flex-direction:column;min-height:0;padding:10px}
  textarea{flex:1;min-height:0;font-family:Consolas,monospace;font-size:16px;line-height:1.35;padding:10px;width:100%;height:100%;resize:none;white-space:pre;overflow-x:auto;overflow-y:hidden}
  textarea[readonly]{background:#fafafa;color:#333}

  .controls{position:relative;z-index:20;padding-top:10px;display:flex;justify-content:space-between;align-items:center}
  .controls .hint{font-size:12px;color:#777}
  button{padding:10px 18px;font-size:15px;border:none;cursor:pointer;border-radius:4px;margin-left:8px}
  button.save{background:green;color:white}
  button.edit{background:#0069d9;color:white}
  button.cancel{background:#aaa;color:white}

  /* Desktop side-arrow click zones */
  .side-arrow{position:fixed;top:0;bottom:0;width:50px;display:flex;align-items:center;justify-content:center;
              font-size:28px;color:rgba(0,0,0,0.25);cursor:pointer;z-index:10;user-select:none}
  .side-arrow:hover{color:rgba(0,0,0,0.5)}
  .side-arrow.left{left:0}
  .side-arrow.right{right:0}

  .mobile-nav{display:none}

  @media (max-width:800px){
    .swipe-container{width:200%}
    .pane{width:50%}
    .side-arrow{display:none}
    .mobile-nav{display:flex;justify-content:space-between;padding:10px;background:#f0f0f0}
    .mobile-nav {position:sticky;bottom:env(safe-area-inset-bottom)}
    .mobile-nav button{flex:1;margin:0 5px}
    .dots{text-align:center;padding:4px;background:#f0f0f0;font-size:12px;color:#888}
  }
  @media (min-width:801px){
    .dots{display:none}
  }
</style>
</head><body>

<div class="topbar">
  <span><b>{{STEM}}</b> &nbsp; (page {{INDEX}} of {{TOTAL}}) &nbsp;
    <span class="status {{STATUS_CLASS}}">{{STATUS}}</span>
  </span>
  <span>
    <a href="/history/{{STEM}}">History</a>
    <a href="/logout">Log out</a>
  </span>
</div>

<div class="dots" id="dots">Image &nbsp;&#9679;&#9675;&nbsp; Text — swipe to switch</div>

<div class="swipe-outer">
  <div class="side-arrow left" onclick="goPrev()">&#8249;</div>
  <div class="side-arrow right" onclick="goNext()">&#8250;</div>

  <div class="swipe-container" id="swipeContainer">
    <div class="pane pane-image">
      <img src="/image/{{STEM}}" alt="journal page">
    </div>
    <div class="pane pane-text">
      <form method="POST" action="/save/{{STEM}}" id="reviewForm" style="display:flex;flex-direction:column;height:100%">
        <textarea name="content" id="textArea" wrap="off" {{READONLY_ATTR}}></textarea>
        <div class="controls">
          <span class="hint">&larr; &rarr; or space to navigate (desktop)</span>
          <span>
            <button type="button" class="edit" id="editBtn"
                    style="display:{{EDIT_BTN_DISPLAY}}" onclick="enableEdit()">Edit</button>
            <button type="button" class="cancel" id="cancelBtn"
                    style="display:none" onclick="cancelEdit()">Cancel</button>
            <button type="submit" class="save" id="saveBtn"
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
          // Save current view before navigation
          if (isMobile()) localStorage.setItem("journalView", currentView);
          window.location = "/page/" + PREV_STEM;
      }
  }

  function goNext() {
      if (NEXT_STEM) {
          // Save current view before navigation
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
    cancelBtn.style.display = 'inline-block';
    saveBtn.style.display = 'inline-block';
  }
  function cancelEdit() {
    textArea.value = originalValue;
    textArea.readOnly = true;
    editBtn.style.display = 'inline-block';
    cancelBtn.style.display = 'none';
    saveBtn.style.display = 'none';
  }

  // ---- Fit font size so exactly 30 lines fill the textarea's height ----
  const LINES_PER_PAGE = 30;
  const MIN_FONT_PX = 10;
  const MAX_FONT_PX = 32;

  function fitFontToLines() {
    const availableHeight = textArea.clientHeight; // includes padding, box-sizing:border-box handles it
    if (!availableHeight) return;

    let lineHeightPx = availableHeight / LINES_PER_PAGE;
    let fontSizePx = (lineHeightPx / 1.35) * 0.97; // small safety margin — no vertical
                                                    // scrollbar to fall back on now, so
                                                    // slightly undersize rather than risk
                                                    // clipping the last line to rounding

    fontSizePx = Math.max(MIN_FONT_PX, Math.min(MAX_FONT_PX, fontSizePx));
    lineHeightPx = fontSizePx * 1.35;

    textArea.style.fontSize = fontSizePx + 'px';
    textArea.style.lineHeight = lineHeightPx + 'px';
  }

  window.addEventListener('load', fitFontToLines);
  window.addEventListener('resize', fitFontToLines);
  window.addEventListener('orientationchange', fitFontToLines);
  fitFontToLines(); // run immediately too, don't wait for full page load (image loading shouldn't delay text sizing)

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

  // ---- Mobile swipe: toggles image/text view, does NOT change page ----
  const swipeContainer = document.getElementById('swipeContainer');
  const dots = document.getElementById('dots');
  let currentView = 0; // 0 = image, 1 = text
  // Restore mobile view if present
  function restoreMobileView() {
    const savedView = localStorage.getItem("journalView");
    if (savedView !== null) {
      currentView = parseInt(savedView, 10);
      setView(currentView);
    }
  }

  window.addEventListener("load", () => {
    setTimeout(restoreMobileView, 50);   // iOS Safari needs this
  });

  let touchStartX = null;

  function isMobile() { return window.matchMedia('(max-width: 800px)').matches; }
  function setView(v) {
      currentView = Math.max(0, Math.min(1, v));
      // Save view for next page load (mobile only)
      if (isMobile()) {
         localStorage.setItem("journalView", currentView);
      }
      swipeContainer.style.transform = 'translateX(' + (-50 * currentView) + '%)';
      if (isMobile()) {
          dots.innerHTML = currentView === 0
              ? 'Image &nbsp;&#9679;&#9675;&nbsp; Text — swipe to switch'
              : 'Image &nbsp;&#9675;&#9679;&nbsp; Text — swipe to switch';
      }
  }


  document.querySelector('.swipe-outer').addEventListener('touchstart', function(e) {
    touchStartX = e.changedTouches[0].screenX;
  }, {passive: true});

  document.querySelector('.swipe-outer').addEventListener('touchend', function(e) {
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