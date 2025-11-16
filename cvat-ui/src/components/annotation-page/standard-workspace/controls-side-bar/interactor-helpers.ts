import type { InteractorResults, Job, MLModel } from 'cvat-core-wrapper';
import { ModelProviders } from 'cvat-core/src/enums';

export type InteractorRequestPayload = {
    frame: number;
    pos_points: number[][];
    neg_points: number[][];
    obj_bbox: number[] | null;
    label_id: number | null;
    start_with_box?: boolean;
};

export type InteractorResponsePayload = InteractorResults & { mask_rle?: number[] };

type LambdaCallFn = (
    taskId: number,
    interactor: MLModel,
    payload: Record<string, unknown>,
) => Promise<InteractorResults>;

export interface ExecuteInteractorDeps {
    jobInstance: Pick<Job, 'id' | 'taskId' | 'runFunctionInteractor'>;
    callLambda: LambdaCallFn;
}

export async function executeInteractorRequest(
    interactor: MLModel,
    payload: InteractorRequestPayload,
    deps: ExecuteInteractorDeps,
): Promise<InteractorResponsePayload> {
    if (interactor.provider === ModelProviders.NATIVE) {
        const functionId = Number(interactor.id);

        if (!Number.isInteger(functionId) || functionId <= 0) {
            throw new Error('Native interactor id must be a positive integer');
        }

        return deps.jobInstance.runFunctionInteractor(functionId, {
            frame: payload.frame,
            posPoints: payload.pos_points,
            negPoints: Array.isArray(payload.neg_points) ? payload.neg_points : [],
            objBBox: payload.obj_bbox && payload.obj_bbox.length ? payload.obj_bbox : null,
            labelId: typeof payload.label_id === 'number' ? payload.label_id : null,
            startWithBox: Boolean(payload.start_with_box),
        });
    }

    return deps.callLambda(
        deps.jobInstance.taskId,
        interactor,
        {
            ...payload,
            job: deps.jobInstance.id,
        },
    ) as Promise<InteractorResponsePayload>;
}

export type RLEDecoder = (rle: number[], width: number, height: number) => number[];

export function normalizeInteractorResponse(
    response: InteractorResponsePayload,
    decode: RLEDecoder,
): InteractorResults & { mask: number[][] } {
    if (response.mask) {
        return response as InteractorResults & { mask: number[][] };
    }

    if (response.mask_rle && response.bounds) {
        const [left, top, right, bottom] = response.bounds;
        const width = Math.max(1, (right - left) + 1);
        const height = Math.max(1, (bottom - top) + 1);
        const flatMask = decode(response.mask_rle, width, height);
        const rows: number[][] = [];

        for (let rowIndex = 0; rowIndex < height; rowIndex++) {
            rows.push(flatMask.slice(rowIndex * width, (rowIndex + 1) * width));
        }

        return {
            ...response,
            mask: rows,
        };
    }

    throw new Error('Interactor response does not include mask data');
}
