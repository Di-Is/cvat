import { describe, expect, it, vi } from 'vitest';
import { ModelProviders, ShapeType } from 'cvat-core/src/enums';

import {
    executeInteractorRequest,
    normalizeInteractorResponse,
    type InteractorRequestPayload,
} from '../src/components/annotation-page/standard-workspace/controls-side-bar/interactor-helpers';
import {
    getTrackerCreationShapeType,
    getTrackerSupportedShapes,
    normalizeTrackerShape,
} from '../src/utils/tracker';

describe('tools-control interactor helpers', () => {
    const basePayload: InteractorRequestPayload = {
        frame: 42,
        pos_points: [[1, 2]],
        neg_points: [],
        obj_bbox: null,
        label_id: null,
        start_with_box: false,
    };

    it('delegates to native interactor for native providers', async () => {
        const interactor = {
            id: '7',
            provider: ModelProviders.NATIVE,
        } as any;

        const runFunctionInteractor = vi.fn().mockResolvedValue({ mask: [[1]] });
        const callLambda = vi.fn();

        const result = await executeInteractorRequest(interactor, basePayload, {
            jobInstance: {
                id: 15,
                taskId: 9,
                runFunctionInteractor,
            },
            callLambda,
        });

        expect(runFunctionInteractor).toHaveBeenCalledWith(7, {
            frame: 42,
            posPoints: [[1, 2]],
            negPoints: [],
            objBBox: null,
            labelId: null,
            startWithBox: false,
        });
        expect(callLambda).not.toHaveBeenCalled();
        expect(result).toEqual({ mask: [[1]] });
    });

    it('falls back to lambda call for non-native providers', async () => {
        const interactor = {
            id: 'tracker-1',
            provider: ModelProviders.NUCLIO,
        } as any;

        const runFunctionInteractor = vi.fn();
        const lambdaResponse = { mask: [[0]] };
        const callLambda = vi.fn().mockResolvedValue(lambdaResponse);

        const result = await executeInteractorRequest(interactor, basePayload, {
            jobInstance: {
                id: 11,
                taskId: 3,
                runFunctionInteractor,
            },
            callLambda,
        });

        expect(runFunctionInteractor).not.toHaveBeenCalled();
        expect(callLambda).toHaveBeenCalledWith(
            3,
            interactor,
            expect.objectContaining({
                frame: 42,
                job: 11,
            }),
        );
        expect(result).toEqual(lambdaResponse);
    });

    it('normalizes mask_rle payloads into 2D masks', () => {
        const response = {
            mask_rle: [3, 1, 0],
            bounds: [2, 2, 3, 3],
        };

        const decode = vi.fn().mockReturnValue([1, 0, 1, 1]);

        const normalized = normalizeInteractorResponse(response, decode);

        expect(decode).toHaveBeenCalledWith([3, 1, 0], 2, 2);
        expect(normalized.mask).toEqual([[1, 0], [1, 1]]);
    });

    it('throws when mask data is missing', () => {
        expect(() => normalizeInteractorResponse({}, vi.fn())).toThrow('mask data');
    });
});

describe('tools-control tracker helpers', () => {
    it('surfaces supported shape types regardless of casing', () => {
        const tracker = {
            supported_shape_types: [ShapeType.POLYGON, ShapeType.MASK],
        } as any;

        expect(getTrackerSupportedShapes(tracker)).toEqual([ShapeType.POLYGON, ShapeType.MASK]);
    });

    it('prefers polygon creation when rectangles are unavailable', () => {
        const tracker = {
            supported_shape_types: [ShapeType.MASK, ShapeType.POLYGON],
        } as any;

        expect(getTrackerCreationShapeType(tracker)).toBe(ShapeType.POLYGON);
    });

    it('keeps original shape data when the tracker supports it', () => {
        const tracker = {
            supported_shape_types: [ShapeType.POLYGON, ShapeType.MASK],
        } as any;
        const polygon = { type: ShapeType.POLYGON, points: [0, 0, 2, 2] };

        expect(normalizeTrackerShape(polygon, tracker)).toBe(polygon);
    });

    it('converts unsupported outputs to rectangles as a fallback', () => {
        const tracker = {
            supported_shape_types: [ShapeType.RECTANGLE],
        } as any;
        const polygon = { type: ShapeType.POLYGON, points: [0, 0, 2, 2, 1, 3] };

        const normalized = normalizeTrackerShape(polygon, tracker);

        expect(normalized.type).toBe(ShapeType.RECTANGLE);
        expect(normalized.points).toEqual([0, 0, 2, 3]);
    });
});
