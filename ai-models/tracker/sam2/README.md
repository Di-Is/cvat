# SAM2 tracker

This directory contains an implementation of a CVAT auto-annotation function
that tracks masks and polygons using the [Segment Anything Model 2][sam2] (SAM2)
from Meta Research.

[sam2]: https://github.com/facebookresearch/sam2

To use this with CVAT CLI, use the following options:

```
--function-file func.py -p model_id=str:<model_id>
```

where `<model_id>` is one of the [SAM2 model IDs][sam2-hf] from Meta's Hugging Face account,
such as `facebook/sam2.1-hiera-small` (the OSS default) or `facebook/sam2.1-hiera-large`.

[sam2-hf]: https://huggingface.co/models?search=facebook%2Fsam2

In addition, you can add `-p device=str:<device>` to run the model on a specific PyTorch device,
such as `cuda`. By default, the model will be run on the CPU.

All other parameters set with the `-p` option will be passed directly to the model constructor.
For example, setting `-p vos_optimized=bool:true` (or exporting `SAM2_TRACKER_VOS_OPTIMIZED=1`)
enables the upstream SAM2 `vos_optimized` mode, which compiles the tracker with
`torch.compile` for substantially faster video inference (requires PyTorch 2.5+).

## Tracker-specific environment flags

- `SAM2_TRACKER_WARMUP_FRAMES` (default: `2`): run a small startup warmup. Increase only if you need
  more aggressive ahead-of-time compilation after container restart.
- `SAM2_TRACKER_USE_CUDAGRAPHS` (unset by default): set to `0` or `1` to override TorchInductor
  cudagraphs; leave unset to inherit PyTorch defaults. Use `0` only when the compiled
  path is unstable on your GPU.
- `SAM2_TRACKER_ALLOW_CPU_FALLBACK` (default: `0`): allow CPU transforms when fast preprocess
  fails. Keep disabled in production to avoid silent slow paths.

## Dependencies

The tracker and interactor share the same dependency set, which is defined via
`pyproject.toml` / `uv.lock` inside `../interactor/sam2`. Run `uv sync` in that
directory (or install the package with `uv pip install /workspace/ai-models/interactor/sam2`
inside containers) to provision the required Python libraries.
