# Test fixtures

`adf-schema-v1.json` is the Atlassian Document Format JSON schema from
[`@atlaskit/adf-schema`](https://www.npmjs.com/package/@atlaskit/adf-schema) **57.6.19**
(`dist/json-schema/v1/full.json`), © Atlassian, licensed under the Apache License 2.0. It's
vendored so the tests can check that everything Waypoint sends to Jira is valid ADF — Jira rejects
invalid ADF outright — without fetching it over the network.

To update it:

```bash
curl -sfL https://unpkg.com/@atlaskit/adf-schema/dist/json-schema/v1/full.json -o tests/fixtures/adf-schema-v1.json
```

and update the version above.
