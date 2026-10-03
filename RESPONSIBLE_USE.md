# Responsible use and publication policy

FirstCall tests real companies' public APIs. These rules govern how it runs and how results are published.

## How runs behave

1. **Test mode only.** Runs use sandbox or test-mode credentials, and the harness refuses live keys. No real money moves, and no production data is touched.
2. **Official, public surfaces only:**
   - public documentation;
   - test or sandbox API environments;
   - the company's own official MCP server.

   No scraping behind logins. No APIs whose terms forbid automated testing.
3. **Light load:**
   - runs are sequential by default and capped by turns and budget;
   - docs pages are fetched with a delay per host;
   - `robots.txt` is respected;
   - the user agent identifies FirstCall and links to this repository.
4. **Traceable test data.** Everything a run creates is tagged with a run id, so it can be found and cleaned up.

## How results are published

1. **The company hears first.** Before any company's results are published, they're sent privately to that company at least 7 days in advance. They can respond, point out errors, or ask for a re-run. If they want, their response is linked next to the results.
2. **Framed as research.** Results describe how one agent setup did on specific tasks on a specific date. They don't judge a company's product or its engineers. Every result links to its full trace and methodology so anyone can reproduce it.
3. **Corrections and removal.** Factual errors are corrected promptly. A company may ask for its results to be removed. Open an issue or email kishanpreetamkommana@gmail.com.
4. **No affiliation.** FirstCall isn't affiliated with or endorsed by any company it tests. Company and product names are used only to identify the APIs tested, and logos aren't used.

## Data

- **No personal data collected.** FirstCall doesn't collect personal data.
- **What traces contain:**
  - documentation URLs;
  - the code the agent wrote;
  - API responses from test mode;
  - test object ids.

  Credentials are redacted before anything is written.
