# SAM2 interactor

This directory contains the native CVAT interactor implementation based on the
[Segment Anything Model 2][sam2] (SAM2) from Meta Research. The function follows
the `InteractorFunction` interface so that it can be served by `cvat-cli
function run-agent` and consumed by the AI Tools sidebar without relying on
Nuclio.

[sam2]: https://github.com/facebookresearch/sam2

## Local development

Dependencies are managed with [uv](https://github.com/astral-sh/uv) via
`pyproject.toml`/`uv.lock`. Run the following from this directory to install the
Python environment:

```bash
uv sync
```

You can then execute local helpers (for example, linting or quick experiments)
with `uv run <script>`.

The tracker function reuses this same dependency set, so keeping this
environment up to date automatically covers both SAM2 agents.

## Running the agent

When packaging this function for CVAT:

1. Create a native interactor via `cvat-cli function create-native` and capture
   the resulting function ID.
2. Start the agent container (or run `cvat-cli function run-agent`) with:

   ```bash
   cvat-cli \
     --server-host <CVAT_URL> \
     function run-agent <FUNCTION_ID> \
     --function-file /workspace/ai-models/interactor/sam2/func.py \
     -p "model_id=str:facebook/sam2.1-hiera-small" \
     -p "device=str:cuda"
   ```

   - `model_id` must match one of the official SAM2 checkpoints on Hugging Face.
   - Optional parameters like `device`, `mask_threshold`, `max_hole_area`, and
     `max_sprinkle_area` are forwarded directly to `SAM2ImagePredictor`. The
     defaults intentionally mirror Meta's `SAM2Transforms` settings
     (`mask_threshold=0.0`, `max_hole_area=0.0`, `max_sprinkle_area=0.0`) so
     that UI prompts can be reused without extra scaling or heuristics.

The agent pulls frames from the assigned tasks, executes the SAM2.1 promptable
segmentation model, and returns `mask_rle` outputs so they can be displayed and
committed from the AI Tools interactor panel. Each response also contains:

- `bounds`: The inclusive `[left, top, right, bottom]` tuple that matches the
  trailing metadata returned by `cvat_sdk.masks.encode_mask`. The UI uses this
  to compute the decoding window, so the agent never appends those values to
  `mask_rle`.
- `mask`: A 2D array mirroring the requested frame resolution. This is kept as
  a temporary fallback in case the UI RLE decoding path lags behind.

SAM2.1 assumes raw pixel coordinates with `normalize_coords=True`, so the
interactor accepts click/box prompts straight from the UI and lets
`SAM2Transforms` normalize/resize them internally.

### Frame caching

Continuous prompting on the same frame should reuse the encoder state that
`SAM2ImagePredictor.set_image()` computed. The interactor keeps an
LRU-cache of these states (including the latest low-res mask logits) and
replays them instead of recalculating embeddings on every request. By default
only one frame is cached to limit GPU memory usage. Set
`SAM2_INTERACTOR_CACHE_FRAMES=<n>` in the agent environment to allow up to `n`
frames to stay warm simultaneously.
