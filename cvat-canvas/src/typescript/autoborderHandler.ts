// Copyright (C) 2020-2022 Intel Corporation
//
// SPDX-License-Identifier: MIT

import * as SVG from 'svg.js';

import consts from './consts';
import { Configuration, Geometry } from './canvasModel';

interface TransformedShape {
    points: string;
    color: string;
}

export interface AutoborderHandler {
    autoborder(enabled: boolean, currentShape?: SVG.Shape, currentID?: number): void;
    configure(configuration: Configuration): void;
    transform(geometry: Geometry): void;
    updateObjects(): void;
}

export class AutoborderHandlerImpl implements AutoborderHandler {
    private currentShape: SVG.Shape | null;
    private currentID?: number;
    private frameContent: SVGSVGElement;
    private enabled: boolean;
    private scale: number;
    private controlPointsSize: number;
    private groups: SVGGElement[];
    private auxiliaryGroupID: number | null;
    private auxiliaryClicks: number[];
    private listeners: Record<string, Record<number, {
        click: (event: MouseEvent) => void;
        dblclick: (event: MouseEvent) => void;
    }>>;

    public constructor(frameContent: SVGSVGElement) {
        this.frameContent = frameContent;
        this.currentID = undefined;
        this.currentShape = null;
        this.enabled = false;
        this.scale = 1;
        this.groups = [];
        this.controlPointsSize = consts.BASE_POINT_SIZE;
        this.auxiliaryGroupID = null;
        this.auxiliaryClicks = [];
        this.listeners = {};
    }

    private removeMarkers(): void {
        this.groups.forEach((group: SVGGElement): void => {
            const groupID = group.dataset?.groupId;
            const listenerGroup = groupID ? this.listeners[groupID] : undefined;
            Array.from(group.children).forEach((child: Element, pointID: number): void => {
                const circle = child as SVGCircleElement;
                const handlers = listenerGroup?.[pointID];
                if (handlers) {
                    circle.removeEventListener('mousedown', handlers.click);
                    circle.removeEventListener('dblclick', handlers.dblclick);
                }
                circle.remove();
            });

            if (groupID) {
                delete this.listeners[groupID];
            }

            group.remove();
        });

        this.groups = [];
        this.auxiliaryGroupID = null;
        this.auxiliaryClicks = [];
        this.listeners = {};
    }

    private release(): void {
        this.removeMarkers();
        this.enabled = false;
        this.currentShape = null;
    }

    private addPointToCurrentShape(x: number, y: number): void {
        if (!this.currentShape) {
            return;
        }

        const array: number[][] = (this.currentShape as any).array().valueOf();
        array.pop();

        // need to append twice (specific of the library)
        array.push([x, y]);
        array.push([x, y]);

        const paintHandler = this.currentShape.remember('_paintHandler');
        if (paintHandler) {
            paintHandler.drawCircles?.();
            paintHandler.set?.members?.forEach((el: SVG.Circle): void => {
                el.attr('stroke-width', 1 / this.scale).attr('r', 2.5 / this.scale);
            });
        }
        (this.currentShape as any).plot(array);
    }

    private resetAuxiliaryShape(): void {
        if (this.auxiliaryGroupID !== null) {
            const group = this.groups[this.auxiliaryGroupID];
            if (group) {
                while (this.auxiliaryClicks.length > 0) {
                    const resetID = this.auxiliaryClicks.pop();
                    if (typeof resetID === 'number') {
                        const element = group.children.item(resetID);
                        if (element instanceof SVGElement) {
                            element.classList.remove('cvat_canvas_autoborder_point_direction');
                        }
                    }
                }
            }
        }

        this.auxiliaryClicks = [];
        this.auxiliaryGroupID = null;
    }

