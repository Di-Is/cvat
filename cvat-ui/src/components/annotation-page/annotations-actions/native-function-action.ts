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
    TrackerRunShapePayload,
    TrackerConversionMode,
} from 'cvat-core-wrapper';
import { getCore } from 'cvat-core-wrapper';
import notification from 'antd/lib/notification';
import { clampTrackerTargetFrame, getTrackerSupportedShapes } from 'utils/tracker';

const core = getCore();
const POLL_INTERVAL_MS = 3000;
const CANCEL_POLL_INTERVAL_MS = 1000;
const INLINE_SHAPE_WARNING_THRESHOLD = 50;
type NativeRunInput = Parameters<BaseCollectionAction['run']>[0];
type NativeRunOutput = Awaited<ReturnType<BaseCollectionAction['run']>>;

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
    #conversionMode: TrackerConversionMode;

    public constructor(nativeFunction: SerializedFunction) {
        super();
        if (nativeFunction.kind !== 'tracker') {
            throw new Error('NativeFunctionTrackerAction requires a tracker function');
        }

        this.#function = nativeFunction;
        this.#instance = null;
        this.#targetFrame = 0;
        this.#supportedShapes = new Set(getTrackerSupportedShapes({
            supportedShapeTypes: nativeFunction.supported_shape_types as ShapeType[] | undefined,
        }));
        this.#displayName = `AI Tracker: ${nativeFunction.name}`;
        this.#conversionMode = 'inline';
    }

    public get name(): string {
        return this.#displayName;
    }

    public get conversionMode(): TrackerConversionMode {
        return this.#conversionMode;
    }

    public setConversionMode(mode: TrackerConversionMode): void {
        this.#conversionMode = mode;
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
        const requestedTarget = Number(parameters['Target frame']);
        this.#targetFrame = clampTrackerTargetFrame(instance, Number.isNaN(requestedTarget) ? null : requestedTarget);
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
            shapes: collection.shapes.filter((shape) => this.isShapeEligible(shape, frameNumber)),
            tags: [],
        };
    }

    public isApplicableForObject(objectState: ObjectState): boolean {
        if (objectState.objectType === ObjectType.TRACK) {
            return this.#supportedShapes.has(objectState.shapeType as ShapeType);
        }

        if (objectState.objectType === ObjectType.SHAPE) {
            return (
                this.#supportedShapes.has(objectState.shapeType as ShapeType)
                && !objectState.outside
            );
        }

        return false;
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

        const uniqueTrackIds = new Set<number>();
        collection.tracks
            .map((track) => track.id)
            .filter((id): id is number => Number.isInteger(id) && (id as number) > 0)
            .forEach((id) => uniqueTrackIds.add(id));

        const shapeClientIds = Array.from(new Set(
            collection.shapes
                .filter((shape) => this.isShapeEligible(shape, frameData.number))
                .map((shape) => shape.clientID)
                .filter((id): id is number => Number.isInteger(id)),
        ));

        if (!uniqueTrackIds.size && !shapeClientIds.length) {
            throw new Error('Select at least one compatible track or shape to run the SAM2 tracker');
        }

        if (frameData.number >= this.#targetFrame) {
            throw new Error('Target frame must be greater than the current frame');
        }

        if (cancelled()) {
            throw new Error('Action has been cancelled');
        }

        if (this.#conversionMode === 'inline' && shapeClientIds.length > INLINE_SHAPE_WARNING_THRESHOLD) {
            notification.warning({
                message: 'Large inline shape selection',
                description: 'A large number of shapes will be converted inline. '
                    + 'Consider enabling “Convert shapes to tracks” before running the tracker to keep the UI responsive.',
            });
        }

        let shapePayloads: TrackerRunShapePayload[] | undefined;
        if (shapeClientIds.length) {
            onProgress('Preparing selected shapes…', 5);
            await this.#instance.annotations.save();
            if (cancelled()) {
                throw new Error('Action has been cancelled');
            }
            const shapeStates = await this.fetchShapeStates(shapeClientIds, frameData.number);
            shapePayloads = shapeClientIds.map((clientId) => {
                const state = shapeStates.get(clientId);
                if (!state) {
                    throw new Error('Unable to resolve one of the selected shapes. Please save and try again.');
                }
                return this.convertStateToShapePayload(state);
            });
        }

        onProgress('Submitting SAM2 tracker request…', 20);
        const result = await this.#instance.runFunctionTrackerAction(this.#function.id, {
            frame: frameData.number,
            targetFrame: this.#targetFrame,
            trackIds: uniqueTrackIds.size ? Array.from(uniqueTrackIds) : undefined,
            shapes: shapePayloads,
            conversionMode: this.#conversionMode,
        });

        await this.waitForRun(result.runId, onProgress, cancelled);
        if (!cancelled()) {
            onProgress('SAM2 tracker completed', 100);
            // tracker updates reside on the server, so drop the cached annotations to force a fresh fetch
            await this.#instance.annotations.clear({ reload: true });
        }
        return NO_CHANGES;
    }

    private async waitForRun(
        runId: string,
        onProgress: (message: string, progress: number) => void,
        cancelled: () => boolean,
    ): Promise<void> {
        let cancellationRequested = false;

        while (true) {
            if (cancelled() && !cancellationRequested) {
                cancellationRequested = true;
                try {
                    await core.functions.runs.cancel(runId);
                } catch (error) {
                    if (error instanceof Error) {
                        throw new Error(`Failed to cancel SAM2 tracker run: ${error.message}`);
                    }
                    throw error;
                }
            }

            const summary = await core.functions.runs.get(runId);
            const percent = Math.round((summary.progress ?? 0) * 100);

            if (summary.status === 'done') {
                return;
            }

            if (summary.status === 'cancelled') {
                notification.info({ message: 'SAM2 tracker run cancelled' });
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

            const statusMessage = cancellationRequested
                ? 'Cancelling SAM2 tracker run...'
                : `SAM2 tracker status: ${summary.status}`;
            onProgress(statusMessage, percent);

            const pollInterval = cancellationRequested ? CANCEL_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
            await this.sleep(pollInterval);
        }
    }

    private async fetchShapeStates(clientIds: number[], frame: number): Promise<Map<number, ObjectState>> {
        if (!this.#instance) {
            throw new Error('AI tracker action is not initialized');
        }

        const clientIdSet = new Set(clientIds);
        const states = await this.#instance.annotations.get(frame, false, []);

        const result = new Map<number, ObjectState>();
        for (const state of states) {
            if (state.objectType !== ObjectType.SHAPE) {
                continue;
            }

            const clientId = state.clientID;
            if (!Number.isInteger(clientId) || !clientIdSet.has(clientId as number)) {
                continue;
            }

            result.set(clientId as number, state);
        }

        return result;
    }

    private convertStateToShapePayload(state: ObjectState): TrackerRunShapePayload {
        if (state.objectType !== ObjectType.SHAPE) {
            throw new Error('Only shapes can be converted into tracker inputs');
        }

        const clientId = state.clientID;
        if (!Number.isInteger(clientId)) {
            throw new Error('Shape does not have a valid client id');
        }

        const labelId = state.label?.id;
        if (!Number.isInteger(labelId)) {
            throw new Error('Shape does not have an associated label id');
        }

        const attributes: TrackerRunShapePayload['attributes'] = Object.entries(state.attributes || {})
            .map(([specId, value]) => ({
                specId: Number(specId),
                value: typeof value === 'undefined' || value === null ? '' : String(value),
            }))
            .filter((attribute) => Number.isInteger(attribute.specId) && attribute.specId > 0);

        return {
            id: typeof state.serverID === 'number' ? state.serverID : null,
            clientId: clientId as number,
            frame: state.frame,
            labelId: labelId as number,
            shapeType: state.shapeType,
            points: Array.isArray(state.points) ? [...state.points] : [],
            zOrder: state.zOrder,
            rotation: typeof state.rotation === 'number' ? state.rotation : 0,
            group: typeof state.group?.id === 'number' ? state.group.id : null,
            occluded: state.occluded,
            outside: state.outside,
            source: state.source,
            attributes,
        };
    }

    private isShapeEligible(shape: NativeRunInput['collection']['shapes'][number], frameNumber: number): boolean {
        if (!shape) {
            return false;
        }

        if (shape.frame !== frameNumber || shape.outside) {
            return false;
        }

        if (!Number.isInteger(shape.clientID)) {
            return false;
        }

        return this.#supportedShapes.has(shape.type as ShapeType);
    }

    private async sleep(duration: number): Promise<void> {
        await new Promise((resolve) => setTimeout(resolve, duration));
    }
}
