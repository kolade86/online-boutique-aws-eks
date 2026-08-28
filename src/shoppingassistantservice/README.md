# Shopping Assistant Service

An LLM-backed shopping assistant: it takes a room photo plus a question, asks
Gemini for design advice, retrieves matching catalog items from a vector store,
and returns product recommendations.

## Status: disabled in this AWS deployment

**This service is not deployed.** In `helm/online-boutique/values.yaml` it is
marked `enabled: false`, so the chart renders no Deployment, Service, HPA, or
PDB for it. The frontend is deployed with `ENABLE_ASSISTANT=false`, which hides
the assistant button and makes `/assistant` and `/bot` return 404.

### Why

The code is written against Google Cloud and needs infrastructure this
AWS/EKS deployment does not have:

| Dependency | Purpose |
|---|---|
| Gemini via `langchain-google-genai` | Chat model and embeddings |
| AlloyDB (`langchain-google-alloydb-pg`) | pgvector store for catalog similarity search |
| Google Secret Manager | AlloyDB password lookup |

It reads seven required environment variables at import time — `PROJECT_ID`,
`REGION`, `ALLOYDB_DATABASE_NAME`, `ALLOYDB_TABLE_NAME`, `ALLOYDB_CLUSTER_NAME`,
`ALLOYDB_INSTANCE_NAME`, `ALLOYDB_SECRET_NAME` — and constructs a Secret Manager
client immediately, so it crashes on startup without a full GCP setup.

### What used to happen

The chart previously deployed `nginx:alpine` under this service's name. The
storefront therefore advertised an assistant that could never answer anything:
the button was visible, `/assistant` rendered a chat page, and `/bot` POSTed to
an nginx that returned a static page instead of a chat response. That
placeholder has been removed rather than left to look like a working feature.

### Enabling it later

The source is kept as future work. Turning it on means:

1. Providing a chat model and embeddings the deployment can actually reach
   (Bedrock, or Gemini via an API key), replacing the `langchain-google-genai`
   wiring.
2. Providing a vector store — the existing RDS PostgreSQL instance with the
   `pgvector` extension is the natural fit, replacing AlloyDB.
3. Moving the AlloyDB secret lookup to AWS Secrets Manager, which the cluster
   already reaches through External Secrets Operator.
4. Setting `enabled: true` for `shoppingassistantservice` and
   `ENABLE_ASSISTANT=true` plus `SHOPPING_ASSISTANT_SERVICE_ADDR` on the
   frontend in `values.yaml`, then adding the service to the build and deploy
   matrices in `.github/workflows/`.

Until then, treat this directory as reference code. It is intentionally not in
the CI build matrix, so no image is published for it.