    // convert each shape to group of clickable points
    // save all groups
    private drawMarkers(transformedShapes: TransformedShape[]): void {
        const svgNamespace = 'http://www.w3.org/2000/svg';

        this.groups = transformedShapes.map(
            (shape: TransformedShape, groupID: number): SVGGElement => {
                const group = document.createElementNS(svgNamespace, 'g');
                const groupKey = `${groupID}`;
                group.setAttribute('data-group-id', groupKey);

                this.listeners[groupKey] = this.listeners[groupKey] || {};
                const pointEntries = shape.points.split(/\s+/).filter((point: string) => point.length);
                const circles = pointEntries.map(
                    (point: string, pointID: number, allPoints: string[]): SVGCircleElement => {
                        const [rawX, rawY] = point.split(',');
                        const cx = typeof rawX === 'string' ? rawX : '0';
                        const cy = typeof rawY === 'string' ? rawY : '0';
                        const xValue = Number(cx);
                        const yValue = Number(cy);
                        const circle = document.createElementNS(svgNamespace, 'circle');
                        circle.classList.add('cvat_canvas_autoborder_point');
                        circle.setAttribute('fill', shape.color);
                        circle.setAttribute('stroke', 'black');
                        circle.setAttribute('stroke-width', `${consts.POINTS_STROKE_WIDTH / this.scale}`);
                        circle.setAttribute('cx', cx);
                        circle.setAttribute('cy', cy);
                        circle.setAttribute('r', `${this.controlPointsSize / this.scale}`);

                        const click = (event: MouseEvent): void => {
                            event.stopPropagation();

                            // another shape was clicked
                            if (this.auxiliaryGroupID !== null && this.auxiliaryGroupID !== groupID) {
                                this.resetAuxiliaryShape();
                            }

                            this.auxiliaryGroupID = groupID;
                            // up clicked group for convenience
                            this.frameContent.appendChild(group);

                            if (this.auxiliaryClicks[1] === pointID) {
                                // the second point was clicked twice
                                this.addPointToCurrentShape(xValue, yValue);
                                this.resetAuxiliaryShape();
                                return;
                            }

                            // the first point can not be clicked twice
                            // just ignore such a click if it is
                            if (this.auxiliaryClicks[0] !== pointID) {
                                this.auxiliaryClicks.push(pointID);
                            } else {
                                return;
                            }

                            // it is the first click
                            if (this.auxiliaryClicks.length === 1) {
                                if (this.currentShape) {
                                    const handler = this.currentShape.remember('_paintHandler');
                                    // draw and remove initial point just to initialize data structures
                                    if (!handler || !handler.startPoint) {
                                        (this.currentShape as any).draw('point', event);
                                        (this.currentShape as any).draw('undo');
                                    }
                                }

                                this.addPointToCurrentShape(xValue, yValue);
                                // is is the second click
                            } else if (this.auxiliaryClicks.length === 2) {
                                circle.classList.add('cvat_canvas_autoborder_point_direction');
                                // it is the third click
                            } else {
                                const [first, second, third] = this.auxiliaryClicks;
                                if (
                                    typeof first !== 'number' ||
                                    typeof second !== 'number' ||
                                    typeof third !== 'number'
                                ) {
                                    return;
                                }

                                // sign defines bypass direction
                                const sign = Math.sign(third - first) *
                                    Math.sign(second - first) *
                                    Math.sign(third - second) || 1;

                                // go via a polygon and get vertices
                                // the first vertex has been already drawn
                                const way: string[] = [];
                                for (let i = first + sign; ; i += sign) {
                                    if (i < 0) {
                                        i = allPoints.length - 1;
                                    } else if (i >= allPoints.length) {
                                        i = 0;
                                    }

                                    way.push(allPoints[i]);

                                    const lastIndex = this.auxiliaryClicks[this.auxiliaryClicks.length - 1];
                                    if (i === lastIndex) {
                                        break;
                                    }
                                }

                                // remove the latest cursor position from drawing array
                                for (const wayPoint of way) {
                                    const [pX, pY] = wayPoint
                                        .split(',')
                                        .map((coordinate: string): number => +coordinate);
                                    this.addPointToCurrentShape(pX, pY);
                                }

                                this.resetAuxiliaryShape();
                            }
                        };

                        const dblclick = (event: MouseEvent): void => {
                            event.stopPropagation();
                        };

                        this.listeners[groupKey][pointID] = {
                            click,
                            dblclick,
                        };

                        circle.addEventListener('mousedown', click);
                        circle.addEventListener('dblclick', dblclick);
                        return circle;
                    },
                );

                group.append(...circles);
                return group;
            },
        );

        this.frameContent.append(...this.groups);
    }

