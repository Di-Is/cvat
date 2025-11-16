// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import { Source, ShapeType } from './enums';

export type TrackerConversionMode = 'inline' | 'preconvert';

export interface TrackerRunShapeAttribute {
    specId: number;
    value: string;
}

export interface TrackerRunShapePayload {
    id: number | null;
    clientId: number;
    frame: number;
    labelId: number;
    shapeType: ShapeType;
    points: number[];
    zOrder: number;
    rotation: number;
    group: number | null;
    occluded: boolean;
    outside: boolean;
    source: Source;
    attributes: TrackerRunShapeAttribute[];
}
