# OPACA metrics function

The read-only function is exposed by Netlify at:

```text
/.netlify/functions/metrics
```

Configure these Netlify environment variables for the deploy context. Their
values must stay in Netlify and must never be copied into the repository,
dashboard `.env` files, Vite variables, or GitHub Actions:

- `APCA_API_KEY_ID`
- `APCA_API_SECRET_KEY`

The function hard-codes Alpaca PAPER trading reads and uses the indicative
options feed. It returns an explicit sanitized DTO, short public cache headers,
and no broker/account identity fields. It accepts GET only and has no broker
mutation route.

The Pages build only needs the safe public variable
`VITE_METRICS_API_URL=https://<netlify-site>/.netlify/functions/metrics`.

Manual deployment:

1. Create or select a Netlify site and connect this repository.
2. Set the two environment variables above in the Netlify site settings for
   the production context without printing their values.
3. Set the function directory to `netlify/functions` (also committed in
   `netlify.toml`) and deploy.
4. Open the function URL with a GET request and confirm it returns only the
   documented public metrics DTO and never private account identity.
5. Set the resulting public function URL as the GitHub repository variable
   `VITE_METRICS_API_URL`, then run the Pages workflow.
