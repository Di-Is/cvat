// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import {
    BaseCollectionAction,
    ActionParameterType,
    Job,
    Task,
    ShapeType,
    ObjectState,
    SerializedFunction,
    ObjectType,
} from 'cvat-core-wrapper';
import { getCore } from 'cvat-core-wrapper';

const core = getCore();
const POLL_INTERVAL_MS = 3000;
type NativeRunInput = Parameters<BaseCollectionAction['run']>[0];
type NativeRunOutput = ReturnType<BaseCollectionAction['run']] extends Promise<infer T> ? T : never;

const NO_CHANGES: NativeRunOutput = {
    created: { shapes: [], tags: [], tracks: [] },
    deleted: { shapes: [], tags: [], tracks: [] },
};

export default class NativeFunctionTrackerAction extends BaseCollectionAction {
    #function: SerializedFunction;
    #instance: Job | null;
    #targetFrame: number;
    #supportedShapes: Set<ShapeType>;
    #displayName: string;

    public constructor(nativeFunction: SerializedFunction) {
        super();
        if (nativeFunction.kind !== 'tracker') {
            throw new Error('NativeFunctionTrackerAction requires a tracker function');
        }

        this.#function = nativeFunction;
        this.#instance = null;
        this.#targetFrame = 0;
        this.#supportedShapes = new Set((nativeFunction.supported_shape_types || []) as ShapeType[]);
        this.#displayName = `AI Tracker: ${nativeFunction.name}`;
    }

    public get name(): string {
        return this.#displayName;
    }

    public get parameters(): BaseCollectionAction['parameters'] {
        return {
            'Target frame': {
                type: ActionParameterType.NUMBER,
                values: ({ instance }: { instance: Job | Task }) => {
                    if (instance instanceof Job) {
                        return [instance.startFrame, instance.stopFrame, 1].map((value) => value.toString());
                    }
                    return [0, instance.size - 1, 1].map((value) => value.toString());
                },
                defaultValue: ({ instance }: { instance: Job | Task }) => {
                    if (instance instanceof Job) {
                        return instance.stopFrame.toString();
                    }
                    return Math.max(0, instance.size - 1).toString();
                },
            },
        };
    }

    public async init(instance: Job | Task, parameters: Record<string, string>): Promise<void> {
        if (!(instance instanceof Job)) {
            throw new Error('AI tracker actions are only supported inside a job workspace');
        }

        this.#instance = instance;
        const fallbackTarget = Number(parameters['Target frame'] ?? instance.stopFrame);
        const numericTarget = Number.isNaN(fallbackTarget) ? instance.stopFrame : fallbackTarget;
        this.#targetFrame = Math.min(Math.max(numericTarget, instance.startFrame + 1), instance.stopFrame);
    }

    public async destroy(): Promise<void> {
        this.#instance = null;
    }

    public applyFilter({ collection, frameData }: Pick<NativeRunInput, 'collection' | 'frameData'>)
        : NativeRunInput['collection'] {
        const frameNumber = frameData.number;
        const filteredTracks = collection.tracks.filter((track) => {
            if (!Number.isInteger(track.id)) {
                return false;
            }

            const keyframe = [...track.shapes]
                .filter((shape) => shape.frame <= frameNumber)
                .sort((a, b) => b.frame - a.frame)[0];

            if (!keyframe || keyframe.outside) {
                return false;
            }

            return this.#supportedShapes.has(keyframe.type as ShapeType);
        });

        return {
            tracks: filteredTracks,
            shapes: [],
            tags: [],
        };
    }

    public isApplicableForObject(objectState: ObjectState): boolean {
        return (
            objectState.objectType === ObjectType.TRACK
            && this.#supportedShapes.has(objectState.shapeType as ShapeType)
        );
    }

    public async run({
        collection,
        frameData,
        onProgress,
        cancelled,
    }: NativeRunInput): Promise<NativeRunOutput> {
        if (!this.#instance) {
            throw new Error('AI tracker action is not initialized');
        }

        const trackIds = collection.tracks
            .map((track) => track.id)
            .filter((id): id is number => Number.isInteger(id) && (id as number) > 0);

        if (!trackIds.length) {
            throw new Error('Select at least one compatible track to run the SAM2 tracker');
        }

        if (frameData.number >= this.#targetFrame) {
            throw new Error('Target frame must be greater than the current frame');
        }

        if (cancelled()) {
            throw new Error('Action has been cancelled');
        }

        onProgress('Submitting SAM2 tracker request…', 5);
        const result = await this.#instance.runFunctionTrackerAction(this.#function.id, {
            frame: frameData.number,
            targetFrame: this.#targetFrame,
            trackIds,
        });

        await this.#waitForRun(result.runId, onProgress, cancelled);
        onProgress('SAM2 tracker completed', 100);
        return NO_CHANGES;
    }

    async #waitForRun(
        runId: string,
        onProgress: (message: string, progress: number) => void,
        cancelled: () => boolean,
    ): Promise<void> {
        while (true) {
            if (cancelled()) {
                throw new Error('Tracker run cancelled by user');
            }

            const summary = await core.functions.runs.get(runId);
            const percent = Math.round((summary.progress ?? 0) * 100);
            onProgress(`SAM2 tracker status: ${summary.status}`, percent);

            if (summary.status === 'done') {
                return;
            }

            if (summary.status === 'failed') {
                let message = 'SAM2 tracker run failed';
                if (summary.failed_request_id) {
                    try {
                        const failedRequest = await core.functions.requests.get(summary.failed_request_id);
                        message = failedRequest.result?.exc_info ?? message;
                    } catch {
                        // ignore secondary errors
                    }
                }
                throw new Error(message);
            }

            await this.#sleep(POLL_INTERVAL_MS);
        }
    }

    async #sleep(duration: number): Promise<void> {
        await new Promise((resolve) => setTimeout(resolve, duration));
    }
}
