# Final submission checklist

## Local packet

- [x] README final with real XLF CSP proof near the top.
- [x] Project description final with paste-ready short and long copy.
- [x] One-page writeup final; narrative is within the requested approximate one-page scope.
- [x] 90-second demo script final.
- [x] 3-minute demo script final.
- [x] Real XLF CSP evidence linked with a repository-relative path.
- [x] Earlier BTC PAPER round-trip kept as secondary context only.
- [x] Limitations state PAPER-only, INDICATIVE-not-OPRA, `SHORT_PUT_OPEN`, and no P&L/cycle claim.

## Verification

- [x] Test-isolation fix committed as `abaffdf`.
- [x] Real CSP evidence committed as `609de36`.
- [x] `tests/wheel` passes after the test-only fix.
- [x] Ruff passes.
- [x] Mypy passes using an isolated temporary cache.
- [x] `git diff --check` passes.
- [ ] Run the full suite in normal macOS Terminal; the restricted Codex sandbox blocks the known localhost-bind intake test.
- [x] Secret scan passes for tracked repository content.
- [x] `.env` is ignored and not tracked.
- [x] `opaca-wheel-paper.db` is ignored and not tracked.

## Official Build with Gemini XPRIZE fields

The live requirements were checked from Devpost. They require a repository,
3-minute video, 500–1000-word narrative, product evidence, revenue and expense
evidence, customer evidence, and form-specific fields. Before any final
Devpost action, complete or explicitly disclose:

- [ ] Verify and document the Gemini API call used by the project.
- [ ] Verify and document the Google Cloud product used during the hackathon.
- [ ] Gather Gemini/Google Cloud observability evidence and required billing or zero-dollar invoices.
- [ ] Gather revenue evidence, monthly revenue values, and a simple P&L.
- [ ] Gather hackathon-period expense evidence, including marketing/customer-acquisition spend even if zero.
- [ ] Gather customer/user evidence and a public testimonial if available; report zero honestly if applicable.
- [ ] Share the repository with `testing@devpost.com` and `judging@hacker.fund`, as required by the live form.
- [ ] Supply the required repository ZIP/file evidence through the official upload flow.
- [ ] Record the exact project start date, residence/country, category, submitter type, and other official field answers.
- [ ] Choose and verify the final judging group/category.

## Manual assets and actions

- [ ] Create or select a truthful cover image.
- [ ] Record the 3-minute demo using the guardrails in `demo-script.md`.
- [ ] Capture 3–5 screenshots showing the architecture, rejection, and real CSP proof.
- [ ] Verify the final public/private GitHub repository URL and access settings.
- [ ] Paste the short and long descriptions into the official form.
- [ ] Upload product, billing, and P&L evidence through the official form.
- [ ] Perform the final Devpost action manually and save the confirmation screenshot.

## Safety freeze

- No more broker mutations.
- Do not submit, cancel, replace, roll, exercise, or close the XLF position from this repository pass.
- Do not expose credentials, raw account IDs, or the runtime Wheel database.
