// Copyright (C) 2020-2022 Intel Corporation
//
// SPDX-License-Identifier: MIT

const cvatCanvas = require('cvat-canvas/src/typescript/canvas');

export const CanvasMode = {
    IDLE: 'idle',
    DRAG: 'drag',
    RESIZE: 'resize',
    DRAW: 'draw',
    EDIT: 'edit',
    MERGE: 'merge',
    SPLIT: 'split',
    GROUP: 'group',
    JOIN: 'join',
    SLICE: 'slice',
    INTERACT: 'interact',
    SELECT_REGION: 'select_region',
    DRAG_CANVAS: 'drag_canvas',
    ZOOM_CANVAS: 'zoom_canvas',
} as const;

export type CanvasMode = typeof CanvasMode[keyof typeof CanvasMode];

export const RectDrawingMethod = {
    CLASSIC: 'By 2 points',
    EXTREME_POINTS: 'By 4 points',
} as const;

export type RectDrawingMethod = typeof RectDrawingMethod[keyof typeof RectDrawingMethod];

export const CuboidDrawingMethod = {
    CLASSIC: 'From rectangle',
    CORNER_POINTS: 'By 4 points',
} as const;

export type CuboidDrawingMethod = typeof CuboidDrawingMethod[keyof typeof CuboidDrawingMethod];

export const HighlightSeverity = {
    ERROR: 'error',
    WARNING: 'warning',
} as const;

export type HighlightSeverity = typeof HighlightSeverity[keyof typeof HighlightSeverity];

export interface CanvasHint {
    type: 'text' | 'list';
    content: string | string[];
    className?: string;
    icon?: 'info' | 'loading';
}

export interface InteractionData {
    enabled: boolean;
    shapeType?: string;
    crosshair?: boolean;
    minPosVertices?: number;
    minNegVertices?: number;
    startWithBox?: boolean;
    enableSliding?: boolean;
    allowRemoveOnlyLast?: boolean;
    intermediateShape?: {
        shapeType: string;
        points: number[];
    };
}

export interface InteractionResult {
    points: number[];
    shapeType: string;
    button: number;
}

export interface Canvas {
    html(): HTMLDivElement;
    setup(frameData: any, objectStates: any[], zLayer?: number): void;
    setupIssueRegions(issueRegions: Record<number, { hidden: boolean; points: number[] }>): void;
    translateFromSVG(points: number[]): number[];
    setupConflictRegions(clientID: number): number[];
    activate(clientID: number | null, attributeID?: number): void;
    highlight(clientIDs: number[] | null, severity: HighlightSeverity | null): void;
    rotate(rotationAngle: number): void;
    focus(clientID: number, padding?: number): void;
    fit(): void;
    grid(stepX: number, stepY: number): void;
    interact(interactionData: InteractionData): void;
    draw(drawData: object): void;
    edit(editData: object): void;
    group(groupData: object): void;
    join(joinData: object): void;
    slice(sliceData: object): void;
    split(splitData: object): void;
    merge(mergeData: object): void;
    select(objectState: any): void;
    fitCanvas(): void;
    bitmap(enable: boolean): void;
    selectRegion(enable: boolean): void;
    dragCanvas(enable: boolean): void;
    zoomCanvas(enable: boolean): void;
    mode(): CanvasMode;
    cancel(): void;
    configure(configuration: object): void;
    isAbleToChangeFrame(): boolean;
    destroy(): void;
    readonly geometry: {
        image: { width: number; height: number };
        canvas: { width: number; height: number };
        grid: { width: number; height: number };
        top: number;
        left: number;
        scale: number;
        offset: number;
        angle: number;
    };
}

type CanvasConstructor = new () => Canvas;

const CanvasClass = cvatCanvas.Canvas as CanvasConstructor;

export function convertShapesForInteractor(shapes: InteractionResult[], type: 'points' | 'rectangle', button: number): number[][] {
    const reducer = (acc: number[][], _: number, index: number, array: number[]): number[][] => {
        if (!(index % 2)) {
            // 0, 2, 4
            acc.push([array[index], array[index + 1]]);
        }
        return acc;
    };

    return shapes
        .filter((shape: InteractionResult): boolean => shape.button === button && shape.shapeType === type)
        .map((shape: InteractionResult): number[] => shape.points)
        .flat()
        .reduce(reducer, []);
}

export {
    CanvasClass as Canvas,
};
