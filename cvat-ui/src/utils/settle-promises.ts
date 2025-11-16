// Copyright (C) 2020-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

type FulfilledResult<T> = {
    status: 'fulfilled';
    value: T;
};

type RejectedResult = {
    status: 'rejected';
    reason: unknown;
};

export type SettledResult<T> = FulfilledResult<T> | RejectedResult;

export async function settlePromise<T>(promise: Promise<T>): Promise<SettledResult<T>> {
    try {
        const value = await promise;
        return {
            status: 'fulfilled',
            value,
        };
    } catch (error) {
        return {
            status: 'rejected',
            reason: error,
        };
    }
}

export async function waitForAll(promises: Promise<unknown>[]): Promise<void> {
    await Promise.all(
        promises.map((promise) => promise.catch(() => undefined)),
    );
}
