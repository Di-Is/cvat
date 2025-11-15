# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import cvat_sdk.auto_annotation as cvataa
import PIL.Image

spec = cvataa.InteractorFunctionSpec(
    min_pos_points=2,
    min_neg_points=0,
    startswith_box=True,
    startswith_box_optional=False,
    help_message="Sample interactor",
    animated_gif="https://example.invalid/demo.gif",
    version=2,
)


def interact(
    context: cvataa.InteractorFunctionContext,
    image: PIL.Image.Image,
    prompt: cvataa.InteractionPrompt,
) -> cvataa.MaskPrediction:
    _ = (context, prompt)
    mask = [[1, 1], [1, 1]]
    return cvataa.MaskPrediction(mask=mask)
