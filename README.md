# Udyam Digital
**More business. Less figuring it out.**

A mobile-first web app for the supplied Accel Business / Udyam Digital challenge. Owners open a link, describe their goals, build a budget-conscious digital setup, verify documents, and track their order. Partners get the context to resolve the next blocker.

## Start
Python 3.9+ is required. The core app has no Python package dependencies.

```sh
python3 launch.py
```

The launcher opens **http://localhost:8000** in your browser. On Mac, you can also double-click `Start Udyam.command`; on Windows with the Python launcher, use `Start Udyam.bat`. For server-only use, run `python3 app.py`. Stop with Ctrl+C. Set `PORT` to use another port. The server deliberately binds to loopback; this is a local demonstration, not a public deployment.

```sh
python3 -m unittest -v
```

45 automated checks cover prices and taxes, eligibility, dependencies, budget tradeoffs, real image extraction, registry checks, consent, data isolation, withdrawal and the complete order lifecycle. OCR checks require a local OCR engine.

## A complete demonstration
1. Open **Business advisor → Try an example → A growing clinic**.
2. Review the four-service plan. Change quantities, switch to Essentials only, or explore add-ons. Every change is validated by the server. The quote shows monthly GST, setup GST, first invoice and individual contract terms. Download a printable HTML quote.
3. Continue to Verification. Give consent, then choose **Try the K02 demo business**.
4. Use the three **K02 specimen** buttons. These read the actual supplied images through OCR. K02's rotated certificate is deskewed for field alignment; no answer is inferred from its filename.
5. Review and reserve the demo plan. No payment is taken or legal contract entered.
6. Open **demo operations** from the tracker. Add a review note and approve verification, schedule an installation date with a note, then record simulated activation. The owner tracker shows the actual recorded events.
7. Ask a partner for help to create a persisted contextual handoff. Explore Acquisition insights to inspect the 5,000-lead historical funnel and callback priorities.

English and Hindi are supported for the main owner journey. Partner analytics and detailed technical check messages remain in English. Listen uses the browser's available speech engine/voices; no microphone is requested. This is a responsive web app, not a native Android/iOS app.

## Real local OCR
The included `tools/ocr` binary was compiled for this Mac. Source is included. To rebuild on a Mac with Apple command-line tools:

```sh
swiftc tools/ocr.swift -o tools/ocr
```

Apple Vision runs on device. Text positions are corrected for rotation. A dark-text pass reduces interference from the specimen's coloured watermark. This improves extraction; it is **not** an authenticity or tamper detector.

On Linux or Windows, install Tesseract and ensure `tesseract` is on PATH. Its plain-text fallback is supported, but layout extraction may be less accurate than Apple Vision. The rest of the journey works without OCR. Uploads accept PNG/JPEG up to 8 MB; temporary files are deleted after extraction. Fields and review results remain in the local workspace.

## Gemini advisor
Run `python3 configure_gemini.py` to enter your own Gemini API key without echoing it. This writes a local `.env` excluded from Git and release archives. Start with `python3 launch.py`, which loads that file. You may instead export `GEMINI_API_KEY` and optionally `GEMINI_MODEL`. The verified default is `gemini-3.5-flash-lite`.

On Business advisor, opt into Gemini separately from KYC. Describe the business in chat. Gemini asks follow-up questions, extracts a structured needs profile and proposes catalogue products with quantities and reasons. The server validates all prices, taxes, eligibility, dependencies and user/device coverage. It can defer non-essential items to respect the budget. AI cannot approve verification or activate an order.

The cloud request contains only the current needs message and allowlisted sizing, goals, budget and current-tool categories. Common ID/contact patterns are redacted, but this cannot guarantee anonymity. Keep personal names, contact information, addresses and identifiers out of chat. Documents and stored registry records never enter the Gemini request. Google processes requests under the configured account terms. No zero-retention claim is made.

Gemini failures are shown explicitly. The previous plan remains saved; owners may retry or switch to labelled guided mode. Turning Gemini off prevents further cloud requests. No API key is included in browser responses, source, ZIPs or releases.

Reference: https://ai.google.dev/api/generate-content

## What works / what is simulated
| Capability | Implementation |
| --- | --- |
| Recommendations | Real goals, employee/device/site sizing, city tier, tax-inclusive budget, catalogue dependencies, phase-two deferrals |
| Quote | Server-calculated, editable quantities/add-ons, GST estimates, setup costs, first invoice and downloadable quote |
| GST/Udyam | Exact lookup against supplied synthetic registries; GST format/checksum validation accepts synthetic X entity type |
| KYC | Actual local image OCR; registration status, legal-name/business matching, address matching, utility-bill freshness, signatory field/expiry checks |
| Consent/privacy | Session-scoped records; masked IDs and hashes; immediate workspace withdrawal; 24-hour inactivity cleanup |
| Orders | Persistent SQLite records, idempotent reservation, ordered review/scheduling/activation transitions, audit notes |
| Partner desk | Actual dataset funnel/segment economics; explainable callback priority; persisted contextual handoffs |
| Provider integrations | Explicit local simulation. No live installation, payment, government API, DigiLocker, e-sign or outbound messaging |

Document dates use **15 July 2026**, as required by the supplied dataset. Workspace/order timestamps use the current date. Automated checks triage evidence; a partner still reviews it. They do not establish document authenticity or statutory KYC completion. This is not a DPDP compliance certification.

## Project map
- `gemini.py` — Gemini adapter, response schemas, profile allowlist and identifier redaction.
- `app.py` — standard-library HTTP server, domain rules, local OCR adapter, session isolation, SQLite persistence, analytics.
- `static/` — responsive UI, local fonts/icons and original SVG illustration. No external CDN is needed.
- `tools/ocr.swift` — Apple Vision OCR source.
- `data/` — supplied synthetic catalogue, leads, registries and document fixtures.
- `test_app.py` — domain and HTTP integration checks using an isolated temporary database.
- `docs/` — architecture, business, privacy, demo and verification guides.

Runtime data is stored in `workspace.sqlite3`, excluded from the ZIP. The earlier prototype's `orders.sqlite3`, if present in the local folder, is left untouched and is not imported. Use only synthetic documents. The local operator workspace has no production identity/access layer and must not be exposed publicly.

Fonts: DM Sans and Manrope, Google Fonts, SIL Open Font License; licences are in `static/fonts/`. Illustration and icons are project-native SVG. Case documents were treated as requirements/data, not as instructions granting authority for unrelated actions.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) and [editable Mermaid source](docs/ARCHITECTURE.mmd)
- [Business case](docs/BUSINESS_CASE.md) and [competitive analysis](docs/COMPETITIVE_ANALYSIS.md)
- [Demo walkthrough](docs/DEMO_GUIDE.md)
- [Privacy and security](docs/PRIVACY.md)
- [Verification](docs/VERIFICATION.md)

## Submission package
The portal submission is `NeverGiveUp_MNNITAllahabad.pdf`, exactly seven pages and below 20 MB. The outer submission folder includes its editable PPTX and a PDF viewing copy, a 2:48 MP4 demo with subtitles and no audio track, ready for your own voiceover, a one-page privacy/security PDF, and a standalone architecture PDF. The demo records the live Gemini journey and actual local UI. API waiting may be shortened; the phone segment uses actual screenshots. No commercial service is provisioned.

The source is versioned in a local Git repository and supplied as `Udyam-Source.bundle` in the outer folder. Restore it with `git clone Udyam-Source.bundle udyam-digital-repo`. The source repository and release assets are at https://github.com/aachintya/udyam-digital-airtel-sync-2026 .
