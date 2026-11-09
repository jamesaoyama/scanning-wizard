# Scanning Wizard 2026 (v4)

Turns a folder of scanned movie photos into eBay listings: straightened, numbered pictures in high and low res, pictures uploaded, titles written, and a CSV ready to import.

## Using it

1. Put the scans in the scans folder (by default `Desktop\Scanned Images For Wizard`).
2. Open Scanning Wizard, pick **SM**, **SO** or **MM**, and press **Start**.
3. It works through every step on its own:
   - **Check the scans.** Files that aren't pictures are skipped and named on screen.
   - **Straighten.** Sideways and upside-down photos are turned using face detection that runs on the PC, with no API.
   - **Number.** Photos become `SM110926_0001`, `_0002` … carrying on from the last number used that day.
   - **Save high res** to `Finished Images Wizard\high res 2026\SM110926\`. JPEG scans that don't need turning are copied exactly; others are saved as quality-95 JPEGs, keeping their DPI.
   - **Make low res** in `low res 2026\SM110926\` (800 px by default).
   - **Upload** the low res pictures by FTP, encrypted when the server allows it, and check that the picture links open over https.
   - **Find titles** using Google Vision, Google Lens, Amazon and the AI writers in your chosen order, then check each film in the free film database.
4. Check the titles. Photos marked yellow need a look:
   - Edit any field and the title rebuilds; typing your own title keeps it.
   - **Turn** fixes a wrongly straightened photo.
   - **Look again** re-runs the search for one photo.
   - **Include** switches a photo in or out of the CSV.
5. Press **Save CSV & finish**. The rows are added to `SM110926.csv`, and the scans go to the Recycle Bin (the high res copies stay).

Everything is saved as it goes. If the window is closed or something fails, **Continue** on the start screen picks up where it stopped.

## Installing on the shop PC

1. Download and run the installer (Windows 8.1 or newer, no Python needed):
   **https://github.com/jamesaoyama/scanning-wizard/releases/latest/download/ScanningWizardSetup.exe**

   It installs for the current user, with no admin prompt, and puts an icon on the desktop.
2. Open **Settings** and add:
   - **Upload:** the FTP username and password.
   - **Identify:** at least one AI key.

**Updates:** Scanning Wizard checks for a new version at most every 6 hours, or when you click **Check for updates**. When there is one, a bar offers **Update now**: it downloads the installer, checks its size and SHA-256 fingerprint, installs it and opens again. Settings and batches are kept.

**Running from the code instead:** install Python 3.10 or newer, then double-click **Start Scanning Wizard.bat**. The first start installs the packages it needs. A copy run this way can't update itself; **Update now** opens the download page instead.

Settings from v3 carry over:
- The keys move into Windows Credential Manager.
- Picture links switch from `http://@…` to `https://…`.
- Retired AI models are swapped for current ones.

## Keys

| Service | What it does | Where to get a key |
|---|---|---|
| Gemini | Writes titles (has a free tier) | aistudio.google.com |
| GPT | Writes titles | platform.openai.com |
| Claude | Writes titles; won't name people from their faces | platform.claude.com |
| Google Vision | Finds web pages showing the same photo | Google Cloud console, Cloud Vision API |
| Amazon Rekognition | Matches famous faces | AWS account, access key with Rekognition permission |
| Google Lens (SerpApi) | Finds similar pictures (needs working https links) | serpapi.com |

The Test button next to each key checks it. The start screen shows a rough cost per photo.

## Files

- `scanning_wizard.py`: the window.
- `wizard_core.py`: everything else (titles, images, services, the batch steps).
- `wizard_updates.py`: the update check and download. `wizard_selftest.py`: the self-test the Windows build runs.
- `installer/` and `.github/workflows/`: the Windows installer and its build. `assets/`: the icon.
- `models/`: the YuNet face model used for straightening (OpenCV Zoo, MIT licence).
- `scanning_wizard_v3_original.py`: the previous version, kept for reference.
- `Finished Images Wizard\wizard data\`: one file per batch, recording how far each photo got.
- Settings: `%USERPROFILE%\.scanning_wizard_config.json`. Log: `%USERPROFILE%\.scanning_wizard.log`.

## Tests

```
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

Optional settings for the test run:
- `WIZARD_FACE_PHOTO=<a one-face photo>` adds the straightening tests.
- `WIZARD_NETWORK_TESTS=1` checks real films against Wikidata.

## Publishing a new version

1. Set the new number in two places: `APP_VERSION` in `wizard_core.py` and `AppVersion` in `installer/ScanningWizard.iss`. A test checks that they match.
2. Commit and push to `main`. GitHub Actions then:
   - runs the tests on Windows;
   - builds the program and self-tests it;
   - builds the installer, installs it, and self-tests the installed copy.
3. When that run passes, download its `ScanningWizardSetup` artifact and publish it:

   ```
   gh release create v4.0.1 ScanningWizardSetup.exe --title "Scanning Wizard 4.0.1" --notes "What changed"
   ```

Installed copies see the new version within 6 hours.