    public updateObjects(): void {
        if (!this.enabled || !this.currentShape) return;
        this.removeMarkers();

        const currentClientIDRaw = this.currentShape.node?.dataset?.originClientId;
        const currentClientID = typeof currentClientIDRaw === 'string' ? Number(currentClientIDRaw) : null;
        const currentShapeID = typeof this.currentID === 'number' ? this.currentID : null;
        const shapeElements = Array.from(this.frameContent.getElementsByClassName('cvat_canvas_shape'));
        const shapes = shapeElements.filter(
            (shape: Element): boolean => {
                const clientAttr = shape.getAttribute('clientID');
                if (!clientAttr) {
                    return false;
                }

                const numericClient = Number(clientAttr);
                if (Number.isNaN(numericClient)) {
                    return false;
                }

                if (currentShapeID !== null && numericClient === currentShapeID) {
                    return false;
                }

                if (currentClientID !== null && numericClient === currentClientID) {
                    return false;
                }

                return !shape.classList.contains('cvat_canvas_hidden');
            },
        );
        const transformedShapes = shapes
            .map((shape: Element): TransformedShape | null => {
                const color = shape.getAttribute('fill');
                if (color === null) return null;

                let points = '';
                if (shape.tagName === 'polyline' || shape.tagName === 'polygon') {
                    const pointsAttr = shape.getAttribute('points');
                    if (!pointsAttr) {
                        return null;
                    }
                    points = pointsAttr;
                } else if (shape.tagName === 'ellipse') {
                    const cxAttr = shape.getAttribute('cx');
                    const cyAttr = shape.getAttribute('cy');
                    if (cxAttr === null || cyAttr === null) {
                        return null;
                    }
                    const cx = Number(cxAttr);
                    const cy = Number(cyAttr);
                    points = `${cx},${cy}`;
                } else if (shape.tagName === 'rect') {
                    const xAttr = shape.getAttribute('x');
                    const yAttr = shape.getAttribute('y');
                    const widthAttr = shape.getAttribute('width');
                    const heightAttr = shape.getAttribute('height');
                    if (xAttr === null || yAttr === null || widthAttr === null || heightAttr === null) {
                        return null;
                    }

                    const x = Number(xAttr);
                    const y = Number(yAttr);
                    const width = Number(widthAttr);
                    const height = Number(heightAttr);

                    if (
                        Number.isNaN(x) ||
                        Number.isNaN(y) ||
                        Number.isNaN(width) ||
                        Number.isNaN(height)
                    ) {
                        return null;
                    }

                    points = `${x},${y} ${x + width},${y} ${x + width},${y + height} ${x},${y + height}`;
                } else if (shape.tagName === 'g') {
                    const polylineID = (shape as HTMLElement).dataset?.polylineId;
                    const polyline = polylineID ?
                        this.frameContent.getElementById(polylineID) as SVGPolylineElement | null : null;
                    const polylinePoints = polyline?.getAttribute('points');
                    if (polyline && polylinePoints) {
                        points = polylinePoints;
                    } else {
                        return null;
                    }
                }

                return {
                    color,
                    points: points.trim(),
                };
            })
            .filter((state: TransformedShape | null): state is TransformedShape => state !== null);

        this.drawMarkers(transformedShapes);
    }

    public autoborder(enabled: boolean, currentShape?: SVG.Shape, currentID?: number): void {
        if (enabled && !this.enabled && currentShape) {
            this.enabled = true;
            this.currentShape = currentShape;
            this.currentID = currentID;
            this.updateObjects();
        } else {
            this.release();
        }
    }

    public transform(geometry: Geometry): void {
        this.scale = geometry.scale;
        this.groups.forEach((group: SVGGElement): void => {
            Array.from(group.children).forEach((child: Element): void => {
                if (child instanceof SVGCircleElement) {
                    child.setAttribute('r', `${this.controlPointsSize / this.scale}`);
                    child.setAttribute('stroke-width', `${consts.BASE_STROKE_WIDTH / this.scale}`);
                }
            });
        });
    }

    public configure(configuration: Configuration): void {
        this.controlPointsSize = configuration.controlPointsSize || consts.BASE_POINT_SIZE;
    }
}
