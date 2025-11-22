# SAM2 Tracker Test Image

This directory keeps a test-only Dockerfile that pins PyTorch 2.9.1 for the SAM2 agent.
Use it when you need to verify compatibility with the newer PyTorch release without
modifying the production Dockerfile.

```bash
# From the repository root
docker build -f ai-models/tracker/sam2/test/Dockerfile \
  -t sam2-tracker:pytorch2.9.1 .
```

Then either update the `sam2-tracker-agent` service to reference this tag or override
the `build.dockerfile` path while running `docker compose` for targeted tests.
