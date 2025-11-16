// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import { Job, MinimalShape, ShapeType } from 'cvat-core-wrapper';

export interface TrackerLike {
    supportedShapeTypes?: ShapeType[] | null;
    supported_shape_types?: ShapeType[] | null;
}

const TRACKER_CREATION_PRIORITY: ShapeType[] = [
    ShapeType.RECTANGLE,
    ShapeType.POLYGON,
    ShapeType.MASK,
];

/**
 * Returns a non-empty list of supported shape types for a tracker. Falls back to rectangles.
 */
export function getTrackerSupportedShapes(tracker?: TrackerLike | null): ShapeType[] {
    if (!tracker) {
        return [];
    }

    const supported = tracker.supportedShapeTypes ?? tracker.supported_shape_types;
    if (Array.isArray(supported) && supported.length > 0) {
        return supported as ShapeType[];
    }

    return [ShapeType.RECTANGLE];
}

/**
 * Resolves the preferred shape type to use when initializing tracking.
 */
export function getTrackerCreationShapeType(
    tracker?: TrackerLike | null,
    priority: readonly ShapeType[] = TRACKER_CREATION_PRIORITY,
): ShapeType | null {
    const supported = getTrackerSupportedShapes(tracker);
    if (!supported.length) {
        return null;
    }

    for (const preferred of priority) {
        if (supported.includes(preferred)) {
            return preferred;
        }
    }

    return supported[0];
}

/**
 * Converts arbitrary polygon-like shapes into rectangles while preserving bounds.
 */
export function trackerRectangleMapper(shape: MinimalShape): MinimalShape {
    return {
        type: ShapeType.RECTANGLE,
        points: shape.points.reduce(
            (acc: number[], value: number, index: number): number[] => {
                if (index % 2) {
                    acc[1] = Math.min(acc[1], value);
                    acc[3] = Math.max(acc[3], value);
                } else {
                    acc[0] = Math.min(acc[0], value);
                    acc[2] = Math.max(acc[2], value);
                }
                return acc;
            },
            [Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER, Number.MIN_SAFE_INTEGER],
        ),
    };
}

/**
 * Ensures tracker output sticks to supported shape types.
 */
export function normalizeTrackerShape(shape: MinimalShape, tracker?: TrackerLike | null): MinimalShape {
    const supported = new Set(getTrackerSupportedShapes(tracker));

    if (supported.has(shape.type)) {
        return shape;
    }

    if (supported.has(ShapeType.RECTANGLE)) {
        return trackerRectangleMapper(shape);
    }

    return shape;
}

/**
 * Clamps target frame used by tracker actions to the [start+1, stop] interval.
 */
export function clampTrackerTargetFrame(job: Job, rawTarget?: number | null): number {
    const fallbackTarget = Number.isFinite(rawTarget) ? Math.trunc(rawTarget as number) : job.stopFrame;
    return Math.min(Math.max(fallbackTarget, job.startFrame + 1), job.stopFrame);
}
