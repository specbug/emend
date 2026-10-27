# Mac validation and first real library/device review

Run this after reviewing and merging the release stack. The automated command
uses only original synthetic books, isolated Calibre configuration, disposable
libraries and a local rclone remote. It never opens your existing Calibre library,
contacts your Drive account, uploads books, or calls the Meta API.

## Prepare tools

Use Python 3.12+, uv, Java 21, Node.js 22+, and rclone. Calibre can be installed
normally in `/Applications/calibre.app`, or downloaded into an isolated cache:

```sh
brew install openjdk@21 rclone
export JAVA_HOME="$(brew --prefix openjdk@21)/libexec/openjdk.jdk/Contents/Home"
export PATH="$JAVA_HOME/bin:$PATH"
export EMEND_TOOLS_DIR="$HOME/Library/Caches/emend/tools"
export EMEND_ACE_DIR="$HOME/Library/Caches/emend/ace"
export PUPPETEER_CACHE_DIR="$HOME/Library/Caches/emend/puppeteer"
export UV_CACHE_DIR="$HOME/Library/Caches/emend/uv"
bash scripts/setup-cloud.sh
uv sync --frozen --extra visual
uv run --extra visual playwright install chromium
bash scripts/setup-ace.sh
```

Keep these exports in the shell used for verification. Homebrew's Java 21 is
keg-only, so `/usr/bin/java` alone may still report that no runtime is installed.
If npm reports that Puppeteer's install script was blocked, explicitly run the
browser installer for this installed dependency:

```sh
(cd "$EMEND_ACE_DIR/node_modules/puppeteer" && node install.mjs)
```

Complete setup steps sequentially: `setup-cloud.sh` syncs the base Python
environment, so run the visual dependency sync after it, before installing or
running Playwright.

For the isolated official Calibre build (checksum-verified, no `/Applications`
installation), also run:

```sh
export EMEND_CALIBRE_DIR="$HOME/Library/Caches/emend/calibre-9.17.0"
bash scripts/setup-calibre.sh
export EMEND_TEST_CALIBRE_BIN="$EMEND_CALIBRE_DIR/calibre.app/Contents/MacOS"
```

Otherwise set `EMEND_TEST_CALIBRE_BIN=/Applications/calibre.app/Contents/MacOS`.
The installer supports Apple silicon and Intel Macs; its disk image is the official
universal distribution. It temporarily mounts the image read-only and copies only
the application into the configured tools directory.

## Run the repeatable verification

```sh
export EMEND_TEST_EPUBCHECK_JAR="$EMEND_TOOLS_DIR/epubcheck-5.3.0/epubcheck.jar"
export EMEND_TEST_ACE="$EMEND_ACE_DIR/node_modules/.bin/ace-cli"
bash scripts/verify-local.sh --require-ace
```

This runs lint, formatting, the complete test suite and package builds. It prints
a temporary evidence directory containing test output and JUnit results. Retain
those locally when diagnosing failures; inspect logs for local paths before
sharing them. An integration failure is a failure. Without `--require-ace`, an
unconfigured Ace produces explicit skipped tests; that is a partial validation.
Calibre, EPUBCheck, Chromium and rclone are required by the script.

The combined EPUB 2/3 tests exercise container repair, evidence-backed metadata
edits, missing-cover creation, an actual browser typography comparison and
application, reading/visual evidence, recovery after corrupted activation,
no-overwrite reader-folder export, actual Calibre import, rich notes/annotations
and supplementary files, a full-library backup transferred with real rclone's
local backend, and restoration reopened through Calibre's own API. Separate tests
cover replacement covers, format retention, custom metadata, provider failures,
publication retries, process kills, stale/tampered evidence and rejected edits.
The local transport is reproducible evidence, not authenticated Drive validation.

## Review real books privately

1. Keep your active Calibre library local. Configure Emend in a different folder.
   Back up the whole Emend data folder separately with Emend closed (for example,
   using Time Machine); Calibre-library backups contain Calibre data, not Emend's
   preserved originals, state database or evidence history.
2. Start with copies of a few representative EPUBs. Import them through
   `emend review --config /path/to/config.yaml`. Inspect edition evidence, cover
   provenance, table of contents, marked notes and fonts. Apply only changes you
   approve. Try typography on samples before extending it to every spine chapter.
3. Open approved EPUBs in Calibre's viewer. Check the beginning/middle/end, cover
   proportions, headings, paragraph rhythm, verse, quotations, tables, images,
   chapter breaks, accented/non-Latin text, note links and return navigation.
   Repeat at several font sizes. Inspect unsampled chapters as well as screenshots.
4. Export to an explicitly mounted reader folder with `export-reader --to ...`,
   or configure `review --reader-directory ...`. Confirm the volume is mounted,
   verify the exported path, and safely eject. Check the same reading details on
   the XTEINK X4 Pro. Firmware behavior, pagination and font fidelity require this
   actual device; browser dimensions are not calibrated device profiles.
5. For Drive, configure an authenticated rclone remote privately and use a
   dedicated backup folder. Close Calibre and every other library writer before
   `backup --calibre-closed --to-drive`. Download one uploaded archive and
   run `verify-backup`, then `restore` into a **new** local directory.
   Open that restored library in Calibre and compare formats, covers, descriptions,
   tags, series, custom columns, author notes/resources, supplementary files and
   viewer annotations. Never restore over the active library or run it inside a
   live-synced Drive folder.

An entire rich-library snapshot preserves these objects together, so reinstalling
Calibre is followed by restoring the snapshot and selecting that library. There
is no Emend subscription or paid cloud service requirement; Google Drive/rclone
account configuration and available storage remain under your control.

Keep personal books, reference links, screenshots containing private content,
configuration and credentials out of the public repository and PR attachments.
Report reproducible problems with a synthetic/redacted example and the tool
versions. Automated validation does not establish visual quality, artwork/edition
identity, accessibility conformance or device compatibility on its own.
