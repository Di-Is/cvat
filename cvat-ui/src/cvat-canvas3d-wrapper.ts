// Copyright (C) 2021-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

const cvatCanvas3d = require('cvat-canvas3d/src/typescript/canvas3d');

export const CanvasMode = {
    IDLE: 'idle',
    DRAW: 'draw',
    EDIT: 'edit',
    DRAG_CANVAS: 'drag_canvas',
    GROUP: 'group',
    MERGE: 'merge',
    SPLIT: 'split',
} as const;

export type CanvasMode = typeof CanvasMode[keyof typeof CanvasMode];

export const MouseInteraction = {
    CLICK: 'click',
    DOUBLE_CLICK: 'dblclick',
    HOVER: 'hover',
} as const;

export type MouseInteraction = typeof MouseInteraction[keyof typeof MouseInteraction];

export const ViewType = {
    PERSPECTIVE: 'perspective',
    TOP: 'top',
    SIDE: 'side',
    FRONT: 'front',
} as const;

export type ViewType = typeof ViewType[keyof typeof ViewType];

export const CameraAction = {
    ZOOM_IN: 'KeyI',
    MOVE_UP: 'KeyU',
    MOVE_DOWN: 'KeyO',
    MOVE_LEFT: 'KeyJ',
    ZOOM_OUT: 'KeyK',
    MOVE_RIGHT: 'KeyL',
    TILT_UP: 'ArrowUp',
    TILT_DOWN: 'ArrowDown',
    ROTATE_RIGHT: 'ArrowRight',
    ROTATE_LEFT: 'ArrowLeft',
} as const;

export type CameraAction = typeof CameraAction[keyof typeof CameraAction];

export type ViewsDOM = {
    [key in ViewType]: HTMLCanvasElement;
};

export interface OrientationVisibility {
    x: boolean;
    y: boolean;
    z: boolean;
}

export interface Canvas3d {
    html(): ViewsDOM;
    setup(frameData: any, objectStates: any[]): void;
    isAbleToChangeFrame(): boolean;
    mode(): CanvasMode;
    render(): void;
    keyControls(keys: KeyboardEvent): void;
    draw(drawData: Record<string, unknown>): void;
    cancel(): void;
    dragCanvas(enable: boolean): void;
    activate(clientID: number | null, attributeID?: number): void;
    configureShapes(shapeProperties: Record<string, unknown>): void;
    fitCanvas(): void;
    fit(): void;
    group(groupData: { enabled: boolean }): void;
    merge(mergeData: { enabled: boolean }): void;
    split(splitData: { enabled: boolean }): void;
    destroy(): void;
}

type Canvas3dConstructor = new () => Canvas3d;

const Canvas3dClass = cvatCanvas3d.Canvas3d as Canvas3dConstructor;

export {
    Canvas3dClass as Canvas3d,
};
