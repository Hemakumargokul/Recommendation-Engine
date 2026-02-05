# Content Recommendation Engine (Autoblog AI)

AI-powered APIs for the **autoblog** application: semantic content recommendations and SEO-friendly image alt-text generation.

## Overview

This repository hosts two Flask services that run in a single container:

| Service   | Port | Purpose |
|----------|------|---------|
| **Retriever** | 8080 | Content recommendation: find similar posts via vector search (ChromaDB + OpenAI embeddings). |
| **Generator** | 8081 | Image alt-text: generate descriptive, SEO-optimized alt text for images using GPT-4o vision. |

Both services are used by the autoblog application to improve content discovery and accessibility.

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           Recommendation Engine          │
                    │  ┌─────────────┐    ┌─────────────────┐ │
                    │  │  Retriever  │    │    Generator     │ │
                    │  │  (port 8080)│    │  (port 8081)    │ │
                    │  └──────┬──────┘    └────────┬────────┘ │
                    └─────────┼────────────────────┼──────────┘
                              │                    │
         ┌────────────────────┼────────────────────┼────────────────────┐
         │                    │                    │                    │
         ▼                    ▼                    ▼                    ▼
   MySQL (blog)          ChromaDB              OpenAI               S3
   (wp_posts)         (embeddings)        (GPT-4o / embeddings)  (ChromaDB backup)
```

- **Retriever**: Reads post content from MySQL, embeds it with OpenAI, and queries ChromaDB for similar posts. ChromaDB is persisted locally and synced to/from S3. A scheduled job (daily) either rebuilds the collection from the DB (production-east) or syncs from S3 (other envs).
- **Generator**: Calls OpenAI’s vision API with an image URL (and optional title) to produce short, SEO-friendly alt text.

## API Reference

### Retriever — Similar content

**`GET /api/results`**

Returns IDs of posts similar to a given post (excluding the post itself).

| Query parameter   | Type | Default | Description |
|-------------------|------|---------|-------------|
| `post_content_id` | int  | *required* | WordPress post ID. |
| `nresults`        | int  | 6       | Number of similar post IDs to return. |

**Example**

```http
GET /api/results?post_content_id=12345&nresults=6
```

**Success (200)**  
JSON array of post IDs, e.g. `["123", "456", "789", ...]`.

**Error (500)**  
JSON with `error` key and optional metrics (e.g. `api_errors`, `api_exception`, `api_results_empty`).

---

### Generator — Image alt text

**`GET /generate-alt-text`**

Returns SEO-oriented alt text for an image (under ~100 characters).

| Query parameter | Type   | Required | Description |
|-----------------|--------|----------|-------------|
| `image_url`     | string | Yes      | Public URL of the image. |
| `image_title`   | string | No       | Optional title to guide the description. |

**Example**

```http
GET /generate-alt-text?image_url=https://example.com/photo.jpg&image_title=Red%20sedan
```

**Success (200)**

```json
{
  "image_url": "https://example.com/photo.jpg",
  "alt_text": "Red sedan front three-quarter view on street"
}
```

**Errors**

- **400** — Missing `image_url`: `{"error": "Image URL is required"}`
- **500** — OpenAI or other server error: `{"error": "<message>"}`

---

## Environment variables

| Variable | Service   | Description |
|----------|-----------|-------------|
| `GPT_API_KEY` | Both | OpenAI API key (embeddings + chat/vision). |
| `AUTOBLOG_BLOG_DB` | Retriever | MySQL host for the blog database. |
| `AUTOBLOG_BLOG_RW_PASSWORD` | Retriever | MySQL read/write password. |
| `AWS_ROLE_ARN` | Retriever / entrypoint | Used to infer environment (`production` / `staging` / `dev`) and S3 bucket. |
| `AWS_REGION` | Retriever / entrypoint | AWS region (e.g. `us-east-1`). S3 bucket is `autoblog-ai-{ENV}-{AWS_REGION}`. |
| `YAMAS_NAMESPACE` | Retriever | Metrics namespace for Yamas. |
| `SIA_KEY_PATH` | Retriever | Client key path for Yamas. |
| `SIA_CERT_PATH` | Retriever | Client cert path for Yamas. |

## Running locally

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables** (at least `GPT_API_KEY`; for Retriever you also need DB and AWS/Yamas if you use that service).

3. **Retriever (recommendations)**  
   Expects ChromaDB data (or will build it if you have DB access and run the update job). Default port 8080:

   ```bash
   python retriever.py
   ```

4. **Generator (alt text)**  
   Port 8081:

   ```bash
   python generator.py
   ```

## Docker

Build and run both services in one container:

```bash
docker build -t recommendation-engine .
docker run -p 8080:8080 -p 8081:8081 \
  -e GPT_API_KEY=... \
  -e AUTOBLOG_BLOG_DB=... \
  -e AUTOBLOG_BLOG_RW_PASSWORD=... \
  -e AWS_ROLE_ARN=... \
  -e AWS_REGION=us-east-1 \
  recommendation-engine
```

- **Entrypoint** (`entrypoint.sh`): Derives `ENV` from `AWS_ROLE_ARN`, downloads ChromaDB from `s3://autoblog-ai-${ENV}-${AWS_REGION}/chromadb`, then starts `retriever.py` (8080) and `generator.py` (8081).

## CI/CD

- **Screwdriver** (`screwdriver.yaml`): Builds the Docker image `autoblogaws/autoblog-ai` and has job hooks for nonprod/staging/prod deploys and Semgrep validation.

## Logs

- Retriever: `api_results.log`
- Generator: `image_analysis.log`

## License

Internal use; see your organization’s policies.
